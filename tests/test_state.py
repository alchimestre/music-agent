"""The job: records round-trip, verdicts are computed rather than stored, and a run knows when it ends."""

from __future__ import annotations

import json
from pathlib import Path

from music_agent import config as config_module
from music_agent import state
from music_agent.rules import Candidate


def make_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\n', encoding="utf-8")
    return config_module.load(path)


def a_settled_track(idx: int = 1) -> state.Record:
    """One Ready candidate, so the system would take it without asking."""
    return state.Record(idx=idx, artist="Kiasmos", title="Blurred", duration_s=449,
                        candidates=[Candidate(user="vinyl_only", path=r"M\Kiasmos - Blurred.flac",
                                              duration=448)])


def a_track_needing_you(idx: int = 2) -> state.Record:
    """An unverifiable length, so it cannot be settled alone."""
    return state.Record(idx=idx, artist="Ntogn", title="Mines", duration_s=528,
                        candidates=[Candidate(user="mole", path=r"M\Ntogn - Mines.aiff",
                                              duration=None)])


def test_a_new_run_writes_its_files(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "Some Playlist")
    assert job.root.is_dir()
    assert job.tracks_path.is_file()
    assert job.events_path.is_file()
    assert [event["event"] for event in job.events()] == ["created"]
    assert job.id.endswith("-some-playlist")


def test_records_round_trip(tmp_path):
    job = state.create(make_config(tmp_path), "run")
    job.write_records([a_settled_track(), a_track_needing_you()])

    read_back = job.records()
    assert [r.idx for r in read_back] == [1, 2]
    assert read_back[0].artist == "Kiasmos"
    assert read_back[1].candidates[0].user == "mole"
    assert read_back[1].candidates[0].duration is None


def test_a_record_computes_its_own_verdicts(tmp_path):
    """Verdicts come from the rule, so a rule correction reaches every old job."""
    settled = a_settled_track()
    assert settled.settled() is not None
    assert settled.needs_you() is False

    waiting = a_track_needing_you()
    assert waiting.settled() is None
    assert waiting.needs_you() is True


def test_verdicts_are_never_written_to_the_file(tmp_path):
    """Only the facts are stored; a stored label could drift away from the rule."""
    job = state.create(make_config(tmp_path), "run")
    job.write_records([a_settled_track()])

    line = json.loads(job.tracks_path.read_text(encoding="utf-8").strip())
    assert line["candidates"][0]["user"] == "vinyl_only"
    for forbidden in ("label", "reason", "verdict", "ready"):
        assert forbidden not in json.dumps(line).lower()


def test_an_empty_file_reads_as_no_records(tmp_path):
    assert state.create(make_config(tmp_path), "run").records() == []


def test_a_record_written_with_an_unknown_field_still_loads(tmp_path):
    """Old jobs keep working when the record grows a field."""
    job = state.create(make_config(tmp_path), "run")
    job.tracks_path.write_text(
        json.dumps({"idx": 1, "artist": "A", "title": "T", "something_new": 7}) + "\n",
        encoding="utf-8")
    assert job.records()[0].title == "T"


def test_the_resume_target_is_the_newest_unfinished_run(tmp_path):
    config = make_config(tmp_path)
    first = state.create(config, "first")
    state.create(config, "second")              # created a moment later, so it sorts last
    assert state.newest_unfinished(config).id != first.id


def test_a_finished_run_is_not_the_resume_target(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "only")
    job.finish()
    assert job.finished() is True
    assert state.newest_unfinished(config) is None


def test_events_append_in_order(tmp_path):
    job = state.create(make_config(tmp_path), "run")
    job.append_event("searching", query="kiasmos blurred")
    job.append_event("downloaded", idx=1, path="/tmp/x.flac")
    assert [event["event"] for event in job.events()] == ["created", "searching", "downloaded"]
    assert job.events()[2]["idx"] == 1


def test_the_status_file_records_the_counts(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "run")
    job.write_records([a_settled_track(), a_track_needing_you()])
    job.write_status(config)

    text = job.status_path.read_text(encoding="utf-8")
    assert "Tracks total: 2" in text
    assert "In review: 1" in text
    assert "Settled: 1" in text
    assert f"State root: {job.root}" in text


def test_find_returns_a_run_by_id(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "named")
    assert state.find(config, job.id) is job.root or state.find(config, job.id).id == job.id
    assert state.find(config, "no-such-run") is None


def test_a_state_root_that_does_not_exist_yet_has_no_runs(tmp_path):
    assert state.all_jobs(make_config(tmp_path)) == []
    assert state.newest_unfinished(make_config(tmp_path)) is None
