"""The reference resolver: the version trap, and every way it must refuse to answer."""

from __future__ import annotations

import pytest

from music_agent import config as config_module
from music_agent import reference
from music_agent.config import MissingCredential
from music_agent.rules import Request


class Fake:
    """A lookup that answers from a list and remembers what it was asked."""

    def __init__(self, recordings: list[reference.Recording] | None = None) -> None:
        self.recordings = recordings or []
        self.queries: list[str] = []

    def find(self, query: str, limit: int = reference.LIMIT) -> list[reference.Recording]:
        self.queries.append(query)
        return list(self.recordings)


def recording(title: str, artist: str = "Kiasmos", ms: int | None = 413_000) -> reference.Recording:
    return reference.Recording(title=title, artist=artist, length_ms=ms)


def request(artist: str = "Kiasmos", title: str = "Wsjr", mix: str = "") -> Request:
    return Request(artist=artist, title=title, mix=mix)


def test_the_query_is_the_two_fields_and_nothing_else():
    assert reference.query_for(request()) == 'recording:"Wsjr" AND artist:"Kiasmos"'


def test_the_query_carries_the_bare_title_not_the_requested_version():
    """MusicBrainz carries the version in the recording title, so the bare title is what returns the
    versions for `resolve` to choose between."""
    queried = reference.query_for(request(title="Blurred [PRA005]"))
    assert queried == 'recording:"Blurred" AND artist:"Kiasmos"'


def test_a_quote_in_a_title_does_not_break_out_of_the_field():
    queried = reference.query_for(request(title='Say "Hello"'))
    assert queried == 'recording:"Say \\"Hello\\"" AND artist:"Kiasmos"'


def test_a_bare_request_resolves_to_the_original():
    """The version trap: `Wsjr` is 413 s and `Wsjr (Orphx remix)` is 453 s, so taking whichever the
    search returned first would reject every correct file."""
    lookup = Fake([recording("Wsjr", ms=413_000), recording("Wsjr (Orphx remix)", ms=453_000)])
    assert reference.resolve(request(), lookup) == 413


def test_a_bare_request_does_not_resolve_to_an_extended_mix():
    """The taking rule accepts a longer edit; the *reference* must stay the original, or the
    window it sets would reject the files that are the original."""
    lookup = Fake([recording("Wsjr", ms=413_000), recording("Wsjr (Extended Mix)", ms=620_000)])
    assert reference.resolve(request(), lookup) == 413


def test_a_named_version_resolves_to_that_version():
    lookup = Fake([recording("Wsjr", ms=413_000), recording("Wsjr (Orphx remix)", ms=453_000)])
    assert reference.resolve(request(mix="Orphx remix"), lookup) == 453


def test_a_named_version_musicbrainz_does_not_hold_resolves_to_nothing():
    lookup = Fake([recording("Wsjr", ms=413_000)])
    assert reference.resolve(request(mix="Orphx remix"), lookup) is None


def test_a_remix_never_stands_in_for_the_original():
    lookup = Fake([recording("Wsjr (Orphx remix)", ms=453_000)])
    assert reference.resolve(request(), lookup) is None


def test_an_original_never_stands_in_for_a_named_remix():
    lookup = Fake([recording("Wsjr", ms=413_000)])
    assert reference.resolve(request(mix="Victor Ruiz Remix"), lookup) is None


def test_a_different_artist_resolves_to_nothing():
    lookup = Fake([recording("Wsjr", artist="Someone Else")])
    assert reference.resolve(request(), lookup) is None


def test_a_recording_with_no_length_resolves_to_nothing():
    lookup = Fake([recording("Wsjr", ms=None)])
    assert reference.resolve(request(), lookup) is None


def test_originals_seconds_apart_resolve_to_nothing():
    """Two edits of the original are two different tracks, and a reference that may be the wrong one
    rejects every correct file."""
    lookup = Fake([recording("Wsjr", ms=413_000), recording("Wsjr (Original Mix)", ms=419_000)])
    assert reference.resolve(request(), lookup) is None


def test_originals_within_the_window_resolve_to_the_longer():
    """The resolver prefers the longer edit among versions that are acceptable."""
    lookup = Fake([recording("Wsjr", ms=413_000), recording("Wsjr (Original Mix)", ms=413_800)])
    assert reference.resolve(request(), lookup) == 414


def test_accents_are_folded_on_both_sides():
    lookup = Fake([recording("Ne-am despartit", artist="Trupa", ms=260_000)])
    assert reference.resolve(request(artist="Trupă", title="Ne-am despărțit"), lookup) == 260


def test_the_primary_artist_is_what_has_to_agree():
    """Credits are matched on the primary artist, so a featured credit does not block the lookup."""
    lookup = Fake([recording("Wsjr", artist="Kiasmos")])
    assert reference.resolve(request(artist="Kiasmos & Someone"), lookup) == 413


def test_a_whitelabel_resolves_to_nothing_without_even_asking():
    lookup = Fake([recording("Wsjr")])
    assert reference.resolve(request(artist=""), lookup) is None
    assert lookup.queries == []


def test_a_title_that_is_only_parentheses_resolves_to_nothing_without_even_asking():
    lookup = Fake([recording("(Untitled)")])
    assert reference.resolve(request(title="(Untitled)"), lookup) is None
    assert lookup.queries == []


def test_the_live_lookup_needs_the_contact_in_the_environment(monkeypatch):
    monkeypatch.delenv("MUSICBRAINZ_CONTACT", raising=False)
    with pytest.raises(MissingCredential, match="MUSICBRAINZ_CONTACT"):
        reference.lookup(config_module.DEFAULTS)
