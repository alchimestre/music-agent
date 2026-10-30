"""The kill policy: a pid's own executable decides, never a command line that happens to mention a name."""

from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

from music_agent import processes

US = 999


class Machine:
    """A process table with nothing behind it: `ps` and `lsof` answer from dictionaries.

    A process is a pid and the command line it was started with, which is all `ps -o command=` reports.
    """

    def __init__(self, argv: dict[int, str], listening: dict[int, list[int]] | None = None) -> None:
        self.argv = dict(argv)
        self.alive = set(argv)
        self.listening = listening or {}
        self.signalled: list[tuple[int, int]] = []
        self.will_not_go: set[int] = set()

    def run(self, args, **_kwargs):
        what = args[0]
        if what == "ps":
            listed = [int(args[-1])] if args[-2] == "-p" else sorted(self.alive)
            rows = "".join(f"{pid} {self.argv[pid]}\n" for pid in listed if pid in self.alive)
            return SimpleNamespace(stdout=rows)
        if what == "lsof":
            port = int(args[2].split(":")[-1])
            return SimpleNamespace(stdout="".join(f"{pid}\n" for pid in self.listening.get(port, [])))
        raise AssertionError(f"unexpected command {args}")

    def signal(self, pid: int, number: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if number:
            self.signalled.append((pid, number))
        if pid in self.will_not_go:
            return
        if number in (signal.SIGTERM, signal.SIGKILL):
            self.alive.discard(pid)


class FastClock:
    """In place of `time` inside `processes`, so a grace period of three seconds costs nothing."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 0.1
        return self.now

    def sleep(self, _seconds: float) -> None:
        pass


@pytest.fixture
def machine(monkeypatch):
    """Install a fake process table: `machine({pid: command line}, {port: [pid, ...]})` returns it."""
    def install(argv, listening=None):
        box = Machine(argv, listening)
        monkeypatch.setattr(processes, "subprocess",
                            SimpleNamespace(run=box.run, TimeoutExpired=processes.subprocess.TimeoutExpired))
        monkeypatch.setattr(processes, "os", SimpleNamespace(kill=box.signal, getpid=lambda: US))
        monkeypatch.setattr(processes, "time", FastClock())
        return box

    return install


def test_named_finds_the_process_whose_own_executable_is_the_client(machine):
    machine({501: "/opt/homebrew/bin/sldl --config a.toml"})

    assert processes.named("sldl") == [501]


def test_named_follows_an_interpreter_to_the_script_it_runs(machine):
    """How a `rekordbox-mcp` is launched on this machine: `python3.12 .../bin/rekordbox-mcp`. Its `ps` name is
    the interpreter, and the `uv run` above it is the launcher, not the writer."""
    machine({37219: "/opt/homebrew/bin/uv run rekordbox-mcp",
             37220: "/Users/x/.venv/bin/python /Users/x/.venv/bin/rekordbox-mcp --stdio"})

    assert processes.named("rekordbox-mcp") == [37220]


def test_named_leaves_a_decoy_that_only_mentions_the_name(machine):
    """The measured case: a command line carrying `sldl.log.sample` came back as an `sldl`."""
    machine({500: "python3 -c print('/tmp/sldl.log.sample')",
             502: "tail -f /tmp/sldl.log",
             503: "/Applications/SoulseekQt.app/Contents/MacOS/SoulseekQt"})

    assert processes.named("sldl") == []
    assert processes.named("soulseekqt") == [503]


def test_a_stranger_under_another_name_is_left_alone(machine):
    box = machine({504: "/usr/bin/editor ~/notes/nicotine/setup.md"})

    assert processes.named("nicotine") == []
    assert box.signalled == []


def test_named_leaves_the_current_process_out(machine):
    machine({US: "sldl --we started it"})

    assert processes.named("sldl") == []


def test_stop_never_signals_or_returns_the_current_process(machine):
    box = machine({US: "sldl --we started it", 501: "sldl"})

    assert processes.stop([US, 501]) == []
    assert [pid for pid, _ in box.signalled] == [501]


def test_stop_names_a_process_that_will_not_go(machine):
    box = machine({501: "sldl"})
    box.will_not_go = {501}

    assert processes.stop([501], grace=0) == [501]
    assert [number for _, number in box.signalled] == [signal.SIGTERM, signal.SIGKILL]


def test_stop_named_stops_the_client_and_not_the_log_reader(machine):
    box = machine({500: "tail -f /tmp/sldl.log", 501: "sldl"})
    said = []

    assert processes.stop_named(("sldl",), log=said.append) == [501]
    assert 500 in box.alive
    assert said == ["stopped sldl (pid 501)"]
