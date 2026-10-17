"""The peers we will not ask again.

A peer that lists a file, accepts the transfer and then delivers nothing is what a paid mirror or a bot looks
like: the file is advertised, the download is accepted, and no byte ever arrives. One such ending is bad luck;
the same user every pass is worth skipping, so failures are counted per user inside a window and the count
earns an ignore that expires by itself.

State is one JSON file, written whole. The ignore is a timestamp rather than a background job, so a pass that
starts after it expires needs nothing to have happened in between.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# fail2ban's three numbers under its own names, because that is the pattern being followed: so many
# failures inside a window earn a ban, and the ban outlives the window on its own.
MAXRETRY = 3
FINDTIME_DAYS = 14
BANTIME_DAYS = 30

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
VERSION = 1


class Unreadable(RuntimeError):
    """The ledger is there and cannot be read.

    Quietly starting from empty would forget every ban and hand the junk peers a clean slate, which is the
    one failure this file exists to prevent.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime(_STAMP)


def _moment(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ignored_at(until: str, moment: datetime) -> bool:
    """Whether an ignore stamp is still running at `moment`. The one reading of the field, so a pass, a
    report and a test cannot disagree about it."""
    stamp = _moment(until)
    return stamp is not None and stamp > moment


@dataclass(frozen=True)
class Peer:
    """One user, as the ledger has them."""

    user: str
    failures: int
    last: str = ""
    why: str = ""
    until: str = ""

    @property
    def ignored(self) -> bool:
        return _ignored_at(self.until, _now())

    @property
    def days_left(self) -> int | None:
        """Whole days of the ignore still to run, for a report. None when there is no ignore."""
        until = _moment(self.until)
        if until is None:
            return None
        return max(0, (until - _now()).days)


class Ledger:
    """The failures per user, and the ignore a count of them earns.

    Read once, then held in memory: a pass works on twenty tracks at once, so every change is made under one
    lock and the file is rewritten whole from what is in memory.
    """

    def __init__(
        self,
        path: Path,
        *,
        maxretry: int = MAXRETRY,
        findtime_days: int = FINDTIME_DAYS,
        bantime_days: int = BANTIME_DAYS,
    ) -> None:
        self.path = Path(path)
        self.maxretry = max(1, int(maxretry))
        self.findtime = timedelta(days=max(0, int(findtime_days)))
        self.bantime = timedelta(days=max(0, int(bantime_days)))
        self._lock = threading.Lock()
        self._peers = self._read()

    @classmethod
    def at(cls, config) -> "Ledger":
        """The ledger `config` names, under the policy `config` carries."""
        return cls(config.peers, maxretry=config.maxretry, findtime_days=config.findtime_days,
                   bantime_days=config.bantime_days)

    def _read(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the ledger is not a JSON object")
            peers = data.get("peers", {})
        except (OSError, ValueError) as error:
            raise Unreadable(f"cannot read the peer ledger at {self.path} ({error})") from error
        if not isinstance(peers, dict):
            raise Unreadable(f"the peer ledger at {self.path} holds no `peers` object")
        return {str(user): dict(entry) for user, entry in peers.items() if isinstance(entry, dict)}

    def _save(self) -> None:
        # Beside the ledger and renamed onto it, so a pass killed mid-write leaves the previous file whole
        # rather than half of a new one.
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        body = {"version": VERSION, "peers": self._peers}
        temporary = directory / (self.path.name + ".tmp")
        temporary.write_text(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False),
                             encoding="utf-8")
        os.replace(temporary, self.path)

    def _recent(self, entry: dict, moment: datetime) -> list[str]:
        """The stamps still inside the window, which is all a count is made of."""
        return [stamp for stamp in entry.get("failures", [])
                if (when := _moment(stamp)) is not None and moment - when <= self.findtime]

    def offence(self, user: str, why: str, *, now: datetime | None = None) -> bool:
        """Count one failure for `user`. True when it is the one that earns an ignore.

        The offender's own failures are pruned first, so a peer that failed twice a month apart is not
        serving a sentence for a pattern it never had.
        """
        moment = now or _now()
        with self._lock:
            entry = self._peers.setdefault(user, {})
            failures = self._recent(entry, moment)
            failures.append(_stamp(moment))
            entry["failures"] = failures
            entry["why"] = why
            entry["last"] = _stamp(moment)
            earned = len(failures) >= self.maxretry
            if earned:
                entry["until"] = _stamp(moment + self.bantime)
            self._save()
        return earned

    def delivered(self, user: str) -> None:
        """A peer that has just handed over a file is not a mirror, whatever it did before."""
        with self._lock:
            if self._peers.pop(user, None) is not None:
                self._save()

    def ignores(self, user: str, *, now: datetime | None = None) -> bool:
        moment = now or _now()
        with self._lock:
            entry = self._peers.get(user)
            if not entry:
                return False
            until = _moment(str(entry.get("until", "")))
        return until is not None and until > moment

    def ignored(self, *, now: datetime | None = None) -> set[str]:
        """Every peer under an ignore right now."""
        moment = now or _now()
        with self._lock:
            return {user for user, entry in self._peers.items()
                    if _ignored_at(str(entry.get("until", "")), moment)}

    def unban(self, user: str) -> bool:
        """Forget a peer: the ignore and the failures behind it. True when there was something to forget."""
        with self._lock:
            forgotten = self._peers.pop(user, None) is not None
            if forgotten:
                self._save()
        return forgotten

    def peers(self, *, now: datetime | None = None) -> list[Peer]:
        """Every user on the ledger, ignored first, then by how many failures earned the place."""
        moment = now or _now()
        with self._lock:
            snapshot = dict(self._peers)
        rows = []
        for user, entry in snapshot.items():
            rows.append(Peer(
                user=user,
                failures=len(self._recent(entry, moment)),
                last=str(entry.get("last", "")),
                why=str(entry.get("why", "")),
                until=str(entry.get("until", "")),
            ))
        rows.sort(key=lambda row: (not _ignored_at(row.until, moment), -row.failures, row.user))
        return rows
