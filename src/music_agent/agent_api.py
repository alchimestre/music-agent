from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import (
    config, deliveries, operations, receipts, spotify_reconcile, spotify_source,
    spotify_workflow, state, store,
)
from .config import Config

_STATUS_LIMIT = 20


class AgentAPI:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config: Config = config.load(config_path)
        self._plans: dict[str, list] = {}
        self._active: set[str] = set()
        self._guard = threading.Lock()
        self._launch_guard = threading.Lock()
        self._closed = False

    def run_playlist(self, source: str) -> dict:
        source = self._validate_source(source)
        with self._launch_guard, self._guard:
            if self._closed:
                raise RuntimeError("AgentAPI is closed.")
            if self._active:
                raise RuntimeError("A playlist pass is already active.")
            job = state.create(self.config, "Spotify playlist", records=[])
            self._active.add(job.id)
        thread = threading.Thread(target=self._run_pass, args=(job, source), daemon=True)
        try:
            thread.start()
        except BaseException:
            with self._guard:
                self._active.discard(job.id)
            raise
        return {"job_id": job.id, "status": "started"}

    def pass_status(self, job_id: str) -> dict:
        job = self._job(job_id)
        report = receipts.build_status(self.config, job)
        progress = self._progress(job)
        stage = report.get("stage") or progress.get("stage") or "pending"
        fulfilled = bool(report.get("complete"))
        terminal = self._terminal(job, fulfilled)
        status = "running" if progress else "pending"
        if terminal:
            status = "fulfilled" if fulfilled else "failed" if report.get("blocker") else "finished"
        counts = report.get("counts", {})
        records = report.get("records", [])
        records_by_idx = {record["idx"]: record.get("label", "")
                          for record in records if isinstance(record.get("idx"), int)}
        acquired = report.get("acquired", [])
        blank_genres = report.get("ungenred", [])
        review = report.get("review", [])
        return {
            "job_id": job.id,
            "status": status,
            "terminal": terminal,
            "fulfilled": fulfilled,
            "stage": stage,
            "blocker": report.get("blocker", ""),
            "stop_reason": report.get("stop_reason", ""),
            "progress": {key: progress[key] for key in ("message", "updated_at", "idx", "fraction")
                         if key in progress},
            "counts": counts,
            "acquired": {"count": counts.get("acquired", len(acquired)),
                         "records": self._record_references(acquired, records_by_idx)},
            "review": {"count": len(review), "reason_counts": self._reason_counts(review),
                       "records": review[:_STATUS_LIMIT]},
            "blank_genres": {"count": counts.get("ungenred", len(blank_genres)),
                             "records": self._record_references(blank_genres, records_by_idx)},
            "targets": self._target_outcomes(records),
        }

    def deliver_pass(self, job_id: str, apply: bool = False) -> dict:
        job = self._job(job_id)
        if not apply:
            plans = deliveries.delivery_plans(self.config, job)
            self._plans[job.id] = plans
            return {"job_id": job.id, "preview": True, "targets": self._plan_reports(plans)}
        approved = self._plans.get(job.id)
        if approved is None:
            raise ValueError("Preview destination changes before delivering.")
        with operations.lock(self.config.state_root), store.writer_lock(self.config.state_root):
            current = deliveries.delivery_plans(self.config, job)
            if current != approved:
                self._plans.pop(job.id, None)
                raise ValueError("Delivery preview is stale; request a new preview.")
            outcomes = deliveries.deliver(self.config, job, approved, emit=lambda stage, message: None)
            self._plans.pop(job.id, None)
            result = {"job_id": job.id, "preview": False,
                      "outcomes": [self._outcome_report(outcome) for outcome in outcomes]}
            self._write_operation_outcome(job, result)
            previous = receipts.build_status(self.config, job)
            receipts.write(self.config, job, stage=previous.get("stage", "pending"),
                           blocker=previous.get("blocker", ""),
                           stop_reason=previous.get("stop_reason", ""))
            return result

    def cleanup_playlist(self, job_id: str, scope: str = "acquired", apply: bool = False) -> dict:
        if scope not in {"acquired", "delivered"}:
            raise ValueError("Cleanup scope must be 'acquired' or 'delivered'.")
        job = self._job(job_id)
        report = spotify_reconcile.reconcile(self.config, job, scope=scope, apply=apply)
        result = {"job_id": job.id, "scope": scope, "apply": apply,
                  "removed": report.get("removed", 0) if apply else 0,
                  "remaining_count": len(report.get("remaining", [])),
                  "eligible_count": len(report.get("eligible_uris", [])),
                  "blocker": report.get("blocker"), "reason": report.get("reason")}
        if apply:
            job.append_event("spotify_cleanup_finished", scope=scope, apply=True,
                             removed=result["removed"], remaining_count=result["remaining_count"],
                             blocker=result["blocker"] or "")
        return result

    def close(self) -> None:
        with self._guard:
            self._closed = True

    def _run_pass(self, job: state.Job, source: str) -> None:
        try:
            (job.root / "progress.json").write_text(json.dumps(
                {"job_id": job.id, "stage": "resolve", "message": "Resolving Spotify playlist."}) + "\n",
                encoding="utf-8")
            with operations.lock(self.config.state_root):
                job = spotify_source.admit(self.config, source, job_id=job.id)
                spotify_workflow.run(self.config, job, emit=lambda event: None)
        except Exception as error:
            message = str(error) or type(error).__name__
            try:
                job.append_event("operation_finished", complete=False, stage="failed",
                                 blocker=message, stop_reason="failed")
                (job.root / "progress.json").write_text(json.dumps(
                    {"job_id": job.id, "stage": "failed", "message": message[:512]}) + "\n",
                    encoding="utf-8")
            except Exception:
                logging.exception("Could not persist failed pass status for job %s", job.id)
        finally:
            with self._guard:
                self._active.discard(job.id)
    def _job(self, job_id: str) -> state.Job:
        if not job_id or Path(job_id).name != job_id or job_id in {".", ".."}:
            raise ValueError("Invalid submission identifier.")
        job = next((entry for entry in state.all_jobs(self.config) if entry.id == job_id), None)
        if job is None:
            raise ValueError(f"No submission named {job_id}.")
        return job

    @staticmethod
    def _validate_source(source: str) -> str:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("A Spotify playlist name, URI, or URL is required.")
        source = source.strip()
        if source.lower().startswith("spotify:") or "://" not in source:
            return source
        parts = urlsplit(source)
        if parts.scheme.lower() not in {"http", "https"} or not (
                parts.hostname and (parts.hostname == "spotify.com" or parts.hostname.endswith(".spotify.com"))):
            raise ValueError("Source must be a Spotify playlist name, URI, or URL.")
        return source

    @staticmethod
    def _progress(job: state.Job) -> dict:
        try:
            value = json.loads((job.root / "progress.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _terminal(job: state.Job, fulfilled: bool) -> bool:
        for event in reversed(job.events()):
            if event.get("event") in {"operation_started", "operation_finished"}:
                return event["event"] == "operation_finished"
        return fulfilled

    @staticmethod
    def _reason_counts(review: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in review:
            reason = record.get("reason", "unknown")
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    @staticmethod
    def _target_outcomes(records: list[dict]) -> dict[str, dict[str, int]]:
        targets: dict[str, dict[str, int]] = {}
        for record in records:
            for name, outcome in record.get("deliveries", {}).items():
                status = outcome.get("status", "unknown")
                counts = targets.setdefault(name, {})
                counts[status] = counts.get(status, 0) + 1
        return targets

    @staticmethod
    def _record_references(indices: list[int], labels_by_idx: dict[int, str]) -> list[dict]:
        return [{"idx": idx, "label": labels_by_idx[idx]}
                for idx in indices if idx in labels_by_idx][:_STATUS_LIMIT]

    @staticmethod
    def _plan_reports(plans: list) -> list[dict]:
        return [{"target": plan.target, "destination": plan.destination, "changes": plan.changes,
                 "requires_confirmation": plan.requires_confirmation, "error": plan.error}
                for plan in plans]

    @staticmethod
    def _outcome_report(outcome) -> dict:
        return {"target": outcome.target, "success": outcome.success, "message": outcome.message,
                "artifacts": [{"idx": item.idx, "status": item.status, "message": item.message}
                              for item in outcome.artifacts]}

    @staticmethod
    def _write_operation_outcome(job: state.Job, result: dict) -> None:
        job.append_event("delivery_finished", outcomes=result["outcomes"])
