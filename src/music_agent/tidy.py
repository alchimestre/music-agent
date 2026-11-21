"""What a pass left behind: the four checks, in one look.

`dedupe`, `backfill`, `sweep` and `status --files` each own a check, and each is a verb a person has to
remember. The report names every finding and the verb that would clear it; nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import backfill, duplicates, exporting, importing, lanes, library, sweep
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

    files = lanes.lane_files(config)
    try:
        rows = _collection(config, files, report)
        paths = None if rows is None else [str(row.path) for row in rows if row.path != Path(".")]
        drift = library.unpointed(config, rekordbox_paths=paths, files=files) if job is not None else None
    except (library.Unreadable, exporting.UnknownTarget) as error:
        report.add("rekordbox", str(error), trouble=True)
        drift = None

    staged = drift.staged if drift is not None else sweep.find(config).all
    report.add("staging", f"{len(staged)} to clear" if staged else "",
               "music-agent sweep --apply", trouble=bool(staged))
    if drift is not None:
        counted = f"{len(drift.lane)} in a lane, {len(drift.gone)} rows whose file has gone"
        found = bool(drift.lane or drift.gone)
        report.add("lanes", counted if found else "", "music-agent status --files", trouble=found)

    return report


def _collection(config: Config, files: list[Path], report: Report) -> list[duplicates.Copy] | None:
    if exporting.named(config, "rekordbox") is None:
        report.add("rekordbox", "not a configured target — its rows are not read", trouble=False)
        return None
    with importing.database(config) as database:
        rows = duplicates.copies(database)
        merged = duplicates.plan(config, db=database, rows=rows)
        gaps = backfill.plan(config, db=database, rows=rows, files=files)
    report.add("duplicates", merged.report() if merged.merges else "",
               "music-agent dedupe --apply", trouble=bool(merged.merges))
    report.add("backfill", gaps.report() if gaps.candidates else "",
               "music-agent backfill --apply", trouble=bool(gaps.candidates))
    return rows
