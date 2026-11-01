"""The job: records round-trip, verdicts are computed rather than stored, and a run knows when it ends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import state, store
from music_agent.rules import Candidate


def make_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\n'
                    'lossless = "lossless"\nlossy = "lossy"\n', encoding="utf-8")
    return config_module.load(path)


def a_settled_track(idx: int = 1) -> state.Record:
    """One Ready candidate, so the system would take it without asking."""
    return state.Record(idx=idx, artist="Kiasmos", title="Blurred", duration_s=449,
                        candidates=[Candidate(user="vinyl_only", path=r"M\Kiasmos - Blurred.flac",
                                              duration=448)])


def a_track_needing_you(idx: int = 2) -> state.Record:
    """A filename that names no track at all, which is what the queue is left for: nothing about the file
    can be read, so somebody has to look at it."""
    return state.Record(idx=idx, artist="Ntogn", title="Mines", duration_s=528,
                        candidates=[Candidate(user="mole", path=r"M\Ntogn - .aiff",
                                              duration=528)])


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
    assert read_back[1].candidates[0].duration == 528


def test_a_record_computes_its_own_verdicts(tmp_path):
    """Verdicts come from the rule, so a rule correction reaches every old job."""
    settled = a_settled_track()
    assert settled.settled() is not None
    assert settled.needs_you() is False

    waiting = a_track_needing_you()
    assert waiting.settled() is None
    assert waiting.needs_you() is True



def test_an_empty_file_reads_as_no_records(tmp_path):
    assert state.create(make_config(tmp_path), "run").records() == []


def test_legacy_records_ignore_unknown_fields_and_default_refusals(tmp_path):
    config = make_config(tmp_path)
    legacy_job = config.state_root / "old-job"
    legacy_job.mkdir(parents=True)
    tracks = legacy_job / state.TRACKS
    tracks.write_text(json.dumps({
        "idx": 1, "artist": "A", "title": "T", "tried": ["p:a.flac"], "something_new": 7,
    }) + "\n", encoding="utf-8")
    (legacy_job / state.EVENTS).write_text(json.dumps({"event": "created"}) + "\n", encoding="utf-8")

    store.import_legacy(config.state_root)

    imported = state.find(config, "old-job")
    assert imported.records()[0].title == "T"
    assert imported.records()[0].refused == []
def test_a_refusal_round_trips(tmp_path):
    job = state.create(make_config(tmp_path), "run")
    refused = a_settled_track()
    refused.refused = [r"vinyl_only:M\Kiasmos - Blurred.flac"]
    job.write_records([refused])

    assert job.records()[0].refused == refused.refused



def test_the_resume_target_is_the_newest_unfinished_run(tmp_path):
    config = make_config(tmp_path)
    first = state.create(config, "first")
    state.create(config, "second")              # created a moment later, so it sorts last
    assert state.newest_unfinished(config).id != first.id


def test_the_newest_run_is_the_last_one_by_id(tmp_path):
    config = make_config(tmp_path)
    state.create(config, "first")
    second = state.create(config, "second")

    assert state.newest(config).id == second.id


def test_a_finished_run_is_still_the_newest_run(tmp_path):
    """A run the import has closed is still the run whose queue, genres and leftovers are worth reading."""
    config = make_config(tmp_path)
    job = state.create(config, "run")
    job.finish()

    assert state.newest_unfinished(config) is None
    assert state.newest(config).id == job.id


def test_no_runs_at_all_is_no_newest_run(tmp_path):
    assert state.newest(make_config(tmp_path)) is None


def test_a_finished_run_is_not_the_resume_target(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "only")
    job.finish()
    assert job.finished() is True
    assert state.newest_unfinished(config) is None


def test_a_run_a_kill_left_behind_is_not_the_resume_target(tmp_path):
    """A killed launch still reads as unfinished, because nothing writes its finish event. It used to
    shadow everything that ran after it, the scan reaching back past the run that did finish."""
    config = make_config(tmp_path)
    state.create(config, "killed")
    ran = state.create(config, "ran-after")     # created a moment later, so it sorts last
    ran.finish()

    assert state.newest_unfinished(config) is None
    assert state.newest(config).id == ran.id


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


def test_the_names_that_fitted_nothing_are_counted_across_runs(tmp_path):
    """A single tracklist is the wrong window for "enough tracks"."""
    config = make_config(tmp_path)
    first = state.create(config, "one")
    first.append_event(state.UNGENERED_EVENT, idx=1, names=["Goa Trance", "Krautrock"])
    second = state.create(config, "two")
    second.append_event(state.UNGENERED_EVENT, idx=1, names=["Goa Trance"])
    second.append_event("searched", idx=1, added=3)

    assert state.ungenred_names([first, second]) == {"Goa Trance": 2, "Krautrock": 1}


# ── the queue, split by why a track is in it ─────────────────────


def a_track_with_only_a_rejection(idx: int = 3) -> state.Record:
    """An aiff half a minute longer than the reference, so the rule rules it out rather than asking."""
    return state.Record(idx=idx, artist="Sven Wunder", title="Lotus", duration_s=260,
                        candidates=[Candidate(user="crate", path=r"C\Sven Wunder - Lotus.aiff",
                                              duration=290)])


def test_a_track_the_rule_can_settle_is_not_in_the_queue(tmp_path):
    """The pass owes that track, not a person — it never reaches the review queue."""
    job = state.create(make_config(tmp_path), "run")
    job.write_records([a_settled_track(idx=1)])

    waiting = state.queue(job)
    assert waiting.undecided == []
    assert waiting.decided == []


def test_the_queue_separates_the_four_reasons_a_track_is_still_open(tmp_path):
    job = state.create(make_config(tmp_path), "run")
    job.write_records([
        a_settled_track(idx=1),
        a_track_needing_you(idx=2),
        a_track_with_only_a_rejection(idx=3),
        state.Record(idx=4, artist="Bogus", title="Rare"),
        state.Record(idx=5, artist="Bought", title="Elsewhere"),
    ])
    job.append_event("searched", idx=4, added=0, offered=0)     # asked, and the network held nothing
    records = job.records()
    records[4].decision = state.DECLINED
    records[4].status = state.NOT_FOUND
    job.write_records(records)

    waiting = state.queue(job)

    assert [r.idx for r in waiting.needs_you] == [2]
    assert [r.idx for r in waiting.refused] == [3]
    assert [r.idx for r in waiting.nothing_offered] == [4]
    assert waiting.not_searched == []
    assert [r.idx for r in waiting.decided] == [5]


def test_a_search_that_never_happened_is_not_a_fact_about_the_track(tmp_path):
    """The whole point: no candidates and no search is not the same as a search that came back
    empty."""
    config = make_config(tmp_path)
    job = state.create(config, "run")
    job.write_records([state.Record(idx=1, artist="Bogus", title="Rare")])

    assert [r.idx for r in state.queue(job).not_searched] == [1]
    assert state.queue(job).nothing_offered == []

    job.append_event("searched", idx=1, added=0, offered=0)

    assert state.queue(job).not_searched == []
    assert [r.idx for r in state.queue(job).nothing_offered] == [1]


def test_a_decided_track_is_not_asked_about_again(tmp_path):
    job = state.create(make_config(tmp_path), "run")
    job.write_records([a_track_needing_you(idx=2)])
    records = job.records()
    records[0].decision = state.SKIPPED
    job.write_records(records)

    waiting = state.queue(job)
    assert waiting.needs_you == []
    assert [r.idx for r in waiting.decided] == [2]


def test_the_summary_says_why_nothing_was_usable(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "run")
    job.write_records([a_settled_track(idx=1), a_track_needing_you(idx=2),
                       a_track_with_only_a_rejection(idx=3),
                       state.Record(idx=4, artist="Bogus", title="Rare")])

    summary = job.summary()
    assert summary["tracks"] == 4
    assert summary["in_review"] == 1
    assert summary["settled"] == 1
    assert summary["not_searched"] == 1
    assert summary["nothing_offered"] == 0
    assert summary["refused"] == 1
    assert summary["nothing_usable"] == 2
    assert summary["decided"] == 0


def test_the_status_file_carries_the_split(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "run")
    job.write_records([a_track_needing_you(idx=2), state.Record(idx=4, artist="Bogus", title="Rare")])
    job.write_status(config)

    text = job.status_path.read_text(encoding="utf-8")
    assert "Not searched: 1" in text
    assert "The network offered nothing: 0" in text
    assert "Every offer refused: 0" in text


def test_legacy_import_retries_without_duplicate_history(tmp_path):
    config = make_config(tmp_path)
    first_root = write_legacy_job(
        config.state_root, "20250101T000000Z-first",
        [state.Record(idx=1, artist="A", title="T", decision=state.SKIPPED).to_json()],
        [{"event": "created"}, {"event": "skipped", "idx": 1}],
    )
    second_root = write_legacy_job(
        config.state_root, "20250102T000000Z-second",
        [state.Record(idx=1, artist="A", title="T", decision=state.ACCEPTED).to_json()],
        [{"event": "created"}, {"event": "accepted", "idx": 1}],
    )
    second_events = second_root / state.EVENTS
    second_events.write_text("{broken json\n", encoding="utf-8")
    original_tracks = (first_root / state.TRACKS).read_text(encoding="utf-8")
    state.Job(first_root)
    assert not (config.state_root / "state.sqlite3").exists()

    with pytest.raises(json.JSONDecodeError):
        store.import_legacy(config.state_root)

    first = state.Job(first_root)
    assert first.records()[0].decision == state.SKIPPED
    backup = first_root / "legacy-backup" / state.TRACKS
    assert backup.read_text(encoding="utf-8") == original_tracks

    second_events.write_text(json.dumps({"event": "created"}) + "\n", encoding="utf-8")
    store.import_legacy(config.state_root)
    store.import_legacy(config.state_root)

    first = state.Job(first_root)
    second = state.Job(second_root)
    assert first.records()[0].decision == state.SKIPPED
    assert second.records()[0].decision == state.ACCEPTED
    assert first.work_id(1) == second.work_id(1)
    assert len(first.events()) == 2
    assert len(second.events()) == 1
    first.write_records([state.Record(idx=1, artist="A", title="T", decision=state.SKIPPED,
                                      notes="edited")])
    assert backup.read_text(encoding="utf-8") == original_tracks


def test_constructor_does_not_import_unlocked_legacy_state(tmp_path):
    config = make_config(tmp_path)
    legacy_job = write_legacy_job(
        config.state_root, "old-job", [{"idx": 1, "artist": "A", "title": "T"}],
        [{"event": "created"}],
    )
    original = (legacy_job / state.TRACKS).read_text(encoding="utf-8")

    state.Job(legacy_job)

    assert (legacy_job / state.TRACKS).read_text(encoding="utf-8") == original
    assert not (config.state_root / "state.sqlite3").exists()


def test_projection_rebuild_preserves_replacement_provenance(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "projection")
    record = state.Record(idx=1, artist="A", title="T", replaces=r"M:\held\T.mp3",
                          local_path=str(tmp_path / "T.aiff"))
    job.write_records([record])
    job.tracks_path.unlink()

    store.rebuild_projections(config.state_root)

    projected = json.loads(job.tracks_path.read_text(encoding="utf-8").strip())
    assert projected["replaces"] == r"M:\held\T.mp3"
    assert projected["local_path"] == str(tmp_path / "T.aiff")


def test_writer_lock_excludes_a_second_process(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from music_agent.store import WriterLocked, writer_lock\n"
        "root = Path(sys.argv[1])\n"
        "try:\n"
        "    with writer_lock(root):\n"
        "        raise SystemExit('lock acquired twice')\n"
        "except WriterLocked:\n"
        "    print('locked')\n"
    )
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, environment.get("PYTHONPATH", "")) if part)

    with store.writer_lock(root):
        result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True,
                                text=True, env=environment, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "locked"


def write_legacy_job(root: Path, job_id: str, records: list[dict],
                     events: list[dict]) -> Path:
    job_root = root / job_id
    job_root.mkdir(parents=True)
    (job_root / state.TRACKS).write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    (job_root / state.EVENTS).write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return job_root


def test_legacy_import_reuses_only_existing_artifacts(tmp_path):
    config = make_config(tmp_path)
    existing_path = tmp_path / "present.aiff"
    existing_path.write_bytes(b"audio")
    missing_path = tmp_path / "missing.aiff"
    legacy_job = write_legacy_job(
        config.state_root, "legacy",
        [
            state.Record(idx=1, artist="A", title="Missing", status=state.IMPORTED,
                         format="aiff", local_path=str(missing_path)).to_json(),
            state.Record(idx=2, artist="B", title="Present", status=state.IMPORTED,
                         format="aiff", local_path=str(existing_path)).to_json(),
        ],
        [{"event": "created"}],
    )

    store.import_legacy(config.state_root)

    records = state.Job(legacy_job).records()
    assert records[0].status == state.RETRY_LATER
    assert records[0].local_path == ""
    assert records[1].status == state.IMPORTED
    assert records[1].local_path == str(existing_path)
