"""Canonical SQLite state for submissions, shared work, and delivery outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from filelock import FileLock, Timeout

from .parsing import fold, is_original
from .rules import DURATION_WINDOW
from .tracklist import request_for

DATABASE = "state.sqlite3"
LOCK_FILE = ".writer.lock"
LEGACY_BACKUP = "legacy-backup"
DELIVERY_STATUSES = frozenset({"pending", "success", "failed"})
LOCAL_FIELDS = ("idx", "artist", "title", "mix", "duration_s", "replaces", "decision", "notes")
WORK_FIELDS = ("status", "local_path", "format", "genre", "candidates", "tried", "refused")
ARTIFACT_STATUSES = frozenset({"downloaded", "converted", "normalized", "tagged", "imported"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    work_id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL,
    duration_evidence TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_identity ON work_items(identity_key);
CREATE TABLE IF NOT EXISTS memberships (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    idx INTEGER NOT NULL,
    work_id TEXT NOT NULL REFERENCES work_items(work_id),
    record_json TEXT NOT NULL,
    PRIMARY KEY(job_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY(job_id, seq)
);
CREATE TABLE IF NOT EXISTS deliveries (
    work_id TEXT NOT NULL REFERENCES work_items(work_id),
    target TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(work_id, target, destination)
);
"""


class WriterLocked(RuntimeError):
    """Another process owns the state writer lock."""


@dataclass(frozen=True)
class AdmissionMatch:
    idx: int
    work_id: str
    existing: bool
    created: bool


@dataclass
class _WorkCandidate:
    work_id: str
    durations: list[int]
    existing: bool


@dataclass
class _LockState:
    lock: FileLock
    guard: threading.RLock
    pid: int
    owners: dict[int, int] = field(default_factory=dict)


_LOCK_STATES: dict[Path, _LockState] = {}
_LOCK_STATES_GUARD = threading.Lock()


def database_path(root: Path) -> Path:
    return Path(root) / DATABASE


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    """Hold the one cross-process writer lock for a state root.

    Nested store writes in the same thread share the lock, other threads queue behind it, and a competing
    process raises `WriterLocked` immediately rather than waiting behind a live CLI or desktop run.
    """
    state_root = Path(root).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / LOCK_FILE
    with _LOCK_STATES_GUARD:
        state = _LOCK_STATES.get(path)
        if state is None or state.pid != os.getpid():
            state = _LockState(FileLock(str(path)), threading.RLock(), os.getpid())
            _LOCK_STATES[path] = state
    owner = threading.get_ident()
    with state.guard:
        if not state.owners:
            try:
                state.lock.acquire(timeout=0)
            except Timeout as error:
                raise WriterLocked(f"state writer is already active: {state_root}") from error
        state.owners[owner] = state.owners.get(owner, 0) + 1
        try:
            yield
        finally:
            state.owners[owner] -= 1
            if state.owners[owner] == 0:
                del state.owners[owner]
            if not state.owners:
                state.lock.release()


def writer_lock_held(root: Path) -> bool:
    """Whether the calling thread is inside `writer_lock` for this root.

    Read without `state.guard`: taking it would wait on another thread that may be waiting on this caller.
    """
    path = Path(root).resolve() / LOCK_FILE
    state = _LOCK_STATES.get(path)
    return (state is not None and state.pid == os.getpid()
            and state.owners.get(threading.get_ident(), 0) > 0)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _initialize(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path(root), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(_SCHEMA)
    return connection


def _connect(root: Path) -> sqlite3.Connection | None:
    path = database_path(root)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _identity_key(record: dict) -> str:
    request = request_for(record.get("artist", ""), record.get("title", ""))
    supplied_mix = record.get("mix", "")
    parsed_mix = request.mix
    supplied_key = _version_key(supplied_mix) if supplied_mix else ""
    parsed_key = _version_key(parsed_mix) if parsed_mix else ""
    if supplied_key and parsed_key and supplied_key != parsed_key:
        version = _json(sorted((supplied_key, parsed_key)))
    else:
        version = supplied_key or parsed_key or "original"
    replacement = PureWindowsPath(record.get("replaces", "")).as_posix().casefold()
    return _json((fold(request.artist), fold(request.title), version, replacement))


def _version_key(mix: str) -> str:
    return "original" if is_original(mix) else fold(mix)


def _work_id(identity_key: str, duration: int | None) -> str:
    seed = _json((identity_key, duration)).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()


def _record_data(record: object) -> dict:
    return record if isinstance(record, dict) else record.to_json()


def _local_state(record: dict) -> dict:
    return {field: record.get(field) for field in LOCAL_FIELDS}


def _work_state(record: dict) -> dict:
    return {field: record.get(field) for field in WORK_FIELDS}


def _work_catalog(connection: sqlite3.Connection | None) -> dict[str, list[_WorkCandidate]]:
    catalog: dict[str, list[_WorkCandidate]] = {}
    if connection is None:
        return catalog
    for row in connection.execute("SELECT work_id, identity_key, duration_evidence FROM work_items"):
        candidate = _WorkCandidate(row["work_id"], json.loads(row["duration_evidence"]), True)
        catalog.setdefault(row["identity_key"], []).append(candidate)
    return catalog


def _matching_work(candidates: list[_WorkCandidate], duration: int | None) -> _WorkCandidate | None:
    if duration is None:
        unmeasured = [candidate for candidate in candidates if not candidate.durations]
        if len(unmeasured) == 1:
            return unmeasured[0]
        return candidates[0] if len(candidates) == 1 else None
    compatible = [candidate for candidate in candidates
                  if all(abs(duration - known) <= DURATION_WINDOW for known in candidate.durations)]
    known = [candidate for candidate in compatible if candidate.durations]
    if not known:
        return compatible[0] if len(compatible) == 1 else None
    distances = [max(abs(duration - known_duration) for known_duration in candidate.durations)
                 for candidate in known]
    closest = min(distances)
    matches = [candidate for candidate, distance in zip(known, distances) if distance == closest]
    return matches[0] if len(matches) == 1 else None


def _remember_duration(candidate: _WorkCandidate, duration: int | None) -> None:
    if duration is not None and duration not in candidate.durations:
        candidate.durations.append(duration)


def _save_duration(connection: sqlite3.Connection, candidate: _WorkCandidate) -> None:
    connection.execute("UPDATE work_items SET duration_evidence = ? WHERE work_id = ?",
                      (_json(candidate.durations), candidate.work_id))


def _insert_work(connection: sqlite3.Connection, record: dict, identity_key: str,
                 duration: int | None) -> _WorkCandidate:
    work_id = _work_id(identity_key, duration)
    candidate = _WorkCandidate(work_id, [] if duration is None else [duration], False)
    connection.execute(
        "INSERT INTO work_items(work_id, identity_key, duration_evidence, state_json) VALUES (?, ?, ?, ?)",
        (work_id, identity_key, _json(candidate.durations), _json(_work_state(record))),
    )
    return candidate


def _insert_membership(connection: sqlite3.Connection, job_id: str, record: dict,
                       candidate: _WorkCandidate) -> None:
    connection.execute(
        "INSERT INTO memberships(job_id, idx, work_id, record_json) VALUES (?, ?, ?, ?)",
        (job_id, record["idx"], candidate.work_id, _json(_local_state(record))),
    )


def _carries_artifact(state: dict) -> bool:
    return bool(state.get("local_path")) or state.get("status") in ARTIFACT_STATUSES


def _merge_work_state(connection: sqlite3.Connection, candidate: _WorkCandidate,
                      record: dict) -> None:
    """Adopt a reported artifact onto work that has none, never replacing settled work."""
    incoming = _work_state(record)
    if not _carries_artifact(incoming):
        return
    row = connection.execute("SELECT state_json FROM work_items WHERE work_id = ?",
                             (candidate.work_id,)).fetchone()
    if _carries_artifact(json.loads(row["state_json"])):
        return
    connection.execute("UPDATE work_items SET state_json = ? WHERE work_id = ?",
                       (_json(incoming), candidate.work_id))


def _admit_records(connection: sqlite3.Connection, job_id: str, records: Sequence[object],
                   catalog: dict[str, list[_WorkCandidate]]) -> list[AdmissionMatch]:
    matches: list[AdmissionMatch] = []
    seen_indices: set[int] = set()
    for item in records:
        record = _record_data(item)
        idx = record["idx"]
        if idx in seen_indices:
            raise ValueError(f"submission contains duplicate index {idx}")
        seen_indices.add(idx)
        prior = connection.execute(
            "SELECT work_id FROM memberships WHERE job_id = ? AND idx = ?", (job_id, idx)
        ).fetchone()
        if prior is not None:
            matches.append(AdmissionMatch(idx, prior["work_id"], True, False))
            continue
        identity = _identity_key(record)
        candidates = catalog.setdefault(identity, [])
        duration = record.get("duration_s")
        candidate = _matching_work(candidates, duration)
        created = candidate is None
        if created:
            candidate = _insert_work(connection, record, identity, duration)
            candidates.append(candidate)
        else:
            _remember_duration(candidate, duration)
            _save_duration(connection, candidate)
            _merge_work_state(connection, candidate, record)
        _insert_membership(connection, job_id, record, candidate)
        matches.append(AdmissionMatch(idx, candidate.work_id, candidate.existing, created))
    return matches


def preview_submission(root: Path, records: Sequence[object]) -> list[AdmissionMatch]:
    """Plan ordered admission without creating the database or changing live state."""
    connection = _connect(Path(root))
    try:
        catalog = _work_catalog(connection)
    finally:
        if connection is not None:
            connection.close()
    matches: list[AdmissionMatch] = []
    for item in records:
        record = _record_data(item)
        identity = _identity_key(record)
        candidates = catalog.setdefault(identity, [])
        duration = record.get("duration_s")
        candidate = _matching_work(candidates, duration)
        created = candidate is None
        if created:
            candidate = _WorkCandidate(_work_id(identity, duration),
                                       [] if duration is None else [duration], False)
            candidates.append(candidate)
        else:
            _remember_duration(candidate, duration)
        matches.append(AdmissionMatch(record["idx"], candidate.work_id,
                                      candidate.existing, created))
    return matches


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"legacy JSONL entry is not an object: {path}")
        entries.append(entry)
    return entries


def _backup_legacy(job_root: Path) -> None:
    backup = job_root / LEGACY_BACKUP
    for filename in ("tracks.jsonl", "events.jsonl"):
        source = job_root / filename
        destination = backup / filename
        if source.is_file() and not destination.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _prepare_legacy_record(data: dict):
    from .state import Record, RETRY_LATER

    record = Record.from_json(data)
    if record.status in ARTIFACT_STATUSES and (not record.local_path or not Path(record.local_path).is_file()):
        record.local_path = ""
        record.format = ""
        record.status = RETRY_LATER
    elif record.local_path and not Path(record.local_path).is_file():
        record.local_path = ""
        record.format = ""
    return record


def _import_legacy_job(root: Path, job_root: Path) -> bool:
    if job_exists(root, job_root.name):
        return False
    _backup_legacy(job_root)
    legacy_records = [_prepare_legacy_record(data) for data in _read_jsonl(job_root / "tracks.jsonl")]
    events = _read_jsonl(job_root / "events.jsonl")
    connection = _initialize(root)
    imported = False
    try:
        with _transaction(connection):
            row = connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_root.name,)).fetchone()
            if row is not None:
                return False
            created = next((event for event in events if event.get("event") == "created"), {})
            name = str(created.get("name", job_root.name))
            connection.execute("INSERT INTO jobs(job_id, name, created_at) VALUES (?, ?, ?)",
                               (job_root.name, name, created.get("ts", _now())))
            catalog = _work_catalog(connection)
            _admit_records(connection, job_root.name, legacy_records, catalog)
            for seq, event in enumerate(events, start=1):
                connection.execute("INSERT INTO events(job_id, seq, event_json) VALUES (?, ?, ?)",
                                   (job_root.name, seq, _json(event)))
            imported = True
    finally:
        connection.close()
    if imported:
        _refresh_projection(root, job_root.name)
    return imported


def _import_legacy_locked(root: Path) -> int:
    if not root.is_dir():
        return 0
    known = set(job_ids(root))
    imported = 0
    for job_root in sorted(path for path in root.iterdir() if path.is_dir()):
        if job_root.name in known:
            continue
        if not ((job_root / "tracks.jsonl").is_file() or (job_root / "events.jsonl").is_file()):
            continue
        if _import_legacy_job(root, job_root):
            imported += 1
    return imported


_LEGACY_SCANNED: set[Path] = set()


def _import_legacy_once(root: Path) -> int:
    """Import legacy job directories once per process; later imports are explicit calls."""
    if root in _LEGACY_SCANNED:
        return 0
    imported = _import_legacy_locked(root)
    _LEGACY_SCANNED.add(root)
    return imported


def import_legacy(root: Path) -> int:
    """Import each legacy job in its own transaction and retain immutable source backups."""
    root = Path(root)
    if not root.is_dir():
        return 0
    with writer_lock(root):
        imported = _import_legacy_locked(root)
        _LEGACY_SCANNED.add(root)
        return imported


def job_exists(root: Path, job_id: str) -> bool:
    connection = _connect(Path(root))
    if connection is None:
        return False
    try:
        row = connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    return row is not None


def _ensure_job_locked(root: Path, job_id: str) -> None:
    job_root = Path(root) / job_id
    if not job_exists(root, job_id):
        has_projection = (job_root / "tracks.jsonl").is_file() or (job_root / "events.jsonl").is_file()
        if has_projection:
            _import_legacy_job(root, job_root)
    connection = _initialize(root)
    try:
        if connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
            return
        with _transaction(connection):
            connection.execute("INSERT INTO jobs(job_id, name, created_at) VALUES (?, ?, ?)",
                               (job_id, job_id, _now()))
    finally:
        connection.close()


def _job_records(connection: sqlite3.Connection, job_id: str) -> list[dict]:
    from .state import Record

    results: list[dict] = []
    rows = connection.execute(
        "SELECT m.record_json, w.state_json FROM memberships m "
        "JOIN work_items w ON w.work_id = m.work_id WHERE m.job_id = ? ORDER BY m.idx",
        (job_id,),
    )
    for row in rows:
        record = json.loads(row["record_json"])
        record.update(json.loads(row["state_json"]))
        results.append(Record.from_json(record).to_json())
    return results


def _job_events(connection: sqlite3.Connection, job_id: str) -> list[dict]:
    return [json.loads(row[0]) for row in connection.execute(
        "SELECT event_json FROM events WHERE job_id = ? ORDER BY seq", (job_id,))]


def _atomic_jsonl(path: Path, entries: Sequence[dict]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    content = "".join(_json(entry) + "\n" for entry in entries)
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _refresh_projection(root: Path, job_id: str) -> None:
    connection = _connect(root)
    if connection is None:
        return
    try:
        records = _job_records(connection, job_id)
        events = _job_events(connection, job_id)
    finally:
        connection.close()
    from .state import EVENTS, TRACKS

    job_root = Path(root) / job_id
    job_root.mkdir(parents=True, exist_ok=True)
    _atomic_jsonl(job_root / TRACKS, records)
    _atomic_jsonl(job_root / EVENTS, events)


def rebuild_projections(root: Path) -> None:
    """Atomically rebuild every external JSONL view from committed SQLite state."""
    root = Path(root)
    if not database_path(root).is_file():
        return
    with writer_lock(root):
        connection = _connect(root)
        if connection is None:
            return
        try:
            known_jobs = [row[0] for row in connection.execute("SELECT job_id FROM jobs ORDER BY job_id")]
        finally:
            connection.close()
        for job_id in known_jobs:
            _refresh_projection(root, job_id)


def admit_submission(root: Path, job_id: str, records: Sequence[object],
                     name: str | None = None) -> list[AdmissionMatch]:
    """Create or extend a submission and admit its ordered memberships atomically."""
    root = Path(root)
    with writer_lock(root):
        _import_legacy_once(root)
        connection = _initialize(root)
        try:
            with _transaction(connection):
                row = connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if row is None:
                    connection.execute("INSERT INTO jobs(job_id, name, created_at) VALUES (?, ?, ?)",
                                       (job_id, name or job_id, _now()))
                    created = {"ts": _now(), "event": "created", "name": name or job_id}
                    connection.execute("INSERT INTO events(job_id, seq, event_json) VALUES (?, 1, ?)",
                                       (job_id, _json(created)))
                catalog = _work_catalog(connection)
                matches = _admit_records(connection, job_id, records, catalog)
        finally:
            connection.close()
        _refresh_projection(root, job_id)
    return matches


def write_records(root: Path, job_id: str, records: Sequence[dict]) -> None:
    """Update membership facts and shared work state without deleting omitted memberships."""
    root = Path(root)
    with writer_lock(root):
        _import_legacy_once(root)
        _ensure_job_locked(root, job_id)
        connection = _initialize(root)
        try:
            with _transaction(connection):
                catalog = _work_catalog(connection)
                for record in records:
                    idx = record["idx"]
                    row = connection.execute(
                        "SELECT work_id FROM memberships WHERE job_id = ? AND idx = ?",
                        (job_id, idx),
                    ).fetchone()
                    if row is None:
                        _admit_records(connection, job_id, [record], catalog)
                        continue
                    work_id = row["work_id"]
                    connection.execute(
                        "UPDATE memberships SET record_json = ? WHERE job_id = ? AND idx = ?",
                        (_json(_local_state(record)), job_id, idx),
                    )
                    connection.execute("UPDATE work_items SET state_json = ? WHERE work_id = ?",
                                       (_json(_work_state(record)), work_id))
        finally:
            connection.close()
        _refresh_projection(root, job_id)


def append_event(root: Path, job_id: str, event: dict) -> None:
    root = Path(root)
    with writer_lock(root):
        _import_legacy_once(root)
        _ensure_job_locked(root, job_id)
        connection = _initialize(root)
        try:
            with _transaction(connection):
                seq = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
                connection.execute("INSERT INTO events(job_id, seq, event_json) VALUES (?, ?, ?)",
                                   (job_id, seq, _json(event)))
        finally:
            connection.close()
        _refresh_projection(root, job_id)


def job_records(root: Path, job_id: str) -> list[dict]:
    connection = _connect(Path(root))
    if connection is None:
        return []
    try:
        return _job_records(connection, job_id)
    finally:
        connection.close()


def job_events(root: Path, job_id: str) -> list[dict]:
    connection = _connect(Path(root))
    if connection is None:
        return []
    try:
        return _job_events(connection, job_id)
    finally:
        connection.close()


def job_ids(root: Path) -> list[str]:
    connection = _connect(Path(root))
    if connection is None:
        return []
    try:
        return [row[0] for row in connection.execute("SELECT job_id FROM jobs ORDER BY job_id")]
    finally:
        connection.close()


def work_ids(root: Path, job_id: str) -> dict[int, str]:
    """Every membership's canonical work, read in one query."""
    connection = _connect(Path(root))
    if connection is None:
        return {}
    try:
        return {row["idx"]: row["work_id"] for row in connection.execute(
            "SELECT idx, work_id FROM memberships WHERE job_id = ?", (job_id,))}
    finally:
        connection.close()


def work_id(root: Path, job_id: str, idx: int) -> str:
    connection = _connect(Path(root))
    if connection is None:
        raise KeyError((job_id, idx))
    try:
        row = connection.execute("SELECT work_id FROM memberships WHERE job_id = ? AND idx = ?",
                                 (job_id, idx)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise KeyError((job_id, idx))
    return row[0]


def delivery_status(root: Path, work_id: str, target: str, destination: str) -> str | None:
    connection = _connect(Path(root))
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT status FROM deliveries WHERE work_id = ? AND target = ? AND destination = ?",
            (work_id, target, destination),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def set_delivery(root: Path, work_id: str, target: str, destination: str,
                 status: str, error: str = "") -> None:
    if status not in DELIVERY_STATUSES:
        raise ValueError(f"unsupported delivery status: {status}")
    with writer_lock(root):
        connection = _initialize(Path(root))
        try:
            with _transaction(connection):
                connection.execute(
                    "INSERT INTO deliveries(work_id, target, destination, status, error, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(work_id, target, destination) DO UPDATE SET "
                    "status = excluded.status, error = excluded.error, updated_at = excluded.updated_at",
                    (work_id, target, destination, status, error, _now()),
                )
        finally:
            connection.close()
