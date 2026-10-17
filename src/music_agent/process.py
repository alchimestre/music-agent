"""The processing stage: staged downloads into the collection, identified and tagged on the way.

Identification and tagging run **ahead of** conversion, because the converter carries metadata across, so the
fingerprint is of the file as downloaded. Lossy files are never converted; both lanes are treated alike.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from . import genre, identify, state, tag
from .config import Config
from .rules import LOSSLESS_EXTENSIONS


class ProcessError(RuntimeError):
    """A file could not be taken from staging to the collection."""


class Unreadable(ProcessError):
    """The file is not audio this machine can decode — a download the peer delivered damaged."""


# Identification is a fingerprint, an AcoustID lookup and a MusicBrainz lookup per file: network waits, not
# CPU. MusicBrainz keeps its one request a second behind its own lock, so more here only queues there.
IDENTIFIERS = 4


class NameTaken(ProcessError):
    """The lane already holds a file of that name, so this copy cannot be placed there."""

    def __init__(self, target: Path) -> None:
        super().__init__(f"a file of that name is already there: {target}")
        self.target = target


class Runner(Protocol):
    """How a canonical tool is invoked. Injected, so the tests can read the command line."""

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess: ...


def execute(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _failure_detail(result: subprocess.CompletedProcess) -> str:
    """The last thing the tool said, for a failure message."""
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[-1] if lines else "no output"


@dataclass
class Tools:
    """What the stage reaches outside itself with, so a test can hand it fakes."""

    runner: Runner = execute
    hearing: identify.Hearing | None = None
    matches: identify.Matches | None = None
    brainz: identify.Brainz | None = None
    discogs: identify.Discogs | None = None
    vocabulary: Sequence[str] = field(default_factory=list)
    mappings: Mapping[str, str] = field(default_factory=dict)

    @property
    def identifies(self) -> bool:
        return self.hearing is not None and self.matches is not None and self.brainz is not None

    @classmethod
    def live(cls, config: Config, *, vocabulary: Sequence[str] = (),
             mappings: Mapping[str, str] | None = None) -> Tools:
        hearing, matches, brainz, discogs = identify.live(config)
        return cls(hearing=hearing, matches=matches, brainz=brainz, discogs=discogs,
                   vocabulary=list(vocabulary), mappings=mappings or {})


@dataclass
class Summary:
    """What a pass came to, by track."""

    placed: list[int] = field(default_factory=list)
    normalized: list[int] = field(default_factory=list)
    tagged: list[int] = field(default_factory=list)
    ungenred: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.placed)} placed", f"{len(self.tagged)} tagged"]
        if self.ungenred:
            parts.append(f"{len(self.ungenred)} without a genre")
        if self.already:
            parts.append(f"{len(self.already)} the collection already had")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


# Characters a filesystem will not take, replaced rather than dropped so that two differently punctuated
# titles cannot collapse onto one name.
_UNSAFE = str.maketrans({"/": "-", "\\": "-", ":": "-", "\0": ""})


def filename_for(record: state.Record, suffix: str) -> str:
    """The house convention the library already uses: `Artist - Title`, with the mix in parentheses.

    A white-label request has no artist, so its title stands alone.
    """
    name = f"{record.artist} - {record.title}" if record.artist else record.title
    if record.mix:
        name = f"{name} ({record.mix})"
    cleaned = " ".join(name.translate(_UNSAFE).split()).strip()
    return (cleaned or "untitled") + suffix.lower()


def tags_for(found: identify.Identified, *, genre_name: str = "") -> tag.Tags:
    """The fields a tag carries, and only those: never BPM or key."""
    return tag.Tags(
        title=found.title, artist=found.artist, album=found.album,
        album_artist=found.album_artist, year=found.year, label=found.label,
        catalog_number=found.catalog_number, genre=genre_name,
        recording_id=found.recording_id, release_id=found.release_id,
        release_group_id=found.release_group_id,
    )


def identify_one(record: state.Record, tools: Tools, log) -> identify.Identified:
    """What the file is, or what its name says it is when nothing can be asked."""
    if not tools.identifies:
        return identify.from_filename(record.request)
    try:
        return identify.identify(Path(record.local_path), record.request,
                                 hearing=tools.hearing, matches=tools.matches, brainz=tools.brainz,
                                 discogs=tools.discogs, log=log)
    except Exception as error:          # a network or a service is not a reason to lose the track
        log(f"      could not identify it ({type(error).__name__}) — the name will have to do")
        # `asked=False`: nothing was consulted, so nothing was answered — which a caller that writes a genre
        # has to tell apart from a service that answered with nothing.
        return replace(identify.from_filename(record.request), asked=False)


def apply_genre(record: state.Record, found: identify.Identified, tools: Tools, log) -> genre.Choice:
    """Resolve the genre against the collection's own vocabulary, or leave it blank.

    Discogs styles come first and MusicBrainz genres second: that order was measured, and it is not what one
    would guess — MusicBrainz's own genres had `electronic` claiming ten different library genres.
    """
    offered = list(found.styles) + list(found.genres)
    if tools.vocabulary:
        choice = genre.resolve(offered, vocabulary=tools.vocabulary, mappings=tools.mappings)
    else:
        choice = genre.Choice(considered=tuple(offered))

    record.genre = choice.genre
    if choice.genre:
        # 2026-10-15: the winner alone hid what else the services offered, and a rule that reads a track's
        # source genres off the run — the retag pass is the only place they are ever named — cannot match a
        # name that lost to a more specific one.
        log(f"      genre {choice.genre!r}, from {choice.source!r} (offered {offered})")
    elif offered:
        log(f"      nothing in the table for {offered} — the caller decides this one")
    else:
        # 2026-10-13: silence here read as a missing answer. The run's log is the only record of what a
        # track's source genres were — the written tag keeps the family and nothing else — so every track
        # needs a line, including the ones the services had nothing to say about.
        log("      no genre offered — MusicBrainz and Discogs had nothing for it")
    return choice


def convert(config: Config, source: Path, *, run: Runner, log) -> Path:
    """Run the canonical converter over one file, in a directory of its own.

    The script converts everything it finds in the directory it is given, so it is handed a pen holding
    exactly this file. It deletes its input on success; on failure the input is put back, because the script
    does not always survive to clean up after itself.
    """
    with tempfile.TemporaryDirectory(prefix="music-agent-convert-") as pen:
        parked = Path(pen) / source.name
        shutil.move(str(source), parked)
        result = run([str(config.convert_tool), pen, str(source.parent)])
        produced = source.parent / f"{parked.stem}.aiff"
        if result.returncode != 0 or not produced.is_file():
            if parked.exists():
                shutil.move(str(parked), source)
            raise ProcessError(f"conversion failed: {_failure_detail(result)}")
        log(f"      converted to {produced.name}")
        return produced


def discard(record: state.Record) -> Path:
    """Delete a copy the collection already has.

    The row it would have become does not exist, so nothing points at the file, and one left in staging reads
    as work in progress to whoever opens the folder.
    """
    source = Path(record.local_path)
    source.unlink()
    return source


def decodable(source: Path, *, run: Runner) -> bool:
    """Whether ffmpeg can open the file at all, before anything is spent converting it."""
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=format_name",
                  "-of", "csv=p=0", str(source)])
    return result.returncode == 0 and bool((result.stdout or "").strip())


def place(config: Config, record: state.Record, *, run: Runner, log) -> Path:
    """Get one file into the lane it belongs in, under the name the collection uses."""
    source = Path(record.local_path)
    if not source.is_file():
        raise ProcessError(f"the staged file is gone: {source}")
    if not decodable(source, run=run):
        # A peer that delivers something the decoder cannot open has delivered nothing. Caught before the
        # converter so the failure is the file's, not the tool's: a failed conversion keeps its download
        # (the converter deletes its input), which left a corrupt .flac in staging with a record pointing at
        # it and no pass that would ever look again.
        raise Unreadable(f"the file is not audio ffmpeg can read: {source.name}")

    suffix = source.suffix.lower()
    lossless = suffix.lstrip(".") in LOSSLESS_EXTENSIONS
    folder = config.lossless if lossless else config.lossy
    folder.mkdir(parents=True, exist_ok=True)

    if lossless and suffix not in {".aiff", ".aif"}:
        source = convert(config, source, run=run, log=log)
        record.local_path = str(source)     # the converted file is the one that will be placed, or parked

    # One extension for one format: pyrekordbox's `add_content` upper-cases the suffix and has no `AIF`
    # member, so a `.aif` copy is renamed as it is placed, never converted — the audio is already what the
    # collection wants.
    target = folder / filename_for(record, ".aiff" if source.suffix.lower() == ".aif" else source.suffix)
    if target.exists():
        # Never overwrite: a name already taken is something the dedupe did not catch, and a quiet
        # replacement would destroy a file rather than report a conflict. The caller deletes this copy.
        raise NameTaken(target)
    shutil.move(str(source), target)
    log(f"[{record.idx}] {record.label}: → {target}")
    return target


def normalize(config: Config, folder: Path, *, run: Runner, log) -> None:
    """Let the canonical normalizer look at the whole directory.

    A directory, not a file, because that is where it keeps its own state: re-measuring a whole lane to
    loudness a handful of files would be the wrong trade.
    """
    result = run([
        sys.executable, str(config.normalize_tool), str(folder),
        "--target-lufs", str(config.target_lufs),
        "--true-peak", str(config.true_peak_db),
        "--tolerance", str(config.tolerance_lu),
    ])
    if result.returncode != 0:
        raise ProcessError(f"normalizing {folder} failed: {_failure_detail(result)}")
    log(f"      normalized {folder}")


@dataclass
class Retagged:
    """What a re-tag pass came to, by track."""

    retagged: list[int] = field(default_factory=list)   # a genre was resolved and written
    cleared: list[int] = field(default_factory=list)    # the old name came off and nothing replaced it
    unasked: list[int] = field(default_factory=list)    # no service could be reached, so nothing was written
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.retagged)} re-tagged"]
        if self.cleared:
            parts.append(f"{len(self.cleared)} cleared")
        if self.unasked:
            parts.append(f"{len(self.unasked)} left alone, the services unreachable")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


def retag(job: state.Job, *, tools: Tools | None = None, log=print) -> Retagged:
    """Re-derive the genre on tracks already placed, and write it even though one is there.

    A run never re-genres a track, because a genre cannot be undone after import, so changing one has to be
    deliberate: the table has changed, and the tracks tagged under the old one carry names the collection no
    longer has. When the services answer that nothing resolves, the old name comes off, because the import
    reads the *file*; when they cannot be reached at all, nothing is written.
    """
    tools = tools or Tools()
    summary = Retagged()
    records = job.records()
    for record in records:
        if record.status not in {state.NORMALIZED, state.TAGGED, state.IMPORTED}:
            continue
        path = Path(record.local_path)
        if not path.is_file():
            record.notes = f"the file is gone: {path}"
            summary.failed.append(record.idx)
            log(f"[{record.idx}] {record.label}: {record.notes}")
            job.write_records(records)
            continue

        found = identify_one(record, tools, log)
        if not found.asked:
            # 2026-10-15: a service that could not be reached is not an answer, and the blank it leaves is not
            # the taxonomy's verdict. Two passes over the same 287 records disagreed on 23 of them, every one a
            # lookup that failed; the second pass took off names the first had read off the same services. So
            # nothing is written here, and the record keeps what its file says.
            record.genre = tag.read(path).genre
            record.notes = "the services could not be asked — its genre stands"
            summary.unasked.append(record.idx)
            log(f"[{record.idx}] {record.label}: the services could not be asked — "
                f"{(record.genre or 'no genre')!r} stands")
            job.write_records(records)
            continue
        # No ungenred event here: those names were counted when the run first genred these tracks, and
        # counting them again would inflate a genre proposal rather than inform one.
        apply_genre(record, found, tools, log)
        try:
            tag.write(path, tags_for(found, genre_name=record.genre), replace_genre=True, log=log)
        except tag.TagError as error:
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("retag_failed", idx=record.idx, error=str(error))
            log(f"[{record.idx}] {record.label}: {error}")
            job.write_records(records)
            continue

        record.genre = tag.read(path).genre    # the record follows the file, as it does in a run
        record.notes = ""
        (summary.retagged if record.genre else summary.cleared).append(record.idx)
        job.append_event("retagged", idx=record.idx, path=str(path), genre=record.genre)
        job.write_records(records)             # a crash costs the current track and nothing else
    return summary


@dataclass
class Reconciled:
    """What a reconcile pass came to, by track."""

    reconciled: list[int] = field(default_factory=list)   # the record was set from its file
    already: list[int] = field(default_factory=list)      # already agreed with its file
    missing: list[int] = field(default_factory=list)      # downloaded, but no file of that name to find

    def report(self) -> str:
        parts = [f"{len(self.reconciled)} rebuilt from their files"]
        if self.already:
            parts.append(f"{len(self.already)} already agreed")
        if self.missing:
            parts.append(f"{len(self.missing)} downloaded but not found in a lane")
        return " · ".join(parts)


def _downloaded(config: Config, job: state.Job) -> set[int]:
    """The indices this run fetched, read from its own events."""
    fetched: set[int] = set()
    for line in (config.state_root / job.id / state.EVENTS).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "downloaded" and event.get("idx") is not None:
            fetched.add(event["idx"])
    return fetched


def _in_lanes(config: Config) -> dict[str, Path]:
    """Both lanes in one scan, by stem. Lossless first: when both hold a stem that copy is the upgrade."""
    on_disk: dict[str, Path] = {}
    for lane in (config.lossless, config.lossy):
        if not lane.is_dir():
            continue
        for path in sorted(lane.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                on_disk.setdefault(path.stem, path)
    return on_disk


def reconcile(config: Config, job: state.Job, *, log=print) -> Reconciled:
    """Rebuild the records from the files this run downloaded.

    Only a record whose `downloaded` event is in the run's own events can have left a file in a lane, so
    matching by name across the whole library instead is what once pulled already-held tracks into a run's
    records. A record whose file is not on disk is reported, never guessed at.
    """
    summary = Reconciled()
    fetched = _downloaded(config, job)
    on_disk = _in_lanes(config)

    records = job.records()
    doomed: list[int] = []
    for record in records:
        if record.idx not in fetched:
            continue
        path = on_disk.get(filename_for(record, ""))
        if path is None:
            summary.missing.append(record.idx)
            log(f"[{record.idx}] {record.label}: downloaded, but no file of that name in a lane")
            continue
        written = tag.read(path)
        if record.status == state.IMPORTED:
            # The file in the lane is not the whole truth about a track the import has finished — the
            # collection's row is. Rebuilt from the file alone, every imported track came back as merely
            # tagged, and the next import then had to find all of them again.
            status = state.IMPORTED
        else:
            status = state.TAGGED if written.title else state.NORMALIZED
        if record.local_path == str(path) and record.status == status and record.genre == written.genre:
            summary.already.append(record.idx)
            continue
        summary.reconciled.append(record.idx)
        log(f"[{record.idx}] {record.label}: {status} · {path.parent.name}/{path.name} · "
            f"{written.genre or '(no genre)'}")
        record.local_path = str(path)
        record.status = status
        record.genre = written.genre
        record.notes = ""
        if record.replaces and Path(record.replaces) == path:
            doomed.append(record.idx)

    if doomed:
        # Refused rather than written: the pair means the import would delete the file it just made a row
        # point at, and a live music file is not worth the risk of trusting the caller got this right.
        raise ProcessError(
            f"refusing to write: {len(doomed)} records would point their row at the file this run made, "
            f"which is the pair the import deletes afterwards: {doomed[:5]}")

    job.write_records(records)
    job.append_event("reconciled", records=summary.reconciled, missing=summary.missing)
    return summary


def run(config: Config, job: state.Job, *, tools: Tools | None = None, log=print) -> Summary:
    """Everything staging holds: identified, genred, converted, placed, normalized, tagged."""
    tools = tools or Tools()
    records = job.records()
    summary = Summary()
    placed: list[state.Record] = []
    found: dict[int, identify.Identified] = {}

    downloads = [record for record in records if record.status == state.DOWNLOADED]
    if downloads:
        def identified(record: state.Record) -> identify.Identified:
            log(f"[{record.idx}] {record.label}: identifying")
            return identify_one(record, tools, log)

        with ThreadPoolExecutor(max_workers=min(IDENTIFIERS, len(downloads))) as pool:
            answers = pool.map(identified, downloads)
            found = dict(zip((record.idx for record in downloads), answers))

    for record in downloads:
        try:
            choice = apply_genre(record, found[record.idx], tools, log)
            if not choice.found and choice.considered:
                # Counted so that a name fitting nothing on enough tracks can be proposed as a genre rather
                # than staying blank for ever.
                job.append_event(state.UNGENERED_EVENT, idx=record.idx,
                                 names=list(choice.considered))
            landed = place(config, record, run=tools.runner, log=log)
        except NameTaken as error:
            # The lane holds that name, and `held` never skips a track whose length was never resolved, so
            # this is a copy the collection already has. It is deleted, and the record settles: nothing keeps
            # a second copy of anything.
            gone = discard(record)
            record.local_path = ""
            record.status = state.FOUND_IN_LIBRARY
            record.notes = f"{error} — this copy was removed"
            summary.already.append(record.idx)
            job.append_event("already_held", idx=record.idx, held=str(error.target), removed=str(gone))
            log(f"[{record.idx}] {record.label}: the collection already holds that name — "
                f"{gone.name} removed")
            job.write_records(records)
            continue
        except Unreadable as error:
            # Nothing can use this copy, so it goes: a file in staging reads as work in progress, and the
            # record would otherwise carry a path to a file no stage will take. Clearing the path lets a
            # later pass ask the network again.
            gone = Path(record.local_path).unlink(missing_ok=True) if record.local_path else None
            record.local_path = ""
            record.status = state.RETRY_LATER
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("placement_failed", idx=record.idx, error=str(error), removed=gone is None)
            log(f"[{record.idx}] {record.label}: {error}")
            continue
        except ProcessError as error:
            record.status = state.RETRY_LATER
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("placement_failed", idx=record.idx, error=str(error))
            log(f"[{record.idx}] {record.label}: {error}")
            continue
        record.local_path = str(landed)     # the record follows the file
        record.status = state.CONVERTED
        placed.append(record)
        summary.placed.append(record.idx)
        job.write_records(records)          # a crash costs the current track and nothing else

    for folder in sorted({Path(record.local_path).parent for record in placed}):
        in_lane = [record for record in placed if Path(record.local_path).parent == folder]
        try:
            normalize(config, folder, run=tools.runner, log=log)
        except ProcessError as error:
            for record in in_lane:
                record.status = state.RETRY_LATER
                record.notes = str(error)
            job.append_event("normalize_failed", folder=str(folder), error=str(error))
            log(f"{folder}: {error}")
            summary.failed.extend(record.idx for record in in_lane)
            continue
        for record in in_lane:
            record.status = state.NORMALIZED
            summary.normalized.append(record.idx)

    for record in placed:
        if record.status != state.NORMALIZED:
            continue
        try:
            written = tag.write(Path(record.local_path),
                                tags_for(found[record.idx], genre_name=record.genre), log=log)
        except tag.TagError as error:
            record.status = state.RETRY_LATER
            record.notes = str(error)
            summary.failed.append(record.idx)
            job.append_event("tagging_failed", idx=record.idx, error=str(error))
            log(f"[{record.idx}] {record.label}: {error}")
            continue
        if record.genre and "genre" not in written:
            # An existing genre is never replaced, so the record has to follow the file rather than claim a
            # genre the file does not carry — the import reads the record, not the tag.
            kept = tag.read(Path(record.local_path)).genre
            offered = list(found[record.idx].styles) + list(found[record.idx].genres)
            log(f"      it already carried a genre, so {kept!r} stays, not {record.genre!r} "
                f"(which came from {offered})")
            record.genre = kept
        record.status = state.TAGGED
        summary.tagged.append(record.idx)
        if not record.genre:
            summary.ungenred.append(record.idx)

    job.write_records(records)
    job.write_status(config)
    # As in acquire: the run is finished by the import, which is where the pipeline ends.
    return summary
