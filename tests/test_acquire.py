"""The acquisition stage, against a stand-in server: no Soulseek, and no waiting either.

The download policy is tested directly — it is the part that would otherwise only be exercisable by
waiting a quarter of an hour on a real peer — and the flow around it is tested end to end through the
protocol, with a clock the test moves by hand.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from music_agent import acquire
from music_agent import config as config_module
from music_agent import library, reference, state
from music_agent.mcp import StdioServer

FAKE = str(Path(__file__).parent / "fake_server.py")

GOOD = [
    {
        "id": "peerA:Music\\Kiasmos - Blurred.flac",
        "username": "peerA",
        "filename": "Music\\Kiasmos - Blurred.flac",
        "filesize": 82_000_000,
        "extension": "flac",
        "bitrate": 855,
        "duration_sec": 449,
        "queue_size": 0,
    }
]
TWO_PEERS = GOOD + [
    {
        "id": "peerB:Other\\Kiasmos - Blurred.flac",
        "username": "peerB",
        "filename": "Other\\Kiasmos - Blurred.flac",
        "filesize": 81_000_000,
        "extension": "flac",
        "bitrate": 900,
        "duration_sec": 449,
        "queue_size": 0,
    }
]
WRONG_LENGTH = [
    {
        "id": "peerA:Music\\Kiasmos - Blurred (Extended Mix).flac",
        "username": "peerA",
        "filename": "Music\\Kiasmos - Blurred (Extended Mix).flac",
        "extension": "flac",
        "duration_sec": 520,
        "queue_size": 0,
    }
]
NO_LENGTH = [
    {
        "id": "peerA:Music\\Kiasmos - Blurred.aiff",
        "username": "peerA",
        "filename": "Music\\Kiasmos - Blurred.aiff",
        "extension": "aiff",
        "duration_sec": None,
        "queue_size": 0,
    }
]


class Clock:
    """A clock the test moves, so no budget costs any real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class Reference:
    """A lookup that answers at once, which is what makes candidates Ready."""

    def __init__(self, seconds: int = 449) -> None:
        self.seconds = seconds
        self.asked: list[str] = []

    def find(self, query: str, limit: int = 25) -> list[reference.Recording]:
        self.asked.append(query)
        return [reference.Recording(title="Blurred", artist="Kiasmos", length_ms=self.seconds * 1000)]


class Quiet:
    """A log and a sleep that do nothing but move the clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def sleep(self, seconds: float) -> None:
        self.clock.tick(10)     # a fixed step, so a budget is crossed in a countable number of polls


def setup(tmp_path: Path, *, results: list[dict], download: str | None = None, tracks: int = 1):
    """A config, a job holding `tracks` tracks, and a stand-in server wired to the scenario.

    Pacing is off here: these tests are about the loop and the download policy, and a pace would put sleeps
    of its own into a clock the test moves by hand. Ticket 21's pace has its own test, which sets one.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[paths]\nstaging = "{tmp_path / "staging"}"\nstate = "state"\n'
                           "[soulseek]\nsearch_interval = 0.0\n",
                           encoding="utf-8")
    (tmp_path / "staging").mkdir()
    config = config_module.load(config_path)

    job = state.create(config, "test")
    titles = ["Blurred"] + [f"Another {index}" for index in range(2, tracks + 1)]
    job.write_records([state.Record(idx=index, artist="Kiasmos", title=title)
                       for index, title in enumerate(titles, start=1)])

    env = dict(os.environ)
    env["FAKE_RESULTS"] = json.dumps(results)
    if download is not None:
        env["FAKE_DOWNLOAD"] = download
    calls = tmp_path / "calls.jsonl"
    env["FAKE_LOG"] = str(calls)

    return config, job, StdioServer([sys.executable, FAKE, "normal"], env=env), calls


def calls_made(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tools_called(path: Path) -> list[str]:
    return [entry["tool"] for entry in calls_made(path)]


def pass_once(config, job, server, lookup, calls: Path, shelf=None) -> tuple[acquire.Summary, Quiet]:
    quiet = Quiet(Clock())
    summary = acquire.run(config, job, server, lookup, library=shelf, log=quiet.log,
                          sleep=quiet.sleep, clock=quiet.clock)
    return summary, quiet


# ── the policy, on its own ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,connection,on_disk,stuck,queue,expected",
    [
        # The disk decides, whatever the server believes: the finished record is dropped after
        # sixty seconds, so a later poll says not_found about a download that worked.
        ("not_found", "", True, 1.0, 0, acquire.Move.DONE),
        ("queued", "waiting_for_peer", True, 999.0, 0, acquire.Move.DONE),
        # A finished transfer whose file has not appeared is the rename, which is quick.
        ("finished", "", False, 1.0, 0, acquire.Move.WAIT),
        ("finished", "", False, 16.0, 0, acquire.Move.FAILED),
        ("failed", "", False, 1.0, 0, acquire.Move.FAILED),
        ("cancelled", "", False, 1.0, 0, acquire.Move.FAILED),
        ("session_expired", "", False, 1.0, 0, acquire.Move.FAILED),
        # A transfer in flight is never rotated on the connect timer, however long it has been at zero.
        ("downloading", "transferring", False, 46.0, 0, acquire.Move.WAIT),
        ("downloading", "transferring", False, 301.0, 0, acquire.Move.ROTATE),
        # A connection that never transferred a byte is rotated at 45 seconds.
        ("queued", "waiting_for_peer", False, 46.0, 0, acquire.Move.ROTATE),
        ("queued", "connecting", False, 30.0, 0, acquire.Move.WAIT),
        # A peer that declared a queue is behaving normally rather than failing, and gets the longer
        # budget.
        ("queued", "waiting_for_peer", False, 46.0, 5, acquire.Move.WAIT),
        ("queued", "waiting_for_peer", False, 301.0, 5, acquire.Move.ROTATE),
    ],
)
def test_the_decision_to_wait_rotate_or_give_up(status, connection, on_disk, stuck, queue, expected):
    transfer = acquire.Transfer(status=status, connection_state=connection)
    assert acquire.move(transfer, on_disk=on_disk, stuck_for=stuck, declared_queue=queue) is expected


# ── the flow ─────────────────────────────────────────────────────────────────


def test_a_download_that_lands_is_recorded_and_the_run_left_open(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, already there when we look")

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.status == state.DOWNLOADED
    assert record.format == "flac"
    assert record.local_path == str(landed)
    assert record.tried == ["peerA:Music\\Kiasmos - Blurred.flac"]
    assert not job.finished(), "the import finishes a run, not this stage"
    assert "download" in tools_called(calls)


def test_the_file_on_disk_wins_over_a_server_that_says_not_found(tmp_path):
    """Ticket 03's trap: the record of a finished download is dropped after a minute, so `not_found` can
    describe a download that succeeded."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD, download=f"not_found::0:{landed}")
    landed.write_bytes(b"a file that did arrive")

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    assert summary.downloaded == [1], quiet.lines
    assert job.records()[0].status == state.DOWNLOADED


def test_a_track_with_nothing_ready_waits_for_a_human(tmp_path):
    config, job, server, calls = setup(tmp_path, results=NO_LENGTH)

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    assert summary.in_review == [1], quiet.lines
    assert job.records()[0].status == state.PENDING
    assert "download" not in tools_called(calls), "nothing was Ready to take"


def test_a_search_with_no_usable_candidate_says_so(tmp_path):
    config, job, server, calls = setup(tmp_path, results=WRONG_LENGTH)

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    assert summary.nothing_usable == [1], quiet.lines
    assert "download" not in tools_called(calls)


def test_an_empty_search_leaves_nothing_usable(tmp_path):
    config, job, server, calls = setup(tmp_path, results=[])

    with server:
        summary, _ = pass_once(config, job, server, Reference(), calls)

    assert summary.nothing_usable == [1]


def test_a_connect_stall_rotates_to_the_next_peer_and_cancels_the_first(tmp_path):
    landed = tmp_path / "staging" / "from peerB.flac"
    config, job, server, calls = setup(
        tmp_path,
        results=TWO_PEERS,
        download=f"queued:waiting_for_peer:0:|downloading:transferring:90000:{landed}",
    )
    landed.write_bytes(b"the one that arrived")

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.local_path == str(landed)
    assert "peerB:Other\\Kiasmos - Blurred.flac" in record.tried
    assert "cancel_download" in tools_called(calls), "a stalled transfer must not hold a transfer slot"


def test_every_candidate_failing_leaves_the_track_to_retry(tmp_path):
    config, job, server, calls = setup(tmp_path, results=TWO_PEERS, download="failed::0:")

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    assert summary.failed == [1], quiet.lines
    assert job.records()[0].status == state.RETRY_LATER
    assert not job.finished(), "a run with a track left to retry is not finished"


def test_the_reference_is_resolved_once_and_only_once(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"x")
    lookup = Reference()

    with server:
        pass_once(config, job, server, lookup, calls)
        pass_once(config, job, server, lookup, calls)

    assert len(lookup.asked) == 1, "a resumed run must not ask MusicBrainz again (the design notes)"
    assert job.records()[0].duration_s == 449


def test_a_track_already_downloaded_is_not_fetched_again(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"x")

    with server:
        pass_once(config, job, server, Reference(), calls)
        second, quiet = pass_once(config, job, server, Reference(), calls)

    assert tools_called(calls).count("download") == 1
    assert second.already_downloaded == [1], quiet.lines
    assert job.records()[0].local_path == str(landed)


def test_a_track_this_run_already_placed_and_tagged_is_not_searched_again(tmp_path):
    """Ticket 09, one status further on. Until the import runs the collection still points at the lossy copy
    being replaced, so the dedupe alone finds a finished track "not held" and re-downloads it, resetting the
    status — which is how a retry pass left 43 placed and tagged records unimportable on 2026-10-12."""
    config, job, server, calls = setup(tmp_path, results=GOOD)
    placed = config.lossless / "Kiasmos - Blurred.aiff"
    placed.parent.mkdir(parents=True, exist_ok=True)
    placed.write_bytes(b"audio")
    record = job.records()[0]
    record.status = state.TAGGED
    record.local_path = str(placed)
    job.write_records([record])

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls)

    assert summary.already_downloaded == [1], quiet.lines
    assert tools_called(calls) == [], "not even a search: the file it produced is right there"
    assert job.records()[0].status == state.TAGGED, "and its status is left where it was"


def test_a_later_pass_asks_only_for_peers_with_free_slots(tmp_path):
    """Ticket 04: peer attributes may scope a retry pass, and never the first one."""
    config, job, server, calls = setup(tmp_path, results=[])
    record = job.records()[0]

    with server:
        acquire.search(server, record, later_pass=False)
        acquire.search(server, record, later_pass=True)

    searches = [entry["arguments"] for entry in calls_made(calls) if entry["tool"] == "search"]
    assert searches[0]["query"] == "kiasmos blurred"
    assert "free_slots_only" not in searches[0]
    assert searches[1]["free_slots_only"] is True


class Nothing:
    """A reference that resolves nothing, the way a white-label does."""

    def find(self, query, limit=25):
        return []


LOSSY_ONLY = [
    {
        "id": "peerA:Music\\Kiasmos - Blurred.m4a",
        "username": "peerA",
        "filename": "Music\\Kiasmos - Blurred.m4a",
        "extension": "m4a",
        "bitrate": 320,
        "duration_sec": 449,
        "queue_size": 0,
    }
]


def held_lossy(tmp_path):
    """The collection holding this track, but only as a lossy copy."""
    return library.Library(
        [library.Held("Kiasmos", "Blurred", 449, path=str(tmp_path / "Losy Tracks" / "Kiasmos - Blurred.mp3"))],
        [],
    )


def test_a_lossy_copy_held_is_an_upgrade_not_a_skip(tmp_path):
    """Ticket 09: a lossy copy does not make a track held, when a lossless one can be had."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the lossless one")

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls, shelf=held_lossy(tmp_path))

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.replaces.endswith("Kiasmos - Blurred.mp3")
    assert "upgrades" in record.notes


def test_the_copy_in_the_collection_is_the_reference_when_musicbrainz_has_none(tmp_path):
    """Ticket 13 governs a bare tracklist; this is not one. Four of five of the user's lossy tracks have no
    MusicBrainz recording at all, and the copy in hand is the same track by definition."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, server, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the lossless one")

    with server:
        summary, quiet = pass_once(config, job, server, Nothing(), calls, shelf=held_lossy(tmp_path))

    assert summary.downloaded == [1], quiet.lines
    assert job.records()[0].duration_s == 449, "the held file's own length"


def test_a_lossy_candidate_is_not_an_upgrade(tmp_path):
    """A lossy copy is not better than the lossy copy the user already has."""
    config, job, server, calls = setup(tmp_path, results=LOSSY_ONLY)

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls, shelf=held_lossy(tmp_path))

    assert summary.in_review == [1], quiet.lines
    assert "download" not in tools_called(calls)


def test_a_track_the_library_already_holds_is_not_even_searched(tmp_path):
    """Ticket 09: nothing is fetched that the collection holds. It is asked before the search, so an
    already-held track costs no request at all — which is exactly what the first smoke run showed it
    was missing."""
    config, job, server, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library(
        [library.Held("Kiasmos", "Blurred", 449, path="/Library/Kiasmos - Blurred.aiff")], []
    )

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls, shelf=shelf)

    record = job.records()[0]
    assert summary.already_held == [1], quiet.lines
    assert record.status == state.FOUND_IN_LIBRARY
    assert record.local_path == ""
    assert tools_called(calls) == [], "an already-held track should not have been searched for"
    assert not job.finished(), "the import finishes a run, not this stage"


def test_a_track_the_library_holds_at_another_length_is_still_fetched(tmp_path):
    config, job, server, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library([library.Held("Kiasmos", "Blurred", 520)], [])

    with server:
        summary, quiet = pass_once(config, job, server, Reference(), calls, shelf=shelf)

    assert summary.already_held == []
    assert "search" in tools_called(calls)


def test_a_server_that_refuses_is_left_to_retry_rather_than_crashing(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[paths]\nstaging = "{tmp_path / "staging"}"\nstate = "state"\n',
                           encoding="utf-8")
    config = config_module.load(config_path)
    job = state.create(config, "test")
    job.write_records([state.Record(idx=1, artist="Kiasmos", title="Blurred")])

    with StdioServer([sys.executable, FAKE, "refuses"]) as server:
        quiet = Quiet(Clock())
        summary = acquire.run(config, job, server, Reference(), log=quiet.log, sleep=quiet.sleep,
                              clock=quiet.clock)

    assert summary.failed == [1]
    assert job.records()[0].status == state.RETRY_LATER
    assert any("refused" in line for line in quiet.lines)


# ── how much of it runs at once (2026-10-12) ─────────────────────────────────────────────────────


def test_the_pass_works_on_several_tracks_at_once(tmp_path):
    """the user: one at a time is nonsense. Two tracks have to be waiting on their own downloads at the
    same moment, which a pass that works through them in turn can never do — it would sit in its first
    poll until the barrier gave up."""
    import threading

    config, job, server, calls = setup(tmp_path, results=GOOD + [{
        "id": "peerA:Music\\Kiasmos - Another 2.flac",
        "username": "peerA",
        "filename": "Music\\Kiasmos - Another 2.flac",
        "extension": "flac",
        "bitrate": 900,
        "duration_sec": 449,
        "queue_size": 0,
    }], tracks=2)
    clock = Clock()
    met = threading.Barrier(2, timeout=5)
    guard = threading.Lock()
    waited: set[str] = set()

    class AnswersAnyTitle:
        """The stand-in lookup answers for one title only, and a track with no reference is reviewed
        rather than downloaded — so this one resolves whatever it is asked about and both tracks poll."""

        def find(self, query: str, limit: int = 25) -> list[reference.Recording]:
            asked = query.split('recording:"')[1].split('"')[0]
            return [reference.Recording(title=asked, artist="Kiasmos", length_ms=449_000)]

    def sleep(seconds: float) -> None:
        clock.tick(10)
        mine = threading.current_thread().name
        with guard:
            first = mine not in waited
            if first:
                waited.add(mine)
        if first:
            met.wait()      # once per worker, so a pass that runs them in turn never gets its second

    with server:
        summary = acquire.run(config, job, server, AnswersAnyTitle(), log=lambda line: None,
                              sleep=sleep, clock=clock)

    settled = (summary.downloaded + summary.already_downloaded + summary.already_held
               + summary.in_review + summary.nothing_usable + summary.failed)
    assert sorted(settled) == [1, 2]
    assert len(job.records()) == 2


def test_searches_are_spaced_out_rather_than_fired_as_fast_as_the_loop_can(tmp_path):
    """Ticket 21. A pass asked for 330 searches in six minutes and everything after the first seconds came
    back empty — no error, no warning, and nothing in the child or in aioslsk that keeps to a rate or
    reports one being refused. So the gap between two searches is the pass's own to keep.

    One worker, so the reservations run in a known order and the gaps are exact; a pace that only held
    with the parallelism turned off would be no pace at all, which is what the busy-wait below checks.
    """
    from dataclasses import replace

    config, job, server, calls = setup(tmp_path, results=[], tracks=4)
    config = replace(config, search_interval=3.0, workers=1)
    clock = Clock()
    gaps: list[float] = []

    def sleep(seconds: float) -> None:
        gaps.append(seconds)
        clock.tick(seconds)

    with server:
        acquire.run(config, job, server, Reference(), log=lambda line: None, sleep=sleep, clock=clock)

    assert len(tools_called(calls)) == 4, "the pace must not cost a track its search"
    assert gaps == [3.0, 3.0, 3.0], "four tracks, three gaps, each exactly the interval"


def test_the_musicbrainz_lookup_is_asked_one_at_a_time():
    """Their rate limiter is a module global and their policy asks for one request a second, so our calls
    queue behind one another rather than racing inside a library that is not written for it."""
    import threading
    import time

    inside = 0
    most = 0
    guard = threading.Lock()

    class Watched:
        def find(self, query: str, limit: int = 25) -> list[reference.Recording]:
            nonlocal inside, most
            with guard:
                inside += 1
                most = max(most, inside)
            time.sleep(0.05)
            with guard:
                inside -= 1
            return []

    serialised = acquire.one_at_a_time(Watched())
    threads = [threading.Thread(target=serialised.find, args=(f"q{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most == 1
