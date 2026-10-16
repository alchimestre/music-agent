"""The Soulseek side: starting the acquisition server, and reading what it answers.

The server reports a failure *inside* an ordinary result — `{"status": "error", "code": ..., "message":
...}` — rather than raising.
"""

from __future__ import annotations

import os

from music_agent.config import ENV_HINT, Config, MissingCredential

from . import processes


class ToolError(RuntimeError):
    """The server refused a call. `code` is its own short reason, e.g. `not_authenticated`."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message


class PortBusy(RuntimeError):
    """Something on this machine holds a Soulseek listening port that is not a Soulseek client."""


# A Soulseek client, by the name its process carries. The squatter here is another harness's `slsk-mcp`,
# started on the same account and never stopped: it answers the same two ports, so the server routes search
# replies to it and a pass on this side comes back empty while looking like a quiet network.
_SLSK_NAMES = ("slsk-mcp", "slsk_mcp", "soulseek", "nicotine", "sldl")


def free_ports(config: Config, *, log=print) -> list[int]:
    """Clear the field: no other Soulseek client, and the two Soulseek ports ours.

    aioslsk registers the listening port with the server, and a client that keeps the port after its run has
    ended is not harmless: the account is the same, so the server hands every search reply to the session
    that still holds it and this pass sees nothing. A holder whose command names a Soulseek client is
    stopped; anything else holding the port is named and raised, because it is not ours to kill.

    A client that is *logged in* is stopped whether or not it holds a port. The server keeps one session per
    account, so a second client makes it close this one's connection: the pass then logs in, answers no
    search, and looks like a dead network. A harness starts such a client again the moment it is killed, so
    this is a defence that has to be repeated rather than a fix — the mount has to go.
    """
    stray = processes.stop_named(_SLSK_NAMES[:2], log=log)

    ports = (config.soulseek_listen_port, config.soulseek_obfuscated_port)
    held: list[int] = []
    for port in ports:
        for pid in processes.listeners(port):
            command = processes.command_of(pid)
            if not any(name in command.lower() for name in _SLSK_NAMES):
                raise PortBusy(
                    f"port {port} is held by {command or f'pid {pid}'} — stop it and run this again"
                )
            if pid not in held:                  # one client holds both ports, and is named once
                held.append(pid)

    if not held:
        return stray

    stubborn = processes.stop(held)
    if stubborn:
        raise PortBusy(f"pids {stubborn} would not stop, and they hold {ports}")
    log(f"stopped a stale Soulseek client holding port{'s' if len(held) > 1 else ''} "
        f"{', '.join(str(port) for port in ports)} (pid {', '.join(str(pid) for pid in held)})")
    return stray + [pid for pid in held if pid not in stray]


class ChildLog:
    """The child's stderr, made readable: what can be acted on, and none of the walls.

    aioslsk logs one warning per peer whose connection it could not broker. They arrive by the dozen, they
    say nothing a person can do, and one refused peer is one candidate that cannot be served — so they are
    counted and reported once. A traceback is a failure worth knowing about, but its frames are the wall:
    the exception keeps its line and loses the rest. Nothing is lost outright — `mcp.StdioServer` keeps the
    child's last lines and quotes them when a call fails.
    """

    NOISE = "failed to fulfill ConnectToPeer request"
    OPENING = "Traceback (most recent call last):"
    # Once per call, once per search and once per task cancelled, and not one of them says anything a person
    # acts on: the pass prints the track it is asking about, and the child's last lines are quoted when a call
    # fails. Counted and left out, so a pass over three hundred tracks does not bury its own bar in protocol.
    CHATTER = (
        "Processing request of type CallToolRequest",
        "Starting search query=",
        "cancelling task :",
        "not sending message, connection is closing",
    )
    # The ways the child loses the ability to answer. It reports each of them and recovers from none.
    # `no valid session` is the server taking the session away. The other is the child's own message reader
    # dying: `DataConnection._message_reader_loop` raises `UnboundLocalError` on a frame it cannot
    # deserialize, the task ends, and nothing says so — every search after it answers nothing, and `_connect`
    # then short-circuits on a flag only `logout()` clears, so it cannot log in again either.
    LOST_SESSION = (
        # Every spelling the child has for it: the search manager's `no valid session was set`, once per
        # search, and the shares manager's `attempted to report shares without valid session` at startup,
        # which is a login that never completed at all.
        "valid session",
        # The login itself being refused, which is the same thing said once: `Login failed: expected login
        # response from server but got : None`. The server holds one session per account, so a client that
        # ended abruptly leaves the next login answered with nothing — it is the account's state, not this
        # child's, and no child gets past it until the server lets go.
        "Login failed",
        # And the server hanging up on a session it had already granted: `server closed connection, will not
        # attempt to reconnect`, which the fork logs once when it happens and then never reconnects. Read as
        # the session's loss rather than let pass as a dead child, because the child that reports it is not
        # crashed — it is connected to nothing, and every search after the line goes unanswered. Measured
        # 2026-10-16: pass 5 of a 315-track grab answered 57 searches, took this line, and then spent 69
        # calls finding out; the three retries after it went out with no wait at all and were refused.
        "server closed connection",
    )
    READER_DIED = (
        # The other way it stops answering, and a different thing entirely: aioslsk's
        # `DataConnection._message_reader_loop` raises on a frame it cannot deserialize — a short read gives
        # it `struct.error: unpack_from requires a buffer of at least 293 bytes` — the task ends, and nothing
        # says so. Told apart from the session because the answer differs: the session belongs to the
        # account and the next child loses it the same way, while this is one child's bad luck and the next
        # one carries on from where the pass left off.
        "MessageDeserializationError",
        "cannot access local variable 'message'",
    )

    def __init__(self, log=print) -> None:
        self._log = log
        self._frames = False
        self.refused = 0
        self.lost_session = 0
        self.reader_died = 0
        self.chatter = 0

    @property
    def cannot_answer(self) -> bool:
        """Whether the child has said it can no longer answer, either way. It recovers from neither."""
        return bool(self.lost_session or self.reader_died)

    def __call__(self, line: str) -> None:
        if any(marker in line for marker in self.LOST_SESSION):
            self.lost_session += 1
            return
        if any(marker in line for marker in self.READER_DIED):
            self.reader_died += 1
            return
        if any(marker in line for marker in self.CHATTER):
            self.chatter += 1
            return
        if self.NOISE in line:
            self.refused += 1
            return
        if line.strip() == self.OPENING:
            self._frames = True
            return
        if self._frames:
            if line[:1].isspace():  # a frame, or the source line beneath it
                return
            self._frames = False
            line = f"{line.strip()} (frames suppressed)"
        self._log(f"slsk: {line}")

    def route(self, say) -> None:
        """Send what this keeps through `say` instead of printing it.

        A pass draws its progress on the same terminal, and a line printed beside a bar that is rewritten in
        place leaves the bar shredded around it; `say` is the bar's own writer, which erases itself first.
        """
        self._log = say

    def report(self) -> None:
        """What the child's noise came to, in one line each, at the end of a pass."""
        if self.lost_session:
            times = "time" if self.lost_session == 1 else "times"
            self._log(f"slsk: the child was left without a session {self.lost_session} {times} — the server "
                      f"stopped answering it, or a login never completed. Every search after the first came "
                      f"back empty")
        if self.reader_died:
            times = "time" if self.reader_died == 1 else "times"
            self._log(f"slsk: the child's message reader died {self.reader_died} {times} on a frame it could "
                      f"not read — a short read raises `struct.error` inside aioslsk — and it answers nothing "
                      f"after that")
        if self.refused:
            self._log(f"slsk: {self.refused} peer connections refused — the network, not the run")
        if self.chatter:
            self._log(f"slsk: {self.chatter} lines of the child's own progress left out of the terminal — one "
                      f"per call, one per search, one per task it cancels on the way down")


def environment(config: Config) -> dict[str, str]:
    """The child's environment: the credentials, our own ports, what it shares, and its ceilings.

    Credentials arrive under whatever names this machine uses and are copied into the two the server looks
    for, so `config.toml` stays free to name them the local way. The ceilings are set here so how much of
    this runs at once is one decision, rather than a default buried in a pinned build.
    """
    for name in (config.slsk_username_env, config.slsk_password_env):
        if not os.environ.get(name):
            raise MissingCredential(f"{name} is not set — add it to .env, or run: {ENV_HINT}")

    env = dict(os.environ)
    env["SLSK_USERNAME"] = os.environ[config.slsk_username_env]
    env["SLSK_PASSWORD"] = os.environ[config.slsk_password_env]
    env["SLSK_DOWNLOAD_DIR"] = str(config.staging)
    # The two lanes are what we share: a client that reports no shared files is a leecher, and peers queue
    # or ban those. `staging` is not one of them — it holds what is still being worked on.
    env["SLSK_SHARE_DIRS"] = os.pathsep.join(str(lane) for lane in (config.lossless, config.lossy))
    env["SLSK_LISTEN_PORT"] = str(config.soulseek_listen_port)
    env["SLSK_OBFUSCATED_PORT"] = str(config.soulseek_obfuscated_port)
    env["SLSK_MAX_CONCURRENT_DL"] = str(config.max_downloads)
    env["SLSK_MAX_CONCURRENT_SEARCH"] = str(config.max_searches)
    env["SLSK_MAX_CONCURRENT_OPS"] = str(config.max_operations)
    return env


def check(payload: object, tool: str) -> dict:
    """The payload, or a `ToolError` if the server reported a failure inside it."""
    if not isinstance(payload, dict):
        raise ToolError(tool, "unexpected", f"expected an object, got {type(payload).__name__}")
    if payload.get("status") == "error":
        raise ToolError(tool, str(payload.get("code", "?")), str(payload.get("message", "")))
    return payload
