"""Identification: what a file is, from its sound first and its name last.

Fingerprint with fpcalc, look that up in AcoustID, read the recording from MusicBrainz — which wants a
reachable contact in its User-Agent and answers at one request a second. Only about a third of AcoustIDs
link to a MusicBrainz recording, so a text search on the normalized artist and title follows, and the
filename is the last resort: either way the track is imported and queued for review. Nothing here writes.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from . import reference
from .config import ENV_HINT, Config, MissingCredential
from .rules import Request

# A fingerprint match below this is not trusted: the one sub-0.9 match that was checked resolved to a
# different track.
SCORE_FLOOR = 0.9

# How many recordings to ask MusicBrainz's text search for.
SEARCH_LIMIT = 10

# `urllib` opens sockets with no timeout by default, and musicbrainzngs never sets one, so a server that
# accepts the connection and then says nothing holds the whole pass — no error, no line, nothing. The
# process stage was silent for half an hour on 2026-10-17 for exactly that reason.
NETWORK_TIMEOUT = 25.0
# A fingerprint of a whole set is seconds of work; five minutes is a stuck process, not a slow file.
FPCALC_TIMEOUT = 300.0


@contextmanager
def _bounded():
    """Every socket opened inside gets a timeout, which is the one lever both libraries expose.

    The setting is the process's, not this call's: the process stage identifies one track at a time, so the
    window belongs to one identification. Running these in threads would have each one restore a value the
    others are still using.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(NETWORK_TIMEOUT)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


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
    """A recording something offered, with the releases MusicBrainz says it is on."""

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
    # Kept separate: the order they are trusted in is decided by `genre`.
    styles: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    score: float = 0.0
    source: str = ""        # fingerprint | search | filename
    # False when no service could be reached at all, which `source` cannot say: the filename is also what is
    # left after a search that answered with nothing.
    asked: bool = True


class Hearing(Protocol):
    """fpcalc: the fingerprint and length of a file."""

    def listen(self, path: Path) -> tuple[str, int]: ...


class Matches(Protocol):
    """AcoustID: what a fingerprint is."""

    def match(self, fingerprint: str, seconds: int) -> list[Match]: ...


class Brainz(Protocol):
    """MusicBrainz: a recording in full, a text search, a release, and a release group's genres."""

    def recording(self, recording_id: str) -> Candidate | None: ...

    def search(self, request: Request) -> list[Match]: ...

    def release(self, release_id: str) -> Release: ...

    def genres(self, release_group_id: str) -> tuple[str, ...]: ...


class Discogs(Protocol):
    """Discogs: the styles a release carries, which are the specific names its genres are not."""

    def styles(self, artist: str, title: str) -> tuple[str, ...]: ...


def from_filename(request: Request) -> Identified:
    """The last resort: the name is all there is."""
    return Identified(title=request.title, artist=request.artist, source="filename")


def _distance(candidate: Candidate, seconds: int | None) -> tuple[int, float]:
    """How far a recording is from the file's length; an unknown length never wins, sorting as `(1, 0.0)`."""
    if seconds is None or candidate.duration is None:
        return (1, 0.0)
    return (0, abs(candidate.duration - seconds))


def best(matches: list[Match], seconds: int | None = None) -> Candidate | None:
    """The recording to believe, or None.

    The score decides first, the length settles what the score cannot — an edit and an album cut are the
    same confidence and different tracks — and the floor decides whether anything is believed at all.
    """
    offered = [(match.score, candidate) for match in matches if match.score >= SCORE_FLOOR
               for candidate in match.recordings]
    if not offered:
        return None
    ordered = sorted(offered, key=lambda pair: (-pair[0], _distance(pair[1], seconds)))
    return ordered[0][1]


def choose(releases: list[Release], group: str = "") -> Release:
    """The release to take the label, catalogue number and year from.

    A release in the group AcoustID matched wins; then the first carrying a label and a date; then the
    first. Every release in the list contains the recording, so taking the first outright once produced a
    single's album and artist for a track whose album was a compilation.
    """
    for release in releases:
        if group and release.release_group_id == group:
            return release
    for release in releases:
        if release.label and release.date:
            return release
    return releases[0] if releases else Release()


def album_artist(release: Release, candidate: Candidate) -> str:
    """The release group's own credit — a single's credit can name somebody else entirely — and
    `Various Artists` when MusicBrainz says so."""
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
    discogs: Discogs | None = None,
    log=lambda *_: None,
) -> Identified:
    """What the file at `path` is: its sound first, a text search second, its filename last."""
    with _bounded():
        return _identify(path, request, hearing=hearing, matches=matches, brainz=brainz,
                         discogs=discogs, log=log)


def _identify(path: Path, request: Request, *, hearing: Hearing, matches: Matches, brainz: Brainz,
              discogs: Discogs | None, log) -> Identified:
    seconds: int | None = None
    try:
        fingerprint, seconds = hearing.listen(path)
    except IdentifyError as error:
        log(f"      could not listen to it ({error}) — the name will have to do")
    else:
        found = best(matches.match(fingerprint, seconds), seconds)
        if found is not None:
            log(f"      the fingerprint answers: {found.title!r}")
            return _in_full(found, brainz, discogs=discogs, source="fingerprint")

    found = best(brainz.search(request), seconds)
    if found is not None:
        log(f"      a text search answers: {found.title!r}")
        return _in_full(found, brainz, discogs=discogs, source="search")

    log("      nothing in MusicBrainz — the filename has to do")
    return from_filename(request)


def _in_full(candidate: Candidate, brainz: Brainz, *, discogs: Discogs | None = None,
             source: str) -> Identified:
    """Ask MusicBrainz about the found recording, and the two services about its genres.

    One release, not every release it appears on: each costs a request at one a second, and a field a
    release does not carry is left empty rather than invented. This is the first point at which a
    trustworthy artist and title exist to ask Discogs about.
    """
    detail = brainz.recording(candidate.id) or candidate
    release = choose([brainz.release(one.id) for one in detail.releases[:3]],
                     group=candidate.release_group)
    artist = " & ".join(detail.artists or candidate.artists)
    title = detail.title or candidate.title
    return Identified(
        title=title,
        artist=artist,
        album=candidate.album or detail.album or release.title,
        album_artist=album_artist(release, candidate),
        year=release.date[:4],
        label=release.label,
        catalog_number=release.catalog_number,
        recording_id=candidate.id,
        release_id=release.id,
        release_group_id=release.release_group_id,
        styles=discogs.styles(artist, title) if discogs is not None and artist and title else (),
        genres=brainz.genres(release.release_group_id) if release.release_group_id else (),
        source=source,
    )


# ── the live versions of the seams ───────────────────────────────────────────


class Fingerprints:
    """fpcalc, chromaprint's own tool."""

    def listen(self, path: Path) -> tuple[str, int]:
        import json
        import subprocess

        try:
            result = subprocess.run(["fpcalc", "-json", str(path)], capture_output=True, text=True,
                                    timeout=FPCALC_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise IdentifyError(f"fpcalc did not finish in {FPCALC_TIMEOUT:g}s") from None
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

        response = acoustid.lookup(self._key, fingerprint, seconds, meta="recordings releasegroups",
                                   timeout=NETWORK_TIMEOUT)
        return [_match_of(result) for result in response.get("results", [])]


class DiscogsStyles:
    """Discogs, keyed by the token their API wants for the rate limit it grants.

    Its search answers with `style` beside `genre`, so one request per track is enough — and the style is
    the useful one, being the specific name (`Jazz-Funk`) where the genre is a coarse bucket.
    """

    def __init__(self, token: str, user_agent: str = "music-agent/0.1") -> None:
        self._token = token
        self._agent = user_agent

    @classmethod
    def from_environment(cls, config: Config) -> DiscogsStyles:
        return cls(required(config.discogs_token_env))

    def styles(self, artist: str, title: str) -> tuple[str, ...]:
        import json
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({"q": f"{artist} {title}", "type": "release",
                                        "per_page": "1", "token": self._token})
        request = urllib.request.Request(f"https://api.discogs.com/database/search?{query}",
                                         headers={"User-Agent": self._agent})
        try:
            with urllib.request.urlopen(request, timeout=25) as handle:
                body = json.load(handle)
        except Exception:                       # a genre is not worth failing an identification over
            return ()
        results = body.get("results") or []
        return tuple(results[0].get("style") or []) if results else ()


def _match_of(result: dict) -> Match:
    """One AcoustID result. Its release group carries the album, its artists and the compilation flag, so
    nothing needs asking for them."""
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
        self._contact = contact

    @classmethod
    def from_environment(cls, config: Config) -> MusicBrainz:
        return cls(required(config.musicbrainz_contact_env))

    def recording(self, recording_id: str) -> Candidate | None:
        """The recording in full, with the releases it appears on.

        `release-groups` is not a valid include on a recording, which is why the group is fetched by a
        separate release call.
        """
        got = self._mb.get_recording_by_id(recording_id, includes=["artists", "releases"])
        recording = got.get("recording") or {}
        candidate = self._as_candidate(recording)
        if candidate is None:
            return None
        return replace(candidate, releases=tuple(self._as_release(release)
                                                 for release in recording.get("release-list", [])))

    def search(self, request: Request) -> list[Match]:
        """The same query the reference is resolved with, scored by MusicBrainz."""
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

    def genres(self, release_group_id: str) -> tuple[str, ...]:
        """A release group's genres, over a raw ws/2 call: musicbrainzngs 0.7.1 has no `genres` include at
        all, only `tags`, which came back empty on every file it was tried against."""
        import json
        import urllib.request

        request = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/release-group/{release_group_id}?inc=genres&fmt=json",
            # Their policy wants the contact in the User-Agent.
            headers={"User-Agent": f"music-agent/0.1 ( {self._contact} )"},
        )
        with urllib.request.urlopen(request, timeout=25) as handle:
            body = json.load(handle)
        return tuple(genre["name"] for genre in body.get("genres", []) if genre.get("name"))

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


def required(name: str) -> str:
    """A credential the environment must carry, named in the error so the fix is obvious."""
    import os

    value = os.environ.get(name)
    if not value:
        raise MissingCredential(f"{name} is not set — add it to .env, or run: {ENV_HINT}")
    return value


def live(config: Config) -> tuple[Fingerprints, AcoustIDMatches, MusicBrainz, DiscogsStyles]:
    """The four seams as they really are, for whatever stage identifies files."""
    return (Fingerprints(), AcoustIDMatches.from_environment(config),
            MusicBrainz.from_environment(config), DiscogsStyles.from_environment(config))
