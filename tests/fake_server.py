"""A stand-in for the acquisition server: enough of the protocol to exercise the client, no Soulseek.

`test_mcp.py` runs this as a child process exactly as the real server is run. The scenario is the piece of
behaviour under test, so each awkward path the network can take has a name:

  normal      answers `tools/list`, a search, and a download
  structured  carries the payload in `structuredContent` instead of a text block
  noisy       pushes a notification and asks the client a question *before* answering, and withholds
              the answer until the question has been answered — a client that drops it deadlocks
  is_error    flags the call as failed with `isError`
  refuses     answers with a JSON-RPC error
  garbage     writes a line that is not JSON before answering
  dies        exits without answering, as a killed server does
  silent      accepts the call and never answers, for the timeout path
  mute        answers nothing at all, not even the handshake

Three environment variables let a test script a download without a network:

  FAKE_RESULTS   the search payload, as JSON                    (default: one good file)
  FAKE_DOWNLOAD  the life of each download, `|`-separated scripts of `;`-separated steps, each
                 `status:connection_state:received_bytes:local_path`. `download` starts the next script,
                 `download_status` walks it, and the last step repeats, so a stalled transfer stalls for
                 as long as the test needs. A two-candidate test fails the first script and lands the
                 second.
  FAKE_LOG       a file to append every tools/call to, as JSON lines, so a test can assert what was asked
"""

from __future__ import annotations

import json
import os
import sys
import time

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "normal"
QUESTION_ID = "server-question"

DEFAULT_RESULTS = [
    {
        "id": "vinyl_only:Music\\Kiasmos - Blurred (Extended Mix).flac",
        "username": "vinyl_only",
        "filename": "Music\\Kiasmos - Blurred (Extended Mix).flac",
        "filesize": 82_000_000,
        "extension": "flac",
        "bitrate": 855,
        "duration_sec": 448.0,
        "has_free_slots": True,
        "queue_size": 0,
    }
]

_pending_call: object | None = None
_group = 0          # which download we are on
_step = 0           # where we are inside its script


def out(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(message_id: object, payload: dict) -> None:
    out({"jsonrpc": "2.0", "id": message_id, "result": payload})


def as_text(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def results() -> list[dict]:
    raw = os.environ.get("FAKE_RESULTS")
    return json.loads(raw) if raw else DEFAULT_RESULTS


def scripts() -> list[list[dict]]:
    raw = os.environ.get("FAKE_DOWNLOAD", "downloading:transferring:1000:")
    return [_parse(chunk) for chunk in raw.split("|")]


def _parse(chunk: str) -> list[dict]:
    parsed = []
    for step in chunk.split(";"):
        status, _, rest = step.partition(":")
        connection, _, rest = rest.partition(":")
        size, _, path = rest.partition(":")
        parsed.append({
            "status": status,
            "connection_state": connection,
            "received_bytes": int(size or 0),
            "progress_pct": 100.0 if status == "finished" else 0.0,
            "local_path": path or os.environ.get("FAKE_LOCAL_PATH", ""),
            "wait_before_poll_secs": 30,
        })
    return parsed


def a_download_starts() -> dict:
    """The first step of this download's own script."""
    global _group, _step
    _step = 0
    script = scripts()
    group = script[min(_group, len(script) - 1)]
    _group += 1
    return group[0]


def the_download_goes_on() -> dict:
    """The next step of the script the current download is walking, holding on its last one."""
    global _step
    script = scripts()
    group = script[min(_group - 1, len(script) - 1)]
    _step += 1
    return group[min(_step, len(group) - 1)]


def record(name: str, arguments: dict) -> None:
    path = os.environ.get("FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": name, "arguments": arguments}) + "\n")


def handle_call(message: dict) -> None:
    global _pending_call
    if SCENARIO == "dies":
        sys.exit(3)
    if SCENARIO == "silent":
        time.sleep(3600)
        return
    if SCENARIO == "is_error":
        reply(message["id"], {"isError": True,
                              "content": [{"type": "text", "text": "the peer refused"}]})
        return
    if SCENARIO == "refuses":
        out({"jsonrpc": "2.0", "id": message["id"],
             "error": {"code": -32601, "message": "no such tool"}})
        return
    if SCENARIO == "noisy":
        _pending_call = message["id"]
        out({"jsonrpc": "2.0", "method": "notifications/message",
             "params": {"level": "info", "data": "searching"}})
        out({"jsonrpc": "2.0", "id": QUESTION_ID, "method": "roots/list"})
        return
    if SCENARIO == "garbage":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()

    name = (message.get("params") or {}).get("name")
    arguments = (message.get("params") or {}).get("arguments") or {}
    record(name, arguments)

    if SCENARIO == "structured":
        reply(message["id"], {"structuredContent": {"results": results()},
                              "content": [{"type": "text", "text": "ignored"}]})
        return
    if name == "search":
        reply(message["id"], as_text({"count": len(results()), "results": results()}))
    elif name == "download":
        reply(message["id"], as_text(a_download_starts()))
    elif name == "download_status":
        reply(message["id"], as_text(the_download_goes_on()))
    elif name == "cancel_download":
        reply(message["id"], as_text({"status": "cancelled", "count": 1}))
    else:
        reply(message["id"], as_text({"status": "error", "code": "unknown_tool",
                                      "message": f"no tool called {name}"}))


def main() -> None:
    global _pending_call
    if SCENARIO == "mute":
        time.sleep(3600)
        return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)

        if "method" not in message:                       # the answer to our question
            if message.get("id") == QUESTION_ID and _pending_call is not None:
                reply(_pending_call, as_text(results()))
                _pending_call = None
            continue
        if "id" not in message:                           # a notification
            continue

        method = message["method"]
        if method == "initialize":
            reply(message["id"], {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "serverInfo": {"name": "fake-soulseek", "version": "0"}})
        elif method == "tools/list":
            reply(message["id"], {"tools": [{"name": "search"}, {"name": "download"}]})
        elif method == "tools/call":
            handle_call(message)


if __name__ == "__main__":
    main()
