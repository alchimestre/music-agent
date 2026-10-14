"""The acquisition stage: a tracklist, through the reference, to files in staging.

Every outcome is settled by looking for the file, never by the status: the server drops a finished record
sixty seconds after completion, so a later poll answers `not_found` about a download that did succeed.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from . import progress, reference, rules, slsk, state
from .config import Config
from .library import Held, Library
from .mcp import ServerError, StdioServer
from .parsing import mix_matches, search_query
from .reference import Lookup
from .rules import Candidate

# The child sleeps exactly this long after a query goes out, so this is how long a peer has to answer at
# all. The server sorts and truncates results, so a small cap discards good candidates before we see them.
SEARCH_TIMEOUT = 12
CALL_TIMEOUT = 15.0
MAX_RESULTS = 60

# Candidates attempted per track, per pass.
MAX_CANDIDATES = 10

# A connection that never transferred a byte is rotated after this long. A peer either opens the connection
# quickly or it is not going to.
CONNECT_BUDGET = 30.0

# A peer that declared a queue is behaving normally rather than failing, so it gets a budget worth waiting
# for; a transfer that has started and stopped moving bytes is dead whatever the server says. The pass waits
# out the last candidate of a track while everything else is done, so these are what the tail of a pass is
# made of: five minutes each across ten candidates is fifty minutes for one track.
QUEUE_BUDGET = 180.0
STALL_BUDGET = 120.0

# A finished transfer whose file has not appeared is the `.part` rename, which is quick. Past this, the
# file is not coming.
FINISHED_GRACE = 15.0

# How often a transfer in flight is asked about. The server asks for thirty seconds between polls; that is
# its own conservatism, and asking costs it a dictionary read. The number matters twice: every budget above
# is decided *on a poll*, so this is the resolution they are kept to, and it is how long a file that has
# already landed goes unnoticed, holding its worker.
POLL_INTERVAL = 10.0

# Statuses after which the server will never change its mind.
_OVER = frozenset({"failed", "cancelled", "not_found", "session_expired"})


class Move(Enum):
    """What to do with the candidate the server is holding."""

    WAIT = "wait"
    ROTATE = "rotate"       # give up on this candidate; another may be better
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Transfer:
    """What the server last said about a download."""

    status: str = ""
    connection_state: str = ""
    progress_pct: float = 0.0
    received_bytes: int = 0
    local_path: str = ""
    message: str = ""


def budget_for(transfer: Transfer, *, declared_queue: int = 0) -> float:
    """How long a candidate the server still believes in may sit without a byte before it is let go.

    A transfer that has started and stopped moving is dead whatever the server says; a peer that declared a
    queue is behaving normally and gets a budget worth waiting for; anything else is a connection that either
    opens quickly or never will.
    """
    if transfer.status == "finished":
        return FINISHED_GRACE
    if transfer.status == "downloading" or transfer.connection_state == "transferring":
        return STALL_BUDGET
    return QUEUE_BUDGET if declared_queue else CONNECT_BUDGET


def move(transfer: Transfer, *, on_disk: bool, stuck_for: float, declared_queue: int = 0) -> Move:
    """What to do next, given seconds since `received_bytes` last increased.

    Before anything transfers, that is simply how long the download has been running.
    """
    if on_disk:
        return Move.DONE
    if transfer.status == "finished":
        # The `.part` rename may not have landed yet; the next poll settles it either way.
        return Move.WAIT if stuck_for <= FINISHED_GRACE else Move.FAILED
    if transfer.status in _OVER:
        return Move.FAILED
    return Move.ROTATE if stuck_for > budget_for(transfer, declared_queue=declared_queue) else Move.WAIT


def transfer_of(payload: dict) -> Transfer:
    """The server's answer to `download` or `download_status`, as far as the decision needs it."""
    return Transfer(
        status=str(payload.get("status") or ""),
        connection_state=str(payload.get("connection_state") or ""),
        progress_pct=float(payload.get("progress_pct") or 0.0),
        received_bytes=int(payload.get("received_bytes") or 0),
        local_path=str(payload.get("local_path") or ""),
        message=str(payload.get("message") or ""),
    )


def length_from_size(candidate: Candidate) -> int | None:
    """A lossy file's length from the bytes it will send and the bitrate the peer reported.

    Bytes are bitrate × seconds / 8, so a peer that reported a bitrate and a size has reported the length
    even when it left the duration attribute empty — and a file the rule can judge is a file it does not have
    to ask the user about. Only for lossy files: on a lossless one `bitrate` carries the server's quality
    score, which is a different quantity and gives a length a few seconds out.
    """
    if candidate.lossless or not candidate.bitrate or not candidate.size_bytes:
        return None
    return round(candidate.size_bytes * 8 / (candidate.bitrate * 1000))


def candidate_of(item: dict) -> Candidate | None:
    """One search result, or None when it names no file we could ask for.

    For a lossless file the server reports its quality score in `bitrate` rather than a kbps rate; harmless,
    because the quality floor only reads that field for lossy files.
    """
    user = str(item.get("username") or "")
    path = str(item.get("filename") or "")
    if not user or not path:
        return None
    duration = item.get("duration_sec")
    candidate = Candidate(
        user=user,
        path=path,
        duration=int(duration) if duration is not None else None,
        bitrate=item.get("bitrate"),
        queue=int(item.get("queue_size") or 0),
        # Carried through from the search result rather than dropped: a peer that answered with no free slot
        # is a wait, not a failure, and the fact belongs to the candidate rather than to the moment.
        slots=bool(item.get("has_free_slots", True)),
        size_bytes=item.get("filesize"),
    )
    if candidate.duration is None:
        candidate = replace(candidate, duration=length_from_size(candidate))
    return candidate


def identifier(candidate: Candidate) -> str:
    """The id the server wants back: `{username}:{filename}`."""
    return f"{candidate.user}:{candidate.path}"


@dataclass
class Summary:
    """What a pass came to, by track."""

    downloaded: list[int] = field(default_factory=list)
    already_downloaded: list[int] = field(default_factory=list)
    already_held: list[int] = field(default_factory=list)
    decided: list[int] = field(default_factory=list)      # settled by hand
    in_review: list[int] = field(default_factory=list)
    nothing_usable: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    unsearched: list[int] = field(default_factory=list)   # the network stopped answering

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
        if self.decided:
            parts.append(f"{len(self.decided)} settled by hand")
        if self.failed:
            parts.append(f"{len(self.failed)} to retry")
        if self.unsearched:
            parts.append(f"{len(self.unsearched)} not searched — the network had gone quiet")
        return " · ".join(parts)


# This many empty searches in a row is a network that has stopped answering, not a run of rare tracks.
QUIET_AFTER = 50


@dataclass
class QuietNetwork:
    """Searches in a row that came back empty, and the decision to stop asking.

    *In a row*: one hit anywhere means the network is answering, so it resets. The event is what the loop
    watches — a thread that sees it stops starting work rather than spending a search to find out.
    """

    limit: int = QUIET_AFTER
    stopped: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._run = 0
        self._trip = 0

    def observe(self, found: int) -> None:
        """One search's raw result count, as the child reports it."""
        with self._lock:
            self._run = 0 if found else self._run + 1
            if self.limit and self._run >= self.limit and not self.stopped.is_set():
                self._trip = self._run
                self.stopped.set()

    @property
    def consecutive(self) -> int:
        with self._lock:
            return self._run

    @property
    def tripped_at(self) -> int:
        """How long the run of empty searches was when the pass gave up on the network."""
        with self._lock:
            return self._trip


class Pacer:
    """Let searches start no more often than `interval` seconds apart.

    Nothing between this loop and the network keeps to the network's rate, and neither the child nor aioslsk
    reports a search being ignored, so the pace is kept here. A gap between two searches rather than a cap
    on how many run at once, because the open searches are only a consequence of the rate.
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

    Peer attributes may scope a retry pass and never the first one, because a filter can eliminate the only
    peer who has the track.
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
        stuck_for = clock() - last_change
        on_disk = bool(local_path) and Path(local_path).is_file()
        decision = move(report, on_disk=on_disk, stuck_for=stuck_for, declared_queue=candidate.queue)
        if decision is not Move.WAIT:
            return decision, local_path

        # The wait is what is left of the budget, never more than one poll: sleeping the interval regardless
        # would keep every budget to the poll's own grid — 36 s on a 10 s poll costs 40 — and would leave a
        # file that landed mid-interval unnoticed for the rest of it.
        budget = budget_for(report, declared_queue=candidate.queue)
        sleep(max(0.0, min(POLL_INTERVAL, budget - stuck_for)))
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


def record_taken(
    job: state.Job,
    record: state.Record,
    candidate: Candidate,
    local_path: str,
    *,
    accepted: bool = False,
    upgrade: Held | None = None,
) -> None:
    """Write down a file that arrived: the record follows it, and the log says where it came from.

    The one place a download is recorded, because `reconcile` rebuilds a record from its own `downloaded`
    event — a second way of recording one would be a second way of losing one. `accepted` is set only by
    `review`, the only writer of the decision axis.
    """
    record.local_path = local_path
    record.format = candidate.ext
    record.status = state.DOWNLOADED
    if accepted:
        record.decision = state.ACCEPTED
    job.append_event("downloaded", idx=record.idx, path=local_path, peer=candidate.user,
                     size=candidate.size_bytes)
    if upgrade is not None:
        record.notes = f"upgrades {upgrade.path}"
        job.append_event("upgrade_downloaded", idx=record.idx, replaces=upgrade.path, path=local_path)


def _prepare(
    job: state.Job,
    record: state.Record,
    lookup: Lookup,
    library: Library | None,
    log,
) -> tuple[str | None, Held | None]:
    """Resolve the reference, then settle what the collection already holds.

    Returns the summary list to file the track under when there is nothing to fetch, and otherwise the lossy
    copy a successful download would replace.
    """
    if not record.candidates:
        # Resolved once, then written into the record: a resumed run never re-queries, and a track that
        # resolved nothing stays review-only rather than being re-asked every time.
        seconds = reference.resolve(record.request, lookup)
        record.duration_s = seconds
        job.append_event("reference", idx=record.idx, seconds=seconds)
        log(f"[{record.idx}] {record.label}: "
            + (f"reference {seconds}s" if seconds else "no MusicBrainz reference — review only"))

    if library is None:
        return None, None

    # Nothing is fetched that the collection already holds, unless the copy it holds is lossy and a lossless
    # one can be had — and that is asked before the search, so a track already held costs no request either.
    held = library.held(record.artist, record.title, record.duration_s)
    if held is None:
        # The length may be the only reason that missed, and for an upgrade there may be no MusicBrainz
        # length at all. So the name is asked on its own — but the *version* still has to agree, or a remix
        # would repoint the row that holds the original, and a row whose length rekordbox never had is still
        # the copy this track would replace (2026-10-13: three tracks went unmatched that way and were
        # searched as if the collection did not hold them).
        for row in library.rows_for(record.artist, record.title):
            if not row.lossless and mix_matches(record.mix, row.mix):
                held = row
                break
    if held is None:
        return None, None
    if held.lossless:
        record.status = state.FOUND_IN_LIBRARY
        record.notes = f"already held at {held.path}" if held.path else "already held"
        job.append_event("already_held", idx=record.idx, path=held.path, genre=held.genre)
        log(f"[{record.idx}] {record.label}: already in the library"
            + (f" — {held.path}" if held.path else ""))
        return "already_held", None
    # Held, but only as a lossy copy: an upgrade rather than a reason to skip, and the lane decides it, not
    # whether the lookup happened to match.
    record.replaces = held.path
    if record.duration_s is None and held.seconds is not None:
        record.duration_s = held.seconds
        job.append_event("reference", idx=record.idx, seconds=held.seconds,
                         from_="the copy in the collection")
        log(f"[{record.idx}] {record.label}: reference {held.seconds}s, from the copy in the "
            "collection — MusicBrainz has nothing for this one")
    else:
        log(f"[{record.idx}] {record.label}: held as a lossy copy — looking for a lossless one")
    return None, held


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
    quiet: QuietNetwork | None = None,
) -> str:
    """Acquire one track. Returns which of the summary's lists it belongs in."""
    if record.decided():
        # GLOSSARY.md: a settled track is not asked about again — that is the whole of what the decision
        # axis means — so a pass leaves it alone rather than spending a search to re-open the question.
        log(f"[{record.idx}] {record.label}: {record.decision} by hand — left alone")
        return "decided"

    if record.local_path and Path(record.local_path).is_file():
        # The file is the truth, not the status: a record this run has already placed must not be searched
        # for again, because until the import runs the collection still points at the copy it replaces.
        if record.status in {state.DOWNLOADED, state.NORMALIZED, state.TAGGED, state.IMPORTED}:
            return "already_downloaded"

    outcome, upgrade = _prepare(job, record, lookup, library, log)
    if outcome is not None:
        return outcome

    if pacer is not None:
        pacer.wait()
    found = search(server, record, later_pass=later_pass)
    if quiet is not None:
        # The raw count, not what merged: a second pass over the same record adds nothing new and would
        # otherwise look like a network answering with silence.
        quiet.observe(len(found))
    added = merge(record, found)
    job.append_event("searched", idx=record.idx, added=added, offered=len(record.candidates))

    if record.settled() is None:
        record.status = state.PENDING
        log(f"[{record.idx}] {record.label}: {len(record.offered())} offered, "
            "nothing to take on its own")
        return "in_review" if record.usable() else "nothing_usable"

    tried = set(record.tried)
    queue = sorted(
        (verdict.candidate for verdict in rules.ready(record.track)
         if identifier(verdict.candidate) not in tried),
        key=rules.rank,
    )
    if upgrade is not None:
        # A lossless copy is strictly better than the lossy one held; a lossy copy is not better than the
        # copy already held, so only the better lane is worth fetching.
        queue = [candidate for candidate in queue if candidate.lossless]
        if not queue:
            record.status = state.PENDING
            job.append_event("no_upgrade_offered", idx=record.idx, replaces=upgrade.path)
            log(f"[{record.idx}] {record.label}: nothing lossless offered — the copy held stands")
            return "in_review"

    for candidate in queue[:MAX_CANDIDATES]:
        record.tried.append(identifier(candidate))
        decision, local_path = await_transfer(server, candidate, log=log, sleep=sleep, clock=clock)

        if decision is Move.DONE:
            record_taken(job, record, candidate, local_path, upgrade=upgrade)
            if upgrade is not None:
                log(f"[{record.idx}] {record.label}: a lossless copy from {candidate.user} → {local_path}"
                    f"  (replacing the lossy one at import)")
            else:
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
    request a second, so lookups queue here rather than race inside a library not written for it.
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
    stream=None,
) -> Summary:
    """The whole pass: every record the job holds, `workers` of them at a time, writing as it goes.

    The transfers and the waiting on them overlap; the searching does not, and goes out at
    `config.search_interval`, because that rate is the network's and not this loop's.
    """
    records = job.records()
    summary = Summary()
    quiet = QuietNetwork()
    bar = progress.Bar(len(records), log=log, stream=stream)

    # The network's rate on searching, and ours to keep. Zero is the loop's own speed, worth having only for
    # a tracklist too small for the rate to matter.
    pacer = (Pacer(config.search_interval, clock=clock, sleep=sleep)
             if config.search_interval > 0 else None)

    # One track's story stays readable while others are running, so lines are printed under the bar's lock.
    def say(line: str) -> None:
        bar.say(line)

    def work(record: state.Record) -> None:
        if quiet.stopped.is_set():
            # The network has stopped answering, so the pass stops asking; the record is left exactly as it
            # was, and nothing is written about a search that never happened.
            summary.unsearched.append(record.idx)
            bar.settle(record.idx, record.label, "unsearched")
            return
        later_pass = retry or bool(record.candidates)
        bar.begin(record.idx, record.label)
        try:
            outcome = one(job, record, server, lookup, library=library, log=say, sleep=sleep,
                          clock=clock, later_pass=later_pass, pacer=pacer, quiet=quiet)
        except (slsk.ToolError, ServerError) as error:
            record.status = state.RETRY_LATER
            outcome = "failed"
            say(f"[{record.idx}] {record.label}: the server refused — {error}")
            job.append_event("refused", idx=record.idx, error=str(error))
        getattr(summary, outcome).append(record.idx)
        bar.settle(record.idx, record.label, outcome)
        job.write_records(records)          # a crash costs the tracks in flight and nothing else

    if records:
        lookup = one_at_a_time(lookup)
        if pacer is not None:
            say(f"one search every {config.search_interval:g}s"
                " — the network's rate, not this loop's")
        with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
            list(pool.map(work, records))

    if quiet.stopped.is_set():
        job.append_event("network_quiet", consecutive=quiet.tripped_at,
                         unsearched=len(summary.unsearched))
        say(f"the last {quiet.tripped_at} searches came back with nothing — the network has stopped "
            f"answering, so {len(summary.unsearched)} tracks were not searched at all")

    # The lists fill in whatever order the tracks finish, so they are put back in track order to report.
    for name in ("downloaded", "already_downloaded", "already_held", "in_review",
                 "nothing_usable", "failed", "unsearched"):
        getattr(summary, name).sort()

    job.write_status(config)
    bar.close()
    # Deliberately not finished: the pipeline ends at the import, so this and `process` leave the run open
    # and the next command picks the same one up without being told which.
    return summary
