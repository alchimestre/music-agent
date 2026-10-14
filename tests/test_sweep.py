"""Staging's own leftovers: an interrupted transfer, and a file no record points at."""

from __future__ import annotations

from pathlib import Path

from music_agent import config as config_module
from music_agent import state, sweep


def make_config(tmp_path: Path) -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nstaging = "{tmp_path}/staging"\nstate = "{tmp_path}/state"\n',
        encoding="utf-8",
    )
    (tmp_path / "staging").mkdir()
    return config_module.load(path)


def test_an_interrupted_transfer_and_a_loose_file_are_both_found(tmp_path):
    config = make_config(tmp_path)
    part = config.staging / "Kiasmos - Blurred.flac.part"
    part.write_bytes(b"")
    loose = config.staging / "Orphan - Thing.aiff"
    loose.write_bytes(b"audio")

    leftovers = sweep.find(config)

    assert leftovers.partial == (part,)
    assert leftovers.loose == (loose,)


def test_a_file_a_record_still_points_at_is_left_alone(tmp_path):
    """That file is a track a pass still owes, whatever its status is."""
    config = make_config(tmp_path)
    owed = config.staging / "Kiasmos - Blurred.flac"
    owed.write_bytes(b"audio")
    job = state.create(config, "test")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred", local_path=str(owed),
                                    status=state.RETRY_LATER)])

    assert sweep.find(config).all == ()


def test_the_finders_own_dotfiles_are_not_the_pipelines_debris(tmp_path):
    config = make_config(tmp_path)
    (config.staging / ".DS_Store").write_bytes(b"junk")

    assert sweep.find(config).all == ()


def test_clearing_removes_them_and_nothing_else(tmp_path):
    config = make_config(tmp_path)
    part = config.staging / "Kiasmos - Blurred.flac.part"
    part.write_bytes(b"")
    owed = config.staging / "Owed - Track.flac"
    owed.write_bytes(b"audio")
    job = state.create(config, "test")
    job.write_records([state.Record(idx=1, artist="Owed", title="Track", local_path=str(owed),
                                    status=state.PENDING)])

    removed = sweep.clear(sweep.find(config), log=lambda *_: None)

    assert removed == 1
    assert not part.exists()
    assert owed.is_file(), "a file a record names is not debris"


def test_an_empty_staging_has_nothing_to_report(tmp_path):
    config = make_config(tmp_path)

    assert sweep.find(config).all == ()
    assert "unaccounted" in sweep.find(config).report()
