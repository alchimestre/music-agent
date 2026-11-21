"""Structured application operations shared with the native desktop."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import acquire, exporting, library, operations, passes, process, reference, state, store, tracklist
from .config import Config
from .credentials import CredentialStore, CredentialStoreError
from .resources import bundle_config
from .settings import load_settings, save_settings, settings_path, validate_settings


@dataclass(frozen=True)
class Progress:
    stage: str
    message: str
    job_id: str = ''
    idx: int | None = None
    fraction: float | None = None


@dataclass
class Preview:
    name: str
    source: str
    records: list[state.Record]
    matches: list
    held: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueRow:
    job_id: str
    idx: int
    label: str
    status: str
    decision: str
    notes: str
    local_path: str
    work_id: str
    source: str = ''
    peer: str = ''
    peer_queue: int | None = None
    progress: str = ''
    queue_reason: str = ""


@dataclass
class RunResult:
    job_id: str
    completed: int = 0
    review: int = 0
    failed: int = 0
    ungenred: list[str] = field(default_factory=list)
    deliveries: list = field(default_factory=list)
    already_held: int = 0
    outstanding: int = 0
    delivery_pending: tuple[str, ...] = ()


class Application:
    def __init__(self, config: Config | None = None, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self.config = bundle_config(config or load_settings(self.path))
        self.credential_error = ''
        if config is None:
            try:
                self.config = CredentialStore().load(self.config)
            except CredentialStoreError as error:
                self.credential_error = str(error)
        from .runtime import SoulseekRuntime

        self.runtime = SoulseekRuntime(self.config)
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self._approved_plans: dict[str, list] = {}

    def save(self, config: Config, remember: bool = True) -> None:
        validate_settings(config, self.path)
        if remember:
            CredentialStore().save(config)
        save_settings(self.path, config)
        self.config = bundle_config(config)
        self.credential_error = ''

    def preview(self, source: str) -> Preview:
        return preview_source(self.config, source, show_matches=True)

    def enqueue(self, preview: Preview) -> str:
        with store.writer_lock(self.config.state_root):
            job = state.create(self.config, preview.name, records=preview.records)
            job.append_event('submission', source=preview.source)
            return job.id

    def snapshot(self, job_id: str | None = None) -> list[QueueRow]:
        rows = []
        jobs = [operations.find_job(self.config, job_id)] if job_id is not None else state.all_jobs(self.config)
        for job in jobs:
            events = job.events()
            source = next((event.get('source', '') for event in events
                           if event.get('event') == 'submission'), '')
            peers = {event['idx']: event.get('peer', '') for event in events
                     if event.get('event') == 'transfer_started' and event.get('idx') is not None}
            work_ids = store.work_ids(self.config.state_root, job.id)
            records = job.records()
            waiting = state.queue(job, records=records, events=events)
            reasons = {
                record.idx: reason
                for reason, group in (
                    ("review", waiting.needs_you), ("nothing_offered", waiting.nothing_offered),
                    ("nothing_usable", waiting.nothing_usable), ("not_searched", waiting.not_searched),
                )
                for record in group
            }
            for record in records:
                rows.append(QueueRow(job.id, record.idx, record.label, record.status,
                                     record.decision, record.notes, record.local_path,
                                     work_ids.get(record.idx, ''), source=source,
                                     peer=peers.get(record.idx, ''), queue_reason=reasons.get(record.idx, '')))
        return rows

    def run(self, job_id: str, emit=lambda event: None) -> RunResult:
        with operations.lock(self.config.state_root):
            self.stop_event.clear()
            job = operations.find_job(self.config, job_id)
            self._prerequisites()
            shelf = library.open(self.config)
            if any(row.status in state.LIVE and not row.decided() for row in job.records()):
                self._acquire(job, shelf, emit)
            if not self.stop_event.is_set():
                self._process(job, shelf, emit)
            return self._result(job)

    def _acquire(self, job: state.Job, shelf, emit) -> None:
        emit(Progress('acquire', 'Starting Soulseek and reconciling transfers.', job.id))
        status = self.runtime.start()
        if status.error:
            raise RuntimeError(status.error)
        acquire.run(self.config, job.execution(), self.runtime.daemon, reference.lookup(self.config),
                    library=shelf, stream=io.StringIO(), stop_event=self.stop_event,
                    pause_event=self.pause_event, retry=bool(job.searched()),
                    log=lambda message: emit(Progress('acquire', message, job.id)),
                    progress=lambda row: self._transfer_progress(job, row, emit))

    def _transfer_progress(self, job: state.Job, row: dict, emit) -> None:
        fraction = row.get('progress')
        if fraction is None:
            return
        emit(Progress('transfer', f"{row.get('peer', '')} · {fraction * 100:.0f}%", job.id,
                      row.get('idx'), float(fraction)))

    def _process(self, job: state.Job, shelf, emit) -> None:
        emit(Progress('process', 'Identifying, converting, normalizing, and tagging.', job.id))
        tools = passes.tools(self.config, shelf=shelf,
                             warn=lambda message: emit(Progress('process', message, job.id)))
        process.run(self.config, job.execution(), tools=tools, stop_event=self.stop_event,
                    log=lambda message: emit(Progress('process', message, job.id)))
        if not self.config.export_targets:
            self._finish(job)

    def _finish(self, job: state.Job) -> None:
        if self.stop_event.is_set() or job.finished():
            return
        if all(record.status in state.FINISHED or
               (record.decided() and record.decision != state.ACCEPTED)
               for record in job.records()):
            job.finish()

    def delivery_plans(self, job_id: str):
        from . import deliveries

        plans = deliveries.delivery_plans(self.config, operations.find_job(self.config, job_id))
        self._approved_plans[job_id] = plans
        return plans

    def deliver(self, job_id: str, emit=lambda event: None):
        from . import deliveries
        if job_id not in self._approved_plans:
            raise ValueError('Preview destination changes before delivering.')

        with operations.lock(self.config.state_root):
            job = operations.find_job(self.config, job_id)
            installed = {target.name for target in exporting.configured(self.config)
                         if target.available(self.config)}
            approved = self._approved_plans.pop(job_id)
            outcomes = deliveries.deliver(self.config, job, approved,
                                          emit=lambda stage, message: emit(Progress(stage, message, job_id)))
            operations.record_delivery(self.config, job, outcomes, plans=approved)
            successful = {outcome.target for outcome in outcomes if outcome.success}
            delivered = any(outcome.artifacts for outcome in outcomes if outcome.target in successful)
            if delivered and installed and installed <= successful and not job.finished():
                job.finish()
            return outcomes

    def shutdown(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        if self.runtime.status().managed:
            self.runtime.stop()

    def _prerequisites(self) -> None:
        for name in (self.config.musicbrainz_contact_env, self.config.acoustid_key_env):
            if not self.config.credential(name):
                raise ValueError(f'{name} is required; enter it in Settings.')
        if not self.config.normalize_tool.is_file():
            raise ValueError('The licensed normalizer package is unavailable; processing cannot start.')

    def _result(self, job: state.Job) -> RunResult:
        records = job.records()
        return RunResult(job.id, completed=sum(record.status in state.FINISHED for record in records),
                         review=state.review_count(records, job.searched()),
                         failed=sum(record.status == state.FAILED for record in records),
                         ungenred=[record.label for record in records
                                   if record.status in state.FINISHED and not record.genre],
                         already_held=sum(record.status == state.FOUND_IN_LIBRARY for record in records),
                         outstanding=sum(record.status in state.LIVE and not record.decided()
                                         for record in records),
                         delivery_pending=self.config.export_targets)


def preview_source(config: Config, source: str, *, show_matches: bool = False) -> Preview:
    from . import deezer, spotify, youtube

    reader = next((module for module in (deezer, spotify, youtube)
                   if module.names_a_playlist(source)), None)
    if reader is None:
        if source.startswith(('http://', 'https://')):
            raise ValueError('Use a public Spotify, Deezer, or YouTube playlist URL.')
        requests = tracklist.read(Path(source).expanduser())
        name = Path(source).stem
    else:
        playlist = reader.playlist(source)
        requests = tracklist.parse(playlist.text())
        name = playlist.name
    if not requests:
        raise ValueError('The source contains no track requests.')
    records = [state.Record(idx=index, artist=request.artist, title=request.title,
                            mix=request.mix, duration_s=request.ref_duration,
                            replaces=request.replaces)
               for index, request in enumerate(requests, 1)]
    if not show_matches:
        return Preview(name, source, records, [])
    matches = store.preview_submission(config.state_root, records)
    shelf = library.Library(library.read_directories(config), [])
    held = {}
    for record in records:
        match = shelf.match(record.artist, record.title, record.mix, record.duration_s)
        if match is not None and match.held.lossless:
            held[record.idx] = str(match.held.path)
    return Preview(name, source, records, matches, held)
