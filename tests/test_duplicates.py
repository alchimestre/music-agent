"""The dedupe verb: which copy wins, what counts as one track, and the write path against a real schema.

The decision is the interesting part and it is tested on its own — `find` takes copies and returns merges, no
database anywhere near it. The last test opens a **copy** of the real collection and merges two rows in it,
which is the only way to know the write path works rather than merely looks right; the live collection is
untouched.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import duplicates
from music_agent.duplicates import Copy


@pytest.fixture(autouse=True)
def rekordbox_is_not_running(monkeypatch):
    """Whether rekordbox is open is the machine's business, not this code's."""
    import pyrekordbox.db6.database

    monkeypatch.setattr(duplicates.importing, "is_running", lambda: False)
    monkeypatch.setattr(pyrekordbox.db6.database, "get_rekordbox_pid", lambda: None)


def copy(identifier: int, name: str, *, seconds: int = 200, artist: str = "Perfect Stranger",
         title: str = "Timewarp", mix: str = "", **rest) -> Copy:
    return Copy(content_id=identifier, path=Path("/Music/Lanetracks") / name, artist=artist, title=title,
                mix=mix, seconds=seconds, **rest)


class EmptyResult:
    def fetchall(self):
        return []

    def scalar(self):
        return 0


class EmptyDatabase:
    """A database whose every statement matches nothing: enough to test the write path's order and its files."""

    class session:
        @staticmethod
        def execute(*args, **kwargs):
            return EmptyResult()

        @staticmethod
        def commit():
            pass


# --- which copy wins --------------------------------------------------------------------------------------


def test_the_lossless_copy_wins_over_the_lossy_one():
    """The rule, and the reason the verb exists: a hand import leaves a second row beside the copy the
    pipeline upgraded, and the lossless file is the one to keep."""
    mp3 = copy(1, "Perfect Stranger - Timewarp.mp3", plays=9, playlists=2)
    aiff_copy = copy(2, "Perfect Stranger - Timewarp.aiff")
    [merged] = duplicates.find([mp3, aiff_copy])
    assert merged.keep is aiff_copy
    assert merged.drops == [mp3]


def test_between_two_lossy_copies_the_one_the_library_uses_wins():
    quiet = copy(1, "a.mp3", plays=0)
    used = copy(2, "b.mp3", plays=3, playlists=1)
    [merged] = duplicates.find([quiet, used])
    assert merged.keep is used


def test_and_between_two_equals_the_older_row_wins():
    """So the answer does not move when a pass is run twice."""
    newer = copy(2, "b.aiff", created="2026-10-13 12:00:00")
    older = copy(1, "a.aiff", created="2026-06-17 09:00:00")
    [merged] = duplicates.find([newer, older])
    assert merged.keep is older
    assert duplicates.find([older, newer])[0].keep is older, "and it does not depend on the input order"


# --- what counts as one track -----------------------------------------------------------------------------


def test_two_lengths_a_minute_apart_are_two_tracks():
    """A name alone has never been enough: the same title at a different length is a different
    version, and merging them would throw one away."""
    short = copy(1, "a.aiff", seconds=200)
    long = copy(2, "b.aiff", seconds=260)
    assert duplicates.find([short, long]) == []


def test_the_window_is_the_pipelines_one_second():
    one = copy(1, "a.aiff", seconds=200)
    assert duplicates.find([one, copy(2, "b.aiff", seconds=201)])[0].keep is one
    assert duplicates.find([one, copy(3, "c.aiff", seconds=203)]) == []


def test_two_mixes_of_one_title_are_two_tracks():
    original = copy(1, "a.aiff", mix="")
    remix = copy(2, "b.aiff", mix="Spektre remix")
    assert duplicates.find([original, remix]) == []


def test_a_mix_that_names_the_original_is_no_mix():
    """`Happy But Sad (Original Mix)` and `Happy But Sad` are one recording. The key kept them apart while
    `rules.mix_matches` — the rule the rest of the pipeline matches candidates with — reads the two as the
    same, so the pair survived in the collection and a device export carried it twice."""
    plain = copy(1, "Keith Carnal - Happy But Sad.aiff", title="Happy But Sad", seconds=456)
    named = copy(2, "Keith Carnal - Happy But Sad (Original Mix).aiff", title="Happy But Sad",
                 mix="Original Mix", seconds=456)
    [merged] = duplicates.find([plain, named])
    assert merged.keep is plain
    assert merged.drops == [named]


def test_one_collaboration_written_two_ways_is_one_track():
    """`A x B` on one file and `A & B` on the other: the files disagree about the credit, and a key that
    spelled the credit out cannot see that these are the same track."""
    one = copy(1, "Altinbas x Cirkle - Time in Motion.aiff", artist="Altinbas x Cirkle",
               title="Time in Motion", seconds=350)
    other = copy(2, "Altinbas, Cirkle - Time in Motion.aiff", artist="Altinbas & Cirkle",
                 title="Time in Motion", seconds=350)
    [merged] = duplicates.find([one, other])
    assert merged.keep is one
    assert merged.drops == [other]


def test_a_pair_named_as_one_credit_is_the_same_track_as_its_members():
    """`Dettmann | Klock` against `Marcel Dettmann & Ben Klock` — two taggers naming the same two people."""
    pair = copy(1, "Dettmann _ Klock - Phantom Studies.aiff", artist="Dettmann | Klock",
                title="Phantom Studies", seconds=350)
    members = copy(2, "Marcel Dettmann, Ben Klock - Phantom Studies.aiff",
                   artist="Marcel Dettmann & Ben Klock", title="Phantom Studies", seconds=350)
    [merged] = duplicates.find([pair, members])
    assert merged.keep is pair


def test_two_credits_that_name_different_people_are_two_tracks():
    """Everything else agrees, so the credit is the only thing left to tell them apart by."""
    assert duplicates.find([copy(1, "a.aiff", artist="Altinbas"),
                            copy(2, "b.aiff", artist="Phara")]) == []


def test_a_row_with_no_length_still_pairs_with_one_that_has_it():
    """rekordbox measures what it holds, so this is rare — and a row with no length is the one case name
    matching has to serve."""
    unmeasured = copy(1, "a.aiff", seconds=None)
    measured = copy(2, "b.aiff", seconds=200)
    [merged] = duplicates.find([unmeasured, measured])
    assert merged.keep is measured, "the row with a length is the better copy of the two"


# --- what a merge says it will do -------------------------------------------------------------------------


def test_the_verb_prints_the_plan_and_writes_nothing_until_apply(tmp_path, monkeypatch, capsys):
    """The CLI's own shape: the merges printed with their costs, "nothing merged yet", and `--apply` — with
    `--leave-files` — passed through to the module. The database is the one thing the verb reaches for by
    itself, so it is stood in for here; the merge itself is tested against a real schema above."""
    import argparse

    from music_agent import cli

    seen: list[tuple[bool, bool]] = []

    def fake_run(config, *, db, apply=False, remove_files=True, log=print):
        seen.append((apply, remove_files))
        return duplicates.Summary(merges=[duplicates.Merge(
            keep=copy(1, "Perfect Stranger - Timewarp.aiff"),
            drops=[copy(2, "Perfect Stranger - Timewarp.mp3", plays=3)])])

    class Database:
        def close(self) -> None:
            pass

    monkeypatch.setattr(duplicates, "run", fake_run)
    monkeypatch.setattr("pyrekordbox.Rekordbox6Database", lambda *args, **kwargs: Database())
    config = replace(config_module.DEFAULTS)

    assert cli._dedupe(config, argparse.Namespace(apply=False, leave_files=False)) == 0
    printed = capsys.readouterr().out
    assert "1 duplicate group" in printed and "3 plays" in printed
    assert "nothing merged yet" in printed
    assert seen == [(False, True)], "a dry run must not ask for the write either"

    assert cli._dedupe(config, argparse.Namespace(apply=True, leave_files=True)) == 0
    assert seen == [(False, True), (True, False)], "--leave-files has to reach the module"


def test_a_merge_names_what_moves():
    kept = copy(1, "Perfect Stranger - Timewarp.aiff")
    dropped = copy(2, "Perfect Stranger - Timewarp.mp3", playlists=2, plays=3, tags=1, cues=4)
    lines = duplicates.Merge(keep=kept, drops=[dropped]).lines()
    assert "keep" in lines[0] and "lossless" in lines[0]
    assert "2 playlist lines" in lines[1] and "3 plays" in lines[1] and "1 My Tag" in lines[1]
    assert "4 cues" in lines[1]


def test_a_merge_keeps_the_only_file_when_the_keepers_is_gone(tmp_path):
    """The survivor is decided on the row's own merits, not on its file existing: a row whose file was moved
    still wins, and deleting the drop's copy then would leave the track with nothing at all."""
    drop_file = tmp_path / "Perfect Stranger - Timewarp.mp3"
    drop_file.write_bytes(b"the only copy")
    kept = replace(copy(1, "Perfect Stranger - Timewarp.aiff"), path=tmp_path / "gone" / "Timewarp.aiff")
    dropped = replace(copy(2, "Perfect Stranger - Timewarp.mp3"), path=drop_file)

    summary = duplicates.Summary(merges=[duplicates.Merge(keep=kept, drops=[dropped])])
    duplicates.merge(replace(config_module.DEFAULTS, lossless=tmp_path, lossy=tmp_path), summary,
                     db=EmptyDatabase(), log=lambda line: None)

    assert drop_file.is_file(), "the keeper's file is missing, so this one is all the track has"
    assert summary.files == [], "nothing was removed"
    assert any("is not on disk" in line for line in summary.lines()), "and the report says which row it is"


def test_a_drop_file_outside_both_lanes_is_never_unlinked(tmp_path):
    """`plan` only looks inside the lanes; the delete has to check again, because the file is gone for good."""
    lossless = tmp_path / "lossless"
    lossy = tmp_path / "lossy"
    lossless.mkdir()
    keeper_file = lossless / "keeper.aiff"
    keeper_file.write_bytes(b"the keeper")
    outside = tmp_path / "elsewhere" / "duplicate.mp3"
    outside.parent.mkdir()
    outside.write_bytes(b"not this pass's to delete")

    config = replace(config_module.DEFAULTS, lossless=lossless, lossy=lossy)
    summary = duplicates.Summary(merges=[duplicates.Merge(
        keep=replace(copy(1, keeper_file.name), path=keeper_file),
        drops=[replace(copy(2, outside.name), path=outside)])])
    duplicates.merge(config, summary, db=EmptyDatabase(), log=lambda line: None)

    assert outside.is_file(), "a path outside both lanes is never this pass's to remove"
    assert summary.files == []


# --- against a copy of the real collection ----------------------------------------------------------------


def real_database(tmp_path):
    """A working copy of the real collection, or a skip when there is none to copy."""
    from pyrekordbox import Rekordbox6Database

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")
    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    return Rekordbox6Database(work)


def test_a_merge_on_a_copy_of_the_real_collection(tmp_path, request):
    """The write path against the real schema: two rows become one, and what hung off the row that went — a
    playlist line and a My Tag — is on the survivor afterwards."""
    from sqlalchemy import text

    database = real_database(tmp_path)
    request.addfinalizer(database.close)
    session = database.session

    template_id = session.execute(text(
        "select ID from djmdContent where rb_local_deleted = 0 "
        "and FolderPath like '%.aiff' order by ID limit 1")).scalar()
    assert template_id is not None
    keeper_id = 900000000
    duplicate_id = 900000001
    keep_file = tmp_path / "keeper.aiff"
    drop_file = tmp_path / "duplicate.mp3"
    keep_file.write_bytes(b"the keeper")
    drop_file.write_bytes(b"the other copy")
    session.execute(text("create table scratch as select * from djmdContent where 1 = 0"))
    session.execute(text("insert into scratch select * from djmdContent where ID = :i"), {"i": template_id})
    for identifier, path in ((keeper_id, keep_file), (duplicate_id, drop_file)):
        session.execute(text("update scratch set ID = :new, FolderPath = :path"),
                        {"new": identifier, "path": str(path)})
        session.execute(text("insert into djmdContent select * from scratch"))
    playlist_id = session.execute(text(
        "select ID from djmdPlaylist where ID not in "
        "(select PlaylistID from djmdSongPlaylist where ContentID = :k) limit 1"), {"k": keeper_id}).scalar()
    tag_id = session.execute(text(
        "select ID from djmdMyTag where ID not in "
        "(select MyTagID from djmdSongMyTag where ContentID = :k) limit 1"), {"k": keeper_id}).scalar()
    # the lines are copies of real ones, because those tables carry more non-null columns than the ids
    session.execute(text("create table scratch_line as select * from djmdSongPlaylist where 1 = 0"))
    session.execute(text("insert into scratch_line select * from djmdSongPlaylist limit 1"))
    session.execute(text("update scratch_line set ID = 900000002, ContentID = :c, PlaylistID = :p, TrackNo = 1"),
                    {"c": duplicate_id, "p": playlist_id})
    session.execute(text("insert into djmdSongPlaylist select * from scratch_line"))
    session.execute(text("create table scratch_tag as select * from djmdSongMyTag where 1 = 0"))
    session.execute(text("insert into scratch_tag select * from djmdSongMyTag limit 1"))
    session.execute(text("update scratch_tag set ID = 900000003, ContentID = :c, MyTagID = :t"),
                    {"c": duplicate_id, "t": tag_id})
    session.execute(text("insert into djmdSongMyTag select * from scratch_tag"))
    session.commit()

    config = replace(config_module.DEFAULTS, lossless=tmp_path, lossy=tmp_path)
    summary = duplicates.plan(config, db=database)
    [mine] = duplicates.find([row for row in duplicates.copies(database)
                              if row.content_id in {keeper_id, duplicate_id}])
    assert summary.merges == [mine]
    assert mine.keep.path == keep_file
    assert [drop.path for drop in mine.drops] == [drop_file]

    def quiet(*args, **kwargs):
        return None
    duplicates.merge(config, duplicates.Summary(merges=[mine]), db=database, log=quiet)

    assert session.execute(text("select count(*) from djmdContent where ID = :i"),
                           {"i": duplicate_id}).scalar() == 0
    assert int(session.execute(text("select ContentID from djmdSongPlaylist where ID = 900000002")).scalar()) \
        == keeper_id, "the playlist line moved onto the surviving row"
    assert int(session.execute(text("select ContentID from djmdSongMyTag where ID = 900000003")).scalar()) \
        == keeper_id, "and so did the My Tag"
    assert keep_file.is_file()
    assert not drop_file.exists(), "the redundant file is deleted, not kept"
    assert not duplicates.plan(config, db=database).merges


def test_a_merge_repoints_the_survivor_when_the_keepers_file_is_gone(tmp_path, request):
    """The row that goes must not take the last file with it: when the keeper's file is missing, the survivor
    is pointed at the drop's copy rather than left naming a path that is not there."""
    from sqlalchemy import text

    database = real_database(tmp_path)
    request.addfinalizer(database.close)
    session = database.session

    template_id = session.execute(text(
        "select ID from djmdContent where rb_local_deleted = 0 "
        "and FolderPath like '%.aiff' order by ID limit 1")).scalar()
    assert template_id is not None
    keeper_id = 900000010
    duplicate_id = 900000011
    drop_file = tmp_path / "the only copy.mp3"
    drop_file.write_bytes(b"the only copy")
    missing = tmp_path / "gone" / "keeper.aiff"
    session.execute(text("create table scratch as select * from djmdContent where 1 = 0"))
    session.execute(text("insert into scratch select * from djmdContent where ID = :i"), {"i": template_id})
    for identifier, path in ((keeper_id, missing), (duplicate_id, drop_file)):
        session.execute(text("update scratch set ID = :new, FolderPath = :path"),
                        {"new": identifier, "path": str(path)})
        session.execute(text("insert into djmdContent select * from scratch"))
    session.commit()

    config = replace(config_module.DEFAULTS, lossless=tmp_path, lossy=tmp_path)
    summary = duplicates.Summary(merges=[duplicates.Merge(
        keep=replace(copy(keeper_id, missing.name), path=missing),
        drops=[replace(copy(duplicate_id, drop_file.name), path=drop_file)])])
    duplicates.merge(config, summary, db=database, log=lambda line: None)

    assert session.execute(text("select count(*) from djmdContent where ID = :i"),
                           {"i": duplicate_id}).scalar() == 0
    where, name, size = session.execute(text(
        "select FolderPath, FileNameL, FileSize from djmdContent where ID = :k"), {"k": keeper_id}).one()
    assert Path(where) == drop_file
    assert name == drop_file.name
    assert int(size) == drop_file.stat().st_size
    assert drop_file.is_file(), "the file the surviving row now names is the track's only copy"
    assert missing in summary.kept_missing


def test_a_dedupe_plan_ignores_rows_outside_both_lanes(tmp_path):
    lossless = tmp_path / "lossless"
    lossy = tmp_path / "lossy"
    config = replace(config_module.DEFAULTS, lossless=lossless, lossy=lossy)
    keep = replace(copy(1, "keep.aiff"), path=lossless / "keep.aiff")
    drop = replace(copy(2, "drop.mp3"), path=lossy / "drop.mp3")
    outside = replace(copy(3, "outside.aiff"), path=tmp_path / "lossless-other" / "outside.aiff", plays=100)

    plan = duplicates.plan(config, db=None, rows=[keep, drop, outside])

    assert plan.merges == [duplicates.Merge(keep=keep, drops=[drop])]



def test_a_row_with_no_length_cannot_bridge_two_different_recordings():
    merges = duplicates.find([copy(1, "unknown.mp3", seconds=None),
                              copy(2, "short.aiff", seconds=200),
                              copy(3, "long.aiff", seconds=260)])

    assert [(merge.keep.content_id, [drop.content_id for drop in merge.drops]) for merge in merges] \
        == [(2, [1])], "the measured pair is one track; the minute apart from it is not"


def test_one_artist_does_not_make_two_collaborations_one_track():
    merges = duplicates.find([copy(10, "a.aiff", seconds=300, artist="Altinbas"),
                              copy(11, "b.aiff", seconds=300, artist="Altinbas & Cirkle"),
                              copy(12, "c.aiff", seconds=300, artist="Altinbas & Phara")])

    assert [(merge.keep.content_id, [drop.content_id for drop in merge.drops]) for merge in merges] \
        == [(10, [11])], "a second collaboration sharing the lead must not be pulled in through the first"
