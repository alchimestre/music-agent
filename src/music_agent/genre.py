"""Choosing a genre from the library's own vocabulary.

A curated table in `genres.toml` maps other people's genre names onto the library's, which is read from the
collection at run time because it exists nowhere else — only 27 of 1627 files carry a genre tag. The most
specific match wins, and when nothing fits the genre is **left blank** for a human rather than invented,
because a genre cannot be undone after import.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .parsing import fold

# Beside the code rather than beside a particular config file: the mappings are data this project ships.
DEFAULT_TABLE_PATH = Path(__file__).resolve().parents[2] / "genres.toml"

# A name that fits nothing is counted, and at five tracks it is put to the user to accept or decline rather
# than staying blank for ever. The pipeline never mints a genre itself.
PROPOSAL_THRESHOLD = 5


@dataclass(frozen=True)
class Proposal:
    """A name that fitted nothing on enough tracks to be worth becoming a genre."""

    name: str
    tracks: int


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
    """How specific a name is: more words first, then the longer name, so `Minimal Techno` beats `Techno`."""
    return (len(fold(style).split()), len(style))


def _document(path: Path | None) -> dict:
    source = Path(path) if path is not None else DEFAULT_TABLE_PATH
    if not source.is_file():
        return {}
    with source.open("rb") as handle:
        return tomllib.load(handle)


def table(path: Path | None = None) -> dict[str, str]:
    """The mappings, folded on both sides so nothing has to remember how they were written."""
    return {fold(name): mapped for name, mapped in (_document(path).get("map") or {}).items()}


def approved(path: Path | None = None) -> tuple[str, ...]:
    """Genre names the collection is allowed to gain, which the database does not have yet.

    `resolve` refuses to hand out a name the collection does not have, so an approved name is the
    collection being told it may have this one.
    """
    return tuple(str(name) for name in (_document(path).get("approved") or ()))


def vocabulary(held: Sequence[str], approved_names: Sequence[str] = ()) -> list[str]:
    """What a genre may be: the collection's own names, plus the ones approved for it."""
    known = list(held)
    folded = {fold(name) for name in known}
    return known + [name for name in approved_names if fold(name) not in folded]


def proposals(counted: Mapping[str, int], *, mappings: Mapping[str, str],
              threshold: int = PROPOSAL_THRESHOLD) -> list[Proposal]:
    """The names worth putting to the user, the best-evidenced first.

    A name that already has a mapping is not a proposal: it resolved, or it pointed somewhere the
    collection does not have, and either way the table is where that gets fixed.
    """
    return [
        Proposal(name, tracks)
        for name, tracks in sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))
        if tracks >= threshold and fold(name) not in mappings
    ]


def resolve(offered: Sequence[str], *, vocabulary: Sequence[str],
            mappings: Mapping[str, str]) -> Choice:
    """The library's genre for what was offered, or a blank one with everything that was considered.

    `offered` is in the caller's order of trust, so the earlier of two equally specific names wins. A
    mapping that points at a genre the collection does not have is ignored rather than trusted, because
    the vocabulary is read from the collection at run time.
    """
    allowed = {fold(genre) for genre in vocabulary}
    ranked: list[tuple[tuple[int, int], str, str]] = []
    for order, source in enumerate(offered):
        mapped = mappings.get(fold(source))
        if mapped and fold(mapped) in allowed:
            ranked.append(((specificity(source), -order), mapped, source))

    if not ranked:
        return Choice(considered=tuple(offered))
    _, genre, source = max(ranked)
    return Choice(genre=genre, source=source, considered=tuple(offered))
