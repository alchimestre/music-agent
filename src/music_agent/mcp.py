"""Spawning an MCP server and speaking to it over stdio.

Newline-delimited JSON-RPC 2.0 on the child's stdin and stdout, with the server's logging on stderr. Calls
answer each other out of order, so each answer is routed to the call that asked for it by its id.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

# What we announce ourselves as. A server that does not support this revision answers with one it does.
PROTOCOL_VERSION = "2024-11-05"

_STARTUP_TIMEOUT = 60.0     # uvx may have to build the server the first time it runs
_CALL_TIMEOUT = 15.0
_GRACE = 0.5                # a server that exits when its input closes does so at once
_KILL_GRACE = 2.0
_STDERR_TAIL = 40           # lines kept to explain a failure

# What the child says when something is wrong with it. Its ordinary log is a line per peer it could not
# reach, so without this the lines that matter are the ones least likely to still be in the tail.
_ERRORISH = re.compile(r"ERROR|Error|Traceback|exception|Exception|critical|CRITICAL")


class ServerError(RuntimeError):
    """The server would not start, died, took too long, or refused a call."""


def _payload(result: dict) -> Any:
    """The tool's return value, whichever of the two ways the server chose to carry it."""
    if "structuredContent" in result:
        return result["structuredContent"]
    blocks = result.get("content") or []
    text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class StdioServer:
    """An MCP server running as a child process, called synchronously."""

    def __init__(
        self,
        argv: Sequence[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_log: Callable[[str], None] | None = None,
        startup_timeout: float = _STARTUP_TIMEOUT,
    ) -> None:
        self._on_log = on_log
        self._waiters: dict[int, queue.Queue] = {}   # one queue per call in flight
        self._state = threading.Lock()               # guards the id counter and the waiter table
        self._writes = threading.Lock()              # stdin is one stream, so writes take turns
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._next_id = 0
        self._closed = False
        self.server_info: dict = {}

        try:
            self._proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ServerError(f"cannot run {argv[0]!r}: {exc}") from exc

        # The group is remembered now and not looked up later: the wrapper is often reaped the moment its
        # input closes, and `getpgid` on a dead pid fails — while the server it started is still in the
        # group, still holding the ports.
        self._pgid = self._proc.pid
        if hasattr(os, "getpgid"):
            try:
                self._pgid = os.getpgid(self._proc.pid)
            except OSError:
                pass

        # stdout carries the protocol and signals its end; stderr only ever produces log lines.
        self._start_reader(self._proc.stdout, self._read_message, terminal=True)
        self._start_reader(self._proc.stderr, self._read_log, terminal=False)
        self.server_info = self._handshake(startup_timeout)

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _start_reader(self, stream: Any, sink: Callable[[str], None], terminal: bool) -> None:
        if stream is None:
            return
        threading.Thread(target=self._pump, args=(stream, sink, terminal), daemon=True).start()

    def _pump(self, stream: Any, sink: Callable[[str], None], terminal: bool) -> None:
        for line in stream:
            sink(line)
        if terminal:
            # End of input: the server is gone, and every call still waiting has to hear it.
            with self._state:
                waiting = list(self._waiters.values())
            for waiter in waiting:
                waiter.put(None)

    def _read_message(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self._log(f"ignoring unparseable output: {line[:160]}")
            return
        if "id" not in message:                    # a notification: nothing here asks for one
            self._log(f"notification {message.get('method')}")
            return
        if "method" in message:                    # a question, which must not hang
            self._log(f"answering the server's {message.get('method')} with an empty result")
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            return
        with self._state:
            waiter = self._waiters.get(message["id"])
        if waiter is None:
            self._log(f"an answer to nothing: request {message['id']}")
            return
        waiter.put(message)

    def _read_log(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self._stderr.append(line)
            self._log(line)

    def _log(self, line: str) -> None:
        if self._on_log is not None:
            self._on_log(line)

    def _tail(self) -> str:
        """What the child last said, with its own errors preferred over its chatter.

        The child logs a line per peer it cannot reach, so a raw tail is twenty lines of the same warning and
        the traceback that killed it scrolls away. The lines that name an error are quoted first, and the raw
        tail follows only when there is nothing else.
        """
        interesting = [line for line in self._stderr if _ERRORISH.search(line)]
        if interesting:
            return " | ".join(interesting[-_STDERR_TAIL:])
        return " | ".join(self._stderr) or "the server said nothing"

    def _send(self, message: dict) -> None:
        try:
            assert self._proc.stdin is not None
            with self._writes:
                self._proc.stdin.write(json.dumps(message) + "\n")
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ServerError(f"the server closed its input: {self._tail()}") from exc

    def _handshake(self, timeout: float) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "music-agent", "version": "0.1"},
            },
            timeout=timeout,
        )
        self.notify("notifications/initialized")
        chosen = result.get("protocolVersion")
        if chosen and chosen != PROTOCOL_VERSION:
            self._log(f"the server chose protocol {chosen}")
        return result.get("serverInfo", {})

    # ── the protocol ─────────────────────────────────────────────────────────

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float = _CALL_TIMEOUT) -> dict:
        """Send a request and wait for *its* answer, whatever else the server is answering meanwhile."""
        waiter: queue.Queue = queue.Queue()
        with self._state:
            self._next_id += 1
            message_id = self._next_id
            self._waiters[message_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ServerError(f"{method}: no answer within {timeout:g}s")
                try:
                    message = waiter.get(timeout=remaining)
                except queue.Empty:
                    raise ServerError(f"{method}: no answer within {timeout:g}s") from None

                if message is None:
                    raise ServerError(f"{method}: the server exited — {self._tail()}")
                if "error" in message:
                    error = message["error"] or {}
                    raise ServerError(f"{method}: {error.get('message', error)}")
                return message.get("result") or {}
        finally:
            with self._state:
                self._waiters.pop(message_id, None)

    def tools(self) -> list[str]:
        return [tool["name"] for tool in self.request("tools/list").get("tools", [])]

    def call(self, tool: str, arguments: dict | None = None, timeout: float = _CALL_TIMEOUT) -> Any:
        """Call a tool and return its payload, raising if the server flagged the call as failed.

        A tool may also report a failure *inside* an otherwise ordinary result, so the caller still has to
        read the payload for that.
        """
        result = self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout
        )
        if result.get("isError"):
            raise ServerError(f"{tool} failed: {_payload(result)}")
        return _payload(result)

    # ── lifetime ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the input, then insist — on the child's whole process group. Safe to call twice.

        The command we run is usually a wrapper (`uvx` around the real server), and the wrapper exiting does
        not take the server with it. A server left behind holds the Soulseek listening ports, and the next
        pass then logs in as a second client: every search comes back empty. So the group goes, every time.
        """
        if self._closed:
            return
        self._closed = True

        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=_GRACE)
            self._signal_group(signal.SIGKILL)
            return
        except subprocess.TimeoutExpired:
            pass

        self._signal_group(signal.SIGTERM)
        try:
            self._proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        """Signal the child and everything it started. The ports belong to the process at the bottom."""
        if not hasattr(os, "killpg"):
            return
        try:
            os.killpg(self._pgid, sig)
        except OSError:                          # every member is gone, or they are not ours to signal
            pass

    def __enter__(self) -> StdioServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
