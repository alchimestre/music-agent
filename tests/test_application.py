import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from filelock import FileLock

from music_agent import acquire, deliveries, exporting, library, operations, reference, state, store
from music_agent.application import Application, preview_source
from music_agent.config import DEFAULTS


def app_for(tmp_path):
    config = replace(DEFAULTS, state_root=tmp_path / 'private' / 'jobs',
                     peers=tmp_path / 'private' / 'peers.json', staging=tmp_path / 'staging',
                     lossless=tmp_path / 'lossless', lossy=tmp_path / 'lossy',
                     shared_dirs=(), export_targets=())
    return Application(config=config, path=tmp_path / 'private' / 'config.toml')


def test_offline_application_opens_and_preserves_submission_order(tmp_path):
    app = app_for(tmp_path)
    tracklist = tmp_path / 'tracks.txt'
    tracklist.write_text('Artist - Song\nArtist - Other\nArtist - Song\n')
    preview = app.preview(str(tracklist))
    job_id = app.enqueue(preview)
    rows = [row for row in app.snapshot() if row.job_id == job_id]
    assert [row.label for row in rows] == ['Artist - Song', 'Artist - Other', 'Artist - Song']
    assert rows[0].work_id == rows[2].work_id
    restarted = app_for(tmp_path)
    assert [(row.job_id, row.idx, row.work_id) for row in restarted.snapshot()] == [
        (row.job_id, row.idx, row.work_id) for row in rows]


def test_enqueue_rechecks_preview_against_intervening_admission(tmp_path):
    app = app_for(tmp_path)
    tracklist = tmp_path / 'tracks.txt'
    tracklist.write_text('Artist - Song\n')
    first_preview = app.preview(str(tracklist))
    second_preview = app.preview(str(tracklist))
    first = app.enqueue(first_preview)
    second = app.enqueue(second_preview)
    rows = app.snapshot()
    assert first != second
    assert rows[0].work_id == rows[1].work_id


def test_enqueued_requests_keep_version_and_replacement_provenance(tmp_path):
    app = app_for(tmp_path)
    record = state.Record(idx=1, artist='Artist', title='Song', mix='Club Mix',
                          duration_s=510, replaces='/collection/old.mp3')
    from music_agent.application import Preview

    job_id = app.enqueue(Preview('Upgrade', 'file', [record], []))
    saved = state.Job(app.config.state_root / job_id).records()[0]
    assert saved.mix == 'Club Mix'
    assert saved.duration_s == 510
    assert saved.replaces == '/collection/old.mp3'


def test_secret_store_failure_does_not_commit_settings_or_expose_secret(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    from music_agent import application
    from music_agent.credentials import CredentialStoreError

    changed = replace(app.config, secrets={app.config.slsk_password_env: 'private-password'},
                      soulseek_username='changed-account')

    def unavailable():
        raise CredentialStoreError('Native credential storage is unavailable.')

    monkeypatch.setattr(application, 'CredentialStore', unavailable)
    with pytest.raises(CredentialStoreError) as failure:
        app.save(changed)
    assert 'private-password' not in str(failure.value)
    assert not app.path.exists()
    assert app.config.soulseek_username != 'changed-account'
    app.save(changed, remember=False)
    assert app.config.credential(app.config.slsk_password_env) == 'private-password'
    assert 'private-password' not in app.path.read_text()


def test_bundled_tools_are_resolved_on_load_and_after_a_save(tmp_path, monkeypatch):
    from music_agent import resources

    app_dir = tmp_path / 'music-agent-desktop.dist'
    bin_dir = app_dir / 'resources' / 'bin'
    bin_dir.mkdir(parents=True)
    for name in ('soulseek-rs', 'ffmpeg', 'ffprobe', 'fpcalc'):
        (bin_dir / name).touch()
    monkeypatch.setattr(resources, '_application_directory', lambda: app_dir)

    app = app_for(tmp_path)
    assert app.config.soulseek_command == (str(bin_dir / 'soulseek-rs'), 'daemon')
    assert app.config.ffmpeg == str(bin_dir / 'ffmpeg')

    default = replace(app.config, soulseek_command=('soulseek-rs', 'daemon'), ffmpeg='ffmpeg')
    app.save(default, remember=False)
    assert app.config.soulseek_command == (str(bin_dir / 'soulseek-rs'), 'daemon')
    assert Application(path=app.path).config.soulseek_command == (str(bin_dir / 'soulseek-rs'), 'daemon')


def _one_track_job(app, tmp_path):
    tracklist = tmp_path / 'tracks.txt'
    tracklist.write_text('Artist - Song\n')
    job_id = app.enqueue(app.preview(str(tracklist)))
    return job_id, operations.find_job(app.config, job_id)


def test_a_polled_transfer_becomes_one_progress_event(tmp_path):
    app = app_for(tmp_path)
    _, job = _one_track_job(app, tmp_path)
    emitted = []

    app._transfer_progress(job, {'idx': 1, 'peer': 'peerA', 'progress': 0.42}, emitted.append)

    assert len(emitted) == 1
    event = emitted[0]
    assert (event.stage, event.job_id, event.idx, event.fraction) == ('transfer', job.id, 1, 0.42)
    assert 'peerA' in event.message
    assert '42%' in event.message


def test_a_transfer_without_a_fraction_emits_nothing(tmp_path):
    app = app_for(tmp_path)
    _, job = _one_track_job(app, tmp_path)
    emitted = []

    app._transfer_progress(job, {'idx': 1, 'peer': 'peerA', 'progress': None}, emitted.append)

    assert emitted == []


def test_the_queue_row_names_the_peer_a_transfer_went_to(tmp_path):
    app = app_for(tmp_path)
    job_id, job = _one_track_job(app, tmp_path)
    job.append_event('transfer_started', idx=1, peer='peerA')

    rows = [row for row in app.snapshot() if row.job_id == job_id]

    assert [row.peer for row in rows] == ['peerA']


def test_snapshot_can_read_a_legacy_job_without_memberships(tmp_path):
    app = app_for(tmp_path)
    _, current = _one_track_job(app, tmp_path)
    legacy = state.Job(app.config.state_root / 'legacy')
    legacy.root.mkdir()
    record = state.Record(idx=1, artist='Other', title='Track')
    legacy.tracks_path.write_text(json.dumps(record.to_json()) + '\n', encoding='utf-8')

    rows = app.snapshot(legacy.id)
    assert [(row.job_id, row.work_id) for row in rows] == [(legacy.id, '')]
    assert {row.job_id for row in app.snapshot()} == {current.id, legacy.id}


def test_snapshot_distinguishes_unsearched_from_empty_search(tmp_path):
    app = app_for(tmp_path)
    job_id, job = _one_track_job(app, tmp_path)
    assert app.snapshot(job_id)[0].queue_reason == "not_searched"

    job.append_event("searched", idx=1, added=0, offered=0)
    assert app.snapshot(job_id)[0].queue_reason == "nothing_offered"


def test_acquire_retries_after_an_earlier_search(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    _, job = _one_track_job(app, tmp_path)
    monkeypatch.setattr(app.runtime, 'start', lambda: SimpleNamespace(error=''))
    monkeypatch.setattr(reference, 'lookup', lambda config: None)
    retries = []
    monkeypatch.setattr(acquire, 'run', lambda *args, **kwargs: retries.append(kwargs['retry']))

    app._acquire(job, None, lambda event: None)
    job.append_event(state.SEARCHED_EVENT, idx=1)
    app._acquire(job, None, lambda event: None)

    assert retries == [False, True]


def test_reading_source_without_preview_does_not_scan_lanes(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    tracklist = tmp_path / 'tracks.txt'
    tracklist.write_text('Artist - Song\n')

    def forbidden(*args):
        pytest.fail('only a preview should read lanes or match stored work')

    monkeypatch.setattr(library, 'read_directories', forbidden)
    monkeypatch.setattr(store, 'preview_submission', forbidden)
    assert preview_source(app.config, str(tracklist)).records[0].title == 'Song'


@pytest.mark.parametrize('success', [True, False])
def test_delivery_records_outcomes_and_finishes_only_when_installed_targets_succeed(tmp_path, monkeypatch, success):
    app = app_for(tmp_path)
    job_id, job = _one_track_job(app, tmp_path)
    app.config = replace(app.config, export_targets=('rekordbox', 'traktor'))
    monkeypatch.setattr(exporting.REGISTRY['traktor'], 'available', lambda config: False)
    monkeypatch.setattr(exporting.REGISTRY['rekordbox'], 'available', lambda config: True)
    before = [deliveries.DeliveryPlan('rekordbox', 'collection', 'add file', True),
              deliveries.DeliveryPlan('traktor', '', '', False, blocked=True)]
    after = [replace(before[0], changes='unchanged'), before[1]]
    plan_reads = iter((before, after))
    monkeypatch.setattr(deliveries, 'delivery_plans', lambda config, job: next(plan_reads))
    monkeypatch.setattr(deliveries, 'delivery_receipts', lambda config, job, plans: {})
    artifacts = [deliveries.ArtifactOutcome(1, 'success' if success else 'failed', 'delivery result')]
    outcomes = [deliveries.DeliveryOutcome('rekordbox', success, 'delivery result', artifacts),
                deliveries.DeliveryOutcome('traktor', False, 'not installed')]
    monkeypatch.setattr(deliveries, 'deliver', lambda config, job, approved, **kwargs: outcomes)

    app.delivery_plans(job_id)
    assert app.deliver(job_id) == outcomes

    event = next(event for event in job.events() if event['event'] == 'delivery_finished')
    assert event['outcomes'][0]['success'] is success
    report = json.loads((job.root / 'receipt.json').read_text(encoding='utf-8'))
    assert report['job_id'] == job_id and not report['complete']
    assert job.finished() is success


def test_delivery_holds_no_state_writer_lock_while_delivering(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    job_id, _ = _one_track_job(app, tmp_path)
    monkeypatch.setattr(deliveries, 'delivery_plans', lambda config, job: [])
    app.delivery_plans(job_id)
    started, release = threading.Event(), threading.Event()
    outcomes = []

    def deliver(config, job, approved, **kwargs):
        started.set()
        assert release.wait(5)
        return []

    def contend():
        with store.writer_lock(app.config.state_root):
            pass
        competitor = FileLock(str(app.config.state_root / store.LOCK_FILE))
        competitor.acquire(timeout=0)  # a competing process would raise WriterLocked here
        competitor.release()

    monkeypatch.setattr(deliveries, 'deliver', deliver)
    worker = threading.Thread(target=lambda: outcomes.append(app.deliver(job_id)))
    worker.start()
    contender = threading.Thread(target=contend, daemon=True)
    try:
        assert started.wait(5)
        contender.start()
        contender.join(5)
        assert not contender.is_alive()
    finally:
        release.set()
        worker.join(5)
    assert outcomes == [[]]
