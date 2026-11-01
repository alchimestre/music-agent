"""Validated nonsecret desktop settings in the platform configuration directory."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .config import DEFAULTS, Config, load


def settings_path() -> Path:
    from platformdirs import user_config_path

    return user_config_path('music-agent', appauthor=False) / 'config.toml'


def load_settings(path: Path | None = None) -> Config:
    path = path or settings_path()
    if path.is_file():
        return load(path)
    from platformdirs import user_data_path

    root = user_data_path('music-agent', appauthor=False)
    music = Path.home() / 'Music' / 'music-agent'
    return replace(DEFAULTS, staging=music / 'staging', lossless=music / 'lossless',
                   lossy=music / 'lossy', state_root=root / 'jobs', peers=root / 'peers.json',
                   shared_dirs=(), export_targets=(), soulseek_state_dir=str(root / 'soulseek'))


def merged_targets(configured: tuple[str, ...], rekordbox_on: bool) -> tuple[str, ...]:
    """The desktop shows a checkbox for one collection; every other configured target is carried
    through, so saving settings there never drops a target the CLI still runs."""
    kept = tuple(target for target in configured if target != "rekordbox" or rekordbox_on)
    if rekordbox_on and "rekordbox" not in kept:
        return kept + ("rekordbox",)
    return kept


def save_settings(path: Path, config: Config) -> None:
    validate_settings(config, path)
    text = settings_text(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_settings(config: Config, path: Path) -> None:
    loudness = (config.target_lufs, config.true_peak_db, config.tolerance_lu)
    if not all(math.isfinite(value) for value in loudness) or config.tolerance_lu < 0:
        raise ValueError('Loudness values must be finite; tolerance cannot be negative.')
    if not 1 <= config.soulseek_listen_port <= 65535:
        raise ValueError('Peer listening port must be between 1 and 65535.')
    if min(config.workers, config.max_downloads, config.upload_slots) < 1:
        raise ValueError('Workers, downloads, and upload slots must be positive.')
    if config.workers > config.max_downloads:
        raise ValueError('Track workers cannot exceed simultaneous downloads.')
    if not math.isfinite(config.search_interval) or config.search_interval < 0:
        raise ValueError('Search interval must be finite and nonnegative.')
    if config.soulseek_server:
        host, separator, port = config.soulseek_server.rpartition(':')
        if not separator or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError('Soulseek server must be host:port with a valid port.')
    outputs = (config.staging.resolve(), config.lossless.resolve(), config.lossy.resolve())
    if any(overlap(left, right) for index, left in enumerate(outputs) for right in outputs[index + 1:]):
        raise ValueError('Staging and output directories must not overlap.')
    private = (config.staging, config.state_root, config.peers.parent, path.parent)
    if config.soulseek_state_dir:
        private += (Path(config.soulseek_state_dir),)
    for shared in config.shared_dirs or ():
        if any(overlap(shared.resolve(), root.resolve()) for root in private):
            raise ValueError('A shared folder overlaps staging or private application data.')
        check_access(shared, writable=False)
    for directory in outputs:
        check_access(directory, writable=True)
    if config.taxonomy is not None and not config.taxonomy.is_file():
        raise ValueError('The selected taxonomy file does not exist.')


def overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def check_access(path: Path, *, writable: bool) -> None:
    existing = path.resolve()
    while not existing.exists():
        existing = existing.parent
    mode = os.R_OK | os.X_OK
    if writable:
        mode |= os.W_OK
    if not existing.is_dir() or not os.access(existing, mode):
        raise ValueError(f'Directory is not accessible: {path}')
    if not writable and not path.is_dir():
        raise ValueError(f'Shared folder does not exist: {path}')


def settings_text(config: Config) -> str:
    sections = {
        'paths': {'staging': config.staging, 'lossless': config.lossless, 'lossy': config.lossy,
                  'state': config.state_root, 'peers': config.peers},
        'loudness': {'target_lufs': config.target_lufs, 'true_peak_db': config.true_peak_db,
                     'tolerance_lu': config.tolerance_lu},
        'credentials': {'musicbrainz_contact': config.musicbrainz_contact_env,
                        'acoustid_key': config.acoustid_key_env, 'discogs_token': config.discogs_token_env,
                        'soulseek_user': config.slsk_username_env,
                        'soulseek_password': config.slsk_password_env, 'contact': config.musicbrainz_contact},
        'soulseek': {'command': list(config.soulseek_command), 'state_dir': config.soulseek_state_dir,
                     'username': config.soulseek_username, 'server': config.soulseek_server,
                     'listen_port': config.soulseek_listen_port, 'upload_slots': config.upload_slots,
                     'max_downloads': config.max_downloads, 'workers': config.workers,
                     'search_interval': config.search_interval, 'maxretry': config.maxretry,
                     'findtime_days': config.findtime_days, 'bantime_days': config.bantime_days,
                     'daemon_address': config.daemon_address},
        'export': {'targets': list(config.export_targets)},
        'genres': {'taxonomy': str(config.taxonomy)} if config.taxonomy else {},
    }
    if config.shared_dirs is not None:
        sections['paths']['shared'] = [str(root) for root in config.shared_dirs]
    lines = []
    for section, values in sections.items():
        lines.append(f'[{section}]')
        for key, value in values.items():
            if isinstance(value, Path):
                value = str(value)
            lines.append(f'{key} = {json.dumps(value, ensure_ascii=False)}')
        lines.append('')
    return '\n'.join(lines)
