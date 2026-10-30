"""The Deezer reader: a public Deezer playlist, as the text `tracklist` reads.

The network is never touched here. What is pinned is the part that has a right answer — the id in a URL, the
share link the phone hands over, the title a request should ask for, and the page walk — with the entries
taken from the playlist this was built against.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from music_agent import deezer, playlists, tracklist


def entry(artist: str, title: str, **extra) -> dict:
    return {"artist": {"name": artist}, "title": title, **extra}


def test_a_url_is_the_playlist_and_the_market_prefix_is_not():
    for source in (
        "https://www.deezer.com/playlist/66079435",
        "https://www.deezer.com/fr/playlist/66079435",
        "https://www.deezer.com/fr/playlist/66079435?host=15562631&utm_source=user_sharing",
        "deezer:playlist:66079435",
    ):
        assert deezer.playlist_id(source) == "66079435"


def test_what_is_not_a_playlist_is_refused_by_name():
    """A bare id is in this list on purpose: it is a legal filename as well as a legal playlist."""
    for source in (
        "https://www.deezer.com/album/302127",
        "https://www.deezer.com/track/3135556",
        "https://example.com/playlist/66079435",
        "66079435",
        "",
    ):
        with pytest.raises(playlists.PlaylistError):
            deezer.playlist_id(source)


def test_a_share_link_is_a_playlist_before_anything_is_fetched():
    """`--tracks` settles this from the argument alone, so reading it must not cost a request."""
    assert deezer.names_a_playlist("https://link.deezer.com/s/34NKfa1yXrklFXuYlrljx")
    assert deezer.names_a_playlist("https://www.deezer.com/playlist/66079435")
    assert not deezer.names_a_playlist("~/Music/Downloads/tracklist.csv")
    assert not deezer.names_a_playlist("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")


def test_a_share_link_is_read_through_the_playlist_it_leads_to(monkeypatch):
    """The link carries no id at all: the id is the last URL in the redirect chain."""
    asked: list[str] = []

    monkeypatch.setattr(deezer, "_followed", lambda source, timeout: (
        "https://www.deezer.com/fr/playlist/66079435?host=15562631&universal_link=1"
    ))

    def answer(request: urllib.request.Request, timeout: float) -> dict:
        asked.append(request.full_url)
        if request.full_url.endswith("/playlist/66079435"):
            return {"title": "01", "nb_tracks": 1}
        return {"data": [entry("Massive Attack", "Teardrop")], "total": 1}

    monkeypatch.setattr(deezer, "_json", answer)

    found = deezer.playlist("https://link.deezer.com/s/34NKfa1yXrklFXuYlrljx")

    assert found.name == "01"
    assert found.lines == ("Massive Attack - Teardrop",)
    assert asked[0] == "https://api.deezer.com/playlist/66079435"


def test_a_share_link_that_leads_off_deezer_is_refused(monkeypatch):
    """A link is somebody else's to write. Following one to another host would make this CLI fetch whatever
    that host served, on this machine's behalf, for a string that claimed to be a Deezer playlist."""
    monkeypatch.setattr(deezer, "_followed", lambda source, timeout: (
        "https://example.com/playlist/66079435"))

    with pytest.raises(playlists.PlaylistError, match="not to Deezer"):
        deezer.playlist("https://link.deezer.com/s/34NKfa1yXrklFXuYlrljx")


def test_a_version_that_is_its_own_edit_is_kept_and_a_master_naming_is_not():
    """Dropping `(Zinc VIP)` would resolve the reference to the original's length and then refuse the file
    that is the entry as too long; carrying `(Album Version)` as the mix would demand a spelling peers
    do not use. A source is never a recording, so it resolves nothing and comes off."""
    for track, expected in (
        (entry("Shadow Child", "23 (Zinc VIP)"), "23 (Zinc VIP)"),
        (entry("Watermät", "Bullit (Radio Edit)"), "Bullit (Radio Edit)"),
        (entry("Hardwell", "Spaceman (Drown The Fish Remix)"), "Spaceman (Drown The Fish Remix)"),
        (entry("Rusko", "Everyday (Netsky Remix)"), "Everyday (Netsky Remix)"),
        (entry("Carpenters", "(They Long To Be) Close To You (1991 Remix)"),
         "(They Long To Be) Close To You (1991 Remix)"),
        (entry("Pep & Rash", "Rumors (Original Mix)"), "Rumors"),
        (entry("Smash Mouth", "All Star (Album Version)"), "All Star"),
        (entry("Will Smith", 'Men In Black (From "Men In Black" Soundtrack)'), "Men In Black"),
        (entry("The Toxic Avenger", "Angst Two (From The Nissan Qashqai Movie)"), "Angst Two"),
        (entry("M83", "Midnight City"), "Midnight City"),
        (entry("Madeon", "Finale (feat. Nicholas Petricca)"), "Finale"),
        (entry("Studio Allstars", "Perfect Exceeder - (Tribute To Mason Vs Princess Superstar)"),
         "Perfect Exceeder"),
    ):
        assert deezer.entry_title(track) == expected


def test_a_title_that_is_only_a_parenthetical_is_not_given_its_version_twice():
    """The same reading `tracklist.request_for` makes: a whole title in brackets is the title."""
    assert deezer.entry_title(entry("Artist", "(Something Remix)")) == "(Something Remix)"


def test_an_entry_is_its_artist_and_its_title():
    assert deezer.entry_line(entry("Massive Attack", "Teardrop")) == "Massive Attack - Teardrop"
    assert deezer.entry_line({"title": "Untitled Dubplate"}) == "Untitled Dubplate"


def test_what_is_not_a_track_is_not_a_line():
    assert deezer.entry_line({}) == ""
    assert deezer.entry_line({"title": "   "}) == ""


def test_a_playlist_longer_than_a_page_is_walked_in_order(monkeypatch):
    """A hundred at a time, and the count is what ends the walk — the order is the point, since the
    playlist's own order is the one the run keeps."""
    pages = [
        {"data": [entry(f"Artist {index}", f"Title {index}") for index in range(100)], "total": 102},
        {"data": [entry("Artist 100", "Title 100"), entry("Artist 101", "Title 101")], "total": 102},
    ]
    starts: list[str] = []

    def answer(request: urllib.request.Request, timeout: float) -> dict:
        if request.full_url.endswith("/playlist/1"):
            return {"title": "Long", "nb_tracks": 102}
        starts.append(request.full_url)
        return pages[len(starts) - 1]

    monkeypatch.setattr(deezer, "_json", answer)

    found = deezer.playlist("https://www.deezer.com/playlist/1")

    assert len(found.lines) == 102
    assert found.lines[0] == "Artist 0 - Title 0"
    assert found.lines[-1] == "Artist 101 - Title 101"
    assert "index=0" in starts[0] and "index=100" in starts[1]


def test_a_page_that_reports_no_count_still_stops(monkeypatch):
    """A full page with no count has to be followed, and an empty one has to end the walk."""
    pages = [{"data": [entry(f"A{index}", "T") for index in range(100)]},
             {"data": [entry("A100", "T")]}, {"data": []}]

    def answer(request: urllib.request.Request, timeout: float) -> dict:
        if request.full_url.endswith("/playlist/1"):
            return {"title": "Long"}
        return pages.pop(0)

    monkeypatch.setattr(deezer, "_json", answer)

    assert len(deezer.playlist("https://www.deezer.com/playlist/1").lines) == 101


def test_a_page_walk_that_never_ends_is_stopped(monkeypatch):
    """No playlist holds that many entries, so a full page forever is the API failing to end. A reader that
    kept asking would never return, and the run it was starting would hang instead of reporting."""
    def answer(request: urllib.request.Request, timeout: float) -> dict:
        if request.full_url.endswith("/playlist/1"):
            return {"title": "Endless"}
        return {"data": [entry("A", "T") for _ in range(deezer._PAGE)]}

    monkeypatch.setattr(deezer, "_json", answer)

    with pytest.raises(playlists.PlaylistError, match="not ending"):
        deezer.playlist("https://www.deezer.com/playlist/1")


def test_a_refusal_arrives_as_what_deezer_said(monkeypatch):
    """A deleted playlist is a 200 with an error object rather than an HTTP failure, and the sentence that
    comes out is the only diagnosis there is."""
    monkeypatch.setattr(deezer, "_json", lambda request, timeout: (_ for _ in ()).throw(
        playlists.PlaylistError("Deezer refused the playlist: no data")))

    with pytest.raises(playlists.PlaylistError, match="no data"):
        deezer.playlist("https://www.deezer.com/playlist/1")


def test_an_error_object_is_a_refusal_not_an_empty_playlist(monkeypatch):
    """The API's own error carries no `title`, and a reader that shrugged would start a run of no tracks."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *rest):
            return False

        def read(self):
            return json.dumps({"error": {"type": "DataException", "message": "no data",
                                         "code": 800}}).encode()

    monkeypatch.setattr(deezer.urllib.request, "urlopen", lambda request, timeout: Response())

    with pytest.raises(playlists.PlaylistError, match="no data"):
        deezer._json(deezer._request(f"{deezer.API_URL}/playlist/1"), 1.0)


def test_a_payload_that_is_not_an_object_is_a_refusal_rather_than_a_traceback(monkeypatch):
    """`listing.get("title")` on a list would raise `AttributeError`, which the CLI does not catch — it
    catches `PlaylistError`, and anything else ends the command with a traceback."""
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *rest):
            return False

        def read(self):
            return b'[{"id": 1}]'

    monkeypatch.setattr(deezer.urllib.request, "urlopen", lambda request, timeout: Response())

    with pytest.raises(playlists.PlaylistError, match="not a playlist"):
        deezer._json(deezer._request(f"{deezer.API_URL}/playlist/1"), 1.0)


def test_the_text_is_a_tracklist_the_reader_already_takes():
    found = playlists.Playlist(name="01", lines=(
        "Massive Attack - Teardrop", "Hardwell - Spaceman (Drown The Fish Remix)"))

    requests = tracklist.parse(found.text())

    assert [(request.artist, request.title, request.mix) for request in requests] == [
        ("Massive Attack", "Teardrop", ""),
        ("Hardwell", "Spaceman", "Drown The Fish Remix"),
    ]
