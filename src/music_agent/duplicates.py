"""One row per track: the duplicates a hand import leaves, merged.

An import mints a row, and when the track was already in the collection under another file — a lossy copy the
pipeline later upgraded, a track imported twice by hand — there are two rows for one track. One rule settles
which of them survives (**the better copy**), and this is that rule with a verb in front of it:

```
music-agent dedupe            # what it would merge, writing nothing
music-agent dedupe --apply    # one backup, one merge
```

The row holding the better copy stays; everything the other still held — its playlist lines, its plays, its My
Tags, its cues — moves onto the survivor rather than being lost, playlist and tag lines being dropped only where
the survivor already has one in the same playlist or the same tag. The redundant **file** is deleted: the row
that named it is gone, so nothing points at it, and a copy left in a lane is the gap the other direction —
`backfill` closes that one and `sweep` clears it. What that leaves behind in Traktor is a dead entry:
`traktor-nml purge` takes those out.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from . import importing, parsing, rules
from .config import Config
from .lanes import path_key

# The tables whose rows are *moved* onto the survivor rather than deleted with the row that goes.
MOVED = ("djmdSongPlaylist", "djmdSongMyTag", "djmdSongHistory", "djmdCue")

ROWS = text("""
    select c.ID, c.FolderPath, c.Title, c.Length, coalesce(a.Name, '') as Artist, c.DateCreated,
           (select count(*) from djmdSongPlaylist p where p.ContentID = c.ID) as Playlists,
           (select count(*) from djmdSongHistory h where h.ContentID = c.ID) as Plays,
           (select count(*) from djmdSongMyTag t where t.ContentID = c.ID) as Tags,
           (select count(*) from djmdCue q where q.ContentID = c.ID) as Cues
      from djmdContent c
      left join djmdArtist a on a.ID = c.ArtistID
     where c.rb_local_deleted = 0
""")


class NotClosed(RuntimeError):
    """Rekordbox is running, and pyrekordbox will not commit while it is."""


@dataclass(frozen=True)
class Copy:
    """One row: the file it points at, and what hangs off it."""

    content_id: int
    path: Path
    title: str
    artist: str
    mix: str
    seconds: int | None = None
    created: str = ""
    playlists: int = 0
    plays: int = 0
    tags: int = 0
    cues: int = 0

    @property
    def lossless(self) -> bool:
        return self.path.suffix.lower().lstrip(".") in rules.LOSSLESS_EXTENSIONS

    @property
    def key(self) -> tuple[str, str]:
        """What makes two rows the same track: the bare title folded, and the mix, which is never a
        substitute for another mix.

        The artist has to stay out of it. `A x B`, `A & B` and `A, B` are one collaboration written three
        ways and the files disagree about which, so a key that spells the credit out cannot see the very
        duplicates this looks for. The credit is compared by name instead, in `find`.
        """
        # A mix that names the original *is* no mix — `rules.mix_matches` reads a candidate that way — so a
        # row titled `Happy But Sad (Original Mix)` is the same recording as one titled `Happy But Sad`, and
        # keeping the two apart left that pair in the collection for a device export to carry twice.
        mix = "" if parsing.is_original(self.mix) else self.mix
        return (parsing.fold(self.title), parsing.fold(mix))

    def used(self) -> int:
        """How much of the library leans on this row."""
        return self.plays + self.playlists + self.tags + self.cues


@dataclass
class Merge:
    """One group to settle: the copy that stays, and the copies that go."""

    keep: Copy
    drops: list[Copy] = field(default_factory=list)

    def lines(self) -> list[str]:
        kept = self.keep
        out = [(f"keep     {kept.path.name}   ("
                f"{'lossless' if kept.lossless else 'lossy'}, {kept.used()} lines of history)")]
        for drop in self.drops:
            what = []
            if drop.playlists:
                what.append(f"{drop.playlists} playlist line{'s' if drop.playlists != 1 else ''}")
            if drop.plays:
                what.append(f"{drop.plays} play{'s' if drop.plays != 1 else ''}")
            if drop.tags:
                what.append(f"{drop.tags} My Tag{'s' if drop.tags != 1 else ''}")
            if drop.cues:
                what.append(f"{drop.cues} cue{'s' if drop.cues != 1 else ''}")
            moves = ", ".join(what) if what else "nothing else on it"
            out.append(f"drop     {drop.path.name}   ({moves})")
        return out


@dataclass
class Summary:
    """What a pass found or did, by group."""

    merges: list[Merge] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.merges)} duplicate group{'s' if len(self.merges) != 1 else ''}"]
        if self.files:
            parts.append(f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} removed")
        return " · ".join(parts)

    def lines(self) -> list[str]:
        out: list[str] = []
        for merge in self.merges:
            out += merge.lines()
        for path in self.files:
            out.append(f"file     {path.name} left the lanes")
        return out


def copies(db) -> list[Copy]:
    """Every live row as a `Copy`."""
    found: list[Copy] = []
    for identifier, where, title, length, artist, created, playlists, plays, tags, cues in db.session.execute(ROWS):
        found.append(Copy(
            content_id=int(identifier),
            path=Path(where or ""),
            title=parsing.bare_title(title or ""),
            mix=parsing.extract_mix(title or ""),
            artist=artist or "",
            seconds=int(length) if length else None,
            created=str(created or ""),
            playlists=playlists, plays=plays, tags=tags, cues=cues,
        ))
    return found


def agrees(one: Copy, other: Copy) -> bool:
    """Whether two rows can be one track: the same length within the pipeline's one-second window, or a length
    one of them does not carry (rekordbox measures every file it holds, so that is rare — and a name alone has
    never been enough to call two rows the same track)."""
    if one.seconds is None or other.seconds is None:
        return True
    return abs(one.seconds - other.seconds) <= 1


def age(copy: Copy) -> float:
    """When the row was added, as a number, so that "the older row" can be sorted on. A row whose stamp cannot
    be read counts as the newest, which keeps it out of the tie-break rather than deciding it."""
    try:
        return datetime.fromisoformat(copy.created).timestamp()
    except ValueError:
        return float("inf")


def find(listed: list[Copy]) -> list[Merge]:
    """The groups of rows that are one track, each already decided: the best copy keeps, the rest go.

    Ordering is the whole decision: **lossless beats lossy**, then the row more of the
    library leans on (plays, playlist lines, My Tags, cues), then the row rekordbox has measured (a length is
    more than a row without one knows), then the older row, then the lower id — so the answer is the same every
    time a pass is run, whatever order the database hands the rows over in.
    """
    buckets: dict[tuple[str, str], list[list[Copy]]] = defaultdict(list)
    for copy in sorted(listed, key=lambda one: (one.key, one.seconds or 0, one.content_id)):
        for bucket in buckets[copy.key]:
            if agrees(bucket[0], copy) and parsing.same_credit(bucket[0].artist, copy.artist):
                bucket.append(copy)
                break
        else:
            buckets[copy.key].append([copy])

    merges: list[Merge] = []
    for groups in buckets.values():
        for group in groups:
            if len(group) < 2:
                continue
            best = max(group, key=lambda one: (one.lossless, one.used(), one.seconds is not None,
                                               -age(one), -one.content_id))
            merges.append(Merge(keep=best, drops=[one for one in group if one is not best]))
    return merges


def plan(config: Config, *, db) -> Summary:
    """What a pass would merge, writing nothing."""
    return Summary(merges=find(copies(db)))


def followers(db) -> list[str]:
    """The one-per-row tables that hang off a track in *this* schema, so a row can be taken out cleanly.

    Looked up rather than hardcoded: rekordbox's schema is versioned — the copy this was built against has no
    `djmdContentRelated` — and a delete against a table that is not there would take the whole merge down.
    """
    session = db.session
    names = [row[0] for row in session.execute(text(
        "select name from sqlite_master where type = 'table' and name like 'djmd%'")).fetchall()]
    out: list[str] = []
    for table in names:
        if table in MOVED or table == "djmdContent":
            continue
        columns = [row[1] for row in session.execute(text(f"pragma table_info({table})")).fetchall()]
        if "ContentID" in columns:
            out.append(table)
    return out


def _move(db, keep: int, drop: int, table: str, column: str) -> None:
    """Move the rows of `table` that hang off `drop` onto `keep`, dropping the ones `keep` already has."""
    session = db.session
    for value in [row[0] for row in session.execute(
            text(f"select {column} from {table} where ContentID = :i"), {"i": drop}).fetchall()]:
        already = session.execute(text(
            f"select count(*) from {table} where ContentID = :k and {column} = :v"),
            {"k": keep, "v": value}).scalar()
        if already:
            session.execute(text(f"delete from {table} where ContentID = :i and {column} = :v"),
                            {"i": drop, "v": value})
        else:
            session.execute(text(f"update {table} set ContentID = :k where ContentID = :i and {column} = :v"),
                            {"k": keep, "i": drop, "v": value})


def merge(config: Config, summary: Summary, *, db, remove_files: bool = True, log=print) -> Summary:
    """Write the merges: one backup, one commit, and a diff of the collection that is the rows that changed."""
    if importing.is_running():
        raise NotClosed("rekordbox is running, and pyrekordbox will not commit while it is — close it, then "
                        "run this again")

    where = importing.backup(db)
    if where is not None:
        log(f"backed the collection up to {where.name}")

    session = db.session
    rest = followers(db)
    doomed: list[Path] = []
    for merge in summary.merges:
        keep = merge.keep.content_id
        for drop in merge.drops:
            _move(db, keep, drop.content_id, "djmdSongPlaylist", "PlaylistID")
            _move(db, keep, drop.content_id, "djmdSongMyTag", "MyTagID")
            session.execute(text("update djmdSongHistory set ContentID = :k where ContentID = :i"),
                            {"k": keep, "i": drop.content_id})
            session.execute(text("update djmdCue set ContentID = :k where ContentID = :i"),
                            {"k": keep, "i": drop.content_id})
            for table in rest:
                session.execute(text(f"delete from {table} where ContentID = :i"), {"i": drop.content_id})
            session.execute(text("delete from djmdContent where ID = :i"), {"i": drop.content_id})
            log(f"merged {drop.path.name} into {merge.keep.path.name}")
            if remove_files and path_key(drop.path) != path_key(merge.keep.path):
                doomed.append(drop.path)
    db.session.commit()
    # The file goes only after the commit: a failed commit must not leave the collection pointing at a
    # path this removed, the way `importing.run` orders it.
    for path in doomed:
        if not path.is_file():
            continue
        path.unlink()
        summary.files.append(path)
        log(f"      {path.name} removed from the lanes")
    return summary


def run(config: Config, *, db, apply: bool = False, remove_files: bool = True, log=print) -> Summary:
    """Read, decide, and write only when asked."""
    summary = plan(config, db=db)
    if not apply or not summary.merges:
        return summary
    return merge(config, summary, db=db, remove_files=remove_files, log=log)
