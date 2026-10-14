"""The job: a directory per run, holding the canonical per-track record.

`.state/jobs/<job-id>/` holds `tracks.jsonl` (one JSON object per track, rewritten whole — it stays small),
`events.jsonl` and `current_status.md`. Verdicts are never stored: they are computed from the candidate
facts by `rules`, so a job replayed tomorrow is judged by the rule as it stands then.
"""

from __future__ import annotations

import json
import re
import threading
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
        # A pass works on several tracks at once, so the two files a run owns are written under one lock
        # rather than trusting to luck.
        self._lock = threading.Lock()

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
        if not self.tracks_path.is_file():
            return []
        found = []
        for line in self.tracks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                found.append(Record.from_json(json.loads(line)))
        return found

    def write_records(self, records: list[Record]) -> None:
        """Rewrite the whole file — it stays small enough that a partial write is not worth the risk."""
        lines = [json.dumps(record.to_json(), ensure_ascii=False) for record in records]
        with self._lock:
            self.tracks_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def append_event(self, event: str, **fields) -> None:
        entry = {"ts": _now(), "event": event}
        entry.update(fields)
        with self._lock, self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        if not self.events_path.is_file():
            return []
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

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


def create(config: Config, name: str) -> Job:
    """Start a run. The id leads with a UTC stamp, so the newest run is the last one sorted."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "run"
    job = Job(config.state_root / f"{stamp}-{slug}")
    job.root.mkdir(parents=True, exist_ok=False)
    job.tracks_path.touch()
    job.events_path.touch()
    job.append_event("created", name=name)
    return job


def all_jobs(config: Config) -> list[Job]:
    if not config.state_root.is_dir():
        return []
    return [Job(path) for path in sorted(config.state_root.iterdir()) if path.is_dir()]


def newest_unfinished(config: Config) -> Job | None:
    """The resume target: the most recent run that never wrote its finish event."""
    for job in reversed(all_jobs(config)):
        if not job.finished():
            return job
    return None


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
