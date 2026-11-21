from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from music_agent import agent_api, config, spotify_account, spotify_source, state
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


def test_run_playlist_reuses_an_admitted_job_and_returns_before_processing(monkeypatch, tmp_path):
    playlist_id = "ABCDEFGHIJKLMNOPQRSTUV"
    source = f"spotify:playlist:{playlist_id}"
    playlist = spotify_account.AccountPlaylist(playlist_id, "Set", source, (
        {"type": "track", "name": "Pacific", "artists": ["747"], "uri": f"spotify:track:{playlist_id}"},
    ))
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: playlist)
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path / "state")
    job = spotify_source.admit(api.config, source)
    job.append_event("operation_finished", complete=False, stage="pending")
    monkeypatch.setattr(agent_api.receipts, "build_status", lambda config, job: {"complete": False})
    started, release, finished = (threading.Event() for _ in range(3))
    run_calls = []

    def run(config_value, selected_job):
        run_calls.append(selected_job.id)
        started.set()
        try:
            release.wait(2)
        finally:
            finished.set()

    monkeypatch.setattr(agent_api.spotify_workflow, "run", run)
    try:
        result = api.run_playlist(source)

        assert result["job_id"] == job.id
        assert [item.id for item in state.all_jobs(api.config)] == [job.id]
        assert started.wait(1)
        assert not finished.is_set()
        assert api.pass_status(job.id)["terminal"] is False
        assert run_calls == [job.id]
    finally:
        release.set()
        finished.wait(1)
        api.close()


def test_failed_admission_does_not_create_a_job(monkeypatch, tmp_path):
    def fail(source):
        raise ValueError("Spotify playlist unavailable")

    monkeypatch.setattr(spotify_source, "resolve", fail)
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path)

    with pytest.raises(ValueError, match="playlist unavailable"):
        api.run_playlist("Set")

    assert state.newest(api.config) is None
    api.close()


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
    admitted = []
    monkeypatch.setattr(spotify_source, "admit",
                        lambda config_value, source: admitted.append(source) or first_job)

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
        assert admitted == ["spotify:playlist:first"]
    finally:
        first_job.release.set()
        first_job.finished.wait(1)


def test_pass_status_rejects_unknown_and_traversal_job_ids(tmp_path):
    api = agent_api.AgentAPI()
    api.config = replace(config.DEFAULTS, state_root=tmp_path / "state")
    api.config.state_root.mkdir()
    (api.config.state_root / "outside").mkdir()

    for job_id in ("missing", "../outside", "", "outside/.."):
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
    monkeypatch.setattr(state, "find", lambda config, job_id: job if job_id == job.id else None)
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
    monkeypatch.setattr(state, "find", lambda config, job_id: job if job_id == job.id else None)
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

    monkeypatch.setattr(state, "find", lambda config, job_id: job if job_id == job.id else None)
    monkeypatch.setattr(agent_api.receipts, "build",
                        lambda config_value, selected_job, *, plans=None: {"complete": True})
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
    cfg = replace(config.DEFAULTS, state_root=tmp_path)
    job = state.create(cfg, "Delivery")
    plans = [DeliveryPlan("rekordbox", "collection", "add 1 track", False)]
    writes = []
    monkeypatch.setattr(agent_api.deliveries, "_plans", lambda *args: plans.copy())
    monkeypatch.setattr(agent_api.deliveries, "_deliver_target",
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

    monkeypatch.setattr(state, "find", lambda config, job_id: job if job_id == job.id else None)
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
    monkeypatch.setattr(agent_api.spotify_workflow, "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pipeline failed")))

    api._run_pass(job)

    assert job.events()[-1]["event"] == "operation_finished"
    assert job.events()[-1]["stage"] == "failed"
    assert agent_api.json.loads((job.root / "progress.json").read_text())["stage"] == "failed"
    api.close()
