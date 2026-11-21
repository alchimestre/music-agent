"""Traktor's half of an import, run by the project that owns it.

Traktor's collection is not this pipeline's to write. `traktor-nml` owns `collection.nml`, its own
configuration and its own rules about which entries may be repointed, and it reads a run's records as data —
the `replaces` field is the whole handshake. So this module does one thing: run that tool if it is installed
here, and report what it said. Nothing about Traktor is decided in this repo.

`plan` writes nothing and `apply` refuses while Traktor is running, so both are safe to call from a run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import state
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


def verify(config: Config, paths: list[str]) -> dict:
    """Read current per-file collection presence from the collection's owner."""
    argv = command(config)
    if argv is None:
        raise Missing("traktor-nml is not installed on this machine")
    requested = sorted({str(Path(path).resolve()) for path in paths})
    try:
        done = subprocess.run(argv + ["verify", "--json", *requested],
                              capture_output=True, text=True, timeout=900.0)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failed(f"verify did not run: {error}") from error
    if done.returncode:
        raise Failed(f"verify failed · {done.stderr.strip() or done.stdout.strip()}")
    try:
        payload = json.loads(done.stdout)
    except ValueError as error:
        raise Failed("traktor-nml returned invalid verification JSON") from error
    return _verified_response(payload, requested)


def _verified_response(payload, requested: list[str]) -> dict:
    if not isinstance(payload, dict):
        raise Failed("traktor-nml returned invalid collection verification")
    collection = payload.get("collection")
    paths = payload.get("paths")
    if not isinstance(collection, str) or not Path(collection).is_absolute():
        raise Failed("traktor-nml verification has no absolute collection identity")
    if (not isinstance(paths, dict) or set(paths) != set(requested)
            or any(type(present) is not bool for present in paths.values())):
        raise Failed("traktor-nml verification must report every requested path as a boolean")
    return payload


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
# Left beside a staged backup so a kill mid-apply leaves the original location recoverable.
MARKER = ".music-agent-replaces"


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
    log("traktor: open Traktor and run Check Consistency → Analyze for the tracks whose audio was replaced")
    return planned.changes


def replacements(config: Config, records: list[state.Record]) -> dict[Path, Path]:
    """Name the old audio that must leave the lane for Traktor to plan a repoint."""
    originals = {}
    for record in records:
        if not record.replaces:
            continue
        old = Path(record.replaces)
        if not old.is_file():
            continue
        new = Path(record.local_path).resolve()
        if (old.is_symlink() or not old.resolve().is_relative_to(config.lossy.resolve())
                or not new.is_file() or not new.is_relative_to(config.lossless.resolve())
                or old.samefile(new)):
            raise Failed(f"unsafe Traktor replacement: {old} → {new}")
        originals[old.resolve()] = new
    return originals


def sync_replacements(config: Config, originals: dict[Path, Path], paths: list[str], *,
                      collection: str | None = None, log=print) -> tuple[str, dict]:
    with stage_replacements(originals):
        planned = plan(config)
        if planned is None:
            raise Missing("traktor-nml is not installed on this machine")
        log("traktor: " + planned.changes.replace("\n", "\n         "))
        said = apply(config)
        log("traktor: " + said.replace("\n", "\n         "))
        verified = verify(config, paths)
        if collection is not None and verified["collection"] != collection:
            raise Failed("Traktor collection identity changed after the approved plan")
        if any(not verified["paths"][str(new)] for new in originals.values()):
            raise Failed("Traktor did not verify every replacement file")
    log("traktor: open Traktor and run Check Consistency → Analyze for the tracks whose audio was replaced")
    return planned.changes, verified


@contextmanager
def stage_replacements(originals: dict[Path, Path]):
    """Hide predecessors from Traktor until apply and verification finish."""
    staged: dict[Path, Path] = {}
    try:
        for old in originals:
            directory = Path(tempfile.mkdtemp(prefix=".music-agent-replaces-", dir=old.parent))
            backup = directory / old.name
            try:
                (directory / MARKER).write_text(str(old), encoding="utf-8")
                old.rename(backup)
            except OSError:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            staged[old] = backup
        yield
    except BaseException as error:
        failures = []
        for old, backup in staged.items():
            try:
                os.link(backup, old)
                _discard_staged(backup)
            except OSError as restore:
                failures.append(f"{old} (backup {backup}): {restore}")
        if failures:
            raise Failed("could not restore replaced audio: " + "; ".join(failures)) from error
        raise
    for backup in staged.values():
        try:
            _discard_staged(backup)
        except OSError as error:
            raise Failed(f"could not remove staged audio {backup}: {error}") from error


def _discard_staged(backup: Path) -> None:
    (backup.parent / MARKER).unlink(missing_ok=True)
    backup.unlink()
    backup.parent.rmdir()