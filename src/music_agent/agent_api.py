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
        self._closed = False

    def run_playlist(self, source: str) -> dict:
        source = self._validate_source(source)
        with self._guard:
            if self._closed:
                raise RuntimeError("AgentAPI is closed.")
            if self._active:
                raise RuntimeError("A playlist job is already active.")
            with operations.lock(self.config.state_root):
                job = spotify_source.admit(self.config, source)
            self._active.add(job.id)
            thread = threading.Thread(target=self._run_pass, args=(job,), daemon=True)
            try:
                thread.start()
            except BaseException:
                self._active.discard(job.id)
                raise
            return {"job_id": job.id, "status": "started"}

    def pass_status(self, job_id: str) -> dict:
        job = operations.find_job(self.config, job_id)
        report = receipts.build_status(self.config, job)
        progress = self._progress(job)
        stage = report.get("stage") or progress.get("stage") or "pending"
        fulfilled = bool(report.get("complete"))
        with self._guard:
            active = job.id in self._active
        terminal = not active and self._terminal(job, fulfilled)
        status = "running" if active or progress else "pending"
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
        job = operations.find_job(self.config, job_id)
        if not apply:
            plans = deliveries.delivery_plans(self.config, job)
            self._plans[job.id] = plans
            return {"job_id": job.id, "preview": True, "targets": self._plan_reports(plans)}
        with operations.lock(self.config.state_root), store.writer_lock(self.config.state_root):
            approved = self._plans.pop(job.id, None)
            if approved is None:
                raise ValueError("Preview destination changes before delivering.")
            outcomes = deliveries.deliver(self.config, job, approved, raise_stale=True)
            reports = operations.record_delivery(self.config, job, outcomes, plans=approved)
            return {"job_id": job.id, "preview": False, "outcomes": reports}

    def cleanup_playlist(self, job_id: str, scope: str = "acquired", apply: bool = False) -> dict:
        if scope not in {"acquired", "delivered"}:
            raise ValueError("Cleanup scope must be 'acquired' or 'delivered'.")
        job = operations.find_job(self.config, job_id)
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

    def _run_pass(self, job: state.Job) -> None:
        try:
            spotify_workflow.run(self.config, job)
        except Exception as error:
            message = str(error) or type(error).__name__
            try:
                job.append_event("operation_finished", complete=False, stage="failed",
                                 blocker=message, stop_reason="failed")
                operations.write_json(job.root / "progress.json",
                                      {"job_id": job.id, "stage": "failed", "message": message[:512]})
            except Exception:
                logging.exception("Could not persist failure for job %s", job.id)
        finally:
            with self._guard:
                self._active.discard(job.id)

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
