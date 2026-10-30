"""The playlist reader: a public Spotify playlist, as the text `tracklist` reads.

The network is never touched here. What is pinned is the part of the flow that has a right answer — the id
in a URL, the title's tail, the page walk, the code the token endpoint will accept — and the part where a
failure has to arrive as a sentence rather than a traceback.
"""

from __future__ import annotations

import io
import urllib.error
from datetime import datetime, timezone

import pytest

from music_agent import playlists, spotify, tracklist


def entry(artist: str, name: str) -> dict:
    return {"itemV2": {"data": {"name": name, "artists": {"items": [{"profile": {"name": artist}}]}}}}


def test_a_url_and_a_uri_are_the_same_playlist():
    """Both shapes turn up: a copied URL carries a `?si=` tail, and a market prefix rides in the path."""
    for source in (
        "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U",
        "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=12ab34cd56ef",
        "https://open.spotify.com/intl-fr/playlist/37i9dQZF1DWXRqgorJj26U",
        "spotify:playlist:37i9dQZF1DWXRqgorJj26U",
    ):
        assert spotify.playlist_id(source) == "37i9dQZF1DWXRqgorJj26U"


def test_what_is_not_a_playlist_is_refused_by_name():
    """A bare id is in this list on purpose: it is a legal filename as well as a legal playlist, and
    `--tracks` has to tell a path from a playlist without asking the filesystem."""
    for source in (
        "https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX",
        "https://example.com/playlist/37i9dQZF1DWXRqgorJj26U",
        "spotify:track:0ofHAoxe9vBkTCp2UQIavz",
        "37i9dQZF1DWXRqgorJj26U",
        "",
    ):
        with pytest.raises(playlists.PlaylistError):
            spotify.playlist_id(source)


def test_an_entry_is_its_credited_artists_and_its_title():
    """The credit is joined the way a credit is written, because that is the shape `parsing.credits` splits
    and the primary artist is the one a search leads with."""
    two = {"itemV2": {"data": {"name": "Dreams", "artists": {"items": [
        {"profile": {"name": "Fleetwood Mac"}}, {"profile": {"name": "Stevie Nicks"}}]}}}}
    assert spotify.entry_line(two) == "Fleetwood Mac, Stevie Nicks - Dreams"


def test_what_is_not_a_track_is_not_a_line():
    """A playlist can hold an episode, a local file or a gap, and none of those is a track to search for.
    A track with no artist keeps its title, which is the shape `tracklist` reads as a white label."""
    assert spotify.entry_line({}) == ""
    assert spotify.entry_line({"itemV2": {"data": {"name": "   "}}}) == ""
    assert spotify.entry_line({"itemV2": {"data": {"name": "Untitled Dubplate"}}}) == "Untitled Dubplate"


def test_the_code_is_the_one_a_live_token_request_carried():
    """`/api/token` refuses a code it cannot check — `400 Unauthorized request`, with or without a cookie —
    so this vector is the whole of the flow's non-obvious half: the secret, the XOR that turns it into
    decimals, and the UTF-8 bytes of those decimals as the HMAC key. From the request the web player made at
    2026-10-13T14:45:24Z."""
    when = datetime(2026, 10, 13, 14, 45, 24, tzinfo=timezone.utc).timestamp()
    assert spotify._totp(when) == "282805"


def test_a_playlist_longer_than_a_page_is_walked_to_the_end(monkeypatch):
    """The query pages, so a playlist of any length arrives in order — the order being the point, since the
    run searches what it is given in the order it is given it."""
    monkeypatch.setattr(spotify, "_PAGE", 2)
    monkeypatch.setattr(spotify, "_token", lambda timeout: "token")
    pages = {
        0: {"name": "Rock Classics", "content": {"totalCount": 3,
                                                 "items": [entry("A", "One"), entry("B", "Two")]}},
        2: {"name": "Rock Classics", "content": {"totalCount": 3, "items": [entry("C", "Three")]}},
    }
    asked: list[int] = []

    def fetch(token, identifier, offset, timeout):
        asked.append(offset)
        return pages[offset]

    monkeypatch.setattr(spotify, "_fetch_playlist", fetch)
    found = spotify.playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")

    assert asked == [0, 2]
    assert found.name == "Rock Classics"
    assert found.lines == ("A - One", "B - Two", "C - Three")


def test_a_playlist_that_reports_no_count_still_stops(monkeypatch):
    """A full page with no count has to be followed, and an empty one has to end the walk: a playlist whose
    count is missing must not become either a loop that never returns or one that stops at the first page."""
    monkeypatch.setattr(spotify, "_PAGE", 2)
    monkeypatch.setattr(spotify, "_token", lambda timeout: "token")
    pages = {
        0: {"name": "Rock Classics", "content": {"items": [entry("A", "One"), entry("B", "Two")]}},
        2: {"name": "Rock Classics", "content": {"items": []}},
    }
    asked: list[int] = []

    def fetch(token, identifier, offset, timeout):
        asked.append(offset)
        return pages[offset]

    monkeypatch.setattr(spotify, "_fetch_playlist", fetch)
    found = spotify.playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")

    assert asked == [0, 2]
    assert found.lines == ("A - One", "B - Two")


def test_a_url_or_a_uri_is_a_playlist_where_a_path_is_a_path():
    """`--tracks` takes a file and a playlist, so which one it got has to be decidable from the argument
    alone — and a bare id is not decidable, which is why it is neither."""
    assert spotify.names_a_playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=12ab")
    assert spotify.names_a_playlist("spotify:playlist:37i9dQZF1DWXRqgorJj26U")
    assert not spotify.names_a_playlist("37i9dQZF1DWXRqgorJj26U")
    assert not spotify.names_a_playlist("/tmp/rock classics.txt")
    assert not spotify.names_a_playlist("~/Music/Downloads/tracklist.csv")


def test_the_text_is_a_tracklist_the_reader_already_takes():
    """One file with one reader: the name rides in a comment, which `tracklist.parse` skips, so writing this
    out and handing it to `acquire --tracks` is the whole of the handover."""
    found = playlists.Playlist(name="Rock Classics",
                             lines=("Fleetwood Mac - Dreams", "Queen - Radio Ga Ga"))
    text = found.text()

    assert text.startswith("# Rock Classics · 2 tracks\n")
    assert [(request.artist, request.title) for request in tracklist.parse(text)] == [
        ("Fleetwood Mac", "Dreams"), ("Queen", "Radio Ga Ga")]


def test_a_refusal_arrives_as_what_spotify_said(monkeypatch):
    """This flow is unsupported and unannounced, so the sentence that comes out is the only diagnosis there
    will be — a traceback would say `HTTPError: 400` and nothing about the TOTP."""
    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://open.spotify.com/api/token", 400, "Bad Request", {},
            io.BytesIO(b'{"error":{"message":"Unauthorized request"}}'))

    monkeypatch.setattr(spotify.urllib.request, "urlopen", refuse)
    request = spotify.urllib.request.Request("https://open.spotify.com/api/token?totp=000000")

    with pytest.raises(playlists.PlaylistError, match="400.*Unauthorized request"):
        spotify._json(request, 1.0)


def test_a_token_response_without_a_token_names_the_likely_cause(monkeypatch):
    monkeypatch.setattr(spotify, "_json", lambda request, timeout: {"clientId": "x"})

    with pytest.raises(playlists.PlaylistError, match="TOTP"):
        spotify._token(1.0)
