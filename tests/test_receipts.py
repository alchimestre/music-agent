from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from music_agent import deliveries, receipts, spotify_source, state, store
from music_agent.config import DEFAULTS


class FakeJob:
    def __init__(self, root: Path, records, events=()):
        self.root = root
        self.id = root.name
        self._records = records
        self._events = list(events)

    def records(self, *, records=None, work_ids=None):
        return self._records

    def execution(self):
        return self

    def events(self):
        return self._events

    def searched(self):
        return set()

    def work_id(self, idx):
        return f"work-{idx}"


def record(idx: int, path: Path | None, **fields):
    if path is not None:
        path.write_bytes(fields.pop("audio", b"audio"))
    values = dict(idx=idx, artist=f"Artist {idx}", title=f"Title {idx}", mix="",
                  status=state.TAGGED, decision=state.ACCEPTED,
                  local_path=str(path) if path else "", genre="Techno")
    values.update(fields)
    return state.Record(**values)

def plan(target: str, destination: str, *, blocked=False, paths=()):
    return deliveries.DeliveryPlan(target, destination, "", False, blocked=blocked,
                                   error="target unavailable" if blocked else "",
                                   verified_artifacts=tuple(str(Path(path).resolve()) for path in paths))


def receipt_config(tmp_path):
    return replace(DEFAULTS, state_root=tmp_path, export_targets=("rekordbox", "traktor"))


def test_empty_reserved_submission_is_not_complete(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "reserved", [])
    monkeypatch.setattr(receipts.deliveries, "delivery_receipts", lambda config, job, plans=None: {})

    report = receipts.build(receipt_config(tmp_path), job)

    assert report["complete"] is False


def test_duplicate_memberships_get_separate_rows_for_every_target(tmp_path, monkeypatch):
    audio = tmp_path / "audio.flac"
    first = record(1, audio)
    second = record(2, audio)
    job = FakeJob(tmp_path / "job", [first, second])
    config = receipt_config(tmp_path)
    targets = [SimpleNamespace(name="rekordbox"), SimpleNamespace(name="traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    monkeypatch.setattr(store, "work_ids", lambda root, job_id: {1: "shared", 2: "shared"})
    plans = [plan("rekordbox", "rb", paths=[audio]), plan("traktor", "tk", paths=[audio])]
    monkeypatch.setattr(store, "delivery_statuses", lambda root, job_id: {
        ("shared", plan.target, deliveries._collection_key(plan.destination, first, "shared")): "success"
        for plan in plans})
    monkeypatch.setattr(deliveries, "delivery_plans", lambda config, job: plans)

    receipts = deliveries.delivery_receipts(config, job)

    assert list(receipts) == [1, 2]
    assert set(receipts[1]) == {"rekordbox", "traktor"}
    assert receipts[1]["rekordbox"]["status"] == "success"
    assert receipts[2]["traktor"]["status"] == "success"


def test_receipt_requires_current_artifact_and_current_destination(tmp_path, monkeypatch):
    audio = tmp_path / "audio.flac"
    item = record(1, audio)
    job = FakeJob(tmp_path / "job", [item])
    config = receipt_config(tmp_path)
    monkeypatch.setattr(deliveries.exporting, "configured",
                        lambda config: [SimpleNamespace(name="rekordbox")])
    monkeypatch.setattr(store, "work_ids", lambda root, job_id: {1: "work-1"})
    old_destination = deliveries._collection_key("old-target", item, "work-1")
    monkeypatch.setattr(store, "delivery_statuses",
                        lambda root, job_id: {("work-1", "rekordbox", old_destination): "success"})
    monkeypatch.setattr(deliveries, "delivery_plans", lambda config, job: [
        plan("rekordbox", "new-target", paths=[audio])])

    receipt = deliveries.delivery_receipts(config, job)[1]["rekordbox"]

    assert receipt["status"] == "pending"
    assert "success" not in receipt["status"]


def test_changed_artifact_invalidates_old_success_receipt(tmp_path, monkeypatch):
    audio = tmp_path / "audio.flac"
    item = record(1, audio)
    job = FakeJob(tmp_path / "job", [item])
    config = receipt_config(tmp_path)
    monkeypatch.setattr(deliveries.exporting, "configured",
                        lambda config: [SimpleNamespace(name="rekordbox")])
    monkeypatch.setattr(store, "work_ids", lambda root, job_id: {1: "work-1"})
    old_destination = deliveries._collection_key("rb", item, "work-1")
    audio.write_bytes(b"replacement audio")
    monkeypatch.setattr(store, "delivery_statuses",
                        lambda root, job_id: {("work-1", "rekordbox", old_destination): "success"})

    receipt = deliveries.delivery_receipts(config, job, [plan("rekordbox", "rb", paths=[audio])])[1]["rekordbox"]

    assert receipt["status"] == "pending"


def test_blocked_current_plan_overrides_saved_success(tmp_path, monkeypatch):
    item = record(1, tmp_path / "audio.flac")
    job = FakeJob(tmp_path / "job", [item])
    config = receipt_config(tmp_path)
    monkeypatch.setattr(deliveries.exporting, "configured",
                        lambda config: [SimpleNamespace(name="rekordbox")])
    monkeypatch.setattr(store, "work_ids", lambda root, job_id: {1: "work-1"})
    monkeypatch.setattr(store, "delivery_statuses", lambda *args: {})

    receipt = deliveries.delivery_receipts(
        config, job, [plan("rekordbox", "rb", blocked=True)])[1]["rekordbox"]

    assert receipt["status"] == "failed"


def test_source_snapshot_mapping_is_added_to_each_record(tmp_path, monkeypatch):
    item = record(7, tmp_path / "audio.flac")
    job = FakeJob(tmp_path / "job", [item])
    monkeypatch.setattr(spotify_source, "snapshot", lambda job: {
        "id": "playlist-id", "name": "Playlist", "uri": "spotify:playlist:playlist-id",
        "tracks": [{"uri": "spotify:track:track-id"}],
        "entries": [{"idx": 7, "position": 4, "uri": "spotify:track:track-id"}],
    })
    config = replace(DEFAULTS, state_root=tmp_path, export_targets=())
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [])

    report = receipts.build(config, job, plans=[])

    assert report["source"]["uri"] == "spotify:playlist:playlist-id"
    assert report["records"][0]["uri"] == "spotify:track:track-id"
    assert report["records"][0]["position"] == 4


def test_partial_and_missing_targets_never_report_complete(tmp_path, monkeypatch):
    item = record(1, tmp_path / "audio.flac")
    job = FakeJob(tmp_path / "job", [item])
    config = receipt_config(tmp_path)
    targets = [SimpleNamespace(name="rekordbox"), SimpleNamespace(name="traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    monkeypatch.setattr(store, "work_ids", lambda root, job_id: {1: "work-1"})
    monkeypatch.setattr(store, "delivery_statuses",
                        lambda root, job_id: {("work-1", "rekordbox",
                                               deliveries._collection_key("rb", item, "work-1")): "success"})
    monkeypatch.setattr(deliveries, "delivery_plans", lambda config, job:
                        [plan("rekordbox", "rb", paths=[item.local_path])])

    report = receipts.build(config, job, plans=[plan("rekordbox", "rb", paths=[item.local_path])])

    assert report["complete"] is False
    assert set(report["records"][0]["deliveries"]) == {"rekordbox", "traktor"}
    assert report["delivery_pending"]["traktor"] == [1]


def test_report_explains_review_genre_and_acquisition_provenance(tmp_path, monkeypatch):
    acquired = record(1, tmp_path / "first.flac", genre="")
    review = record(2, tmp_path / "second.flac", decision=state.UNDECIDED, status=state.PENDING,
                    candidates=[])
    held = record(3, tmp_path / "third.flac", status=state.FOUND_IN_LIBRARY)
    job = FakeJob(tmp_path / "job", [acquired, review, held], [
        {"event": "downloaded", "idx": 1}, {"event": "already_held", "idx": 3},
    ])
    config = replace(DEFAULTS, state_root=tmp_path, export_targets=())
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [])
    monkeypatch.setattr(deliveries, "delivery_plans", lambda config, job: [])

    report = receipts.build(config, job, plans=[])

    assert report["counts"]["requested"] == 3
    assert report["acquired"] == [1]
    assert report["already_held"] == [3]
    assert report["review"][0]["idx"] == 2
    assert report["ungenred"] == [1]
    assert report["outstanding"] == [2]
    assert report["complete"] is False


def test_missing_artifact_and_zero_targets_cannot_be_complete(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [record(1, None)])
    config = replace(DEFAULTS, state_root=tmp_path, export_targets=())
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [])
    monkeypatch.setattr(deliveries, "delivery_plans", lambda config, job: [])

    report = receipts.build(config, job, plans=[])

    assert report["complete"] is False
    assert report["outstanding"] == [1]


def test_write_atomically_persists_stage_and_blocker(tmp_path, monkeypatch):
    item = record(1, tmp_path / "audio.flac")
    job = FakeJob(tmp_path / "job", [item])
    config = replace(DEFAULTS, state_root=tmp_path, export_targets=())
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: {"job_id": job.id,
                                                                                "complete": False})

    report = receipts.write(config, job, stage="acquire", blocker="waiting")

    saved = json.loads((job.root / "receipt.json").read_text())
    assert saved == report
    assert report["stage"] == "acquire"
    assert report["blocker"] == "waiting"


def test_build_status_clears_recovered_collection_blocker(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [], [
        {"event": "operation_finished", "complete": False, "stage": "blocked",
         "blocker": "traktor: collection unavailable", "stop_reason": "collection_blocked"},
        {"event": "delivery_finished", "outcomes": [{"target": "traktor", "success": True}]},
    ])
    config = receipt_config(tmp_path)
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: {"complete": True})

    status = receipts.build_status(config, job)

    assert status["stage"] == "complete"
    assert status["blocker"] == ""
    assert status["stop_reason"] == "complete"


def test_build_status_preserves_acquisition_failure_after_delivery(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [], [
        {"event": "operation_finished", "complete": False, "stage": "blocked",
         "blocker": "network stopped", "stop_reason": "displaced"},
        {"event": "delivery_finished", "outcomes": [{"target": "traktor", "success": True}]},
    ])
    config = receipt_config(tmp_path)
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: {"complete": False})

    status = receipts.build_status(config, job)

    assert status["stage"] == "blocked"
    assert status["blocker"] == "network stopped"
    assert status["stop_reason"] == "displaced"


def test_build_status_clears_collection_blocker_when_other_tracks_remain(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [], [
        {"event": "operation_finished", "complete": False, "stage": "blocked",
         "blocker": "traktor: collection unavailable", "stop_reason": "collection_blocked"},
        {"event": "delivery_finished", "outcomes": [{"target": "traktor", "success": True}]},
    ])
    report = {"complete": False, "records": [
        {"idx": 1, "status": state.TAGGED, "local_path": "/audio/ready.flac",
         "deliveries": {"traktor": {"status": "success"}}},
        {"idx": 2, "status": state.PENDING, "local_path": "",
         "deliveries": {"traktor": {"status": "failed"}}},
    ]}
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: report)

    status = receipts.build_status(receipt_config(tmp_path), job)

    assert status["stage"] == "pending"
    assert status["blocker"] == ""
    assert status["stop_reason"] == "pending"


def test_build_status_keeps_current_delivery_failure_after_prior_success(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [], [
        {"event": "operation_finished", "complete": False, "stage": "blocked",
         "blocker": "traktor: collection unavailable", "stop_reason": "collection_blocked"},
        {"event": "delivery_finished", "outcomes": [{"target": "traktor", "success": True}]},
    ])
    report = {"complete": False, "records": [
        {"idx": 1, "status": state.TAGGED, "local_path": "/audio/ready.flac",
         "deliveries": {"traktor": {"status": "failed"}}},
    ]}
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: report)

    status = receipts.build_status(receipt_config(tmp_path), job)

    assert status["stage"] == "blocked"
    assert status["blocker"] == "traktor: collection unavailable"
    assert status["stop_reason"] == "collection_blocked"


def test_a_resumed_pass_does_not_carry_the_previous_runs_blocker(tmp_path, monkeypatch):
    job = FakeJob(tmp_path / "job", [record(1, tmp_path / "audio.flac")], [
        {"event": "operation_finished", "stage": "blocked", "complete": False,
         "blocker": "network stopped", "stop_reason": "transient_disconnected"},
        {"event": "operation_started"},
    ])
    monkeypatch.setattr(receipts, "build", lambda config, job, plans=None: {"complete": False, "records": []})

    status = receipts.build_status(receipt_config(tmp_path), job)

    assert status.get("stage") != "blocked"
    assert not status.get("blocker"), "the closed run's blocker is not the running pass's"
