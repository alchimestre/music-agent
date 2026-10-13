"""The review screen: what it draws, and what its keys write down.

Driven headlessly through textual's own harness, so nothing here needs a terminal. The one decision that
reaches the network — accepting — fetches through the stand-in child process the other tests use, so there
is no Soulseek anywhere in this file.

Ticket 12 keeps two axes apart and these fixtures do too: a candidate's *label* is the rules' business, and
a track's *decision* is the only thing this screen writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from music_agent import config as config_module
from music_agent import mcp, review_tui, state
from music_agent.mcp import StdioServer
from music_agent.rules import Candidate

FAKE = str(Path(__file__).parent / "fake_server.py")


def the_tracks() -> list[state.Record]:
    """A run of four tracks: two the rule will not settle alone, one it settles, one it cannot use.

    The set is the invariant: a track with a Ready candidate is taken before the screen ever sees it, and a
    track whose every candidate was ruled out is not asked about either. Track 1 also carries a rejection,
    which the screen must not draw. Both of its shown cards are there for the one thing the screen is for
    since 2026-10-12 — a peer that reported no length, which is what the ±1 s window cannot be applied to.
    """
    return [
        state.Record(idx=1, artist="Kiasmos", title="Blurred", mix="Extended Mix", duration_s=449,
                     candidates=[
                         Candidate(user="vinyl_only",
                                   path=r"M\Kiasmos - Blurred (Extended Mix).flac", duration=None),
                         Candidate(user="aifflord",
                                   path=r"M\Kiasmos - Blurred (Extended Mix).flac", duration=None,
                                   slots=False),
                         Candidate(user="junk", path=r"M\Other Artist - Something Else.flac",
                                   duration=448),
                     ]),
        state.Record(idx=2, artist="Ntogn", title="Mines", duration_s=528,
                     candidates=[Candidate(user="mole", path=r"M\Ntogn - Mines.aiff",
                                           duration=None)]),
        state.Record(idx=3, artist="Sven Wunder", title="Lotus", duration_s=260,
                     candidates=[Candidate(user="crate",
                                           path=r"M\Sven Wunder - Lotus (Original Mix).aiff",
                                           duration=260)]),
        state.Record(idx=4, artist="Altinbas", title="Trail Of", duration_s=371,
                     candidates=[Candidate(user="peershare", path=r"M\Altinbas - Trail Of.aiff",
                                           duration=374)]),
    ]


def no_server():
    raise AssertionError("a server was opened for a decision that fetches nothing")


def seed(tmp_path: Path, records: list[state.Record]) -> tuple[config_module.Config, state.Job]:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[paths]\nstate = "state"\n', encoding="utf-8")
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records(records)
    return config, job


def run_screen(app: review_tui.ReviewApp, scenario) -> None:
    """Textual's own harness in a loop this test owns — there is no async test plugin here."""
    async def drive() -> None:
        async with app.run_test(size=(132, 38)) as pilot:
            await scenario(pilot)

    asyncio.run(drive())


def status_of(app: review_tui.ReviewApp) -> str:
    return str(app.query_one("#status", Static).render())


def cards(app: review_tui.ReviewApp) -> list[review_tui.CandidateCard]:
    pane = app.query_one("#pane", VerticalScroll)
    return [child for child in pane.children if isinstance(child, review_tui.CandidateCard)]


# ── the queue ────────────────────────────────────────────────────────────────


def test_the_queue_holds_the_tracks_the_run_asks_about(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        table = pilot.app.query_one("#queue", DataTable)
        assert [column.label.plain for column in table.ordered_columns] == ["DECISION", "TRACK"]
        assert table.row_count == 2, "the settled track and the unusable one are not asked about"
        assert [str(table.get_row_at(index)[1]) for index in range(2)] == [
            "Kiasmos - Blurred (Extended Mix)", "Ntogn - Mines"]
        assert [str(table.get_row_at(index)[0]) for index in range(2)] == ["·", "·"]

    run_screen(app, scenario)


def test_a_track_with_a_ready_candidate_is_never_in_the_queue(tmp_path):
    """Ticket 18: the ladder settles a tie, so nothing Ready is ever presented as a question."""
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        table = pilot.app.query_one("#queue", DataTable)
        drawn = [str(table.get_row_at(index)[1]) for index in range(table.row_count)]
        assert not any("Lotus" in label for label in drawn)

    run_screen(app, scenario)


# ── the cards ────────────────────────────────────────────────────────────────


def test_the_pane_draws_the_candidates_on_offer_and_no_others(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        pane = pilot.app.query_one("#pane", VerticalScroll)
        assert "Kiasmos - Blurred" in str(pane.children[0].render())
        drawn = [str(card.render()) for card in cards(pilot.app)]
        assert len(drawn) == 2, "three candidates were offered and one was ruled out"
        assert all("junk" not in text for text in drawn)
        assert "the peer reports no length" in drawn[0], "the reason is on the selected card"
        assert "the peer reports no length" not in drawn[1], "and not on the others"

    run_screen(app, scenario)


def test_the_mix_is_emphasised_without_the_gap_before_it(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        line = cards(pilot.app)[0].render()
        emphasised = next(span for span in line.spans if span.style == "bold reverse")
        assert line.plain[emphasised.start:emphasised.end] == "(Extended Mix)"
        assert line.plain[:emphasised.start].endswith("  "), "the gap is not part of the emphasis"

    run_screen(app, scenario)


def test_the_peer_line_says_whether_the_peer_has_a_slot(tmp_path):
    """Ticket 12's card: a peer with a queue behind it is a wait, and every offer the record holds is one
    the pass may have been told about seconds ago."""
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        drawn = [str(card.render()) for card in cards(pilot.app)]
        assert "peer vinyl_only · queue 0 · slots yes" in drawn[0]
        assert "peer aifflord · queue 0 · slots no" in drawn[1]

    run_screen(app, scenario)


def test_the_status_line_carries_the_run_and_the_counts(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        line = status_of(pilot.app)
        assert line.startswith(job.id), "the run this screen is over"
        assert "candidate 1/2" in line
        assert "nothing decided yet" in line

    run_screen(app, scenario)


def test_n_and_p_wrap_around_the_candidates(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        await pilot.press("n")
        assert "candidate 2/2" in status_of(pilot.app)
        await pilot.press("n")
        assert "candidate 1/2" in status_of(pilot.app), "`n` on the last card comes back round"
        await pilot.press("p")
        assert "candidate 2/2" in status_of(pilot.app)

    run_screen(app, scenario)


def test_the_arrows_move_the_queue_and_the_pane_follows(tmp_path):
    """The table keeps its own navigation keys, as the prototype's did."""
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        await pilot.press("down")
        await pilot.pause()
        assert pilot.app.query_one("#queue", DataTable).cursor_row == 1
        assert "Ntogn - Mines" in str(pilot.app.query_one("#pane", VerticalScroll).children[0].render())
        assert "candidate 1/1" in status_of(pilot.app)
        await pilot.press("up")
        await pilot.pause()
        assert "candidate 1/2" in status_of(pilot.app), "back on the first track, with its two cards"

    run_screen(app, scenario)


# ── the decisions ────────────────────────────────────────────────────────────


def test_skip_writes_the_decision_and_moves_to_the_next_track(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        await pilot.press("s")
        table = pilot.app.query_one("#queue", DataTable)
        assert str(table.get_row_at(0)[0]) == "–"
        assert table.get_row_at(0)[1].style == "dim"
        assert table.cursor_row == 1, "the next track is under the cursor"
        assert "1 skipped" in status_of(pilot.app)

    run_screen(app, scenario)

    record = job.records()[0]
    assert record.decision == state.SKIPPED
    assert record.status == state.PENDING, "nothing about the track changed but the decision"
    assert [entry["event"] for entry in job.events()][-1] == "skipped"


def test_not_found_writes_the_decision_and_moves_to_the_next_track(tmp_path):
    config, job = seed(tmp_path, the_tracks())
    app = review_tui.ReviewApp(config, job, no_server)

    async def scenario(pilot):
        await pilot.press("f")
        assert str(pilot.app.query_one("#queue", DataTable).get_row_at(0)[0]) == "✗"
        assert "1 not found" in status_of(pilot.app)

    run_screen(app, scenario)

    record = job.records()[0]
    assert record.decision == state.DECLINED
    assert record.status == state.NOT_FOUND


def test_accepting_fetches_the_candidate_and_settles_the_track(tmp_path):
    """The one decision that needs the network, through the stand-in child process."""
    config, job = seed(tmp_path, the_tracks())
    landed = tmp_path / "staging" / "Kiasmos - Blurred (Original Mix).flac"
    landed.parent.mkdir()
    landed.write_bytes(b"the file, there when we look")
    env = dict(os.environ)
    env["FAKE_RESULTS"] = json.dumps([])
    env["FAKE_DOWNLOAD"] = f"downloading:transferring:90000:{landed}"
    env["FAKE_LOG"] = str(tmp_path / "calls.jsonl")

    def factory():
        return StdioServer([sys.executable, FAKE, "normal"], env=env)

    app = review_tui.ReviewApp(config, job, factory)

    async def scenario(pilot):
        await pilot.press("a")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert "accepted" in status_of(pilot.app)
        assert str(pilot.app.query_one("#queue", DataTable).get_row_at(0)[0]) == "✓"

    run_screen(app, scenario)

    record = job.records()[0]
    assert record.status == state.DOWNLOADED
    assert record.decision == state.ACCEPTED
    assert record.local_path == str(landed)
    assert record.format == "flac"
    calls = [json.loads(line)["tool"] for line in
             (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "download" in calls, "the file was fetched through the injected server"


def test_a_fetch_that_fails_says_so_and_leaves_the_queue_usable(tmp_path):
    config, job = seed(tmp_path, the_tracks())

    def refuses():
        raise mcp.ServerError("the child would not start")

    app = review_tui.ReviewApp(config, job, refuses)

    async def scenario(pilot):
        await pilot.press("a")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert "the child would not start" in status_of(pilot.app)
        await pilot.press("n")
        assert "candidate 2/2" in status_of(pilot.app), "the screen still answers"

    run_screen(app, scenario)

    record = job.records()[0]
    assert record.decision == state.UNDECIDED, "nothing was decided"
    assert record.status == state.PENDING


# ── the Δ ────────────────────────────────────────────────────────────────────


def test_the_change_in_length_is_coloured_by_how_close_it_is():
    reference = 300
    green = review_tui.delta_text(Candidate(user="a", path="a.flac", duration=300), reference)
    yellow = review_tui.delta_text(Candidate(user="a", path="a.flac", duration=303), reference)
    red = review_tui.delta_text(Candidate(user="a", path="a.flac", duration=306), reference)
    unknown = review_tui.delta_text(Candidate(user="a", path="a.flac", duration=None), reference)

    assert green.plain == "Δ+0s" and green.style == "bold green"
    assert yellow.plain == "Δ+3s" and yellow.style == "bold yellow"
    assert red.plain == "Δ+6s" and red.style == "bold red"
    assert unknown.plain == "Δ ?" and unknown.style == "dim"
    assert review_tui.delta_text(Candidate(user="a", path="a.flac", duration=300), None).plain == "Δ ?"


def test_the_mix_of_a_bare_name_is_not_invented():
    """Ticket 05: a name with no mix is the Original Mix, and the card simply has no mix line for it."""
    assert review_tui.split_mix("Ntogn - Mines.aiff") == ("Ntogn - Mines", "")
    assert review_tui.split_mix("Kiasmos - Blurred (Extended Mix).flac") == (
        "Kiasmos - Blurred", "Extended Mix")


# ── what the screen is not ───────────────────────────────────────────────────


def test_nothing_needs_a_decision_means_no_screen_at_all(tmp_path, capsys):
    """Nothing to ask about is not a reason to take the terminal over."""
    config, job = seed(tmp_path, [the_tracks()[2]])

    assert review_tui.run(config, job, server_factory=no_server) == 0
    assert "nothing needs a decision" in capsys.readouterr().out


def test_closing_the_screen_is_a_clean_exit(tmp_path, monkeypatch):
    """`q` leaves through `App.exit`, which yields None, and the CLI wants an exit code."""
    config, job = seed(tmp_path, the_tracks())
    monkeypatch.setattr(review_tui.ReviewApp, "run", lambda self, **kwargs: None)

    assert review_tui.run(config, job, server_factory=no_server) == 0
