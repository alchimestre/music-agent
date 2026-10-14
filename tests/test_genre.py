"""Choosing a genre: the most specific mapping wins, and a miss is a blank rather than a guess."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_agent import genre
from music_agent.parsing import fold

VOCABULARY = ["Techno", "House", "Deep House", "Minimal / Deep Tech", "Ambient / Experimental",
              "Jazz", "Rock", "World", "Pop", "Drum & Bass"]

# `genre.table` folds both sides as it loads, so a mapping passed straight in has to be folded too.
MAPPINGS = {fold(name): mapped for name, mapped in {
    "techno": "Techno",
    "minimal techno": "Minimal / Deep Tech",
    "dub techno": "Minimal / Deep Tech",
    "deep house": "Deep House",
    "house": "House",
    "ambient": "Ambient / Experimental",
    "jazz-funk": "Jazz",
    "psychedelic rock": "Rock",
    "drum n bass": "Drum & Bass",
}.items()}


def resolve(*offered: str) -> genre.Choice:
    return genre.resolve(offered, vocabulary=VOCABULARY, mappings=MAPPINGS)


def test_a_single_mapping_is_taken():
    assert resolve("Jazz-Funk").genre == "Jazz"


def test_case_and_punctuation_do_not_matter():
    """Both sides are folded, so nobody has to remember how a mapping was written."""
    assert resolve("jazz funk").genre == "Jazz"
    assert resolve("JAZZ-FUNK").genre == "Jazz"


def test_the_most_specific_name_wins():
    """`Minimal Techno` is not `Techno`, and `Dub Techno` is not either."""
    assert resolve("Techno", "Minimal Techno").genre == "Minimal / Deep Tech"
    assert resolve("Minimal Techno", "Dub Techno").genre == "Minimal / Deep Tech"
    assert resolve("Techno").genre == "Techno"


def test_between_equally_specific_names_the_first_offered_wins():
    """The caller's order is its order of trust: Discogs styles ahead of MusicBrainz genres."""
    assert resolve("House", "Deep House").genre == "Deep House"
    assert resolve("Deep House", "House").genre == "Deep House"


def test_a_mapping_to_a_genre_the_library_does_not_have_is_ignored():
    """The vocabulary is read from the collection at run time, and the table may have drifted from it."""
    drifted = {"techno": "Techno (Old Name)"}
    choice = genre.resolve(["Techno"], vocabulary=VOCABULARY, mappings=drifted)
    assert choice.found is False


def test_nothing_that_fits_is_a_blank_and_not_a_guess():
    choice = resolve("Krautrock", "Field Recording")
    assert choice.found is False
    assert choice.genre == ""
    assert choice.considered == ("Krautrock", "Field Recording"), \
        "what was considered travels with the answer, for a human to look at"


def test_the_source_of_the_decision_is_kept():
    choice = resolve("Techno", "Dub Techno")
    assert choice.source == "Dub Techno"
    assert choice.genre == "Minimal / Deep Tech"


def test_an_empty_list_is_a_blank():
    assert genre.resolve([], vocabulary=VOCABULARY, mappings=MAPPINGS).found is False


def test_specificity_is_words_first_then_length():
    assert genre.specificity("Dub Techno") > genre.specificity("Techno")
    assert genre.specificity("Techno") > genre.specificity("Tech")


def real_vocabulary() -> list[str]:
    """The collection's own genres, when a collection is here to be read."""
    from music_agent import library

    try:
        _, genres = library.read_database()
    except Exception:                       # no rekordbox here: the test has nothing to check against
        return []
    return genres


def test_the_shipped_table_loads_and_points_only_at_real_genres():
    """A mapping that points at a genre the collection does not have would never match, and would say
    nothing about why."""
    mappings = genre.table()
    assert mappings, "genres.toml should carry the mappings"

    vocabulary = real_vocabulary()
    if not vocabulary:
        pytest.skip("no collection here to check the table against")
    allowed = {name.casefold() for name in vocabulary}
    stray = {name: mapped for name, mapped in mappings.items() if mapped.casefold() not in allowed}
    assert not stray, f"the table points outside the collection's vocabulary: {stray}"


def test_a_table_that_is_not_there_is_empty_rather_than_an_error(tmp_path: Path):
    assert genre.table(tmp_path / "absent.toml") == {}


# ── growing the vocabulary, which is allowed ---------------------------------


def test_approved_names_come_from_the_file(tmp_path):
    path = tmp_path / "genres.toml"
    path.write_text('approved = ["Goa Trance"]\n\n[map]\n"x" = "Techno"\n', encoding="utf-8")
    assert genre.approved(path) == ("Goa Trance",)
    assert genre.approved(tmp_path / "absent.toml") == ()


def test_the_vocabulary_is_the_collections_own_plus_what_is_approved():
    assert genre.vocabulary(["Techno"], ["Goa Trance"]) == ["Techno", "Goa Trance"]
    assert genre.vocabulary(["Techno"], ["techno"]) == ["Techno"], "already there, said once"
    assert genre.vocabulary(["Techno"]) == ["Techno"]


def test_only_an_approved_name_lets_a_genre_out_of_the_vocabulary():
    """`resolve` refuses a name the collection does not have, so approval is what makes a new one
    possible at all."""
    shortcut = {fold("goa trance"): "Goa Trance"}
    assert genre.resolve(["Goa Trance"], vocabulary=["Techno"], mappings=shortcut).found is False
    approved = genre.vocabulary(["Techno"], ["Goa Trance"])
    assert genre.resolve(["Goa Trance"], vocabulary=approved, mappings=shortcut).genre == "Goa Trance"


def test_proposals_need_enough_tracks_and_come_best_evidenced_first():
    found = genre.proposals({"Goa Trance": 7, "Krautrock": 4, "Dark Disco": 12}, mappings={})
    assert [proposal.name for proposal in found] == ["Dark Disco", "Goa Trance"]
    assert (found[0].name, found[0].tracks) == ("Dark Disco", 12)


def test_four_tracks_is_not_yet_a_proposal():
    assert genre.proposals({"Krautrock": 4}, mappings={}) == []
    assert genre.proposals({"Goa Trance": 5}, mappings={}) != [], "five is the bar"


def test_a_name_that_already_has_a_mapping_is_never_a_proposal():
    """It resolved, or it pointed at a genre the collection does not have — and either way the table is
    where that gets fixed, not a proposal."""
    assert genre.proposals({"Techno": 9}, mappings={fold("techno"): "Techno"}) == []


def test_the_threshold_can_be_moved():
    assert genre.proposals({"Goa Trance": 2}, mappings={}, threshold=2) != []
