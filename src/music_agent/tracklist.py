"""The tracklist file: the one reading of the input, owned here.

One track per line as `Artist - Title`, optionally numbered, or a CSV whose header names both an artist and
a title column in any other columns' company; comments and blank lines are skipped. The title keeps its mix
suffix in `Request.mix` rather than in `Request.title`, because the query builder and the matcher read those
apart.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .parsing import bare_title, extract_mix, fold, parse_filename
from .rules import AUDIO_EXTENSIONS, Request

_COMMENT = "#"

# A numbered list entry, and only that: `01. `, `3) `, `A1 - `. A bare space is deliberately *not* accepted,
# because `808 State - Pacific` is an artist rather than item 808. Missing a track number costs a lookup that
# finds nothing; stripping an artist that begins with digits asks about a different artist.
_LIST_NUMBER = re.compile(r"^\s*(?:[A-Za-z]{1,2})?\d{1,3}\s*([.)\]:]|\s-\s)\s*")

# The column names an export uses. Exact matches, so `Album Artist` is not mistaken for the artist.
_ARTIST_HEADS = frozenset({"artist", "artist name", "artists", "artist(s)", "performer", "band"})
_TITLE_HEADS = frozenset({"title", "track", "track name", "track title", "name", "song", "song name"})


def header_columns(line: str) -> tuple[int, int] | None:
    """The (artist, title) column positions if this line is a header naming both, else None."""
    fields = [fold(field) for field in next(csv.reader([line]))]
    artist = next((i for i, field in enumerate(fields) if field in _ARTIST_HEADS), None)
    title = next((i for i, field in enumerate(fields) if field in _TITLE_HEADS), None)
    return None if artist is None or title is None else (artist, title)


def request_for(artist: str, title: str) -> Request:
    """A request from two strings, with the version read off the title rather than left in it."""
    title = title.strip()
    mix = extract_mix(title)
    bare = bare_title(title)
    if not bare:
        # The whole title was parenthesised, so it is a title and not a version suffix.
        return Request(artist=artist.strip(), title=title, mix="")
    return Request(artist=artist.strip(), title=bare, mix=mix)


def from_line(line: str) -> Request | None:
    """One text line. None when it is a comment, a blank, or otherwise nothing to ask for."""
    text = _LIST_NUMBER.sub("", line.strip(), count=1).strip()
    if not text or text.startswith(_COMMENT):
        return None
    artist, separator, title = text.partition(" - ")
    if not separator:
        # No artist: a white-label or an unnamed edit, which stays review-only rather than guessed at.
        artist, title = "", text
    if not title.strip():
        return None
    return request_for(artist, title)


def from_csv(lines: list[str], columns: tuple[int, int]) -> list[Request]:
    """Every row but the header, reading only the two columns that were named."""
    artist_at, title_at = columns
    requests: list[Request] = []
    for row in csv.reader(lines):
        if max(artist_at, title_at) >= len(row):
            continue                     # a short row: nothing to read rather than something to guess
        title = row[title_at].strip()
        if title:
            requests.append(request_for(row[artist_at], title))
    return requests


def parse(text: str) -> list[Request]:
    """Every request the text carries, in order."""
    lines = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith(_COMMENT)
    ]
    if not lines:
        return []
    columns = header_columns(lines[0])
    if columns is not None:
        return from_csv(lines[1:], columns)
    return [request for request in (from_line(line) for line in lines) if request is not None]


def from_lane(directory: Path) -> list[Request]:
    """Every audio file in a lane, named the way the pipeline named it.

    `--tracks ~/Music/Losy Tracks` is the upgrade pass: the lane is already the list, and each name already
    carries artist, title and version — reading it through `parse_filename` is the same reading the library
    makes of the same file.
    """
    requests = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
            named = parse_filename(path.name)
            requests.append(Request(artist=named.artist, title=named.title, mix=named.mix))
    return requests


def read(path: Path) -> list[Request]:
    """A tracklist file, or a directory of audio files."""
    path = Path(path).expanduser()
    if path.is_dir():
        return from_lane(path)
    return parse(path.read_text(encoding="utf-8-sig"))
