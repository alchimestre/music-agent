"""Shared job lookup, operation locking, and status persistence."""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from . import deliveries, receipts, state
from .config import Config
from .store import WriterLocked


@contextmanager
def lock(root: Path):
    directory = Path(root).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    mutex = FileLock(str(directory / 'operation.lock'), is_singleton=True)
    try:
        mutex.acquire(timeout=0)
    except Timeout as error:
        raise WriterLocked('another pipeline operation is active; resume after it finishes') from error
    try:
        yield
    finally:
        mutex.release()


def find_job(config: Config, job_id: str | None) -> state.Job:
    """Require an explicitly named job."""
    if not job_id:
        raise ValueError('Give a job ID.')
    job = state.find(config, job_id)
    if job is None:
        raise ValueError(f'No job named {job_id!r}.')
    return job


def write_json(path: Path, value: dict) -> None:
    """Replace a JSON report atomically."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False)
            file.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_delivery(config: Config, job: state.Job, outcomes: list[deliveries.DeliveryOutcome], *,
                    plans: list[deliveries.DeliveryPlan] | None = None) -> list[dict]:
    """Log a delivery and refresh the job's receipt, over plans recomputed after the write."""
    reports = [_outcome_report(outcome) for outcome in outcomes]
    job.append_event('delivery_finished', outcomes=reports)
    if plans is None:
        plans = deliveries.delivery_plans(config, job)
    previous = receipts.build_status(config, job, plans=plans)
    receipts.write(config, job, stage=previous.get('stage', 'pending'), blocker=previous.get('blocker', ''),
                   stop_reason=previous.get('stop_reason', ''), plans=plans)
    return reports


def _outcome_report(outcome: deliveries.DeliveryOutcome) -> dict:
    return {'target': outcome.target, 'success': outcome.success, 'message': outcome.message,
            'artifacts': [{'idx': item.idx, 'status': item.status, 'message': item.message}
                          for item in outcome.artifacts]}
