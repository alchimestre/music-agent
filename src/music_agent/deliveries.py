"""Review-then-write delivery to every configured collection, with a durable outcome per file.

`delivery_plans` reads the collections and writes nothing. `deliver` refuses to run unless the plans it
recomputes match the ones the user approved, so a collection opened or changed after the preview cannot be
written without another preview.
"""

from __future__ import annotations

import json
import ntpath
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import exporting, importing, library, state, store, traktor
from .config import Config


@dataclass(frozen=True)
class DeliveryPlan:
    target: str
    destination: str
    changes: str
    requires_confirmation: bool
    error: str = ""
    fingerprint: str = field(default="", repr=False)
    target_identity: str = field(default="", repr=False)
    artifact_identity: tuple[str, ...] = field(default=(), repr=False)
    artifact_errors: tuple[tuple[int, str], ...] = field(default=(), repr=False)
    blocked: bool = field(default=False, repr=False)


@dataclass(frozen=True)
class ArtifactOutcome:
    idx: int
    status: str
    message: str


@dataclass(frozen=True)
class DeliveryOutcome:
    target: str
    success: bool
    message: str
    artifacts: list[ArtifactOutcome] = field(default_factory=list)


def _normal_path(value: str | Path) -> str:
    text = os.fspath(value)
    if not text:
        return ""
    drive, _ = ntpath.splitdrive(text)
    if drive or text.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(text))
    return os.path.normcase(os.path.abspath(text))


def _content_stamp(record: state.Record) -> tuple[int | None, int | None]:
    try:
        details = Path(record.local_path).stat()
    except OSError:
        return None, None
    return details.st_size, details.st_mtime_ns


def _file_identity(record: state.Record) -> str:
    size, modified = _content_stamp(record)
    return _key(record.idx, _normal_path(record.local_path), size, modified)


def _collection_key(base: str, record: state.Record, work_id: str) -> str:
    """The durable key for one file in one collection.

    Replacing or reprocessing the audio changes the size or the modification time, so the new file gets its
    own key and cannot inherit the old file's success.
    """
    size, modified = _content_stamp(record)
    return _key(base, work_id, _normal_path(record.local_path), size, modified)


def _work_ids(config: Config, job: state.Job, records: list[state.Record]) -> dict[int, str]:
    try:
        mapping = store.work_ids(config.state_root, job.id)
    except Exception:
        mapping = {}
    if mapping:
        return {record.idx: mapping.get(record.idx, "") for record in records}
    work_ids = {}
    for record in records:
        try:
            work_ids[record.idx] = job.work_id(record.idx)
        except Exception:
            work_ids[record.idx] = ""
    return work_ids


def _key(*parts) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _failure(target: str, records: list[state.Record], message: str) -> DeliveryOutcome:
    artifacts = [ArtifactOutcome(record.idx, "failed", message) for record in records]
    return DeliveryOutcome(target, False, message, artifacts)


def _unavailable(target: str, destination: str, message: str) -> DeliveryPlan:
    return DeliveryPlan(
        target=target,
        destination=destination,
        changes="",
        requires_confirmation=False,
        error=message,
        fingerprint=_key("blocked", message),
        target_identity=destination,
        blocked=True,
    )


def _rekordbox_plan(target, config: Config, job: state.Job) -> DeliveryPlan:
    if not target.available(config):
        return _unavailable(target.name, "rekordbox:unavailable", "target is not installed on this machine")
    try:
        planned = importing.plan(config, job)
    except (importing.NotClosed, importing.ImportFailed, library.Unreadable) as error:
        return _unavailable(target.name, "rekordbox:unavailable", str(error))
    except Exception as error:              # pyrekordbox raises its own types and its own messages
        return _unavailable(target.name, "rekordbox:unavailable",
                            f"cannot read the rekordbox collection: {error}")
    failures = dict(planned.failed)
    return DeliveryPlan(
        target=target.name,
        destination=planned.destination,
        changes=planned.report(),
        requires_confirmation=planned.requires_confirmation,
        error=_failure_details(failures, job),
        fingerprint=_key(planned.destination, planned.fingerprint),
        target_identity=planned.destination,
        artifact_errors=tuple(sorted(failures.items())),
    )


def _traktor_plan(target, config: Config, job: state.Job) -> DeliveryPlan:
    identity = traktor.destination(config)
    if not target.available(config):
        return _unavailable(target.name, identity, "target is not installed on this machine")
    try:
        planned = traktor.plan(config)
    except (traktor.Missing, traktor.Failed) as error:
        return _unavailable(target.name, identity, str(error))
    except Exception as error:
        return _unavailable(target.name, identity, f"cannot read the Traktor collection: {error}")
    if planned is None:
        return _unavailable(target.name, identity, "traktor-nml is not installed on this machine")
    return DeliveryPlan(
        target=target.name,
        destination="the Traktor collection selected by the configured traktor-nml command",
        changes=planned.changes,
        requires_confirmation=planned.requires_confirmation,
        fingerprint=_key(planned.destination, planned.changes),
        target_identity=planned.destination,
    )


def _failure_details(failures: dict[int, str], job: state.Job) -> str:
    labels = {record.idx: record.label for record in job.records()}
    lines = []
    for idx, message in sorted(failures.items()):
        label = labels.get(idx) or f"track {idx}"
        lines.append(f"[{idx}] {label}: {message}")
    return "\n".join(lines)


def _plan_target(target, config: Config, job: state.Job) -> DeliveryPlan:
    if target.name == "rekordbox":
        return _rekordbox_plan(target, config, job)
    if target.name == "traktor":
        return _traktor_plan(target, config, job)
    return _unavailable(target.name, target.name,
                        f"durable delivery is unsupported for target {target.name}")


def delivery_plans(config: Config, job: state.Job) -> list[DeliveryPlan]:
    """What each configured collection would change, without writing any of them."""
    identities = tuple(_file_identity(record) for record in importing.delivery_records(job))
    plans = []
    for target in exporting.configured(config):
        plans.append(replace(_plan_target(target, config, job), artifact_identity=identities))
    return plans


def _stale(current: list[DeliveryPlan], approved: list[DeliveryPlan],
           records: list[state.Record]) -> list[DeliveryOutcome]:
    message = "collection or input files changed; preview again before delivery"
    names = list(dict.fromkeys([plan.target for plan in current] + [plan.target for plan in approved]))
    return [_failure(name, records, message) for name in names]


def _save_status(config: Config, target: str, work_id: str, destination: str,
                 status: str, message: str = "") -> None:
    store.set_delivery(config.state_root, work_id, target, destination, status,
                       message if status == "failed" else "")


def _prepare(config: Config, target: str, planned: DeliveryPlan,
             record: state.Record, work_id: str) -> tuple[str, ArtifactOutcome | None]:
    """The durable key for one file, and the outcome known without writing (None when it needs the collection)."""
    base = planned.target_identity or planned.destination
    destination = _collection_key(base, record, work_id)
    if not work_id:
        return destination, ArtifactOutcome(record.idx, "failed", "record has no canonical work_id")
    if planned.blocked:
        message = planned.error or "target is unavailable"
        try:
            _save_status(config, target, work_id, destination, "failed", message)
        except Exception as error:
            message = f"{message}; could not save delivery status: {error}"
        return destination, ArtifactOutcome(record.idx, "failed", message)
    try:
        status = store.delivery_status(config.state_root, work_id, target, destination)
    except Exception as error:
        return destination, ArtifactOutcome(record.idx, "failed", f"could not read delivery status: {error}")
    if status == "success":
        return destination, ArtifactOutcome(record.idx, "success",
                                            "already delivered to this collection")
    if status not in {None, "pending", "failed"}:
        return destination, ArtifactOutcome(record.idx, "failed",
                                            f"unknown saved delivery status: {status}")
    try:
        _save_status(config, target, work_id, destination, "pending")
    except Exception as error:
        return destination, ArtifactOutcome(record.idx, "failed",
                                            f"could not record pending delivery: {error}")
    return destination, None


def _record_outcome(config: Config, target: str, destination: str, work_id: str,
                    outcome: ArtifactOutcome) -> ArtifactOutcome:
    try:
        _save_status(config, target, work_id, destination, outcome.status, outcome.message)
    except Exception as error:
        return ArtifactOutcome(outcome.idx, "failed",
                               f"{outcome.message}; could not save delivery status: {error}")
    return outcome


def _execute_rekordbox(config: Config, job: state.Job, planned: DeliveryPlan,
                       records: list[state.Record], emit) -> list[ArtifactOutcome]:
    errors = dict(planned.artifact_errors)
    if not planned.requires_confirmation:
        return [
            ArtifactOutcome(record.idx, "failed", errors[record.idx]) if record.idx in errors
            else ArtifactOutcome(record.idx, "success", "already in the collection")
            for record in records
        ]

    if importing.is_running():
        raise importing.NotClosed("rekordbox is running — close it before importing tracks")

    with importing.database(config) as db:
        summary = importing.run(
            config,
            job,
            db=db,
            backup_first=True,
            log=lambda message: emit("delivery", message),
            record_indices={record.idx for record in records if record.idx not in errors},
            stop_mcp=False,
        )
    updated = {record.idx: record for record in job.records()}
    delivered = set(summary.imported + summary.repointed + summary.already)
    outcomes = []
    for record in records:
        if record.idx in errors:
            outcomes.append(ArtifactOutcome(record.idx, "failed", errors[record.idx]))
        elif record.idx in delivered:
            outcomes.append(ArtifactOutcome(record.idx, "success", "added to rekordbox"))
        elif record.idx in summary.failed:
            note = getattr(updated.get(record.idx), "notes", "") or "rekordbox refused this file"
            outcomes.append(ArtifactOutcome(record.idx, "failed", note))
        else:
            outcomes.append(ArtifactOutcome(record.idx, "failed",
                                            "rekordbox did not report an outcome for this file"))
    return outcomes


def _execute_target(target, config: Config, job: state.Job, records: list[state.Record],
                    planned: DeliveryPlan, emit) -> list[ArtifactOutcome]:
    if target.name == "rekordbox":
        return _execute_rekordbox(config, job, planned, records, emit)
    if target.name == "traktor":
        if planned.requires_confirmation:
            traktor.apply(config)
            emit("delivery", "Traktor collection updated; open Traktor and run Check Consistency → Analyze")
            message = "written by traktor-nml"
        else:
            message = "already in sync"
        return [ArtifactOutcome(record.idx, "success", message) for record in records]
    message = f"durable delivery is unsupported for target {target.name}"
    return [ArtifactOutcome(record.idx, "failed", message) for record in records]


def _summary(artifacts: list[ArtifactOutcome]) -> str:
    if not artifacts:
        return "no audio files ready for delivery"
    failed = sum(artifact.status != "success" for artifact in artifacts)
    if failed:
        return f"{len(artifacts) - failed} delivered · {failed} failed"
    return f"{len(artifacts)} delivered"


def _deliver_target(config: Config, job: state.Job, target, planned: DeliveryPlan,
                    records: list[state.Record], work_ids: dict[int, str], emit) -> DeliveryOutcome:
    outcomes: dict[int, ArtifactOutcome] = {}
    destinations: dict[int, str] = {}
    pending: list[state.Record] = []
    for record in records:
        work_id = work_ids.get(record.idx, "")
        destination, outcome = _prepare(config, target.name, planned, record, work_id)
        destinations[record.idx] = destination
        if outcome is None:
            pending.append(record)
        else:
            outcomes[record.idx] = outcome

    if pending:
        try:
            delivered = _execute_target(target, config, job, pending, planned, emit)
        except Exception as error:
            delivered = [ArtifactOutcome(record.idx, "failed", str(error)) for record in pending]
        by_idx = {outcome.idx: outcome for outcome in delivered}
        for record in pending:
            outcome = by_idx.get(record.idx)
            if outcome is None:
                outcome = ArtifactOutcome(record.idx, "failed",
                                          "target did not report an outcome for this file")
            elif outcome.status not in {"success", "failed"}:
                outcome = ArtifactOutcome(record.idx, "failed",
                                          f"target returned invalid status: {outcome.status}")
            outcomes[record.idx] = _record_outcome(config, target.name, destinations[record.idx],
                                                   work_ids.get(record.idx, ""), outcome)

    artifacts = [outcomes[record.idx] for record in records if record.idx in outcomes]
    for artifact in artifacts:
        emit("delivery", f"[{artifact.idx}] {target.name}: {artifact.status} · {artifact.message}")
    success = all(artifact.status == "success" for artifact in artifacts)
    return DeliveryOutcome(target.name, success, _summary(artifacts), artifacts)


def deliver(config: Config, job: state.Job, approved_plans: list[DeliveryPlan] | None,
            emit=lambda stage, message: None) -> list[DeliveryOutcome]:
    """Write the approved plans, one durable outcome per file and collection, and keep going past a failure."""
    current = delivery_plans(config, job)
    if approved_plans is None or current != approved_plans:
        return _stale(current, list(approved_plans or ()), importing.delivery_records(job))

    records = importing.delivery_records(job)
    work_ids = _work_ids(config, job, records)
    return [
        _deliver_target(config, job, target, planned, records, work_ids, emit)
        for target, planned in zip(exporting.configured(config), current)
    ]
