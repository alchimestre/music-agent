"""Stage the executables a standalone Windows build ships, and record where each came from.

Run from the repository root inside the locked environment: every download is checked against
`inputs.json`, so a replaced or rewritten archive stops the build instead of shipping. The
normalization runtime is not in that manifest: it is the `vendor/loudness_normalizer` submodule, MIT
licensed, staged from the checkout.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "payload"
CACHE = HERE / "build" / "downloads"
REPOSITORY = HERE.parents[1]
NORMALIZER_INPUT = REPOSITORY / "vendor" / "loudness_normalizer"
NORMALIZER_NAME = "loudness_normalizer.py"
NORMALIZER_PACKAGE = "loudness_normalizer"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, destination: Path) -> Path:
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return destination


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as response:
        return response.read().decode("utf-8")


def write_license(path: Path, heading: str, source: str, license_name: str, url: str) -> None:
    text = f"{heading}\nSource: {source}\nLicense: {license_name}\nLicense text: {url}\n\n{fetch_text(url)}"
    path.write_text(text, encoding="utf-8")


def verified_archive(entry: dict) -> Path:
    archive = fetch(entry["archive"], CACHE / Path(entry["archive"]).name)
    actual = sha256_of(archive)
    if actual != entry["sha256"]:
        raise SystemExit(f"{archive.name}: sha256 {actual} is not the pinned {entry['sha256']}")
    return archive


def extract(archive: Path, names: list[str], destination: Path) -> None:
    with zipfile.ZipFile(archive) as contents:
        for name in names:
            member = next((item for item in contents.namelist() if Path(item).name == name), None)
            if member is None:
                raise SystemExit(f"{archive.name}: no {name} in the archive")
            with contents.open(member) as source, (destination / name).open("wb") as target:
                shutil.copyfileobj(source, target)


def stage_normalizer(binaries: Path, licenses: Path) -> dict:
    source = NORMALIZER_INPUT / NORMALIZER_NAME
    if not source.is_file():
        raise SystemExit(
            f"Refusing to build: no normalizer at {source}. It is the vendor/loudness_normalizer "
            "submodule; check the build out with submodules."
        )
    text = NORMALIZER_INPUT / "LICENSE"
    if not text.is_file():
        raise SystemExit(f"Refusing to build: {NORMALIZER_INPUT} holds no LICENSE for {NORMALIZER_NAME}.")
    shutil.copy2(source, binaries / NORMALIZER_NAME)
    shutil.copy2(text, licenses / f"{NORMALIZER_PACKAGE}.txt")
    return {
        "name": NORMALIZER_PACKAGE,
        "sha256": sha256_of(source),
        "source": "https://github.com/alchimestre/loudness_normalizer.git",
        "license": "MIT",
    }


def python_packages() -> list[dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name:
            rows[name.lower()] = {
                "name": name,
                "version": distribution.version,
                "license": distribution.metadata["License-Expression"] or distribution.metadata["License"] or "",
            }
    return [rows[key] for key in sorted(rows)]


def binary_records(manifest: dict, normalizer: dict) -> list[dict]:
    records = [
        {
            "name": entry["name"],
            "version": entry["version"],
            "sha256": entry["sha256"],
            "source": entry["archive"],
            "license": entry["license"],
        }
        for entry in manifest["binaries"]
    ]
    records.append(normalizer)
    return records


def record(manifest: dict, normalizer: dict) -> None:
    document = {
        "platform": manifest["platform"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "binaries": binary_records(manifest, normalizer),
        "python_packages": python_packages(),
    }
    text = json.dumps(document, indent=2, sort_keys=True)
    (PAYLOAD / "release-inputs.json").write_text(text + "\n", encoding="utf-8")


def main() -> int:
    manifest = json.loads((HERE / "inputs.json").read_text(encoding="utf-8"))
    binaries = PAYLOAD / "bin"
    licenses = PAYLOAD / "licenses"
    for directory in (binaries, licenses):
        directory.mkdir(parents=True, exist_ok=True)
    for entry in manifest["binaries"]:
        extract(verified_archive(entry), entry["files"], binaries)
        heading = f"{entry['name']} {entry['version']}\nsha256: {entry['sha256']}"
        write_license(licenses / f"{entry['name']}.txt", heading, entry["archive"], entry["license"],
                      entry["license_url"])
    normalizer = stage_normalizer(binaries, licenses)
    record(manifest, normalizer)
    for path in sorted(binaries.iterdir()):
        print(f"{path.name}\t{sha256_of(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
