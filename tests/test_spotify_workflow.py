from dataclasses import replace
import json

import pytest

from music_agent import cli, deliveries, passes, state
from music_agent.config import DEFAULTS


def test_cli_json_keeps_unavailable_target_pending_without_reacquiring(tmp_path, monkeypatch, capsys):

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs',
                     staging=tmp_path / 'staging', lossless=tmp_path / 'lossless',
                     lossy=tmp_path / 'lossy', export_targets=('rekordbox',))
    audio = tmp_path / 'track.aiff'
    audio.write_bytes(b'processed audio')
    job = state.create(config, 'Resume', [state.Record(
        1, '747', 'Example', status=state.TAGGED, local_path=str(audio), genre='Techno')])

    def forbidden(*args, **kwargs):
        raise AssertionError('A tagged resume must not contact Soulseek')

    monkeypatch.setattr(passes, 'one_pass', forbidden)
    monkeypatch.setattr(cli.config_module, 'load', lambda path: config)
    monkeypatch.setattr(deliveries, '_plan_target', lambda target, config, job, records:
                        deliveries.DeliveryPlan(target.name, 'unavailable', '', False,
                                                error='collection is unavailable', blocked=True))
    exit_code = cli.main(['spotify', 'run', '--job', job.id, '--json'])
    output = capsys.readouterr()
    result = json.loads(output.out)

    assert exit_code == 1
    assert 'blocked:' in output.err

    assert result['complete'] is False
    assert result['delivery_pending'] == {'rekordbox': [1]}
    assert result['records'][0]['status'] == state.TAGGED
    assert result['records'][0]['delivered'] is False
    assert result['records'][0]['deliveries']['rekordbox']['status'] != 'success'
    assert result['stop_reason'] == 'collection_blocked'
    assert json.loads((job.root / 'progress.json').read_text())['stage'] == 'blocked'


@pytest.mark.parametrize('lost_session, expected', [
    (True, 'displaced'), (False, 'transient_disconnected'),
])
def test_acquisition_stop_reason_is_coded_without_retrying(tmp_path, monkeypatch, lost_session, expected):
    from music_agent import acquire, spotify_workflow

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs', export_targets=())
    job = state.create(config, 'Stopped', [state.Record(1, 'Artist', 'Track')])
    attempts = []

    def disconnected(*args, **kwargs):
        attempts.append(job.id)
        raise acquire.ServerStuck('network stopped', lost_session=lost_session)

    monkeypatch.setattr(passes, 'one_pass', disconnected)
    report = spotify_workflow.run(config, job)

    assert report['stop_reason'] == expected
    assert report['complete'] is False
    assert attempts == [job.id]
    assert json.loads((job.root / 'receipt.json').read_text())['stop_reason'] == expected


def test_progress_is_bounded_and_stage_transitions_are_immediate(tmp_path, monkeypatch):
    from music_agent import spotify_workflow

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs')
    job = state.create(config, 'Progress', [])
    events = []
    monkeypatch.setattr(spotify_workflow.time, 'monotonic', lambda: 100.0)
    monitor = spotify_workflow._Monitor(job, events.append)
    for number in range(100):
        monitor.update('acquire', f'{number}: ' + 'long message ' * 100)
    monitor.update('process', 'Identifying audio')

    assert [event['stage'] for event in events] == ['acquire', 'process']
    assert len(events[0]['message']) <= 240
    assert json.loads((job.root / 'progress.json').read_text())['stage'] == 'process'


def test_retained_processing_failure_is_not_retried_after_a_new_download(tmp_path, monkeypatch):
    from collections import Counter
    from music_agent import process, spotify_workflow

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs', staging=tmp_path / 'staging',
                     lossless=tmp_path / 'lossless', lossy=tmp_path / 'lossy', export_targets=())
    retained_path = tmp_path / 'staging' / 'retained.aiff'
    retained_path.parent.mkdir()
    retained_path.write_bytes(b'retained audio')
    job = state.create(config, 'Mixed', [
        state.Record(1, 'Artist', 'Retained', status=state.RETRY_LATER,
                     local_path=str(retained_path)),
        state.Record(2, 'Artist', 'New'),
    ])
    new_path = tmp_path / 'staging' / 'new.aiff'
    attempts = Counter()

    def acquire_new_track(*args, **kwargs):
        new_path.write_bytes(b'new audio')
        records = job.records()
        records[1].status = state.DOWNLOADED
        records[1].local_path = str(new_path)
        job.write_records(records)

    def process_audio(config, processing_job, *, tools, log):
        records = processing_job.records()
        for record in records:
            if record.status in {state.DOWNLOADED, state.RETRY_LATER} and record.local_path:
                attempts[record.idx] += 1
                if record.status == state.DOWNLOADED:
                    record.status = state.TAGGED
        processing_job.write_records(records)
        return type('Summary', (), {'report': lambda self: ''})()

    monkeypatch.setattr(passes, 'one_pass', acquire_new_track)
    monkeypatch.setattr(passes, 'tools', lambda config: object())
    monkeypatch.setattr(process, 'run', process_audio)
    monkeypatch.setattr(spotify_workflow, '_deliver', lambda *args: [])

    stop_reason, blocker = spotify_workflow._stages(
        config, job, spotify_workflow._Monitor(job, lambda *_: None))

    assert attempts == {1: 1, 2: 1}
    assert job.records()[0].status == state.RETRY_LATER
    assert job.records()[1].status == state.TAGGED


@pytest.mark.parametrize(
    'failure, receipt_state, expected, expected_blocker',
    [
        ('displaced', 'missing', 'displaced', 'network stopped'),
        ('transient', 'corrupt', 'transient_disconnected', 'network stopped'),
        ('user_stopped', 'stale_complete', 'user_stopped',
         'interrupted; resume this job with --job'),
    ],
)
def test_status_json_keeps_last_operation_outcome_with_fresh_evidence(
        tmp_path, monkeypatch, capsys, failure, receipt_state, expected, expected_blocker):
    from music_agent import acquire, spotify_workflow

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs', staging=tmp_path / 'staging',
                     lossless=tmp_path / 'lossless', lossy=tmp_path / 'lossy', export_targets=())
    expected_path = tmp_path / 'track.aiff'
    expected_path.write_bytes(b'current audio')
    job = state.create(config, 'Stopped', [
        state.Record(1, 'Artist', 'Track', status=state.TAGGED,
                     local_path=str(expected_path), genre='Techno'),
        state.Record(2, 'Artist', 'Pending'),
    ])

    def disconnected(*args, **kwargs):
        if failure == 'user_stopped':
            raise KeyboardInterrupt
        raise acquire.ServerStuck('network stopped', lost_session=failure == 'displaced')
    monkeypatch.setattr(cli.config_module, 'load', lambda path: config)

    monkeypatch.setattr(passes, 'one_pass', disconnected)
    spotify_workflow.run(config, job)
    receipt_path = job.root / 'receipt.json'
    if receipt_state == 'missing':
        receipt_path.unlink()
    elif receipt_state == 'corrupt':
        receipt_path.write_text('{', encoding='utf-8')
    else:
        receipt = json.loads(receipt_path.read_text())
        receipt.update(complete=True, stage='complete', blocker='', stop_reason='complete')
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')

    exit_code = cli.main(['status', '--job', job.id, '--json'])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result['stop_reason'] == expected
    assert result['blocker'] == expected_blocker
    assert result['complete'] is False
    assert result['stage'] == 'blocked'
    assert result['records'][0]['local_path'] == str(expected_path)
    assert result['records'][0]['status'] == state.TAGGED
