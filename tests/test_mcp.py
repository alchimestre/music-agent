"""The stdio client, exercised against a stand-in server: no Soulseek, no network.

The awkward paths are the point of these tests. A dead server, a silent one, a question from the other
side and junk on stdout all have to end in a clear outcome rather than a hang.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

import fake_server
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
        payload = server.call("search", {"query": "kiasmos blurred"})
    assert payload["count"] == 1
    assert payload["results"][0]["username"] == "vinyl_only"
    assert payload["results"][0]["duration_sec"] == 448.0


def test_a_structured_payload_is_used_when_offered():
    with open_server("structured") as server:
        assert server.call("search") == {"results": fake_server.DEFAULT_RESULTS}


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
        assert server.call("search")["results"][0]["extension"] == "flac"


def test_a_command_that_cannot_be_run_names_the_command():
    with pytest.raises(ServerError, match="no-such-server-binary"):
        StdioServer(["no-such-server-binary-xyz"], startup_timeout=5)


def test_closing_twice_is_harmless():
    server = open_server("normal")
    server.close()
    server.close()


WRAPPER = '''
import os, subprocess, sys

# A stand-in for `uvx`: it starts something of its own, says which, then becomes the server. What was started
# stays in this process's group — which is the point.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
sys.stderr.write("child %d\\n" % child.pid)
sys.stderr.flush()
os.execv(sys.executable, [sys.executable, {fake!r}, "normal"])
'''


def test_closing_takes_the_whole_process_group(tmp_path):
    """2026-10-14: the command is usually a wrapper — `uvx` around the server — and killing the wrapper alone
    leaves the server holding the Soulseek ports. A client left there answers the same account, so the next
    pass sees an empty network instead of a port conflict. The child's group goes, so its own children go."""
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(WRAPPER.format(fake=FAKE), encoding="utf-8")
    seen: list[str] = []

    server = StdioServer([sys.executable, str(wrapper)], on_log=seen.append)
    grandchild = int(next(line for line in seen if line.startswith("child ")).split()[1])
    try:
        server.close()
        for _ in range(50):                         # SIGKILL is not instant, and the pid outlives the process
            if not alive(grandchild):
                return
            time.sleep(0.1)
        pytest.fail("the server outlived the wrapper that started it")
    finally:
        # Whether it died where it should or not, nothing here is left running.
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


DEFERRING = '''
import json, sys

pending = []


def out(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


def answer(message_id, payload):
    out({"jsonrpc": "2.0", "id": message_id,
         "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        out({"jsonrpc": "2.0", "id": message["id"],
             "result": {"protocolVersion": "2024-11-05", "serverInfo": {}}})
        continue
    if method == "tools/list":
        out({"jsonrpc": "2.0", "id": message["id"],
             "result": {"tools": [{"name": "first"}, {"name": "second"}]}})
        continue
    name = (message.get("params") or {}).get("name")
    if not name:                         # the notifications/initialized handshake, which asks nothing
        continue
    if name == "first":
        pending.append(message)          # held back, so its answer lands after the next call's
        continue
    answer(message["id"], {"which": name})
    for held in pending:
        answer(held["id"], {"which": "first"})
    pending.clear()
'''


def test_answers_are_routed_when_they_come_back_out_of_order(tmp_path):
    """Calls in flight together, answered in the wrong order: the first call must still get its own
    answer. A shared queue would let one call read the other's and leave it waiting for one already
    spent — which is what the acquisition would hit the moment two tracks are searched at once."""
    import threading
    import time

    script = tmp_path / "deferring.py"
    script.write_text(DEFERRING, encoding="utf-8")

    with StdioServer([sys.executable, str(script)], startup_timeout=10) as server:
        first: dict = {}
        asker = threading.Thread(target=lambda: first.update(server.call("first", timeout=10)))
        asker.start()
        time.sleep(0.2)                       # so the two calls are genuinely in flight together
        second = server.call("second", timeout=10)
        asker.join(timeout=10)

    assert second == {"which": "second"}
    assert first == {"which": "first"}
