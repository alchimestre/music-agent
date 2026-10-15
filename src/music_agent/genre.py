"""Choosing a genre from the collection's own vocabulary.

Two things have to meet: the genre names the collection actually holds, read from the library at run time
because they exist nowhere else, and a mapping from everybody else's names onto those. The mapping is
music-tagger's taxonomy — the same table that project classifies the collection with — so it is read from
there rather than kept a second time here. The most specific name wins, and when nothing fits, the genre is
left blank rather than invented, because a genre cannot be undone after an import.

Nothing here is put to a person: a name that fits nothing is counted and printed, and that is the end of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .parsing import fold


@dataclass(frozen=True)
class Choice:
    """What the vocabulary made of what was offered, and whether it made anything of it at all."""

    genre: str = ""
    source: str = ""
    considered: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.genre)


def specificity(style: str) -> tuple[int, int]:
    """How specific a name is: more words first, then the longer name, so `Minimal Techno` beats `Techno`."""
    return (len(fold(style).split()), len(style))


def document(path: str | Path | None) -> dict:
    """The taxonomy, or nothing when there is none to read. A missing file is not an error: a machine
    without music-tagger installed simply gets blank genres."""
    if not path:
        return {}
    source = Path(path).expanduser()
    if not source.is_file():
        return {}
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def families(path: str | Path | None = None) -> list[str]:
    """The genre families themselves, in the taxonomy's own order."""
    return [
        tier1
        for tier1 in (str(group.get("tier1") or "").strip()
                      for group in document(path).get("groups") or [])
        if tier1
    ]


def table(path: str | Path | None = None) -> dict[str, str]:
    """Every name the taxonomy maps onto a family, folded on both sides as it loads."""
    mappings: dict[str, str] = {}
    for group in document(path).get("groups") or []:
        family = str(group.get("tier1") or "").strip()
        if not family:
            continue
        for name in group.get("from") or []:
            if str(name).strip():
                mappings[fold(str(name))] = family
    return mappings


def vocabulary(held: Sequence[str], family_names: Sequence[str] = ()) -> list[str]:
    """What a genre may be: the collection's own names, plus the families it does not have yet.

    The collection's own names come first because they are what its files and its screen already say.
    """
    known = list(held)
    folded = {fold(name) for name in known}
    return known + [name for name in family_names if fold(name) not in folded]


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
