"""Where a person settles a track the rule would not.

This is the only writer of the decision axis (GLOSSARY.md): a candidate carries a *label*, a track carries a
*decision*. Accepting fetches the one candidate named and hands the record to `process`; skipping and
not-found write the decision alone. `acquire` reads the same axis, so a decided track is left alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import acquire, state
from .rules import Candidate, Verdict


class NotInTheQueue(RuntimeError):
    """No track with that index is waiting on anything."""


class NoSuchCandidate(RuntimeError):
    """That candidate is not one of the ones on offer for that track."""


class NotAccepted(RuntimeError):
    """The file was not handed over, so nothing was decided."""


@dataclass(frozen=True)
class Settled:
    """What a decision came to."""

    idx: int
    label: str
    decision: str
    candidate: Candidate | None = None
    path: str = ""
    from_disk: bool = False

    def report(self) -> str:
        if self.decision == state.ACCEPTED:
            where = "already in staging" if self.from_disk else "downloaded"
            return (f"[{self.idx}] {self.label}: accepted — {self.candidate.user}'s "
                    f"{self.candidate.ext} ({where}) → {self.path}")
        if self.decision == state.SKIPPED:
            return f"[{self.idx}] {self.label}: skipped — passed over for now"
        return f"[{self.idx}] {self.label}: not found — no usable copy exists"


def _record(records: list[state.Record], idx: int) -> state.Record:
    for record in records:
        if record.idx == idx:
            return record
    raise NotInTheQueue(f"track {idx} is not in this run")


def choose(record: state.Record, number: int | None = None) -> Verdict:
    """The candidate a person pointed at: `number` counts the ones on offer, from 1.

    Rejections have no number, so the list a person reads is the list they choose from.
    """
    offered = record.offered()
    if not offered:
        raise NoSuchCandidate(
            f"track {record.idx} has no candidate on offer to take"
            f" ({len(record.candidates)} were offered and every one was ruled out)"
        )
    if number is None:
        if len(offered) > 1:
            raise NoSuchCandidate(
                f"track {record.idx} has {len(offered)} candidates on offer — say which one"
            )
        return offered[0]
    if not 1 <= number <= len(offered):
        raise NoSuchCandidate(
            f"track {record.idx} has {len(offered)} candidates on offer, so {number} is not one of them"
        )
    return offered[number - 1]


def accept(
    job: state.Job,
    idx: int,
    *,
    number: int | None = None,
    server_factory=None,
    log=print,
    sleep=time.sleep,
    clock=time.monotonic,
) -> Settled:
    """Take the candidate a person picked, whatever the rule said about it.

    This is the one decision that needs the network: the file still has to be fetched. A record already
    pointing at a file is settled from the disk instead, so the server is opened only when it is needed —
    which is why it is a factory.
    """
    records = job.records()
    record = _record(records, idx)
    verdict = choose(record, number)
    candidate = verdict.candidate

    settled_from_disk = bool(record.local_path) and Path(record.local_path).is_file()
    if settled_from_disk:
        path = record.local_path
        log(f"[{record.idx}] {record.label}: the file is already in staging — not fetching it again")
    else:
        if server_factory is None:
            raise NotAccepted(f"track {record.idx} needs its file fetched, and no server was given")
        with server_factory() as server:
            decision, path = acquire.await_transfer(server, candidate, log=log, sleep=sleep, clock=clock)
        if decision is not acquire.Move.DONE:
            # The record is left exactly as it was: nothing was decided, and the candidate is marked tried
            # so the next pass moves on rather than offering the same peer again.
            record.tried.append(acquire.identifier(candidate))
            job.write_records(records)
            raise NotAccepted(
                f"track {record.idx}: {candidate.user} did not hand the file over ({decision.value})"
            )

    record.tried.append(acquire.identifier(candidate))
    acquire.record_taken(job, record, candidate, path, accepted=True)
    job.append_event("accepted", idx=idx, candidate=acquire.identifier(candidate), path=path,
                     from_="the file already in staging" if settled_from_disk else candidate.user)
    job.write_records(records)
    return Settled(idx=idx, label=record.label, decision=state.ACCEPTED, candidate=candidate,
                   path=path, from_disk=settled_from_disk)


def skip(job: state.Job, idx: int) -> Settled:
    """Pass over a track for now. Nothing about it changes but the decision."""
    records = job.records()
    record = _record(records, idx)
    record.decision = state.SKIPPED
    job.append_event("skipped", idx=idx)
    job.write_records(records)
    return Settled(idx=idx, label=record.label, decision=state.SKIPPED)


def not_found(job: state.Job, idx: int) -> Settled:
    """Say no usable copy exists. The track stops being asked about at all."""
    records = job.records()
    record = _record(records, idx)
    record.decision = state.DECLINED
    record.status = state.NOT_FOUND
    record.notes = "no usable copy exists"
    job.append_event("declined", idx=idx)
    job.write_records(records)
    return Settled(idx=idx, label=record.label, decision=state.DECLINED)
