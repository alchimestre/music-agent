"""The lanes: what is on disk, and the name a record's file carries.

The collection's rows are `library`'s business; this module answers the other side. The record-to-file
mapping is one convention and one scan, so reconcile, the leftover report and the sweep all read the same
answer.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import state
from .config import Config
from .parsing import fold, parse_filename
from .rules import AUDIO_EXTENSIONS, LOSSLESS_EXTENSIONS


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


def read_directories(config: Config) -> list[Held]:
    """The files themselves, for what the database has not got."""
    found: list[Held] = []
    for folder in (config.lossless, config.lossy):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS:
                continue
            parsed = parse_filename(path.name)
            found.append(Held(artist=parsed.artist, title=parsed.title, mix=parsed.mix,
                              seconds=seconds_of(path), path=str(path)))
    return found


# Characters a filesystem will not take, replaced rather than dropped so that two differently punctuated
# titles cannot collapse onto one name.
_UNSAFE = str.maketrans({"/": "-", "\\": "-", ":": "-", "\0": ""})


def filename_for(record: state.Record, suffix: str) -> str:
    """The house convention the library already uses: `Artist - Title`, with the mix in parentheses.

    A white-label request has no artist, so its title stands alone.
    """
    name = f"{record.artist} - {record.title}" if record.artist else record.title
    if record.mix:
        name = f"{name} ({record.mix})"
    cleaned = " ".join(name.translate(_UNSAFE).split()).strip()
    return (cleaned or "untitled") + suffix.lower()


@dataclass(frozen=True)
class Lanes:
    """Both lanes by stem, lossless first: when both hold a stem, that copy is the upgrade."""

    by_stem: dict[str, Path]

    @classmethod
    def scan(cls, config: Config) -> Lanes:
        on_disk: dict[str, Path] = {}
        for lane in (config.lossless, config.lossy):
            if not lane.is_dir():
                continue
            for path in sorted(lane.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    on_disk.setdefault(path.stem, path)
        return cls(on_disk)

    def file_for(self, record: state.Record) -> Path | None:
        """The lane file this record would have made, if it is there."""
        return self.by_stem.get(filename_for(record, ""))


def claimed(config: Config) -> set[Path]:
    """Every path any run's records name, so a file a pass still owes is never mistaken for debris."""
    named = set()
    for job in state.all_jobs(config):
        for record in job.records():
            if record.local_path:
                named.add(Path(record.local_path).resolve())
    return named
