from types import SimpleNamespace


import pytest

from music_agent import spotify_reconcile, state

PLAYLIST = {
    'id': 'ABCDEFGHIJKLMNOPQRSTUV',
    'name': 'Source',
    'uri': 'spotify:playlist:ABCDEFGHIJKLMNOPQRSTUV',
}
A = 'spotify:track:AAAAAAAAAAAAAAAAAAAAAA'
B = 'spotify:track:BBBBBBBBBBBBBBBBBBBBBB'
EXTRA = 'spotify:track:CCCCCCCCCCCCCCCCCCCCCC'


@pytest.fixture
def setup_reconcile(tmp_path, monkeypatch):
    config = SimpleNamespace(state_root=tmp_path / 'state', export_targets=('rekordbox', 'traktor'))
    job = state.create(config, 'cleanup')
    snapshot = {
        **PLAYLIST,
        'tracks': [{'uri': A}, {'uri': A}, {'uri': B}],
        'entries': [
            {'idx': 1, 'position': 0, 'uri': A},
            {'idx': 2, 'position': 1, 'uri': A},
            {'idx': 3, 'position': 2, 'uri': B},
        ],
    }
    monkeypatch.setattr('music_agent.spotify_source.snapshot', lambda _: snapshot)
    records = [
        {'idx': idx, 'uri': uri, 'position': position, 'delivered': True,
         'deliveries': {'rekordbox': {'status': 'success'}, 'traktor': {'status': 'success'}}}
        for idx, uri, position in [(1, A, 0), (2, A, 1), (3, B, 2)]
    ]
    monkeypatch.setattr('music_agent.receipts.build', lambda *_args, **_kwargs: {'records': records})

    class Remote:
        tracks = [A, A, B, EXTRA]
        writes = 0
        fail_before_write = False
        fail_after_write = False
        add_during_write = False

        @classmethod
        def command(cls, arguments):
            if arguments[:2] == ['playlist', 'info']:
                return {**PLAYLIST, 'total_tracks': len(cls.tracks)}
            if arguments[:2] == ['playlist', 'tracks']:
                return {'items': [{'uri': uri} for uri in cls.tracks], 'total': len(cls.tracks)}
            if arguments[:2] == ['playlist', 'remove']:
                return cls.remove_tracks(arguments[2], arguments[3:])
            raise AssertionError(f'Unexpected account command: {arguments}')

        @classmethod
        def remove_tracks(cls, _playlist, uris):
            cls.writes += 1
            if cls.fail_before_write:
                raise TimeoutError('request timed out before server mutation')
            cls.tracks = [uri for uri in cls.tracks if uri not in set(uris)]
            if cls.add_during_write:
                cls.tracks.append('spotify:track:DDDDDDDDDDDDDDDDDDDDDD')
            if cls.fail_after_write:
                raise TimeoutError('response lost after server mutation')

    monkeypatch.setattr('music_agent.spotify_account._command', Remote.command)
    return config, job, snapshot, records, Remote


def test_preview_requires_verified_occurrences_and_never_writes(setup_reconcile):
    config, job, _, _, remote = setup_reconcile

    report = spotify_reconcile.reconcile(config, job)

    assert report['eligible_occurrences'] and len(report['eligible_occurrences']) == 3
    assert report['removed'] == 0
    assert report['remaining'] == [A, A, B, EXTRA]
    assert remote.writes == 0


def test_apply_removes_only_verified_members_and_preserves_unrelated_tracks(setup_reconcile):
    config, job, _, _, remote = setup_reconcile

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert report['removed'] == 3
    assert report['remaining'] == [EXTRA]
    assert remote.writes == 1
    assert [event['event'] for event in job.events() if event['event'].startswith('spotify_remove')] == [
        'spotify_remove_intent', 'spotify_remove_result'
    ]


def test_unverified_duplicate_stays_while_independent_delivered_uri_is_removed(setup_reconcile):
    config, job, _, records, remote = setup_reconcile
    records[1]['deliveries']['traktor']['status'] = 'failed'

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert remote.tracks == [A, A, EXTRA]
    assert remote.writes == 1
    assert report['eligible_uris'] == [B]
    assert report['removed'] == 1


def test_changed_occurrence_count_blocks_instead_of_dropping_duplicate(setup_reconcile):
    config, job, _, _, remote = setup_reconcile
    remote.tracks = [A, B, EXTRA]

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert remote.tracks == [A, B, EXTRA]
    assert remote.writes == 0
    assert report['blocker']


def test_unknown_write_is_reconciled_without_repeating(setup_reconcile):
    config, job, _, _, remote = setup_reconcile
    remote.fail_after_write = True

    first = spotify_reconcile.reconcile(config, job, apply=True)
    remote.fail_after_write = False
    second = spotify_reconcile.reconcile(config, job, apply=True)

    assert first['removed'] == 3
    assert second['removed'] == 0
    assert remote.tracks == [EXTRA]
    assert remote.writes == 1
    assert second['remaining'] == [EXTRA]


def test_receipt_membership_must_match_saved_occurrences(setup_reconcile):
    config, job, _, records, remote = setup_reconcile
    records.pop(1)

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert remote.writes == 0
    assert remote.tracks == [A, A, B, EXTRA]
    assert report['eligible_uris'] == []

def test_unknown_write_without_visible_removal_is_not_retried(setup_reconcile):
    config, job, _, _, remote = setup_reconcile
    remote.fail_before_write = True

    first = spotify_reconcile.reconcile(config, job, apply=True)
    remote.fail_before_write = False
    second = spotify_reconcile.reconcile(config, job, apply=True)

    assert first['blocker']
    assert second['blocker']
    assert remote.tracks == [A, A, B, EXTRA]
    assert remote.writes == 1


def test_later_unrelated_addition_is_preserved_and_verified(setup_reconcile):
    config, job, _, _, remote = setup_reconcile
    remote.add_during_write = True

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert report['removed'] == 3
    assert report['remaining'] == [EXTRA, 'spotify:track:DDDDDDDDDDDDDDDDDDDDDD']

def test_reappeared_same_uri_is_not_removed_again(setup_reconcile):
    config, job, _, _, remote = setup_reconcile
    spotify_reconcile.reconcile(config, job, apply=True)
    remote.tracks.append(A)

    report = spotify_reconcile.reconcile(config, job, apply=True)

    assert report['blocker']
    assert remote.tracks == [EXTRA, A]
    assert remote.writes == 1


def test_later_delivery_removes_remaining_uri_without_repeating_prior_removal(setup_reconcile):
    config, job, _, records, remote = setup_reconcile
    records[1]['deliveries']['traktor']['status'] = 'failed'
    first = spotify_reconcile.reconcile(config, job, apply=True)
    records[1]['deliveries']['traktor']['status'] = 'success'

    second = spotify_reconcile.reconcile(config, job, apply=True)
    remote.tracks.append(B)
    third = spotify_reconcile.reconcile(config, job, apply=True)

    assert first['removed'] == 1
    assert second['removed'] == 2
    assert third['blocker']
    assert remote.tracks == [EXTRA, B]
    assert remote.writes == 2
