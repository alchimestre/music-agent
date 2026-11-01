"""Native credential storage with explicit session-only operation."""

from __future__ import annotations

import sys
from dataclasses import replace

from .config import Config

SERVICE = 'music-agent'


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self) -> None:
        try:
            if sys.platform == 'win32':
                from keyring.backends.Windows import WinVaultKeyring

                self.backend = WinVaultKeyring()
            elif sys.platform == 'darwin':
                from keyring.backends.macOS import Keyring

                self.backend = Keyring()
            else:
                from keyring.backends.SecretService import Keyring

                self.backend = Keyring()
        except Exception as error:
            raise CredentialStoreError('Native credential storage is unavailable; use session-only entry.') from error

    def load(self, config: Config) -> Config:
        values = dict(config.secrets)
        try:
            for name in secret_names(config):
                stored = self.backend.get_password(SERVICE, name)
                if stored is not None:
                    values[name] = stored
        except Exception as error:
            raise CredentialStoreError('Native credential storage could not be read; use session-only entry.') from error
        return replace(config, secrets=values)

    def save(self, config: Config) -> None:
        try:
            for name in secret_names(config):
                if name in config.secrets:
                    self.backend.set_password(SERVICE, name, config.secrets[name])
        except Exception as error:
            raise CredentialStoreError('Credentials could not all be saved; use session-only entry or retry.') from error


def secret_names(config: Config) -> tuple[str, ...]:
    return config.slsk_password_env, config.acoustid_key_env, config.discogs_token_env
