"""The matching rule: what a candidate file is, and which one the system takes.

Ported from `prototype/review_model.py`, which exists to show this rule and nothing else. Vocabulary
follows GLOSSARY.md: a candidate carries a *label* (what would happen to it), a track carries a
*decision* (what the human did) — the rule knows only about labels.

Pure by construction: no clock, no disk, no network, so every branch the map settled is testable
without a peer or a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

from .parsing import appears_in, artists_appear, extract_mix, mix_matches, parse_filename

# A candidate's length may differ from the reference by this much and no more (the design notes). Anything
# wider is not a near miss, it is a different file, and it is rejected rather than offered.
DURATION_WINDOW = 1

# Below this a lossy file is not worth taking, whatever its length says.
QUALITY_FLOOR = 192

# Below this a file is a preview clip, not a track. Tested before anything else, so a preview is
# never explained away as a wrong mix.
MIN_SECONDS = 60

# Ticket 08 as revised by the user: the extension decides, nothing is probed, and an ALAC-in-m4a is
# taken as lossy. Lossless is the lane that reaches the AIFF pipeline.
LOSSLESS_EXTENSIONS = frozenset({"flac", "wav", "aiff", "aif"})

# Everything else we take, as-is. `.aif` is listed because it is the same format as `.aiff` and peers
# use both. Anything not in either set is refused outright — artwork, cue sheets, logs, checksums.
AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | frozenset({"m4a", "mp3", "ogg", "opus"})


class Label(Enum):
    """What would happen to a candidate if it were left alone."""

    READY = "Ready"
    NEEDS_YOU = "Needs you"
    REJECTED = "Rejected"


@dataclass(frozen=True)
class Request:
    """What is being asked for.

    `ref_duration` is the MusicBrainz recording length in whole seconds, or None when nothing could be
    resolved — and a None reference can never settle a track (the design notes).
    """

    artist: str
    title: str
    mix: str = ""
    ref_duration: int | None = None


@dataclass(frozen=True)
class Candidate:
    """One file a peer offered. `path` is the peer's own path, which is not a local path."""

    user: str
    path: str
    duration: int | None = None
    bitrate: int | None = None
    queue: int = 0
    size_bytes: int | None = None

    @property
    def name(self) -> str:
        """The basename under either separator, since peers announce Windows paths."""
        return PurePosixPath(self.path.replace("\\", "/")).name

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def is_audio(self) -> bool:
        """A format we take. Anything else in a result — artwork, cue sheets, logs — is refused."""
        return self.ext in AUDIO_EXTENSIONS

    @property
    def lossless(self) -> bool:
        return self.ext in LOSSLESS_EXTENSIONS

    @property
    def mix(self) -> str:
        """The trailing parenthesised mix, defined once in `parsing` so that matching and query
        building cannot drift apart."""
        return extract_mix(self.name)


@dataclass(frozen=True)
class Verdict:
    """A candidate together with what the rule says about it."""

    candidate: Candidate
    label: Label
    reason: str

    @property
    def rejected(self) -> bool:
        return self.label is Label.REJECTED


@dataclass
class Track:
    """A request and everything offered against it."""

    request: Request
    candidates: list[Candidate] = field(default_factory=list)


def hms(seconds: int | None) -> str:
    return "  ?  " if seconds is None else "%d:%02d" % (seconds // 60, seconds % 60)


def version_matches(candidate: Candidate, request: Request) -> bool:
    """A bare request means the original; a named one must appear in the candidate's mix.

    Defined in `parsing`, so a candidate's mix suffix and a MusicBrainz recording's version cannot be
    judged by two different rules.
    """
    return mix_matches(request.mix, candidate.mix)


def classify(candidate: Candidate, request: Request) -> Verdict:
    """What would happen to this candidate if it were left alone.

    Order matters: a preview would otherwise be judged as a wrong mix, a length outside the window
    says more than a name ever could — the duration is the arbiter — and a wrong track outranks a
    wrong mix.
    """
    if not candidate.is_audio:
        return Verdict(candidate, Label.REJECTED, f"a .{candidate.ext} file, not one we take")

    if candidate.duration is not None and candidate.duration < MIN_SECONDS:
        return Verdict(candidate, Label.REJECTED, f"{hms(candidate.duration)} — a preview clip")

    if candidate.duration is None:
        return Verdict(candidate, Label.NEEDS_YOU, "the peer reports no length")

    if request.ref_duration is None:
        return Verdict(candidate, Label.NEEDS_YOU, "no reference length for this track")

    offset = candidate.duration - request.ref_duration
    if abs(offset) > DURATION_WINDOW:
        return Verdict(candidate, Label.REJECTED, "runs %+d s against the reference" % offset)

    # The length says how big it is; the name says what it is. A run of the right duration is still
    # the wrong track. The artist is looked for across the whole path because peers keep it in the
    # folder — `…\Aphex Twin\Selected Ambient Works 85-92\12 Delphium.m4a` — and the title is looked
    # for in the basename, which is the one place a title is always written (the design notes).
    parsed = parse_filename(candidate.path)

    if not parsed.title:
        return Verdict(candidate, Label.NEEDS_YOU, "the filename names no track")

    if not artists_appear(request.artist, candidate.path):
        return Verdict(candidate, Label.REJECTED, "a different artist")

    if not appears_in(request.title, parsed.title):
        return Verdict(candidate, Label.REJECTED, "a different track")

    # Ticket 08: below the floor a lossy file is never taken unattended, but it is not discarded
    # either — it goes to review, so the only copy of a rare track can still be accepted deliberately.
    if not candidate.lossless and candidate.bitrate is not None and candidate.bitrate < QUALITY_FLOOR:
        return Verdict(candidate, Label.NEEDS_YOU, f"{candidate.bitrate} kbps — below the quality floor")

    if not version_matches(candidate, request):
        return Verdict(candidate, Label.NEEDS_YOU, f"a different mix — {candidate.mix or 'unnamed'}")

    return Verdict(candidate, Label.READY, "same mix, length matches")


def verdicts(track: Track) -> list[Verdict]:
    """Every candidate with its label, rejections included."""
    return [classify(c, track.request) for c in track.candidates]


def visible(track: Track) -> list[Verdict]:
    """The candidates worth a human's attention. A rejection is never shown."""
    return [v for v in verdicts(track) if not v.rejected]


def ready(track: Track) -> list[Verdict]:
    """The candidates that could be taken unattended."""
    return [v for v in verdicts(track) if v.label is Label.READY]


def rank(candidate: Candidate) -> tuple[int, int, int]:
    """The ladder used when more than one candidate is Ready: lossless first, then the higher
    bitrate, then the shorter queue (the design notes)."""
    return (0 if candidate.lossless else 1, -(candidate.bitrate or 0), candidate.queue or 0)


def pick(track: Track) -> Verdict | None:
    """The candidate the system takes on its own, or None when nothing is Ready.

    The ladder is deliberately blind to anything but those three facts, so when it ties — two
    lossless files of the same length with the same queue depth — the first as offered wins. The
    choice is logged, and that log is what makes the arbitrary tie visible after the fact.
    """
    candidates = ready(track)
    return min(candidates, key=lambda v: rank(v.candidate)) if candidates else None


def taken(track: Track) -> bool:
    """Any Ready candidate means the track is settled without asking."""
    return bool(ready(track))


def in_review(track: Track) -> bool:
    """A track needs a human only when no candidate is Ready, so every card in review reads
    'Needs you'."""
    return bool(visible(track)) and not taken(track)


def nothing_usable(track: Track) -> bool:
    """Every candidate was rejected — there is nothing here to decide about."""
    return not visible(track)
