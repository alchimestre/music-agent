"""Traktor's half of an import, run by the project that owns it.

Traktor's collection is not this pipeline's to write. `traktor-nml` owns `collection.nml`, its own
configuration and its own rules about which entries may be repointed, and it reads a run's records as data —
the `replaces` field is the whole handshake. So this module does one thing: run that tool if it is installed
here, and report what it said. Nothing about Traktor is decided in this repo.

`plan` writes nothing and `apply` refuses while Traktor is running, so both are safe to call from a run.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from .config import Config


class Missing(RuntimeError):
    """Traktor's tool is not installed on this machine, so its collection is not this pipeline's business."""


class Failed(RuntimeError):
    """The tool ran and would not do what it was asked; its collection is untouched."""


def command(config: Config) -> list[str] | None:
    """The tool's argv, or None when it is not installed here."""
    argv = list(config.traktor_command)
    if not argv or shutil.which(argv[0]) is None:
        return None
    return argv


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
    """Plan, apply if the plan changes anything, and report both.

    Returns what the tool said about the plan, or None when it is not installed. Raises `Failed` when the
    tool exits nonzero, which is how it refuses — Traktor being open, a missing collection. Traktor's own
    step is left to the person: the tracks it added have not been analyzed yet.
    """
    argv = command(config)
    if argv is None:
        log("traktor: not installed on this machine — its collection is untouched")
        return None

    ok, plan = _run(argv, "plan")
    log("traktor: " + plan.replace("\n", "\n         "))
    if not ok:
        raise Failed(f"plan failed · {plan}")
    if nothing_to_do(plan):
        return plan

    ok, said = _run(argv, "apply")
    log("traktor: " + said.replace("\n", "\n         "))
    if not ok:
        raise Failed(f"apply failed · {said}")
    log("traktor: open Traktor and run Check Consistency → Analyze for the tracks it added")
    return plan