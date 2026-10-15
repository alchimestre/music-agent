"""Staging, left the way it was found.

A pass is not always tidy about the shell it leaves. A transfer that fails part-way leaves a `.part`, and a
conversion whose placement was refused leaves a file no record points at. Neither is anything a later pass
will pick up again, and both read as work in progress to whoever opens the folder.

Nothing a record names is in the list: that file is a track a pass still owes, and the collection is never
touched at all here. Removing what is left is a dry run until `--apply`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import state
from .config import Config

PART = ".part"


@dataclass(frozen=True)
class Leftovers:
    """What staging holds that the pipeline will not pick up again."""

    partial: tuple[Path, ...]
    loose: tuple[Path, ...]

    def __post_init__(self) -> None:
        # Whatever the caller has to hand — a slice of another Leftovers, a list from a directory listing —
        # is kept as a tuple, because `all` concatenates the two and a list next to a tuple is a TypeError
        # waiting for the one caller who guessed differently.
        object.__setattr__(self, "partial", tuple(self.partial))
        object.__setattr__(self, "loose", tuple(self.loose))

    @property
    def all(self) -> tuple[Path, ...]:
        return self.partial + self.loose

    def report(self) -> str:
        if not self.all:
            return "nothing staging holds is unaccounted for"
        lines = []
        if self.partial:
            lines.append(f"{len(self.partial)} interrupted transfer(s), incomplete by definition:")
            lines += [f"      {path}" for path in self.partial]
        if self.loose:
            lines.append(f"{len(self.loose)} file(s) no record points at:")
            lines += [f"      {path}" for path in self.loose]
        return "\n".join(lines)


def named_by_records(config: Config) -> set[Path]:
    """Every path any run's records name, so a file a pass still owes is never mistaken for debris."""
    named = set()
    for job in state.all_jobs(config):
        for record in job.records():
            if record.local_path:
                named.add(Path(record.local_path).resolve())
    return named


def find(config: Config) -> Leftovers:
    if not config.staging.is_dir():
        return Leftovers((), ())
    named = named_by_records(config)
    partial: list[Path] = []
    loose: list[Path] = []
    for path in sorted(config.staging.iterdir()):
        if path.is_dir() or path.name.startswith("."):     # `.DS_Store` is the Finder's, not the pipeline's
            continue
        if path.name.endswith(PART):
            partial.append(path)
        elif path.resolve() not in named:
            loose.append(path)
    return Leftovers(tuple(partial), tuple(loose))


def clear(leftovers: Leftovers, *, log=print) -> int:
    removed = 0
    for path in leftovers.all:
        try:
            path.unlink()
        except OSError as error:
            log(f"      could not remove {path.name}: {error}")
            continue
        log(f"      removed {path.name}")
        removed += 1
    return removed
