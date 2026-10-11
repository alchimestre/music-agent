"""The processing stage: what was downloaded, into the collection, at the right format and loudness.

Ticket 07 puts the writes here, and the map keeps two tools canonical rather than reimplementing them:
`convert_to_aiff.sh` converts, and the normalizer measures and rewrites in place. This stage drives both
and owns what neither knows about — which lane a file belongs in, what it is called once it arrives, and
the order the two tools run in.

Three facts read out of the converter rather than assumed. It keeps a file's **basename** and changes only
the extension, so a peer's `08 - Solution.flac` arrives as `08 - Solution.aiff` and has to be renamed to
the house convention the library already uses. It **carries metadata across** (`-map 0` together with
`-map_metadata 0 -write_id3v2 1`), so the tagging step, when it exists, belongs *before* conversion and its
work survives. And it **deletes its input on success**, so a staged file is consumed rather than left
behind — which is also why it cannot simply be pointed at the staging directory: the lossy files there must
not be converted, and neither must another record's.

Lossy files are never converted (the design notes). They go to the lossy lane as they are, and both lanes are
normalized and imported alike.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import state
from .config import Config
from .rules import LOSSLESS_EXTENSIONS


class ProcessError(RuntimeError):
    """A file could not be taken from staging to the collection."""


class Runner(Protocol):
    """How a canonical tool is invoked. Injected, so the tests can read the command line."""

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess: ...


def execute(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@dataclass
class Summary:
    """What a pass came to, by track."""

    placed: list[int] = field(default_factory=list)
    normalized: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.placed)} placed", f"{len(self.normalized)} normalized"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


# Characters a filesystem will not take, replaced rather than dropped so that two differently punctuated
# titles cannot collapse onto one name.
_UNSAFE = str.maketrans({"/": "-", "\\": "-", ":": "-", "\0": ""})


def filename_for(record: state.Record, suffix: str) -> str:
    """The house convention the library already uses: `Artist - Title`, with the mix in parentheses.

    Ticket 05 measured it — 1457 of 1627 files carry no version marker, and the names are `Artist - Track`.
    A white-label request has no artist, so its title stands alone.
    """
    name = f"{record.artist} - {record.title}" if record.artist else record.title
    if record.mix:
        name = f"{name} ({record.mix})"
    cleaned = " ".join(name.translate(_UNSAFE).split()).strip()
    return (cleaned or "untitled") + suffix.lower()


def convert(config: Config, source: Path, *, run: Runner, log) -> Path:
    """Run the canonical converter over one file, in a directory of its own.

    The script converts everything it finds in the directory it is given, so it is handed a pen holding
    exactly this file. It deletes its input on success; on failure the input is put back, because the
    script does not always survive to clean up after itself.
    """
    with tempfile.TemporaryDirectory(prefix="music-agent-convert-") as pen:
        parked = Path(pen) / source.name
        shutil.move(str(source), parked)
        result = run([str(config.convert_tool), pen, str(source.parent)])
        produced = source.parent / f"{parked.stem}.aiff"
        if result.returncode != 0 or not produced.is_file():
            if parked.exists():
                shutil.move(str(parked), source)
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise ProcessError(f"conversion failed: {detail[-1] if detail else 'no output'}")
        log(f"      converted to {produced.name}")
        return produced


def place(config: Config, record: state.Record, *, run: Runner, log) -> Path:
    """Get one file into the lane it belongs in, under the name the collection uses.

    Returns where the file now is, because everything after this — normalizing, tagging, importing —
    needs the path rather than the lane.
    """
    source = Path(record.local_path)
    if not source.is_file():
        raise ProcessError(f"the staged file is gone: {source}")

    suffix = source.suffix.lower()
    lossless = suffix.lstrip(".") in LOSSLESS_EXTENSIONS
    folder = config.lossless if lossless else config.lossy
    folder.mkdir(parents=True, exist_ok=True)

    if lossless and suffix not in {".aiff", ".aif"}:
        source = convert(config, source, run=run, log=log)

    target = folder / filename_for(record, source.suffix)
    if target.exists():
        # Never overwrite: a name already taken means something the dedupe did not catch, and a quiet
        # replacement would destroy a file rather than report a conflict.
        raise ProcessError(f"a file of that name is already there: {target}")
    shutil.move(str(source), target)
    log(f"[{record.idx}] {record.label}: → {target}")
    return target


def normalize(config: Config, folder: Path, *, run: Runner, log) -> None:
    """Let the canonical normalizer look at the whole directory.

    A directory, not a file, because that is how it keeps its own state (the design notes) — it records what it
    has done inside the directory it is run on, and re-measuring 1283 files to loudness a handful would be
    the wrong trade.
    """
    result = run([
        sys.executable, str(config.normalize_tool), str(folder),
        "--target-lufs", str(config.target_lufs),
        "--true-peak", str(config.true_peak_db),
        "--tolerance", str(config.tolerance_lu),
    ])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ProcessError(f"normalizing {folder} failed: {detail[-1] if detail else 'no output'}")
    log(f"      normalized {folder}")


def run(config: Config, job: state.Job, *, runner: Runner = execute, log=print) -> Summary:
    """Place everything staging holds, then normalize each directory that received something."""
    records = job.records()
    summary = Summary()
    touched: set[Path] = set()
    placed: list[state.Record] = []

    for record in records:
        if record.status != state.DOWNLOADED:
            continue
        try:
            landed = place(config, record, run=runner, log=log)
        except ProcessError as error:
            record.status = state.RETRY_LATER
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("placement_failed", idx=record.idx, error=str(error))
            log(f"[{record.idx}] {record.label}: {error}")
            continue
        record.local_path = str(landed)     # the record follows the file
        record.status = state.CONVERTED
        touched.add(landed.parent)
        placed.append(record)
        summary.placed.append(record.idx)
        job.write_records(records)          # as in acquire: a crash costs the current track

    def in_folder(folder: Path) -> list[state.Record]:
        return [record for record in placed if Path(record.local_path).parent == folder]

    for folder in sorted(touched):
        try:
            normalize(config, folder, run=runner, log=log)
        except ProcessError as error:
            for record in in_folder(folder):
                record.status = state.RETRY_LATER
                record.notes = str(error)
            job.append_event("normalize_failed", folder=str(folder), error=str(error))
            log(f"{folder}: {error}")
            summary.failed.extend(record.idx for record in in_folder(folder))
            continue
        for record in in_folder(folder):
            record.status = state.NORMALIZED
            summary.normalized.append(record.idx)

    job.write_records(records)
    job.write_status(config)
    if not summary.failed:
        job.finish()
    return summary
