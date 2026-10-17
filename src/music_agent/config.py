"""Configuration: `config.toml`, read with the standard library.

With no path given, the file beside the package is read first — a checkout — then
`~/.config/music-agent/config.toml`, which is where an installed copy finds one; with neither, the defaults are
a working configuration. `config.example.toml` is the template that ships — copy it and edit. It holds the
directories, the loudness targets and tolerances, the search pace, the programs the pipeline drives, and the
*names* of the environment variables carrying the credentials — never the secrets, so it is safe to commit.

The Soulseek listening ports are not in it either: they come from the environment
(`SLSK_LISTEN_PORT`, `SLSK_USERNAME`, `SLSK_PASSWORD`), like the credentials.
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
    # The ignore list, which outlives any run: `.state/jobs/` holds runs, and this is the one file that
    # belongs to the account rather than to a pass.
    peers: Path
    target_lufs: float
    true_peak_db: float
    tolerance_lu: float
    musicbrainz_contact_env: str
    acoustid_key_env: str
    discogs_token_env: str
    soulseek_command: tuple[str, ...]
    # Where the daemon keeps its socket and its durable transfer state. Empty means `SOULSEEK_STATE_DIR`,
    # then `~/.local/share/soulseek-rs/state` — the daemon's own default, so both ends look in one place.
    soulseek_state_dir: str
    # Traktor's collection belongs to another project, which owns it and its own config. This is how to run
    # that tool here; empty means the machine has no Traktor tool and its half is skipped.
    traktor_command: tuple[str, ...]
    # The collections a run exports to, in order. `rekordbox` and `traktor` ship; a new target is a module
    # beside this file and an entry in `exporting.REGISTRY`, not a branch in the pipeline.
    export_targets: tuple[str, ...]
    soulseek_listen_port: int
    # How much of the acquisition runs at once: `max_downloads` is the daemon's own transfer ceiling and
    # `workers` is how many tracks a pass works on at a time.
    max_downloads: int
    workers: int
    # A floor on the gap between two searches we ask for, in seconds. Ours rather than the daemon's:
    # nothing in the pinned build paces searches, and nothing reports one being ignored. 0 turns it off.
    search_interval: float
    # fail2ban's triple, for peers that list a file and then deliver nothing: `maxretry` failures inside
    # `findtime_days` earn an ignore lasting `bantime_days`, counted across runs. A delivery clears it.
    maxretry: int
    findtime_days: int
    bantime_days: int
    slsk_username_env: str
    slsk_password_env: str
    # The genre vocabulary's mapping table, which lives in music-tagger: its taxonomy maps the names the
    # rest of the world uses onto the families this collection has. Unset means no genres are assigned.
    taxonomy: Path | None
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
    """The listening port, under the field name `Config` gives it."""
    return {
        "soulseek_listen_port": _port_from_env("SLSK_LISTEN_PORT", DEFAULTS.soulseek_listen_port),
    }


DEFAULTS = Config(
    staging=Path("~/Music/music-agent/staging").expanduser(),
    lossless=Path("~/Music/music-agent/lossless").expanduser(),
    lossy=Path("~/Music/music-agent/lossy").expanduser(),
    state_root=Path("~/Music/music-agent/state").expanduser(),
    peers=Path("~/Music/music-agent/state/peers.json").expanduser(),
    target_lufs=-14.0,
    true_peak_db=-1.0,
    tolerance_lu=0.5,
    musicbrainz_contact_env="MUSICBRAINZ_CONTACT",
    acoustid_key_env="ACOUSTID_API_KEY",
    discogs_token_env="DISCOGS_TOKEN",
    # The daemon from `michel/tap/soulseek-rs`: one process that holds the account, speaks JSON-RPC over a
    # Unix socket, and keeps its transfers across passes. The pipeline starts it when the socket answers
    # nothing and leaves it running.
    soulseek_command=("soulseek-rs", "daemon"),
    soulseek_state_dir="",
    slsk_username_env="SLSK_USERNAME",
    slsk_password_env="SLSK_PASSWORD",
    # The tool's own name on PATH. A machine that runs it out of a checkout sets this to its own argv.
    traktor_command=("traktor-nml",),
    # Both collections this machine has. A config that names one target only is a complete configuration.
    export_targets=("rekordbox", "traktor"),
    # The port the server hands to peers, and the one forwarded here. The daemon registers exactly this one;
    # its neighbour is only ever swept in case a client that used both is still alive.
    soulseek_listen_port=20000,
    # Twenty transfers at a time, which is how many tracks a pass works on: the daemon runs them, and the
    # server's own default is three.
    max_downloads=20,
    workers=20,
    # Known-good rate: 34 searches per 220 seconds, from slsk-batchdl, whose README warns that higher values
    # earn 30-minute bans. This is the same sustained rate without its burst. A pass still spends ten seconds
    # collecting each search; 315 tracks at this pace is about 34 minutes of searching.
    search_interval=6.47,
    # Three silent failures inside a fortnight, then a month off. Bots and paid mirrors fail every request,
    # so they reach three in one pass; a peer whose queue was long, or whose one file was a bad rip, does not
    # fail again for a fortnight and is never ignored at all.
    maxretry=3,
    findtime_days=14,
    bantime_days=30,
    # No taxonomy by default: a machine without music-tagger installed assigns no genres and prints one line
    # saying so, rather than inventing a vocabulary of its own.
    taxonomy=None,
    # Both ship with the project: `tools/convert_to_aiff.sh` and the normalizer submodule under `vendor/`.
    convert_tool=HERE / "tools/convert_to_aiff.sh",
    normalize_tool=HERE / "vendor/loudness_normalizer/loudness_normalizer.py",
)


def _command(value) -> tuple[str, ...]:
    """A TOML author writes `command = "soulseek-rs daemon"`; `tuple()` of that is one letter per argv."""
    if isinstance(value, str):
        return tuple(value.split())
    return tuple(value)


def _names(value) -> tuple[str, ...]:
    """A single target written as a bare string stays one name, never one character per name."""
    if isinstance(value, str):
        return (value,)
    return tuple(value)


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
    traktor = data.get("traktor", {})
    export = data.get("export", {})
    genres = data.get("genres", {})
    tools = data.get("tools", {})

    return Config(
        staging=_resolve(paths.get("staging", str(DEFAULTS.staging)), base),
        lossless=_resolve(paths.get("lossless", str(DEFAULTS.lossless)), base),
        lossy=_resolve(paths.get("lossy", str(DEFAULTS.lossy)), base),
        state_root=_resolve(paths.get("state", str(DEFAULTS.state_root)), base),
        peers=_resolve(paths.get("peers", str(DEFAULTS.peers)), base),
        target_lufs=float(loudness.get("target_lufs", DEFAULTS.target_lufs)),
        true_peak_db=float(loudness.get("true_peak_db", DEFAULTS.true_peak_db)),
        tolerance_lu=float(loudness.get("tolerance_lu", DEFAULTS.tolerance_lu)),
        musicbrainz_contact_env=credentials.get("musicbrainz_contact",
                                                DEFAULTS.musicbrainz_contact_env),
        acoustid_key_env=credentials.get("acoustid_key", DEFAULTS.acoustid_key_env),
        discogs_token_env=credentials.get("discogs_token", DEFAULTS.discogs_token_env),
        soulseek_command=_command(soulseek.get("command", DEFAULTS.soulseek_command)),
        soulseek_state_dir=str(soulseek.get("state_dir", DEFAULTS.soulseek_state_dir)),
        traktor_command=_command(traktor.get("command", DEFAULTS.traktor_command)),
        export_targets=_names(export.get("targets", DEFAULTS.export_targets)),
        **_listening_ports(),
        max_downloads=int(soulseek.get("max_downloads", DEFAULTS.max_downloads)),
        workers=int(soulseek.get("workers", DEFAULTS.workers)),
        search_interval=float(soulseek.get("search_interval", DEFAULTS.search_interval)),
        maxretry=int(soulseek.get("maxretry", DEFAULTS.maxretry)),
        findtime_days=int(soulseek.get("findtime_days", DEFAULTS.findtime_days)),
        bantime_days=int(soulseek.get("bantime_days", DEFAULTS.bantime_days)),
        slsk_username_env=credentials.get("soulseek_user", DEFAULTS.slsk_username_env),
        slsk_password_env=credentials.get("soulseek_password", DEFAULTS.slsk_password_env),
        taxonomy=(_resolve(genres["taxonomy"], base) if genres.get("taxonomy") else DEFAULTS.taxonomy),
        convert_tool=_resolve(tools.get("convert", str(DEFAULTS.convert_tool)), base),
        normalize_tool=_resolve(tools.get("normalize", str(DEFAULTS.normalize_tool)), base),
    )
