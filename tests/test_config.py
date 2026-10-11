"""Configuration, tested at the seam the CLI uses: `load()`."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent.config import DEFAULTS, load


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_file_is_not_an_error():
    """The defaults are a working configuration, so the CLI runs before anyone writes a file."""
    assert load(Path("/nonexistent/config.toml")) == DEFAULTS


def test_the_directories_come_from_the_file(tmp_path):
    config = load(write(tmp_path, """
[paths]
staging = "~/Music/Staging"
lossy = "Losy Tracks"
"""))
    assert config.staging == Path("~/Music/Staging").expanduser()
    assert config.lossy == (tmp_path / "Losy Tracks").resolve()


def test_a_relative_path_reads_against_the_config_files_own_directory(tmp_path):
    """So the whole layout moves with the repo rather than with whatever directory you ran from."""
    config = load(write(tmp_path, '[paths]\nstate = ".state/jobs"\n'))
    assert config.state_root == (tmp_path / ".state/jobs").resolve()


def test_untouched_keys_keep_their_defaults(tmp_path):
    config = load(write(tmp_path, "[loudness]\ntarget_lufs = -9.0\n"))
    assert config.target_lufs == -9.0
    assert config.true_peak_db == DEFAULTS.true_peak_db
    assert config.tolerance_lu == DEFAULTS.tolerance_lu


def test_the_file_carries_variable_names_rather_than_secrets(tmp_path):
    config = load(write(tmp_path, '[credentials]\nacoustid_key = "MY_ACOUSTID"\n'))
    assert config.acoustid_key_env == "MY_ACOUSTID"
    assert config.musicbrainz_contact_env == "MUSICBRAINZ_CONTACT"


def test_a_malformed_file_is_an_error(tmp_path):
    """Ignoring it quietly would run the pipeline against the wrong directories."""
    with pytest.raises(tomllib.TOMLDecodeError):
        load(write(tmp_path, "this is not toml = = ="))


def test_the_shipped_config_is_valid_and_agrees_with_the_defaults():
    config = load(config_module.DEFAULT_CONFIG_PATH)
    assert config.staging == DEFAULTS.staging
    assert config.target_lufs == DEFAULTS.target_lufs
    assert config.state_root == config_module.DEFAULT_CONFIG_PATH.parent / ".state/jobs"


def test_the_shipped_config_pins_the_acquisition_server_and_moves_it_off_the_default_ports():
    """Two regressions worth catching here. Floating the pin on a branch would let a rebuild move the
    fields the match rules read (the design notes), and the listening ports must not go back to aioslsk's
    defaults, which the omp MCP's own slsk server already holds on this machine."""
    config = load(config_module.DEFAULT_CONFIG_PATH)

    assert config.soulseek_command[0] == "uvx"
    assert any(part.startswith("git+") and "@" in part for part in config.soulseek_command), \
        "the acquisition server is not pinned to a commit"
    assert (config.soulseek_listen_port, config.soulseek_obfuscated_port) != (60000, 60001)
