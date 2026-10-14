"""The import stage: what was tagged, into the rekordbox collection.

Its own command, because pyrekordbox refuses to commit while rekordbox is running. The write goes through
pyrekordbox rather than the MCP, which buys one batch commit and one backup per run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import library, state, tag
from .config import Config


class NotClosed(RuntimeError):
    """Rekordbox is running, and pyrekordbox will not commit while it is."""


class ImportFailed(RuntimeError):
    """A track could not be added to the collection."""


# What a rekordbox collection has rows for. pyrekordbox looks this up in its own `FileType` table, but it
# catches the wrong exception doing it — an extension it does not know escapes as AttributeError — so one
# unsupported file took a whole import down with it, before anything had been committed.
COLLECTION_FORMATS = frozenset({".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif"})


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


def backup(db) -> Path | None:
    """Copy the database **and its write-ahead files**.

    A copy without `-wal` can describe a state that never existed: the write-ahead file holds a megabyte of
    the collection's most recent changes.
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


def file_columns(path: Path) -> dict:
    """A row's file-derived columns, in rekordbox's own units: `BitRate` is kbps and `FileSize` bytes.

    Measured against the library rather than guessed. The suffix maps onto pyrekordbox's `FileType` member
    names, which is safe here because `process.place` renames an `.aif` as it lands — the enum has no `AIF`.
    """
    from pyrekordbox.db6.tables import FileType

    sound = library.sound_of(path)
    columns: dict = {"FileNameL": path.name, "FileSize": path.stat().st_size}
    member = getattr(FileType, path.suffix.lstrip(".").upper(), None)
    if member is not None:
        columns["FileType"] = member.value
    if sound.seconds is not None:
        columns["Length"] = int(sound.seconds)
    if sound.sample_rate:
        columns["SampleRate"] = int(sound.sample_rate)
    if sound.bit_rate:
        columns["BitRate"] = int(sound.bit_rate) // 1000        # kbps, as rekordbox stores it
    if sound.bit_depth:
        columns["BitDepth"] = int(sound.bit_depth)
    return columns


def find_or_create(rows, create, name: str):
    """The row for a name, minted only when the collection does not already have it.

    pyrekordbox's `add_*` helpers refuse a name that already exists, so every relation is looked up before it
    is minted — otherwise the first House track would fail on the collection's own House row.
    """
    wanted = name.strip().casefold()
    if wanted:
        for row in rows:
            if row.Name and row.Name.strip().casefold() == wanted:
                return row
    return create(name.strip())


def add(db, record: state.Record) -> object:
    """One tagged track into the collection, and the row it became.

    The row is built from the file's **own tags** rather than the record, and carries title, artist, album,
    label, genre, year, length, sample rate and bit rate — never BPM or key, which rekordbox's own analysis
    fills.
    """
    path = Path(record.local_path)
    if not path.is_file():
        raise ImportFailed(f"the file is gone: {path}")
    if path.suffix.lower() not in COLLECTION_FORMATS:
        raise ImportFailed(f"the collection cannot point at {path.suffix.lower() or 'an extensionless file'}"
                           f" — {path}")

    written = tag.read(path)
    columns = file_columns(path)
    title = written.title or record.title or path.stem
    artist = written.artist or record.artist
    genre = written.genre or record.genre
    year = written.year.strip()

    # A relation takes a row or an id, never a name: a bare name string dies inside SQLAlchemy.
    relations = {}
    if artist:
        relations["ArtistID"] = find_or_create(db.get_artist(), db.add_artist, artist).ID
    if written.album:
        relations["AlbumID"] = find_or_create(db.get_album(), db.add_album, written.album).ID
    if written.label:
        relations["LabelID"] = find_or_create(db.get_label(), db.add_label, written.label).ID
    if genre:
        relations["GenreID"] = find_or_create(db.get_genre(), db.add_genre, genre).ID

    row_fields = {name: columns[name] for name in ("Length", "SampleRate", "BitRate", "BitDepth")
                  if name in columns}
    return db.add_content(
        str(path),
        Title=title,
        ReleaseYear=int(year) if year.isdigit() else None,
        **row_fields,
        **relations,
    )


def repoint(db, record: state.Record, log) -> bool:
    """Point the row that already holds this track at the new file.

    A plain write of the path, deliberately **not** `update_content_path`: that helper also moves the track's
    ANLZ analysis, which fails when that lives under `~/Library/Pioneer/rekordbox/share`. The plain write
    keeps the row's ID and UUID — the UUID is what the analysis is filed under, so the beat grid and the cues
    stay with the track. The file-derived columns are rewritten too, because rekordbox reads *those* for the
    bitrate and the size it displays.
    """
    row = next((one for one in db.get_content() if one.FolderPath == record.replaces), None)
    if row is None:
        return False
    path = Path(record.local_path)
    if not path.is_file():
        # Never point a row at a file that is not there: this project reports missing files, it does not
        # make them.
        raise ImportFailed(f"the file is gone: {record.local_path}")
    row.FolderPath = str(path)
    for column, value in file_columns(path).items():
        setattr(row, column, value)
    log(f"      it replaces {Path(record.replaces).name}")
    return True


def run(config: Config, job: state.Job, *, db, backup_first: bool = True, log=print) -> Summary:
    """Every tagged track into the collection, behind one backup and two commits — see the comment on the
    first commit for why the upgrades and the additions cannot share one."""
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
        where = backup(db)
        if where is not None:
            log(f"backed the collection up to {where.name}")

    replaced: list[Path] = []
    pending: list[state.Record] = []

    def failed(record: state.Record, error: Exception) -> None:
        record.notes = str(error)
        summary.failed.append(record.idx)
        job.append_event("import_failed", idx=record.idx, error=str(error))
        log(f"[{record.idx}] {record.label}: {error}")

    for record in tagged:
        if record.replaces and Path(record.replaces) == Path(record.local_path):
            # Nothing should produce a row that already points at the file this run made: repoint would
            # write the path back and the delete below would then remove the file, leaving the row pointing
            # at nothing. Refused rather than trusted.
            record.notes = "the row already points at this file"
            summary.failed.append(record.idx)
            job.append_event("refused", idx=record.idx, reason="replaces == local_path")
            log(f"[{record.idx}] {record.label}: the row already points at this file — left alone")
            continue
        if not record.replaces:
            pending.append(record)
            continue
        try:
            repointed = repoint(db, record, log)
        except ImportFailed as error:
            failed(record, error)
            continue
        if not repointed:
            log(f"[{record.idx}] {record.label}: the row it was to replace is gone — adding it")
            pending.append(record)
            continue
        record.status = state.IMPORTED
        record.notes = ""
        summary.repointed.append(record.idx)
        replaced.append(Path(record.replaces))
        job.append_event("repointed", idx=record.idx, was=record.replaces, now=record.local_path)
        log(f"[{record.idx}] {record.label}: upgraded — the row now points at the lossless file")

    # The upgrades go in first, on their own commit. pyrekordbox mints a new row's ID as an *int* while every
    # ID read back from the collection is *text*, and a flush carrying both kinds has to sort them together,
    # which SQLAlchemy refuses: `InvalidRequestError: Could not sort objects by primary key`. On 2026-10-13 a
    # run of 47 upgrades and 4 additions computed every write and then committed none of them.
    if replaced:
        db.commit(autoinc=True)

    for record in pending:
        try:
            add(db, record)
        except ImportFailed as error:
            failed(record, error)
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
        summary.imported.append(record.idx)
        job.append_event("imported", idx=record.idx, path=record.local_path)
        log(f"[{record.idx}] {record.label}: imported")

    if pending:
        db.commit(autoinc=True)             # the additions, likewise
    job.write_records(records)              # after the commits, so a crash cannot lose it

    for old in replaced:
        # The replaced file goes only after the commit, so nothing is deleted while the collection still
        # points at it.
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
