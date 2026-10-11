"""Identification: the score floor, the two fallbacks, and which fields each route fills.

The three seams are fakes, so nothing here touches the network — and nothing here needs one, because the
decisions are about what to believe rather than about how to ask.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from music_agent import identify
from music_agent.config import MissingCredential
from music_agent.rules import Request


def listening(fingerprint: str = "FINGERPRINT", seconds: int = 312) -> identify.Hearing:
    class Hearing:
        def listen(self, path: Path) -> tuple[str, int]:
            return fingerprint, seconds

    return Hearing()


def deaf() -> identify.Hearing:
    class Deaf:
        def listen(self, path: Path) -> tuple[str, int]:
            raise identify.IdentifyError("fpcalc is not installed")

    return Deaf()


def candidate(recording_id: str = "rec-1", title: str = "Hit ’Em Up", artists: tuple[str, ...] = ("2Pac",),
              duration: float | None = 313.0, album: str = "", album_artists: tuple[str, ...] = (),
              compilation: bool = False, release_group: str = "") -> identify.Candidate:
    return identify.Candidate(id=recording_id, title=title, artists=artists, duration=duration,
                              album=album, album_artists=album_artists, compilation=compilation,
                              release_group=release_group)


def matches(score: float = 0.978, *recordings: identify.Candidate) -> identify.Matches:
    class Matches:
        def match(self, fingerprint: str, seconds: int) -> list[identify.Match]:
            return [identify.Match(score=score, recordings=tuple(recordings))]

    return Matches()


def nothing_matched() -> identify.Matches:
    class Nothing:
        def match(self, fingerprint: str, seconds: int) -> list[identify.Match]:
            return []

    return Nothing()


def brainz(recording: identify.Candidate | None = None,
           search: identify.Match | None = None,
           release: identify.Release | None = None) -> identify.Brainz:
    class Brainz:
        def __init__(self) -> None:
            self.asked_for_release: list[str] = []

        def recording(self, recording_id: str) -> identify.Candidate | None:
            return recording

        def search(self, request: Request) -> list[identify.Match]:
            return [search] if search is not None else []

        def release(self, release_id: str) -> identify.Release:
            self.asked_for_release.append(release_id)
            return release or identify.Release()

    return Brainz()


def request(artist: str = "2Pac", title: str = "Hit ’Em Up") -> Request:
    return Request(artist=artist, title=title)


def quiet(*_: object) -> None:
    pass


# ── the floor ────────────────────────────────────────────────────────────────


def test_a_fingerprint_above_the_floor_is_believed():
    found = identify.best([identify.Match(score=0.98, recordings=(candidate(),))])
    assert found is not None
    assert found.title == "Hit ’Em Up"


def test_a_fingerprint_below_the_floor_is_ignored():
    """Ticket 15: the one sub-0.9 match that was checked resolved to a different track."""
    assert identify.best([identify.Match(score=0.89, recordings=(candidate(),))]) is None


def test_a_score_of_exactly_the_floor_is_believed():
    assert identify.best([identify.Match(score=0.9, recordings=(candidate(),))]) is not None


def test_between_recordings_the_length_decides():
    """Only a tiebreak: both were already matched by the same fingerprint."""
    far = candidate(recording_id="far", title="Long", duration=400.0)
    near = candidate(recording_id="near", title="Right", duration=313.0)
    found = identify.best([identify.Match(score=0.99, recordings=(far, near))], seconds=312)
    assert found is not None
    assert found.id == "near"


def test_a_recording_with_no_length_never_beats_one_with_one():
    """The bug the live chain caught: AcoustID lists recordings it has no length for, and treating "no
    length" as "no distance" made a different song on the same album beat the right one — `Heartz of Men`
    over `Hit ’Em Up`, 313 s against a 312.6 s file."""
    unknown = candidate(recording_id="unknown", title="Heartz of Men", duration=None)
    right = candidate(recording_id="right", title="Hit ’Em Up", duration=313.0)
    found = identify.best([identify.Match(score=0.978, recordings=(unknown, right))], seconds=313)
    assert found is not None
    assert found.id == "right"


def test_the_lower_score_loses_even_with_the_closer_length():
    """Confidence comes first: the fingerprint's own opinion outranks a length two seconds closer."""
    sure = candidate(recording_id="sure", title="Hit ’Em Up", duration=322.0)
    unsure = candidate(recording_id="unsure", title="Heartz of Men", duration=312.0)
    found = identify.best([
        identify.Match(score=0.978, recordings=(sure,)),
        identify.Match(score=0.912, recordings=(unsure,)),
    ], seconds=313)
    assert found is not None
    assert found.id == "sure"


def test_with_no_length_at_all_the_score_still_decides():
    right = candidate(recording_id="right", title="Right", duration=313.0)
    other = candidate(recording_id="other", title="Other", duration=999.0)
    found = identify.best([identify.Match(score=0.99, recordings=(right, other))], seconds=None)
    assert found is not None
    assert found.id == "right"


# ── the routes ───────────────────────────────────────────────────────────────


def test_the_fingerprint_comes_first(tmp_path):
    found = identify.identify(
        tmp_path / "x.aiff", request(), hearing=listening(),
        matches=matches(0.98, candidate()),
        brainz=brainz(recording=candidate(), release=identify.Release(id="rel-1", title="Greatest Hits")),
    )
    assert found.source == "fingerprint"
    assert found.title == "Hit ’Em Up"


def test_a_text_search_rescues_what_the_fingerprint_could_not(tmp_path):
    """Ticket 15: about two thirds of AcoustIDs link to no MusicBrainz recording at all."""
    found = identify.identify(
        tmp_path / "x.aiff", request(), hearing=listening(),
        matches=nothing_matched(),
        brainz=brainz(recording=candidate(recording_id="rec-9", title="Found By Name"),
                      search=identify.Match(score=0.95, recordings=(candidate(recording_id="rec-9",
                                                                             title="Found By Name"),))),
    )
    assert found.source == "search"
    assert found.recording_id == "rec-9"


def test_a_text_search_below_the_floor_is_not_believed(tmp_path):
    found = identify.identify(
        tmp_path / "x.aiff", request(), hearing=listening(), matches=nothing_matched(),
        brainz=brainz(search=identify.Match(score=0.4, recordings=(candidate(),))),
    )
    assert found.source == "filename"


def test_the_filename_is_the_last_resort(tmp_path):
    found = identify.identify(tmp_path / "x.aiff", request(artist="Nobody", title="Nothing"),
                              hearing=listening(), matches=nothing_matched(), brainz=brainz())
    assert found.source == "filename"
    assert (found.artist, found.title) == ("Nobody", "Nothing")
    assert found.album == "" and found.recording_id == ""


def test_a_file_that_cannot_be_listened_to_falls_straight_through(tmp_path):
    found = identify.identify(
        tmp_path / "x.aiff", request(), hearing=deaf(), matches=matches(0.99, candidate()),
        brainz=brainz(search=identify.Match(score=0.95, recordings=(candidate(title="By Name"),))),
    )
    assert found.source == "search"


# ── the fields ───────────────────────────────────────────────────────────────


def test_the_release_fills_the_label_the_number_and_the_year(tmp_path):
    release = identify.Release(id="rel-1", title="Blurred", date="2017-10-06",
                               label="Erased Tapes", catalog_number="ERATP102DL")
    # The live chain learns which releases to ask about from the recording itself, so the fake carries
    # them the same way `MusicBrainz.recording` does.
    detail = identify.Candidate(id="rec-1", title="Hit ’Em Up", artists=("2Pac",),
                                releases=(identify.Release(id="rel-1"),))
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, candidate()),
                              brainz=brainz(recording=detail, release=release))
    assert (found.label, found.catalog_number, found.year) == ("Erased Tapes", "ERATP102DL", "2017")
    assert found.album == "Blurred"
    assert found.release_id == "rel-1"


def test_a_few_releases_are_asked_about_until_the_right_group_is_found(tmp_path):
    """Each release costs a request at one a second, so only the first few are worth asking about."""
    detail = identify.Candidate(
        id="rec-1", title="Hit ’Em Up", artists=("2Pac",),
        releases=(identify.Release(id="rel-1"), identify.Release(id="rel-2"),
                  identify.Release(id="rel-3")),
    )
    asked = brainz(recording=detail, release=identify.Release(id="rel-2", label="Death Row"))
    identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                      matches=matches(0.99, candidate()), brainz=asked)
    assert asked.asked_for_release == ["rel-1", "rel-2", "rel-3"]


def test_the_release_in_acoustids_group_wins_over_the_first(tmp_path):
    """The file that caught this was answered with its *single* — `How Do U Want It` — for a track whose
    album is `Death Row: Greatest Hits`."""
    detail = identify.Candidate(
        id="rec-1", title="Hit ’Em Up",
        releases=(identify.Release(id="rel-1", release_group_id="singlegroup"),
                  identify.Release(id="rel-2", release_group_id="albumgroup")),
    )
    singles = identify.Release(id="rel-1", title="How Do U Want It", date="1996",
                               artist="2Pac & K‐Ci & JoJo", release_group_id="singlegroup")
    albums = identify.Release(id="rel-2", title="Greatest Hits", date="1998",
                              artist="2Pac", release_group_id="albumgroup")

    class Brainz:
        def recording(self, recording_id: str) -> identify.Candidate | None:
            return detail

        def search(self, request: Request) -> list[identify.Match]:
            return []

        def release(self, release_id: str) -> identify.Release:
            return {"rel-1": singles, "rel-2": albums}[release_id]

    matched = candidate(release_group="albumgroup", album="Death Row: Greatest Hits",
                        album_artists=("Various Artists",), compilation=True)
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, matched), brainz=Brainz())
    assert found.release_id == "rel-2"
    assert found.year == "1998"
    assert found.album == "Death Row: Greatest Hits"
    assert found.album_artist == "Various Artists"


def test_a_release_without_a_label_leaves_the_field_empty(tmp_path):
    """Never invented: a release that does not carry it means the tag stays blank (the spirit)."""
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, candidate()),
                              brainz=brainz(recording=candidate(),
                                            release=identify.Release(id="rel-1", title="Untitled")))
    assert (found.label, found.catalog_number, found.year) == ("", "", "")


def test_a_compilation_takes_various_artists_as_the_album_artist(tmp_path):
    """Ticket 17: the track keeps its own credited artist; the album artist comes from the release."""
    compilation = candidate(album="Death Row: Greatest Hits",
                            album_artists=("Various Artists",), compilation=True)
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, compilation),
                              brainz=brainz(recording=compilation,
                                            release=identify.Release(id="rel-1", title="Greatest Hits")))
    assert found.album_artist == "Various Artists"
    assert found.artist == "2Pac", "the track's own credit, not the album's"


def test_a_release_that_does_not_say_compilation_uses_its_own_artist(tmp_path):
    plain = candidate(album_artists=("2Pac",))
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, plain),
                              brainz=brainz(recording=plain,
                                            release=identify.Release(id="rel-1",
                                                                     artist="2Pac")))
    assert found.album_artist == "2Pac"


def test_the_album_artist_falls_back_to_the_release_group(tmp_path):
    group = candidate(album_artists=("Kiasmos",))
    found = identify.identify(tmp_path / "x.aiff", request(), hearing=listening(),
                              matches=matches(0.99, group),
                              brainz=brainz(recording=group, release=identify.Release(id="rel-1")))
    assert found.album_artist == "Kiasmos"


# ── the live seams, as far as they can be checked without a network ──────────


def test_the_live_seams_need_their_credentials(monkeypatch, tmp_path):
    from music_agent import config as config_module

    for name in ("ACOUSTID_API_KEY", "MUSICBRAINZ_CONTACT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingCredential, match="ACOUSTID_API_KEY"):
        identify.live(config_module.DEFAULTS)
    monkeypatch.setenv("ACOUSTID_API_KEY", "a-key")
    with pytest.raises(MissingCredential, match="MUSICBRAINZ_CONTACT"):
        identify.live(config_module.DEFAULTS)


def test_an_acoustid_result_becomes_recordings_with_their_release_group():
    """The shape the live service answers with, taken from a real response."""
    result = {
        "score": 0.9783426,
        "recordings": [
            {
                "id": "0fc9df70-72ac-4164-9b8c-980705a18c61",
                "title": "Hit ’Em Up",
                "duration": 322.0,
                "artists": [{"id": "382f1005", "name": "2Pac"}],
                "releasegroups": [
                    {"id": "rg-1", "title": "Makaveli 3: Hit ’Em Up", "type": "Album",
                     "secondarytypes": ["Compilation"],
                     "artists": [{"id": "89ad4ac3", "name": "Various Artists"}]}
                ],
            }
        ],
    }
    (match,) = [_match for _match in [identify._match_of(result)]]
    assert match.score == pytest.approx(0.9783426)
    (recording,) = match.recordings
    assert recording.artists == ("2Pac",)
    assert recording.album == "Makaveli 3: Hit ’Em Up"
    assert recording.compilation is True
    assert recording.album_artists == ("Various Artists",)
