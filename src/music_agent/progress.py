"""Where a pass is, while it runs.

A terminal gets one line, rewritten in place, so a long pass shows its position and what is in flight without
scrolling the per-track log away. Anything that is not a terminal — a harness reading the pass back, a file —
gets one line per track as it settles instead, because a carriage return is not an update in a captured stream.
"""

from __future__ import annotations

import shutil
import sys
import threading
from collections import Counter


class Bar:
    """The pass's position, and the lines it prints, under one lock so they cannot interleave."""

    def __init__(self, total: int, *, log=print, stream=None, enabled: bool = True) -> None:
        self.total = total
        self.enabled = enabled
        self._log = log
        self._stream = stream if stream is not None else sys.stdout
        self._rewrites = bool(getattr(self._stream, "isatty", lambda: False)())
        self._lock = threading.Lock()
        self._done = 0
        self._inflight: dict[int, str] = {}
        self._counts: Counter[str] = Counter()
        self._drawn = ""

    def say(self, line: str) -> None:
        """A line that stays: the per-track log, kept above the bar."""
        with self._lock:
            self._erase()
            self._log(line)
            self._draw()

    def begin(self, idx: int, label: str) -> None:
        with self._lock:
            self._inflight[idx] = label
            self._draw()

    def settle(self, idx: int, label: str, outcome: str) -> None:
        with self._lock:
            self._inflight.pop(idx, None)
            self._done += 1
            self._counts[outcome.replace("_", " ")] += 1
            if self.enabled and not self._rewrites:
                self._stream.write(f"  {self._done:>4}/{self.total}  {outcome.replace('_', ' '):<18}{label}\n")
                self._stream.flush()
            self._draw()

    def close(self) -> None:
        with self._lock:
            self._erase()
            self._stream.flush()

    def _erase(self) -> None:
        if self._drawn:
            self._stream.write("\r" + " " * len(self._drawn) + "\r")
            self._drawn = ""

    def _draw(self) -> None:
        if not (self.enabled and self._rewrites):
            return
        width = 20
        filled = int(width * self._done / self.total) if self.total else width
        counts = "  ".join(f"{count} {name}" for name, count in self._counts.most_common())
        flight = " · ".join(list(self._inflight.values())[:2])
        line = f"  {self._done}/{self.total} [{'█' * filled}{'─' * (width - filled)}]  {counts}"
        if flight:
            line += f"  —  {flight}"
        line = line[: shutil.get_terminal_size(fallback=(100, 24)).columns - 1]
        self._stream.write("\r" + line + " " * max(0, len(self._drawn) - len(line)))
        self._stream.flush()
        self._drawn = line
