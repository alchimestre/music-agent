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
    verified_artifacts: tuple[str, ...] = field(default=(), repr=False)
    replacements: tuple[tuple[str, str, int, int], ...] = field(default=(), repr=False)


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
    """Canonical work per membership; a job with no database falls back to the records it holds."""
    mapping = store.work_ids(config.state_root, job.id)
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


def _rekordbox_plan(target, config: Config, job: state.Job,
                    records: list[state.Record]) -> DeliveryPlan:
    if not target.available(config):
        return _unavailable(target.name, "rekordbox:unavailable", "target is not installed on this machine")
    try:
        planned = importing.plan(config, job, records=records)
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
        error=_failure_details(failures, records),
        fingerprint=_key(planned.destination, planned.fingerprint),
        target_identity=planned.destination,
        artifact_errors=tuple(sorted(failures.items())),
        verified_artifacts=tuple(sorted({
            str(Path(record.local_path).resolve()) for record in records
            if record.idx in planned.already and record.local_path
        })),
    )


def _traktor_plan(target, config: Config, job: state.Job, records: list[state.Record]) -> DeliveryPlan:
    identity = traktor.destination(config)
    if not target.available(config):
        return _unavailable(target.name, identity, "target is not installed on this machine")
    try:
        store.ensure_projection(config.state_root, job.id)
    except Exception as error:
        return _unavailable(target.name, identity, f"cannot refresh the job's projection: {error}")
    try:
        work_ids = _work_ids(config, job, records)
    except Exception as error:
        return _unavailable(target.name, identity, f"cannot read canonical work ids: {error}")
    errors = {record.idx: "record has no canonical work_id"
              for record in records if not work_ids.get(record.idx)}
    deliverable = [record for record in records if record.idx not in errors]
    waiting = [record for record in deliverable if record.replaces and Path(record.replaces).is_file()]
    if waiting and config.export_targets != ("traktor",):
        return _unavailable(
            target.name, identity,
            f"{len(waiting)} file(s) this run replaces are still present — deliver the collection that "
            "removes them, then preview and deliver again")
    try:
        originals = traktor.replacements(config, waiting)
        replacements = tuple(_replacement_stamp(old, new) for old, new in sorted(originals.items()))
        planned = traktor.plan(config)
        if planned is None:
            return _unavailable(target.name, identity, "traktor-nml is not installed on this machine")
        paths = [record.local_path for record in records if record.local_path]
        verified = traktor.verify(config, paths) if paths else None
    except (traktor.Missing, traktor.Failed) as error:
        return _unavailable(target.name, identity, str(error))
    except Exception as error:
        return _unavailable(target.name, identity, f"cannot read the Traktor collection: {error}")
    return DeliveryPlan(
        target=target.name,
        destination="the Traktor collection selected by the configured traktor-nml command",
        changes=(" · ".join((planned.changes, f"{len(replacements)} replacement file(s) to move for repointing"))
                 if replacements else planned.changes),
        requires_confirmation=bool(replacements) or planned.requires_confirmation,
        fingerprint=_key(verified["collection"] if verified else planned.destination, planned.changes),
        target_identity=verified["collection"] if verified else planned.destination,
        verified_artifacts=tuple(sorted(path for path, present in verified["paths"].items() if present))
                           if verified else (),
        replacements=replacements,
        artifact_errors=tuple(sorted(errors.items())),
    )


def _replacement_stamp(old: Path, new: Path) -> tuple[str, str, int, int]:
    details = old.stat()
    return str(old), str(new), details.st_size, details.st_mtime_ns


def _failure_details(failures: dict[int, str], records: list[state.Record]) -> str:
    labels = {record.idx: record.label for record in records}
    lines = []
    for idx, message in sorted(failures.items()):
        label = labels.get(idx) or f"track {idx}"
        lines.append(f"[{idx}] {label}: {message}")
    return "\n".join(lines)


def _plan_target(target, config: Config, job: state.Job, records: list[state.Record]) -> DeliveryPlan:
    if target.name == "rekordbox":
        return _rekordbox_plan(target, config, job, records)
    if target.name == "traktor":
        return _traktor_plan(target, config, job, records)
    return _unavailable(target.name, target.name,
                        f"durable delivery is unsupported for target {target.name}")


def delivery_plans(config: Config, job: state.Job) -> list[DeliveryPlan]:
    """What each configured collection would change, without writing any of them."""
    return _plans(config, job, importing.delivery_records(job), exporting.configured(config))


def _plans(config: Config, job: state.Job, records: list[state.Record], targets) -> list[DeliveryPlan]:
    identities = tuple(_file_identity(record) for record in records)
    return [replace(_plan_target(target, config, job, records), artifact_identity=identities)
            for target in targets]


def delivery_receipts(config: Config, job: state.Job,
                      plans: list[DeliveryPlan] | None = None, *,
                      records: list[state.Record] | None = None) -> dict[int, dict[str, dict]]:
    """Read durable per-file outcomes without invoking a collection writer."""
    if records is None:
        records = job.records()
    work_ids = _work_ids(config, job, records)
    targets = exporting.configured(config)
    if plans is None:
        plans = delivery_plans(config, job)
    planned = {plan.target: plan for plan in plans}
    statuses = store.delivery_statuses(config.state_root, job.id) if targets else {}
    result = {record.idx: {} for record in records}
    for target in targets:
        plan = planned.get(target.name)
        errors = dict(plan.artifact_errors) if plan else {}
        verified = set(plan.verified_artifacts) if plan else set()
        for record in records:
            result[record.idx][target.name] = _delivery_receipt(
                target.name, plan, record, work_ids.get(record.idx, ""), statuses, errors, verified)
    return result


def refresh_receipts(config: Config, job: state.Job, targets: list[str], *,
                     plans: list[DeliveryPlan] | None = None) -> list[DeliveryOutcome]:
    """Save fresh verification after a legacy import, without writing to any collection."""
    if not targets:
        return []
    records = importing.delivery_records(job)
    work_ids = _work_ids(config, job, records)
    selected = exporting.configured(config, only=targets)
    if plans is None:
        plans = _plans(config, job, records, selected)
    return [_refresh_target(config, plan, records, work_ids)
            for target in selected for plan in plans if target.name == plan.target]


def _refresh_target(config: Config, plan: DeliveryPlan, records: list[state.Record],
                    work_ids: dict[int, str]) -> DeliveryOutcome:
    errors = dict(plan.artifact_errors)
    verified_paths = set(plan.verified_artifacts)
    artifacts = []
    entries = {}
    for record in records:
        work_id = work_ids.get(record.idx)
        if not work_id:
            artifacts.append(ArtifactOutcome(record.idx, "failed", "record has no canonical work_id"))
            continue
        path = Path(record.local_path)
        verified = (not plan.blocked and record.idx not in errors and path.is_file()
                    and str(path.resolve()) in verified_paths)
        status = "success" if verified else "failed"
        message = ("delivered to this collection" if verified
                   else errors.get(record.idx) or plan.error or "current artifact is not verified")
        artifact = ArtifactOutcome(record.idx, status, message)
        artifacts.append(artifact)
        if not plan.blocked:
            destination = _collection_key(plan.target_identity or plan.destination, record, work_id)
            entries[record.idx] = _entry(plan.target, work_id, destination, artifact)
    errors_saving = _save_outcomes(config, entries, {artifact.idx: artifact for artifact in artifacts})
    artifacts = [errors_saving[artifact.idx] for artifact in artifacts]
    success = not plan.blocked and all(artifact.status == "success" for artifact in artifacts)
    return DeliveryOutcome(plan.target, success, _summary(artifacts), artifacts)


def _delivery_receipt(target: str, plan: DeliveryPlan | None, record: state.Record, work_id: str,
                      statuses: dict, artifact_errors: dict[int, str], verified: set[str]) -> dict:
    if not Path(record.local_path).is_file():
        return {"status": "failed", "message": "current artifact is missing"}
    if not work_id:
        return {"status": "failed", "message": "record has no canonical work_id"}
    if plan is None:
        return {"status": "failed", "message": "target has no current delivery plan"}
    if record.idx in artifact_errors:
        return {"status": "failed", "message": artifact_errors[record.idx]}
    if plan.blocked:
        return {"status": "failed", "message": plan.error or "target is unavailable"}
    if str(Path(record.local_path).resolve()) not in verified:
        return {"status": "pending", "message": "current collection does not verify this artifact"}
    destination = _collection_key(plan.target_identity or plan.destination, record, work_id)
    status = statuses.get((work_id, target, destination))
    if status == "success":
        return {"status": "success", "message": "delivered to this collection"}
    if status in {None, "pending"}:
        return {"status": "pending", "message": "no current success receipt"}
    return {"status": "failed", "message": "delivery receipt is failed or unrecognized"}


def _stale(current: list[DeliveryPlan], approved: list[DeliveryPlan],
           records: list[state.Record]) -> list[DeliveryOutcome]:
    message = "collection or input files changed; preview again before delivery"
    names = list(dict.fromkeys([plan.target for plan in current] + [plan.target for plan in approved]))
    return [_failure(name, records, message) for name in names]


def _entry(target: str, work_id: str, destination: str, outcome: ArtifactOutcome) -> tuple:
    return work_id, target, destination, outcome.status, outcome.message if outcome.status == "failed" else ""


def _save_outcomes(config: Config, entries: dict[int, tuple],
                   outcomes: dict[int, ArtifactOutcome]) -> dict[int, ArtifactOutcome]:
    try:
        store.set_deliveries(config.state_root, list(entries.values()))
    except Exception as error:
        for idx in entries:
            message = outcomes[idx].message
            outcomes[idx] = ArtifactOutcome(idx, "failed", f"{message}; could not save delivery status: {error}")
    return outcomes


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
            records=records,
        )
    delivered = set(summary.imported + summary.repointed + summary.already)
    outcomes = []
    for record in records:
        if record.idx in errors:
            outcomes.append(ArtifactOutcome(record.idx, "failed", errors[record.idx]))
        elif record.idx in delivered:
            outcomes.append(ArtifactOutcome(record.idx, "success", "added to rekordbox"))
        elif record.idx in summary.failed:
            note = record.notes or "rekordbox refused this file"
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
        return _execute_traktor(config, job, records, planned, emit)
    message = f"durable delivery is unsupported for target {target.name}"
    return [ArtifactOutcome(record.idx, "failed", message) for record in records]


def _execute_traktor(config: Config, job: state.Job, records: list[state.Record],
                     planned: DeliveryPlan, emit) -> list[ArtifactOutcome]:
    store.ensure_projection(config.state_root, job.id)
    paths = [record.local_path for record in records]
    if planned.replacements:
        originals = {}
        for old, new, size, modified in planned.replacements:
            try:
                details = Path(old).stat()
            except OSError:
                details = None
            if details is None or (details.st_size, details.st_mtime_ns) != (size, modified):
                raise traktor.Failed("replacement files changed; preview again before delivery")
            originals[Path(old)] = Path(new)
        _, verified = traktor.sync_replacements(
            config, originals, paths, collection=planned.target_identity,
            log=lambda message: emit("delivery", message))
    else:
        if planned.requires_confirmation:
            traktor.apply(config)
            emit("delivery", "Traktor updated; run Check Consistency → Analyze on tracks whose audio was replaced")
        verified = traktor.verify(config, paths)
        if verified["collection"] != planned.target_identity:
            raise traktor.Failed("Traktor collection identity changed after the approved plan")
    return [
        ArtifactOutcome(record.idx, "success", "verified in the Traktor collection")
        if verified["paths"][str(Path(record.local_path).resolve())]
        else ArtifactOutcome(record.idx, "failed", "file is not present in the Traktor collection")
        for record in records
    ]


def _summary(artifacts: list[ArtifactOutcome]) -> str:
    if not artifacts:
        return "no audio files ready for delivery"
    failed = sum(artifact.status != "success" for artifact in artifacts)
    if failed:
        return f"{len(artifacts) - failed} delivered · {failed} failed"
    return f"{len(artifacts)} delivered"


def _deliver_target(config: Config, job: state.Job, target, planned: DeliveryPlan,
                    records: list[state.Record], work_ids: dict[int, str], statuses: dict,
                    emit) -> DeliveryOutcome:
    outcomes: dict[int, ArtifactOutcome] = {}
    destinations: dict[int, str] = {}
    pending: list[state.Record] = []
    entries = {}
    verified = set(planned.verified_artifacts)
    for record in records:
        work_id = work_ids.get(record.idx, "")
        destination = _collection_key(planned.target_identity or planned.destination, record, work_id)
        destinations[record.idx] = destination
        status = statuses.get((work_id, target.name, destination))
        outcome = _prepare(record, work_id, planned, verified, status)
        outcomes[record.idx] = outcome
        if outcome.status == "pending":
            pending.append(record)
        if work_id and outcome.status == "pending":
            entries[record.idx] = _entry(target.name, work_id, destination, outcome)

    _save_outcomes(config, entries, outcomes)
    pending = [record for record in pending if outcomes[record.idx].status == "pending"]
    _deliver_pending(config, job, target, planned, pending, work_ids, destinations, outcomes, emit)
    artifacts = list(outcomes.values())
    for artifact in artifacts:
        emit("delivery", f"[{artifact.idx}] {target.name}: {artifact.status} · {artifact.message}")
    success = not planned.blocked and all(artifact.status == "success" for artifact in artifacts)
    message = (planned.error or "target is unavailable") if planned.blocked else _summary(artifacts)
    return DeliveryOutcome(target.name, success, message, artifacts)


def _prepare(record: state.Record, work_id: str, planned: DeliveryPlan,
             verified: set[str], status: str | None) -> ArtifactOutcome:
    if not work_id:
        return ArtifactOutcome(record.idx, "failed", "record has no canonical work_id")
    if planned.blocked:
        return ArtifactOutcome(record.idx, "failed", planned.error or "target is unavailable")
    if (status == "success" and not planned.replacements
            and str(Path(record.local_path).resolve()) in verified):
        return ArtifactOutcome(record.idx, "success", "already delivered to this collection")
    if status is not None and status not in store.DELIVERY_STATUSES:
        return ArtifactOutcome(record.idx, "failed", f"unknown saved delivery status: {status}")
    return ArtifactOutcome(record.idx, "pending", "")


def _deliver_pending(config: Config, job: state.Job, target, planned: DeliveryPlan,
                     pending: list[state.Record], work_ids: dict[int, str], destinations: dict[int, str],
                     outcomes: dict[int, ArtifactOutcome], emit) -> None:
    if not pending:
        return
    try:
        delivered = _execute_target(target, config, job, pending, planned, emit)
    except Exception as error:
        delivered = [ArtifactOutcome(record.idx, "failed", str(error)) for record in pending]
    by_idx = {outcome.idx: outcome for outcome in delivered}
    entries = {}
    for record in pending:
        idx = record.idx
        outcome = by_idx.get(idx)
        if outcome is None:
            outcome = ArtifactOutcome(idx, "failed", "target did not report an outcome for this file")
        elif outcome.status not in {"success", "failed"}:
            outcome = ArtifactOutcome(idx, "failed", f"target returned invalid status: {outcome.status}")
        outcomes[idx] = outcome
        entries[idx] = _entry(target.name, work_ids[idx], destinations[idx], outcome)
    _save_outcomes(config, entries, outcomes)


def deliver(config: Config, job: state.Job, approved_plans: list[DeliveryPlan] | None,
            emit=lambda stage, message: None, *, raise_stale: bool = False) -> list[DeliveryOutcome]:
    """Write only approved plans and keep going past a failure in one target."""
    records = importing.delivery_records(job)
    targets = exporting.configured(config)
    try:
        work_ids = _work_ids(config, job, records)
    except Exception as error:
        return [_failure(target.name, records, f"cannot read canonical work ids: {error}")
                for target in targets]
    current = _plans(config, job, records, targets)
    if approved_plans is None or current != approved_plans:
        if raise_stale:
            raise ValueError("collection or input files changed; preview again before delivery")
        return _stale(current, list(approved_plans or ()), records)

    statuses = store.delivery_statuses(config.state_root, job.id)
    return [_deliver_target(config, job, target, planned, records, work_ids, statuses, emit)
            for target, planned in zip(targets, current)]
