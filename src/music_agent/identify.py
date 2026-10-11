"""Identification: what a file is, from its sound first and its name last.

Ticket 01: fingerprint with fpcalc, look that up in AcoustID, and read the recording from MusicBrainz —
which wants a real User-Agent with a contact they can reach, and answers at one request a second.
Ticket 15: only about a third of AcoustIDs link to a MusicBrainz recording, so a MusicBrainz text search on
the normalized artist and title follows it, and there is a 0.9 score floor below which a fingerprint result
is ignored rather than trusted — the one sub-0.9 match that was checked resolved to a *different* track.
When both fail, artist and title come from the filename, everything else stays empty, and the track is
imported anyway and queued for review.

Nothing here writes anything. Identification answers what a file is; the tag writer and the genre mapping
are the two things that use the answer, and the genre vocabulary is the library's rather than MusicBrainz's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import reference
from .config import ENV_HINT, Config, MissingCredential
from .rules import Request

# Ticket 15: below this a fingerprint match is ignored rather than trusted.
SCORE_FLOOR = 0.9

# How many recordings to ask MusicBrainz's text search for.
SEARCH_LIMIT = 10


class IdentifyError(RuntimeError):
    """A file could not be listened to at all."""


@dataclass(frozen=True)
class Release:
    """A release, as much of it as a tag needs."""

    id: str = ""
    title: str = ""
    date: str = ""
    label: str = ""
    catalog_number: str = ""
    artist: str = ""
    compilation: bool = False
    release_group_id: str = ""


@dataclass(frozen=True)
class Candidate:
    """A recording something offered, and — once MusicBrainz has been asked — the releases it is on."""

    id: str
    title: str
    artists: tuple[str, ...] = ()
    duration: float | None = None
    album: str = ""
    album_artists: tuple[str, ...] = ()
    compilation: bool = False
    release_group: str = ""
    releases: tuple[Release, ...] = ()


@dataclass(frozen=True)
class Match:
    """One scored answer, from AcoustID or from a MusicBrainz search — the same shape either way."""

    score: float
    recordings: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Identified:
    """What a file is, as far as anything could tell, and by which route it was told."""

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    label: str = ""
    catalog_number: str = ""
    recording_id: str = ""
    release_id: str = ""
    release_group_id: str = ""
    score: float = 0.0
    source: str = ""        # fingerprint | search | filename


class Hearing(Protocol):
    """fpcalc: the fingerprint and length of a file."""

    def listen(self, path: Path) -> tuple[str, int]: ...


class Matches(Protocol):
    """AcoustID: what a fingerprint is."""

    def match(self, fingerprint: str, seconds: int) -> list[Match]: ...


class Brainz(Protocol):
    """MusicBrainz: a recording in full, a text search, and a release."""

    def recording(self, recording_id: str) -> Candidate | None: ...

    def search(self, request: Request) -> list[Match]: ...

    def release(self, release_id: str) -> Release: ...


def from_filename(request: Request) -> Identified:
    """The last resort: the name is all there is (the design notes)."""
    return Identified(title=request.title, artist=request.artist, source="filename")


def _distance(candidate: Candidate, seconds: int | None) -> tuple[int, float]:
    """How far a recording is from the file's length — and a recording whose length is unknown never
    wins.

    This is not a detail. AcoustID lists recordings it has no length for, and treating "no length" as
    "no distance" made a different song on the same album beat the right one by a mile: on the file that
    caught it, `Heartz of Men` (no length at all) outranked `Hit ’Em Up` at 313 s against the file's
    312.6 s.
    """
    if seconds is None or candidate.duration is None:
        return (1, 0.0)
    return (0, abs(candidate.duration - seconds))


def best(matches: list[Match], seconds: int | None = None) -> Candidate | None:
    """The recording to believe, or None.

    The score decides first — it is the service's own confidence — and the length settles what the score
    cannot: two recordings of one song, an edit and an album cut, are the same confidence and different
    tracks. The floor decides whether anything is believed at all, and a missing length is never better
    than a known one.
    """
    offered = [(match.score, candidate) for match in matches if match.score >= SCORE_FLOOR
               for candidate in match.recordings]
    if not offered:
        return None
    ordered = sorted(offered, key=lambda pair: (-pair[0], _distance(pair[1], seconds)))
    return ordered[0][1]


def choose(releases: list[Release], group: str = "") -> Release:
    """The release to take the label, catalogue number and year from (the design notes).

    Every release in the list contains the recording — that is how they were found — so which one is a
    real question, and the file that prompted it answered it: taking the first gave the *single* `How Do U
    Want It` for a track whose album is `Death Row: Greatest Hits`, so the album and its artist came out
    as the single's. AcoustID says which release group it matched, so a release in that group wins, and
    the first is the fallback when none of them is in it.
    """
    for release in releases:
        if group and release.release_group_id == group:
            return release
    for release in releases:
        if release.label and release.date:
            return release
    return releases[0] if releases else Release()


def album_artist(release: Release, candidate: Candidate) -> str:
    """Ticket 17: the release group's own credit, and `Various Artists` when MusicBrainz says so.

    The group rather than the release's own credit, because a release can be a single whose credit is
    somebody else entirely — the very file that caught this had `2Pac & K‐Ci & JoJo` on it.
    """
    if release.compilation or candidate.compilation:
        return "Various Artists"
    return " & ".join(candidate.album_artists) or release.artist


def identify(
    path: Path,
    request: Request,
    *,
    hearing: Hearing,
    matches: Matches,
    brainz: Brainz,
    log=lambda *_: None,
) -> Identified:
    """What the file at `path` is: its sound first, a text search second, its filename last."""
    seconds: int | None = None
    try:
        fingerprint, seconds = hearing.listen(path)
    except IdentifyError as error:
        log(f"      could not listen to it ({error}) — the name will have to do")
    else:
        found = best(matches.match(fingerprint, seconds), seconds)
        if found is not None:
            log(f"      the fingerprint answers: {found.title!r}")
            return _in_full(found, brainz, source="fingerprint")

    found = best(brainz.search(request), seconds)
    if found is not None:
        log(f"      a text search answers: {found.title!r}")
        return _in_full(found, brainz, source="search")

    log("      nothing in MusicBrainz — the filename has to do")
    return from_filename(request)


def _in_full(candidate: Candidate, brainz: Brainz, *, source: str) -> Identified:
    """Ask MusicBrainz about the recording that was found, and about one release of it.

    One release, not every release it appears on: each costs a request at one a second, MusicBrainz orders
    them by relevance, and a field a release simply does not carry is left empty rather than invented.
    """
    detail = brainz.recording(candidate.id) or candidate
    release = choose([brainz.release(one.id) for one in detail.releases[:3]] or [],
                     group=candidate.release_group)
    return Identified(
        title=detail.title or candidate.title,
        artist=" & ".join(detail.artists or candidate.artists),
        album=candidate.album or detail.album or release.title,
        album_artist=album_artist(release, candidate),
        year=release.date[:4],
        label=release.label,
        catalog_number=release.catalog_number,
        recording_id=candidate.id,
        release_id=release.id,
        release_group_id=release.release_group_id,
        source=source,
    )


# ── the live versions of the three seams ─────────────────────────────────────


class Fingerprints:
    """fpcalc, chromaprint's own tool (the design notes)."""

    def listen(self, path: Path) -> tuple[str, int]:
        import json
        import subprocess

        result = subprocess.run(["fpcalc", "-json", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise IdentifyError(detail[-1] if detail else "fpcalc failed")
        body = json.loads(result.stdout)
        return body["fingerprint"], round(body["duration"])


class AcoustIDMatches:
    """AcoustID, keyed by the client key their policy requires."""

    def __init__(self, key: str) -> None:
        self._key = key

    @classmethod
    def from_environment(cls, config: Config) -> AcoustIDMatches:
        return cls(required(config.acoustid_key_env))

    def match(self, fingerprint: str, seconds: int) -> list[Match]:
        import acoustid

        response = acoustid.lookup(self._key, fingerprint, seconds, meta="recordings releasegroups")
        return [_match_of(result) for result in response.get("results", [])]


def _match_of(result: dict) -> Match:
    """One AcoustID result. The release group is where the album, its artists and the compilation flag
    already are, so nothing needs asking for them."""
    recordings = []
    for recording in result.get("recordings", []):
        group = (recording.get("releasegroups") or [{}])[0]
        recordings.append(Candidate(
            id=recording.get("id", ""),
            title=recording.get("title", ""),
            artists=tuple(artist.get("name", "") for artist in recording.get("artists", [])),
            duration=recording.get("duration"),
            album=group.get("title", ""),
            album_artists=tuple(artist.get("name", "") for artist in group.get("artists", [])),
            compilation="Compilation" in (group.get("secondarytypes") or []),
            release_group=group.get("id", ""),
        ))
    return Match(score=float(result.get("score", 0.0)), recordings=tuple(recordings))


class MusicBrainz:
    """MusicBrainz, serialized at the one request a second their policy asks for."""

    def __init__(self, contact: str) -> None:
        import musicbrainzngs

        self._mb = musicbrainzngs
        self._mb.set_useragent("music-agent", "0.1", contact)
        self._mb.set_rate_limit(1.0, 1)

    @classmethod
    def from_environment(cls, config: Config) -> MusicBrainz:
        return cls(required(config.musicbrainz_contact_env))

    def recording(self, recording_id: str) -> Candidate | None:
        """The recording in full, with the releases it appears on.

        `releases` is the include that carries them. Asking a *recording* for `release-groups` is not a
        valid include at all, which is why a release is fetched separately when its group is wanted.
        """
        got = self._mb.get_recording_by_id(recording_id, includes=["artists", "releases"])
        recording = got.get("recording") or {}
        candidate = self._as_candidate(recording)
        if candidate is None:
            return None
        return _with(candidate, releases=tuple(self._as_release(release)
                                                for release in recording.get("release-list", [])))

    def search(self, request: Request) -> list[Match]:
        """Ticket 15's rescue: the same query the reference is resolved with, scored by MusicBrainz."""
        found = self._mb.search_recordings(query=reference.query_for(request), limit=SEARCH_LIMIT)
        listed = found.get("recording-list", [])
        recordings = tuple(candidate for candidate in
                           (self._as_candidate(recording) for recording in listed)
                           if candidate is not None)
        if not recordings:
            return []
        top = max(int(recording.get("ext:score", 0)) for recording in listed)
        return [Match(score=top / 100.0, recordings=recordings)]

    def release(self, release_id: str) -> Release:
        got = self._mb.get_release_by_id(
            release_id, includes=["labels", "release-groups", "artist-credits"])
        return self._as_release(got.get("release") or {})

    def _as_candidate(self, recording: dict) -> Candidate | None:
        if not recording.get("id"):
            return None
        return Candidate(
            id=recording["id"],
            title=recording.get("title", ""),
            artists=tuple(part["artist"]["name"] for part in (recording.get("artist-credit") or [])
                          if isinstance(part, dict) and part.get("artist")),
            duration=(float(recording["length"]) / 1000.0) if recording.get("length") else None,
        )

    def _as_release(self, release: dict) -> Release:
        info = (release.get("label-info-list") or [{}])[0]
        group = release.get("release-group") or {}
        credit = release.get("artist-credit") or group.get("artist-credit") or []
        return Release(
            id=release.get("id", ""),
            title=release.get("title", ""),
            date=release.get("date", ""),
            label=(info.get("label") or {}).get("name", ""),
            catalog_number=info.get("catalog-number", ""),
            artist=" & ".join(part["artist"]["name"] for part in credit
                              if isinstance(part, dict) and part.get("artist")),
            compilation="Compilation" in (group.get("secondary-types") or []),
            release_group_id=group.get("id", ""),
        )


def _with(candidate: Candidate, *, releases: tuple[Release, ...]) -> Candidate:
    """`Candidate` is frozen, so the releases ride along in a copy rather than being bolted on."""
    return Candidate(
        id=candidate.id, title=candidate.title, artists=candidate.artists,
        duration=candidate.duration, album=candidate.album,
        album_artists=candidate.album_artists, compilation=candidate.compilation,
        release_group=candidate.release_group, releases=releases,
    )


def required(name: str) -> str:
    """A credential the environment must carry, named in the error so the fix is obvious."""
    import os

    value = os.environ.get(name)
    if not value:
        raise MissingCredential(f"{name} is not set — add it to .env, or run: {ENV_HINT}")
    return value


def live(config: Config) -> tuple[Fingerprints, AcoustIDMatches, MusicBrainz]:
    """The three seams as they really are, for whatever stage identifies files."""
    return (Fingerprints(), AcoustIDMatches.from_environment(config),
            MusicBrainz.from_environment(config))
