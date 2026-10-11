"""The Soulseek contract: the child's environment, and reading a failure out of a normal result."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import slsk


def make_config(tmp_path: Path, credentials: str = "") -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(
        '[paths]\nstaging = "staging"\nstate = "state"\n'
        '[soulseek]\nlisten_port = 20000\nobfuscated_port = 20001\n'
        + credentials,
        encoding="utf-8",
    )
    return config_module.load(path)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SLSK_USERNAME", "someone")
    monkeypatch.setenv("SLSK_PASSWORD", "a secret")


def test_the_environment_carries_what_the_server_reads(tmp_path, creds):
    config = make_config(tmp_path)
    env = slsk.environment(config)

    assert env["SLSK_USERNAME"] == "someone"
    assert env["SLSK_PASSWORD"] == "a secret"
    assert env["SLSK_DOWNLOAD_DIR"] == str(config.staging)
    assert (env["SLSK_LISTEN_PORT"], env["SLSK_OBFUSCATED_PORT"]) == ("20000", "20001")


def test_credentials_are_renamed_to_what_the_server_expects(tmp_path, monkeypatch):
    """config.toml may call them whatever this machine calls them; the child gets the real names."""
    monkeypatch.setenv("MY_SLSK_USER", "someone")
    monkeypatch.setenv("MY_SLSK_PASSWORD", "a secret")
    config = make_config(
        tmp_path,
        '[credentials]\nsoulseek_user = "MY_SLSK_USER"\nsoulseek_password = "MY_SLSK_PASSWORD"\n',
    )

    env = slsk.environment(config)

    assert env["SLSK_USERNAME"] == "someone"
    assert env["SLSK_PASSWORD"] == "a secret"


def test_a_missing_credential_names_the_variable(tmp_path, creds, monkeypatch):
    monkeypatch.delenv("SLSK_PASSWORD")
    with pytest.raises(slsk.MissingCredential, match="SLSK_PASSWORD"):
        slsk.environment(make_config(tmp_path))


def test_an_error_inside_an_ordinary_result_raises_with_the_servers_code():
    payload = {"status": "error", "code": "not_authenticated", "message": "Login failed"}

    with pytest.raises(slsk.ToolError) as caught:
        slsk.check(payload, "search")

    assert caught.value.code == "not_authenticated"
    assert caught.value.tool == "search"


def test_a_normal_payload_passes_straight_through():
    payload = {"results": []}
    assert slsk.check(payload, "search") is payload


def test_a_payload_that_is_not_an_object_is_reported():
    with pytest.raises(slsk.ToolError, match="unexpected"):
        slsk.check("some text", "search")
