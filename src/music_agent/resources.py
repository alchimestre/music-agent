"""Where a frozen desktop build finds the executables it ships.

The Windows build stages its external tools beside the application executable, in `resources/bin`, and
the release workflow verifies that payload before it builds. A source checkout has no such directory, so
nothing here changes a command-line run: the configured names and files stay exactly as `config.toml`
gave them.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from music_agent.config import Config

# Beside the executable rather than inside the Qt payload: the installer copies one directory verbatim.
RESOURCE_DIRECTORY = Path("resources") / "bin"
# The normalizer is not a package this repository may redistribute, so a build that lacks a licensed copy
# must point at the missing file and let the prerequisite check refuse to process, never fall back to a
# checkout's copy.
NORMALIZER_NAME = "loudness_normalizer.py"


def _application_directory() -> Path:
    return Path(sys.executable).resolve().parent


def _resource_directory() -> Path:
    return _application_directory() / RESOURCE_DIRECTORY


def _executable_suffix() -> str:
    return ".exe" if sys.platform == "win32" else ""


def bundled_executable(name: str) -> Path | None:
    """The staged executable `name`, when this build ships one."""
    path = _resource_directory() / f"{name}{_executable_suffix()}"
    return path if path.is_file() else None


def _bundled(configured: str, name: str) -> str:
    """Keep an explicitly configured tool; a default tool name follows the staged build."""
    if configured != name:
        return configured
    executable = bundled_executable(name)
    return str(executable) if executable else configured


def _bundled_command(configured: tuple[str, ...], name: str) -> tuple[str, ...]:
    """The same rule for a command, whose first word is the tool and whose rest is its own arguments."""
    if not configured:
        return configured
    return (_bundled(configured[0], name), *configured[1:])


def bundle_config(config: Config) -> Config:
    """Point a packaged build at its own payload, and leave a source checkout untouched."""
    if not _resource_directory().is_dir():
        return config
    return replace(
        config,
        soulseek_command=_bundled_command(config.soulseek_command, "soulseek-rs"),
        ffmpeg=_bundled(config.ffmpeg, "ffmpeg"),
        ffprobe=_bundled(config.ffprobe, "ffprobe"),
        fpcalc=_bundled(config.fpcalc, "fpcalc"),
        normalize_tool=_normalizer(config.normalize_tool),
    )


def _normalizer(configured: Path) -> Path:
    """A packaged build runs its own staged copy: a path outside the payload is never executed, and a
    missing payload file stays missing so the prerequisite check refuses to process."""
    return _resource_directory() / (configured.name or NORMALIZER_NAME)
