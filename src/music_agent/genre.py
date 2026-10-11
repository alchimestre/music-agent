"""Choosing a genre from the library's own vocabulary.

Ticket 06: a curated table maps other people's genres onto the library's 25, read from the collection at
run time because they exist nowhere else — only 27 of 1627 files carry a genre tag at all. The most
specific match wins, and when nothing fits the genre is **left blank** and put in front of a human rather
than invented, because a genre cannot be undone after import.

Ticket 01's order is kept and it was measured rather than assumed: Discogs styles first, MusicBrainz
genres second, nothing third. The measurement behind that is worth recording, because it is not what one
would guess. MusicBrainz's own genres turned out to be useless for this repertoire — a sample of the
collection had `electronic` alone claiming **ten different library genres**, and `techno` claiming five
including Drum & Bass — while Discogs styles name a single thing (`Jazz-Funk`, `Dub Techno`, `Deep
House`). So the table is built from styles, and MusicBrainz's genres sit behind them as a weak second
opinion rather than a peer.

The table lives in `genres.toml` beside the code, so a mapping can be corrected without touching any of
this. Ticket 18 puts the gaps in the working procedure's hands: where the table misses, a model chooses among the
library's genres or declines.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .parsing import fold

# Beside the code rather than beside a particular config file: the mappings are data this project ships,
# not something an installation sets.
DEFAULT_TABLE_PATH = Path(__file__).resolve().parents[2] / "genres.toml"


@dataclass(frozen=True)
class Choice:
    """What the table made of what was offered, and whether it made anything of it at all."""

    genre: str = ""
    source: str = ""
    considered: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.genre)


def specificity(style: str) -> tuple[int, int]:
    """How specific a name is: more words first, then a longer name.

    `Minimal Techno` beats `Techno`, and `Dub Techno` beats both — which is what the design notes means by the
    most specific genre winning.
    """
    return (len(fold(style).split()), len(style))


def table(path: Path | None = None) -> dict[str, str]:
    """The mappings, folded on both sides so nothing has to remember how they were written."""
    source = Path(path) if path is not None else DEFAULT_TABLE_PATH
    if not source.is_file():
        return {}
    with source.open("rb") as handle:
        data = tomllib.load(handle)
    return {fold(name): mapped for name, mapped in (data.get("map") or {}).items()}


def resolve(offered: Sequence[str], *, vocabulary: Sequence[str],
            mappings: Mapping[str, str]) -> Choice:
    """The library's genre for what was offered, or a blank one with everything that was considered.

    `offered` is in the caller's order of trust, so Discogs styles come before MusicBrainz genres and the
    earlier of two equally specific names wins. A mapping that points at a genre the collection does not
    have is ignored rather than trusted, because the vocabulary is read from the collection at run time
    and the table may have drifted from it.
    """
    allowed = {fold(genre) for genre in vocabulary}
    best: tuple[tuple[int, int], int, str, str] | None = None

    for order, source in enumerate(offered):
        mapped = mappings.get(fold(source))
        if not mapped or fold(mapped) not in allowed:
            continue
        candidate = (specificity(source), -order, mapped, source)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    if best is None:
        return Choice(considered=tuple(offered))
    return Choice(genre=best[2], source=best[3], considered=tuple(offered))
