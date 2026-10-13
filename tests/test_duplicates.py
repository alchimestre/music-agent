"""The dedupe verb: which copy wins, what counts as one track, and the write path against a real schema.

The decision is the interesting part and it is tested on its own — `find` takes copies and returns merges, no
database anywhere near it. The last test opens a **copy** of the real collection and merges two rows in it,
which is the only way to know the write path works rather than merely looks right; nothing of the user's is
touched.
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


# --- which copy wins --------------------------------------------------------------------------------------


def test_the_lossless_copy_wins_over_the_lossy_one():
    """Ticket 09's rule, and the reason the verb exists: a hand import leaves a second row beside the copy the
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
    """A name alone has never been enough (the design notes): the same title at a different length is a different
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

    def fake_run(config, *, db, apply=False, quarantine_files=True, log=print):
        seen.append((apply, quarantine_files))
        return duplicates.Summary(merges=[duplicates.Merge(
            keep=copy(1, "Perfect Stranger - Timewarp.aiff"),
            drops=[copy(2, "Perfect Stranger - Timewarp.mp3", plays=3)])])

    monkeypatch.setattr(duplicates, "run", fake_run)
    monkeypatch.setattr("pyrekordbox.Rekordbox6Database", lambda *args, **kwargs: object())
    config = replace(config_module.DEFAULTS, duplicates=tmp_path / "Duplicates")

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


# --- against a copy of the real collection ----------------------------------------------------------------


def real_database(tmp_path):
    """A working copy of the user's collection, or a skip when there is none to copy."""
    from pyrekordbox import Rekordbox6Database

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")
    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    return Rekordbox6Database(work)


def test_a_merge_on_a_copy_of_the_real_collection(tmp_path):
    """The write path against the real schema: two rows become one, and what hung off the row that went — a
    playlist line and a My Tag — is on the survivor afterwards."""
    from sqlalchemy import text

    database = real_database(tmp_path)
    session = database.session

    keeper = session.execute(text(
        "select ID, FolderPath, Title from djmdContent where rb_local_deleted = 0 "
        "and FolderPath like '%.aiff' order by ID limit 1")).fetchone()
    assert keeper is not None
    duplicate_id = 900000001
    # a second row for the same track, pointing at a file of our own, with a playlist line and a tag on it
    session.execute(text("create table scratch as select * from djmdContent where 1 = 0"))
    session.execute(text("insert into scratch select * from djmdContent where ID = :i"), {"i": keeper[0]})
    session.execute(text("update scratch set ID = :new, FolderPath = :path, Length = Length"),
                    {"new": duplicate_id, "path": str(tmp_path / "duplicate.mp3")})
    session.execute(text("insert into djmdContent select * from scratch"))
    playlist_id = session.execute(text(
        "select ID from djmdPlaylist where ID not in "
        "(select PlaylistID from djmdSongPlaylist where ContentID = :k) limit 1"), {"k": keeper[0]}).scalar()
    tag_id = session.execute(text(
        "select ID from djmdMyTag where ID not in "
        "(select MyTagID from djmdSongMyTag where ContentID = :k) limit 1"), {"k": keeper[0]}).scalar()
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
    (tmp_path / "duplicate.mp3").write_bytes(b"the other copy")

    config = replace(config_module.DEFAULTS, duplicates=tmp_path / "Duplicates",
                     lossless=tmp_path, lossy=tmp_path)
    summary = duplicates.plan(config, db=database)
    mine = [merge for merge in summary.merges
            if any(drop.content_id == duplicate_id for drop in merge.drops)]
    assert len(mine) == 1, "the row we made has to be found as a duplicate"
    assert mine[0].keep.content_id == int(keeper[0]), "the real row is lossless, so it keeps"

    def quiet(*args, **kwargs):
        return None
    duplicates.apply(config, summary, db=database, log=quiet)

    assert session.execute(text("select count(*) from djmdContent where ID = :i"),
                           {"i": duplicate_id}).scalar() == 0
    assert int(session.execute(text("select ContentID from djmdSongPlaylist where ID = 900000002")).scalar()) \
        == int(keeper[0]), "the playlist line moved onto the surviving row"
    assert int(session.execute(text("select ContentID from djmdSongMyTag where ID = 900000003")).scalar()) \
        == int(keeper[0]), "and so did the My Tag"
    assert not (tmp_path / "duplicate.mp3").exists(), "the redundant file left the lanes"
    assert (tmp_path / "Duplicates" / "duplicate.mp3").is_file()
    again = duplicates.plan(config, db=database)
    assert not [merge for merge in again.merges
                if any(drop.content_id == duplicate_id for drop in merge.drops)], \
        "and a second pass has nothing left to do with the row it merged"
