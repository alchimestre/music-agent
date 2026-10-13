"""The CLI contract: what `status` prints, which is what the omp skill reads."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from audio_files import aiff

from music_agent import cli as cli_module
from music_agent import config as config_module
from music_agent import library, spotify, state
from music_agent.cli import main
from music_agent.mcp import StdioServer
from music_agent.rules import Candidate

FAKE = str(Path(__file__).parent / "fake_server.py")

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
    path.write_text('[paths]\nstate = "state"\n'
                    'lossless = "lossless"\nlossy = "lossy"\n', encoding="utf-8")
    return path


def seed(config_path: Path, records: list[state.Record]) -> state.Job:
    job = state.create(config_module.load(config_path), "run")
    job.write_records(records)
    return job


def test_status_before_any_run_says_so(tmp_path, capsys):
    assert main(["--config", str(make_config(tmp_path)), "status"]) == 0
    assert "no runs yet" in capsys.readouterr().out


def test_a_finished_run_is_still_the_one_a_command_works_on(tmp_path, capsys):
    """The import finishes a run and nothing else, so what it leaves open has to stay reachable without
    naming the run. On 2026-10-12 `music-agent review` said there was nothing to review while a queue of
    26 tracks sat in the only run there was."""
    config_path = make_config(tmp_path)
    job = seed(config_path, [SETTLED, WAITING])
    job.finish()

    assert main(["--config", str(config_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "finished: yes" in out
    assert "1 in review" in out

    assert main(["--config", str(config_path), "review"]) == 0
    assert "Ntogn - Mines" in capsys.readouterr().out, "the queue is still there to work"


def test_a_run_still_in_flight_wins_over_a_newer_finished_one(tmp_path, capsys):
    """Resume means the run in flight, not the newest run there is."""
    config_path = make_config(tmp_path)
    in_flight = seed(config_path, [SETTLED, WAITING])
    finished = state.create(config_module.load(config_path), "later")
    finished.write_records([SETTLED, WAITING])
    finished.finish()

    assert main(["--config", str(config_path), "review", "--skip", "2"]) == 0

    assert in_flight.records()[1].decision == state.SKIPPED
    assert finished.records()[1].decision == state.UNDECIDED


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
    assert "1. aiff" in out, "a candidate line starts with the number review --accept takes"
    assert "Ntogn - Mines.aiff" in out, "and names the file that number would fetch"
    assert "every one was refused" in out           # and the unusable one is named, and why


def test_the_counts_say_why_nothing_was_usable(tmp_path, capsys):
    """Ticket 21: a quiet network written down as a fact about the tracks was the whole problem."""
    config_path = make_config(tmp_path)
    job = seed(config_path, [SETTLED, WAITING, EMPTY])
    job.write_records([SETTLED, WAITING, EMPTY])

    assert main(["--config", str(config_path), "status"]) == 0

    out = capsys.readouterr().out
    assert "1 with nothing usable" in out
    assert "of those: 0 not searched" in out
    assert "0 the network offered nothing" in out
    assert "1 every offer refused" in out


def test_a_track_no_pass_reached_is_not_called_unusable(tmp_path, capsys):
    """A search that answered with nothing and one that never happened are not the same fact, and the
    log is where the difference is kept."""
    config_path = make_config(tmp_path)
    job = seed(config_path, [state.Record(idx=1, artist="Sven Wunder", title="Lotus")])
    job.append_event("created", idx=1)

    main(["--config", str(config_path), "status", "--queue"])

    out = capsys.readouterr().out
    assert "1 not searched" in out

    job.append_event("searched", idx=1, added=0, offered=0)
    main(["--config", str(config_path), "status", "--queue"])
    out = capsys.readouterr().out
    assert "1 the network offered nothing" in out
    assert "not searched" in out and "0 not searched" in out


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


# ── the review command (the design notes) ───────────────────────────────────────────


def test_review_skips_a_track_by_hand(tmp_path, capsys):
    """The queue is durable state and the flags are one client of it: nothing blocks on a keystroke."""
    config_path = make_config(tmp_path)
    job = seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review", "--skip", "2"]) == 0

    assert "skipped" in capsys.readouterr().out
    assert job.records()[1].decision == state.SKIPPED
    assert job.events()[-1]["event"] == "skipped"


def test_review_calls_a_track_not_found(tmp_path, capsys):
    config_path = make_config(tmp_path)
    job = seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review", "--not-found", "2"]) == 0

    record = job.records()[1]
    assert record.decision == state.DECLINED
    assert record.status == state.NOT_FOUND


def test_a_track_settled_by_hand_leaves_the_queue(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING])
    main(["--config", str(config_path), "review", "--skip", "2"])
    capsys.readouterr()

    assert main(["--config", str(config_path), "status", "--queue"]) == 0

    out = capsys.readouterr().out
    assert "the queue is empty" in out
    assert "1 settled by hand" in out
    assert "skipped" in out


def test_review_takes_one_decision_at_a_time(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review", "--skip", "2", "--not-found", "2"]) == 1
    assert "one decision at a time" in capsys.readouterr().err


def test_review_of_a_track_that_is_not_in_the_run_is_an_error(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review", "--skip", "99"]) == 1
    assert "not in this run" in capsys.readouterr().err


def test_review_with_no_terminal_prints_the_queue(tmp_path, capsys):
    """The skill reads the queue rather than drawing it: the track, why it is there, and what a number takes."""
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review"]) == 0

    out = capsys.readouterr().out
    assert "Ntogn - Mines" in out
    assert "the peer reports no length" in out
    assert "Ntogn - Mines.aiff" in out
    assert "settle one by hand" in out


def test_accepting_without_a_candidate_to_take_is_refused(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, EMPTY])              # EMPTY's only offer is rejected

    assert main(["--config", str(config_path), "review", "--accept", "3"]) == 1
    assert "no candidate on offer" in capsys.readouterr().err


def test_a_candidate_number_outside_the_offers_is_refused(tmp_path, capsys):
    config_path = make_config(tmp_path)
    seed(config_path, [SETTLED, WAITING])

    assert main(["--config", str(config_path), "review", "--accept", "2", "--candidate", "3"]) == 1
    assert "so 3 is not one of them" in capsys.readouterr().err


# ── what nothing points at (the map's leftovers) ─────────────────────────────


def make_paths_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nstate = "state"\nstaging = "staging"\n'
                    'lossless = "lossless"\nlossy = "lossy"\n', encoding="utf-8")
    return path


def test_the_files_report_names_what_nothing_points_at(tmp_path, capsys, monkeypatch):
    """A refused placement leaves its file in staging, a re-download can leave a second copy in a lane, and
    a row can outlive its file. All three are reported and none is repaired unasked."""
    config_path = make_paths_config(tmp_path)
    config = config_module.load(config_path)
    job = seed(config_path, [SETTLED])
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred",
                                    status=state.DOWNLOADED,
                                    local_path=str(tmp_path / "staging" / "Kiasmos - Blurred.flac"))])

    (tmp_path / "staging").mkdir()
    (tmp_path / "staging" / "Kiasmos - Blurred.flac").write_bytes(b"the track's own file")
    (tmp_path / "staging" / "01 - Left Behind.aiff.part").write_bytes(b"a cancelled transfer's leftovers")
    (tmp_path / "lossless").mkdir()
    (tmp_path / "lossless" / "Nobody - Nothing.aiff").write_bytes(b"a copy no row points at")
    gone = library.Held(artist="Gone", title="Missing",
                        seconds=200, path=str(tmp_path / "lossless" / "Gone - Missing.aiff"))
    monkeypatch.setattr(library, "read_database", lambda: ([gone], []))

    assert main(["--config", str(config_path), "status", "--files"]) == 0

    out = capsys.readouterr().out
    assert "01 - Left Behind.aiff.part" in out
    assert "in staging that this run does not point at" in out
    assert "Nobody - Nothing.aiff" in out
    assert "no row and no record points at" in out
    assert "Gone - Missing.aiff" in out, "a row whose file has gone is the other half of the same report"
    assert "Kiasmos - Blurred.flac" not in out, "the run's own staged file is not a leftover"


def test_the_files_report_says_so_when_there_is_nothing_to_report(tmp_path, capsys, monkeypatch):
    config_path = make_paths_config(tmp_path)
    seed(config_path, [SETTLED])
    monkeypatch.setattr(library, "read_database", lambda: ([], []))

    assert main(["--config", str(config_path), "status", "--files"]) == 0
    assert "nothing left behind" in capsys.readouterr().out


def test_reconcile_from_the_command_line_rebuilds_a_record(tmp_path, capsys):
    """The one stage `main` reaches whose summary is not a `process.Summary`: it has no failure list, and a
    shared report tail that assumes one breaks exactly here."""
    config_path = make_paths_config(tmp_path)
    config = config_module.load(config_path)
    job = seed(config_path, [SETTLED])
    lane = tmp_path / "lossless"
    lane.mkdir(parents=True, exist_ok=True)
    placed = aiff(lane / "Kiasmos - Blurred.aiff")
    records = job.records()
    records[0].status = state.DOWNLOADED
    records[0].local_path = str(tmp_path / "staging" / "whatever.flac")
    job.write_records(records)
    job.append_event("downloaded", idx=1, path=records[0].local_path, peer="vinyl_only")

    assert main(["--config", str(config_path), "reconcile"]) == 0

    out = capsys.readouterr().out
    assert "rebuilt from their files" in out
    record = job.records()[0]
    assert record.local_path == str(placed), "the record follows the file"
    assert record.status == state.NORMALIZED


def test_accepting_carries_the_record_on_to_process(tmp_path, capsys, monkeypatch):
    """The one review decision that needs the network, driven through the stand-in child the way
    `test_acquire` drives the pass."""
    config_path = make_paths_config(tmp_path)
    job = seed(config_path, [WAITING])
    landed = tmp_path / "staging" / "Ntogn - Mines.aiff"
    landed.parent.mkdir()
    landed.write_bytes(b"the file, there when we look")

    fake = {"FAKE_RESULTS": "[]", "FAKE_DOWNLOAD": f"downloading:transferring:90000:{landed}"}
    monkeypatch.setattr(cli_module.slsk, "environment", lambda config: {})
    monkeypatch.setattr(cli_module.mcp, "StdioServer",
                        lambda command, env=None, on_log=None:
                        StdioServer([sys.executable, FAKE], env=fake, on_log=on_log))

    assert main(["--config", str(config_path), "review", "--accept", "2"]) == 0

    out = capsys.readouterr().out
    record = job.records()[0]
    assert record.decision == state.ACCEPTED
    assert record.status == state.DOWNLOADED
    assert record.local_path == str(landed)
    assert "accepted" in out
    assert "music-agent process" in out, "the next command is the useful thing to say"


def test_a_playlist_url_starts_the_run_a_file_would_have(tmp_path, monkeypatch):
    """the user, 2026-10-13: *I'd rather pass a playlist url the same way I pass a file.* So `--tracks` takes
    both, and what a file's stem gave the run a playlist's own name gives it."""
    config = config_module.load(make_paths_config(tmp_path))
    monkeypatch.setattr(cli_module.spotify, "playlist",
                        lambda source, timeout=None: spotify.Playlist(
                            name="Rock Classics",
                            lines=("Kiasmos - Blurred", "Ntogn - Ame No Hi (Original Mix)")))

    job = cli_module._start(config, Namespace(
        tracks="https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=12ab", name=None))

    assert job.id.endswith("-rock-classics")
    assert [(record.artist, record.title, record.mix) for record in job.records()] == [
        ("Kiasmos", "Blurred", ""), ("Ntogn", "Ame No Hi", "Original Mix")]


def test_a_file_still_starts_its_run_the_way_it_always_did(tmp_path):
    """The path branch is the one that was there first, and a URL is not allowed to disturb it."""
    config = config_module.load(make_paths_config(tmp_path))
    tracklist = tmp_path / "Kiasmos - Blurred.txt"
    tracklist.write_text("Kiasmos - Blurred\n", encoding="utf-8")

    job = cli_module._start(config, Namespace(tracks=str(tracklist), name=None))

    assert job.id.endswith("-kiasmos-blurred")
    assert [(record.artist, record.title) for record in job.records()] == [("Kiasmos", "Blurred")]


def test_a_playlist_that_cannot_be_read_starts_nothing(tmp_path, monkeypatch, capsys):
    """A refused playlist has to arrive as what Spotify said, and it must not leave a run behind."""
    config = config_module.load(make_paths_config(tmp_path))

    def refuse(source, timeout=None):
        raise spotify.PlaylistError("Spotify answered 400 for /api/token: Unauthorized request")

    monkeypatch.setattr(cli_module.spotify, "playlist", refuse)

    assert cli_module._start(config, Namespace(
        tracks="https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U", name=None)) is None
    assert "400" in capsys.readouterr().err
    assert not list((tmp_path / "state").glob("*")), "nothing is started for a playlist nobody could read"


def test_a_tracklist_that_cannot_be_read_says_so(tmp_path, capsys):
    """A path with a typo in it is the likeliest way this command is ever wrong, and it has to read like the
    playlist refusal beside it rather than like a traceback."""
    config = config_module.load(make_paths_config(tmp_path))

    assert cli_module._start(config, Namespace(tracks=str(tmp_path / "nope.txt"), name=None)) is None
    assert f"cannot read {tmp_path / 'nope.txt'}" in capsys.readouterr().err
