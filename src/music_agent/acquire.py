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

from . import peers, progress, reference, rules, slsk, state
from .config import Config
from .library import Held, Library
from .mcp import ServerError, StdioServer
from .parsing import mix_matches, search_query
from .reference import Lookup
from .rules import Candidate

# The child sleeps exactly this long after a query goes out, so this is how long a peer has to answer at
# all. The server sorts and truncates results, so a small cap discards good candidates before we see them.
SEARCH_TIMEOUT = 10
CALL_TIMEOUT = 8.0
MAX_RESULTS = 60

# Candidates attempted per track, per pass. Enough to work past a peer or two who never opens the
# connection, and no more: each one costs its own budget below.
MAX_CANDIDATES = 6

# Search calls in a row that timed out instead of answering. One costs a full deadline to learn nothing; a
# run of them is a child that has stopped talking, and the pass stops rather than spending twenty more.
STUCK_AFTER = 4

# A connection that never transferred a byte is rotated after this long. A peer either opens the connection
# quickly or it is not going to.
CONNECT_BUDGET = 12.0

# A peer that declared a queue is behaving normally rather than failing, so it gets a budget worth waiting
# for; a transfer that has started and stopped moving bytes is dead whatever the server says.
QUEUE_BUDGET = 45.0
STALL_BUDGET = 45.0

# One candidate may not own the pass. Every budget above is reset by a byte, so a peer that trickles never
# trips one: 0.107 MB/s sustained on a 99 MB file held a whole pass open for 764 of its 1,772 seconds, and
# the pool ends only when the last record does, so nothing else ran while it did. Past this ceiling the
# candidate is rotated like any other slow peer and the track is left to a later pass, which costs nothing
# that reaching the ceiling did not already cost: a file this slow was not arriving inside the pass.
TRANSFER_CEILING = 600.0

# A finished transfer whose file has not appeared is the `.part` rename, which is quick. Past this, the
# file is not coming.
FINISHED_GRACE = 5.0

# How often a transfer in flight is asked about. The server asks for thirty seconds between polls; that is
# its own conservatism, and asking costs it a dictionary read. The number matters twice: every budget above
# is decided *on a poll*, so this is the resolution they are kept to, and it is how long a file that has
# already landed goes unnoticed, holding its worker.
POLL_INTERVAL = 3.0

# Statuses after which the server will never change its mind.
_OVER = frozenset({"failed", "cancelled", "not_found", "session_expired"})


class ServerStuck(RuntimeError):
    """The child stopped answering — searches timed out in a row, so the pass stopped asking rather than
    spending a deadline each time to learn nothing.

    `lost_session` tells the two kinds apart for the caller that decides whether to try again. Timed-out
    calls are the child's own problem and a fresh child is worth having; a session the server took away is
    the account's, and a second child loses it the same way.
    """

    def __init__(self, message: str, *, lost_session: bool = False) -> None:
        super().__init__(message)
        self.lost_session = lost_session


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


def move(transfer: Transfer, *, on_disk: bool, stuck_for: float, declared_queue: int = 0,
         elapsed: float = 0.0) -> Move:
    """What to do next, given seconds since `received_bytes` last increased.

    Before anything transfers, that is simply how long the download has been running. `elapsed` is how long
    the candidate has had altogether, which is the only thing that stops a peer trickling slowly enough to
    reset every other budget on the way.
    """
    if on_disk:
        return Move.DONE
    if transfer.status == "finished":
        # The `.part` rename may not have landed yet; the next poll settles it either way.
        return Move.WAIT if stuck_for <= FINISHED_GRACE else Move.FAILED
    if transfer.status in _OVER:
        return Move.FAILED
    if elapsed > TRANSFER_CEILING:
        return Move.ROTATE
    return Move.ROTATE if stuck_for > budget_for(transfer, declared_queue=declared_queue) else Move.WAIT


# Length a peer can declare a queue to and still be believed. A real share's queue is in the tens; paid
# mirrors and promo pools report five and six figures so that every request sits in them forever, and
# "it declared a queue" must not be the thing that keeps them off the list.
ABSURD_QUEUE = 1000


def offence(decision: Move, report: Transfer, candidate: Candidate) -> str | None:
    """What the peer did wrong, or None when the ending was not the peer's to answer for.

    This distinction is why a peer is ignored at all. A budget we chose to stop at, a queue within reason
    that a peer declared, and a session the child lost are endings about us, and a peer that has already
    sent bytes has shown it can send them. What is left is a peer that advertised a file and then delivered
    nothing — twice, three times, every pass.
    """
    if decision is Move.DONE:
        return None
    if report.status in {"session_expired", "cancelled"}:
        return None
    if decision is Move.FAILED:
        return report.status or "failed"
    if report.received_bytes:
        return None
    if candidate.queue and candidate.queue < ABSURD_QUEUE:
        return None
    return "nothing arrived"


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
    dropped: list[int] = field(default_factory=list)      # every candidate refused, nothing left to try
    # The peers this pass put on the ignore list, by name: a run that silences three mirrors should say so.
    ignored: set[str] = field(default_factory=set)

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
        if self.dropped:
            parts.append(f"{len(self.dropped)} dropped — every offer was refused")
        if self.ignored:
            parts.append(f"{len(self.ignored)} peers ignored — music-agent peers lists them")
        if self.failed:
            parts.append(f"{len(self.failed)} to retry")
        if self.unsearched:
            parts.append(f"{len(self.unsearched)} not searched — the network had gone quiet")
        return " · ".join(parts)


# This many empty searches in a row is a network that has stopped answering, not a run of rare tracks.
#
# Fifty, and it was lowered to fifteen for a day: on a 305-track techno playlist the pass stopped twice at
# fifteen and left 184 tracks unsearched, because once the findable tracks are taken the ones still to search
# are exactly the rare ones and their searches answer nothing one after another. Two hundred searches had run
# and 179 of them had offered candidates, so the network was answering perfectly well. A streak this long is
# the cheap signal to trust; a shorter one is the tail of a playlist.
QUIET_AFTER = 50


@dataclass
class QuietNetwork:
    """Searches in a row that came back empty, and the decision to stop asking.

    *In a row*: one hit anywhere means the network is answering, so it resets. The event is what the loop
    watches — a thread that sees it stops starting work rather than spending a search to find out.

    A search that times out instead of answering counts separately: an empty answer is the network, and no
    answer at all is the child. A run of those stops the pass as well, because every one of them costs a
    full deadline to learn nothing.
    """

    limit: int = QUIET_AFTER
    stuck_after: int = STUCK_AFTER
    # The child's stderr, when the caller has one: it is the only thing that can tell a search that came
    # back empty from a search the child could not send at all.
    child: "slsk.ChildLog | None" = None
    stopped: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._run = 0
        self._trip = 0
        self._fails = 0
        self._hung = 0

    @property
    def session_lost(self) -> bool:
        """Whether the child has said it can no longer answer — a session it does not have, or a message
        reader that died on a frame it could not read.

        The server drops a client, aioslsk does not reconnect, and every search from then on is an ordinary
        success with nothing in it — which is what a rare track looks like too. Nothing else in the stack can
        tell those apart, and the child recovers from neither.
        """
        return self.child is not None and self.child.cannot_answer

    def observe(self, found: int) -> None:
        """One search's raw result count, as the child reports it."""
        if self.session_lost:
            # An empty answer from a child with no session is not the network's answer, and there is no
            # streak to count: the pass is over as soon as this is known.
            self.stopped.set()
            return
        with self._lock:
            self._run = 0 if found else self._run + 1
            self._fails = 0
            if self.limit and self._run >= self.limit and not self.stopped.is_set():
                self._trip = self._run
                self.stopped.set()

    def failed(self) -> None:
        """One search that never answered."""
        with self._lock:
            self._fails += 1
            if self.stuck_after and self._fails >= self.stuck_after and not self.stopped.is_set():
                self._hung = self._fails
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

    @property
    def hung(self) -> int:
        """How many calls in a row timed out, when that is what stopped the pass. Zero means it did not."""
        with self._lock:
            return self._hung


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
) -> tuple[Move, str, Transfer]:
    """Take one candidate as far as it goes. Returns how it ended, where the file is, and what the server
    last said about it — the last of those is what the peer's own failure is read from."""
    payload = slsk.check(server.call("download", {"id": identifier(candidate)}, timeout=CALL_TIMEOUT),
                         "download")
    report = transfer_of(payload)
    local_path = report.local_path
    log(f"      asking {candidate.user} for {candidate.name}")

    last_bytes = 0
    last_change = clock()
    started = last_change
    while True:
        stuck_for = clock() - last_change
        on_disk = bool(local_path) and Path(local_path).is_file()
        decision = move(report, on_disk=on_disk, stuck_for=stuck_for, declared_queue=candidate.queue,
                        elapsed=clock() - started)
        if decision is not Move.WAIT:
            return decision, local_path, report

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


def drop_exhausted(job: state.Job, log=print) -> list[int]:
    """Close every track whose every candidate was tried and refused.

    Nothing is left to try, so the track is settled rather than left in the queue: a pass that ended with a
    peer's queue full is worth another pass, and a track with one untried candidate is left alone. A track a
    hand settled, or one no pass has reached, is never touched.
    """
    dropped: list[int] = []
    records = job.records()
    for record in records:
        if record.decided() or record.status != state.RETRY_LATER:
            continue
        tried = set(record.tried)
        if not tried or any(identifier(candidate) not in tried for candidate in record.candidates):
            continue
        record.decision = state.DECLINED
        record.status = state.NOT_FOUND
        record.notes = "every offer was refused"
        job.append_event("dropped", idx=record.idx, tried=len(tried))
        dropped.append(record.idx)
    if dropped:
        job.write_records(records)
        log(f"{len(dropped)} dropped — every offer was refused, nothing left to try")
    return dropped


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
        # resolved nothing is not re-asked every time — it is simply taken on its name.
        seconds = reference.resolve(record.request, lookup)
        record.duration_s = seconds
        job.append_event("reference", idx=record.idx, seconds=seconds)
        log(f"[{record.idx}] {record.label}: "
            + (f"reference {seconds}s" if seconds else "no reference length — taken on its name"))

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
    ledger: peers.Ledger | None = None,
    ignored: set[str] | None = None,
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
    if ledger is not None:
        # Dropped here rather than filtered later, so nothing downstream — the ladder, the queue, the counts —
        # ever sees a peer this account has given up on.
        kept = [candidate for candidate in found if not ledger.ignores(candidate.user)]
        if len(kept) != len(found):
            log(f"[{record.idx}] {record.label}: {len(found) - len(kept)} files from ignored peers")
        found = kept
    added = merge(record, found)
    job.append_event("searched", idx=record.idx, added=added, offered=len(record.candidates))

    if record.settled() is None:
        record.status = state.PENDING
        log(f"[{record.idx}] {record.label}: {len(record.offered())} offered, "
            "nothing to take on its own")
        return "in_review" if record.usable() else "nothing_usable"

    tried = set(record.tried)
    queue = sorted(
        (verdict.candidate for verdict in rules.takeable(record.track)
         if identifier(verdict.candidate) not in tried),
        key=rules.rank,
    )
    silenced = 0
    if ledger is not None:
        # Asked again for the files already in hand: a peer banned a moment ago, by the first file of its
        # own that it failed to deliver, must not be asked for the other five.
        before = len(queue)
        queue = [candidate for candidate in queue if not ledger.ignores(candidate.user)]
        silenced = before - len(queue)
    if upgrade is not None:
        # A lossless copy is strictly better than the lossy one held; a lossy copy is not better than the
        # copy already held, so only the better lane is worth fetching.
        queue = [candidate for candidate in queue if candidate.lossless]
        if not queue:
            record.status = state.PENDING
            job.append_event("no_upgrade_offered", idx=record.idx, replaces=upgrade.path)
            log(f"[{record.idx}] {record.label}: nothing lossless offered — the copy held stands")
            return "in_review"

    if not queue and silenced:
        # Nothing is left to try that the account is still willing to ask, which is not the same thing as a
        # pass that failed: the record stays open, and a person may still take one of those offers by hand.
        record.status = state.PENDING
        job.append_event("only_ignored_peers", idx=record.idx, ignored=silenced)
        log(f"[{record.idx}] {record.label}: every offer left is from an ignored peer")
        return "in_review" if record.usable() else "nothing_usable"

    for candidate in queue[:MAX_CANDIDATES]:
        if ledger is not None and ledger.ignores(candidate.user):
            # Banned by an earlier file of its own in this same pass: the rest of its offers are the same
            # question, already answered.
            silenced += 1
            continue
        record.tried.append(identifier(candidate))
        decision, local_path, report = await_transfer(server, candidate, log=log, sleep=sleep,
                                                      clock=clock)

        if decision is Move.DONE:
            if ledger is not None:
                ledger.delivered(candidate.user)
            record_taken(job, record, candidate, local_path, upgrade=upgrade)
            if upgrade is not None:
                log(f"[{record.idx}] {record.label}: a lossless copy from {candidate.user} → {local_path}"
                    f"  (replacing the lossy one at import)")
            else:
                log(f"[{record.idx}] {record.label}: from {candidate.user} → {local_path}")
            return "downloaded"

        why = offence(decision, report, candidate)
        if decision is Move.ROTATE:
            give_up(server, candidate, log)
            log(f"      {candidate.user} went nowhere; rotating ({len(record.tried)} tried)")
        else:
            log(f"      {candidate.user} failed")
        job.append_event("candidate_lost", idx=record.idx, peer=candidate.user,
                         path=candidate.path, why=decision.value, reason=why or "")
        if ledger is not None and why is not None and ledger.offence(candidate.user, why):
            if ignored is not None:
                ignored.add(candidate.user)
            job.append_event("peer_ignored", idx=record.idx, peer=candidate.user, reason=why)
            log(f"      {candidate.user} ignored — {ledger.maxretry} failures in "
                f"{ledger.findtime.days} days (music-agent peers --unban {candidate.user})")

    record.status = state.RETRY_LATER
    job.append_event("exhausted", idx=record.idx, tried=len(record.tried))
    if silenced:
        log(f"[{record.idx}] {record.label}: everything asked failed, {silenced} offers left from ignored "
            "peers — left to retry")
    else:
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
    child: "slsk.ChildLog | None" = None,
) -> Summary:
    """The whole pass: every record the job holds, `workers` of them at a time, writing as it goes.

    The transfers and the waiting on them overlap; the searching does not, and goes out at
    `config.search_interval`, because that rate is the network's and not this loop's.
    """
    records = job.records()
    summary = Summary()
    quiet = QuietNetwork(child=child)
    bar = progress.Bar(len(records), log=log, stream=stream)

    # The ignore list, read once: twenty workers ask it on every search and every failure, and `offence`
    # writes it whole from what this object holds.
    ledger = peers.Ledger.at(config)
    already_ignored = sorted(ledger.ignored())
    if already_ignored:
        log(f"{len(already_ignored)} peers ignored — music-agent peers lists them, --unban USER relents")

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
                          clock=clock, later_pass=later_pass, pacer=pacer, quiet=quiet,
                          ledger=ledger, ignored=summary.ignored)
        except (slsk.ToolError, ServerError) as error:
            record.status = state.RETRY_LATER
            outcome = "failed"
            if quiet is not None:
                quiet.failed()
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

    if quiet.hung or quiet.session_lost:
        account = quiet.session_lost and child is not None and child.lost_session > 0
        summary.failed.sort()
        summary.dropped = drop_exhausted(job, log=say)
        summary.failed = [idx for idx in summary.failed if idx not in set(summary.dropped)]
        if quiet.session_lost:
            # The child answered, and every answer was empty because it had no way to search. Named here
            # because from the inside this is indistinguishable from a quiet network, and a pass that stops
            # on it while returning success is a pass that lies.
            job.append_event("lost_session" if account else "reader_died",
                             unsearched=len(summary.unsearched))
            reason = (
                f"the child was left without a session — {len(summary.unsearched)} tracks were never "
                f"searched. Another client has the account, or the server has stopped answering this one, "
                f"and a fresh child loses it the same way, so the passes stop here."
                if account else
                f"the child's message reader died on a frame it could not read — "
                f"{len(summary.unsearched)} tracks were never searched. That is one child's bad luck, not "
                f"the account's, so a fresh child resumes the pass from here."
            )
        else:
            job.append_event("server_stuck", consecutive=quiet.hung, unsearched=len(summary.unsearched))
            reason = (f"{quiet.hung} searches timed out in a row — {len(summary.unsearched)} tracks were "
                      f"never searched. The child had stopped answering; the run is open, so a fresh pass "
                      f"picks it up.")
        job.write_status(config)
        bar.close()
        raise ServerStuck(reason, lost_session=account)

    if quiet.stopped.is_set():
        job.append_event("network_quiet", consecutive=quiet.tripped_at,
                         unsearched=len(summary.unsearched))
        say(f"the last {quiet.tripped_at} searches came back with nothing — the network has stopped "
            f"answering, so {len(summary.unsearched)} tracks were not searched at all")

    # A track whose every candidate refused is closed here rather than put to a person: there is nothing
    # left to try, and asking about it would only be asking permission to stop.
    summary.dropped = drop_exhausted(job, log=say)
    summary.failed = [idx for idx in summary.failed if idx not in set(summary.dropped)]

    # The lists fill in whatever order the tracks finish, so they are put back in track order to report.
    for name in ("downloaded", "already_downloaded", "already_held", "in_review",
                 "nothing_usable", "failed", "unsearched", "dropped"):
        getattr(summary, name).sort()

    job.write_status(config)
    bar.close()
    # Deliberately not finished: the pipeline ends at the import, so this and `process` leave the run open
    # and the next command picks the same one up without being told which.
    return summary
