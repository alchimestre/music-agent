"""The matching rule, tested at its seam: `classify` and the queue predicates.

Expected values come from the grilling record — the duration table and GLOSSARY.md's
definitions of the three labels — not from re-running the code that produces them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from music_agent.rules import (
    Candidate,
    Label,
    Request,
    Track,
    classify,
    in_review,
    nothing_usable,
    pick,
    taken,
    version_matches,
    visible,
)

PROTOTYPE = Path(__file__).resolve().parent.parent / "prototype" / "review_model.py"


def candidate(name: str = "Artist - Title (Original Mix).flac", **kw) -> Candidate:
    """A candidate whose path carries the name under test."""
    return Candidate(user="peer", path=rf"Music\FLAC\{name}", **kw)


def verdict_for(name, request, **kw) -> Label:
    return classify(candidate(name, **kw), request).label


# The placeholder filenames below read `Artist - Title (Mix).ext`, so the request matches them by
# construction. A candidate whose name disagrees is now a case of its own, tested on its own.
REQUEST = Request(artist="Artist", title="Title", ref_duration=300)


# -- length is the arbiter (the design notes) -------------------------------------

@pytest.mark.parametrize("duration", [299, 300, 301])
def test_a_length_within_one_second_is_ready(duration):
    assert verdict_for("Artist - Title (Original Mix).flac", REQUEST,
                       duration=duration) is Label.READY


@pytest.mark.parametrize("duration", [298, 302])
def test_anything_more_than_a_second_off_is_rejected(duration):
    assert verdict_for("Artist - Title (Original Mix).flac", REQUEST,
                       duration=duration) is Label.REJECTED


def test_a_rejected_candidate_is_never_shown():
    track = Track(REQUEST, [candidate(duration=302)])
    assert [v.candidate for v in visible(track)] == []


def test_a_peer_that_reports_no_length_needs_a_human():
    assert verdict_for("Artist - Title (Original Mix).flac", REQUEST,
                       duration=None) is Label.NEEDS_YOU


def test_a_track_with_no_reference_length_can_never_be_settled():
    """Ticket 13: resolve the length from MusicBrainz, else the track is review-only."""
    unresolved = Request(artist="Kraftwerk", title="Neonlicht", ref_duration=None)
    assert verdict_for("Artist - Title (Original Mix).flac", unresolved,
                       duration=300) is Label.NEEDS_YOU


# -- junk is ruled out outright --------------------------------------------

def test_a_clip_under_a_minute_is_a_preview_not_a_wrong_mix():
    """The preview test runs first, so the reason names the real problem."""
    verdict = classify(candidate("Other - Different (Someone Remix).mp3", duration=30, bitrate=192),
                       REQUEST)
    assert verdict.label is Label.REJECTED
    assert "preview" in verdict.reason


def test_a_lossy_file_below_the_floor_needs_a_human_rather_than_being_discarded():
    """Ticket 08: never taken unattended, but it is not discarded either — it goes to review, so the
    only copy of a rare track can still be accepted deliberately rather than by accident."""
    low = candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=128)
    assert classify(low, REQUEST).label is Label.NEEDS_YOU

    track = Track(REQUEST, [low])
    assert in_review(track) is True
    assert pick(track) is None


def test_the_floor_itself_is_acceptable():
    assert verdict_for("Artist - Title (Original Mix).mp3", REQUEST,
                       duration=300, bitrate=192) is Label.READY


def test_a_lossy_file_of_unknown_bitrate_is_not_rejected_for_quality():
    """Unknown is not the same as bad — only a declared bitrate below the floor rejects."""
    assert verdict_for("Artist - Title (Original Mix).mp3", REQUEST,
                       duration=300, bitrate=None) is Label.READY


# -- what is not audio at all (the design notes) ----------------------------------

# Rows observed on Soulseek on 2026-10-11 in a search for "folder.jpg": the peer reports complete
# attributes for the FLACs beside these, and none at all for the artwork, cue sheets and checksums.
NOT_AUDIO = [
    ("MonoplacePreluded", r"@@music\House\202206\X\00-x.jpg", "jpg", 28201),
    ("MonoplacePreluded", r"@@music\House\202206\X\00-x.nfo", "nfo", 840),
    ("MonoplacePreluded", r"@@music\House\202206\X\00-x.m3u", "m3u", 523),
    ("MonoplacePreluded", r"@@music\House\202206\X\00-x.sfv", "sfv", 293),
]


@pytest.mark.parametrize("user,path,ext,size", NOT_AUDIO)
def test_a_non_audio_file_is_rejected_outright(user, path, ext, size):
    """These report no length because they have no audio to report one for — so they must be excluded
    by extension, never mistaken for a track whose length could not be verified."""
    verdict = classify(Candidate(user=user, path=path, size_bytes=size), REQUEST)
    assert verdict.label is Label.REJECTED
    assert ext in verdict.reason


def test_a_non_audio_file_never_reaches_the_review_queue():
    track = Track(REQUEST, [Candidate(user="p", path=r"M\x.nfo")])
    assert visible(track) == []
    assert in_review(track) is False
    assert nothing_usable(track) is True


def test_an_image_named_as_audio_is_caught_by_its_length():
    """Observed in the same search: `folder.jpg.mp3`, 52 KB and two seconds long."""
    verdict = classify(Candidate(user="toothpile", path=r"music\X\folder.jpg.mp3",
                                 duration=2, bitrate=144), REQUEST)
    assert verdict.label is Label.REJECTED
    assert "preview" in verdict.reason


def test_the_extension_alone_decides_the_lane():
    """Ticket 08 as revised: nothing is probed, so an ALAC-in-m4a is taken as lossy like any other."""
    for name in ("a.flac", "a.wav", "a.aiff", "a.aif"):
        assert Candidate(user="p", path=rf"M\{name}").lossless is True
    for name in ("a.m4a", "a.mp3", "a.ogg", "a.opus"):
        assert Candidate(user="p", path=rf"M\{name}").lossless is False


# -- what the name says (the design notes) ----------------------------------------

def test_a_different_artist_is_rejected_however_right_the_length_is():
    verdict = classify(candidate("Someone Else - Title (Original Mix).flac", duration=300), REQUEST)
    assert verdict.label is Label.REJECTED
    assert "different artist" in verdict.reason


def test_an_unrelated_title_is_rejected():
    verdict = classify(candidate("Artist - Something Else (Original Mix).flac", duration=300), REQUEST)
    assert verdict.label is Label.REJECTED
    assert "different track" in verdict.reason


def test_a_title_carrying_extra_words_is_still_taken():
    """Containment, not equality: `Trail Of Tears` answers `Trail Of`. What stops a wrong version
    getting through is the one-second window and the mix, not the title's exactness."""
    request = Request(artist="Artist", title="Trail Of", ref_duration=300)
    assert classify(candidate("Artist - Trail Of Tears.flac", duration=300),
                    request).label is Label.READY


def test_the_artist_may_be_in_the_folder_rather_than_the_name():
    """`Artist/Album/Track` is the usual shape on Soulseek, and the file is often just `12 Title`."""
    real = Candidate(user="fiercechild", path=r"@@lkqdr\Music\Artist\Some Album\12 Title.m4a",
                     duration=300)
    assert classify(real, REQUEST).label is Label.READY


def test_a_name_with_no_title_at_all_goes_to_review():
    """Ticket 04 calls an ambiguous artist or title a review, not junk."""
    verdict = classify(Candidate(user="peer", path=r"M\.flac", duration=300), REQUEST)
    assert verdict.label is Label.NEEDS_YOU
    assert "names no track" in verdict.reason


def test_a_candidate_crediting_more_artists_still_matches():
    """A featured artist does not disqualify the file that credits it."""
    verdict = classify(candidate("Artist feat. Guest - Title (Original Mix).flac", duration=300),
                       REQUEST)
    assert verdict.label is Label.READY


# -- the mix ---------------------------------------------------------------

def test_a_named_mix_must_be_the_one_asked_for():
    extended = Request(artist="Artist", title="Title", mix="Extended Mix", ref_duration=300)
    assert verdict_for("Artist - Title (Original Mix).flac", extended,
                       duration=300) is Label.NEEDS_YOU
    assert verdict_for("Artist - Title (Extended Mix).flac", extended,
                       duration=300) is Label.READY


@pytest.mark.parametrize("name", [
    "Artist - Title (Original Mix).flac",   # the original, named
    "Artist - Title.flac",                  # the original, bare
])
def test_a_bare_request_accepts_the_original_however_it_is_written(name):
    assert verdict_for(name, REQUEST, duration=300) is Label.READY


def test_two_mixes_of_one_title_are_two_different_tracks():
    """A remix is not a copy of the original, so it cannot be taken for it."""
    assert version_matches(candidate("Artist - Title (Victor Ruiz Remix).flac"), REQUEST) is False


# -- settling a track without asking (the design notes) ---------------------------

def test_lossless_beats_lossy():
    track = Track(REQUEST, [
        candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=320),
        candidate("Artist - Title (Original Mix).flac", duration=300),
    ])
    assert pick(track).candidate.lossless is True


def test_the_higher_bitrate_wins_among_lossy_files():
    track = Track(REQUEST, [
        candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=320, queue=0),
        candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=256, queue=5),
    ])
    assert pick(track).candidate.bitrate == 320


def test_the_shorter_queue_breaks_an_otherwise_equal_tie():
    track = Track(REQUEST, [
        candidate("Artist - Title (Original Mix).flac", duration=300, queue=7),
        candidate("Artist - Title (Original Mix).flac", duration=300, queue=1),
    ])
    assert pick(track).candidate.queue == 1


def test_when_the_ladder_itself_ties_the_first_offered_wins():
    """Named, not hidden: the log is what makes this arbitrary choice visible afterwards."""
    first, second = (candidate("Artist - Title (Original Mix).flac", duration=300, queue=0),
                     candidate("Artist - Title (Original Mix).flac", duration=300, queue=0))
    assert pick(Track(REQUEST, [first, second])).candidate is first


def test_a_lone_ready_candidate_settles_the_track():
    assert taken(Track(REQUEST, [candidate(duration=300)])) is True


# -- what belongs in review ------------------------------------------------

def test_no_ready_candidate_puts_a_track_in_review():
    """The fixture's Altinbas case: four copies, none of them takeable unattended."""
    track = Track(REQUEST, [
        candidate("Artist - Title (Original Mix).aiff", duration=304),          # outside the window
        candidate("Artist - Title.wav", duration=None),                          # unverifiable
        candidate("Artist - Title (Kayla Painter Remix).flac", duration=300),    # wrong mix
    ])
    assert in_review(track) is True
    assert taken(track) is False
    assert pick(track) is None


def test_a_settled_track_never_reaches_review():
    assert in_review(Track(REQUEST, [candidate(duration=300)])) is False


def test_a_track_of_nothing_but_rejections_is_reported_not_reviewed():
    track = Track(REQUEST, [
        candidate("Artist - Title (Original Mix).mp3", duration=280),    # too short by 20 s
        candidate("Artist - Title (Original Mix).flac", duration=290),   # too long by 10 s
    ])
    assert nothing_usable(track) is True
    assert in_review(track) is False


def test_a_track_with_nothing_found_is_not_usable_either():
    assert nothing_usable(Track(REQUEST, [])) is True


# -- the port reproduces the prototype it came from -------------------------

def _prototype():
    spec = importlib.util.spec_from_file_location("prototype_review_model", PROTOTYPE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_grilled_fixture_still_settles_and_queues_the_same_way():
    """The prototype's fixture is the case the user eyeballed on the screen, and this is the drift
    detector for it.

    Verdict reasons are deliberately not compared: the port has been corrected against two resolved
    decisions since the prototype was drawn — the design notes sends a below-floor file to review rather than
    the bin, and a filename now has to name the requested track — so the labels and reasons differ by
    design. Which tracks are settled, queued, or empty must not.
    """
    prototype = _prototype()
    for fixture_track in prototype.FIXTURE:
        request = Request(
            artist=fixture_track["request"]["artist"],
            title=fixture_track["request"]["title"],
            mix=fixture_track["request"]["version"] or "",
            ref_duration=fixture_track["request"]["ref_duration"],
        )
        ported = Track(request, [
            Candidate(user=c["user"], path=c["file"], duration=c["duration"],
                      bitrate=c["bitrate"], queue=c["queue"])
            for c in fixture_track["candidates"]
        ])

        assert taken(ported) == prototype.taken(fixture_track), request.title
        assert in_review(ported) == prototype.in_review(fixture_track), request.title
        assert nothing_usable(ported) == (fixture_track in prototype.nothing_usable()), request.title


def test_the_fixture_still_settles_and_queues_the_same_three_and_two():
    """The prototype's summary, as a literal: if the rules drift, this is where it shows."""
    prototype = _prototype()
    summary = prototype.summary()
    assert summary.startswith("2 in review")
    assert "2 settled automatically" in summary
    assert "1 with nothing usable" in summary
