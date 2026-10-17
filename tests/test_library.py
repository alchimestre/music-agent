"""The library: what is already held, and the vocabulary a genre must come from."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import library


def held(artist: str, title: str, seconds: int | None, path: str = "") -> library.Held:
    return library.Held(artist=artist, title=title, seconds=seconds, path=path)


@pytest.fixture
def shelf() -> library.Library:
    return library.Library(
        rows=[
            held("Kiasmos", "Blurred", 449, "/Music/Loseless Tracks/Kiasmos - Blurred.aiff"),
            held("Skee Mask", "Junt", 214, "/Music/Loseless Tracks/Skee Mask - Junt.aiff"),
            held("ROSALÍA", "De plata", 269, "/Music/Losy Tracks/ROSALÍA - De plata.m4a"),
            held("Unknown", "No length", None, "/Music/Loseless Tracks/Unknown - No length.aiff"),
        ],
        genres=["Techno", "House", "Ambient / Experimental"],
    )


def test_a_track_the_collection_holds_is_found(shelf):
    found = shelf.held("Kiasmos", "Blurred", 449)
    assert found is not None
    assert found.path.endswith("Kiasmos - Blurred.aiff")


def test_a_length_inside_the_window_still_counts(shelf):
    assert shelf.held("Skee Mask", "Junt", 215) is not None


def test_a_length_outside_the_window_is_a_different_track(shelf):
    """The tune may be held, but that version is not — the same ±1 s rule the matcher uses."""
    assert shelf.held("Kiasmos", "Blurred", 460) is None


def test_accents_and_case_do_not_change_the_answer(shelf):
    """Comparison is folded, because the files and the database disagree about both."""
    assert shelf.held("Rosalia", "De plata", 269) is not None
    assert shelf.held("rosalía", "de plata", 269) is not None
    assert shelf.held("KIASMOS", "BLURRED", 449) is not None


def test_a_track_the_collection_does_not_hold_is_not_found(shelf):
    assert shelf.held("Someone", "Else", 300) is None


def test_one_collaboration_written_two_ways_is_held_once():
    """The files disagree about the credit, so the match rides on the title and the length, with the credit
    checked by name beside them — otherwise the same track is acquired a second time."""
    shelf = library.Library(rows=[held("Altinbas x Cirkle", "Time in Motion", 350, "/L/a.aiff")], genres=[])
    assert shelf.held("Altinbas & Cirkle", "Time in Motion", 350) is not None
    assert shelf.held("Altinbas, Cirkle", "Time in Motion", 350) is not None
    assert shelf.held("Phara", "Time in Motion", 350) is None
    assert shelf.held("Altinbas & Cirkle", "Time in Motion", 353) is None


def test_an_unknown_length_never_counts_as_held(shelf):
    """Nothing to compare, so the answer is no: a search costs less than a wrong skip."""
    assert shelf.held("Unknown", "No length", None) is None
    assert shelf.held("Unknown", "No length", 200) is None
    assert shelf.held("Kiasmos", "Blurred", None) is None


def test_the_genre_vocabulary_is_the_librarys_own(shelf):
    assert shelf.genres() == ["Techno", "House", "Ambient / Experimental"]


def test_the_genres_handed_out_cannot_be_edited_in_place(shelf):
    shelf.genres().append("Nothing")
    assert "Nothing" not in shelf.genres()


def test_rows_without_a_title_are_ignored():
    shelf = library.Library(rows=[held("Somebody", "", 100)], genres=[])
    assert len(shelf) == 0


def test_paths_are_compared_nfc_and_casefolded():
    """Raw equality matches only 1342 of 1627 paths."""
    assert library.path_key("/Music/Losy Tracks/ROSALÍA - De plata.m4a") == \
        library.path_key("/Music/Losy Tracks/Rosalía - De plata.m4a")


# ── reading the directories ──────────────────────────────────────────────────


def write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    """A real, if tiny, WAV, so a length can be read from a header rather than mocked."""
    frames = int(rate * seconds)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def make_config(tmp_path: Path) -> config_module.Config:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\nlossless = "{tmp_path / "Loseless Tracks"}"\n'
        f'lossy = "{tmp_path / "Losy Tracks"}"\nstate = "state"\n',
        encoding="utf-8",
    )
    return config_module.load(config_path)


def test_the_directories_are_read_with_their_lengths(tmp_path):
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    write_wav(config.lossless / "Kiasmos - Blurred.wav", seconds=3.0)

    found = library.read_directories(config)

    assert len(found) == 1
    assert (found[0].artist, found[0].title) == ("Kiasmos", "Blurred")
    assert found[0].seconds == 3


def test_a_file_whose_length_cannot_be_read_is_still_listed(tmp_path):
    """It is honest about not knowing, and `held` will not match on a length it does not have."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    (config.lossless / "Nobody - Nothing.flac").write_bytes(b"not a flac at all")

    found = library.read_directories(config)

    assert len(found) == 1
    assert found[0].seconds is None


def test_artwork_beside_the_tracks_is_not_a_track(tmp_path):
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    write_wav(config.lossless / "Kiasmos - Blurred.wav", seconds=2.0)
    (config.lossless / "cover.jpg").write_bytes(b"jpeg")
    (config.lossless / "notes.txt").write_text("hello")

    assert len(library.read_directories(config)) == 1


def test_a_missing_directory_is_not_an_error(tmp_path):
    config = make_config(tmp_path)
    assert library.read_directories(config) == []


def test_the_directories_and_the_database_are_one_shelf(tmp_path):
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    write_wav(config.lossless / "Kiasmos - Blurred.wav", seconds=3.0)

    shelf = library.Library(library.read_directories(config), [])
    assert shelf.held("Kiasmos", "Blurred", 3) is not None


# ── what nothing points at (the map's leftovers) ─────────────────────────────


def make_config_with_staging(tmp_path: Path) -> config_module.Config:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\nlossless = "{tmp_path / "Loseless Tracks"}"\n'
        f'lossy = "{tmp_path / "Losy Tracks"}"\n'
        f'staging = "{tmp_path / "Downloads"}"\n'
        'state = "state"\n',
        encoding="utf-8",
    )
    return config_module.load(config_path)


def test_unpointed_separates_the_three_ways_a_run_leaves_something_behind(tmp_path, monkeypatch):
    config = make_config_with_staging(tmp_path)
    config.lossless.mkdir(parents=True)
    config.staging.mkdir(parents=True)
    write_wav(config.lossless / "Kiasmos - Blurred.wav", seconds=3.0)
    write_wav(config.lossless / "Nobody - Nothing.wav", seconds=4.0)
    (config.staging / "01 - Left Behind.aiff.part").write_bytes(b"a cancelled transfer")
    (config.staging / "Kiasmos - Blurred.wav").write_bytes(b"the run's own staged file")
    gone = held("Gone", "Missing", 200, path=str(config.lossless / "Gone - Missing.wav"))
    monkeypatch.setattr(library, "read_database",
                        lambda: ([held("Kiasmos", "Blurred", 3, path=str(config.lossless / "Kiasmos - Blurred.wav")), gone], []))

    leftovers = library.unpointed(config, claimed=[str(config.staging / "Kiasmos - Blurred.wav")])

    assert [path.name for path in leftovers.staged] == ["01 - Left Behind.aiff.part"]
    assert [path.name for path in leftovers.lane] == ["Nobody - Nothing.wav"]
    assert leftovers.gone == [gone.path]
    assert leftovers.empty() is False


def test_nothing_left_behind_is_reported_as_such(tmp_path, monkeypatch):
    config = make_config_with_staging(tmp_path)
    config.lossless.mkdir(parents=True)
    write_wav(config.lossless / "Kiasmos - Blurred.wav", seconds=3.0)
    monkeypatch.setattr(library, "read_database",
                        lambda: ([held("Kiasmos", "Blurred", 3,
                                       path=str(config.lossless / "Kiasmos - Blurred.wav"))], []))

    assert library.unpointed(config).empty() is True


def test_a_stored_title_is_split_the_way_a_tracklist_splits_one(tmp_path, monkeypatch):
    """2026-10-13: the collection indexed a row under its whole stored title while a request looked for the
    bare one, so `Galbi (feat Leïla Koumiya)` read as a track the collection did not hold — searched as if
    it were new, carrying no upgrade flag, and its queue offered copies of the entire EP."""
    from types import SimpleNamespace

    row = SimpleNamespace(Title="Galbi (feat Leïla Koumiya)", Artist=SimpleNamespace(Name="KasbaH"),
                          Length=199, FolderPath="/Music/Losy Tracks/Galbi.m4a", Genre=None, ID=1)
    monkeypatch.setattr("pyrekordbox.Rekordbox6Database",
                        lambda *args, **kwargs: SimpleNamespace(get_content=lambda: [row], get_genre=lambda: []))
    monkeypatch.setattr(library, "read_directories", lambda config: [])

    shelf = library.open(make_config(tmp_path))

    found = shelf.held("KasbaH", "Galbi", 199)
    assert found is not None, "the row answers to the bare title a tracklist produces"
    assert (found.title, found.mix) == ("Galbi", ""), "a credit is neither the title nor a version"


def test_a_file_in_a_folder_inside_a_lane_is_still_held(tmp_path):
    """The README says a lane holds every file in it; one level of `iterdir` missed organised albums."""
    config = make_config(tmp_path)
    album = config.lossless / "Kiasmos - II"
    album.mkdir(parents=True)
    write_wav(album / "Kiasmos - Blurred.wav", seconds=3.0)

    assert [track.title for track in library.read_directories(config)] == ["Blurred"]
