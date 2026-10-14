"""The matching rule, tested at its seam: `classify` and the queue predicates.

Expected values come from the rule as written — the ±1 s duration window — and GLOSSARY.md's
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


# -- length is the arbiter -------------------------------------

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
    """The length is resolved from MusicBrainz, and a track with none is review-only."""
    unresolved = Request(artist="Kraftwerk", title="Neonlicht", ref_duration=None)
    assert verdict_for("Kraftwerk - Neonlicht (Original Mix).flac", unresolved,
                       duration=300) is Label.NEEDS_YOU


def test_a_file_that_names_another_track_is_not_even_asked_about():
    """2026-10-13: the queue's one question goes only to a file that is this track at all — what the name
    says needs no reference length to read. A request that named a single remix was offered fifteen files
    of one EP, six of them other tracks entirely."""
    unresolved = Request(artist="Kraftwerk", title="Neonlicht", ref_duration=None)
    verdict = classify(candidate("Kraftwerk - Autobahn (Original Mix).flac", duration=300), unresolved)
    assert verdict.label is Label.REJECTED
    assert "a different track" in verdict.reason


def test_a_version_that_was_not_asked_for_is_not_even_asked_about():
    """Same day, same complaint: the request named the Belaria remix, and the queue offered the original."""
    unresolved = Request(artist="Costello", title="Madbot", mix="Belaria Remix", ref_duration=None)
    verdict = classify(candidate("Costello - Madbot (Original Mix).flac", duration=300), unresolved)
    assert verdict.label is Label.REJECTED
    assert "a different mix" in verdict.reason


# -- junk is ruled out outright --------------------------------------------

def test_a_clip_under_a_minute_is_a_preview_not_a_wrong_mix():
    """The preview test runs first, so the reason names the real problem."""
    verdict = classify(candidate("Other - Different (Someone Remix).mp3", duration=30, bitrate=192),
                       REQUEST)
    assert verdict.label is Label.REJECTED
    assert "preview" in verdict.reason


def test_a_lossy_file_below_the_floor_is_ruled_out_rather_than_offered():
    """Settled on 2026-10-12: a file nobody would take has no business in a list a person has to read.
    Below the floor is ruled out rather than offered, where the prototype asked about it."""
    low = candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=128)
    assert classify(low, REQUEST).label is Label.REJECTED

    track = Track(REQUEST, [low])
    assert visible(track) == [], "nothing below the floor is shown"
    assert in_review(track) is False
    assert pick(track) is None
    assert nothing_usable(track) is True, "the track is reported as having nothing usable, not as empty"


def test_the_floor_itself_is_acceptable():
    assert verdict_for("Artist - Title (Original Mix).mp3", REQUEST,
                       duration=300, bitrate=192) is Label.READY


def test_a_lossy_file_of_unknown_bitrate_is_not_rejected_for_quality():
    """Unknown is not the same as bad — only a declared bitrate below the floor rejects."""
    assert verdict_for("Artist - Title (Original Mix).mp3", REQUEST,
                       duration=300, bitrate=None) is Label.READY


# -- what is not audio at all ----------------------------------

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
    """Nothing is probed, so an ALAC-in-m4a is taken as lossy like any other."""
    for name in ("a.flac", "a.wav", "a.aiff", "a.aif"):
        assert Candidate(user="p", path=rf"M\{name}").lossless is True
    for name in ("a.m4a", "a.mp3", "a.ogg", "a.opus"):
        assert Candidate(user="p", path=rf"M\{name}").lossless is False


# -- what the name says ----------------------------------------

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
    """An ambiguous artist or title is a review, not junk."""
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
    # The 2026-10-12 decision: a version that is not the one asked for is gone, not offered with a reason.
    assert verdict_for("Artist - Title (Original Mix).flac", extended,
                       duration=300) is Label.REJECTED
    assert verdict_for("Artist - Title (Extended Mix).flac", extended,
                       duration=300) is Label.READY


def test_a_wrong_version_is_never_shown():
    """Exactly what was asked for: alternate mixes do not appear in the list a person reads."""
    extended = Request(artist="Artist", title="Title", mix="Extended Mix", ref_duration=300)
    track = Track(extended, [
        candidate("Artist - Title (Original Mix).flac", duration=300),
        candidate("Artist - Title (Extended Mix).flac", duration=300),
    ])
    assert [v.candidate.mix for v in visible(track)] == ["Extended Mix"]


@pytest.mark.parametrize("name", [
    "Artist - Title (Original Mix).flac",   # the original, named
    "Artist - Title.flac",                  # the original, bare
])
def test_a_bare_request_accepts_the_original_however_it_is_written(name):
    assert verdict_for(name, REQUEST, duration=300) is Label.READY


@pytest.mark.parametrize("name", [
    "Artist - Title (original_mix).flac",   # the same version, in the peer's spelling
    "Artist - Title (ORIGINAL MIX).flac",
    "Artist - Title (Original Version).flac",
    "Artist - Title.flac",                  # and with no suffix at all
])
def test_the_original_is_the_original_however_the_file_spells_it(name):
    """2026-10-12: a request for `(Original Mix)` against a file called `(original_mix)` was reported as
    *a different mix*, and the two are one version."""
    original = Request(artist="Artist", title="Title", mix="Original Mix", ref_duration=300)
    assert verdict_for(name, original, duration=300) is Label.READY


def test_a_longer_edit_is_taken_for_a_request_that_names_no_version():
    """The version rule's second clause, settled on 2026-10-12: an unsuffixed request accepts any
    version that is not somebody else's remix, and the ±1 s window is what keeps that honest."""
    assert verdict_for("Artist - Title (Extended Mix).flac", REQUEST, duration=300) is Label.READY


def test_original_mix_is_the_bare_request_so_it_takes_the_same_thing():
    """The version rule's first clause makes `(Original Mix)` and no suffix one request, so what one takes
    other takes — including a longer edit. Asking for `(Original Mix)` is not asking for an edit to be
    refused."""
    named = Request(artist="Artist", title="Title", mix="Original Mix", ref_duration=300)
    assert verdict_for("Artist - Title (Extended Mix).flac", named, duration=300) is Label.READY


@pytest.mark.parametrize("mix", [
    "Victor Ruiz Remix", "kate_stein_remix", "Remix", "Bootleg", "Younger Rebinds Remix 2",
])
def test_somebody_elses_remix_is_ruled_out_for_a_request_that_names_no_version(mix):
    """The one thing a bare request refuses, however close the length is — and it is refused silently, since
    a candidate nobody would take is not worth a line in the queue."""
    assert verdict_for(f"Artist - Title ({mix}).flac", REQUEST, duration=300) is Label.REJECTED


def test_a_named_version_that_was_not_asked_for_is_ruled_out():
    remix = Request(artist="Artist", title="Title", mix="Kate Stein Remix", ref_duration=300)
    assert verdict_for("Artist - Title (Ajna Samm Edit).flac", remix,
                       duration=300) is Label.REJECTED


def test_the_reason_names_the_version_taken_rather_than_the_mix():
    """A bare request that takes an edit must not say the mixes are the same."""
    verdict = classify(candidate("Artist - Title (Extended Mix).flac", duration=300), REQUEST)
    assert verdict.reason == "Extended Mix, same length as the reference"


def test_punctuation_is_never_the_difference_between_two_versions():
    remix = Request(artist="Artist", title="Title", mix="Kate Stein Remix", ref_duration=300)
    assert verdict_for("Artist - Title (kate_stein_remix).flac", remix, duration=300) is Label.READY


# -- an upgrade is optional -------------------------------------------------

def an_upgrade(**kwargs) -> Request:
    base = dict(artist="Artist", title="Title", ref_duration=300, replaces="/Music/Losy/Artist - Title.mp3")
    return Request(**{**base, **kwargs})


def test_a_lossy_offer_is_never_an_upgrade():
    """A lossy file is not better than the lossy copy held, so it is ruled out rather than offered. The pass
    always refused it; the queue kept showing it until 2026-10-12."""
    lossy = candidate("Artist - Title (Original Mix).mp3", duration=300, bitrate=320)
    assert classify(lossy, an_upgrade()).label is Label.REJECTED
    assert visible(Track(an_upgrade(), [lossy])) == []


def test_an_upgrade_nobody_can_check_is_ruled_out_too():
    """The copy already held plays, so an unverifiable file buys nothing worth a question."""
    unknown = candidate("Artist - Title (Original Mix).flac", duration=None)
    assert classify(unknown, an_upgrade()).label is Label.REJECTED


def test_a_lossless_upgrade_that_matches_is_still_taken():
    """The point of the whole upgrade path, which the two rules above must not swallow."""
    better = candidate("Artist - Title (Original Mix).flac", duration=300)
    assert classify(better, an_upgrade()).label is Label.READY


def test_a_track_that_is_not_held_still_asks_about_an_uncheckable_file():
    """The queue's remaining question: a file that may be the only copy, on a track nothing else can settle."""
    unknown = candidate("Artist - Title (Original Mix).flac", duration=None)
    assert classify(unknown, REQUEST).label is Label.NEEDS_YOU
    assert in_review(Track(REQUEST, [unknown])) is True


def test_two_mixes_of_one_title_are_two_different_tracks():
    """A remix is not a copy of the original, so it cannot be taken for it."""
    assert version_matches(candidate("Artist - Title (Victor Ruiz Remix).flac"), REQUEST) is False


# -- settling a track without asking ---------------------------

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
    """The prototype's fixture is the case that was eyeballed on the screen, and this is the drift
    detector for it.

    Verdict reasons are deliberately not compared: the port has been corrected against two resolved
    decisions since the prototype was drawn — a below-floor file goes to review rather than the bin, and
    a filename has to name the requested track — so the labels and reasons differ by design. Which
    tracks are settled, queued, or empty must not.
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
