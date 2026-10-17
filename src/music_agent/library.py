"""What the collection already holds, and the vocabulary a genre has to come from.

The collection is a SQLCipher database that pyrekordbox reads with the key it caches, and the directories
are read as well because a file can be present with no row at all. Two field traps: `Content.FolderPath`
holds the whole file path despite its name, and `Length` is already in seconds.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .parsing import bare_title, extract_mix, fold, parse_filename, same_credit
from .rules import AUDIO_EXTENSIONS, DURATION_WINDOW, LOSSLESS_EXTENSIONS


class Unreadable(RuntimeError):
    """The collection could not be read — the database is missing, locked, or not what it should be."""


@dataclass(frozen=True)
class Held:
    """One track the collection holds, whether from the database or from the directories.

    `title` is the bare title and `mix` the version beside it, the same split the tracklist reader makes of a
    request — the two sides are looked up against each other by name, so a row titled `Galbi (feat Leïla
    Koumiya)` and a request titled `Galbi` have to arrive at the same key (2026-10-13).
    """
    artist: str
    title: str
    seconds: int | None = None
    path: str = ""
    genre: str = ""
    content_id: int | None = None
    mix: str = ""

    @property
    def key(self) -> str:
        """The identity a match is made on — the title, folded, so accents and case cannot break it.

        The credit is deliberately not part of it. `A x B`, `A & B` and `A, B` are one collaboration written
        three ways, the files themselves disagree about which, and a key that spells the credit out holds
        one track twice. The credit is compared by name instead, once the title has narrowed the field.
        """
        return fold(self.title)

    @property
    def lossless(self) -> bool:
        """Which lane this copy is in, from its extension."""
        return Path(self.path).suffix.lower().lstrip(".") in LOSSLESS_EXTENSIONS


def path_key(path: str | Path) -> str:
    """NFC and casefolded: raw equality matches only 1342 of the 1627 library paths."""
    return unicodedata.normalize("NFC", str(path)).casefold()


class Library:
    """The collection as the decisions see it: what is held, and what a genre may be."""

    def __init__(self, rows: list[Held], genres: list[str]) -> None:
        self._genres = list(genres)
        self._by_key: dict[str, list[Held]] = {}
        for row in rows:
            if row.title:
                self._by_key.setdefault(row.key, []).append(row)

    def genres(self) -> list[str]:
        """The vocabulary a genre has to come from."""
        return list(self._genres)

    def held(self, artist: str, title: str, seconds: int | None) -> Held | None:
        """The row for this track when the collection already holds it, else None.

        Folded title, the matcher's own `DURATION_WINDOW` on the length, and a credit naming the same
        people — so "already held" can never be looser than "the right file". Without a length there is
        nothing to compare, so the answer is None: the track is not *known* to be held, and a search costs
        less than a wrong skip.
        """
        if seconds is None:
            return None
        for row in self._by_key.get(fold(title), []):
            if (row.seconds is not None and abs(row.seconds - seconds) <= DURATION_WINDOW
                    and same_credit(row.artist, artist)):
                return row
        return None

    def rows_for(self, artist: str, title: str) -> list[Held]:
        """Every row under this name, whatever its length.

        Only the upgrade case needs this: a length mismatch usually means a different version, but when the
        copy in the collection *is* the reference — a white-label MusicBrainz holds no recording for — the
        name is the only thing left to recognise it by.
        """
        return [row for row in self._by_key.get(fold(title), []) if same_credit(row.artist, artist)]

    def __len__(self) -> int:
        return sum(len(rows) for rows in self._by_key.values())


@dataclass(frozen=True)
class Sound:
    """What a header says about a file, which is all the import needs of it."""

    seconds: int | None = None
    sample_rate: int | None = None
    bit_rate: int | None = None
    bit_depth: int | None = None


def sound_of(path: Path) -> Sound:
    """A file's length, sample rate and bit rate, read from its header.

    Read rather than measured: a header costs about a millisecond where decoding the audio would cost a
    great deal more. `bit_rate` is bits per second as the header gives it, and the import — which writes
    these into the collection — converts it to the kbps the collection wants.
    """
    from mutagen import File as MutagenFile

    try:
        audio = MutagenFile(path)
        info = getattr(audio, "info", None)
    except Exception:
        return Sound()
    if info is None:
        return Sound()
    seconds = getattr(info, "length", None)
    bits = getattr(info, "bits_per_sample", None) or getattr(info, "sample_size", None)
    return Sound(
        seconds=round(seconds) if seconds else None,
        sample_rate=getattr(info, "sample_rate", None),
        bit_rate=getattr(info, "bitrate", None),
        bit_depth=int(bits) if bits else None,
    )


def seconds_of(path: Path) -> int | None:
    """A file's length alone, for the places that want nothing else."""
    return sound_of(path).seconds


def read_database() -> tuple[list[Held], list[str]]:
    """Every row the collection holds, and its genre vocabulary.

    pyrekordbox is imported here rather than at the top, so the matching rules in this module can be
    read and tested without a database anywhere near them.
    """
    from pyrekordbox import Rekordbox6Database

    database = Rekordbox6Database()
    rows: list[Held] = []
    for row in database.get_content():
        # rekordbox never deletes a row: taking a track out of the collection sets `rb_local_deleted`, and the
        # app hides it from then on. A removed row is not held, and it is not a row whose file has gone either —
        # it is not a row, which is why this stops the report of missing files at the collection the app shows.
        # (`update_lossy_list.py` has always filtered this the same way; this was the odd one out.)
        if getattr(row, "rb_local_deleted", 0):
            continue
        title = row.Title or ""
        if not title:
            continue
        identifier = str(getattr(row, "ID", "") or "")
        rows.append(Held(
            artist=(getattr(row.Artist, "Name", "") or "") if row.Artist else "",
            title=bare_title(title),
            mix=extract_mix(title),
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
            found.append(Held(artist=parsed.artist, title=parsed.title, mix=parsed.mix,
                              seconds=seconds_of(path), path=str(path)))
    return found


def _target_rows(config: Config) -> tuple[list[Held], list[str], bool]:
    """Every configured target that can read a collection, asked in order, and whether any answered.

    A target that cannot be read is not a reason to stop the pipeline: the directories are still a library,
    and a config with no reading target is a complete config. `Rekordbox` raises `Unreadable` itself when a
    configured database is there but broken, which is the case that must not be mistaken for an empty one.
    The flag separates "no collection was read" from "the collection held nothing".
    """
    from . import exporting

    rows: list[Held] = []
    genres: list[str] = []
    read = False
    for target in exporting.configured(config):
        if not target.reads or not target.available(config):
            continue
        found, vocabulary = target.rows(config)
        rows.extend(found)
        genres.extend(vocabulary)
        read = True
    return rows, genres, read


def open(config: Config) -> Library:
    """The collection as it stands: the targets' rows, and the files beside them."""
    rows, genres, _read = _target_rows(config)
    return Library(rows + read_directories(config), genres)


@dataclass(frozen=True)
class Unpointed:
    """Files and rows that do not line up — what a run leaves behind.

    None of it is wrong enough to repair unasked, so it is reported and left alone: a re-download puts a
    second copy in a lane, a refused placement leaves its file in staging, and a row outlives its file.
    """

    staged: list[Path] = field(default_factory=list)   # in staging, with no record pointing at it
    lane: list[Path] = field(default_factory=list)     # in a lane, with no row and no record pointing at it
    gone: list[str] = field(default_factory=list)      # a row points at a file that is not there

    def empty(self) -> bool:
        return not (self.staged or self.lane or self.gone)


def unpointed(config: Config, *, claimed: Iterable[str] = ()) -> Unpointed:
    """What nothing points at, and what points at nothing.

    `claimed` is the path keys of the run's own records: a staged file a record still owns is not a
    leftover, however long the run has held it.
    """
    owned = {path_key(path) for path in claimed}
    rows, _genres, read = _target_rows(config)
    row_paths = {path_key(row.path) for row in rows if row.path}

    leftovers = Unpointed()
    if read:
        # Without a collection to compare against, a lane file is not evidence of anything.
        for held in sorted(read_directories(config), key=lambda entry: entry.path):
            key = path_key(held.path)
            if key not in row_paths and key not in owned:
                leftovers.lane.append(Path(held.path))
    if config.staging.is_dir():
        for path in sorted(config.staging.iterdir()):
            # A cancelled transfer leaves the client's own `.part` file here, which is as much a leftover as
            # a whole track is, so nothing is filtered by extension.
            if path.is_file() and not path.name.startswith(".") and path_key(path) not in owned:
                leftovers.staged.append(path)
    leftovers.gone.extend(row.path for row in rows if row.path and not Path(row.path).is_file())
    return leftovers
