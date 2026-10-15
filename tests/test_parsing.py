"""Names, tested at their own seam: the fold, credits, code tails, queries and filenames.

The examples are the ones measured against the library — 117 `&` credits, 60 `feat.`, 41 label
codes, 298 files with diacritics — not invented ones.
"""

from __future__ import annotations

import pytest

from music_agent.parsing import (
    appears_in,
    artists_appear,
    credits,
    extract_mix,
    fold,
    mix_matches,
    parse_filename,
    primary_artist,
    same_credit,
    search_query,
    strip_code_tail,
)


# -- one fold --------------------------------------------------------------

def test_the_fold_unifies_case_and_diacritics():
    assert fold("Claude François") == "claude francois"
    assert fold("Claude François") == fold("claude francois")
    assert fold("Fòrabanda") == "forabanda"


def test_the_fold_treats_punctuation_as_a_separator():
    assert fold("Khazad-dûm") == "khazad dum"
    assert fold("skee_mask_slow_music_128") == "skee mask slow music 128"
    assert fold("  spaced   out  ") == "spaced out"


def test_the_fold_keeps_marks_that_are_part_of_the_letter():
    """Dropping them would collide two different names, which is worse than leaving one unmatched."""
    assert fold("かたぎり") == "かたぎり"
    assert fold("かたぎり") != fold("かたきり")
    assert fold("Ø") == "ø"


# -- credits ---------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("Califato ¾ & Ángeles Toledano", ["Califato ¾", "Ángeles Toledano"]),
    ("Acid Arab feat. Cem Yıldız", ["Acid Arab", "Cem Yıldız"]),
    ("Acid Arab ft. Cem Yıldız", ["Acid Arab", "Cem Yıldız"]),
    ("A featuring B", ["A", "B"]),
    ("A vs B", ["A", "B"]),
    ("A vs. B", ["A", "B"]),
    ("A x B", ["A", "B"]),
    ("Solo Artist", ["Solo Artist"]),
])
def test_credits_split_on_the_separators_the_library_uses(line, expected):
    assert credits(line) == expected


def test_a_name_containing_a_lone_x_is_not_split_in_two():
    """The `x` separator needs its spaces, or Malcolm X becomes Malcolm."""
    assert credits("Malcolm X") == ["Malcolm X"]


@pytest.mark.parametrize("one, other", [
    ("Altinbas x Cirkle", "Altinbas & Cirkle"),
    ("Altinbas x Cirkle", "Altinbas, Cirkle"),
    ("A feat. B", "A & B"),
    ("Rødhåd", "Rødhåd & Vril"),
    ("Dettmann | Klock", "Marcel Dettmann & Ben Klock"),
    ("Ø [Phase]", "Ø [Phase]"),
])
def test_two_spellings_of_one_credit_are_one_credit(one, other):
    """The files disagree about how a collaboration is written, and the collection gets the track twice."""
    assert same_credit(one, other) is True
    assert same_credit(other, one) is True


@pytest.mark.parametrize("one, other", [
    ("Altinbas", "Phara"),
    ("A & B", "A & C"),
    ("Marcel Dettmann", "Ben Klock"),
    ("", "Altinbas"),
])
def test_two_credits_naming_different_people_are_not_one_credit(one, other):
    assert same_credit(one, other) is False


def test_two_tracks_that_name_nobody_are_matched_on_title_and_length_alone():
    """A white label credits no one, and that is not evidence against the match — the title and the length
    have already carried it."""
    assert same_credit("", "") is True


def test_a_solo_artist_is_not_the_namesake_whose_name_contains_it():
    """`X` is a word of `Malcolm X`, so a bare subset rule would take one person for the other. A credit is
    only allowed to name fewer people when the other names more than one, which is what stops it."""
    assert same_credit("Malcolm X", "X") is False
    assert same_credit("Malcolm X", "Malcolm") is False


def test_a_repeated_credit_is_counted_once():
    assert credits("A & A") == ["A"]


def test_the_primary_artist_is_the_one_the_credit_leads_with():
    assert primary_artist("Acid Arab feat. Cem Yıldız") == "Acid Arab"
    assert primary_artist("") == ""


def test_a_featured_candidate_answers_a_request_for_the_lead_artist():
    assert artists_appear("Acid Arab", r"M\X\Acid Arab feat. Cem Yıldız - Ejma.flac") is True


def test_a_candidate_naming_one_artist_does_not_answer_a_request_that_names_both():
    assert artists_appear("Acid Arab feat. Cem Yıldız", r"M\X\Acid Arab - Ejma.flac") is False


def test_artist_presence_folds_accents_and_rejects_other_names():
    assert artists_appear("Claude François", r"M\Claude Francois - X.flac") is True
    assert artists_appear("Kiasmos", r"M\Kiasmos & Someone - X.flac") is True
    assert artists_appear("Kiasmos", r"M\Blurred\X.flac") is False


def test_the_artist_is_found_in_the_folder_when_the_name_omits_it():
    """`Artist/Album/Track` is the usual shape, so the artist is often only in the path."""
    path = r"@@uqfjk\Music\Aphex Twin\Selected Ambient Works 85-92\12 Delphium.m4a"
    assert artists_appear("Aphex Twin", path) is True
    assert artists_appear("Someone Else", path) is False


def test_a_request_with_no_artist_matches_nobody():
    assert artists_appear("", r"M\Anyone - X.flac") is False


def test_containment_is_on_word_boundaries_not_substrings():
    assert appears_in("of", "often") is False
    assert appears_in("of", "Trail of Tears") is True
    assert appears_in("Xtal", "12 Xtal") is True
    assert appears_in("Xtal", "Xylophone") is False
    assert appears_in("", "anything") is False


# -- brackets --------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Lask - Black Ibis [PRA005]", "Lask - Black Ibis"),
    ("UFO95 - Privation [digi only]", "UFO95 - Privation"),
    ("Artist - Title [RLG]", "Artist - Title"),
    ("Artist - Title", "Artist - Title"),
])
def test_a_trailing_code_or_marker_is_stripped(name, expected):
    assert strip_code_tail(name) == expected


@pytest.mark.parametrize("name", [
    "Ø [Phase] - Burden Of Proof",           # the bracket is the artist's name
    "Artist - Title (Vinyl - 24-96)",        # mixed case, so not a code
    "Artist - Title [FLAC - Log - Cue]",     # descriptive, not a code
])
def test_a_bracket_that_is_part_of_the_name_survives(name):
    assert strip_code_tail(name) == name


def test_the_mix_is_the_trailing_parenthesised_part():
    assert extract_mix("Kiasmos - Blurred (Extended Mix).flac") == "Extended Mix"
    assert extract_mix("Kiasmos - Blurred.flac") == ""
    assert extract_mix("A - B (One) (Two).flac") == "Two"


def test_a_credit_is_not_a_version():
    """2026-10-13: reading `(feat X)` as a mix made a request for `Galbi (feat Leïla Koumiya)` demand a
    version by that name, so the file that *is* the track came out a wrong version and was rejected."""
    assert extract_mix("KasbaH - Galbi (feat Leïla Koumiya).flac") == ""
    assert extract_mix("X - Y (ft. Someone).flac") == ""
    assert extract_mix("X - Y (featuring Someone).flac") == ""
    assert extract_mix("X - Y (with Someone).flac") == ""


# -- one version, however it is written -------------------------

@pytest.mark.parametrize("wanted, have", [
    ("Original Mix", "original_mix"),
    ("Original Mix", "ORIGINAL MIX"),
    ("Original Mix", ""),
    ("Original Version", "Original Mix"),
    ("", "original_mix"),
    ("", "Original Version"),
    ("Kate Stein Remix", "kate_stein_remix"),
    ("Victor  Ruiz  Remix", "victor ruiz remix"),
])
def test_one_version_spelled_two_ways_is_one_version(wanted, have):
    assert mix_matches(wanted, have) is True


@pytest.mark.parametrize("wanted, have", [
    ("Original Mix", "Extended Mix"),
    ("", "Victor Ruiz Remix"),
    ("Extended Mix", "Extended"),            # a named version must be that version, not part of it
    ("Extended Mix", ""),
    ("Original Mix", "Original Mix Remastered"),
    ("Kate Stein Remix", "Kate Stein Edit"),
])
def test_a_different_version_is_a_different_version(wanted, have):
    assert mix_matches(wanted, have) is False


# -- the query -------------------------------------------------------------

def test_the_query_is_the_artist_and_the_title_and_nothing_else():
    assert search_query("Kiasmos", "Blurred") == "kiasmos blurred"


def test_the_query_drops_a_mix_and_a_label_code():
    assert search_query("Lask", "Black Ibis (Extended Mix)") == "lask black ibis"
    assert search_query("Lask", "Black Ibis [PRA005]") == "lask black ibis"


def test_the_query_carries_no_genre_words():
    """Extra words return zero results, so none are ever added."""
    assert len(search_query("Kiasmos", "Blurred").split()) == 2


def test_the_query_is_folded_so_a_dropped_accent_can_still_match():
    assert search_query("Claude François", "Ne-am despărțit") == "claude francois ne am despartit"


# -- the filename ----------------------------------------------------------

def test_a_filename_reads_back_into_its_parts():
    parsed = parse_filename(r"Music\FLAC\Kiasmos - Blurred (Extended Mix).flac")
    assert (parsed.artist, parsed.title, parsed.mix) == ("Kiasmos", "Blurred", "Extended Mix")


def test_the_folders_do_not_change_the_artist_or_title():
    parsed = parse_filename(r"@@x\Techno\2019\Artist - Title.flac")
    assert (parsed.artist, parsed.title, parsed.mix) == ("Artist", "Title", "")


def test_a_leading_track_number_comes_off_the_title():
    assert parse_filename(r"M\Aphex Twin\SAW\12 Delphium.m4a").title == "Delphium"
    assert parse_filename(r"M\Aphex Twin\SAW\01 Xtal.m4a").title == "Xtal"
    assert parse_filename(r"M\X\A1 - Xtal.flac").title == "Xtal"


def test_a_name_that_only_looks_like_a_track_number_keeps_its_title():
    assert parse_filename("skee_mask_slow_music_128.mp3").title == "skee_mask_slow_music_128"


def test_the_folder_above_the_track_is_carried_as_an_album_hint():
    """Artist/album/track is the usual shape, so the parent folder is the album — a hint, never a rule."""
    parsed = parse_filename(r"@@uqfjk\Music\Aphex Twin\Selected Ambient Works 85-92\12 Delphium.m4a")
    assert parsed.album_hint == "Selected Ambient Works 85-92"
    assert parsed.artist == ""


def test_a_trailing_code_is_not_part_of_the_title():
    parsed = parse_filename("Lask - She's a good girl [LASK005].m4a")
    assert parsed.artist == "Lask"
    assert parsed.title == "She's a good girl"


def test_a_name_off_the_convention_comes_back_without_an_artist():
    """Rather than a guess — the caller decides whether that is a review or a refusal."""
    parsed = parse_filename("skee_mask_slow_music_128.mp3")
    assert parsed.artist == ""
    assert parsed.title == "skee_mask_slow_music_128"


def test_the_artists_bracket_keeps_the_artist_intact():
    assert parse_filename("Ø [Phase] - Burden Of Proof.flac").artist == "Ø [Phase]"
