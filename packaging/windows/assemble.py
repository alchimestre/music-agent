"""Copy the staged payload beside the built application, prove it is complete, and archive the result."""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "payload"
BUNDLED = ("ffmpeg.exe", "ffprobe.exe", "fpcalc.exe", "soulseek-rs.exe", "loudness_normalizer.py")
LICENSED = ("ffmpeg", "fpcalc", "soulseek-rs", "loudness_normalizer")


def distribution(exec_directory: Path) -> Path:
    candidates = [path for path in exec_directory.glob("*.dist") if path.is_dir()]
    if len(candidates) != 1:
        raise SystemExit(f"{exec_directory}: expected one *.dist directory, found {len(candidates)}")
    return candidates[0]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(resources: Path) -> None:
    missing = [name for name in BUNDLED if not (resources / "bin" / name).is_file()]
    unlicensed = [name for name in LICENSED if not (resources / "licenses" / f"{name}.txt").is_file()]
    if missing or unlicensed:
        raise SystemExit(f"incomplete payload: missing {missing}, licenses {unlicensed}")
    if not (resources / "release-inputs.json").is_file():
        raise SystemExit("incomplete payload: no release-inputs.json")


def archive(dist: Path, suffix: str) -> Path:
    destination = dist.parent / f"music-agent-windows{suffix}.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(dist.parent))
    digest = sha256_of(destination)
    destination.with_suffix(".zip.sha256").write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    return destination


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        raise SystemExit("usage: assemble.py <exec-directory> [--signed]")
    if not PAYLOAD.is_dir():
        raise SystemExit(f"{PAYLOAD}: run stage.py first")
    dist = distribution(Path(argv[0]).resolve())
    resources = dist / "resources"
    shutil.copytree(PAYLOAD, resources, dirs_exist_ok=True)
    verify(resources)
    print(archive(dist, "" if "--signed" in argv else "-unsigned"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
