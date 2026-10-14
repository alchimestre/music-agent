"""The backfill: which lane files get a row, which are refused and why, and the mint path against a real
schema.

`import` is scoped to a run's records, so a file placed in a lane outside a run never gets a row — this is the
other direction from `dedupe`, and the decisions are tested with no database near them. The last test mints a
row in a **copy** of the real collection, which is the only way to know that path works.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from audio_files import aiff
from test_duplicates import copy

from music_agent import backfill, duplicates, tag
from music_agent import config as config_module
from music_agent.duplicates import Copy


@pytest.fixture(autouse=True)
def rekordbox_is_not_running(monkeypatch):
    monkeypatch.setattr(backfill.importing, "is_running", lambda: False)


def lanes(tmp_path: Path) -> config_module.Config:
    (tmp_path / "Loseless Tracks").mkdir(exist_ok=True)
    (tmp_path / "Losy Tracks").mkdir(exist_ok=True)
    return replace(config_module.DEFAULTS, lossless=tmp_path / "Loseless Tracks",
                   lossy=tmp_path / "Losy Tracks", duplicates=tmp_path / "Duplicates")


def held(identifier: int, name: str, *, artist: str = "Someone", title: str = "Something",
         seconds: int = 200) -> Copy:
    return Copy(content_id=identifier, path=Path("/Music/Loseless Tracks") / name, artist=artist,
                title=title, mix="", seconds=seconds)


# --- what is a candidate, and what is not -----------------------------------------------------------------


def test_a_merge_is_reached_by_run_with_apply(tmp_path, monkeypatch):
    summary = duplicates.Summary(merges=[duplicates.Merge(
        keep=copy(1, "Perfect Stranger - Timewarp.aiff"),
        drops=[copy(2, "Perfect Stranger - Timewarp.mp3")])])
    monkeypatch.setattr(duplicates, "plan", lambda config, *, db: summary)
    reached: list[str] = []
    monkeypatch.setattr(duplicates, "merge", lambda config, found, *, db, **rest: reached.append("merge"))
    duplicates.run(config_module.DEFAULTS, db=object(), apply=True)
    assert reached == ["merge"], "run with apply has to reach the merge"


def test_a_mint_is_reached_by_run_with_apply(tmp_path, monkeypatch):
    summary = backfill.Summary(candidates=[backfill.Candidate(path=Path("/x.aiff"), title="t", artist="a")])
    monkeypatch.setattr(backfill, "plan", lambda config, *, db: summary)
    reached: list[str] = []
    monkeypatch.setattr(backfill, "mint", lambda config, found, *, db, **rest: reached.append("mint"))
    backfill.run(config_module.DEFAULTS, db=object(), apply=True)
    assert reached == ["mint"], "run with apply has to reach the mint"


def test_a_lane_file_with_no_row_is_a_candidate(tmp_path):
    """The gap itself: a file in a lane the collection has no row for."""
    config = lanes(tmp_path)
    aiff(config.lossless / "Band X - Tune.aiff", frames=44100)          # a second of silence, so it measures
    [candidate] = backfill.candidates(config, [held(1, "other.aiff")])
    assert candidate.addable
    assert (candidate.artist, candidate.title) == ("Band X", "Tune"), "the name when the tags are silent"
    assert candidate.seconds == 1
    assert "add" in candidate.line()


def test_a_file_with_tags_is_named_by_them(tmp_path):
    """Ticket 15's order: the tags first, the name only as the fallback."""
    config = lanes(tmp_path)
    path = aiff(config.lossless / "ripped - by - nobody.aiff")
    tag.write(path, tag.Tags(title="Real Title", artist="Real Artist"))
    [candidate] = backfill.candidates(config, [])
    assert (candidate.artist, candidate.title) == ("Real Artist", "Real Title")


def test_a_file_the_collection_already_holds_is_refused(tmp_path):
    """Minting here would make a duplicate, which is `dedupe`'s business: the track is held, under another
    file, and the candidate says where."""
    config = lanes(tmp_path)
    aiff(config.lossless / "Band X - Tune.aiff", frames=44100)
    rows = [held(1, "Band X - Tune.aiff", title="Tune", artist="Band X", seconds=1)]
    [candidate] = backfill.candidates(config, rows)
    assert not candidate.addable
    assert candidate.held_by is not None and candidate.held_by.name == "Band X - Tune.aiff"
    assert "already holds this track" in candidate.why
    assert "skip" in candidate.line()


def test_a_row_for_a_different_length_is_not_the_same_track(tmp_path):
    """The one-second window, as everywhere else: a file a minute longer than the row is a different version."""
    config = lanes(tmp_path)
    aiff(config.lossless / "Band X - Tune.aiff", frames=44100)
    [candidate] = backfill.candidates(config, [held(1, "elsewhere.aiff", title="Tune", artist="Band X",
                                                    seconds=200)])
    assert candidate.addable


def test_a_file_that_is_not_audio_is_reported_not_added(tmp_path):
    config = lanes(tmp_path)
    (config.lossless / "Broken - File.aiff").write_bytes(b"audio")
    [candidate] = backfill.candidates(config, [])
    assert not candidate.addable
    assert candidate.why == "not readable audio"


def test_a_file_a_row_already_points_at_is_not_a_candidate(tmp_path):
    config = lanes(tmp_path)
    path = aiff(config.lossless / "Band X - Tune.aiff")
    assert backfill.candidates(config, [held(1, "Band X - Tune.aiff")])[0].path == path
    assert backfill.candidates(config, [Copy(1, path, "Tune", "Band X", "")]) == [], \
        "a row pointing at the file means there is no gap"


# --- against a copy of the real collection ----------------------------------------------------------------


def test_a_row_is_minted_in_a_copy_of_the_real_collection(tmp_path):
    """The mint path against the real schema, which is the only way to know it works rather than looks right:
    the file's own metadata becomes a row, and a second pass finds nothing left to do."""
    from pyrekordbox import Rekordbox6Database
    from sqlalchemy import text

    live = Path("~/Library/Pioneer/rekordbox/master.db").expanduser()
    if not live.is_file():
        pytest.skip("no rekordbox collection here to copy")
    work = tmp_path / "master.db"
    shutil.copy2(live, work)
    database = Rekordbox6Database(work)

    config = lanes(tmp_path)
    path = aiff(config.lossless / "Backfill Test - Nothing Like It.aiff")
    tag.write(path, tag.Tags(title="Nothing Like It", artist="Backfill Test", album="A Test"))

    summary = backfill.plan(config, db=database)
    assert [one.path for one in summary.addable()] == [path]

    def quiet(*args, **kwargs):
        return None

    backfill.mint(config, summary, db=database, log=quiet)
    assert [one.path for one in summary.minted] == [path]

    row = database.session.execute(text(
        "select Title, FolderPath, FileNameL, Length from djmdContent where FolderPath = :p"),
        {"p": str(path)}).fetchone()
    assert row is not None, "the row has to be there"
    # rekordbox's Length is the header's length *rounded* (this file is 0.018 s), where Traktor's PLAYTIME
    # rounds up — each collection written the way it reads
    assert (row[0], row[3]) == ("Nothing Like It", 0), "named from the file's tags, measured from its header"
    artist = database.session.execute(text(
        "select a.Name from djmdContent c join djmdArtist a on a.ID = c.ArtistID "
        "where c.FolderPath = :p"), {"p": str(path)}).scalar()
    assert artist == "Backfill Test", "and the artist row is looked up or minted, not invented"

    again = backfill.plan(config, db=database)
    assert not again.addable(), "a second pass must find the file held"
