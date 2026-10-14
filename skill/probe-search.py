"""Does the network answer a search at all?

Run this before spending a pass, and whenever a pass comes back with tracks that all found nothing.

A pass that fires hundreds of searches in a couple of minutes is answered with nothing after its first
seconds — silently, from the child and from the network alike — and the same searches return results again
later, so that empty answer is a spent allowance rather than a broken search path. How long one lasts cannot
be known from here, which is why it is measured rather than assumed. The measurement is one track the network
cannot be short of, and the child's own account of the search.

    set -a; source .env; set +a
    python skill/probe-search.py

The package is found from this file's own location, so it runs from any directory, and a different track or a
different window can be passed as arguments. It binds the ports `SLSK_LISTEN_PORT` and `SLSK_OBFUSCATED_PORT`
name, so it cannot run while a pass is running.
"""

import sys
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "pyproject.toml").is_file())
sys.path.insert(0, str(ROOT / "src"))

from music_agent import config as config_module  # noqa: E402
from music_agent import mcp, slsk  # noqa: E402

QUERY = sys.argv[1] if len(sys.argv) > 1 else "Michael Jackson Thriller"
WINDOW = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

config = config_module.load()
print(f"query {QUERY!r} window {WINDOW:g}s")
with mcp.StdioServer(
    list(config.soulseek_command),
    env=slsk.environment(config),
    on_log=lambda line: print(f"    server: {line}"),
) as server:
    payload = server.call(
        "search",
        {"query": QUERY, "timeout": WINDOW, "max_results": 60},
        timeout=WINDOW + 15.0,
    )
    results = (payload or {}).get("results") or []
    print(f"results: {len(results)}")
    for item in results[:8]:
        print(f"  {item.get('user')}: {item.get('filename')} "
              f"({item.get('size')} bytes, {item.get('bitrate')})")

print("the network is answering" if results else
      "nothing came back: a search this broad finding nothing is the network, not the tracklist — unless\n"
      "SLSK_LISTEN_PORT names a port peers cannot reach, which looks the same from here")
