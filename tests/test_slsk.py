"""The Soulseek contract: the child's environment, and reading a failure out of a normal result."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import slsk


def make_config(tmp_path: Path, credentials: str = "") -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(
        '[paths]\nstaging = "staging"\nlossless = "lossless"\nlossy = "lossy"\nstate = "state"\n'
        + credentials,
        encoding="utf-8",
    )
    return config_module.load(path)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SLSK_USERNAME", "someone")
    monkeypatch.setenv("SLSK_PASSWORD", "a secret")


def test_the_environment_carries_what_the_server_reads(tmp_path, creds, monkeypatch):
    """Including the listening ports, which come from the environment: a machine whose forwarded port is
    not aioslsk's default has to bind the one peers can actually reach."""
    monkeypatch.setenv("SLSK_LISTEN_PORT", "41000")
    monkeypatch.setenv("SLSK_OBFUSCATED_PORT", "41001")
    config = make_config(tmp_path)
    env = slsk.environment(config)

    assert env["SLSK_USERNAME"] == "someone"
    assert env["SLSK_PASSWORD"] == "a secret"
    assert env["SLSK_DOWNLOAD_DIR"] == str(config.staging)
    assert (env["SLSK_LISTEN_PORT"], env["SLSK_OBFUSCATED_PORT"]) == ("41000", "41001")


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


def test_the_ceilings_on_concurrency_reach_the_child(tmp_path, monkeypatch):
    """2026-10-12: how much of this runs at once is decided in config.toml, not by the pinned build's own
    defaults — which are 3 transfers, 4 searches, and a single operation that serialises every search."""
    from music_agent import config as config_module
    from music_agent import slsk as slsk_module

    monkeypatch.setenv("SLSK_USERNAME", "someone")
    monkeypatch.setenv("SLSK_PASSWORD", "secret")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[soulseek]\nmax_downloads = 20\nmax_searches = 8\nmax_operations = 8\nworkers = 20\n",
        encoding="utf-8",
    )

    env = slsk_module.environment(config_module.load(config_path))

    assert env["SLSK_MAX_CONCURRENT_DL"] == "20"
    assert env["SLSK_MAX_CONCURRENT_SEARCH"] == "8"
    assert env["SLSK_MAX_CONCURRENT_OPS"] == "8"


def test_the_wall_of_peer_refusals_is_counted_rather_than_printed():
    """2026-10-13: aioslsk logs one warning per peer it could not connect to. They arrive by the dozen,
    none of them is actionable, and one refused peer is one candidate that cannot be served."""
    said = []
    logs = slsk.ChildLog(log=said.append)
    wall = (
        "WARNING:aioslsk.network.network:failed to fulfill ConnectToPeer request : "
        "ConnectToPeer.Response(username='oyeattache', typ='P', ip='68.54.115.113', port=62749, ticket=14174,"
        " privileged=False, obfuscated_port_amount=1, obfuscated_port=62750) : "
        "PeerConnectionError('failed connect on user request')"
    )

    for _ in range(12):
        logs(wall)
    assert said == []

    logs.report()
    assert said == ["slsk: 12 peer connections refused — the network, not the run"]


def test_a_traceback_keeps_its_exception_and_loses_its_frames():
    """2026-10-13: one discarded response made aioslsk log an ERROR and five lines of frames."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    for line in [
        "ERROR:aioslsk.network.connection:[server.slsknet.org:2416] error during callback : "
        "GetPeerAddress.Response(username='Kalesony', ip='77.236.3.89', port=49488)",
        "Traceback (most recent call last):",
        '  File "/home/user/.cache/uv/archive-v0/oZfObPwQF1g5rGWw/lib/python3.13/site-packages/'
        'aioslsk/network/connection.py", line 550, in _perform_message_callback',
        "    await self.network.on_message_received(message, self)",
        "asyncio.InvalidStateError: invalid state",
    ]:
        logs(line)

    assert len(said) == 2
    assert said[0].startswith("slsk: ERROR:aioslsk.network.connection")
    assert said[1] == "slsk: asyncio.InvalidStateError: invalid state (frames suppressed)"


def test_the_line_that_says_the_network_went_quiet_survives_the_filter():
    """The accounting leans on that one line, so nothing may swallow it — and indentation alone is not a frame."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    logs("INFO:aioslsk.network.network:Search complete: 0 raw results")
    logs("    an indented line that is not a traceback")

    assert said == [
        "slsk: INFO:aioslsk.network.network:Search complete: 0 raw results",
        "slsk:     an indented line that is not a traceback",
    ]


def test_the_child_saying_it_has_no_session_is_counted_rather_than_repeated():
    """It says this once per search and there can be a thousand of them; the count is what the pass needs,
    and the line on its own says nothing a person can do anything about."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    for _ in range(3):
        logs("WARNING:aioslsk.search.manager:not returning search results : no valid session was set")

    assert logs.lost_session == 3
    assert said == []

    logs.report()
    assert len(said) == 1
    assert "session" in said[0]


def test_a_login_that_never_completed_counts_as_the_loss_too():
    """The shares manager says it at startup, before a single search: `attempted to report shares without
    valid session`. That is a login that never became a session, and every search after it answers nothing."""
    logs = slsk.ChildLog(log=lambda line: None)

    logs("WARNING:aioslsk.shares.manager:attempted to report shares without valid session")

    assert logs.lost_session == 1


def test_the_reader_dying_counts_as_the_same_loss_as_the_session():
    """2026-10-15: aioslsk's message reader raised `UnboundLocalError` on a frame it could not deserialize,
    the task ended, and every search after it answered nothing while the child looked healthy. It is the
    same loss as the server taking the session, and it needs the same answer: a fresh child."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    logs('ERROR:aioslsk.client:unhandled exception : UnboundLocalError("cannot access local variable '
         "'message' where it is not associated with a value\")")
    logs("aioslsk.exceptions.MessageDeserializationError: failed to deserialize message (frames suppressed)")
    logs("INFO:aioslsk.network.network:Search complete: 0 raw results")

    assert logs.lost_session == 2
    assert said == ["slsk: INFO:aioslsk.network.network:Search complete: 0 raw results"]
