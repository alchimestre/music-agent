"""The CLI contract: what `status` prints, which is what the omp skill reads."""

from __future__ import annotations

from pathlib import Path

from music_agent import config as config_module
from music_agent import state
from music_agent.cli import main
from music_agent.rules import Candidate

SETTLED = state.Record(idx=1, artist="Kiasmos", title="Blurred", duration_s=449,
                       candidates=[Candidate(user="vinyl_only", path=r"M\Kiasmos - Blurred.flac",
                                             duration=448)])
WAITING = state.Record(idx=2, artist="Ntogn", title="Mines", duration_s=528,
                       candidates=[Candidate(user="mole", path=r"M\Ntogn - Mines.aiff",
                                             duration=None)])
EMPTY = state.Record(idx=3, artist="Sven Wunder", title="Lotus", duration_s=260,
                     candidates=[Candidate(user="crateguy", path=r"M\Sven Wunder - Lotus.aiff",
                                           duration=290)])


def make_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\n', encoding="utf-8")
    return path


def seed(config_path: Path, records: list[state.Record]) -> state.Job:
    job = state.create(config_module.load(config_path), "run")
    job.write_records(records)
    return job


def test_status_before_any_run_says_so(tmp_path, capsys):
    assert main(["--config", str(make_config(tmp_path)), "status"]) == 0
    assert "no runs yet" in capsys.readouterr().out


def test_status_reports_the_counts(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING, EMPTY])
    assert main(["--config", str(config_path), "status"]) == 0

    out = capsys.readouterr().out
    assert "3 tracks" in out
    assert "1 in review" in out
    assert "1 settled" in out
    assert "1 with nothing usable" in out


def test_the_queue_lists_what_needs_a_decision(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING, EMPTY])
    main(["--config", str(config_path), "status", "--queue"])

    out = capsys.readouterr().out
    assert "Ntogn - Mines" in out
    assert "the peer reports no length" in out
    assert "Kiasmos" not in out                     # settled, so not the human's problem
    assert "another search is needed" in out        # and the unusable one is named


def test_the_queue_says_so_when_it_is_empty(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED])
    main(["--config", str(config_path), "status", "--queue"])

    out = capsys.readouterr().out
    assert "the queue is empty" in out
    assert "another search" not in out


def test_the_config_may_be_given_before_or_after_the_command(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED])

    assert main(["--config", str(config_path), "status"]) == 0
    assert "1 track ·" in capsys.readouterr().out
    assert main(["status", "--config", str(config_path)]) == 0
    assert "1 track ·" in capsys.readouterr().out


def test_an_unknown_run_is_an_error(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED])
    assert main(["--config", str(config_path), "status", "--job", "nope"]) == 1
    assert "no run called" in capsys.readouterr().err


def test_a_named_run_can_be_selected(tmp_path, capsys):
    config_path = make_config(tmp_path)
    job = seed(config_path, [SETTLED])
    assert main(["--config", str(config_path), "status", "--job", job.id]) == 0
    assert job.id in capsys.readouterr().out
