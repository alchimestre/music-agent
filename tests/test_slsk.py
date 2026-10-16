"""The Soulseek contract: the child's environment, and reading a failure out of a normal result."""

from __future__ import annotations

import os
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


def test_the_child_is_told_what_to_share(tmp_path, creds):
    """A client that reports no shared files reads as a leecher, and peers queue or ban those. The two
    lanes are the shares; staging is not, since it holds what is still being worked on."""
    config = make_config(tmp_path)

    env = slsk.environment(config)

    assert env["SLSK_SHARE_DIRS"] == os.pathsep.join([str(config.lossless), str(config.lossy)])
    assert str(config.staging) not in env["SLSK_SHARE_DIRS"]


def test_a_child_that_goes_deaf_can_still_be_asked_where_it_stopped(tmp_path, creds):
    """2026-10-16: a child that had answered 57 searches stopped servicing every call at once — no error, no
    exit, no line of its own — and the pass ended it on four timeouts without ever learning why. With
    faulthandler armed, `kill -ABRT <pid>` writes its Python stack into the pass's own log, and py-spy (which
    needs root on macOS) names the asyncio task each unanswered call is sitting on."""
    config = make_config(tmp_path)

    env = slsk.environment(config)

    assert env["PYTHONFAULTHANDLER"] == "1"


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


def test_the_server_hanging_up_counts_as_the_session_going():
    """2026-10-16: pass 5 of a 315-track grab logged in, answered 57 searches, and then took
    `server closed connection, will not attempt to reconnect`, after which 69 calls went unanswered. Read as
    the child dying, the three retries after it went out with no wait and were refused inside the same closed
    window; read as the session going, they wait instead."""
    logs = slsk.ChildLog(log=lambda line: None)

    logs("WARNING:aioslsk.network.network:server closed connection, will not attempt to reconnect")

    assert logs.lost_session == 1
    assert logs.cannot_answer is True


def test_the_report_names_which_loss_it_was():
    """2026-10-16: three passes in a row said only that the child had been left without a session, and nothing
    said which of the three ways it was — the server hanging up, a login it refused, or the child's own state
    once either had happened. They call for different things, and this line is the only place that says so."""
    said: list[str] = []
    logs = slsk.ChildLog(log=said.append)

    logs("WARNING:aioslsk.network.network:server closed connection, will not attempt to reconnect")
    logs("ERROR:slsk_mcp:Login failed: expected login response from server but got : None")
    logs("WARNING:aioslsk.search.manager:not returning search results : no valid session was set")

    logs.report()

    assert logs.lost_session == 3
    line = next(text for text in said if "left without a session" in text)
    assert "3 times" in line
    assert "the server closed the connection" in line
    assert "the server refused the login" in line
    assert "no session to search with" in line


def test_the_reader_dying_is_told_apart_from_the_session_going():
    """2026-10-15: aioslsk's message reader raised on a frame it could not read — a short read gives it
    `struct.error: unpack_from requires a buffer of at least 293 bytes` — the task ended, and every search
    after it answered nothing while the child looked healthy.

    Counted apart from the session on purpose, because the pass answers them differently: the session belongs
    to the account and the next child loses it the same way, while this is one child's bad luck."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    logs("ERROR:aioslsk.client:unhandled exception : UnboundLocalError(\"cannot access local variable "
         "'message' where it is not associated with a value\")")
    logs("aioslsk.exceptions.MessageDeserializationError: failed to deserialize message (frames suppressed)")
    logs("INFO:aioslsk.network.network:Search complete: 0 raw results")

    assert logs.reader_died == 2
    assert logs.lost_session == 0
    assert logs.cannot_answer is True
    assert said == ["slsk: INFO:aioslsk.network.network:Search complete: 0 raw results"]


def test_the_child_own_progress_is_counted_rather_than_printed():
    """One per call, one per search, one per task cancelled on the way down. A pass over three hundred tracks
    printed hundreds of these lines, and every one of them landed in the middle of the progress bar."""
    said = []
    logs = slsk.ChildLog(log=said.append)

    logs("INFO:mcp.server.lowlevel.server:Processing request of type CallToolRequest")
    logs("INFO:slsk_mcp:Starting search query='kiasmos blurred' timeout=10")
    logs("INFO:aioslsk.tasks:cancelling task : upnp-task")
    logs("WARNING:aioslsk.network.connection:[server.slsknet.org:2416] not sending message, connection is "
         "closing / closed : FileSearch.Request(ticket=39, query='kiasmos blurred')")

    assert said == [], "not one of these is worth a line while a pass is running"
    assert logs.chatter == 4

    logs.report()
    assert len(said) == 1
    assert "left out" in said[0]


def test_a_routed_log_writes_through_the_bar_instead_of_around_it():
    """A pass draws its bar on the same terminal and rewrites it in place, so a line printed beside it leaves
    the bar shredded around the line. `route` is how the pass hands the child to the bar's own writer."""
    said = []
    logs = slsk.ChildLog(log=lambda line: said.append(f"printed: {line}"))

    logs.route(said.append)
    logs("INFO:aioslsk.shares.manager:completed scan in 1.214489 seconds")

    assert said == ["slsk: INFO:aioslsk.shares.manager:completed scan in 1.214489 seconds"]
