"""Serialized playlist operations, durable outcomes, and bounded progress."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import acquire, deliveries, importing, operations, process, receipts, slsk
from . import spotify_reconcile, spotify_source, state, store, sweep
from .config import Config
from .parsing import render

_PROGRESS_INTERVAL = 2.0
_MESSAGE_LIMIT = 240
_PROCESSING = frozenset({state.DOWNLOADED, state.CONVERTED, state.NORMALIZED})


def start(config: Config, source: str | None = None, *, job_id: str | None = None,
          remove_delivered: bool = False, emit=None) -> dict:
    if bool(source) == bool(job_id):
        raise ValueError('Give a playlist source or --job, not both.')
    with operations.lock(config.state_root):
        job = spotify_source.admit(config, source) if source else submission(config, job_id)
        return _run(config, job, remove_delivered, emit)


def submission(config: Config, job_id: str) -> state.Job:
    if not job_id or Path(job_id).name != job_id or job_id in {'.', '..'}:
        raise ValueError('Invalid submission identifier.')
    job = next((item for item in state.all_jobs(config) if item.id == job_id), None)
    if job is None:
        raise ValueError(f'No submission named {job_id}.')
    return job


def run(config: Config, job: state.Job, *, remove_delivered: bool = False, emit=None) -> dict:
    with operations.lock(config.state_root):
        return _run(config, job, remove_delivered, emit)


def _run(config: Config, job: state.Job, remove_delivered: bool, emit) -> dict:
    monitor = _Monitor(job, emit)
    blocker = ''
    stop_reason = ''
    cleanup = None
    job.append_event('operation_started')
    try:
        stop_reason, blocker = _stages(config, job, monitor)
        if remove_delivered:
            monitor.update('spotify', 'Reconciling verified deliveries with the source playlist.')
            cleanup = spotify_reconcile.reconcile(config, job, apply=True)
            blocker = blocker or cleanup.get('blocker', '')
            if cleanup.get('blocker') and not stop_reason:
                stop_reason = 'spotify_blocked'
    except KeyboardInterrupt:
        blocker = 'interrupted; resume this submission with --job'
        stop_reason = 'user_stopped'
    except Exception as error:
        blocker = str(error) or type(error).__name__
        stop_reason = 'failed'
        monitor.log(f'{type(error).__name__}: {blocker}')
    report = receipts.write(config, job, stage='blocked' if blocker else 'complete',
                            blocker=blocker, stop_reason=stop_reason)
    if cleanup is not None:
        report['spotify'] = cleanup
        _write_json(job.root / 'receipt.json', report)
    if report['complete'] and not blocker and not job.finished():
        job.finish()
    job.append_event('operation_finished', complete=report['complete'], stage=report['stage'],
                     blocker=blocker, stop_reason=report['stop_reason'])
    monitor.update(report['stage'], blocker or 'Completion receipt saved.')
    return report


def _stages(config: Config, job: state.Job, monitor: _Monitor) -> tuple[str, str]:
    from . import cli

    _process_audio(config, job, monitor)
    blocker = ''
    stop_reason = ''
    needs_acquisition = any(record.status in state.LIVE and not record.decided()
                            and not (record.local_path and Path(record.local_path).is_file())
                            for record in job.records())
    new_indices = None
    if needs_acquisition:
        previous = {record.idx: (record.status, record.local_path) for record in job.records()}
        monitor.update('acquire', 'Acquiring outstanding requests.')
        try:
            with open(os.devnull, 'w') as stream:
                cli._one_pass(config, job, retry=bool(job.searched()), log=monitor.log,
                              stream=stream, progress=monitor.transfer)
        except cli.ACQUIRE_ERRORS as error:
            blocker = str(error)
            if isinstance(error, acquire.ServerStuck) and error.lost_session:
                stop_reason = 'displaced'
            elif isinstance(error, (acquire.ServerStuck, slsk.DaemonError)):
                stop_reason = 'transient_disconnected'
            else:
                stop_reason = 'acquisition_failed'
        new_indices = {record.idx for record in job.records()
                       if record.status in _PROCESSING and
                       (record.status, record.local_path) != previous.get(record.idx)}
        _process_audio(config, job, monitor, indices=new_indices)
    _held_audio(config, job)
    outcomes = _deliver(config, job, monitor)
    if not stop_reason and any(not outcome.success for outcome in outcomes):
        stop_reason = 'collection_blocked'
        blocker = '; '.join(f'{outcome.target}: {outcome.message}' for outcome in outcomes
                            if not outcome.success)
    if not stop_reason and not importing.delivery_records(job):
        stop_reason = 'no_acquisitions'
    monitor.update('sweep', 'Clearing interrupted partial transfers.')
    leftovers = sweep.find(config)
    sweep.clear(sweep.Leftovers(leftovers.partial, ()), log=monitor.log)
    return stop_reason, blocker


class _SelectedExecution:
    def __init__(self, execution, indices: set[int]) -> None:
        self.execution = execution
        self.indices = indices

    def __getattr__(self, name):
        return getattr(self.execution, name)

    def records(self) -> list[state.Record]:
        return [record for record in self.execution.records() if record.idx in self.indices]

    def write_records(self, records: list[state.Record]) -> None:
        self.execution.write_records(records)


def _process_audio(config: Config, job: state.Job, monitor: _Monitor,
                   *, indices: set[int] | None = None) -> None:
    from . import cli

    execution = job.execution()
    if indices is not None:
        execution = _SelectedExecution(execution, indices)
    records = execution.records()
    if not any(record.status in _PROCESSING or
               record.status == state.RETRY_LATER and record.local_path for record in records):
        return
    monitor.update('process', 'Processing retained audio checkpoints.')
    tools = cli._tools(config)
    summary = process.run(config, execution, tools=tools, log=monitor.log)
    monitor.log(summary.report())


def _held_audio(config: Config, job: state.Job) -> None:
    work_ids = store.work_ids(config.state_root, job.id)
    held = {work_ids.get(event.get('idx')): event for event in job.events()
            if event.get('event') == 'already_held' and event.get('idx') in work_ids}
    records = job.records()
    changed = False
    for record in records:
        if record.status != state.FOUND_IN_LIBRARY or record.local_path:
            continue
        event = held.get(work_ids[record.idx], {})
        path = event.get('path') or event.get('held')
        if not path or not Path(path).is_file():
            continue
        record.local_path = path
        record.genre = event.get('genre') or record.genre
        record.format = Path(path).suffix.lstrip('.')
        changed = True
    if changed:
        job.write_records(records)


def _deliver(config: Config, job: state.Job, monitor: _Monitor) -> list:
    if not importing.delivery_records(job):
        return []
    monitor.update('import', 'Verifying configured collection changes.')
    plans = deliveries.delivery_plans(config, job)
    for planned in plans:
        monitor.update(f'preview:{planned.target}', planned.error or planned.changes)
    outcomes = deliveries.deliver(config, job, approved_plans=plans,
                                  emit=lambda stage, message: monitor.log(f'{stage}: {message}'))
    for outcome in outcomes:
        monitor.update(f'import:{outcome.target}', outcome.message)
    return outcomes


class _Monitor:
    def __init__(self, job: state.Job, emit) -> None:
        self.job = job
        self.emit = emit
        self.stage = ''
        self.last = 0.0
        self.guard = threading.RLock()

    def update(self, stage: str, message: str, *, idx=None, fraction=None) -> None:
        with self.guard:
            now = time.monotonic()
            if stage == self.stage and now - self.last < _PROGRESS_INTERVAL:
                return
            self.stage = stage
            self.last = now
            row = {'job_id': self.job.id, 'stage': stage, 'message': render(str(message))[:_MESSAGE_LIMIT],
                   'updated_at': time.time(), 'idx': idx, 'fraction': fraction}
            _write_json(self.job.root / 'progress.json', row)
            if self.emit is not None:
                self.emit(row)

    def log(self, message: str) -> None:
        with self.guard:
            with (self.job.root / 'run.log').open('a', encoding='utf-8') as stream:
                stream.write(str(message) + '\n')
            self.update(self.stage, str(message)[-_MESSAGE_LIMIT:])

    def transfer(self, row: dict) -> None:
        fraction = row.get('progress')
        if fraction is not None:
            self.update('acquire', f"Track {row.get('idx')}: {fraction:.0%}",
                        idx=row.get('idx'), fraction=fraction)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
