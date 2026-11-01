from dataclasses import replace

import pytest

from music_agent.config import DEFAULTS
from music_agent.settings import load_settings, merged_targets, save_settings


def configured(tmp_path):
    return replace(DEFAULTS, staging=tmp_path / 'staging', lossless=tmp_path / 'lossless',
                   lossy=tmp_path / 'lossy', state_root=tmp_path / 'private' / 'jobs',
                   peers=tmp_path / 'private' / 'peers.json', shared_dirs=(),
                   export_targets=(), soulseek_username='account', musicbrainz_contact='contact')


def test_settings_roundtrip_keeps_explicit_empty_shares_and_no_secrets(tmp_path):
    settings = configured(tmp_path)
    settings = replace(settings, secrets={settings.slsk_password_env: 'private-password'},
                       soulseek_listen_port=31000, upload_slots=4, search_interval=3.5)
    path = tmp_path / 'private' / 'settings.toml'
    save_settings(path, settings)
    loaded = load_settings(path)
    assert loaded.shared_dirs == ()
    assert loaded.export_targets == ()
    assert loaded.soulseek_listen_port == 31000
    assert loaded.upload_slots == 4
    assert loaded.staging == settings.staging
    assert loaded.secrets == {}
    assert 'private-password' not in path.read_text()


def test_rejected_form_does_not_replace_saved_settings(tmp_path):
    settings = configured(tmp_path)
    path = tmp_path / 'private' / 'settings.toml'
    save_settings(path, settings)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_settings(path, replace(settings, soulseek_listen_port=70000))
    assert path.read_bytes() == original


@pytest.mark.parametrize('exposed', ['parent', 'staging', 'private'])
def test_shares_cannot_expose_private_data(tmp_path, exposed):
    settings = configured(tmp_path)
    roots = {'parent': tmp_path, 'staging': settings.staging, 'private': settings.state_root.parent}
    with pytest.raises(ValueError, match='share'):
        save_settings(tmp_path / 'private' / 'settings.toml',
                      replace(settings, shared_dirs=(roots[exposed],)))


def test_symlink_cannot_hide_private_share_overlap(tmp_path):
    settings = configured(tmp_path)
    settings.staging.mkdir()
    alias = tmp_path / 'public'
    alias.symlink_to(settings.staging, target_is_directory=True)
    with pytest.raises(ValueError, match='share'):
        save_settings(tmp_path / 'private' / 'settings.toml',
                      replace(settings, shared_dirs=(alias,)))


def test_explicit_credentials_override_environment_without_mutation(monkeypatch):
    monkeypatch.setenv(DEFAULTS.slsk_password_env, 'environment-password')
    settings = replace(DEFAULTS, secrets={DEFAULTS.slsk_password_env: 'session-password'})
    assert settings.credential(settings.slsk_password_env) == 'session-password'
    assert DEFAULTS.credential(DEFAULTS.slsk_password_env) == 'environment-password'
    assert 'session-password' not in repr(settings)


@pytest.mark.parametrize('field,value', [('target_lufs', float('nan')),
                                        ('true_peak_db', float('inf')),
                                        ('tolerance_lu', -1)])
def test_invalid_loudness_does_not_corrupt_saved_configuration(tmp_path, field, value):
    settings = configured(tmp_path)
    path = tmp_path / 'private' / 'settings.toml'
    save_settings(path, settings)
    original = path.read_bytes()
    with pytest.raises(ValueError, match='Loudness'):
        save_settings(path, replace(settings, **{field: value}))
    assert path.read_bytes() == original


def test_saving_from_the_desktop_keeps_a_target_it_does_not_show():
    """The desktop offers one collection checkbox; a config naming more must not lose them on save."""
    assert merged_targets(('rekordbox', 'traktor'), False) == ('traktor',)
    assert merged_targets(('traktor',), True) == ('traktor', 'rekordbox')
    assert merged_targets(('rekordbox',), True) == ('rekordbox',)
    assert merged_targets((), False) == ()
