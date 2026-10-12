"""The acquisition stage: a tracklist, through the reference, to files in staging.

Ticket 07 puts every deterministic step in the CLI, so the stage is a plain loop over a job's records:
resolve the reference, search, classify, take the ladder's pick, download it, and watch it until the file
is on disk or the candidate is hopeless. the user, 2026-10-12: `workers` of those ladders run at once, in
threads — nearly all of a track's time is spent waiting, and the server's ceiling on transfers, not our
loop, is what decides how many files move together.

Five things are worth reading the code for.

- **The disk decides, never the status.** The server drops a finished record sixty seconds after
  completion, so a later poll answers `not_found` about a download that in fact succeeded (the design notes).
  Every outcome here is settled by looking for the file.
- **A rotated download is cancelled.** The server transfers `max_downloads` files at once, so a stalled
  transfer left running does not merely waste bandwidth — it holds a slot the candidate we try next wants.
- **A peer that declared a queue is not a stall.** It gets a longer budget, and is not rotated on the
  45-second timer that a stalled connection earns (the design notes).
- **A transfer in flight is never rotated on that timer either.** Bytes are the signal: only a transfer
  that has stopped moving bytes for minutes is dead.
- **Searches are paced, and the pace is ours.** The network answers searches at a rate, nothing below us
  keeps to it, and nothing below us says when it has stopped answering at all — a throttled search and a
  rare track are the same empty payload (the design notes). So the pass spaces its own searches out and says what
  the pace is; the overlap the child allows is a consequence of that rate, never the target.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import reference, rules, slsk, state
from .config import Config
from .library import Held, Library
from .mcp import ServerError, StdioServer
from .parsing import search_query
from .reference import Lookup
from .rules import Candidate

# Seconds to let peers answer a search, and how many results to ask for. Ticket 03: results are sorted and
# truncated server-side, so a small cap discards good candidates before we ever see them.
SEARCH_TIMEOUT = 12         # the child sleeps exactly this long after the query goes out, so it is how long
                            # a peer has to answer at all. Seven had been taken as the child's own floor, and
                            # discarded peers answering at second ten; the design notes: with the pace below
                            # deciding how much searching happens at once, a generous window costs nothing.
CALL_TIMEOUT = 15.0         # the user, 2026-10-12: a minute waiting on one track is too long
MAX_RESULTS = 60

# Ticket 22: candidates attempted per track, per pass. Three was the, chosen when a pass was bound
# by its workers — so one track's bad luck could not be allowed to spend the pass. What a pass costs now is
# the searching (the pace), not the attempting, so an untried candidate is only a peer that may be
# gone by a later pass: 38 Ready ones were left that way in a single 2026-10-12 pass. Ten rather than
# unbounded, because the budgets below still bound a track's total time.
MAX_CANDIDATES = 10

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
    already_downloaded: list[int] = field(default_factory=list)
    already_held: list[int] = field(default_factory=list)
    in_review: list[int] = field(default_factory=list)
    nothing_usable: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [
            f"{len(self.downloaded)} downloaded",
            f"{len(self.in_review)} need a decision",
            f"{len(self.nothing_usable)} with nothing usable",
        ]
        if self.already_held:
            parts.append(f"{len(self.already_held)} already in the library")
        if self.already_downloaded:
            parts.append(f"{len(self.already_downloaded)} already downloaded")
        if self.failed:
            parts.append(f"{len(self.failed)} to retry")
        return " · ".join(parts)


class Pacer:
    """Let searches start no more often than `interval` seconds apart.

    Ticket 21: the network answers searches at a rate, and nothing between this loop and it keeps to that
    rate — the child fires whatever it is handed, and neither it nor aioslsk reports a search being ignored.
    So the pace is kept here. It is a gap between two searches rather than a cap on how many run at once,
    because the open searches are only a consequence: at five seconds a search, a twelve-second window
    leaves about two open, and the child's own ceiling never binds.
    """

    def __init__(self, interval: float, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._earliest: float | None = None      # when the next search may start

    def wait(self) -> None:
        """Hold this call back until its search may start."""
        with self._lock:
            now = self._clock()
            start = now if self._earliest is None else max(now, self._earliest)
            self._earliest = start + self._interval
            delay = start - now
        if delay > 0:
            self._sleep(delay)


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
    payload = slsk.check(server.call("search", arguments, timeout=SEARCH_TIMEOUT + CALL_TIMEOUT),
                         "search")
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
    payload = slsk.check(server.call("download", {"id": identifier(candidate)}, timeout=CALL_TIMEOUT),
                         "download")
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
                server.call("download_status", {"id": identifier(candidate)}, timeout=CALL_TIMEOUT),
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
    library: Library | None,
    log,
    sleep,
    clock,
    later_pass: bool,
    pacer: Pacer | None = None,
) -> str:
    """Acquire one track. Returns which of the summary's lists it belongs in."""
    if record.status == state.DOWNLOADED and record.local_path and Path(record.local_path).is_file():
        return "already_downloaded"

    if not record.candidates:
        # Ticket 13: resolved once, then written into the record — a resumed run never re-queries, and a
        # track that resolved nothing is review-only for good rather than re-asked every time.
        seconds = reference.resolve(record.request, lookup)
        record.duration_s = seconds
        job.append_event("reference", idx=record.idx, seconds=seconds)
        log(f"[{record.idx}] {record.label}: "
            + (f"reference {seconds}s" if seconds else "no MusicBrainz reference — review only"))

    # Ticket 09: nothing is fetched that the collection already holds — unless the copy it holds is lossy
    # and a lossless one can be had. That is what an upgrade is, and it is asked before the search so a
    # track already held costs no request either.
    upgrade: Held | None = None
    if library is not None:
        held = library.held(record.artist, record.title, record.duration_s)
        if held is None:
            # The length may be the only reason that missed, and for an upgrade there may be no
            # MusicBrainz length at all. So the name is asked on its own, and a lossy copy found that way
            # is the reference: the same track by definition, with a length measurable here and now.
            for row in library.rows_for(record.artist, record.title):
                if not row.lossless and row.seconds is not None:
                    held = row
                    break
        if held is not None and held.lossless:
            record.status = state.FOUND_IN_LIBRARY
            record.notes = f"already held at {held.path}" if held.path else "already held"
            job.append_event("already_held", idx=record.idx, path=held.path, genre=held.genre)
            log(f"[{record.idx}] {record.label}: already in the library"
                + (f" — {held.path}" if held.path else ""))
            return "already_held"
        if held is not None:
            # Held, but only as a lossy copy: that is an upgrade rather than a reason to skip (the design notes),
            # and the lane is what decides it — not whether the lookup happened to match.
            upgrade = held
            record.replaces = held.path
            if record.duration_s is None:
                record.duration_s = held.seconds
                job.append_event("reference", idx=record.idx, seconds=held.seconds,
                                 from_="the copy in the collection")
                log(f"[{record.idx}] {record.label}: reference {held.seconds}s, from the copy in the "
                    "collection — MusicBrainz has nothing for this one")
            else:
                log(f"[{record.idx}] {record.label}: held as a lossy copy — looking for a lossless one")

    if pacer is not None:
        pacer.wait()                            # the design notes: the network's rate, never the loop's
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
    if upgrade is not None:
        # Ticket 09: a lossless copy is strictly better than the lossy one held; a lossy copy is not better
        # than what the user already has, so only the better lane is worth fetching.
        queue = [candidate for candidate in queue if candidate.lossless]
        if not queue:
            record.status = state.PENDING
            job.append_event("no_upgrade_offered", idx=record.idx, replaces=upgrade.path)
            log(f"[{record.idx}] {record.label}: nothing lossless offered — the copy the user has stands")
            return "in_review"

    for candidate in queue[:MAX_CANDIDATES]:
        record.tried.append(identifier(candidate))
        decision, local_path = await_transfer(server, candidate, log=log, sleep=sleep, clock=clock)

        if decision is Move.DONE:
            record.local_path = local_path
            record.format = candidate.ext
            record.status = state.DOWNLOADED
            job.append_event("downloaded", idx=record.idx, path=local_path,
                             peer=candidate.user, size=candidate.size_bytes)
            if upgrade is not None:
                record.notes = f"upgrades {upgrade.path}"
                job.append_event("upgrade_downloaded", idx=record.idx, replaces=upgrade.path,
                                 path=local_path)
                log(f"[{record.idx}] {record.label}: a lossless copy from {candidate.user} → {local_path}"
                    f"  (replacing the lossy one at import)")
                return "downloaded"
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


def one_at_a_time(lookup: Lookup) -> Lookup:
    """The lookup, wrapped so it is asked once at a time.

    MusicBrainz's rate limiter is a module global inside `musicbrainzngs` and their policy asks for one
    request a second, so lookups queue here rather than racing each other inside a library that is not
    written for it.
    """
    lock = threading.Lock()

    class Serialised:
        def find(self, query: str, limit: int = reference.LIMIT) -> list[reference.Recording]:
            with lock:
                return lookup.find(query, limit)

    return Serialised()


def run(
    config: Config,
    job: state.Job,
    server: StdioServer,
    lookup: Lookup,
    *,
    library: Library | None = None,
    log=print,
    sleep=time.sleep,
    clock=time.monotonic,
    retry: bool = False,
) -> Summary:
    """The whole pass: every record the job holds, `workers` of them at a time, writing as it goes.

    the user, 2026-10-12: one track at a time is nonsense. A track spends nearly all of its time waiting —
    on MusicBrainz, on a search window, on a peer's queue — so the waiting overlaps instead of queueing,
    and the server's own ceiling on transfers is what decides how many files actually move at once.

    Ticket 21: the searching does not overlap freely, though. What is parallel here is the transfers and
    the waiting on them; searches go out at `config.search_interval`, because that rate is the network's
    and not this loop's.
    """
    records = job.records()
    summary = Summary()

    # Ticket 21: the network's rate on searching, and ours to keep. Zero is the loop's own speed, which is
    # worth having only for a tracklist too small for the rate to matter.
    pacer = (Pacer(config.search_interval, clock=clock, sleep=sleep)
             if config.search_interval > 0 else None)

    # One track's story stays readable while others are running, so lines are printed under a lock.
    printing = threading.Lock()

    def say(line: str) -> None:
        with printing:
            log(line)

    def work(record: state.Record) -> None:
        later_pass = retry or bool(record.candidates)
        try:
            outcome = one(job, record, server, lookup, library=library, log=say, sleep=sleep,
                          clock=clock, later_pass=later_pass, pacer=pacer)
        except (slsk.ToolError, ServerError) as error:
            record.status = state.RETRY_LATER
            outcome = "failed"
            say(f"[{record.idx}] {record.label}: the server refused — {error}")
            job.append_event("refused", idx=record.idx, error=str(error))
        getattr(summary, outcome).append(record.idx)
        job.write_records(records)          # a crash costs the tracks in flight and nothing else

    if records:
        lookup = one_at_a_time(lookup)
        if pacer is not None:
            say(f"one search every {config.search_interval:g}s"
                " — the network's rate, not this loop's (the design notes)")
        with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
            list(pool.map(work, records))

    # The lists fill in whatever order the tracks finish, so they are put back in track order to report.
    for name in ("downloaded", "already_downloaded", "already_held", "in_review",
                 "nothing_usable", "failed"):
        getattr(summary, name).sort()

    job.write_status(config)
    # Deliberately not finished: the pipeline ends at the import, so `acquire` and `process` leave the run
    # open and the next command picks the same one up without being told which (the design notes).
    return summary
