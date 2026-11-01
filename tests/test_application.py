from dataclasses import replace

import pytest

from music_agent import state
from music_agent.application import Application
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
