"""The acquisition stage: a tracklist, through the reference, to files in staging.

Ticket 07 puts every deterministic step in the CLI, so the stage is a plain loop over a job's records:
resolve the reference, search, classify, take the ladder's pick, download it, and watch it until the file
is on disk or the candidate is hopeless.

Four things are worth reading the code for.

- **The disk decides, never the status.** The server drops a finished record sixty seconds after
  completion, so a later poll answers `not_found` about a download that in fact succeeded (the design notes).
  Every outcome here is settled by looking for the file.
- **A rotated download is cancelled.** The server transfers at most two files at once, so a stalled
  transfer left running does not merely waste bandwidth — it blocks the candidate we try next.
- **A peer that declared a queue is not a stall.** It gets a longer budget, and is not rotated on the
  45-second timer that a stalled connection earns (the design notes).
- **A transfer in flight is never rotated on that timer either.** Bytes are the signal: only a transfer
  that has stopped moving bytes for minutes is dead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import reference, rules, slsk, state
from .config import Config
from .mcp import ServerError, StdioServer
from .parsing import search_query
from .reference import Lookup
from .rules import Candidate

# Seconds to let peers answer a search, and how many results to ask for. Ticket 03: results are sorted and
# truncated server-side, so a small cap discards good candidates before we ever see them.
SEARCH_TIMEOUT = 12
MAX_RESULTS = 60

# Ticket 04: candidates attempted per track, per pass.
MAX_CANDIDATES = 3

# Ticket 04: a connection that never transferred a byte is rotated after 45 seconds.
CONNECT_BUDGET = 45.0

# Not fixed by the map. A peer that said it had a queue is behaving normally rather than failing, so it
# gets a budget long enough to be worth waiting for; and a transfer that has stopped moving bytes for five
# minutes is dead whatever the server says about it.
QUEUE_BUDGET = 300.0
STALL_BUDGET = 300.0

# A finished transfer whose file has not appeared is the `.part` rename, which is quick. Past this, the
# file is not coming.
FINISHED_GRACE = 15.0

# Statuses after which the server will never change its mind.
_OVER = frozenset({"failed", "cancelled", "not_found", "session_expired"})


class Move(Enum):
    """What to do with the candidate the server is holding."""

    WAIT = "wait"
    ROTATE = "rotate"       # give up on this candidate; another may be better
    DONE = "done"           # the file is on disk
    FAILED = "failed"       # this candidate will never produce a file


@dataclass(frozen=True)
class Transfer:
    """What the server last said about a download."""

    status: str = ""
    connection_state: str = ""
    progress_pct: float = 0.0
    received_bytes: int = 0
    local_path: str = ""
    message: str = ""
    wait_before_poll: float = 30.0


def move(transfer: Transfer, *, on_disk: bool, stuck_for: float, declared_queue: int = 0) -> Move:
    """What to do next, given how long the transfer has gone without moving a byte.

    `stuck_for` is seconds since `received_bytes` last increased — before anything transfers, that is
    simply how long the download has been running.
    """
    if on_disk:
        return Move.DONE
    if transfer.status == "finished":
        # The rename may not have landed yet; the next poll settles it either way.
        return Move.WAIT if stuck_for <= FINISHED_GRACE else Move.FAILED
    if transfer.status in _OVER:
        return Move.FAILED
    if transfer.status == "downloading" or transfer.connection_state == "transferring":
        return Move.ROTATE if stuck_for > STALL_BUDGET else Move.WAIT
    budget = QUEUE_BUDGET if declared_queue else CONNECT_BUDGET
    return Move.ROTATE if stuck_for > budget else Move.WAIT


def transfer_of(payload: dict) -> Transfer:
    """The server's answer to `download` or `download_status`, as far as the decision needs it."""
    return Transfer(
        status=str(payload.get("status") or ""),
        connection_state=str(payload.get("connection_state") or ""),
        progress_pct=float(payload.get("progress_pct") or 0.0),
        received_bytes=int(payload.get("received_bytes") or 0),
        local_path=str(payload.get("local_path") or ""),
        message=str(payload.get("message") or ""),
        wait_before_poll=float(payload.get("wait_before_poll_secs") or 30.0),
    )


def candidate_of(item: dict) -> Candidate | None:
    """One search result, or None when it names no file we could ask for.

    For a lossless file the server reports its quality score in `bitrate` rather than a kbps rate. That is
    harmless: the quality floor only reads the field for lossy files, and the ladder reads it as a quality
    tiebreak, which is what it is.
    """
    user = str(item.get("username") or "")
    path = str(item.get("filename") or "")
    if not user or not path:
        return None
    duration = item.get("duration_sec")
    return Candidate(
        user=user,
        path=path,
        duration=int(duration) if duration is not None else None,
        bitrate=item.get("bitrate"),
        queue=int(item.get("queue_size") or 0),
        size_bytes=item.get("filesize"),
    )


def identifier(candidate: Candidate) -> str:
    """The id the server wants back. Ticket 03: it is `{username}:{filename}`, nothing more."""
    return f"{candidate.user}:{candidate.path}"


@dataclass
class Summary:
    """What a pass came to, by track."""

    downloaded: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    in_review: list[int] = field(default_factory=list)
    nothing_usable: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [
            f"{len(self.downloaded)} downloaded",
            f"{len(self.in_review)} need a decision",
            f"{len(self.nothing_usable)} with nothing usable",
        ]
        if self.already:
            parts.append(f"{len(self.already)} already held")
        if self.failed:
            parts.append(f"{len(self.failed)} to retry")
        return " · ".join(parts)


def search(server: StdioServer, record: state.Record, *, later_pass: bool) -> list[Candidate]:
    """Ask the server, and read back what it offered.

    Ticket 04: peer attributes may scope a retry pass and never the first one, because a filter can
    eliminate the only peer who has the track.
    """
    arguments: dict = {
        "query": search_query(record.artist, record.title),
        "timeout": SEARCH_TIMEOUT,
        "max_results": MAX_RESULTS,
    }
    if later_pass:
        arguments["free_slots_only"] = True
    payload = slsk.check(server.call("search", arguments, timeout=SEARCH_TIMEOUT + 30), "search")
    offered = payload.get("results") or []
    return [candidate for candidate in (candidate_of(item) for item in offered) if candidate is not None]


def merge(record: state.Record, found: list[Candidate]) -> int:
    """Add what the search found to what the record already holds, without repeating anything."""
    known = {identifier(candidate) for candidate in record.candidates}
    added = [candidate for candidate in found if identifier(candidate) not in known]
    record.candidates.extend(added)
    return len(added)


def await_transfer(
    server: StdioServer,
    candidate: Candidate,
    *,
    log,
    sleep,
    clock,
) -> tuple[Move, str]:
    """Take one candidate as far as it goes. Returns how it ended and where the file is."""
    payload = slsk.check(server.call("download", {"id": identifier(candidate)}, timeout=60), "download")
    report = transfer_of(payload)
    local_path = report.local_path
    log(f"      asking {candidate.user} for {candidate.name}")

    last_bytes = 0
    last_change = clock()
    while True:
        on_disk = bool(local_path) and Path(local_path).is_file()
        decision = move(
            report, on_disk=on_disk, stuck_for=clock() - last_change, declared_queue=candidate.queue
        )
        if decision is not Move.WAIT:
            return decision, local_path

        sleep(report.wait_before_poll)
        report = transfer_of(
            slsk.check(
                server.call("download_status", {"id": identifier(candidate)}, timeout=60),
                "download_status",
            )
        )
        local_path = report.local_path or local_path
        if report.received_bytes > last_bytes:
            last_bytes = report.received_bytes
            last_change = clock()


def give_up(server: StdioServer, candidate: Candidate, log) -> None:
    """Stop the transfer, because the next candidate cannot start while the slots are full."""
    try:
        server.call("cancel_download", {"id": identifier(candidate)}, timeout=30)
    except Exception as error:                  # a courtesy, never a reason to fail the track
        log(f"      could not cancel {candidate.user}: {error}")


def one(
    job: state.Job,
    record: state.Record,
    server: StdioServer,
    lookup: Lookup,
    *,
    log,
    sleep,
    clock,
    later_pass: bool,
) -> str:
    """Acquire one track. Returns which of the summary's lists it belongs in."""
    if record.status == state.DOWNLOADED and record.local_path and Path(record.local_path).is_file():
        return "already"

    if not record.candidates:
        # Ticket 13: resolved once, then written into the record — a resumed run never re-queries, and a
        # track that resolved nothing is review-only for good rather than re-asked every time.
        seconds = reference.resolve(record.request, lookup)
        record.duration_s = seconds
        job.append_event("reference", idx=record.idx, seconds=seconds)
        log(f"[{record.idx}] {record.label}: "
            + (f"reference {seconds}s" if seconds else "no MusicBrainz reference — review only"))

    added = merge(record, search(server, record, later_pass=later_pass))
    job.append_event("searched", idx=record.idx, added=added, offered=len(record.candidates))

    if record.settled() is None:
        record.status = state.PENDING
        outcome = "in_review" if record.usable() else "nothing_usable"
        log(f"[{record.idx}] {record.label}: {len(record.offered())} offered, "
            "nothing to take on its own")
        return outcome

    tried = set(record.tried)
    queue = sorted(
        (verdict.candidate for verdict in rules.ready(record.track)
         if identifier(verdict.candidate) not in tried),
        key=rules.rank,
    )

    for candidate in queue[:MAX_CANDIDATES]:
        record.tried.append(identifier(candidate))
        decision, local_path = await_transfer(server, candidate, log=log, sleep=sleep, clock=clock)

        if decision is Move.DONE:
            record.local_path = local_path
            record.format = candidate.ext
            record.status = state.DOWNLOADED
            job.append_event("downloaded", idx=record.idx, path=local_path,
                             peer=candidate.user, size=candidate.size_bytes)
            log(f"[{record.idx}] {record.label}: from {candidate.user} → {local_path}")
            return "downloaded"

        if decision is Move.ROTATE:
            give_up(server, candidate, log)
            log(f"      {candidate.user} went nowhere; rotating ({len(record.tried)} tried)")
        else:
            log(f"      {candidate.user} failed")
        job.append_event("candidate_lost", idx=record.idx, peer=candidate.user,
                         path=candidate.path, why=decision.value)

    record.status = state.RETRY_LATER
    job.append_event("exhausted", idx=record.idx, tried=len(record.tried))
    log(f"[{record.idx}] {record.label}: every candidate failed — left to retry")
    return "failed"


def run(
    config: Config,
    job: state.Job,
    server: StdioServer,
    lookup: Lookup,
    *,
    log=print,
    sleep=time.sleep,
    clock=time.monotonic,
    retry: bool = False,
) -> Summary:
    """The whole pass: every record the job holds, in order, writing as it goes."""
    records = job.records()
    summary = Summary()

    for record in records:
        later_pass = retry or bool(record.candidates)
        try:
            outcome = one(job, record, server, lookup, log=log, sleep=sleep, clock=clock,
                          later_pass=later_pass)
        except (slsk.ToolError, ServerError) as error:
            record.status = state.RETRY_LATER
            outcome = "failed"
            log(f"[{record.idx}] {record.label}: the server refused — {error}")
            job.append_event("refused", idx=record.idx, error=str(error))
        getattr(summary, outcome).append(record.idx)
        job.write_records(records)          # a crash costs the current track and nothing else

    job.write_status(config)
    if not summary.failed:
        job.finish()
    return summary
