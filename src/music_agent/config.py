"""Configuration: `config.toml` at the repo root, read with the standard library.

It holds the directories, the loudness targets and tolerances, and the *names* of the environment variables
carrying the credentials — never the secrets, so this file is safe to commit.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# One string, so every credential tells the human the same thing.
ENV_HINT = "set -a; source .env; set +a"


class MissingCredential(RuntimeError):
    """The environment does not carry a credential something needs."""


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
    # Verified against this commit. Pinned rather than floating on a branch: the match rules read
    # specific fields, and a rebuild that moved them should fail loudly, not quietly.
    soulseek_command=(
        "uvx", "--from",
        "git+https://github.com/voidtype/slsk_mcp.git@df06ca17e2931246b1f139abf028a22e808da7a3",
        "slsk-mcp",
    ),
    slsk_username_env="SLSK_USERNAME",
    slsk_password_env="SLSK_PASSWORD",
    # aioslsk would listen on 60000/60001, which the omp MCP's own slsk server already holds on this
    # machine — a second binder fails its login outright. 20000 is the port Soulseek uses here.
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
    # The map keeps both of these canonical, so the CLI drives them instead of reimplementing them. The
    # normalizer needs only ffmpeg and mutagen, and mutagen is one of our own dependencies.
    convert_tool=Path("~/Music/convert_to_aiff.sh").expanduser(),
    normalize_tool=Path("~/projects/loudness_normalizer/loudness_normalizer.py").expanduser(),
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
        soulseek_listen_port=int(soulseek.get("listen_port", DEFAULTS.soulseek_listen_port)),
        soulseek_obfuscated_port=int(
            soulseek.get("obfuscated_port", DEFAULTS.soulseek_obfuscated_port)
        ),
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
