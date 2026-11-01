"""Traktor's half of an import, run by the project that owns it.

Traktor's collection is not this pipeline's to write. `traktor-nml` owns `collection.nml`, its own
configuration and its own rules about which entries may be repointed, and it reads a run's records as data —
the `replaces` field is the whole handshake. So this module does one thing: run that tool if it is installed
here, and report what it said. Nothing about Traktor is decided in this repo.

`plan` writes nothing and `apply` refuses while Traktor is running, so both are safe to call from a run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config


class Missing(RuntimeError):
    """Traktor's tool is not installed on this machine, so its collection is not this pipeline's business."""


class Failed(RuntimeError):
    """The tool ran and would not do what it was asked; its collection is untouched."""


def command(config: Config) -> list[str] | None:
    """The tool's argv, or None when it is not installed here."""
    argv = list(config.traktor_command)
    if not argv:
        return None
    executable = shutil.which(argv[0])
    if executable is None and argv == ["traktor-nml"]:
        try:
            from .resources import bundled_executable

            bundled = bundled_executable("traktor-nml")
            executable = str(bundled) if bundled is not None else None
        except (ImportError, AttributeError, OSError):
            executable = None
    if executable is None:
        return None
    return [executable, *argv[1:]]


@dataclass(frozen=True)
class Plan:
    destination: str
    changes: str
    requires_confirmation: bool


def destination(config: Config) -> str:
    """A stable identity for the collection the configured command points at."""
    return json.dumps(list(config.traktor_command), separators=(",", ":"))


def plan(config: Config) -> Plan | None:
    """Run the external planner without applying its changes."""
    argv = command(config)
    if argv is None:
        return None
    ok, changes = _run(argv, "plan")
    if not ok:
        raise Failed(f"plan failed · {changes}")
    return Plan(
        destination=destination(config),
        changes=changes,
        requires_confirmation=not nothing_to_do(changes),
    )


def apply(config: Config) -> str:
    """Apply the external plan; its own tool owns the backup and running-app checks."""
    argv = command(config)
    if argv is None:
        raise Missing("traktor-nml is not installed on this machine")
    ok, said = _run(argv, "apply")
    if not ok:
        raise Failed(f"apply failed · {said}")
    return said


def _run(argv: list[str], verb: str, timeout: float = 900.0) -> tuple[bool, str]:
    try:
        done = subprocess.run(argv + [verb], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{verb} did not run: {error}"
    said = "\n".join(part for part in (done.stdout, done.stderr) if part and part.strip())
    return done.returncode == 0, said.strip() or f"{verb} said nothing"


# The tool's plan counts in its own words: `0 to repoint · 0 to add`. Which of those it prints depends on what
# it found, so every numbered count it does print is read, and the plan is nothing to do only when they are
# all zero.
_COUNTED = re.compile(r"(\d+) to ([a-z][a-z ]*)")


def nothing_to_do(plan: str) -> bool:
    """Whether the plan would change nothing. Any count above zero means it would."""
    counted = _COUNTED.findall(plan)
    return bool(counted) and all(int(number) == 0 for number, _ in counted)


def sync(config: Config, *, log=print) -> str | None:
    """Preserve the CLI's plan-then-apply behavior."""
    planned = plan(config)
    if planned is None:
        log("traktor: not installed on this machine — its collection is untouched")
        return None

    log("traktor: " + planned.changes.replace("\n", "\n         "))
    if not planned.requires_confirmation:
        return planned.changes

    said = apply(config)
    log("traktor: " + said.replace("\n", "\n         "))
    log("traktor: open Traktor and run Check Consistency → Analyze for the tracks it added")
    return planned.changes