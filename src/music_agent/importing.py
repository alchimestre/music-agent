"""The import stage: what was tagged, into the rekordbox collection.

Ticket 07: this is its own command because pyrekordbox refuses to commit while rekordbox is running — the
commit raises outright — so acquisition and processing run with rekordbox open and the import runs once it
is closed. Ticket 02: the write goes through pyrekordbox rather than the MCP, which buys one batch commit
and one backup per run instead of the MCP's per-track backup.

Four things read out of pyrekordbox rather than assumed, all of them found by trying:

- `add_content` takes the file **path** positionally and the row's own columns as keywords. The relations
  need rows or ids rather than names: `Artist="Someone"` dies inside SQLAlchemy with `'str' object has no
  attribute '_sa_instance_state'`.
- Its `add_*` helpers **refuse a name that already exists**, so every relation is looked up before it is
  minted — otherwise the first House track would fail on the collection's own House row.
- A path already in the collection raises `ValueError`: a duplicate to report, not a failure to retry.
- The database's own file is on the session's bind, which is how the backup knows what to copy.

What is written is the list the design notes names and nothing else — title, artist, album, label, genre, year,
length, sample rate, bit rate — and **never BPM or key**, which rekordbox's own analysis fills. Checked on
a copy of the collection: both read back empty on a row this project added.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import library, state, tag
from .config import Config


class NotClosed(RuntimeError):
    """Rekordbox is running, and pyrekordbox will not commit while it is."""


class ImportFailed(RuntimeError):
    """A track could not be added to the collection."""


@dataclass
class Summary:
    """What a pass came to, by track."""

    imported: list[int] = field(default_factory=list)
    repointed: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.imported)} imported"]
        if self.repointed:
            parts.append(f"{len(self.repointed)} upgraded")
        if self.already:
            parts.append(f"{len(self.already)} already in the collection")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


def is_running() -> bool:
    """Whether rekordbox is up, asked the same way pyrekordbox asks before it refuses to commit."""
    from pyrekordbox.utils import get_rekordbox_pid

    return bool(get_rekordbox_pid())


def database_path(db) -> Path | None:
    """The file behind an open database, so the backup can copy the right one."""
    try:
        found = db.session.get_bind().url.database
    except Exception:
        return None
    return Path(found) if found else None


def backup(db, log) -> Path | None:
    """Copy the database **and its write-ahead files**.

    A database copied without `-wal` can describe a state that never existed: at the time of writing the
    write-ahead file held a megabyte of the collection's most recent changes. A backup is only worth taking
    if it is the collection.
    """
    source = database_path(db)
    if source is None or not source.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = source.with_name(f"{source.name}.{stamp}.backup")
    for suffix in ("", "-wal", "-shm"):
        beside = Path(f"{source}{suffix}")
        if beside.is_file():
            shutil.copy2(beside, Path(f"{target}{suffix}"))
    return target


def find_or_create(rows, create, name: str):
    """The row for a name, minted only when the collection does not already have it."""
    wanted = name.strip().casefold()
    if wanted:
        for row in rows:
            if row.Name and row.Name.strip().casefold() == wanted:
                return row
    return create(name.strip())


def add(db, record: state.Record, log) -> object:
    """One tagged track into the collection, and the row it became.

    The row is built from the file's **own tags**, not from the job: the design notes made the tags the cache, so
    the album, the label and the year are read back from the file rather than carried around in a record
    that deliberately does not hold them.
    """
    path = Path(record.local_path)
    if not path.is_file():
        raise ImportFailed(f"the file is gone: {path}")

    written = tag.read(path)
    sound = library.sound_of(path)
    title = written.title or record.title or path.stem
    artist = written.artist or record.artist
    genre = written.genre or record.genre
    year = written.year.strip()

    relations = {}
    if artist:
        relations["ArtistID"] = find_or_create(db.get_artist(), db.add_artist, artist).ID
    if written.album:
        relations["AlbumID"] = find_or_create(db.get_album(), db.add_album, written.album).ID
    if written.label:
        relations["LabelID"] = find_or_create(db.get_label(), db.add_label, written.label).ID
    if genre:
        relations["GenreID"] = find_or_create(db.get_genre(), db.add_genre, genre).ID

    return db.add_content(
        str(path),
        Title=title,
        ReleaseYear=int(year) if year.isdigit() else None,
        Length=sound.seconds,
        SampleRate=sound.sample_rate,
        BitRate=sound.bit_rate,
        **relations,
    )


def repoint(db, record: state.Record, log) -> bool:
    """Point the row that already holds this track at the new file (tickets 09 and 16).

    A plain write of the path, deliberately **not** `update_content_path`: that helper also reads and moves
    the track's ANLZ analysis, locating it from the audio file's own drive, so it fails for a master
    collection whose analysis lives under `~/Library/Pioneer/rekordbox/share`. This keeps the row's ID and
    its UUID, and the UUID is what the analysis is filed under — so the beat grid and the cues stay with
    the track without a file being moved.
    """
    row = next((one for one in db.get_content() if one.FolderPath == record.replaces), None)
    if row is None:
        return False
    if not Path(record.local_path).is_file():
        # Never point a row at a file that is not there: that is how a collection acquires rows whose
        # files have gone, and this project reports those rather than making them.
        raise ImportFailed(f"the file is gone: {record.local_path}")
    row.FolderPath = str(Path(record.local_path))
    log(f"      it replaces {Path(record.replaces).name}")
    return True


def run(config: Config, job: state.Job, *, db, backup_first: bool = True, log=print) -> Summary:
    """Every tagged track into the collection, behind one commit and one backup."""
    if is_running():
        raise NotClosed("rekordbox is running, and pyrekordbox will not commit while it is — close it, "
                        "then run this again")

    records = job.records()
    tagged = [record for record in records if record.status == state.TAGGED]
    summary = Summary()
    if not tagged:
        log("nothing to import: no track in this run has been tagged yet")
        return summary

    if backup_first:
        where = backup(db, log=log)
        if where is not None:
            log(f"backed the collection up to {where.name}")

    replaced: list[Path] = []
    for record in tagged:
        try:
            repointed = repoint(db, record, log) if record.replaces else False
            if not repointed:
                if record.replaces:
                    log(f"[{record.idx}] {record.label}: the row it was to replace is gone — adding it")
                add(db, record, log)
        except ImportFailed as error:
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("import_failed", idx=record.idx, error=str(error))
            log(f"[{record.idx}] {record.label}: {error}")
            continue
        except ValueError as error:
            # pyrekordbox raises this for a path already in the collection: a duplicate, not a failure.
            record.status = state.FOUND_IN_LIBRARY
            record.notes = str(error)
            summary.already.append(record.idx)
            job.append_event("already_imported", idx=record.idx, path=record.local_path)
            log(f"[{record.idx}] {record.label}: already in the collection")
            continue
        record.status = state.IMPORTED
        record.notes = ""
        if repointed:
            summary.repointed.append(record.idx)
            replaced.append(Path(record.replaces))
            job.append_event("repointed", idx=record.idx, was=record.replaces, now=record.local_path)
            log(f"[{record.idx}] {record.label}: upgraded — the row now points at the lossless file")
        else:
            summary.imported.append(record.idx)
            job.append_event("imported", idx=record.idx, path=record.local_path)
            log(f"[{record.idx}] {record.label}: imported")

    db.commit(autoinc=True)                 # one commit for the whole run (the design notes)
    job.write_records(records)              # after the commit, so a crash cannot lose it

    for old in replaced:
        # the user's decision, 2026-10-12: the file being replaced goes. After the commit and never before,
        # so nothing can be deleted while the collection still points at it.
        try:
            if old.is_file():
                old.unlink()
                job.append_event("replaced_file_deleted", path=str(old))
                log(f"      removed the lossy file it replaced: {old.name}")
        except OSError as error:
            log(f"      could not remove {old.name}: {error}")
    job.write_status(config)
    if not summary.failed:
        # The pipeline ends here, so this is where a run is finished: acquire and process leave it open
        # deliberately, so the next command picks the same run up without being told which.
        job.finish()
    return summary
