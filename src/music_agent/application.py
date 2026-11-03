"""Structured application operations shared with the native desktop."""

from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import acquire, genre, library, process, reference, review, state, store, tracklist
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


@dataclass(frozen=True)
class CandidateView:
    number: int
    user: str
    path: str
    format: str
    reason: str
    label: str


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
        return preview_source(self.config, source)

    def enqueue(self, preview: Preview) -> str:
        with store.writer_lock(self.config.state_root):
            job = state.create(self.config, preview.name, records=preview.records)
            job.append_event('submission', source=preview.source)
            return job.id

    def snapshot(self) -> list[QueueRow]:
        rows = []
        for job in state.all_jobs(self.config):
            source = next((event.get('source', '') for event in job.events()
                           if event.get('event') == 'submission'), '')
            for record in job.records():
                rows.append(QueueRow(job.id, record.idx, record.label, record.status,
                                     record.decision, record.notes, record.local_path,
                                     job.work_id(record.idx), source=source))
        return rows

    def candidates(self, job_id: str, idx: int) -> list[CandidateView]:
        record = self._record(job_id, idx)
        return [CandidateView(number, verdict.candidate.user, verdict.candidate.path,
                              verdict.candidate.ext, verdict.reason, verdict.label.value)
                for number, verdict in enumerate(record.offered(), 1)]

    def run(self, job_id: str, emit=lambda event: None) -> RunResult:
        self.stop_event.clear()
        job = self._job(job_id)
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
        self.runtime.apply_settings(self.config)
        acquire.run(self.config, job.execution(), self.runtime.daemon, reference.lookup(self.config),
                    library=shelf, stream=io.StringIO(), stop_event=self.stop_event,
                    pause_event=self.pause_event,
                    log=lambda message: emit(Progress('acquire', message, job.id)))

    def _process(self, job: state.Job, shelf, emit) -> None:
        emit(Progress('process', 'Identifying, converting, normalizing, and tagging.', job.id))
        families = genre.families(self.config.taxonomy)
        tools = process.Tools.live(self.config, vocabulary=genre.vocabulary(shelf.genres(), families),
                                   mappings=genre.table(self.config.taxonomy))
        process.run(self.config, job.execution(), tools=tools,
                    log=lambda message: emit(Progress('process', message, job.id)))
        if not self.config.export_targets:
            result = self._result(job)
            if not result.failed and not result.review and all(
                    record.status in {state.TAGGED, state.IMPORTED, state.FOUND_IN_LIBRARY}
                    or record.decided() and record.decision != state.ACCEPTED
                    for record in job.records()):
                job.finish()

    def decide(self, job_id: str, idx: int, action: str, candidate: int | None = None,
               emit=lambda event: None):
        job = self._job(job_id)
        with store.writer_lock(self.config.state_root):
            if action == 'skip':
                return review.skip(job, idx)
            if action == 'not_found':
                return review.not_found(job, idx)
            if action != 'accept':
                raise ValueError('Review action must be accept, skip, or not_found.')
            self._prerequisites()
            return review.accept(job, idx, staging=self.config.staging, number=candidate,
                                 daemon_factory=self._review_daemon,
                                 log=lambda message: emit(Progress('review', message, job.id, idx)))

    @contextmanager
    def _review_daemon(self):
        status = self.runtime.start()
        if status.error:
            raise RuntimeError(status.error)
        yield self.runtime.daemon

    def delivery_plans(self, job_id: str):
        from . import deliveries

        plans = deliveries.delivery_plans(self.config, self._job(job_id))
        self._approved_plans[job_id] = plans
        return plans

    def deliver(self, job_id: str, emit=lambda event: None):
        from . import deliveries
        if job_id not in self._approved_plans:
            raise ValueError('Preview destination changes before delivering.')

        with store.writer_lock(self.config.state_root):
            return deliveries.deliver(self.config, self._job(job_id), self._approved_plans.pop(job_id),
                                      emit=lambda stage, message: emit(Progress(stage, message, job_id)))

    def shutdown(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        if self.runtime.status().managed:
            self.runtime.stop()

    def _job(self, job_id: str) -> state.Job:
        if not job_id or Path(job_id).name != job_id or job_id in {'.', '..'}:
            raise ValueError('Invalid submission identifier.')
        job = next((job for job in state.all_jobs(self.config) if job.id == job_id), None)
        if job is None:
            raise ValueError('Submission does not exist.')
        return job

    def _record(self, job_id: str, idx: int) -> state.Record:
        record = next((row for row in self._job(job_id).records() if row.idx == idx), None)
        if record is None:
            raise ValueError('Track does not exist in this submission.')
        return record

    def _prerequisites(self) -> None:
        for name in (self.config.musicbrainz_contact_env, self.config.acoustid_key_env):
            if not self.config.credential(name):
                raise ValueError(f'{name} is required; enter it in Settings.')
        if not self.config.normalize_tool.is_file():
            raise ValueError('The licensed normalizer package is unavailable; processing cannot start.')

    def _result(self, job: state.Job) -> RunResult:
        records = job.records()
        completed = {state.TAGGED, state.IMPORTED, state.FOUND_IN_LIBRARY}
        return RunResult(job.id, completed=sum(record.status in completed for record in records),
                         review=sum(record.needs_you() and not record.decided() for record in records),
                         failed=sum(record.status == state.FAILED for record in records),
                         ungenred=[record.label for record in records
                                   if record.status in completed and not record.genre],
                         already_held=sum(record.status == state.FOUND_IN_LIBRARY for record in records),
                         outstanding=sum(record.status in state.LIVE and not record.decided()
                                         for record in records),
                         delivery_pending=self.config.export_targets)


def preview_source(config: Config, source: str) -> Preview:
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
    matches = store.preview_submission(config.state_root, records)
    shelf = library.Library(library.read_directories(config), [])
    held = {}
    for record in records:
        match = shelf.match(record.artist, record.title, record.mix, record.duration_s)
        if match is not None and match.held.lossless:
            held[record.idx] = str(match.held.path)
    return Preview(name, source, records, matches, held)
