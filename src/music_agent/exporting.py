"""The export seam: a collection this run writes to.

`import` is a loop over configured targets, not a rekordbox step followed by a Traktor step. A target says
whether it is installed, what it can read, how to add a run's tracks, and what to call it. Nothing above this
module names a collection.

`rekordbox` is the one that owns a database; `traktor` delegates to the project that owns `collection.nml`.
A third target is a class here and a name in `[export] targets`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import Config
from . import state


class UnknownTarget(ValueError):
    """A configured name that no target answers to."""


@dataclass
class Summary:
    """What one target did, in its own words."""

    lines: list[str] = field(default_factory=list)
    failed: int = 0
    ran: bool = True

    def report(self) -> str:
        return " · ".join(line for line in self.lines if line)


class Target(Protocol):
    """One collection a run can be exported to."""

    name: str
    reads: bool

    def available(self, config: Config) -> bool:
        """Whether this machine can write it now."""

    def rows(self, config: Config) -> tuple[list, list[str]]:
        """What the collection holds, and its genre vocabulary. Only `reads` targets answer."""

    def import_run(self, config: Config, job: state.Job, *,
                   backup_first: bool = True, log=print) -> Summary:
        """Add the run's tagged tracks, and report what happened."""


class Rekordbox:
    """rekordbox's collection, through pyrekordbox."""

    name = "rekordbox"
    reads = True

    def available(self, config: Config) -> bool:
        # pyrekordbox is a dependency of this package, so the library is always importable here; whether the
        # database itself reads is `rows`' question, and its failure must not look like an empty collection.
        return True

    def rows(self, config: Config) -> tuple[list, list[str]]:
        from . import library

        try:
            return library.read_database()
        except Exception as error:              # pyrekordbox raises its own types and its own messages
            raise library.Unreadable(
                f"cannot read the rekordbox collection ({error}) — is rekordbox installed, and is the "
                "database where pyrekordbox expects it?"
            ) from error

    def import_run(self, config: Config, job: state.Job, *,
                   backup_first: bool = True, log=print) -> Summary:
        from . import importing

        with importing.database(config) as database:
            result = importing.run(config, job, db=database, backup_first=backup_first, log=log)
        return Summary(lines=[result.report()], failed=len(result.failed))


class Traktor:
    """Traktor's collection, written by the project that owns it."""

    name = "traktor"
    reads = False

    def available(self, config: Config) -> bool:
        from . import traktor

        return traktor.command(config) is not None

    def rows(self, config: Config) -> tuple[list, list[str]]:
        return [], []

    def import_run(self, config: Config, job: state.Job, *,
                   backup_first: bool = True, log=print) -> Summary:
        from . import traktor

        try:
            plan = traktor.sync(config, log=log)
        except traktor.Failed as error:
            return Summary(lines=[str(error)], failed=1, ran=False)
        return Summary(lines=[plan] if plan else [], ran=plan is not None)


REGISTRY: dict[str, Target] = {
    "rekordbox": Rekordbox(),
    "traktor": Traktor(),
}


def configured(config: Config, *, only: list[str] | None = None,
               without: list[str] | None = None) -> list[Target]:
    """The targets this run uses, in configured order.

    `only` narrows to the names given; `without` removes names. A name neither hits is refused rather than
    silently ignored, because a typo would otherwise mean an export that did not happen.
    """
    names = list(only) if only else list(config.export_targets)
    skipped = list(without or ())
    unknown = [name for name in names + skipped if name not in REGISTRY]
    if unknown:
        raise UnknownTarget(
            f"no export target named {', '.join(unknown)} — this build has "
            f"{', '.join(sorted(REGISTRY))}"
        )
    return [REGISTRY[name] for name in names if name not in skipped]


def named(config: Config, name: str) -> Target | None:
    """The configured target called `name`, when this machine can write it."""
    for target in configured(config):
        if target.name == name and target.available(config):
            return target
    return None


def run(config: Config, job: state.Job, *, targets: list[Target] | None = None,
        backup_first: bool = True, log=print) -> list[Summary]:
    """Import into every configured target, and finish the run only when all of them have.

    A target that is not installed is reported and skipped: it is a fact about the machine, not a failure of
    the tracks. If nothing ran at all, the run stays open, because no collection received it.
    """
    results: list[Summary] = []
    for target in (targets if targets is not None else configured(config)):
        if not target.available(config):
            log(f"{target.name}: not installed on this machine — skipped")
            results.append(Summary(lines=["not installed on this machine"], ran=False))
            continue
        results.append(target.import_run(config, job, backup_first=backup_first, log=log))

    failed = sum(result.failed for result in results)
    if any(result.ran for result in results) and not failed:
        job.finish()
    job.write_status(config)
    return results
