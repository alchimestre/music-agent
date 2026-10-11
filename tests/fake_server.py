"""A stand-in for the acquisition server: enough of the protocol to exercise the client, no Soulseek.

`test_mcp.py` runs this as a child process, exactly as the real server is run. The scenario is the one
piece of behaviour under test, so each awkward path the network can take has a name:

  normal      answers `tools/list` and a search with a payload
  structured  carries the payload in `structuredContent` instead of a text block
  noisy       pushes a notification and asks the client a question *before* answering, and withholds
              the answer until the question has been answered — a client that drops it deadlocks
  is_error    flags the call as failed with `isError`
  refuses     answers with a JSON-RPC error
  garbage     writes a line that is not JSON before answering
  dies        exits without answering, as a killed server does
  silent      accepts the call and never answers, for the timeout path
  mute        answers nothing at all, not even the handshake
"""

from __future__ import annotations

import json
import sys
import time

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "normal"
QUESTION_ID = "server-question"

RESULTS = [
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

_pending_call: object | None = None


def out(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(message_id: object, payload: dict) -> None:
    out({"jsonrpc": "2.0", "id": message_id, "result": payload})


def as_text(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


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
    if SCENARIO == "structured":
        reply(message["id"], {"structuredContent": {"results": RESULTS},
                              "content": [{"type": "text", "text": "ignored"}]})
        return
    reply(message["id"], as_text(RESULTS))


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
                reply(_pending_call, as_text(RESULTS))
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
