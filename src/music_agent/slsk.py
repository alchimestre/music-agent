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
    # The ways the child loses the ability to answer. It reports each of them and recovers from none.
    # `no valid session` is the server taking the session away. The other is the child's own message reader
    # dying: `DataConnection._message_reader_loop` raises `UnboundLocalError` on a frame it cannot
    # deserialize, the task ends, and nothing says so — every search after it answers nothing, and `_connect`
    # then short-circuits on a flag only `logout()` clears, so it cannot log in again either.
    LOST_SESSION = (
        "no valid session was set",
        "MessageDeserializationError",
        "cannot access local variable 'message'",
    )

    def __init__(self, log=print) -> None:
        self._log = log
        self._frames = False
        self.refused = 0
        self.lost_session = 0

    def __call__(self, line: str) -> None:
        if any(marker in line for marker in self.LOST_SESSION):
            self.lost_session += 1
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

    def report(self) -> None:
        """What the child's noise came to, in one line each, at the end of a pass."""
        if self.lost_session:
            times = "time" if self.lost_session == 1 else "times"
            self._log(f"slsk: the child lost its ability to answer {self.lost_session} {times} — the server "
                      f"took the session, or its own message reader died on a frame it could not read. It "
                      f"recovers from neither, so every search after the first came back empty")
        if self.refused:
            self._log(f"slsk: {self.refused} peer connections refused — the network, not the run")


def environment(config: Config) -> dict[str, str]:
    """The child's environment: the credentials, our own ports, and its ceilings.

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
