"""A playlist from any service, as the tracklist a run starts from.

A reader is a module that answers three things, and needs nothing else:

- `names_a_playlist(source)` — whether an argument is its own, decided from the argument alone. It has to
  be pure: `cli._start` asks every reader before anything is fetched, and a reader that went to the network
  to answer would spend a request on every path it was handed.
- `playlist(source, timeout)` — a `Playlist`, or `PlaylistError` with a sentence a person can act on.
- that error, raised rather than a traceback, because `cli._start` catches exactly this one.

Every reader — `spotify`, `deezer`, `youtube` — turns its own service into this one shape: a name, and one
`Artist - Title` line per entry in playlist order. What comes out is the text `tracklist` reads, so a
playlist and a file reach a run the same way and are read once, by `tracklist`. A new service is a module
and a line in `cli._PLAYLIST_SOURCES`, not a branch in either reader. A reader may reach its service
however that service is reachable — Deezer over its public API, YouTube through `yt-dlp`, which is the
only interface to a playlist that is not assembled by YouTube's own scripts.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


class PlaylistError(RuntimeError):
    """The playlist could not be read, with the reason a person can act on."""


def id_in(source: str, *, host: str, kind: str, pattern: re.Pattern[str]) -> str | None:
    """The id a service's URL or its `<service>:<kind>:<id>` URI carries, or None when it carries none.

    A whole URL is taken apart rather than pattern-matched, because the path carries a market prefix
    (`/intl-fr/playlist/…`, `/us/playlist/…`) and a query tail, and neither belongs to the id.
    """
    text = source.strip()
    service = host.split(".")[0]
    if text.startswith(f"{service}:"):
        parts = text.split(":")
        if len(parts) > 2 and parts[1] == kind and pattern.match(parts[2]):
            return parts[2]
        return None
    if host not in text:
        return None
    segments = [segment for segment in urllib.parse.urlparse(text).path.split("/") if segment]
    if kind not in segments:
        return None
    after = segments[segments.index(kind) + 1:]
    return after[0] if after and pattern.match(after[0]) else None


@dataclass(frozen=True)
class Playlist:
    """A playlist as a tracklist: its name, and one `Artist - Title` line per entry, in playlist order."""

    name: str
    lines: tuple[str, ...]

    def text(self) -> str:
        """The file's own text. The name rides in a comment, which `tracklist` skips, so it costs no track."""
        count = len(self.lines)
        header = f"# {self.name} · {count} track{'s' if count != 1 else ''}\n"
        return header + "".join(f"{line}\n" for line in self.lines)
