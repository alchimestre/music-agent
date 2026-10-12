"""The processing stage: what was downloaded, into the collection, identified and tagged on the way.

Ticket 07 puts the writes here, and the map keeps two tools canonical rather than reimplementing them:
`convert_to_aiff.sh` converts, and the normalizer measures and rewrites in place. This stage drives both,
identifies and tags, and owns what none of them knows about — which lane a file belongs in, what it is
called once it arrives, and the order everything runs in.

The order is not the one the map lists, and the reason is in the converter: it carries metadata across
(`-map_metadata 0` with `-write_id3v2 1`), so anything written before it survives. Identification and
tagging therefore run **ahead of** conversion, which also means the fingerprint is taken from the file as
it was downloaded rather than from a re-encode of it.

Three facts read out of the converter rather than assumed. It keeps a file's **basename** and changes only
the extension, so a peer's `08 - Solution.flac` arrives as `08 - Solution.aiff` and has to be renamed to
the house convention the library already uses. It **carries metadata across**, as above. And it **deletes
its input on success**, which is why it cannot simply be pointed at the staging directory: the lossy files
there must not be converted, and neither must another record's.

Lossy files are never converted (the design notes). They go to the lossy lane as they are, and both lanes are
identified, genred, tagged and imported alike.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import genre, identify, state, tag
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
class Tools:
    """What the stage reaches outside itself with, so a test can hand it fakes.

    Every seam is optional. Without the identification ones the stage still converts, places and
    normalizes — which is what it did before it could identify anything.
    """

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
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.placed)} placed", f"{len(self.tagged)} tagged"]
        if self.ungenred:
            parts.append(f"{len(self.ungenred)} without a genre")
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


def tags_for(found: identify.Identified, *, genre_name: str = "") -> tag.Tags:
    """Ticket 07's field list, and only that list: never BPM or key."""
    return tag.Tags(
        title=found.title, artist=found.artist, album=found.album,
        album_artist=found.album_artist, year=found.year, label=found.label,
        catalog_number=found.catalog_number, genre=genre_name,
        recording_id=found.recording_id, release_id=found.release_id,
        release_group_id=found.release_group_id,
    )


def identify_one(record: state.Record, tools: Tools, log) -> identify.Identified:
    """What the file is, or what its name says it is when nothing can be asked (the design notes).

    A service being unreachable is deliberately not fatal: the track still has a name, and the tags it
    can carry from that name are better than no track at all.
    """
    if not tools.identifies:
        return identify.from_filename(record.request)
    try:
        return identify.identify(Path(record.local_path), record.request,
                                 hearing=tools.hearing, matches=tools.matches, brainz=tools.brainz,
                                 discogs=tools.discogs, log=log)
    except Exception as error:          # a network or a service is not a reason to lose the track
        log(f"      could not identify it ({type(error).__name__}) — the name will have to do")
        return identify.from_filename(record.request)


def apply_genre(record: state.Record, found: identify.Identified, tools: Tools, log) -> genre.Choice:
    """Resolve the genre against the collection's own vocabulary, or leave it blank (the design notes).

    Discogs styles come first and MusicBrainz genres second: that order was measured, and it is not what
    one would guess. MusicBrainz's own genres had `electronic` claiming ten different library genres.
    """
    offered = list(found.styles) + list(found.genres)
    if tools.vocabulary:
        choice = genre.resolve(offered, vocabulary=tools.vocabulary, mappings=tools.mappings)
    else:
        choice = genre.Choice(considered=tuple(offered))

    record.genre = choice.genre
    if choice.genre:
        log(f"      genre {choice.genre!r}, from {choice.source!r}")
    elif offered:
        log(f"      nothing in the table for {offered} — the working procedure decides this one")
    return choice


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


@dataclass
class Retagged:
    """What a re-tag pass came to, by track."""

    retagged: list[int] = field(default_factory=list)   # a genre was resolved and written
    cleared: list[int] = field(default_factory=list)    # the old name came off and nothing replaced it
    failed: list[int] = field(default_factory=list)

    def report(self) -> str:
        parts = [f"{len(self.retagged)} re-tagged"]
        if self.cleared:
            parts.append(f"{len(self.cleared)} cleared")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return " · ".join(parts)


def retag(config: Config, job: state.Job, *, tools: Tools | None = None, log=print) -> Retagged:
    """Re-derive the genre on tracks already placed, and write it even though one is there (the design notes).

    Ticket 10 stops a *run* from re-genring a track, because a genre cannot be undone after import and
    changing one therefore has to be deliberate. This is the deliberate half of that rule: the table has
    changed, and the tracks tagged under the old one carry names the collection no longer has. The genre is
    resolved again from the services and written over whatever the file holds; when nothing resolves, the
    old name is taken off, because the import reads the *file* and would mint a row for it.
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
        # No ungenred event here, deliberately: the design notes counts names across runs, and these were counted
        # the first time round. Counting them again would inflate a proposal rather than inform one.
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


def run(config: Config, job: state.Job, *, tools: Tools | None = None, log=print) -> Summary:
    """Everything staging holds: identified, genred, converted, placed, normalized, tagged."""
    tools = tools or Tools()
    records = job.records()
    summary = Summary()
    touched: set[Path] = set()
    placed: list[state.Record] = []
    found: dict[int, identify.Identified] = {}

    for record in records:
        if record.status != state.DOWNLOADED:
            continue
        try:
            found[record.idx] = identify_one(record, tools, log)
            choice = apply_genre(record, found[record.idx], tools, log)
            if not choice.found and choice.considered:
                # Ticket 06: counted so that a name fitting nothing on enough tracks can be proposed as a
                # genre rather than staying blank for ever.
                job.append_event(state.UNGENERED_EVENT, idx=record.idx,
                                 names=list(choice.considered))
            landed = place(config, record, run=tools.runner, log=log)
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
        job.write_records(records)          # a crash costs the current track and nothing else

    def in_folder(folder: Path) -> list[state.Record]:
        return [record for record in placed if Path(record.local_path).parent == folder]

    for folder in sorted(touched):
        try:
            normalize(config, folder, run=tools.runner, log=log)
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
            # An existing genre is never replaced (the design notes), so the record has to follow the file rather
            # than claim a genre the file does not carry — the import reads the record, not the tag.
            kept = tag.read(Path(record.local_path)).genre
            log(f"      it already carried a genre, so {kept!r} stays rather than {record.genre!r}")
            record.genre = kept
        record.status = state.TAGGED
        summary.tagged.append(record.idx)
        if not record.genre:
            summary.ungenred.append(record.idx)

    job.write_records(records)
    job.write_status(config)
    # As in acquire: the run is finished by the import, which is where the pipeline ends.
    return summary
