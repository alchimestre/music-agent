import json
import subprocess

import pytest

from music_agent import spotify_account

PLAYLIST = 'spotify:playlist:ABCDEFGHIJKLMNOPQRSTUV'


def test_short_pages_preserve_order_and_duplicates(monkeypatch):
    rows = [
        {'type': 'track', 'uri': 'spotify:track:a', 'name': 'A', 'artists': ['One']},
        {'type': 'episode', 'uri': 'spotify:episode:b'},
        {'type': 'track', 'uri': 'spotify:track:a', 'name': 'A', 'artists': ['One']},
    ]
    def run(command, **kwargs):
        if 'info' in command:
            payload = {'name': 'Playlist', 'total_tracks': 3}
        else:
            offset = int(command[-1])
            payload = {'items': rows[offset:offset + 1], 'total': 3}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
    monkeypatch.setattr(spotify_account.shutil, 'which', lambda _: '/bin/spogo')
    monkeypatch.setattr(spotify_account.subprocess, 'run', run)
    playlist = spotify_account.read_playlist(PLAYLIST)
    assert [row['uri'] for row in playlist.tracks] == [row['uri'] for row in rows]
    assert playlist.tracklist.lines == ('One - A', 'One - A')
    assert playlist.tracks[2]['position'] == 2


def test_invalid_track_never_runs_mutation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid input reached subprocess')
    monkeypatch.setattr(spotify_account.subprocess, 'run', forbidden)
    with pytest.raises(spotify_account.SpotifyAccountError):
        spotify_account.add_tracks(PLAYLIST, ['--help'])


def test_changed_total_refuses_partial_result(monkeypatch):
    pages = iter([{'items': [{'uri': 'a'}], 'total': 2}, {'items': [], 'total': 3}])
    monkeypatch.setattr(spotify_account, '_command', lambda _: next(pages))
    with pytest.raises(spotify_account.SpotifyAccountError):
        spotify_account.list_playlists()


@pytest.mark.parametrize('total', [0, 1])
def test_null_collection_is_empty_only_when_total_is_zero(monkeypatch, total):
    monkeypatch.setattr(spotify_account, '_command', lambda _: {'items': None, 'total': total})
    if total:
        with pytest.raises(spotify_account.SpotifyAccountError):
            spotify_account.list_playlists()
    else:
        assert spotify_account.list_playlists() == []
