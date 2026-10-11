"""The reference length: the one number that decides whether a candidate is the right file.

Ticket 13: the length comes from MusicBrainz, and a result is accepted only on folded artist and title
equality under the version rules. A bare request resolves to the original, a named version must
appear in the recording's title, and anything ambiguous yields *no* reference — because a wrong reference
does not degrade the match, it rejects every correct file. MusicBrainz holds `Wsjr` at 413 s and
`Wsjr (Orphx remix)` at 453 s, and a ±1 s window turns picking the wrong one into a total failure.

The answer is cached in the job's own `tracks.jsonl` and nowhere else, so resuming a run never re-queries
and nothing goes stale between runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .config import ENV_HINT, Config, MissingCredential
from .parsing import bare_title, extract_mix, fold, mix_matches, primary_artist
from .rules import DURATION_WINDOW, Request

# How many recordings to ask for. Wide enough that the right version is in the list — MusicBrainz orders
# by relevance, and a title that is also a common word can otherwise fill the page with the wrong ones.
LIMIT = 25


@dataclass(frozen=True)
class Recording:
    """One MusicBrainz recording, as much of it as the decision needs."""

    title: str
    artist: str
    length_ms: int | None = None
    id: str = ""

    @property
    def mix(self) -> str:
        """The version, read from the recording title the same way a candidate's is read."""
        return extract_mix(self.title)


class Lookup(Protocol):
    """Where recordings come from. MusicBrainz in production, a list in the tests."""

    def find(self, query: str, limit: int = LIMIT) -> list[Recording]: ...


def escape(text: str) -> str:
    """Lucene syntax: an unescaped quote would close the field early and search for something else."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def query_for(request: Request) -> str:
    """Ticket 13's query — both fields, and nothing else.

    The *bare* title goes in, not the requested version: MusicBrainz carries the version in the
    recording title, so the bare title returns the versions for `resolve` to choose between, while asking
    for a version MusicBrainz spells differently returns nothing at all.
    """
    return f'recording:"{escape(bare_title(request.title))}" AND artist:"{escape(request.artist)}"'


def resolve(request: Request, lookup: Lookup) -> int | None:
    """The reference length in whole seconds, or None when nothing can be pinned down.

    None is a real answer, not a failure: it means review-only, which is always better than a reference
    that may be the wrong edit (the design notes).
    """
    title = bare_title(request.title)
    if not request.artist.strip() or not title:
        return None              # a white-label or a nameless track: nothing to look up

    lengths: set[int] = set()
    for recording in lookup.find(query_for(request)):
        if recording.length_ms is None:
            continue
        if fold(bare_title(recording.title)) != fold(title):
            continue
        if fold(primary_artist(recording.artist)) != fold(primary_artist(request.artist)):
            continue
        if not mix_matches(request.mix, recording.mix):
            continue
        lengths.add(round(recording.length_ms / 1000))

    if not lengths:
        return None
    if max(lengths) - min(lengths) > DURATION_WINDOW:
        # Several versions of the original, seconds apart, are several different edits. Taking one would
        # reject the files that are the other.
        return None
    return max(lengths)              # the design notes prefers the longer edit among acceptable versions


class MusicBrainz:
    """The live lookup, serialized to one request a second as their policy asks (the design notes)."""

    def __init__(self, contact: str, limit: int = LIMIT) -> None:
        import musicbrainzngs

        self._mb = musicbrainzngs
        self._mb.set_useragent("music-agent", "0.1", contact)
        self._mb.set_rate_limit(1.0, 1)
        self.limit = limit

    def find(self, query: str, limit: int | None = None) -> list[Recording]:
        result = self._mb.search_recordings(query=query, limit=limit or self.limit)
        return [_recording(item) for item in result.get("recording-list", [])]


def _recording(item: dict) -> Recording:
    """One search result. The credit leads with the artist we compare against, so take that one."""
    credited = ""
    for credit in item.get("artist-credit", []):
        if isinstance(credit, dict) and credit.get("artist"):
            credited = credit["artist"].get("name", "") or credited
            break
    length = item.get("length")
    return Recording(
        title=item.get("title", ""),
        artist=credited,
        length_ms=int(length) if length else None,
        id=item.get("id", ""),
    )


def lookup(config: Config, limit: int = LIMIT) -> MusicBrainz:
    """The live lookup, with the contact their policy requires taken from the environment."""
    name = config.musicbrainz_contact_env
    contact = os.environ.get(name)
    if not contact:
        raise MissingCredential(f"{name} is not set — add it to .env, or run: {ENV_HINT}")
    return MusicBrainz(contact, limit=limit)
