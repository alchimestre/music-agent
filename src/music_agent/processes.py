"""Stopping the processes that hold something a stage needs.

Two kinds do. A Soulseek client left on the listening ports by an earlier run is logged in on the same
account, so the server hands it every search reply and every search this pass asks about comes back empty. And a
`rekordbox-mcp` left holding `master.db` is a second writer on a file the import has to open on its own.

Neither is this program's process, and both are another harness's children — restarted when that harness
wants them — so stopping one costs nothing. Anything else holding the resource is named and left alone:
this is not the place to kill a stranger's process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

# How long a process has to go quietly before it is killed outright.
GRACE = 3.0


def listeners(port: int) -> list[int]:
    """The pids listening on one TCP port, as `lsof` reports them. Empty when lsof cannot say."""
    try:
        found = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(line) for line in found.stdout.split() if line.strip().isdigit()]


def command_of(pid: int) -> str:
    """One process's command line, or '' when it is already gone."""
    try:
        found = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return found.stdout.strip()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def named(name: str) -> list[int]:
    """Every running process whose command line contains `name`."""
    try:
        found = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(line) for line in found.stdout.split() if line.strip().isdigit()]


def stop(pids: list[int], *, grace: float = GRACE) -> list[int]:
    """Stop these pids: SIGTERM, a moment to go quietly, then SIGKILL. Returns the ones that are still there."""
    wanted = [pid for pid in pids if pid != os.getpid()]
    for pid in wanted:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(pid) for pid in wanted):
        time.sleep(0.1)
    for pid in wanted:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return [pid for pid in wanted if alive(pid)]


def stop_named(names: tuple[str, ...], *, log=print) -> list[int]:
    """Stop every process whose command line contains one of these names, saying which it stopped."""
    for name in names:
        found = named(name)
        if not found:
            continue
        stubborn = stop(found)
        log(f"stopped {name} (pid{'s' if len(found) > 1 else ''} {', '.join(str(pid) for pid in found)})"
            + (f" — {stubborn} would not go" if stubborn else ""))
        return found
    return []
