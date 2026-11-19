from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from music_agent import agent_api, config
from music_agent.deliveries import DeliveryOutcome, DeliveryPlan



class FakeJob:
    def __init__(self, root, job_id="job-1"):
        self.id = job_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.started = threading.Event()
        self.finished = threading.Event()
        self.release = threading.Event()
        self._events = []

    def append_event(self, event, **fields):
        self._events.append({"event": event, **fields})

    def events(self):
        return self._events


def test_constructor_uses_explicit_config_path(tmp_path):
    config_path = tmp_path / "custom.toml"
    config_path.write_text("""[paths]
state = ".state/jobs"
""", encoding="utf-8")

    api = agent_api.AgentAPI(config_path)

    assert api.config.state_root == (tmp_path / ".state/jobs").resolve()
    api.close()


def test_run_playlist_returns_durable_job_id_before_one_background_pass(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    run_calls = []
    admission_started = threading.Event()
    release_admission = threading.Event()
    monkeypatch.setattr(agent_api.state, "create", lambda *args, **kwargs: job)

    def admit(config_value, source, *, job_id=None):
        assert job_id == job.id
        admission_started.set()
        release_admission.wait(2)
        return job

    monkeypatch.setattr(agent_api.spotify_source, "admit", admit)

    def run(config_value, selected_job, **kwargs):
        run_calls.append((selected_job, kwargs))
        selected_job.started.set()
        try:
            selected_job.release.wait(2)
            return {"complete": True, "stage": "complete"}
        finally:
            selected_job.finished.set()

    monkeypatch.setattr(agent_api.spotify_workflow, "run", run)
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path / "state")
    try:
        before = time.monotonic()
        result = api.run_playlist("spotify:playlist:abc")
        elapsed = time.monotonic() - before

        assert result["job_id"] == job.id
        assert elapsed < 1
        assert admission_started.wait(1)
        assert run_calls == []
        release_admission.set()
        assert job.started.wait(1)
        assert len(run_calls) == 1
        assert run_calls[0][0] is job
    finally:
        release_admission.set()
        job.release.set()
        job.finished.wait(1)

def test_run_playlist_rejects_non_spotify_source_without_admitting(monkeypatch):
    admitted = []
    monkeypatch.setattr(agent_api.spotify_source, "admit", lambda *args, **kwargs: admitted.append(args))
    api = agent_api.AgentAPI()

    with pytest.raises(ValueError, match="Spotify playlist"):
        api.run_playlist("https://example.invalid/playlist")

    assert admitted == []
    api.close()


def test_plain_playlist_names_can_contain_colons():
    assert agent_api.AgentAPI._validate_source("House: Classics") == "House: Classics"


def test_second_active_pass_is_rejected_before_admission(monkeypatch, tmp_path):
    first_job = FakeJob(root=tmp_path / "first")
    jobs = {first_job.id: first_job}
    created = []
    monkeypatch.setattr(agent_api.state, "create",
                        lambda *args, **kwargs: created.append(kwargs) or first_job)

    def admit(config_value, source, *, job_id=None):
        return jobs[job_id]

    monkeypatch.setattr(agent_api.spotify_source, "admit", admit)

    def run(config_value, job, **kwargs):
        job.started.set()
        try:
            job.release.wait(2)
            return {}
        finally:
            job.finished.set()

    monkeypatch.setattr(agent_api.spotify_workflow, "run", run)
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path / "state")
    try:
        result = api.run_playlist("spotify:playlist:first")
        assert first_job.started.wait(1)

        with pytest.raises(RuntimeError, match="active"):
            api.run_playlist("spotify:playlist:second")

        assert result["job_id"] == first_job.id
        assert len(created) == 1
    finally:
        first_job.release.set()
        first_job.finished.wait(1)


def test_pass_status_rejects_unknown_and_traversal_job_ids(monkeypatch):
    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config_value: [])
    api = agent_api.AgentAPI()

    for job_id in ("missing", "../outside", ""):
        with pytest.raises(ValueError):
            api.pass_status(job_id)

    api.close()


def test_pass_status_labels_acquired_and_blank_genre_records(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    report = {
        "job_id": job.id,
        "complete": True,
        "stage": "complete",
        "counts": {"acquired": 1, "ungenred": 1},
        "records": [{"idx": 4, "label": "Artist - Title", "deliveries": {}}],
        "acquired": [4],
        "ungenred": [4],
        "review": [],
    }
    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config_value: [job])
    monkeypatch.setattr(agent_api.receipts, "build_status", lambda config_value, selected_job: report)
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path)

    status = api.pass_status(job.id)

    assert status["acquired"]["records"] == [{"idx": 4, "label": "Artist - Title"}]
    assert status["blank_genres"]["records"] == [{"idx": 4, "label": "Artist - Title"}]
    assert "candidates" not in status
    api.close()


def test_partial_pass_finishes_without_claiming_every_request_fulfilled(tmp_path, monkeypatch):
    job = FakeJob(root=tmp_path / "job")
    job.append_event("operation_finished", stage="pending", complete=False, stop_reason="no_acquisitions")
    report = {"complete": False, "stage": "pending", "stop_reason": "no_acquisitions"}
    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config: [job])
    monkeypatch.setattr(agent_api.receipts, "build_status", lambda config, job: report)
    api = agent_api.AgentAPI()

    status = api.pass_status(job.id)

    assert status["terminal"] is True
    assert status["fulfilled"] is False
    assert status["status"] == "finished"
    api.close()
def test_delivery_requires_preview_and_persists_updated_receipt(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    cfg = replace(config.DEFAULTS, state_root=tmp_path)
    job.append_event("operation_finished", stage="blocked", stop_reason="collection_blocked",
                     blocker="rekordbox unavailable", complete=False)
    plan = DeliveryPlan("rekordbox", "collection", "add 1 track", False)
    outcomes = [DeliveryOutcome("rekordbox", True, "delivered", [])]

    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config_value: [job])
    monkeypatch.setattr(agent_api.receipts, "build",
                        lambda config_value, selected_job: {"complete": True})
    monkeypatch.setattr(agent_api.deliveries, "delivery_plans", lambda config_value, selected_job: [plan])
    monkeypatch.setattr(agent_api.deliveries, "deliver",
                        lambda config_value, selected_job, approved, **kwargs: outcomes)
    api = agent_api.AgentAPI()
    api.config = cfg

    with pytest.raises(ValueError, match="Preview destination changes"):
        api.deliver_pass(job.id, apply=True)

    preview = api.deliver_pass(job.id)
    assert preview["preview"] is True
    applied = api.deliver_pass(job.id, apply=True)

    assert applied["outcomes"] == [
        {"target": "rekordbox", "success": True, "message": "delivered", "artifacts": []}]
    receipt = json.loads((job.root / "receipt.json").read_text())
    assert receipt["stage"] == "complete"
    assert receipt["blocker"] == ""
    api.close()


def test_stale_delivery_preview_does_not_write_or_update_receipts(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    cfg = replace(config.DEFAULTS, state_root=tmp_path)
    plans = [DeliveryPlan("rekordbox", "collection", "add 1 track", False)]
    writes = []
    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config_value: [job])
    monkeypatch.setattr(agent_api.deliveries, "delivery_plans",
                        lambda config_value, selected_job: plans.copy())
    monkeypatch.setattr(agent_api.deliveries, "deliver",
                        lambda *args, **kwargs: pytest.fail("stale approved plans must not be written"))
    monkeypatch.setattr(agent_api.receipts, "write", lambda *args, **kwargs: writes.append(args))
    api = agent_api.AgentAPI()
    api.config = cfg
    api.deliver_pass(job.id)
    plans.append(DeliveryPlan("rekordbox", "collection", "add 2 tracks", False))

    with pytest.raises(ValueError, match="preview"):
        api.deliver_pass(job.id, apply=True)

    assert writes == []
    api.close()


def test_cleanup_is_preview_only_by_default_and_reports_counts(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    cfg = replace(config.DEFAULTS, state_root=tmp_path)
    calls = []
    raw = {"removed": 3, "remaining": ["spotify:track:kept"], "eligible_uris": ["spotify:track:old"]}

    monkeypatch.setattr(agent_api.state, "all_jobs", lambda config_value: [job])
    monkeypatch.setattr(agent_api.spotify_reconcile, "reconcile",
                        lambda config_value, selected_job, **kwargs: calls.append(kwargs) or raw)
    api = agent_api.AgentAPI()
    api.config = cfg

    preview = api.cleanup_playlist(job.id)
    assert calls[-1] == {"scope": "acquired", "apply": False}
    assert preview["removed"] == 0
    assert job.events() == []
    assert "remaining" not in preview
    assert "eligible_uris" not in preview

    applied = api.cleanup_playlist(job.id, scope="acquired", apply=True)
    assert calls[-1] == {"scope": "acquired", "apply": True}
    assert applied["removed"] == 3
    assert job.events()[-1]["event"] == "spotify_cleanup_finished"
    api.close()

def test_background_failure_is_persisted_as_terminal_status(monkeypatch, tmp_path):
    job = FakeJob(root=tmp_path / "job-1")
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path / "state")
    monkeypatch.setattr(agent_api.spotify_source, "admit",
                        lambda config_value, source, *, job_id=None: job)
    monkeypatch.setattr(agent_api.spotify_workflow, "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pipeline failed")))

    api._run_pass(job, "spotify:playlist:test")

    assert job.events()[-1]["event"] == "operation_finished"
    assert job.events()[-1]["stage"] == "failed"
    assert agent_api.json.loads((job.root / "progress.json").read_text())["stage"] == "failed"
    api.close()
