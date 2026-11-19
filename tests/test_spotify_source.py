from pathlib import Path

import pytest

from music_agent import config as config_module, spotify_account, spotify_source, state


PLAYLIST_ID = "ABCDEFGHIJKLMNOPQRSTUV"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
TRACK_URI = "spotify:track:AAAAAAAAAAAAAAAAAAAAAA"


def make_config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\nlossless = "lossless"\nlossy = "lossy"\n', encoding="utf-8")
    return config_module.load(path)


def playlist(tracks, name="Set"):
    return spotify_account.AccountPlaylist(PLAYLIST_ID, name, PLAYLIST_URI, tuple(tracks))


def track(uri, name, artists, kind="track"):
    return {"type": kind, "uri": uri, "name": name, "artists": artists}


def test_admission_preserves_duplicates_and_episode_positions(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    source = playlist([
        track(TRACK_URI, "Pacific", ["747"]),
        track("spotify:episode:episode", "Talk", ["Host"], "episode"),
        track(TRACK_URI, "Pacific - Extended Mix", ["747", "808 State"]),
    ])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: source)

    job = spotify_source.admit(config, PLAYLIST_URI)
    saved = spotify_source.snapshot(job)

    assert saved == {
        "id": PLAYLIST_ID,
        "name": "Set",
        "uri": PLAYLIST_URI,
        "tracks": list(source.tracks),
        "entries": [
            {"idx": 1, "position": 0, "uri": TRACK_URI},
            {"idx": 2, "position": 2, "uri": TRACK_URI},
        ],
    }
    assert [(record.artist, record.title, record.mix) for record in job.records()] == [
        ("747", "Pacific", ""),
        ("747, 808 State", "Pacific", "Extended Mix"),
    ]
    assert spotify_source.snapshot(state.find(config, job.id)) == saved
    assert spotify_source.admit(config, PLAYLIST_URI).id == job.id


def test_reserved_admission_keeps_returned_id_and_canonical_source(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    source = playlist([track(TRACK_URI, "Pacific", ["747"])])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: source)
    previous = spotify_source.admit(config, PLAYLIST_URI)
    reserved = state.create(config, "reserved", records=[])

    admitted = spotify_source.admit(config, PLAYLIST_URI, job_id=reserved.id)

    assert admitted.id == reserved.id
    assert admitted.id != previous.id
    assert admitted.records()[0].title == "Pacific"
    assert spotify_source.snapshot(state.find(config, reserved.id))["uri"] == PLAYLIST_URI


def test_reserved_admission_never_overwrites_populated_submission(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    source = playlist([track(TRACK_URI, "Pacific", ["747"])])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: source)
    existing = spotify_source.admit(config, PLAYLIST_URI)
    replacement = playlist([track(TRACK_URI, "Replacement", ["Other"])])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: replacement)

    with pytest.raises(ValueError, match="empty"):
        spotify_source.admit(config, PLAYLIST_URI, job_id=existing.id)

    assert existing.records()[0].title == "Pacific"
    assert spotify_source.snapshot(existing)["tracks"] == list(source.tracks)


def test_changed_ordered_source_content_creates_new_job(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    current = playlist([track(TRACK_URI, "First", ["One"])])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: current)
    first = spotify_source.admit(config, PLAYLIST_URI)

    current = playlist([track(TRACK_URI, "Changed", ["One"])])
    second = spotify_source.admit(config, PLAYLIST_URI)

    assert second.id != first.id
    assert spotify_source.snapshot(second)["tracks"] == list(current.tracks)


def test_unsupported_rows_stay_raw_without_admitted_indices(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    source = playlist([
        track("spotify:episode:episode", "Episode", ["Host"], "episode"),
        track("spotify:track:bad", "No artist", [], "track"),
    ])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: source)

    with pytest.raises(ValueError, match="no supported tracks"):
        spotify_source.admit(config, PLAYLIST_URI)

    assert state.all_jobs(config) == []


def test_mixed_snapshot_keeps_unsupported_source_rows(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    source = playlist([
        track("spotify:episode:episode", "Episode", ["Host"], "episode"),
        track("spotify:track:short", "Invalid ID", ["Artist"]),
        track("spotify:local:artist:album:title", "Local only", ["Artist"]),
        track(TRACK_URI, "Valid", ["Artist"]),
    ])
    monkeypatch.setattr(spotify_account, "read_playlist", lambda _: source)

    job = spotify_source.admit(config, PLAYLIST_URI)

    assert spotify_source.snapshot(job)["tracks"] == list(source.tracks)
    assert spotify_source.snapshot(job)["entries"] == [
        {"idx": 1, "position": 3, "uri": TRACK_URI}
    ]


def test_playlist_name_resolution_requires_unique_exact_name(monkeypatch):
    rows = [
        {"id": "ABCDEFGHIJKLMNOPQRSTUV", "name": "My Set", "uri": PLAYLIST_URI},
        {"id": "BCDEFGHIJKLMNOPQRSTUVW", "name": "My Setlist", "uri": "spotify:playlist:BCDEFGHIJKLMNOPQRSTUVW"},
    ]
    monkeypatch.setattr(spotify_account, "list_playlists", lambda: rows)
    fetched = []
    monkeypatch.setattr(spotify_account, "read_playlist", lambda source: fetched.append(source) or playlist([], "My Set"))

    result = spotify_source.resolve("my set")

    assert fetched == [PLAYLIST_URI]
    assert result.name == "My Set"


def test_ambiguous_case_insensitive_name_is_rejected(monkeypatch):
    monkeypatch.setattr(spotify_account, "list_playlists", lambda: [
        {"id": "ABCDEFGHIJKLMNOPQRSTUV", "name": "My Set"},
        {"id": "BCDEFGHIJKLMNOPQRSTUVW", "name": "MY SET"},
    ])

    with pytest.raises(ValueError, match="ambiguous"):
        spotify_source.resolve("my set")


def test_source_id_validation_is_not_treated_as_a_name(monkeypatch):
    monkeypatch.setattr(spotify_account, "list_playlists", lambda: pytest.fail("invalid source treated as name"))

    with pytest.raises(ValueError):
        spotify_source.resolve("spotify:playlist:not-an-id")


def test_bare_id_and_uri_admission_use_real_account_reader(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    raw = track(TRACK_URI, "Pacific", ["747"])

    def command(arguments):
        if arguments[:2] == ["playlist", "info"]:
            return {"name": "Set", "uri": PLAYLIST_URI, "total_tracks": 1}
        if arguments[:2] == ["playlist", "tracks"]:
            return {"items": [raw], "total": 1}
        raise AssertionError(f"Unexpected account command: {arguments}")

    monkeypatch.setattr(spotify_account, "_command", command)
    job = spotify_source.admit(config, PLAYLIST_ID)

    assert job.records()[0].artist == "747"
    assert spotify_source.admit(config, PLAYLIST_URI).id == job.id
    assert spotify_source.snapshot(job)["entries"] == [{"idx": 1, "position": 0, "uri": TRACK_URI}]
