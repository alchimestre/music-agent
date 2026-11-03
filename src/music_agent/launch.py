"""Entry point for the packaged desktop application.

The freezer compiles this file as a script, where `__package__` is empty and a relative import has
no parent to resolve against, so the window module is imported by its absolute name. A build
without a console exits with nothing on screen, so a failure is written to a file instead.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path


def crash_log() -> Path:
    """Where a failure is recorded, beside the application's own settings."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home()
    return root / "music-agent" / "crash.log"


def main() -> int:
    try:
        from music_agent.desktop import main as run_desktop

        return run_desktop()
    except BaseException:
        log = crash_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
