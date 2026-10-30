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
from pathlib import Path

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


def commands(selector: list[str]) -> dict[int, str]:
    """`ps` over some processes: each pid and its command line."""
    try:
        found = subprocess.run(["ps", "-o", "pid=,command=", *selector],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for line in found.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and command.strip():
            table[int(pid)] = command.strip()
    return table


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spelling(name: str) -> str:
    """What a program's name is once case and separators are dropped: `SoulseekQt` and `soulseek-qt` agree."""
    return "".join(character for character in name.casefold() if character.isalnum())


# Executables whose first argument is the program they are running. A client started through one of these is
# that client, and its `ps` name is the interpreter.
LAUNCHERS = ("python", "node")


def program(command: str) -> str:
    """The basename of what one command line runs.

    The executable decides, never a name further along the line: `tail -f sldl.log` runs `tail`, and an editor
    open on `notes/nicotine/setup.md` runs the editor. An interpreter is the exception, because the script it
    was handed is the program.
    """
    words = command.split()
    if not words:
        return ""
    executable = Path(words[0]).name
    launcher = executable.casefold()
    if len(words) > 1 and any(launcher.startswith(name) for name in LAUNCHERS):
        script = words[1]
        if not script.startswith("-"):
            return Path(script).stem
    return executable


def runs(pid: int, name: str) -> bool:
    """Whether this pid *is* that program."""
    command = commands(["-p", str(pid)]).get(pid, "")
    return bool(command) and spelling(program(command)) == spelling(name)


def named(name: str) -> list[int]:
    """Every running process whose own program is `name`, this process excluded."""
    wanted = spelling(name)
    ours = os.getpid()
    return [pid for pid, command in commands(["-A"]).items()
            if pid != ours and spelling(program(command)) == wanted]


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
    """Stop every process whose own program is one of these names, saying which it stopped."""
    for name in names:
        found = named(name)
        if not found:
            continue
        stubborn = stop(found)
        log(f"stopped {name} (pid{'s' if len(found) > 1 else ''} {', '.join(str(pid) for pid in found)})"
            + (f" — {stubborn} would not go" if stubborn else ""))
        return found
    return []
