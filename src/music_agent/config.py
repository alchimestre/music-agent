"""Configuration: `config.toml`, read with the standard library.

With no path given, the file beside the package is read first — a checkout — then
`~/.config/music-agent/config.toml`, which is where an installed copy finds one; with neither, the defaults are
a working configuration. `config.example.toml` is the template that ships — copy it and edit. It holds the
directories, the loudness targets and tolerances, the search pace, the programs the pipeline drives, and the
*names* of the environment variables carrying the credentials — never the secrets, so it is safe to commit.

The Soulseek listening ports are not in it either: they come from the environment
(`SLSK_LISTEN_PORT`, `SLSK_OBFUSCATED_PORT`), like the credentials.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

# One string, so every credential tells the human the same thing.
ENV_HINT = "set -a; source .env; set +a"


class MissingCredential(RuntimeError):
    """The environment does not carry a credential something needs."""


# src/music_agent/config.py -> src/music_agent -> src -> the repo root
HERE = Path(__file__).resolve().parents[2]
# The file this machine reads. A fresh clone has none, and the defaults are a working configuration.
DEFAULT_CONFIG_PATH = HERE / "config.toml"
# The template that ships. Its values are the defaults, so copying it changes nothing — which is what the tests
# check, rather than checking this machine's own file.
EXAMPLE_CONFIG_PATH = HERE / "config.example.toml"
# Where an installed copy looks for its configuration: `uv tool install` puts the package inside a virtualenv,
# and the file beside it is not a place to edit. Expanded when it is read, not at import.
USER_CONFIG_PATH = Path("~/.config/music-agent/config.toml")


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
    # How much of the acquisition runs at once. The first three are the child's own ceilings, handed to it
    # as SLSK_* variables; `workers` is how many tracks a pass works on at a time.
    max_downloads: int
    max_searches: int
    max_operations: int
    workers: int
    # A floor on the gap between two searches we ask for, in seconds. Ours rather than the child's:
    # nothing in the pinned build paces searches, and nothing reports one being ignored. 0 turns it off.
    search_interval: float
    slsk_username_env: str
    slsk_password_env: str
    convert_tool: Path
    normalize_tool: Path


def _resolve(value: str, base: Path) -> Path:
    """Expand `~`, and read a relative path against the config file's own directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _port_from_env(name: str, default: int) -> int:
    """A listening port from the environment, where this machine keeps the network's own facts."""
    value = os.environ.get(name)
    return int(value) if value else default


def _listening_ports() -> dict[str, int]:
    """Both ports, under the field names `Config` gives them."""
    return {
        "soulseek_listen_port": _port_from_env("SLSK_LISTEN_PORT", DEFAULTS.soulseek_listen_port),
        "soulseek_obfuscated_port": _port_from_env(
            "SLSK_OBFUSCATED_PORT", DEFAULTS.soulseek_obfuscated_port
        ),
    }


DEFAULTS = Config(
    staging=Path("~/Music/music-agent/staging").expanduser(),
    lossless=Path("~/Music/music-agent/lossless").expanduser(),
    lossy=Path("~/Music/music-agent/lossy").expanduser(),
    state_root=Path("~/Music/music-agent/state").expanduser(),
    target_lufs=-14.0,
    true_peak_db=-1.0,
    tolerance_lu=0.5,
    musicbrainz_contact_env="MUSICBRAINZ_CONTACT",
    acoustid_key_env="ACOUSTID_API_KEY",
    discogs_token_env="DISCOGS_TOKEN",
    # Verified against this commit. Pinned rather than floating on a branch: the match rules read
    # specific fields, and a rebuild that moved them should fail loudly, not quietly.
    soulseek_command=(
        "uvx", "--from",
        "git+https://github.com/voidtype/slsk_mcp.git@df06ca17e2931246b1f139abf028a22e808da7a3",
        "slsk-mcp",
    ),
    slsk_username_env="SLSK_USERNAME",
    slsk_password_env="SLSK_PASSWORD",
    # aioslsk's defaults are 60000/60001, which a second Soulseek client may already hold.
    soulseek_listen_port=20000,
    soulseek_obfuscated_port=20001,
    # Twenty transfers at a time, not one: the server's own defaults are 3 transfers, 4 searches and
    # *one* operation — that last is the connection lock, which holds a search for its whole window, so
    # searches cannot overlap at all until it is raised.
    max_downloads=20,
    max_searches=8,
    max_operations=8,
    workers=20,
    # A pass once fired 330 searches in six minutes — eight at a time — and every search after the first
    # seconds was answered with nothing, silently. Five seconds is twelve a minute, a quarter of that
    # rate. The threshold is a guess, which is why it is a knob.
    search_interval=5.0,
    # Both ship with the project: `tools/convert_to_aiff.sh` and the normalizer submodule under `vendor/`.
    convert_tool=HERE / "tools/convert_to_aiff.sh",
    normalize_tool=HERE / "vendor/loudness_normalizer/loudness_normalizer.py",
)


def load(path: Path | None = None) -> Config:
    """Read the config, falling back to the defaults for anything it does not set.

    A missing file is not an error — the defaults are a working configuration — but a malformed one is,
    because ignoring it quietly would run the pipeline against the wrong directories.

    With no path given: the file beside the package (a checkout), then the user's own, then the defaults.
    """
    if path is None:
        path = next((one for one in (DEFAULT_CONFIG_PATH, USER_CONFIG_PATH.expanduser()) if one.is_file()),
                    DEFAULT_CONFIG_PATH)
    else:
        path = Path(path).expanduser()
    base = path.parent
    if not path.is_file():
        # The defaults are a working configuration, but the ports still come from the environment.
        return replace(DEFAULTS, **_listening_ports())

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    paths = data.get("paths", {})
    loudness = data.get("loudness", {})
    credentials = data.get("credentials", {})
    soulseek = data.get("soulseek", {})
    tools = data.get("tools", {})

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
        **_listening_ports(),
        max_downloads=int(soulseek.get("max_downloads", DEFAULTS.max_downloads)),
        max_searches=int(soulseek.get("max_searches", DEFAULTS.max_searches)),
        max_operations=int(soulseek.get("max_operations", DEFAULTS.max_operations)),
        workers=int(soulseek.get("workers", DEFAULTS.workers)),
        search_interval=float(soulseek.get("search_interval", DEFAULTS.search_interval)),
        slsk_username_env=credentials.get("soulseek_user", DEFAULTS.slsk_username_env),
        slsk_password_env=credentials.get("soulseek_password", DEFAULTS.slsk_password_env),
        convert_tool=_resolve(tools.get("convert", str(DEFAULTS.convert_tool)), base),
        normalize_tool=_resolve(tools.get("normalize", str(DEFAULTS.normalize_tool)), base),
    )
