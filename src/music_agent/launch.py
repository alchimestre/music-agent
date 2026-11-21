"""Entry point for the packaged desktop application.

The freezer compiles this file as a script, where `__package__` is empty and a relative import has
no parent to resolve against, so the window module is imported by its absolute name. A build
without a console exits with nothing on screen, so a failure is written to a file instead.

`--normalize` runs the shipped normalizer and returns before the window module, and with it Qt, is
imported: each normalization spawns this executable again.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from music_agent.resources import expose_payload_on_path

def crash_log() -> Path:
    """Where a failure is recorded, beside the application's own settings."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home()
    return root / "music-agent" / "crash.log"


def _run_normalizer(argv: list[str]) -> int:
    report, script = Path(argv[0]), Path(argv[1])
    spec = importlib.util.spec_from_file_location("loudness_normalizer", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot read the normalizer at {script}")
    module = importlib.util.module_from_spec(spec)
    original_argv = sys.argv
    sys.argv = [str(script), *argv[2:]]
    try:
        # A windowed release has no standard streams.
        with report.open("w", encoding="utf-8", buffering=1) as output:
            with redirect_stdout(output), redirect_stderr(output):
                spec.loader.exec_module(module)
                return module.main() or 0
    finally:
        sys.argv = original_argv


def main() -> int:
    try:
        if sys.argv[1:2] == ["--normalize"]:
            expose_payload_on_path()
            return _run_normalizer(sys.argv[2:])
        from music_agent.desktop import main as run_desktop

        return run_desktop()
    except BaseException:
        log = crash_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
