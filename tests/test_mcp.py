"""The stdio client, exercised against a stand-in server: no Soulseek, no network.

The awkward paths are the point of these tests. A dead server, a silent one, a question from the other
side and junk on stdout all have to end in a clear outcome rather than a hang.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from music_agent.mcp import ServerError, StdioServer

FAKE = str(Path(__file__).parent / "fake_server.py")


def open_server(scenario: str, **kwargs) -> StdioServer:
    return StdioServer([sys.executable, FAKE, scenario], **kwargs)


def test_it_shakes_hands_and_lists_tools():
    with open_server("normal") as server:
        assert server.server_info["name"] == "fake-soulseek"
        assert server.tools() == ["search", "download"]


def test_a_tool_result_comes_back_as_data():
    with open_server("normal") as server:
        results = server.call("search", {"query": "kiasmos blurred"})
    assert results[0]["username"] == "vinyl_only"
    assert results[0]["duration_sec"] == 448.0


def test_a_structured_payload_is_used_when_offered():
    with open_server("structured") as server:
        assert server.call("search") == {"results": _results()}


def test_it_answers_the_servers_question_rather_than_hanging():
    """The stand-in withholds the answer until it hears back, so dropping the question would time out."""
    log: list[str] = []
    with open_server("noisy", on_log=log.append) as server:
        assert server.call("search") is not None
    assert any("notification" in line for line in log)
    assert any("roots/list" in line for line in log)


def test_a_flagged_failure_raises():
    with open_server("is_error") as server:
        with pytest.raises(ServerError, match="the peer refused"):
            server.call("search")


def test_a_jsonrpc_error_raises():
    with open_server("refuses") as server:
        with pytest.raises(ServerError, match="no such tool"):
            server.call("search")


def test_a_server_that_dies_is_reported_at_once():
    with open_server("dies") as server:
        with pytest.raises(ServerError, match="the server exited"):
            server.call("search")


def test_a_silent_server_times_out():
    with open_server("silent") as server:
        with pytest.raises(ServerError, match="no answer within"):
            server.call("search", timeout=1.0)


def test_a_mute_server_times_out_on_the_handshake():
    with pytest.raises(ServerError, match="no answer within"):
        open_server("mute", startup_timeout=1.0)


def test_junk_on_stdout_does_not_derail_the_exchange():
    with open_server("garbage") as server:
        assert server.call("search")[0]["extension"] == "flac"


def test_a_command_that_cannot_be_run_names_the_command():
    with pytest.raises(ServerError, match="no-such-server-binary"):
        StdioServer(["no-such-server-binary-xyz"], startup_timeout=5)


def test_closing_twice_is_harmless():
    server = open_server("normal")
    server.close()
    server.close()


def _results() -> list[dict]:
    return [
        {
            "id": "vinyl_only:Music\\Kiasmos - Blurred (Extended Mix).flac",
            "username": "vinyl_only",
            "filename": "Music\\Kiasmos - Blurred (Extended Mix).flac",
            "filesize": 82_000_000,
            "extension": "flac",
            "bitrate": None,
            "duration_sec": 448.0,
            "has_free_slots": True,
            "queue_size": 0,
        }
    ]
