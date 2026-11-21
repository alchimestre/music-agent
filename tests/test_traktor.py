"""What the traktor target tells the seam: a plan, and whether the tool refused."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from music_agent import exporting, traktor
from music_agent.config import DEFAULTS

CONFIG = replace(DEFAULTS, traktor_command=("traktor-nml", "test"))


def answered(returncode: int, out: str = ""):
    """One `subprocess.run` result, and the argv it was called with, per call."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=out, stderr="")

    return run, calls


def test_a_refused_plan_raises_and_writes_nothing(monkeypatch):
    run, calls = answered(1, "Traktor is running, and it rewrites the collection as it exits")
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml", "test"])
    monkeypatch.setattr(traktor.subprocess, "run", run)

    with pytest.raises(traktor.Failed, match="plan failed"):
        traktor.sync(CONFIG, log=lambda line: None)

    assert [call[-1] for call in calls] == ["plan"]


def test_a_refused_apply_raises(monkeypatch):
    replies = iter([SimpleNamespace(returncode=0, stdout="2 to repoint · 0 to add", stderr=""),
                    SimpleNamespace(returncode=1, stdout="", stderr="collection.nml is not the one read")])
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml", "test"])
    monkeypatch.setattr(traktor.subprocess, "run", lambda argv, **kwargs: next(replies))

    with pytest.raises(traktor.Failed, match="apply failed"):
        traktor.sync(CONFIG, log=lambda line: None)


def test_a_plan_with_nothing_to_do_is_not_applied(monkeypatch):
    run, calls = answered(0, "0 to repoint · 0 to add")
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml", "test"])
    monkeypatch.setattr(traktor.subprocess, "run", run)

    plan = traktor.sync(CONFIG, log=lambda line: None)

    assert plan == "0 to repoint · 0 to add"
    assert [call[-1] for call in calls] == ["plan"]


def test_the_target_turns_a_refusal_into_a_failed_summary(monkeypatch):
    def refuse(config, *, log):
        raise traktor.Failed("traktor apply failed · collection.nml is not the one read")

    monkeypatch.setattr(traktor, "sync", refuse)

    result = exporting.Traktor().import_run(CONFIG, job=None, log=lambda line: None)

    assert (result.failed, result.ran) == (1, False)
    assert "apply failed" in result.report()


def test_verify_reads_per_file_membership_from_owner(tmp_path, monkeypatch):
    present = str(tmp_path / "present.aiff")
    absent = str(tmp_path / "absent.aiff")
    collection = str(tmp_path / "collection.nml")
    payload = {"collection": collection, "paths": {present: True, absent: False}}
    run, _ = answered(0, json.dumps(payload))
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml"])
    monkeypatch.setattr(traktor.subprocess, "run", run)

    assert traktor.verify(CONFIG, [present, absent]) == payload


@pytest.mark.parametrize("paths", [{}, {"unexpected": True}, {"requested": "true"}])
def test_verify_rejects_incomplete_or_nonboolean_owner_evidence(tmp_path, monkeypatch, paths):
    requested = str(tmp_path / "requested.aiff")
    evidence = {requested if key == "requested" else key: value for key, value in paths.items()}
    run, _ = answered(0, json.dumps({"collection": str(tmp_path / "collection.nml"), "paths": evidence}))
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml"])
    monkeypatch.setattr(traktor.subprocess, "run", run)

    with pytest.raises(traktor.Failed):
        traktor.verify(CONFIG, [requested])


def test_a_staged_backup_keeps_the_name_and_a_finder_marker(tmp_path):
    old = tmp_path / "held.mp3"
    old.write_bytes(b"held")
    new = tmp_path / "upgrade.aiff"
    new.write_bytes(b"upgrade")

    with traktor.stage_replacements({old: new}):
        (directory,) = tmp_path.glob(".music-agent-replaces-*")
        assert (directory / "held.mp3").read_bytes() == b"held"
        assert str(old) in (directory / traktor.MARKER).read_text(encoding="utf-8")
        assert not old.exists()

    assert not old.exists()
    assert not list(tmp_path.glob(".music-agent-replaces-*"))


def test_a_failed_apply_restores_the_staged_predecessor(tmp_path):
    old = tmp_path / "held.mp3"
    old.write_bytes(b"held")

    with pytest.raises(RuntimeError):
        with traktor.stage_replacements({old: tmp_path / "upgrade.aiff"}):
            raise RuntimeError("apply failed")

    assert old.read_bytes() == b"held"
    assert not list(tmp_path.glob(".music-agent-replaces-*"))

