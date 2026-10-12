"""The CLI contract: what `status` prints, which is what the omp skill reads."""

from __future__ import annotations

from pathlib import Path

from music_agent import config as config_module
from music_agent import library, state
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


def test_a_name_on_enough_tracks_is_put_to_vincent(tmp_path, capsys):
    """The proposal is the to accept or decline, so it has to say what accepting looks like."""
    config_path = make_config(tmp_path)
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                    status=state.TAGGED, genre="")])
    for index in range(5):
        job.append_event(state.UNGENERED_EVENT, idx=index, names=["Goa Trance"])

    assert main(["--config", str(config_path), "status", "--queue"]) == 0

    out = capsys.readouterr().out
    assert "waiting on a genre" in out
    assert "Goa Trance" in out
    assert "5 tracks" in out
    assert '"goa trance" = "Goa Trance"' in out, "the line to paste, not just the name"


def test_a_name_on_too_few_tracks_is_not_put_to_him(tmp_path, capsys):
    config_path = make_config(tmp_path)
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                    status=state.TAGGED, genre="")])
    for index in range(2):
        job.append_event(state.UNGENERED_EVENT, idx=index, names=["Goa Trance"])

    main(["--config", str(config_path), "status", "--queue"])

    assert "to become a genre" not in capsys.readouterr().out


def test_the_genres_to_choose_between_are_printed_with_the_tracks_that_need_one(tmp_path, capsys,
                                                                               monkeypatch):
    """The genre is the one judgement left to a model, and it has to be made among the collection's own
    genres — so they are printed beside the tracks waiting for one."""
    config_path = make_config(tmp_path)
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                    status=state.TAGGED, genre="")])
    shelf = library.Library([], ["Techno", "Jazz"])
    monkeypatch.setattr(library, "open", lambda config: shelf)

    main(["--config", str(config_path), "status", "--queue"])

    out = capsys.readouterr().out
    assert "waiting on a genre" in out
    assert "Jazz" in out and "Techno" in out, "the vocabulary the choice is made from"


def test_the_collection_is_not_opened_when_no_track_needs_a_genre(tmp_path, monkeypatch):
    """Status is a display command; the database is asked only when there is a judgement to make."""
    config_path = make_config(tmp_path)
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                    status=state.TAGGED, genre="Techno")])

    def never(config):
        raise AssertionError("the collection was read for nothing")

    monkeypatch.setattr(library, "open", never)
    assert main(["--config", str(config_path), "status", "--queue"]) == 0
