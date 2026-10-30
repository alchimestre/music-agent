"""The YouTube reader: a public playlist, as the text `tracklist` reads.

The network and the tool are never touched here. What is pinned is what a video's title and channel have to
be read back to — a track's artist and its name — with the entries taken from the playlist this was built
against, and the failures a person actually meets: no tool, a tool that fails, an answer that is not a
playlist.
"""

from __future__ import annotations

import json

import pytest

from music_agent import playlists, tracklist, youtube

WATCH = "https://www.youtube.com/watch?v=tNVWqNn2auM&list=PLX4mtKna7vU975Z3p7wfL7xhZxGNuCvdt"
MUSIC = "https://music.youtube.com/playlist?list=PLX4mtKna7vU975Z3p7wfL7xhZxGNuCvdt"
PLAIN = "https://www.youtube.com/playlist?list=PLX4mtKna7vU975Z3p7wfL7xhZxGNuCvdt"

LIST = "PLX4mtKna7vU975Z3p7wfL7xhZxGNuCvdt"


class Finished:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def video(title: str, channel: str) -> dict:
    return {"title": title, "channel": channel, "id": "x", "duration": 200}


def installed(monkeypatch, stdout: str = "", stderr: str = "", returncode: int = 0):
    """A machine with the tool on it, answering whatever the test says."""
    monkeypatch.setattr(youtube.shutil, "which", lambda name: "/opt/homebrew/bin/yt-dlp")
    monkeypatch.setattr(
        youtube.subprocess, "run",
        lambda *args, **kwargs: Finished(stdout=stdout, stderr=stderr, returncode=returncode),
    )


def test_the_list_id_is_the_same_from_all_three_link_forms():
    for source in (
        WATCH,
        MUSIC,
        PLAIN,
        f"https://m.youtube.com/playlist?list={LIST}",
        f"https://youtu.be/tNVWqNn2auM?list={LIST}&si=abc",
    ):
        assert youtube.playlist_id(source) == LIST


def test_a_single_video_is_not_a_tracklist():
    """A watch link with no `list=` is one video, and reading it as a run would start a run of one track."""
    for source in (
        "https://www.youtube.com/watch?v=tNVWqNn2auM",
        "https://youtu.be/tNVWqNn2auM",
        f"https://example.com/playlist?list={LIST}",
        "https://www.youtube.com/channel/UC1234567890",
        LIST,
        "",
    ):
        with pytest.raises(playlists.PlaylistError):
            youtube.playlist_id(source)


def test_names_a_playlist_runs_nothing(monkeypatch):
    """`--tracks` asks every reader whether the argument is its own before anything happens."""
    monkeypatch.setattr(youtube.shutil, "which", lambda name: pytest.fail("which was called"))

    assert youtube.names_a_playlist(WATCH)
    assert youtube.names_a_playlist(MUSIC)
    assert not youtube.names_a_playlist("https://www.youtube.com/watch?v=tNVWqNn2auM")
    assert not youtube.names_a_playlist("~/Music/Downloads/tracklist.csv")


def test_a_playlist_is_listed_without_fetching_a_video(monkeypatch):
    seen: list[list[str]] = []

    def finished(command, **kwargs):
        seen.append(command)
        return Finished(stdout=json.dumps({"title": "Couscoussière", "entries": [
            {"title": "Stratosfear", "channel": "Tangerine Dream"},
            {"title": "Plantasia", "channel": "Mort Garson - Topic"},
        ]}))

    monkeypatch.setattr(youtube.shutil, "which", lambda name: "/opt/homebrew/bin/yt-dlp")
    monkeypatch.setattr(youtube.subprocess, "run", finished)

    found = youtube.playlist(MUSIC)

    assert found.name == "Couscoussière"
    assert found.lines == ("Tangerine Dream - Stratosfear", "Mort Garson - Plantasia")
    assert "--flat-playlist" in seen[0] and MUSIC in seen[0]


def test_a_credit_written_into_the_title_is_the_artist():
    """The channel is the publisher, and the publisher is often not the artist — the title's own credit is."""
    for entry, expected in (
        (video("Yasmine Hamdan - I Remember I Forget ينسى وبتذكر (Official Music Video)",
               "Yasmine Hamdan"), "Yasmine Hamdan - I Remember I Forget ينسى وبتذكر"),
        (video("Timboletti - Helva Selva (Original Mix)", "iT-Music"), "Timboletti - Helva Selva"),
        (video("TAXI KEBAB - Ardina [Official Video]", "Taxi Kebab"), "TAXI KEBAB - Ardina"),
        (video('Tinariwen -  "Chaghaybou" (Full Album Stream)', "ANTI- Records"),
         "Tinariwen - Chaghaybou"),
        (video("Function - Against The Wall (OSTGULP12)", "PatV forMatt"),
         "Function - Against The Wall (OSTGULP12)"),
    ):
        assert youtube.entry_line(entry) == expected


def test_a_channel_that_stands_for_an_artist_names_it():
    """Music's own channels are the artist's, and VEVO's are the label's: `Mort Garson - Topic` and
    `ArtistVEVO` both name one, so the suffix comes off rather than into the search."""
    assert youtube.channel_artist("Mort Garson - Topic") == "Mort Garson"
    assert youtube.channel_artist("coil - Topic") == "coil"
    assert youtube.channel_artist("LinkinParkVEVO") == "LinkinPark"
    assert youtube.channel_artist("PatV forMatt") == "PatV forMatt"
    assert youtube.channel_artist("ANTI- Records") == "ANTI- Records"


def test_what_the_video_is_comes_off_the_track():
    """A title says what the video is, and a version says which recording — the first is noise, and only the
    second is a request."""
    for entry, expected in (
        (video("Going Up (Remastered)", "coil - Topic"), "coil - Going Up"),
        (video("Ricochet (Live On European Tour / 1975 / Pt. 1 / Remastered 2018)", "Tangerine Dream"),
         "Tangerine Dream - Ricochet"),
        (video("Ne Am Despartit (Original Mix)", "Raze De Soare - Topic"), "Raze De Soare - Ne Am Despartit"),
        (video("Taurus (Mixed)", "Glass Beams"), "Glass Beams - Taurus"),
        (video("Chant of the Paladin (Remastered)", "Dead Can Dance"), "Dead Can Dance - Chant of the Paladin"),
        (video("Very Never (My Mind Extends) (feat. Cathy Lucas)", "Tomaga - Topic"),
         "Tomaga - Very Never (My Mind Extends)"),
        (video("Laba Staifia (Ammar 808 Remix)", "Acid Arab"), "Acid Arab - Laba Staifia (Ammar 808 Remix)"),
        (video("Phylum (Estray Remix)", "Lerr - Topic"), "Lerr - Phylum (Estray Remix)"),
    ):
        assert youtube.entry_line(entry) == expected


def test_a_gone_video_is_not_a_line():
    """A playlist keeps the place of a video that was removed or made private, and neither is a track."""
    assert youtube.entry_line(video("[Private video]", "Nobody")) == ""
    assert youtube.entry_line(video("[Deleted video]", "Nobody")) == ""
    assert youtube.entry_line({}) == ""
    assert youtube.entry_line(video("   ", "Nobody")) == ""


def test_a_machine_without_the_tool_says_what_to_install(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: None)

    with pytest.raises(playlists.PlaylistError, match="brew install yt-dlp"):
        youtube.playlist(MUSIC)


def test_a_tool_that_fails_carries_its_own_reason(monkeypatch):
    installed(monkeypatch, stderr="ERROR: [youtube:tab] Private playlist\n", returncode=1)

    with pytest.raises(playlists.PlaylistError, match="Private playlist"):
        youtube.playlist(MUSIC)


def test_an_answer_that_is_not_a_listing_is_a_refusal(monkeypatch):
    installed(monkeypatch, stdout="not json at all")

    with pytest.raises(playlists.PlaylistError, match="not JSON"):
        youtube.playlist(MUSIC)


def test_a_listing_without_a_playlist_is_a_refusal(monkeypatch):
    """A single video answers with a dict that has a title but no entries — a run of no tracks is worse than
    a sentence."""
    installed(monkeypatch, stdout=json.dumps({"entries": [{"title": "x"}]}))

    with pytest.raises(playlists.PlaylistError, match="without a playlist"):
        youtube.playlist(MUSIC)


def test_the_text_is_a_tracklist_the_reader_already_takes():
    found = playlists.Playlist(name="Couscoussière", lines=(
        "Tangerine Dream - Stratosfear", "Acid Arab - Laba Staifia (Ammar 808 Remix)"))

    requests = tracklist.parse(found.text())

    assert [(request.artist, request.title, request.mix) for request in requests] == [
        ("Tangerine Dream", "Stratosfear", ""),
        ("Acid Arab", "Laba Staifia", "Ammar 808 Remix"),
    ]
