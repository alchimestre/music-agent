"""What the collection already holds, and the vocabulary a genre has to come from.

Ticket 02: the collection is a SQLCipher database that pyrekordbox reads with the key it caches. Ticket 09
needs one question answered before anything is fetched — is this track already held, by normalized artist
and title with a length inside the same ±1 s window the matcher uses — and the design notes needs the genre
vocabulary, which exists nowhere else: only 27 of 1627 files carry a genre tag, so the list lives in the
database and has to be read from it.

Three things about the fields, all found rather than assumed. `Content.FolderPath` holds the **whole file
path**, despite the name. `Length` is already in seconds, not milliseconds. And the rows do not all point
at files: of 1567 rows today, 1544 have their file on disk and 23 do not.

The directories are read as well as the database, because a file can be present with no row at all — ticket
10 counted 83 of those. Reading a length from a header costs about a millisecond, so the library indexes in
roughly a second and nothing here needs caching.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .parsing import fold, parse_filename
from .rules import AUDIO_EXTENSIONS, DURATION_WINDOW


class Unreadable(RuntimeError):
    """The collection could not be read — the database is missing, locked, or not what it should be."""


@dataclass(frozen=True)
class Held:
    """One track the collection holds, whether from the database or from the directories."""
    artist: str
    title: str
    seconds: int | None = None
    path: str = ""
    genre: str = ""
    content_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        """The identity a match is made on — folded, so accents and case cannot break it (tickets 09, 14)."""
        return (fold(self.artist), fold(self.title))


def path_key(path: str | Path) -> str:
    """Ticket 10: NFC and casefolded, because raw equality matches only 1342 of 1627 library paths."""
    return unicodedata.normalize("NFC", str(path)).casefold()


class Library:
    """The collection as the decisions see it: what is held, and what a genre may be."""

    def __init__(self, rows: list[Held], genres: list[str]) -> None:
        self._genres = list(genres)
        self._by_key: dict[tuple[str, str], list[Held]] = {}
        for row in rows:
            if row.title:
                self._by_key.setdefault(row.key, []).append(row)

    def genres(self) -> list[str]:
        """The vocabulary a genre has to come from (the design notes)."""
        return list(self._genres)

    def held(self, artist: str, title: str, seconds: int | None) -> Held | None:
        """The row for this track when the collection already holds it, else None.

        Ticket 09: normalized artist and title, and a length within the window the matcher already uses —
        the same `DURATION_WINDOW`, so "already held" can never be looser than "the right file". Without a
        length there is nothing to compare, so the answer is None: the track is not *known* to be held, and
        a search costs less than a wrong skip.
        """
        if seconds is None:
            return None
        for row in self._by_key.get((fold(artist), fold(title)), []):
            if row.seconds is not None and abs(row.seconds - seconds) <= DURATION_WINDOW:
                return row
        return None

    def __len__(self) -> int:
        return sum(len(rows) for rows in self._by_key.values())


def seconds_of(path: Path) -> int | None:
    """A file's length, read from its header. Absent when it cannot be read at all."""
    from mutagen import File as MutagenFile

    try:
        audio = MutagenFile(path)
        length = getattr(getattr(audio, "info", None), "length", None)
    except Exception:
        return None
    return round(length) if length else None


def read_database() -> tuple[list[Held], list[str]]:
    """Every row the collection holds, and its genre vocabulary.

    pyrekordbox is imported here rather than at the top so that the matching rules in this module can be
    read, and tested, without a database anywhere near them.
    """
    from pyrekordbox import Rekordbox6Database

    database = Rekordbox6Database()
    rows: list[Held] = []
    for row in database.get_content():
        title = row.Title or ""
        if not title:
            continue
        identifier = str(getattr(row, "ID", "") or "")
        rows.append(Held(
            artist=(getattr(row.Artist, "Name", "") or "") if row.Artist else "",
            title=title,
            seconds=getattr(row, "Length", None),
            path=getattr(row, "FolderPath", "") or "",
            genre=(getattr(row.Genre, "Name", "") or "") if row.Genre else "",
            content_id=int(identifier) if identifier.isdigit() else None,
        ))
    genres = [genre.Name for genre in database.get_genre() if genre.Name]
    return rows, genres


def read_directories(config: Config) -> list[Held]:
    """The files themselves, for what the database has not got."""
    found: list[Held] = []
    for folder in (config.lossless, config.lossy):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS:
                continue
            parsed = parse_filename(path.name)
            found.append(Held(artist=parsed.artist, title=parsed.title,
                              seconds=seconds_of(path), path=str(path)))
    return found


def open(config: Config) -> Library:
    """The collection as it stands: the database's rows, and the files beside them."""
    try:
        rows, genres = read_database()
    except Exception as error:              # pyrekordbox raises its own types, and its own messages
        raise Unreadable(
            f"cannot read the rekordbox collection ({error}) — is rekordbox installed, and is the "
            "database where pyrekordbox expects it?"
        ) from error
    return Library(rows + read_directories(config), genres)
