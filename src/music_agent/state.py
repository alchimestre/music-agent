"""The job: one submission over the canonical SQLite state, with derived JSONL files for readers.

`state.sqlite3` holds jobs, ordered memberships, shared work, events and deliveries. Each job directory keeps
`tracks.jsonl` and `events.jsonl` as atomic projections for external consumers, and `current_status.md` as a
human report. Verdicts are never stored: they are computed from the candidate facts by `rules`, so a job
replayed tomorrow is judged by the rule as it stands then.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .rules import Candidate, Request, Track, Verdict, in_review, nothing_usable, pick, visible

TRACKS = "tracks.jsonl"
EVENTS = "events.jsonl"
STATUS = "current_status.md"

# Pipeline position: where the pipeline got to, which is not the same thing as what the user decided —
# see the decision values below.
PENDING = "pending"
FOUND_IN_LIBRARY = "found_in_library"
DOWNLOADED = "downloaded"
CONVERTED = "converted"
NORMALIZED = "normalized"
TAGGED = "tagged"
IMPORTED = "imported"
RETRY_LATER = "retry_later"
NOT_FOUND = "not_found"
FAILED = "failed"

# What the user decided — GLOSSARY.md's axis, which is not the same thing as where the pipeline got to.
UNDECIDED = "undecided"
ACCEPTED = "accepted"
SKIPPED = "skipped"
DECLINED = "not found"
DECIDED = frozenset({ACCEPTED, SKIPPED, DECLINED})

# A record the pipeline has not settled: it is either waiting on the network or waiting on a person. Every
# other status means a stage has the track in hand, so a queue never asks about it.
LIVE = frozenset({PENDING, RETRY_LATER})

SEARCHED_EVENT = "searched"

FINISHED_EVENT = "finished"

# A track that was identified, offered names, and matched none of them. Counted from the event
# logs rather than a new store, because "enough tracks" means across runs and not across one tracklist.
UNGENERED_EVENT = "ungenred"


def ungenred_names(jobs: Iterable[Job]) -> dict[str, int]:
    """How many tracks carried each name that resolved to nothing."""
    counted: dict[str, int] = {}
    for job in jobs:
        for entry in job.events():
            if entry.get("event") != UNGENERED_EVENT:
                continue
            for name in entry.get("names", []):
                if name:
                    counted[name] = counted.get(name, 0) + 1
    return counted


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# The three facts a run reads off the two files it owns. Each takes the snapshot it is given rather than
# reaching for the file itself, so a report that needs two of them reads the file once.
def _counted(records: list[Record]) -> dict[str, int]:
    counted: dict[str, int] = {}
    for record in records:
        counted[record.status] = counted.get(record.status, 0) + 1
    return counted


def _searched_indices(events: list[dict]) -> set[int]:
    return {entry["idx"] for entry in events
            if entry.get("event") == SEARCHED_EVENT and entry.get("idx") is not None}


def _finished(events: list[dict]) -> bool:
    return any(entry.get("event") == FINISHED_EVENT for entry in events)


@dataclass
class Record:
    """One track: what was asked for, where the pipeline got to, what was decided, what was offered."""

    idx: int
    artist: str
    title: str
    mix: str = ""
    duration_s: int | None = None
    status: str = PENDING
    decision: str = UNDECIDED
    local_path: str = ""
    format: str = ""
    genre: str = ""
    # An upgrade: the path of the file in the collection this one is to replace. Empty for a
    # track that is simply new.
    replaces: str = ""
    notes: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    # The candidates we have already asked a peer for, as `user:path`. Recorded so a retry pass moves on
    # to the next one rather than asking the same peer the same question.
    tried: list[str] = field(default_factory=list)
    # The tried candidates the peer itself failed to deliver, as `user:path`: a rotate we chose is not one.
    refused: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        title = f"{self.artist} - {self.title}"
        return f"{title} ({self.mix})" if self.mix else title

    @property
    def request(self) -> Request:
        return Request(self.artist, self.title, self.mix, self.duration_s, self.replaces)

    @property
    def track(self) -> Track:
        return Track(self.request, self.candidates)

    def offered(self) -> list[Verdict]:
        """The candidates worth showing — rejections are never shown."""
        return visible(self.track)

    def settled(self) -> Verdict | None:
        """What the system would take on its own, or None when nothing is Ready."""
        return pick(self.track)

    def needs_you(self) -> bool:
        return in_review(self.track)

    def usable(self) -> bool:
        return not nothing_usable(self.track)

    def decided(self) -> bool:
        """Whether a person has settled this track — the axis `review` writes and every pass reads."""
        return self.decision in DECIDED

    def to_json(self) -> dict:
        # `asdict` already recurses into the candidate dataclasses, so serializing them again here would
        # deep-copy every candidate a second time for the same result.
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Record:
        known = set(cls.__dataclass_fields__) - {"candidates"}
        candidate_fields = set(Candidate.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known},
                   candidates=[Candidate(**{k: v for k, v in c.items() if k in candidate_fields})
                               for c in data.get("candidates", [])])


class Job:
    """A run. Its records are the only source of truth about it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def tracks_path(self) -> Path:
        return self.root / TRACKS

    @property
    def events_path(self) -> Path:
        return self.root / EVENTS

    @property
    def status_path(self) -> Path:
        return self.root / STATUS

    def records(self) -> list[Record]:
        from . import store

        if store.job_exists(self.root.parent, self.id):
            return [Record.from_json(data) for data in store.job_records(self.root.parent, self.id)]
        return [Record.from_json(data) for data in _jsonl(self.tracks_path)]

    def write_records(self, records: list[Record]) -> None:
        from . import store

        store.write_records(self.root.parent, self.id, [record.to_json() for record in records])

    def append_event(self, event: str, **fields) -> None:
        from . import store

        entry = {"ts": _now(), "event": event}
        entry.update(fields)
        store.append_event(self.root.parent, self.id, entry)

    def events(self) -> list[dict]:
        from . import store

        if store.job_exists(self.root.parent, self.id):
            return store.job_events(self.root.parent, self.id)
        return _jsonl(self.events_path)

    def work_id(self, idx: int) -> str:
        """The immutable canonical work this membership points at."""
        from . import store

        return store.work_id(self.root.parent, self.id, idx)

    def execution(self) -> ExecutionJob:
        """The view a pass schedules: one representative record per shared work item."""
        return ExecutionJob(self)

    def searched(self) -> set[int]:
        """The tracks a pass has actually asked the network about.

        A search that answered with nothing and a search that never happened both leave no candidates, so
        which one it was is read from the log rather than from the record.
        """
        return _searched_indices(self.events())

    def finished(self) -> bool:
        """A run is done when it says so in its log — nothing else writes a flag."""
        return _finished(self.events())

    def finish(self) -> None:
        self.append_event(FINISHED_EVENT)

    def counts(self) -> dict[str, int]:
        return _counted(self.records())

    def summary(self) -> dict[str, int]:
        """What the run adds up to, with the queue split by why a track is still in it."""
        return _summary_of(self.records(), _searched_indices(self.events()))

    def write_status(self, config: Config) -> None:
        # One read of each file: the queue, the settled count and `finished` all come off these snapshots.
        events = self.events()
        records = self.records()
        counts = _counted(records)
        summary = _summary_of(records, _searched_indices(events))
        lines = [
            f"# Job: {self.id}",
            "",
            f"State root: {self.root}",
            f"Staging: {config.staging}",
            f"Lossless: {config.lossless}",
            f"Lossy: {config.lossy}",
            f"Finished: {'yes' if _finished(events) else 'no'}",
            "",
            "## Counts",
            "",
            f"Tracks total: {summary['tracks']}",
            f"In review: {summary['in_review']}",
            f"Decided by hand: {summary['decided']}",
            f"Settled: {summary['settled']}",
            f"Nothing usable: {summary['nothing_usable']}",
            f"Not searched: {summary['not_searched']}",
            f"The network offered nothing: {summary['nothing_offered']}",
            f"Every offer refused: {summary['refused']}",
            "",
            "## By pipeline status",
            "",
        ]
        lines += [f"{name}: {count}" for name, count in sorted(counts.items())] or ["nothing yet"]
        self.status_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pass_order(record: Record) -> tuple[int, int]:
    """Prefer a membership still worth working on over one a person passed over."""
    return (0 if record.decision in (UNDECIDED, ACCEPTED) else 1, record.idx)


class ExecutionJob:
    """A job as a pass sees it: one record per shared work item, writes folded back onto every membership.

    A submission may hold the same work twice. A pass must not fetch it twice, so it schedules the
    representative; the other memberships stay in the job and read the shared work state written here.
    """

    def __init__(self, job: Job) -> None:
        self.base = job

    def __getattr__(self, name: str):
        return getattr(self.base, name)

    def records(self) -> list[Record]:
        from . import store

        mapping = store.work_ids(self.base.root.parent, self.base.id)
        chosen: dict[object, Record] = {}
        for record in self.base.records():
            work_id = mapping.get(record.idx, record.idx)
            current = chosen.get(work_id)
            if current is None or _pass_order(record) < _pass_order(current):
                chosen[work_id] = record
        return sorted(chosen.values(), key=lambda record: record.idx)

    def write_records(self, records: list[Record]) -> None:
        """Write the pass's representatives, then settle still-open duplicates of the same work."""
        from . import store

        self.base.write_records(records)
        mapping = store.work_ids(self.base.root.parent, self.base.id)
        decisions = {mapping.get(record.idx, record.idx): record.decision
                     for record in records if record.decided()}
        if not decisions:
            return
        siblings = [record for record in self.base.records()
                    if not record.decided() and mapping.get(record.idx, record.idx) in decisions]
        if not siblings:
            return
        for record in siblings:
            record.decision = decisions[mapping.get(record.idx, record.idx)]
        self.base.write_records(siblings)


@dataclass
class Queue:
    """The tracks a run is not done with, split by why (GLOSSARY.md: the review queue).

    `needs_you` is the review queue proper. The other three are tracks it cannot offer a candidate for, and
    they are kept apart because they are three different facts: the pass never asked, the network answered
    with nothing, or every offer was ruled out.
    """

    needs_you: list[Record] = field(default_factory=list)
    decided: list[Record] = field(default_factory=list)
    not_searched: list[Record] = field(default_factory=list)
    nothing_offered: list[Record] = field(default_factory=list)
    refused: list[Record] = field(default_factory=list)

    @property
    def undecided(self) -> list[Record]:
        """Everything in the queue a person has not settled."""
        return self.needs_you + self.not_searched + self.nothing_offered + self.refused


def _queue_of(records: list[Record], searched: set[int]) -> Queue:
    waiting = Queue()
    for record in records:
        if record.decided():
            waiting.decided.append(record)
            continue
        if record.status not in LIVE:
            continue                        # a stage has the track in hand; nothing asks about it
        track = record.track                # one snapshot, so the rule reads the candidates once
        if pick(track) is not None:
            continue                        # the pass can settle this one itself; it owes the network
        if in_review(track):
            waiting.needs_you.append(record)
        elif record.candidates:
            waiting.refused.append(record)  # files were offered, and every one was ruled out
        elif record.idx in searched:
            waiting.nothing_offered.append(record)   # a search answered and held no files
        else:
            waiting.not_searched.append(record)      # no search ever reached this track
    return waiting


def _summary_of(records: list[Record], searched: set[int]) -> dict[str, int]:
    """What a run's records add up to, split by why a live track is still in the queue."""
    waiting = _queue_of(records, searched)
    return {
        "tracks": len(records),
        "in_review": len(waiting.needs_you),
        "settled": sum(1 for record in records if pick(record.track) is not None),
        "decided": len(waiting.decided),
        "not_searched": len(waiting.not_searched),
        "nothing_offered": len(waiting.nothing_offered),
        "refused": len(waiting.refused),
        "nothing_usable": (len(waiting.not_searched) + len(waiting.nothing_offered)
                           + len(waiting.refused)),
    }


def queue(job: Job) -> Queue:
    """Where each of a run's live tracks belongs."""
    return _queue_of(job.records(), job.searched())


def create(config: Config, name: str, records: list[Record] | None = None) -> Job:
    """Start a run. The id leads with a UTC stamp, so the newest run is the last one sorted."""
    from . import store

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "run"
    with store.writer_lock(config.state_root):
        job = _new_job(config.state_root, f"{stamp}-{slug}")
        try:
            store.admit_submission(config.state_root, job.id, list(records or []), name=name)
        except BaseException:
            try:
                job.root.rmdir()     # only if admission left it empty; a committed job keeps its files
            except OSError:
                pass
            raise
    return job


def _new_job(state_root: Path, job_id: str) -> Job:
    """A run id nothing holds: enqueueing twice in the same second adds a suffix instead of failing."""
    from . import store

    candidate = job_id
    suffix = 2
    while (state_root / candidate).exists() or store.job_exists(state_root, candidate):
        candidate = f"{job_id}-{suffix}"
        suffix += 1
    job = Job(state_root / candidate)
    job.root.mkdir(parents=True, exist_ok=False)
    return job


def all_jobs(config: Config) -> list[Job]:
    from . import store

    if not config.state_root.is_dir():
        return []
    ids = {path.name for path in config.state_root.iterdir() if path.is_dir()}
    ids.update(store.job_ids(config.state_root))
    return [Job(config.state_root / job_id) for job_id in sorted(ids)]


def newest_unfinished(config: Config) -> Job | None:
    """The resume target: the most recent run that never wrote its finish event.

    Only the newest run can be it. A run older than a finished one was abandoned — its process was killed,
    and nothing will ever write that event — so reaching back past a finished run hands every command
    without a `--job` a run that stopped days ago.
    """
    job = newest(config)
    return None if job is None or job.finished() else job


def newest(config: Config) -> Job | None:
    """The most recent run there is, finished or not.

    The import is what finishes a run, and it finishes nothing else: the queue can still hold tracks waiting
    on a decision, the genre list can still hold tracks waiting on a person, and a lane can still hold
    leftovers. So a command that works on any of those has to be able to reach a finished run.
    """
    jobs = all_jobs(config)
    return jobs[-1] if jobs else None


def find(config: Config, job_id: str) -> Job | None:
    for job in all_jobs(config):
        if job.id == job_id:
            return job
    return None
