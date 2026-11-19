from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from . import deliveries, spotify_source, state, store
from .config import Config


def build(config: Config, job: state.Job, plans=None) -> dict:
    """Build a machine-readable report from job state and current target receipts."""
    records = job.records()
    events = job.events()
    targets = deliveries.exporting.configured(config)
    target_names = [target.name for target in targets]
    statuses = deliveries.delivery_receipts(config, job, plans)
    source, memberships = _source(job)
    acquired = _acquired_memberships(config, job, records, events)
    already_held = _event_indices(events, "already_held")
    review = _review(job)
    ungenred = [record.idx for record in records if _request_fulfilled(record) and not record.genre.strip()]
    report_records = [_record_report(record, statuses.get(record.idx, {}), memberships) for record in records]
    delivery_pending = {target: [record.idx for record in records
                                 if statuses.get(record.idx, {}).get(target, {}).get("status") != "success"]
                        for target in target_names}
    delivery_pending = {target: indices for target, indices in delivery_pending.items() if indices}
    outstanding = [record.idx for record in records if not _request_fulfilled(record)]
    delivered = {record.idx for record in records
                 if target_names and all(statuses.get(record.idx, {}).get(target, {}).get("status") == "success"
                                         for target in target_names)}
    complete = bool(records) and not outstanding and len(delivered) == len(records) and bool(target_names)
    counts = Counter(record.status for record in records)
    counts.update(requested=len(records), acquired=len(acquired), already_held=len(already_held),
                  review=len(review), ungenred=len(ungenred), delivered=len(delivered))
    return {"job_id": job.id, "source": source, "counts": dict(counts), "records": report_records,
            "acquired": acquired, "already_held": already_held, "review": review,
            "ungenred": ungenred, "outstanding": outstanding,
            "delivery_pending": delivery_pending, "complete": complete}


def build_status(config: Config, job: state.Job) -> dict:
    report = build(config, job)
    outcome = _last_operation(job)
    if outcome is None:
        return report

    blocker = outcome.get("blocker", "")
    blocker = blocker if isinstance(blocker, str) else ""
    stage = outcome.get("stage")
    if not isinstance(stage, str):
        stage = "blocked" if blocker else "complete" if outcome.get("complete") else "pending"
    stop_reason = outcome.get("stop_reason", "")
    if not isinstance(stop_reason, str) or not stop_reason:
        stop_reason = "complete" if outcome.get("complete") else "pending"
    if not report["complete"]:
        if stage == "complete":
            stage = "pending"
        if stop_reason == "complete":
            stop_reason = "pending"

    if (stop_reason == "collection_blocked" and _successful_delivery_after_operation(job)
            and _current_deliveries_succeeded(report)):
        blocker = ""
        stage = "complete" if report["complete"] else "pending"
        stop_reason = "complete" if report["complete"] else "pending"
    report.update(stage=stage, blocker=blocker, stop_reason=stop_reason)
    return report


def _last_operation(job: state.Job) -> dict | None:
    for event in reversed(job.events()):
        if event.get("event") == "operation_started":
            return None     # a pass is running: the run it started over is not what its status says
        if event.get("event") == "operation_finished":
            return event

    try:
        receipt = json.loads((job.root / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return receipt if isinstance(receipt, dict) else None

def _successful_delivery_after_operation(job: state.Job) -> bool:
    for event in reversed(job.events()):
        if event.get("event") == "operation_finished":
            return False
        if event.get("event") == "delivery_finished":
            outcomes = event.get("outcomes")
            return isinstance(outcomes, list) and bool(outcomes) and all(
                isinstance(outcome, dict) and outcome.get("success") is True for outcome in outcomes)
    return False


def _current_deliveries_succeeded(report: dict) -> bool:
    if report["complete"]:
        return True
    retained = [record for record in report.get("records", [])
                if record.get("local_path")
                and record.get("status") in {state.TAGGED, state.IMPORTED, state.FOUND_IN_LIBRARY}]
    return bool(retained) and all(
        record.get("deliveries") and all(outcome.get("status") == "success"
                                        for outcome in record["deliveries"].values())
        for record in retained)


def _request_fulfilled(record: state.Record) -> bool:
    return record.status in {state.TAGGED, state.IMPORTED, state.FOUND_IN_LIBRARY} and bool(
        record.local_path and Path(record.local_path).is_file())


def _acquired_memberships(config: Config, job: state.Job, records: list[state.Record],
                         events: list[dict]) -> list[int]:
    indices = set(_event_indices(events, "downloaded"))
    if not indices:
        return []
    work_ids = store.work_ids(config.state_root, job.id)
    acquired_work = {work_ids[idx] for idx in indices if idx in work_ids}
    return sorted(record.idx for record in records
                  if record.idx in indices or work_ids.get(record.idx) in acquired_work)


def _event_indices(events: list[dict], event: str) -> list[int]:
    return sorted({entry["idx"] for entry in events
                   if entry.get("event") == event and isinstance(entry.get("idx"), int)})


def _review(job: state.Job) -> list[dict]:
    queue = state.queue(job)
    reasons = (("needs_you", queue.needs_you), ("not_searched", queue.not_searched),
               ("nothing_offered", queue.nothing_offered), ("refused", queue.refused))
    return [{"idx": record.idx, "label": record.label, "reason": reason, "notes": record.notes}
            for reason, entries in reasons for record in entries]


def _source(job: state.Job) -> tuple[dict | None, dict[int, dict]]:
    snapshot = spotify_source.snapshot(job)
    if snapshot is None:
        return None, {}
    entries = {entry["idx"]: {"uri": entry["uri"], "position": entry["position"]}
               for entry in snapshot.get("entries", []) if "idx" in entry}
    return ({"id": snapshot.get("id"), "name": snapshot.get("name"),
             "uri": snapshot.get("uri"), "tracks": len(snapshot.get("tracks", [])),
             "memberships": len(entries)}, entries)


def _record_report(record: state.Record, deliveries_by_target: dict[str, dict],
                   memberships: dict[int, dict]) -> dict:
    report = {"idx": record.idx, "artist": record.artist, "title": record.title, "mix": record.mix,
              "label": record.label, "status": record.status, "decision": record.decision,
              "local_path": record.local_path, "genre": record.genre,
              "deliveries": deliveries_by_target,
              "delivered": bool(deliveries_by_target) and all(
                  result.get("status") == "success" for result in deliveries_by_target.values())}
    report.update(memberships.get(record.idx, {}))
    return report


def write(config: Config, job: state.Job, *, stage: str = "complete", blocker: str = "",
          stop_reason: str = "") -> dict:
    """Atomically persist a receipt and return the saved report."""
    report = build(config, job)
    if stage == "complete" and not report["complete"]:
        stage = "pending"
    report.update(stage=stage, blocker=blocker,
                  stop_reason=stop_reason or ("complete" if report["complete"] else "pending"))
    path = job.root / "receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return report
