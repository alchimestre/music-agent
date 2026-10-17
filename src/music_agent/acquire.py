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
from .parsing import mix_matches, search_query
from .reference import Lookup
from .rules import Candidate
from .slsk import Daemon, DaemonError

# The window a search is given: the query goes out, peers answer on their own connections, and the pass reads
# once at the end. The pace is one search every 6.47s — the network's rate — so a longer window is time the
# pass spends asleep with nothing to do. Five seconds fills the 60 offers it keeps from the hundreds a common
# title returns; the daemon is told the same.
SEARCH_TIMEOUT = 5.0
# Offers kept from one search. The daemon answers with everything it collected, and a common title can put
# thousands in one line; the server has already sorted them, and the ladder only ever asks six peers.
MAX_RESULTS = 60

# Candidates attempted per track, per pass. Enough to work past a peer or two who never opens the
# connection, and no more: each one costs its own budget below.
MAX_CANDIDATES = 6

# Search calls in a row that timed out instead of answering. One costs a full deadline to learn nothing; a
# run of them is a daemon that has stopped talking, and the pass stops rather than spending twenty more.
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

# Statuses after which the daemon will never change its mind.
_OVER = frozenset({"failed", "cancelled", "timed_out"})


class ServerStuck(RuntimeError):
    """The network stopped answering — searches timed out in a row, or the daemon recorded a lost session.

    `lost_session` tells the two kinds apart for the caller that decides whether to try again. A daemon that
    stopped answering is worth restarting; a session the server took away is the account's, and a second
    daemon loses it the same way.
    """

    def __init__(self, message: str, *, lost_session: bool = False) -> None:
        super().__init__(message)
        self.lost_session = lost_session


class Move(Enum):
    """What to do with the candidate the daemon is holding."""

    WAIT = "wait"
    ROTATE = "rotate"       # give up on this candidate; another may be better
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Transfer:
    """What the daemon last said about a download."""

    status: str = ""
    received_bytes: int = 0
    total_bytes: int | None = None
    queue: int = 0
    local_path: str = ""
    message: str = ""


# A queue a peer can declare and still be believed. A real share's is in the tens; paid mirrors and promo
# pools report five and six figures so that every request sits in them forever.
ABSURD_QUEUE = 1000


def budget_for(transfer: Transfer) -> float:
    """How long a candidate the daemon still believes in may sit without a byte before it is let go.

    A transfer that has started and stopped moving is dead whatever the daemon says; a peer whose queue is a
    real queue is behaving normally and gets a budget worth waiting for; anything else is a connection that
    either opens quickly or never will.
    """
    if transfer.status == "completed":
        return FINISHED_GRACE
    if transfer.status in {"in_progress", "paused"}:
        return STALL_BUDGET
    if transfer.status == "queued":
        # A queued transfer is one the peer accepted and put in its queue, which is normal Soulseek. A
        # position in the five and six figures is a paid mirror holding every request it ever gets, and the
        # position is the evidence rather than the excuse.
        return CONNECT_BUDGET if transfer.queue >= ABSURD_QUEUE else QUEUE_BUDGET
    return CONNECT_BUDGET


def move(transfer: Transfer, *, on_disk: bool, stuck_for: float, elapsed: float = 0.0) -> Move:
    """What to do next, given seconds since `received_bytes` last increased.

    Before anything transfers, that is simply how long the download has been running. `elapsed` is how long
    the candidate has had altogether, which is the only thing that stops a peer trickling slowly enough to
    reset every other budget on the way.
    """
    if on_disk:
        return Move.DONE
    if transfer.status == "completed":
        # The daemon's own `.part` rename has not landed yet; the next poll settles it either way.
        return Move.WAIT if stuck_for <= FINISHED_GRACE else Move.FAILED
    if transfer.status in _OVER:
        return Move.FAILED
    if elapsed > TRANSFER_CEILING:
        return Move.ROTATE
    return Move.ROTATE if stuck_for > budget_for(transfer) else Move.WAIT


def offence(decision: Move, report: Transfer) -> str | None:
    """What the peer did wrong, or None when the ending was not the peer's to answer for.

    This distinction is why a peer is ignored at all. A budget we chose to stop at, a queue within reason
    that a peer declared, and a transfer we cancelled are endings about us, and a peer that has already sent
    bytes has shown it can send them. What is left is a peer that advertised a file and then delivered
    nothing — twice, three times, every pass.
    """
    if decision is Move.DONE:
        return None
    if report.status in {"cancelled", "completed"}:
        return None
    if decision is Move.FAILED:
        return report.status or "failed"
    if report.received_bytes:
        return None
    if report.queue and report.queue < ABSURD_QUEUE:
        return None
    return "nothing arrived"


def transfer_of(download: dict) -> Transfer:
    """One `DownloadDto`, as far as the decision needs it.

    The daemon reports the state as a tagged union: only the in-progress and paused kinds carry byte counts,
    and only a queued one carries a position.
    """
    status = download.get("status") or {}
    total = status.get("total_bytes")
    return Transfer(
        status=str(status.get("state") or ""),
        received_bytes=int(status.get("bytes_downloaded") or 0),
        total_bytes=int(total) if total else None,
        queue=int(download.get("queue_position") or 0),
        local_path=slsk.local_path(download),
        message=str(status.get("reason") or ""),
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
        duration=int(duration) if duration else None,
        bitrate=item.get("bitrate"),
        # Carried through from the search result rather than dropped: a peer that answered with no free slot
        # is a wait, not a failure, and the fact belongs to the candidate rather than to the moment.
        slots=bool(item.get("has_free_slots", True)),
        size_bytes=item.get("filesize"),
        speed=item.get("avg_speed"),
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
            parts.append(f"{len(self.unsearched)} not searched — the daemon had stopped answering")
        return " · ".join(parts)


# A search that comes back with nothing is not a failure. Once the findable tracks are taken, what is left to
# search is the rare ones, and their searches answer nothing one after another, so the tail of a tracklist and
# a dead network look the same from inside a pass. Counting those in a row was a stop signal once, and it ended
# passes over healthy networks. Only the daemon is evidence: no answer at all, or a session it reports lost.


@dataclass
class Watch:
    """Whether the daemon can still search, and the decision to stop asking.

    Both ways a pass stops are read from the daemon rather than inferred from empty answers. A run of calls
    that never answered is a daemon that has stopped talking; `daemon.status` records a session the server
    took away, and from inside a search that is indistinguishable from a rare track — every answer after it
    is an ordinary success with nothing in it. The event is what the loop watches: a thread that sees it
    stops starting work rather than spending a search to find out.
    """

    daemon: "slsk.Daemon | None" = None
    stuck_after: int = STUCK_AFTER
    # Timeouts a daemon that reports itself reconnecting may burn before they count. Each one costs a full
    # search timeout, so this is minutes of grace, not attempts.
    RECONNECTS: int = 8
    dead: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._fails = 0
        self._hung = 0
        self._lost = False
        self._reconnects = 0

    @property
    def session_lost(self) -> bool:
        return self._lost

    def answered(self) -> None:
        """One search that came back, whatever it held.

        An answer resets the run of timeouts, because the daemon is talking. What it held decides nothing:
        the count is the network's, and a live daemon answering with nothing is a rare track. The daemon's own
        status is asked here because a lost session is not visible in a search answer at all.
        """
        if self._lost:
            return
        lost = False
        if self.daemon is not None:
            try:
                lost = self.daemon.session_lost()
            except DaemonError:
                lost = False          # the timeout path stops the pass if the daemon has gone
        if lost:
            self._lost = True
            self.dead.set()
            return
        with self._lock:
            self._fails = 0
            self._reconnects = 0

    def timed_out(self) -> None:
        """One search that never answered.

        A daemon that reports its server link down is reconnecting on its own, and a search that cannot be
        forwarded times out exactly like a dead network does. Those are not counted until the daemon has had
        `RECONNECTS` timeouts to come back, because four in a row were ending passes while the account was
        still logged in.
        """
        reconnecting = False
        if self.daemon is not None:
            try:
                reconnecting = self.daemon.disconnected()
            except DaemonError:
                reconnecting = False      # an unanswering daemon is not evidence either way
        with self._lock:
            if reconnecting:
                self._reconnects += 1
                if self._reconnects <= self.RECONNECTS:
                    return
            self._fails += 1
            if self.stuck_after and self._fails >= self.stuck_after and not self.dead.is_set():
                self._hung = self._fails
                self.dead.set()

    @property
    def hung(self) -> int:
        """How many calls in a row timed out, when that is what stopped the pass. Zero means it did not."""
        with self._lock:
            return self._hung


class Pacer:
    """Let searches start no more often than `interval` seconds apart.

    Nothing between this loop and the network keeps to the network's rate, and neither the daemon nor the
    server reports a search being ignored, so the pace is kept here. A gap between two searches rather than a cap
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


def search(daemon: Daemon, record: state.Record, *, later_pass: bool) -> list[Candidate]:
    """Ask the daemon, and read back what it offered.

    A retry pass prefers peers with a free slot, and never the first one, because a filter can eliminate the
    only peer who has the track. When no peer has a free slot the rest are kept: an offer is worth a later
    pass, and dropping them would close the track instead.
    """
    offered = daemon.search(search_query(record.artist, record.title), timeout=SEARCH_TIMEOUT)
    found = [candidate for candidate in (candidate_of(item) for item in offered) if candidate is not None]
    found = found[:MAX_RESULTS]
    if later_pass:
        free = [candidate for candidate in found if candidate.slots]
        if free:
            return free
    return found


def merge(record: state.Record, found: list[Candidate]) -> int:
    """Add what the search found to what the record already holds, without repeating anything."""
    known = {identifier(candidate) for candidate in record.candidates}
    added = [candidate for candidate in found if identifier(candidate) not in known]
    record.candidates.extend(added)
    return len(added)


def await_transfer(
    daemon: Daemon,
    candidate: Candidate,
    *,
    log,
    sleep,
    clock,
) -> tuple[Move, str, Transfer]:
    """Take one candidate as far as it goes. Returns how it ended, where the file is, and what the daemon
    last said about it — the last of those is what the peer's own failure is read from."""
    payload = daemon.download(
        candidate.user,
        candidate.path,
        size=candidate.size_bytes or 0,
        metadata={
            "bitrate": candidate.bitrate,
            "length_seconds": candidate.duration,
            "peer_free_slots": 1 if candidate.slots else 0,
            "peer_upload_speed": candidate.speed,
        },
    )
    report = transfer_of(payload)
    local_path = report.local_path
    log(f"      asking {candidate.user} for {candidate.name}")
    key = (candidate.user, candidate.path)

    last_bytes = 0
    last_change = clock()
    started = last_change
    while True:
        stuck_for = clock() - last_change
        on_disk = bool(local_path) and Path(local_path).is_file()
        decision = move(report, on_disk=on_disk, stuck_for=stuck_for, elapsed=clock() - started)
        if decision is not Move.WAIT:
            return decision, local_path, report

        # The wait is what is left of the budget, never more than one poll: sleeping the interval regardless
        # would keep every budget to the poll's own grid — 36 s on a 10 s poll costs 40 — and would leave a
        # file that landed mid-interval unnoticed for the rest of it.
        budget = budget_for(report)
        sleep(max(0.0, min(POLL_INTERVAL, budget - stuck_for)))
        current = daemon.downloads().get(key)
        if current is None:
            # The daemon has forgotten the transfer and the file is not on disk: nothing is coming.
            return Move.FAILED, local_path, Transfer(status="missing", local_path=local_path)
        report = transfer_of(current)
        local_path = report.local_path or local_path
        if report.received_bytes > last_bytes:
            last_bytes = report.received_bytes
            last_change = clock()


def give_up(daemon: Daemon, candidate: Candidate, log) -> None:
    """Stop the transfer, because the next candidate cannot start while the slots are full."""
    try:
        daemon.cancel(candidate.user, candidate.path)
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
        #
        # With no length at all, `held` answered nothing: there was nothing to compare, not a mismatch. The
        # name and the version are then the evidence there is, and a bare request takes only an original —
        # so the collection's copy is the same track and fetching it again duplicates the library.
        for row in library.rows_for(record.artist, record.title):
            if mix_matches(record.mix, row.mix) and (
                    record.duration_s is None or row.seconds is None):
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
    daemon: Daemon,
    lookup: Lookup,
    *,
    library: Library | None,
    log,
    sleep,
    clock,
    later_pass: bool,
    watch: Watch,
    pacer: Pacer | None = None,
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

    if record.status == state.FOUND_IN_LIBRARY:
        # The collection check already answered this track, and the collection does not change under a pass.
        # Asking again costs a MusicBrainz reference per track per pass: 130 of the 305 tracks in a run on
        # 2026-10-17 were already held, and every pass spent five minutes re-resolving them to reach the same
        # answer before it searched anything.
        return "already_held"

    outcome, upgrade = _prepare(job, record, lookup, library, log)
    if outcome is not None:
        return outcome

    if pacer is not None:
        pacer.wait()
    found = search(daemon, record, later_pass=later_pass)
    # Before the peers are filtered: a search that came back at all is the daemon talking, whatever it held.
    watch.answered()
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
        decision, local_path, report = await_transfer(daemon, candidate, log=log, sleep=sleep,
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

        why = offence(decision, report)
        if decision is Move.ROTATE:
            give_up(daemon, candidate, log)
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
    daemon: Daemon,
    lookup: Lookup,
    *,
    library: Library | None = None,
    log=print,
    sleep=None,
    clock=time.monotonic,
    retry: bool = False,
    stream=None,
) -> Summary:
    """The whole pass: every record the job holds, `workers` of them at a time, writing as it goes.

    The transfers and the waiting on them overlap; the searching does not, and goes out at
    `config.search_interval`, because that rate is the network's and not this loop's.
    """
    # Time spent waiting is time the daemon spends pushing events at us; reading them keeps its socket free
    # while a share is being leeched. A caller that substitutes its own sleep (tests) loses that, and nothing
    # else.
    sleep = sleep or daemon.settle
    # The tracks no pass has asked about yet go first. A daemon that stops early leaves the rest of the list
    # unasked, and a pass that starts at the top of the file spends every attempt on the same head: on
    # 2026-10-16 five passes over 315 tracks, between 20:54 and 22:28, searched the first 91 records over and
    # over and never reached the other 224.
    asked = job.searched()
    records = job.records()
    # Only the order the pass works through is sorted; the records are written back as they were read, so the
    # file a person opens stays in track order and a run does not churn it every pass.
    schedule = sorted(records, key=lambda record: (record.idx in asked, record.idx))
    summary = Summary()
    watch = Watch(daemon=daemon)
    # A daemon that lost its session before this pass started is not worth one search: every answer would be
    # an ordinary success with nothing in it, which is what a tracklist of rare tracks looks like.
    watch.answered()
    bar = progress.Bar(len(schedule), log=log, stream=stream)
    unsearched: list[int] = []

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
        if watch.dead.is_set() or watch.session_lost:
            # The daemon is gone, so the pass stops asking; the record is left exactly as it was, and nothing
            # is written about a search that never happened. Not settled either: the bar counts what the pass
            # got through, and settling these one by one races it through the rest of the list as if it had.
            unsearched.append(record.idx)
            return
        later_pass = retry or bool(record.candidates)
        bar.begin(record.idx, record.label)
        try:
            outcome = one(job, record, daemon, lookup, library=library, log=say, sleep=sleep,
                          clock=clock, later_pass=later_pass, watch=watch, pacer=pacer,
                          ledger=ledger, ignored=summary.ignored)
        except DaemonError as error:
            record.status = state.RETRY_LATER
            outcome = "failed"
            watch.timed_out()
            say(f"[{record.idx}] {record.label}: the server refused — {error}")
            job.append_event("refused", idx=record.idx, error=str(error))
        except Exception as error:          # noqa: BLE001 — one malformed reply must not end the pass
            record.status = state.RETRY_LATER
            outcome = "failed"
            say(f"[{record.idx}] {record.label}: {type(error).__name__} — {error}")
            job.append_event("failed", idx=record.idx, error=f"{type(error).__name__}: {error}")
        getattr(summary, outcome).append(record.idx)
        bar.settle(record.idx, record.label, outcome)
        job.write_records(records)          # a crash costs the tracks in flight and nothing else

    if schedule:
        lookup = one_at_a_time(lookup)
        if pacer is not None:
            say(f"one search every {config.search_interval:g}s"
                " — the network's rate, not this loop's")
        try:
            with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
                list(pool.map(work, schedule))
        except BaseException:
            # The pass is over either way, and a bar left drawn under the traceback outlives it.
            bar.close()
            raise

    if unsearched:
        unsearched.sort()
        summary.unsearched.extend(unsearched)

    if watch.hung or watch.session_lost:
        summary.failed.sort()
        summary.dropped = drop_exhausted(job, log=say)
        summary.failed = [idx for idx in summary.failed if idx not in set(summary.dropped)]
        if watch.session_lost:
            # The daemon answered, and every answer was empty because it had no session to search with.
            # Named here because from the inside this is indistinguishable from the network holding nothing,
            # and a pass that stops on it while returning success is a pass that lies.
            job.append_event("lost_session", unsearched=len(summary.unsearched))
            reason = (
                f"the daemon was left without a session — {len(summary.unsearched)} tracks were never "
                f"searched. Another client has the account, or the server has stopped answering this one, "
                f"and a fresh daemon loses it the same way until the server lets go."
            )
        else:
            job.append_event("server_stuck", consecutive=watch.hung, unsearched=len(summary.unsearched))
            reason = (f"{watch.hung} searches timed out in a row — {len(summary.unsearched)} tracks were "
                      f"never searched. The daemon had stopped answering; the run is open, so a fresh pass "
                      f"picks it up.")
        job.write_status(config)
        bar.close()
        raise ServerStuck(reason, lost_session=watch.session_lost)

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
