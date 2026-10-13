"""The Soulseek side: starting the acquisition server, and reading what it answers.

The server reports a failure *inside* an ordinary result — `{"status": "error", "code": ..., "message":
...}` — rather than raising.
"""

from __future__ import annotations

import os

from music_agent.config import ENV_HINT, Config, MissingCredential


class ToolError(RuntimeError):
    """The server refused a call. `code` is its own short reason, e.g. `not_authenticated`."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message


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

    def __init__(self, log=print) -> None:
        self._log = log
        self._frames = False
        self.refused = 0

    def __call__(self, line: str) -> None:
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
        """What the refusals came to, in one line, at the end of a pass."""
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
