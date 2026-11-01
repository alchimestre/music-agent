"""SQLite admission keeps ordered submissions separate from shared work."""

from __future__ import annotations

from pathlib import Path

from music_agent import config as config_module
from music_agent import state, store


def make_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\n'
                    'lossless = "lossless"\nlossy = "lossy"\n', encoding="utf-8")
    return config_module.load(path)


def make_record(idx: int, *, artist: str = "Kiasmos", title: str = "Blurred",
                mix: str = "", duration_s: int | None = 449, replaces: str = "",
                decision: str = state.UNDECIDED, status: str = state.PENDING,
                local_path: str = "") -> state.Record:
    return state.Record(idx=idx, artist=artist, title=title, mix=mix, duration_s=duration_s,
                        replaces=replaces, decision=decision, status=status, local_path=local_path)


def admit(config, name: str, records: list[state.Record]):
    job = state.create(config, name)
    matches = store.admit_submission(config.state_root, job.id, records)
    return job, matches


def test_overlapping_submissions_keep_duplicate_entries_and_share_work(tmp_path):
    config = make_config(tmp_path)
    entries = [make_record(1), make_record(2, artist="KIASMOS", title="Blurréd!",
                                         mix="Original Version", duration_s=450)]

    preview = store.preview_submission(config.state_root, entries)
    assert preview[0].work_id == preview[1].work_id
    assert [(match.existing, match.created) for match in preview] == [(False, True), (False, False)]

    first, first_matches = admit(config, "first", entries)
    second, second_matches = admit(config, "second", [make_record(1, mix="Original Mix")])

    assert [match.idx for match in first_matches] == [1, 2]
    assert first_matches[0].work_id == first_matches[1].work_id == second_matches[0].work_id
    assert [match.created for match in first_matches] == [True, False]
    assert second_matches[0].existing is True
    assert second_matches[0].created is False
    assert [record.idx for record in first.records()] == [1, 2]
    assert len(second.records()) == 1


def test_named_versions_conflicting_durations_and_replacements_stay_distinct(tmp_path):
    config = make_config(tmp_path)
    entries = [
        make_record(1, mix="Original Mix", duration_s=300, replaces="M:/held-one.flac"),
        make_record(2, mix="Kate Stein Remix", duration_s=360, replaces="M:/held-one.flac"),
        make_record(3, mix="Original Version", duration_s=355, replaces="M:/held-one.flac"),
        make_record(4, mix="Original Mix", duration_s=300, replaces="M:/held-two.flac"),
    ]

    _, matches = admit(config, "versions", entries)

    assert len({match.work_id for match in matches}) == len(entries)
    assert all(match.created for match in matches)


def test_conflicting_historic_decisions_stay_submission_local(tmp_path):
    config = make_config(tmp_path)
    skipped, skipped_matches = admit(
        config, "skipped", [make_record(1, decision=state.SKIPPED)])
    accepted, accepted_matches = admit(
        config, "accepted", [make_record(1, decision=state.ACCEPTED,
                                          status=state.DOWNLOADED, local_path="shared.flac")])

    assert skipped_matches[0].work_id == accepted_matches[0].work_id
    assert skipped.records()[0].decision == state.SKIPPED
    assert accepted.records()[0].decision == state.ACCEPTED
    assert skipped.records()[0].local_path == "shared.flac"
    assert skipped.records()[0].status == state.DOWNLOADED


def test_delivery_state_is_independent_per_target_and_destination(tmp_path):
    config = make_config(tmp_path)
    _, matches = admit(config, "delivery", [make_record(1)])
    work_id = matches[0].work_id
    target = "rekordbox"

    store.set_delivery(config.state_root, work_id, target, "library-a", "success")
    store.set_delivery(config.state_root, work_id, target, "library-b", "failed", error="offline")

    assert store.delivery_status(config.state_root, work_id, target, "library-a") == "success"
    assert store.delivery_status(config.state_root, work_id, target, "library-b") == "failed"
    assert store.delivery_status(config.state_root, work_id, "traktor", "library-a") is None


def test_state_create_returns_populated_job_with_stable_work_mapping(tmp_path):
    config = make_config(tmp_path)
    job = state.create(config, "created", records=[make_record(1)])
    original_work_id = job.work_id(1)

    record = job.records()[0]
    record.title = "Changed title"
    job.write_records([record])

    assert job.records()[0].title == "Changed title"
    assert job.work_id(1) == original_work_id


def test_execution_view_collapses_work_and_keeps_all_job_memberships(tmp_path):
    config = make_config(tmp_path)
    job = state.create(
        config, "duplicates",
        records=[
            make_record(1, decision=state.SKIPPED),
            make_record(2, decision=state.ACCEPTED),
        ],
    )

    execution = job.execution()
    records = execution.records()
    assert [record.idx for record in records] == [2]

    records[0].status = state.DOWNLOADED
    records[0].local_path = "shared.flac"
    execution.write_records(records)

    all_records = job.records()
    assert [record.idx for record in all_records] == [1, 2]
    assert [record.decision for record in all_records] == [state.SKIPPED, state.ACCEPTED]
    assert [record.local_path for record in all_records] == ["shared.flac", "shared.flac"]
