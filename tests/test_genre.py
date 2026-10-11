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
    """Ticket 06: `Minimal Techno` is not `Techno`, and `Dub Techno` is not either."""
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
