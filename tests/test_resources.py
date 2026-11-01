from dataclasses import replace
from pathlib import Path

from music_agent import config, resources


def _default_config():
    return replace(
        config.DEFAULTS,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        fpcalc="fpcalc",
        soulseek_command=("soulseek-rs", "daemon"),
        normalize_tool=Path("loudness_normalizer.py"),
    )


def test_bundle_config_resolves_default_tools_and_missing_normalizer(monkeypatch, tmp_path):
    app_dir = tmp_path / "music-agent-desktop.dist"
    bin_dir = app_dir / "resources" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("soulseek-rs", "ffmpeg", "ffprobe", "fpcalc"):
        (bin_dir / f"{name}.exe").touch()
    monkeypatch.setattr(resources, "_application_directory", lambda: app_dir)
    monkeypatch.setattr(resources.sys, "platform", "win32")

    packaged = resources.bundle_config(_default_config())

    assert packaged.soulseek_command == (str(bin_dir / "soulseek-rs.exe"), "daemon")
    assert packaged.ffmpeg == str(bin_dir / "ffmpeg.exe")
    assert packaged.ffprobe == str(bin_dir / "ffprobe.exe")
    assert packaged.fpcalc == str(bin_dir / "fpcalc.exe")
    assert packaged.normalize_tool == bin_dir / "loudness_normalizer.py"
    assert not packaged.normalize_tool.exists()


def test_bundle_config_preserves_custom_tool_commands(monkeypatch, tmp_path):
    app_dir = tmp_path / "music-agent-desktop.dist"
    monkeypatch.setattr(resources, "_application_directory", lambda: app_dir)
    monkeypatch.setattr(resources.sys, "platform", "win32")
    config = replace(
        _default_config(),
        ffmpeg="D:/tools/custom-ffmpeg.exe",
        ffprobe="D:/tools/custom-ffprobe.exe",
        fpcalc="D:/tools/custom-fpcalc.exe",
        soulseek_command=("D:/tools/custom-soulseek.exe", "daemon"),
    )

    packaged = resources.bundle_config(config)

    assert packaged.ffmpeg == config.ffmpeg
    assert packaged.ffprobe == config.ffprobe
    assert packaged.fpcalc == config.fpcalc
    assert packaged.soulseek_command == config.soulseek_command


def test_bundled_executable_returns_only_existing_resource(monkeypatch, tmp_path):
    app_dir = tmp_path / "music-agent-desktop.dist"
    bin_dir = app_dir / "resources" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg.exe"
    ffmpeg.touch()
    monkeypatch.setattr(resources, "_application_directory", lambda: app_dir)
    monkeypatch.setattr(resources.sys, "platform", "win32")

    assert resources.bundled_executable("ffmpeg") == ffmpeg
    assert resources.bundled_executable("fpcalc") is None
