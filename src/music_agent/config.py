"""Configuration: `config.toml` at the repo root, read with the standard library.

Ticket 07: it holds the directories, the loudness targets and tolerances, and the *names* of the
environment variables carrying the credentials — never the secrets themselves, so this file is safe to
read and to commit.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/music_agent/config.py -> src/music_agent -> src -> the repo root
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"


@dataclass(frozen=True)
class Config:
    """Where things go, how loud, and which environment variables hold the credentials."""

    staging: Path
    lossless: Path
    lossy: Path
    state_root: Path
    target_lufs: float
    true_peak_db: float
    tolerance_lu: float
    musicbrainz_contact_env: str
    acoustid_key_env: str
    discogs_token_env: str
    soulseek_command: tuple[str, ...]
    soulseek_listen_port: int
    soulseek_obfuscated_port: int
    slsk_username_env: str
    slsk_password_env: str


def _resolve(value: str, base: Path) -> Path:
    """Expand `~`, and read a relative path against the config file's own directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


DEFAULTS = Config(
    staging=Path("~/Music/Downloads").expanduser(),
    lossless=Path("~/Music/Loseless Tracks").expanduser(),
    lossy=Path("~/Music/Losy Tracks").expanduser(),
    state_root=Path(".state/jobs"),
    target_lufs=-14.0,
    true_peak_db=-1.0,
    tolerance_lu=0.5,
    musicbrainz_contact_env="MUSICBRAINZ_CONTACT",
    acoustid_key_env="ACOUSTID_API_KEY",
    discogs_token_env="DISCOGS_TOKEN",
    # Ticket 03 was verified against this commit. Pinned rather than floating on a branch: the match
    # rules read specific fields, and a rebuild that moved them should fail loudly, not quietly.
    soulseek_command=(
        "uvx", "--from",
        "git+https://github.com/voidtype/slsk_mcp.git@df06ca17e2931246b1f139abf028a22e808da7a3",
        "slsk-mcp",
    ),
    slsk_username_env="SLSK_USERNAME",
    slsk_password_env="SLSK_PASSWORD",
    # aioslsk would listen on 60000/60001, which the omp MCP's own slsk server already holds on this
    # machine — a second binder fails its login outright. 20000 is the port Soulseek uses here, with
    # the obfuscated listener alongside it.
    soulseek_listen_port=20000,
    soulseek_obfuscated_port=20001,
)


def load(path: Path | None = None) -> Config:
    """Read the config, falling back to the defaults for anything it does not set.

    A missing file is not an error — the defaults are a working configuration — but a malformed one is,
    because ignoring it quietly would run the pipeline against the wrong directories.
    """
    path = DEFAULT_CONFIG_PATH if path is None else Path(path).expanduser()
    base = path.parent
    if not path.is_file():
        return DEFAULTS

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    paths = data.get("paths", {})
    loudness = data.get("loudness", {})
    credentials = data.get("credentials", {})
    soulseek = data.get("soulseek", {})

    return Config(
        staging=_resolve(paths.get("staging", str(DEFAULTS.staging)), base),
        lossless=_resolve(paths.get("lossless", str(DEFAULTS.lossless)), base),
        lossy=_resolve(paths.get("lossy", str(DEFAULTS.lossy)), base),
        state_root=_resolve(paths.get("state", str(DEFAULTS.state_root)), base),
        target_lufs=float(loudness.get("target_lufs", DEFAULTS.target_lufs)),
        true_peak_db=float(loudness.get("true_peak_db", DEFAULTS.true_peak_db)),
        tolerance_lu=float(loudness.get("tolerance_lu", DEFAULTS.tolerance_lu)),
        musicbrainz_contact_env=credentials.get("musicbrainz_contact",
                                                DEFAULTS.musicbrainz_contact_env),
        acoustid_key_env=credentials.get("acoustid_key", DEFAULTS.acoustid_key_env),
        discogs_token_env=credentials.get("discogs_token", DEFAULTS.discogs_token_env),
        soulseek_command=tuple(soulseek.get("command", DEFAULTS.soulseek_command)),
        soulseek_listen_port=int(soulseek.get("listen_port", DEFAULTS.soulseek_listen_port)),
        soulseek_obfuscated_port=int(
            soulseek.get("obfuscated_port", DEFAULTS.soulseek_obfuscated_port)
        ),
        slsk_username_env=credentials.get("soulseek_user", DEFAULTS.slsk_username_env),
        slsk_password_env=credentials.get("soulseek_password", DEFAULTS.slsk_password_env),
    )
