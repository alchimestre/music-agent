"""Real, if tiny, audio files, so a test that tags something has something real to tag.

`aiff` is built byte by byte and needs nothing at all. `mp4` goes through ffmpeg, which the pipeline
itself requires, and skips when it is not to hand rather than failing for a reason that is not the code's.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest


def aiff(path: Path, frames: int = 800) -> Path:
    """A real AIFF: FORM/AIFF with a COMM chunk and some silence in SSND."""
    channels, bits = 2, 16
    data = b"\x00\x00" * frames * channels
    comm = struct.pack(">hLh", channels, frames, bits) + b"\x40\x0e\xac\x44" + b"\x00" * 6
    chunks = b"COMM" + struct.pack(">L", len(comm)) + comm
    body = struct.pack(">LL", 0, 0) + data
    chunks += b"SSND" + struct.pack(">L", len(body)) + body
    path.write_bytes(b"FORM" + struct.pack(">L", 4 + len(chunks)) + b"AIFF" + chunks)
    return path


def mp4(path: Path, seconds: float = 0.2) -> Path:
    """A real m4a. Fabricating one by hand is not worth it, and ffmpeg is already a dependency."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not here to make an m4a with")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}",
         "-c:a", "aac", str(path)],
        capture_output=True, check=True,
    )
    return path


def opus(path: Path, seconds: float = 0.2) -> Path:
    """A real Ogg Opus file. What matters here is the container family: its comments reject a key they do
    not recognise by raising, where ID3 answers "not here"."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not here to make an opus file with")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}",
         "-c:a", "libopus", "-f", "ogg", str(path)],
        capture_output=True, check=True,
    )
    return path
