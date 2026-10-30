"""The target seam: order, selection, and the one place a run is finished."""

from __future__ import annotations

import contextlib
from dataclasses import replace

import pytest

from music_agent import exporting, lanes, library
from music_agent.config import DEFAULTS


class FakeTarget:
    def __init__(self, name: str, *, reads: bool = False, available: bool = True,
                 failed: int = 0) -> None:
        self.name = name
        self.reads = reads
        self._available = available
        self._failed = failed
        self.calls = 0

    def available(self, config) -> bool:
        return self._available

    def rows(self, config):
        return ([lanes.Held(path="/music/a.aiff", artist="A", title="T")], ["House"])

    def import_run(self, config, job, *, backup_first=True, log=print):
        self.calls += 1
        return exporting.Summary(lines=[f"{self.name} did it"], failed=self._failed)


class FakeJob:
    def __init__(self) -> None:
        self.finished = False
        self.written = 0

    def finish(self) -> None:
        self.finished = True

    def write_status(self, config) -> None:
        self.written += 1


@pytest.fixture
def registry(monkeypatch):
    """Each test owns the names it adds; nothing leaks into another test's registry."""
    def add(*targets):
        for target in targets:
            monkeypatch.setitem(exporting.REGISTRY, target.name, target)
    return add


def test_targets_are_configured_in_order(registry):
    registry(FakeTarget("alpha"), FakeTarget("beta"))
    assert [target.name for target in exporting.configured(
        replace(DEFAULTS, export_targets=("alpha", "beta")))] == ["alpha", "beta"]


def test_only_and_without_narrow_the_same_list(registry):
    registry(FakeTarget("alpha"), FakeTarget("beta"))
    config = replace(DEFAULTS, export_targets=("alpha", "beta"))

    assert [t.name for t in exporting.configured(config, only=["beta"])] == ["beta"]
    assert [t.name for t in exporting.configured(config, without=["alpha"])] == ["beta"]


def test_a_typo_is_refused_not_ignored(registry):
    registry(FakeTarget("alpha"))

    with pytest.raises(exporting.UnknownTarget):
        exporting.configured(replace(DEFAULTS, export_targets=("rekordbx",)))
    with pytest.raises(exporting.UnknownTarget):
        exporting.configured(replace(DEFAULTS, export_targets=("alpha",)), without=["alpah"])


def test_a_run_finishes_only_after_every_target_ran():
    job = FakeJob()
    first, second = FakeTarget("first"), FakeTarget("second")

    exporting.run(DEFAULTS, job, targets=[first, second], log=lambda line: None)

    assert (first.calls, second.calls) == (1, 1)
    assert job.finished


def test_a_failed_target_keeps_the_run_open():
    job = FakeJob()

    results = exporting.run(DEFAULTS, job,
                            targets=[FakeTarget("clean"), FakeTarget("failed", failed=1)],
                            log=lambda line: None)

    assert not job.finished
    assert [result.failed for result in results] == [0, 1]


def test_an_unavailable_target_does_not_finish_alone():
    job = FakeJob()
    absent = FakeTarget("absent", available=False)

    exporting.run(DEFAULTS, job, targets=[absent], log=lambda line: None)

    assert not job.finished


def test_an_import_that_received_nothing_leaves_the_run_open(tmp_path, monkeypatch):
    """Every track still pending meant `rekordbox 0 imported`, a `finished` event, and a run the next pass
    skipped as done — though no collection had received it."""
    from music_agent import importing, state

    @contextlib.contextmanager
    def empty_database(config):
        yield object()

    monkeypatch.setattr(importing, "is_running", lambda: False)
    monkeypatch.setattr(importing.processes, "stop_named", lambda *args, **kwargs: None)
    monkeypatch.setattr(importing, "database", empty_database)

    config = replace(DEFAULTS, state_root=tmp_path / "state")
    job = state.create(config, "test")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred", status=state.PENDING)])

    results = exporting.run(config, job, targets=[exporting.Rekordbox()], log=lambda line: None)

    assert [result.ran for result in results] == [False]
    assert not job.finished()


def test_a_traktor_plan_that_changes_nothing_did_not_run(monkeypatch):
    """The tool prints its plan whether or not it has anything to do, so a plan of zeros is not a target
    that received the run."""
    from music_agent import traktor

    monkeypatch.setattr(traktor, "sync", lambda config, log=print: "0 to repoint · 0 to add")
    assert exporting.Traktor().import_run(DEFAULTS, FakeJob()).ran is False

    monkeypatch.setattr(traktor, "sync", lambda config, log=print: "2 to repoint · 0 to add")
    assert exporting.Traktor().import_run(DEFAULTS, FakeJob()).ran is True


def test_library_rows_come_from_reading_targets_only(registry):
    registry(FakeTarget("reader", reads=True), FakeTarget("silent", reads=False))
    config = replace(DEFAULTS, export_targets=("reader", "silent"))

    rows, genres, read = library._target_rows(config)

    assert [row.artist for row in rows] == ["A"]
    assert genres == ["House"]
    assert read


def test_no_reading_target_reports_no_lane_leftovers(registry, tmp_path):
    registry(FakeTarget("silent", reads=False))
    config = replace(DEFAULTS, export_targets=("silent",), lossless=tmp_path / "lossless",
                     lossy=tmp_path / "lossy")
    config.lossless.mkdir()
    (config.lossless / "A - T.aiff").write_bytes(b"x")

    leftovers = library.unpointed(config)

    rows, _genres, read = library._target_rows(config)
    assert rows == []
    assert not read
    assert leftovers.lane == []
