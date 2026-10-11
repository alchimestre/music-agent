"""The job: a directory per run, holding the canonical per-track record.

Ticket 07 lays out `.state/jobs/<job-id>/` with `tracks.jsonl`, `events.jsonl` and `current_status.md`.
One JSON object per track: JSONL because music metadata is full of commas and quotes, and the file stays
small enough to rewrite whole. Alongside the pipeline's own fields the record carries the two axes this
project needs: what the user *decided* about a track, and which candidates were offered for it.

The shape was lifted from the older hand-run acquisition job. Nothing reads it but us, so it is ours to
change.

Verdicts are never stored. They are computed from the candidate facts by `rules`, so a job replayed
tomorrow reaches the verdicts the rule reaches today, and a rule correction reaches every old job.
"""

from __future__ import annotations

import json
import re
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
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
CONVERTED = "converted"
IMPORTED = "imported"
RETRY_LATER = "retry_later"
NOT_FOUND = "not_found"
FAILED = "failed"

# What the user decided — GLOSSARY.md's axis, which is not the same thing as where the pipeline got to.
UNDECIDED = "undecided"
ACCEPTED = "accepted"
SKIPPED = "skipped"
DECLINED = "not found"

FINISHED_EVENT = "finished"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    notes: str = ""
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def request(self) -> Request:
        return Request(self.artist, self.title, self.mix, self.duration_s)

    @property
    def track(self) -> Track:
        return Track(self.request, list(self.candidates))

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

    def to_json(self) -> dict:
        data = asdict(self)
        data["candidates"] = [asdict(c) for c in self.candidates]
        return data

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
        if not self.tracks_path.is_file():
            return []
        found = []
        for line in self.tracks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                found.append(Record.from_json(json.loads(line)))
        return found

    def write_records(self, records: list[Record]) -> None:
        """Rewrite the whole file, which the working procedure's own guidance does: it stays small."""
        lines = [json.dumps(record.to_json(), ensure_ascii=False) for record in records]
        self.tracks_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def append_event(self, event: str, **fields) -> None:
        entry = {"ts": _now(), "event": event}
        entry.update(fields)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        if not self.events_path.is_file():
            return []
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def finished(self) -> bool:
        """A run is done when it says so in its log — nothing else writes a flag."""
        return any(entry.get("event") == FINISHED_EVENT for entry in self.events())

    def finish(self) -> None:
        self.append_event(FINISHED_EVENT)

    def counts(self) -> dict[str, int]:
        counted: dict[str, int] = {}
        for record in self.records():
            counted[record.status] = counted.get(record.status, 0) + 1
        return counted

    def summary(self) -> dict[str, int]:
        """The three things the review screen is a view over, plus the total."""
        records = self.records()
        return {
            "tracks": len(records),
            "in_review": sum(1 for r in records if r.needs_you()),
            "settled": sum(1 for r in records if r.settled() is not None),
            "nothing_usable": sum(1 for r in records if not r.usable()),
        }

    def write_status(self, config: Config) -> None:
        """The entry point for whoever — or whatever — picks this run up next."""
        counts = self.counts()
        summary = self.summary()
        lines = [
            f"# Job: {self.id}",
            "",
            f"State root: {self.root}",
            f"Staging: {config.staging}",
            f"Lossless: {config.lossless}",
            f"Lossy: {config.lossy}",
            f"Finished: {'yes' if self.finished() else 'no'}",
            "",
            "## Counts",
            "",
            f"Tracks total: {summary['tracks']}",
            f"In review: {summary['in_review']}",
            f"Settled: {summary['settled']}",
            f"Nothing usable: {summary['nothing_usable']}",
            "",
            "## By pipeline status",
            "",
        ]
        lines += [f"{name}: {count}" for name, count in sorted(counts.items())] or ["nothing yet"]
        self.status_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def find(config: Config, job_id: str) -> Job | None:
    for job in all_jobs(config):
        if job.id == job_id:
            return job
    return None
