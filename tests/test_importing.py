"""The import stage: the field set, the relations, the duplicate, and a real copy of the collection.

Most of these run against a stand-in that records what it was asked to write, because the interesting
decisions are about *what* is written. The last one opens a copy of the real database, which is the only
way to know the write path works rather than merely looks right.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio_files import aiff

from music_agent import config as config_module
from music_agent import importing, state, tag


@pytest.fixture(autouse=True)
def rekordbox_is_not_running(monkeypatch):
    """Whether rekordbox is open is a property of the machine, not of this code, and it must not fail the suite.

    `importing.run` refuses to commit while the app is up, and pyrekordbox guards its own `commit` the same
    way, so a suite run with rekordbox open goes red for a reason that has nothing to do with the change
    under test. Both are patched here. The guard keeps its own test, which patches `is_running` again and so
    overrides this; the two tests that write to a copy of the real schema touch only a temp file.
    """
    import pyrekordbox.db6.database

    monkeypatch.setattr(importing, "is_running", lambda: False)
    monkeypatch.setattr(pyrekordbox.db6.database, "get_rekordbox_pid", lambda: None)


class Row:
    def __init__(self, name: str, identifier: int) -> None:
        self.Name = name
        self.ID = identifier


class ContentRow:
    """A row of the collection itself, mutable in the one field the repoint touches."""

    def __init__(self, path: str, identifier: int = 42, title: str = "Blurred") -> None:
        self.FolderPath = path
        self.ID = identifier
        self.Title = title


class FakeDatabase:
    """Records what it was asked to add, and refuses a path twice the way pyrekordbox does."""

    def __init__(self, *, genre: str = "Techno", artist: str = "") -> None:
        self.existing = {"genre": [genre] if genre else [], "artist": [artist] if artist else [],
                         "album": [], "label": []}
        self.added: list[dict] = []
        self.content: list[ContentRow] = []
        self.committed = 0
        self._next = 100
        self.session = SimpleNamespace(get_bind=lambda: SimpleNamespace(
            url=SimpleNamespace(database="")))

    def _rows(self, kind: str) -> list[Row]:
        return [Row(name, 1) for name in self.existing[kind]]

    def _mint(self, kind: str, name: str) -> Row:
        self.existing[kind].append(name)
        self._next += 1
        return Row(name, self._next)

    def get_content(self): return list(self.content)
    def get_genre(self): return self._rows("genre")
    def get_artist(self): return self._rows("artist")
    def get_album(self): return self._rows("album")
    def get_label(self): return self._rows("label")
    def add_genre(self, name): return self._mint("genre", name)
    def add_artist(self, name): return self._mint("artist", name)
    def add_album(self, name): return self._mint("album", name)
    def add_label(self, name): return self._mint("label", name)

    def add_content(self, path: str, **fields):
        if any(entry["path"] == path for entry in self.added):
            raise ValueError(f"Track with path '{path}' already exists in database")
        self.added.append({"path": path, **fields})
        return Row(Path(path).stem, 999)

    def commit(self, autoinc: bool = True) -> None:
        self.committed += 1


def make_config(tmp_path: Path) -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(f'[paths]\nstaging = "{tmp_path}/staging"\n'
                    f'lossless = "{tmp_path}/Loseless Tracks"\n'
                    f'lossy = "{tmp_path}/Losy Tracks"\nstate = "state"\n', encoding="utf-8")
    (tmp_path / "staging").mkdir()
    return config_module.load(path)


def tagged(tmp_path: Path, index: int = 1, name: str = "Kiasmos - Blurred.aiff", *,
           artist: str = "Kiasmos", title: str = "Blurred", genre: str = "Techno",
           album: str = "Blurred", label: str = "Erased Tapes", year: str = "2017") -> state.Record:
    """A processed track: the file carries the tags, and the record carries what the pipeline knew.

    The album, label and year live only in the tags — the tags are the cache — so the import reads them
    back from the file, and a test has to put them there.
    """
    path = aiff(tmp_path / "staging" / name)
    tag.write(path, tag.Tags(title=title, artist=artist, album=album, label=label, year=year,
                             genre=genre), log=quiet)
    return state.Record(idx=index, artist=artist, title=title, genre=genre,
                        local_path=str(path), format="aiff", status=state.TAGGED)


def job_with(config: config_module.Config, records: list[state.Record]) -> state.Job:
    job = state.create(config, "test")
    job.write_records(records)
    return job


def quiet(*_: object) -> None:
    pass


# ── what it writes ───────────────────────────────────────────────────────────


def test_a_tagged_track_becomes_a_row(tmp_path):
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path)])
    database = FakeDatabase()

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.imported == [1]
    (written,) = database.added
    assert written["Title"] == "Blurred"
    assert written["ReleaseYear"] == 2017
    assert written["GenreID"] == 1, "the collection's own Techno, not a new one"
    assert written["Length"] == 0, "the tiny fixture's own length"
    assert written["SampleRate"] == 44100
    assert written["BitRate"] == 1411, "kbps: the header says 1411200 and the collection stores kbps"
    assert written["BitDepth"] == 16
    assert job.records()[0].status == state.IMPORTED


def test_a_file_the_collection_has_no_row_for_is_reported_rather_than_crashing(tmp_path):
    """pyrekordbox looks an extension up in its own table and catches the wrong exception doing it, so one
    unsupported file escaped as AttributeError and took the whole import down — nothing committed yet."""
    from audio_files import opus

    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", format="opus",
                          local_path=str(opus(tmp_path / "Kiasmos - Blurred.opus")),
                          status=state.TAGGED)

    with pytest.raises(importing.ImportFailed, match=r"cannot point at \.opus"):
        importing.add(FakeDatabase(), record)


def test_bpm_and_key_are_never_written(tmp_path):
    """rekordbox's own analysis is the source of both."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path)])
    database = FakeDatabase()

    importing.run(config, job, db=database, backup_first=False, log=quiet)

    (written,) = database.added
    assert "BPM" not in written
    assert not any("key" in name.lower() for name in written)


def test_one_commit_covers_every_addition_in_a_run(tmp_path):
    """One batch commit rather than one per track. Three additions are one commit, not three."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path, 1, "One.aiff"), tagged(tmp_path, 2, "Two.aiff"),
                            tagged(tmp_path, 3, "Three.aiff")])
    database = FakeDatabase()

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert len(summary.imported) == 3
    assert database.committed == 1


def test_an_upgrade_and_an_addition_do_not_share_a_commit(tmp_path):
    """2026-10-13: they cannot. pyrekordbox mints a new row's ID as an int while every ID read back from the
    collection is text, and one flush carrying both kinds has to sort them together, which raises
    `InvalidRequestError: Could not sort objects by primary key` — the run of 47 upgrades and 4 additions
    that computed every write and committed none of them."""
    config = make_config(tmp_path)
    upgraded = tagged(tmp_path, 1, "One.aiff")
    upgraded.replaces = str(a_lossy_file(tmp_path, "Old.mp3"))
    added = tagged(tmp_path, 2, "Two.aiff")
    job = job_with(config, [upgraded, added])
    database = FakeDatabase()
    database.content = [ContentRow(upgraded.replaces)]

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.repointed == [1], summary.report()
    assert summary.imported == [2], summary.report()
    assert database.committed == 2, "the upgrades commit first, the additions after"


def test_an_existing_relation_is_reused_and_a_new_one_is_minted(tmp_path):
    """pyrekordbox refuses to add a name that exists, so looking first is the only way to reuse a row."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path, artist="Somebody New", genre="Techno")])
    database = FakeDatabase(genre="Techno")

    importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert database.existing["artist"] == ["Somebody New"], "minted once"
    assert database.existing["genre"] == ["Techno"], "and the collection's own Techno used as it is"


def test_a_track_with_no_genre_is_imported_without_one(tmp_path):
    """A blank genre is a real state: the caller decides it, and an import without one is better than a
    guessed one."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path, genre="")])
    database = FakeDatabase(genre="")

    importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert "GenreID" not in database.added[0], "left out so pyrekordbox's own default applies"
    assert database.existing["genre"] == [], "and no genre row is minted for a blank"


def test_importing_leaves_the_run_open_for_the_next_target(tmp_path):
    """One target cannot finish the run: another collection may still have to receive it."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path)])
    assert not job.finished()

    importing.run(config, job, db=FakeDatabase(), backup_first=False, log=quiet)

    assert not job.finished()


def test_nothing_tagged_means_nothing_written(tmp_path):
    config = make_config(tmp_path)
    job = job_with(config, [state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                         status=state.DOWNLOADED)])
    database = FakeDatabase()

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.imported == []
    assert database.added == []
    assert database.committed == 0, "nothing to commit means no commit"


# ── what it refuses ──────────────────────────────────────────────────────────


def test_a_track_already_in_the_collection_is_reported_not_retried(tmp_path):
    """pyrekordbox raises for a path it already has. That is a duplicate, not a failure."""
    config = make_config(tmp_path)
    record = tagged(tmp_path)
    job = job_with(config, [record])
    database = FakeDatabase()
    database.added.append({"path": record.local_path})       # as if a previous run imported it

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.already == [1]
    assert summary.failed == []
    assert job.records()[0].status == state.FOUND_IN_LIBRARY
    assert "already exists" in job.records()[0].notes


def test_a_file_that_is_gone_is_a_failure_to_retry(tmp_path):
    config = make_config(tmp_path)
    record = tagged(tmp_path)
    Path(record.local_path).unlink()
    job = job_with(config, [record])
    database = FakeDatabase()

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.failed == [1]
    assert database.added == []
    assert not job.finished(), "a failed import leaves the run open"


def test_a_running_rekordbox_stops_it_before_anything_is_written(tmp_path, monkeypatch):
    """pyrekordbox refuses to commit while rekordbox runs, so this fails fast instead."""
    config = make_config(tmp_path)
    job = job_with(config, [tagged(tmp_path)])
    database = FakeDatabase()
    monkeypatch.setattr(importing, "is_running", lambda: True)

    with pytest.raises(importing.NotClosed, match="rekordbox is running"):
        importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert database.added == [], "and nothing has been written"
    assert database.committed == 0


# ── the backup ───────────────────────────────────────────────────────────────


def test_the_backup_copies_the_write_ahead_files(tmp_path):
    """A database copied without `-wal` describes a state that never existed."""
    database = FakeDatabase()
    live = tmp_path / "master.db"
    live.write_bytes(b"the collection")
    (tmp_path / "master.db-wal").write_bytes(b"recent changes")
    (tmp_path / "master.db-shm").write_bytes(b"shared memory")
    database.session = SimpleNamespace(get_bind=lambda: SimpleNamespace(
        url=SimpleNamespace(database=str(live))))

    where = importing.backup(database)

    assert where is not None
    assert where.read_bytes() == b"the collection"
    assert Path(f"{where}-wal").read_bytes() == b"recent changes"
    assert Path(f"{where}-shm").read_bytes() == b"shared memory"


def test_a_backup_with_nothing_to_copy_says_so_rather_than_failing(tmp_path):
    assert importing.backup(FakeDatabase()) is None


# ── against the real thing, on a copy ────────────────────────────────────────


def test_a_track_added_to_a_copy_of_the_real_collection(tmp_path):
    """The write path itself, against the real schema. A copy, so the live collection is untouched."""
    from pyrekordbox import Rekordbox6Database

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")

    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    database = Rekordbox6Database(work)
    try:
        config = make_config(tmp_path)
        record = tagged(tmp_path, name="Music Agent - Test Track.aiff", album="Some Album",
                        label="Some Label")
        job = job_with(config, [record])

        summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

        assert summary.imported == [1], summary.report()
        row = next(row for row in database.get_content() if row.Title == "Blurred")
        assert row.Genre.Name == "Techno"
        assert row.Artist.Name == "Kiasmos"
        assert row.Album.Name == "Some Album"
        assert row.Label.Name == "Some Label"
        assert row.ReleaseYear == 2017
        assert row.Length == 0
        assert row.SampleRate == 44100
        assert row.BitRate == 1411, "kbps: the header says 1411200 and the collection stores kbps"
        assert row.BitDepth == 16
        assert row.FileType == 12, "pyrekordbox's own code for an AIFF, which it derives from the suffix"
        assert row.FileNameL == "Music Agent - Test Track.aiff"
        assert row.FileSize == Path(record.local_path).stat().st_size
        assert row.BPM is None, "never written"
        assert getattr(row, "KeyName", None) is None, "never written"
    finally:
        database.close()


# ── the upgrade: repointing a row rather than adding one ─────────────────────


def a_lossy_file(tmp_path: Path, name: str = "Kiasmos - Blurred.mp3") -> Path:
    where = tmp_path / "Losy Tracks" / name
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(b"the lossy copy already held")
    return where


def test_a_record_whose_row_already_points_at_its_file_is_left_alone(tmp_path):
    """The delete at the end is what makes this worth refusing: repoint would write the path, then the file
    it "replaced" would be removed, and the row would end up pointing at nothing at all."""
    config = make_config(tmp_path)
    record = tagged(tmp_path)
    record.replaces = record.local_path                  # the pair nothing should ever produce
    job = job_with(config, [record])
    database = FakeDatabase()
    database.content = [ContentRow(str(record.local_path))]

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.failed == [record.idx]
    assert summary.repointed == [] and summary.imported == []
    assert Path(record.local_path).is_file(), "the file the delete would have taken is still here"


def test_a_record_that_replaces_a_row_repoints_it_rather_than_adding(tmp_path):
    """The upgrade keeps the row, so the cues, the playlists and the history it carries survive the
    swap."""
    config = make_config(tmp_path)
    old = a_lossy_file(tmp_path)
    record = tagged(tmp_path)
    record.replaces = str(old)
    job = job_with(config, [record])
    database = FakeDatabase()
    database.content = [ContentRow(str(old))]

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.repointed == [1], summary.report()
    assert database.added == [], "nothing was added: the row was already there"
    assert database.content[0].FolderPath == record.local_path
    assert database.content[0].ID == 42, "the row keeps its identity, and its analysis with it"
    assert database.committed == 1
    assert not old.is_file(), "by decision: the file being replaced goes"
    assert job.records()[0].status == state.IMPORTED


def test_a_repoint_rewrites_the_columns_that_describe_the_file(tmp_path):
    """On 2026-10-12 every upgraded track still displayed the old m4a's bitrate and size: rekordbox shows
    the row's own columns, not the file it points at, and `repoint` had only moved the path.

    The values come from the file now, in the units rekordbox's own rows carry — its 1,214 AIFFs say 1411
    where mutagen says 1411200 — and 12 is its code for AIFF.
    """
    config = make_config(tmp_path)
    old = a_lossy_file(tmp_path)
    record = tagged(tmp_path)
    record.replaces = str(old)
    job = job_with(config, [record])
    database = FakeDatabase()
    database.content = [ContentRow(str(old))]

    importing.run(config, job, db=database, backup_first=False, log=quiet)

    row = database.content[0]
    assert row.FileNameL == "Kiasmos - Blurred.aiff", "what the old file was called is no longer true"
    assert row.FileType == 12
    assert row.BitRate == 1411, "kbps: the unit rekordbox's own rows use, not the header's bits per second"
    assert row.BitDepth == 16
    assert row.SampleRate == 44100
    assert row.FileSize == Path(record.local_path).stat().st_size


def test_a_repoint_that_finds_no_row_adds_instead_and_keeps_the_file(tmp_path):
    """If the row is gone, the track is simply new — and nothing deletes a file the collection may
    still be using."""
    config = make_config(tmp_path)
    old = a_lossy_file(tmp_path)
    record = tagged(tmp_path)
    record.replaces = str(old)
    job = job_with(config, [record])
    database = FakeDatabase()

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.imported == [1]
    assert summary.repointed == []
    assert old.is_file()


def test_nothing_is_deleted_when_the_import_itself_fails(tmp_path):
    config = make_config(tmp_path)
    old = a_lossy_file(tmp_path)
    record = tagged(tmp_path)
    record.replaces = str(old)
    Path(record.local_path).unlink()                    # the new file is gone
    job = job_with(config, [record])
    database = FakeDatabase()
    database.content = [ContentRow(str(old))]

    summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

    assert summary.failed == [1]
    assert old.is_file(), "the copy in the collection is not collateral"
    assert database.content[0].FolderPath == str(old), "and the row still points at it"


def test_a_repoint_against_a_copy_of_the_real_collection(tmp_path):
    """The mechanism itself, on the real schema: a plain write keeps the row, its ID and its UUID — and
    the UUID is what the analysis is filed under, so the beat grid and the cues stay with the track.

    Everything this touches is a temp file: the copy's row is pointed at one first, so the delete that
    follows can never reach a real one.
    """
    from pyrekordbox import Rekordbox6Database

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")

    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    database = Rekordbox6Database(work)
    try:
        row = next(r for r in database.get_content() if r.FolderPath and "Losy Tracks" in r.FolderPath)
        identifier, uuid, title = row.ID, row.UUID, row.Title
        old = a_lossy_file(tmp_path, "Whatever - Old.mp3")
        row.FolderPath = str(old)
        database.commit(autoinc=True)

        config = make_config(tmp_path)
        record = tagged(tmp_path, name="Whatever - New.aiff")
        record.replaces = str(old)
        job = job_with(config, [record])

        summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

        again = next(r for r in database.get_content() if r.ID == identifier)
        assert summary.repointed == [1], summary.report()
        assert again.FolderPath == record.local_path
        assert again.UUID == uuid, "the analysis is filed under this, so it follows the track"
        assert again.Title == title, "a repoint moves the path and leaves the row alone"
        assert again.BitRate == 1411, "and the columns that describe the file survive the real write"
        assert again.FileType == 12 and again.FileNameL == "Whatever - New.aiff"
        assert not old.is_file(), "and the file it replaced is gone"
    finally:
        database.close()


def test_a_repoint_and_an_add_in_one_run_against_a_copy_of_the_real_collection(tmp_path):
    """2026-10-13: an upgrade run does both. Most tracks repoint the row that held the lossy copy; the ones
    whose row has gone are added. pyrekordbox mints a new row's ID as an *int* — `generate_unused_id` returns
    one — while every ID read back from the collection is *text*, so one flush carrying both has to sort them
    together and cannot: `InvalidRequestError: Could not sort objects by primary key`.

    That is what a run of 47 upgrades and 4 additions hit: it computed every write and committed none.
    """
    from pyrekordbox import Rekordbox6Database

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")

    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    database = Rekordbox6Database(work)
    try:
        row = next(r for r in database.get_content() if r.FolderPath and "Losy Tracks" in r.FolderPath)
        old = a_lossy_file(tmp_path, "Whatever - Old.mp3")
        row.FolderPath = str(old)
        database.commit(autoinc=True)

        config = make_config(tmp_path)
        upgraded = tagged(tmp_path, index=1, name="Whatever - New.aiff")
        upgraded.replaces = str(old)
        added = tagged(tmp_path, index=2, name="Nothing - To Repoint.aiff")
        added.replaces = str(tmp_path / "gone.mp3")
        job = job_with(config, [upgraded, added])

        summary = importing.run(config, job, db=database, backup_first=False, log=quiet)

        assert summary.repointed == [1], summary.report()
        assert summary.imported == [2], summary.report()
        assert next(r for r in database.get_content() if r.FolderPath == upgraded.local_path)
        assert next(r for r in database.get_content() if r.FolderPath == added.local_path)
    finally:
        database.close()
