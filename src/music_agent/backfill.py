"""The gap the other direction: files in the lanes that the collection has no row for.

`import` is scoped to one run's records, so a file that arrives in a lane outside a run — placed by hand, or by
the older conversion workflow — is never given a row, and the collection quietly holds less than the library
does. The scope is *gap-filling only, dry run by default*; this is the driver:

```
music-agent backfill            # what it would add, writing nothing
music-agent backfill --apply    # one backup, one commit
```

A row is minted from what the **file itself says** — its tags, falling back to its name, which is also the
order `importing.add` uses with the record it is handed — so nothing is invented and nothing is
guessed at. Two things are never touched: a file the collection **already holds** that track under (the
pipeline's own identity, lengths inside the one-second window — minting would make a duplicate, which is
`dedupe`'s business instead), and a file this project cannot read at all. Nothing here identifies or re-tags:
minting a row for a file the pipeline never processed is the gap being closed, and a file whose tags are
unreadable keeps them rather than having new ones guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import importing, parsing, tag
from .config import Config
from .duplicates import Copy, agrees, copies
from .lanes import path_key, read_directories, seconds_of


class NotClosed(RuntimeError):
    """Rekordbox is running, and pyrekordbox will not commit while it is."""


@dataclass
class Candidate:
    """One lane file with no row: what the file says it is, and whether it can be given one."""

    path: Path
    title: str
    artist: str
    seconds: int | None = None
    # The row that already holds this track under another file, when there is one.
    held_by: Path | None = None
    # Why it will not be minted, when it will not.
    why: str = ""

    @property
    def addable(self) -> bool:
        return not self.why

    def line(self) -> str:
        label = f"{self.artist} - {self.title}" if self.artist else self.title
        if not self.addable:
            return f"skip     {self.path.name} — {self.why}"
        return f"add      {self.path.name}   ({label[:60]}, {self.seconds}s)"


@dataclass
class Summary:
    """What a pass found, and what it did."""

    candidates: list[Candidate] = field(default_factory=list)
    minted: list[Candidate] = field(default_factory=list)

    def addable(self) -> list[Candidate]:
        return [one for one in self.candidates if one.addable]

    def report(self) -> str:
        parts = [f"{len(self.minted) or len(self.addable())} to add"]
        skipped = len(self.candidates) - len(self.addable())
        if skipped:
            parts.append(f"{skipped} skipped")
        return " · ".join(parts)

    def lines(self) -> list[str]:
        return [one.line() for one in self.candidates]


def identity_of(path: Path) -> tuple[str, str, int | None]:
    """What a file says it is: its tags, then its name, and the length off its header."""
    written = tag.read(path)
    parsed = parsing.parse_filename(path.name)
    title = written.title or parsed.title or path.stem
    return (written.artist or parsed.artist, title, seconds_of(path))


def candidates(config: Config, rows: list[Copy]) -> list[Candidate]:
    """Every file in a lane the collection has no live row for, with what the file says it is.

    A file whose *track* the collection already holds under another file is not a gap to fill but a duplicate
    to merge, and it says so rather than being minted twice.
    """
    held = {path_key(row.path) for row in rows}
    out: list[Candidate] = []
    for entry in read_directories(config):
        path = Path(entry.path)
        if path_key(path) in held:
            continue
        try:
            artist, title, seconds = identity_of(path)
        except Exception:                   # noqa: BLE001 — the question is whether it can be read at all
            out.append(Candidate(path=path, title=path.stem, artist="", why="not readable audio"))
            continue
        candidate = Candidate(path=path, title=title, artist=artist, seconds=seconds)

        mine = Copy(content_id=0, path=path, artist=artist, title=parsing.bare_title(title),
                    mix=parsing.extract_mix(title), seconds=seconds)
        clash = next((row for row in rows
                      if row.key == mine.key and agrees(row, mine)
                      and parsing.same_credit(row.artist, mine.artist)), None)
        if clash is not None:
            candidate.held_by = clash.path
            candidate.why = f"the collection already holds this track, under {clash.path.name}"
        out.append(candidate)
    return out


def plan(config: Config, *, db) -> Summary:
    """What a pass would add, writing nothing."""
    return Summary(candidates=candidates(config, copies(db)))


def mint(config: Config, summary: Summary, *, db, log=print) -> Summary:
    """Mint the rows: one backup, one commit, and the file's own metadata for every one of them."""
    if importing.is_running():
        raise NotClosed("rekordbox is running, and pyrekordbox will not commit while it is — close it, then "
                        "run this again")

    addable = summary.addable()
    if not addable:
        return summary

    where = importing.backup(db)
    if where is not None:
        log(f"backed the collection up to {where.name}")

    from . import state

    for index, candidate in enumerate(addable):
        record = state.Record(idx=index, artist=candidate.artist, title=candidate.title,
                              local_path=str(candidate.path))
        try:
            importing.add(db, record)
        except importing.ImportFailed as error:
            candidate.why = str(error)
            log(f"      {candidate.path.name}: {error}")
            continue
        except ValueError as error:                 # pyrekordbox's word for a path already in the collection
            candidate.why = str(error)
            log(f"      {candidate.path.name}: already in the collection")
            continue
        summary.minted.append(candidate)
        log(f"added    {candidate.path.name}")
    # the additions go in one commit, and `autoinc` is what makes pyrekordbox sort a minted int id beside the
    # text ids every row read back carries (see the import's own comment)
    db.commit(autoinc=True)
    return summary


def run(config: Config, *, db, apply: bool = False, log=print) -> Summary:
    """Read, decide, and write only when asked."""
    summary = plan(config, db=db)
    if not apply or not summary.addable():
        return summary
    return mint(config, summary, db=db, log=log)
