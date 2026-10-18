"""What a pass left behind: the four checks, in one look.

`dedupe`, `backfill`, `sweep` and `status --files` each own a check, and each is a verb a person has to
remember. The report names every finding and the verb that would clear it; nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import backfill, duplicates, exporting, importing, library, sweep
from .config import Config
from .state import Job


@dataclass(frozen=True)
class Check:
    """One line of the report: what was found, and the verb that acts on it."""

    label: str
    detail: str = ""
    action: str = ""
    trouble: bool = False


@dataclass
class Report:
    """The four checks, in the order the collection is asked."""

    checks: list[Check] = field(default_factory=list)

    @property
    def trouble(self) -> int:
        return sum(1 for check in self.checks if check.trouble)

    def add(self, label: str, detail: str = "", action: str = "", *, trouble: bool) -> None:
        self.checks.append(Check(label, detail, action, trouble))


def plan(config: Config, job: Job | None) -> Report:
    """Look at the collection and the lanes, and write nothing."""
    report = Report()

    try:
        rekordbox = exporting.named(config, "rekordbox")
    except exporting.UnknownTarget as error:
        report.add("rekordbox", str(error), trouble=True)
    else:
        if rekordbox is None:
            report.add("rekordbox", "not a configured target — its rows are not read", trouble=False)
        else:
            try:
                with importing.database(config) as database:
                    report.checks.extend(_collection(config, database))
            except library.Unreadable as error:
                report.add("rekordbox", str(error), trouble=True)

    left = sweep.find(config)
    report.add("staging", f"{len(left.all)} to clear" if left.all else "",
               "music-agent sweep --apply", trouble=bool(left.all))

    if job is not None:
        try:
            drift = library.unpointed(
                config, claimed=[record.local_path for record in job.records() if record.local_path])
        except (library.Unreadable, exporting.UnknownTarget) as error:
            report.add("lanes", str(error), trouble=True)
        else:
            counted = f"{len(drift.lane)} in a lane, {len(drift.gone)} rows whose file has gone"
            found = bool(drift.lane or drift.gone)
            report.add("lanes", counted if found else "", "music-agent status --files", trouble=found)

    return report


def _collection(config: Config, database) -> list[Check]:
    """The two checks that read the collection's rows."""
    checks: list[Check] = []
    try:
        rows = duplicates.run(config, db=database, apply=False, remove_files=False)
    except duplicates.NotClosed as error:
        checks.append(Check("duplicates", str(error), trouble=True))
    else:
        found = bool(rows.merges)
        checks.append(Check("duplicates", rows.report() if found else "",
                            "music-agent dedupe --apply", trouble=found))

    try:
        gaps = backfill.run(config, db=database, apply=False)
    except backfill.NotClosed as error:
        checks.append(Check("backfill", str(error), trouble=True))
    else:
        found = bool(gaps.candidates)
        checks.append(Check("backfill", gaps.report() if found else "",
                            "music-agent backfill --apply", trouble=found))
    return checks
