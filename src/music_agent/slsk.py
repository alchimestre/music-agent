"""The Soulseek side: the daemon's control socket, and what it answers.

`soulseek-rs daemon` is one process that holds the account and speaks newline-delimited JSON-RPC over a
Unix socket. Everything the pipeline asks of the network goes through it — a search, a download, the
transfers in flight — and the daemon owns the session, the shares and the files. Nothing is scraped from
another process's stderr, and a pass that ends leaves the session and any unfinished transfers where they are.

The daemon is started here when the socket answers nothing, and left running afterwards: a login costs the
network's timeout, and a transfer that survives one pass is resumed by the next.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import processes
from .config import ENV_HINT, Config, MissingCredential

# The daemon's control protocol, as `soulseek-rs` 19.1.0 speaks it. It is bumped whenever a change would
# break a client written against the old shape, and a mismatch is refused rather than guessed at.
PROTOCOL = 2
# A connection that has not authenticated is closed after thirty seconds; a daemon that has to build itself
# the first time is slower than that.
STARTUP_TIMEOUT = 45.0
# A local round trip. The daemon answers from its own memory except when starting a transfer.
CALL_TIMEOUT = 15.0
# The daemon caps the lines it *accepts* at 1 MiB; its own answers are not capped. A broad search answer —
# "aphex twin" — measured 1.6 MB in one line on 2026-10-17, so the limit here is the reply's, not the
# request's.
_LINE = 8 << 20


class PortBusy(RuntimeError):
    """Something else holds the Soulseek listening port and would not stop."""


class DaemonError(RuntimeError):
    """The daemon refused a call, or the socket stopped answering."""

    def __init__(self, method: str, message: str, exit_status: int | None = None) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.exit_status = exit_status


def state_dir(config: Config) -> Path:
    """Where the daemon keeps its socket, token and durable transfer state."""
    if config.soulseek_state_dir:
        return Path(config.soulseek_state_dir).expanduser()
    return Path(os.environ.get("SOULSEEK_STATE_DIR")
                or Path.home() / ".local" / "share" / "soulseek-rs" / "state")


def socket_path(config: Config) -> Path:
    return state_dir(config) / "daemon.sock"


def environment(config: Config) -> dict[str, str]:
    """The daemon's environment: the credentials, the one listening port, and its ceilings.

    The shares and the download directory are per session, so they are set over the socket once the daemon
    is up — a folder added at runtime is served without a restart.
    """
    username = os.environ.get(config.slsk_username_env)
    password = os.environ.get(config.slsk_password_env)
    if not username:
        raise MissingCredential(f"{config.slsk_username_env} is not set — add it to .env, or run: "
                                f"{ENV_HINT}")
    if not password:
        raise MissingCredential(f"{config.slsk_password_env} is not set — add it to .env, or run: "
                                f"{ENV_HINT}")
    env = dict(os.environ)
    env["SOULSEEK_USERNAME"] = username
    env["SOULSEEK_PASSWORD"] = password
    env["SOULSEEK_STATE_DIR"] = str(state_dir(config))
    env["SOULSEEK_LISTENER_PORT"] = str(config.soulseek_listen_port)
    env["SOULSEEK_MAX_CONCURRENT_DOWNLOADS"] = str(config.max_downloads)
    # The server's own default is 10s, which cuts the window short before the pass has read a slow peer's
    # answer; the pass decides its own window and reads once, so the daemon's is set to match.
    env["SOULSEEK_SEARCH_TIMEOUT"] = "10"
    return env


# The clients that squat the account and the port. `soulseek-rs` is deliberately absent: the daemon is ours,
# and this function is what clears the field *before* it is started.
SQUATTERS = ("nicotine", "sldl", "soulseekqt", "soulseek-qt")


def free_ports(config: Config, *, log=print) -> list[int]:
    """Clear the field: no other Soulseek client, and the listening port ours.

    A second client on the same account makes the server hand it every search reply, so this pass asks and
    hears nothing. The processes stopped are another harness's clients, restarted on demand; anything else
    on the port is named and left alone.
    """
    stopped = []
    for name in SQUATTERS:
        pids = [pid for pid in processes.named(name) if pid != os.getpid()]
        if pids:
            stubborn = processes.stop(pids)
            if stubborn:
                raise PortBusy(f"pids {stubborn} ({name}) would not stop")
            log(f"stopped {name} (pid {', '.join(str(pid) for pid in pids)})")
            stopped.extend(pids)

    port = config.soulseek_listen_port
    held = [pid for pid in processes.listeners(port) if pid != os.getpid()]
    if not held:
        return stopped

    stubborn = processes.stop(held)
    if stubborn:
        raise PortBusy(f"pids {stubborn} would not stop, and they hold {port}")
    log(f"stopped a stale Soulseek client holding port {port} "
        f"(pid {', '.join(str(pid) for pid in held)})")
    return stopped + held


class Daemon:
    """The daemon's socket: one request at a time, notifications skipped, replies matched by id."""

    def __init__(self, config: Config, *, log=print) -> None:
        self._config = config
        self._log = log
        self._sock: socket.socket | None = None
        self._buffer = b""
        # Re-entrant: `call` closes the socket on a read error while holding it, and `close` takes it again.
        self._lock = threading.RLock()         # one request in flight, so replies cannot cross
        self._searches: dict[str, threading.Lock] = {}
        self._search_lock = threading.Lock()   # guards `_searches`
        self._ids = iter(range(1, 1 << 30))
        self.username = ""

    # ── lifecycle ────────────────────────────────────────────────────────────

    def ensure(self) -> None:
        """Connect, starting the daemon first when nothing answers on the socket."""
        if self._sock is not None:
            return
        if self._open():
            return
        self.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not self._open():
            if time.monotonic() > deadline:
                raise DaemonError("start", f"no daemon on {socket_path(self._config)} after "
                                           f"{STARTUP_TIMEOUT:g}s — see "
                                           f"{state_dir(self._config) / 'daemon.log'}")
            time.sleep(0.25)

    def start(self) -> None:
        """Clear the port, then start the daemon detached, its own log beside its state."""
        free_ports(self._config, log=self._log)
        directory = state_dir(self._config)
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "daemon.log"
        with open(log, "ab") as handle:
            try:
                subprocess.Popen(
                    list(self._config.soulseek_command),
                    env=environment(self._config),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=handle,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise DaemonError("start", f"cannot run {self._config.soulseek_command[0]!r}: {exc}") from exc
        self._log(f"started the Soulseek daemon — its log is {log}")

    def stop(self) -> None:
        """Ask the daemon to shut down, then let go of the socket."""
        try:
            self.call("daemon.stop", {})
        except (DaemonError, OSError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
                    self._buffer = b""

    def __enter__(self) -> "Daemon":
        self.ensure()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # ── the wire ─────────────────────────────────────────────────────────────

    def _open(self) -> bool:
        path = socket_path(self._config)
        if not path.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
        except OSError:
            sock.close()
            return False
        self._sock = sock
        self._buffer = b""
        try:
            answer = self.call("auth", {"protocol": PROTOCOL}, timeout=STARTUP_TIMEOUT)
        except DaemonError as error:
            self.close()
            raise
        if answer.get("protocol") != PROTOCOL:
            self.close()
            raise DaemonError("auth", f"the daemon speaks protocol {answer.get('protocol')}, this client "
                                      f"{PROTOCOL} — pin the binary and the client to one version")
        self.username = str(answer.get("username") or "")
        self._check_account()
        return True

    def _check_account(self) -> None:
        """The daemon's account is the one this machine configured; another one would mix two libraries."""
        wanted = os.environ.get(self._config.slsk_username_env)
        if wanted and self.username and wanted.lower() != self.username.lower():
            raise DaemonError("auth", f"the daemon is logged in as {self.username!r}, and this machine's "
                                      f"{self._config.slsk_username_env} names {wanted!r} — stop it with "
                                      f"`soulseek-rs daemon stop`, then run the pass again")

    def call(self, method: str, params: dict | None = None,
             timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        """One call, its answer, and any notification the daemon pushed meanwhile."""
        with self._lock:
            if self._sock is None:
                raise DaemonError(method, "the daemon's socket is closed")
            message_id = next(self._ids)
            line = json.dumps({"jsonrpc": "2.0", "id": message_id, "method": method,
                               "params": params or {}}) + "\n"
            try:
                self._sock.sendall(line.encode("utf-8"))
            except OSError as error:
                self.close()
                raise DaemonError(method, f"the socket closed on the write: {error}") from error
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DaemonError(method, f"no answer within {timeout:g}s")
                try:
                    self._sock.settimeout(remaining)
                    message = json.loads(self._readline())
                except socket.timeout:
                    raise DaemonError(method, f"no answer within {timeout:g}s") from None
                except OSError as error:
                    self.close()
                    raise DaemonError(method, f"the socket closed on the read: {error}") from error
                except json.JSONDecodeError:
                    continue                     # a line that is not ours is not an answer either way
                if message.get("id") != message_id:
                    continue                     # an event, or an answer to a call already given up on
                error = message.get("error")
                if error is not None:
                    data = error.get("data") or {}
                    raise DaemonError(method, str(error.get("message") or "refused"), data.get("exit"))
                return message.get("result") or {}

    def _readline(self) -> str:
        assert self._sock is not None
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("end of file")
            self._buffer += chunk
            if len(self._buffer) > _LINE:
                self._buffer = b""
                raise OSError("a line larger than the daemon's own limit")
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("utf-8", "replace")

    # ── what the pipeline asks ───────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """The daemon's own view: connected, logged in, shares, and the last session loss."""
        return self.call("daemon.status", {})


    def session_lost(self) -> bool:
        """Whether the account's session was taken by another login.

        The daemon reports two kinds. `disconnected` is a dropped connection, which it logs back in from on
        its own; `displaced` is another client logging in as this account, and no retry of ours can win that.
        Only the second stops a pass.
        """
        return self.status().get("session_loss") == "displaced"

    def set_dir(self, directory: Path | str) -> None:
        self.call("download.set_dir", {"directory": str(directory)})

    def share(self, directories: Iterable[Path | str]) -> dict[str, Any]:
        """Serve these folders at once, without a restart. The answer is the index it built."""
        return self.call("shares.set", {"directories": [str(path) for path in directories]})

    def search(self, query: str, *, timeout: float) -> list[dict[str, Any]]:
        """One search: the query on the wire, the window waited here, every answer read once.

        The daemon returns immediately and collects answers for as long as they come; the window is the
        client's, because a client's search must not be time the daemon spends not answering anyone else.
        Two records with the same query take turns rather than sharing one window.
        """
        with self._search_lock:
            lock = self._searches.setdefault(query, threading.Lock())
        with lock:
            self._forget(query)
            try:
                self.call("search.start", {"query": query})
                time.sleep(timeout)
                payload = self.call("search.results", {"query": query})
            finally:
                self._forget(query)
        return _files(payload.get("results") or [])

    def _forget(self, query: str) -> None:
        try:
            self.call("search.forget", {"query": query})
        except DaemonError:
            pass                                  # nothing was remembered under that query

    def download(self, user: str, filename: str, *, size: int = 0,
                 metadata: dict | None = None) -> dict[str, Any]:
        payload = self.call("download.start", {"username": user, "filename": filename,
                                               "size": int(size or 0),
                                               "metadata": metadata or {}})
        return payload.get("download") or {}

    def downloads(self) -> dict[tuple[str, str], dict[str, Any]]:
        payload = self.call("download.list", {})
        return {(str(item.get("username")), str(item.get("filename"))): item
                for item in payload.get("downloads") or []}

    def cancel(self, user: str, filename: str) -> None:
        self.call("download.cancel", {"username": user, "filename": filename})


def _files(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One flat offer per file, in the shape `acquire.candidate_of` reads.

    The daemon groups a peer's answers under the peer, with the protocol's attribute codes beside each file:
    0 is the bitrate, 1 the duration in seconds.
    """
    offers = []
    for peer in results:
        for item in peer.get("files") or []:
            attribs = item.get("attribs") or {}
            offers.append({
                "username": peer.get("username") or "",
                "filename": item.get("name") or "",
                "filesize": item.get("size"),
                "bitrate": attribs.get("0"),
                "duration_sec": attribs.get("1"),
                "has_free_slots": int(peer.get("slots") or 0) > 0,
                "avg_speed": peer.get("speed"),
            })
    return offers


def local_path(download: dict[str, Any]) -> str:
    """Where the daemon puts a finished file: its directory and the peer's basename, flattened."""
    filename = str(download.get("filename") or "")
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    directory = str(download.get("download_directory") or "")
    return str(Path(directory) / name) if directory and name else ""
