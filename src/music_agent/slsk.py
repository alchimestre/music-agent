"""The Soulseek side: starting the acquisition server, and reading what it answers.

Ticket 03: the server is a child process, and when it fails it does so *inside* an ordinary result —
`{"status": "error", "code": ..., "message": ...}` — rather than raising. Verified against the pinned
build: a login that could not bind its listening port came back exactly that way.
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


def environment(config: Config) -> dict[str, str]:
    """The child's environment: the credentials under the names the server reads, our own ports, and the
    ceilings on how much of it runs at once.

    The credentials arrive under whatever name this machine uses them and are *copied* into the two the
    server looks for, so `config.toml` stays free to name them the local way.
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
    # The child's own ceilings, so how much of this runs at once is one decision made here rather than a
    # default buried in a pinned build.
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
