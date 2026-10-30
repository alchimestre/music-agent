"""Choosing a genre from the collection's own vocabulary, mapped through music-tagger's taxonomy."""

from __future__ import annotations

import json
from pathlib import Path

from music_agent import genre
from music_agent.parsing import fold

# A taxonomy small enough to reason about, in the shape music-tagger ships: a family, the names that map
# onto it, and the finer tiers this module never reads.
TAXONOMY = {
    "_design": {"note": "written by music-tagger, not read here"},
    "groups": [
        {"tier1": "Techno", "from": ["Techno", "Dub Techno", "Minimal Techno"],
         "tier2": {"Raw / Deep / Hypnotic": "Techno (Raw / Deep / Hypnotic)"}, "tier3": {}},
        {"tier1": "House", "from": ["House", "Deep House", "Acid House"],
         "tier2": {}, "tier3": {}},
        {"tier1": "Ambient", "from": ["Ambient", "Field Recording", "Cold Drone"],
         "tier2": {}, "tier3": {}},
    ],
}


def written(tmp_path: Path, document: dict = TAXONOMY) -> Path:
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def resolved(tmp_path: Path, *offered: str) -> genre.Choice:
    """Resolve against the written taxonomy, for a collection that holds its families."""
    path = written(tmp_path)
    return genre.resolve(offered, vocabulary=genre.families(path), mappings=genre.table(path))


# ── reading the taxonomy ------------------------------------------------------


def test_the_taxonomy_is_read_from_the_file(tmp_path):
    assert genre.document(written(tmp_path)) == TAXONOMY


def test_no_taxonomy_is_an_empty_document_and_never_an_error(tmp_path):
    """A machine without music-tagger installed gets blank genres rather than a crash."""
    assert genre.document(None) == {}
    assert genre.document("") == {}
    assert genre.document(tmp_path / "absent.json") == {}


def test_a_taxonomy_that_does_not_parse_is_empty_rather_than_an_error(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert genre.document(broken) == {}


def test_a_json_list_is_not_a_taxonomy(tmp_path):
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2, 3]", encoding="utf-8")
    assert genre.document(listed) == {}


def test_families_are_the_tier1_names_in_the_files_order(tmp_path):
    assert genre.families(written(tmp_path)) == ["Techno", "House", "Ambient"]


def test_every_from_name_maps_to_its_family_folded(tmp_path):
    assert genre.table(written(tmp_path)) == {
        fold("Techno"): "Techno",
        fold("Dub Techno"): "Techno",
        fold("Minimal Techno"): "Techno",
        fold("House"): "House",
        fold("Deep House"): "House",
        fold("Acid House"): "House",
        fold("Ambient"): "Ambient",
        fold("Field Recording"): "Ambient",
        fold("Cold Drone"): "Ambient",
    }


def test_a_name_two_groups_claim_goes_to_the_last_of_them(tmp_path):
    """Nothing stops a taxonomy listing a name twice, and nothing here notices: the later group
    overwrites the earlier one's answer."""
    document = {"groups": [
        {"tier1": "Techno", "from": ["Club"]},
        {"tier1": "House", "from": ["Club"]},
    ]}
    assert genre.table(written(tmp_path, document))[fold("Club")] == "House"


# ── what a genre may be -------------------------------------------------------


def test_the_vocabulary_is_the_collections_names_then_the_families(tmp_path):
    held = ["Techno", "Deep House"]
    assert genre.vocabulary(held, genre.families(written(tmp_path))) == [
        "Techno", "Deep House", "House", "Ambient"]


def test_a_family_the_collection_already_holds_is_not_repeated():
    """Compared folded, so `TECHNO` in the collection is the family `Techno` already."""
    assert genre.vocabulary(["TECHNO"], ["Techno", "House"]) == ["TECHNO", "House"]


def test_the_vocabulary_is_the_collections_own_when_there_are_no_families():
    assert genre.vocabulary(["Techno"]) == ["Techno"]


# ── choosing ------------------------------------------------------------------


def test_a_name_that_maps_and_is_allowed_resolves(tmp_path):
    choice = resolved(tmp_path, "Dub Techno")
    assert (choice.genre, choice.source) == ("Techno", "Dub Techno")
    assert choice.considered == ("Dub Techno",)
    assert choice.found is True


def test_a_mapping_to_a_genre_the_collection_lacks_is_ignored(tmp_path):
    """The vocabulary is the collection's, and the shipped table may have drifted from it."""
    choice = genre.resolve(["Dub Techno"], vocabulary=["House"],
                           mappings={fold("Dub Techno"): "Techno"})
    assert choice.found is False
    assert choice.genre == ""


def test_the_most_specific_of_two_matching_names_wins(tmp_path):
    choice = resolved(tmp_path, "Techno", "Dub Techno")
    assert (choice.genre, choice.source) == ("Techno", "Dub Techno")
    assert choice.considered == ("Techno", "Dub Techno")


def test_between_equally_specific_names_the_earlier_offered_wins(tmp_path):
    """`Cold Drone` and `Deep House` are both two words and ten characters, so the caller's order of
    trust decides — Discogs styles ahead of MusicBrainz genres."""
    assert resolved(tmp_path, "Cold Drone", "Deep House").genre == "Ambient"
    assert resolved(tmp_path, "Deep House", "Cold Drone").genre == "House"


def test_nothing_that_fits_is_a_blank_that_keeps_what_was_considered(tmp_path):
    choice = resolved(tmp_path, "Krautrock", "Footwork")
    assert choice.found is False
    assert choice.genre == ""
    assert choice.considered == ("Krautrock", "Footwork"), \
        "what was considered travels with the answer, for a human to look at"


def test_an_empty_offer_is_a_blank(tmp_path):
    path = written(tmp_path)
    choice = genre.resolve([], vocabulary=genre.families(path), mappings=genre.table(path))
    assert choice.found is False
    assert choice.considered == ()


def test_a_blank_from_a_consulted_table_is_still_an_answer(tmp_path):
    assert resolved(tmp_path, "Krautrock").asked is True
    assert genre.Choice().asked is True


def test_specificity_is_words_first():
    assert genre.specificity("Minimal Techno") > genre.specificity("Techno")


def test_specificity_is_length_between_the_same_number_of_words():
    assert genre.specificity("Minimal Techno") > genre.specificity("Dub Techno")
