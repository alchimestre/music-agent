"""The acquisition stage, against a stand-in server: no Soulseek, and no waiting either.

The download policy is tested directly — it is the part that would otherwise only be exercisable by
waiting a quarter of an hour on a real peer — and the flow around it is tested end to end through the
protocol, with a clock the test moves by hand.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from music_agent import acquire
from music_agent import config as config_module
from music_agent import lanes, library, peers, reference, slsk, state
from music_agent.rules import Candidate

from fake_server import Direct

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
# A peer that answered while it had no free slot: the offer is kept, because a peer with a queue behind it
# is a wait rather than a failure.
NO_SLOTS = [
    {
        "id": "busy:Music\\Kiasmos - Blurred.aiff",
        "username": "busy",
        "filename": "Music\\Kiasmos - Blurred.aiff",
        "extension": "aiff",
        "duration_sec": None,
        "queue_size": 3,
        "has_free_slots": False,
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


def setup(tmp_path: Path, *, results: list[dict], download: str | None = None, tracks: int = 1,
          soulseek: str = "", session_loss: bool = False):
    """A config, a job holding `tracks` tracks, and a stand-in server wired to the scenario.

    Pacing is off here: these tests are about the loop and the download policy, and a pace would put sleeps
    of its own into a clock the test moves by hand. The pace has its own test, which sets one.

    The ignore list is written under `tmp_path` too: a pass reads and writes it, and the default path is a
    real one on a machine that runs this project.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[paths]\nstaging = "{tmp_path / "staging"}"\n'
                           f'lossless = "{tmp_path / "Loseless Tracks"}"\n'
                           f'lossy = "{tmp_path / "Losy Tracks"}"\n'
                           'state = "state"\n'
                           f'peers = "{tmp_path / "peers.json"}"\n'
                           '[soulseek]\nsearch_interval = 0.0\n'
                           f'state_dir = "{tmp_path}"\n' + soulseek,
                           encoding="utf-8")
    (tmp_path / "staging").mkdir()
    config = config_module.load(config_path)

    job = state.create(config, "test")
    titles = ["Blurred"] + [f"Another {index}" for index in range(2, tracks + 1)]
    job.write_records([state.Record(idx=index, artist="Kiasmos", title=title)
                       for index, title in enumerate(titles, start=1)])

    daemon = Direct(results=results, download=download, session_loss=session_loss)
    return config, job, daemon, daemon.calls


def calls_made(calls: list[tuple[str, dict]]) -> list[dict]:
    return [{"tool": method, "arguments": params} for method, params in calls]


def tools_called(calls: list[tuple[str, dict]]) -> list[str]:
    return [method for method, _ in calls]


def pass_once(config, job, daemon, lookup, calls, shelf=None) -> tuple[acquire.Summary, Quiet]:
    quiet = Quiet(Clock())
    summary = acquire.run(config, job, daemon, lookup, library=shelf, log=quiet.log,
                          sleep=quiet.sleep, clock=quiet.clock)
    return summary, quiet


# ── the policy, on its own ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,on_disk,stuck,queue,expected",
    [
        # The disk decides, whatever the daemon believes: a transfer the daemon has forgotten is still a
        # download that worked if its file is there.
        ("queued", True, 999.0, 0, acquire.Move.DONE),
        ("missing", True, 1.0, 0, acquire.Move.DONE),
        # A completed transfer whose file has not appeared is the rename, which is quick.
        ("completed", False, 1.0, 0, acquire.Move.WAIT),
        ("completed", False, 6.0, 0, acquire.Move.FAILED),
        ("failed", False, 1.0, 0, acquire.Move.FAILED),
        ("cancelled", False, 1.0, 0, acquire.Move.FAILED),
        ("timed_out", False, 1.0, 0, acquire.Move.FAILED),
        # A transfer in flight is never rotated on the connect timer, however long it has been at zero.
        ("in_progress", False, 44.0, 0, acquire.Move.WAIT),
        ("in_progress", False, 46.0, 0, acquire.Move.ROTATE),
        # A queued transfer is a wait, not a connection that never opened.
        ("queued", False, 44.0, 0, acquire.Move.WAIT),
        ("queued", False, 46.0, 0, acquire.Move.ROTATE),
        # A position in the five figures is a mirror, and is rotated on the connect budget.
        ("queued", False, 13.0, 159_626, acquire.Move.ROTATE),
        ("queued", False, 11.0, 159_626, acquire.Move.WAIT),
    ],
)
def test_the_decision_to_wait_rotate_or_give_up(status, on_disk, stuck, queue, expected):
    transfer = acquire.Transfer(status=status, queue=queue)
    assert acquire.move(transfer, on_disk=on_disk, stuck_for=stuck) is expected


@pytest.mark.parametrize(
    "decision,status,received,queue,expected",
    [
        # Nothing to answer for: the file arrived.
        (acquire.Move.DONE, "completed", 90_000, 0, None),
        # A peer that fails outright, or claims a file and leaves no bytes, is the whole point.
        (acquire.Move.FAILED, "failed", 0, 0, "failed"),
        (acquire.Move.FAILED, "timed_out", 0, 0, "timed_out"),
        # Ours, not theirs: a cancel is a decision we made.
        (acquire.Move.FAILED, "cancelled", 0, 0, None),
        (acquire.Move.FAILED, "completed", 0, 0, None),
        # A connect that never opened is theirs.
        (acquire.Move.ROTATE, "queued", 0, 0, "nothing arrived"),
        # A declared queue is normal Soulseek, and a peer that has sent bytes has shown it can send them.
        (acquire.Move.ROTATE, "queued", 0, 5, None),
        (acquire.Move.ROTATE, "in_progress", 4096, 0, None),
        # A queue in the hundreds of thousands is a paid mirror holding every request it ever gets: the
        # declaration is the evidence, not the excuse (2026-10-16, from a real pass: DJ-Promo 10230,
        # musicmasterrdjpool 159626, both with no free slots, neither ever delivering a byte).
        (acquire.Move.ROTATE, "queued", 0, acquire.ABSURD_QUEUE, "nothing arrived"),
        (acquire.Move.ROTATE, "queued", 0, 159_626, "nothing arrived"),
    ],
)
def test_what_a_lost_download_says_about_the_peer(decision, status, received, queue, expected):
    """Eating a budget is not the peer's fault, which is the difference between an ignore list and a pass
    that bans whoever it was waiting for."""
    report = acquire.Transfer(status=status, received_bytes=received, queue=queue)

    assert acquire.offence(decision, report) == expected


# ── the candidate a search result becomes ────────────────────────────────────


def test_a_lossy_result_with_no_length_gets_one_from_its_size():
    """A peer that reports a bitrate and a size has reported the length: bytes = bitrate × seconds / 8. Without
    that, a file the rule could have judged was offered for review instead (2026-10-12)."""
    item = {"username": "peer", "filename": r"M\Kiasmos - Blurred.mp3", "filesize": 12_000_000,
            "bitrate": 320, "queue_size": 0}

    assert acquire.candidate_of(item).duration == 300


def test_a_lossless_result_is_not_given_a_length_from_its_size():
    """`bitrate` on a lossless file is the server's quality score, not the rate it was encoded at: the
    arithmetic would be a couple of seconds out, and the one-second window would act on it."""
    item = {"username": "peer", "filename": r"M\Kiasmos - Blurred.flac", "filesize": 37_773_404,
            "bitrate": 887, "queue_size": 0}

    assert acquire.candidate_of(item).duration is None


def test_a_reported_length_is_left_alone():
    item = {"username": "peer", "filename": r"M\Kiasmos - Blurred.mp3", "duration_sec": 448.9,
            "filesize": 12_000_000, "bitrate": 320, "queue_size": 0}

    assert acquire.candidate_of(item).duration == 448


def test_a_size_with_no_bitrate_still_gives_no_length():
    item = {"username": "peer", "filename": r"M\Kiasmos - Blurred.mp3", "filesize": 12_000_000,
            "bitrate": None, "queue_size": 0}

    assert acquire.candidate_of(item).duration is None


def test_the_servers_own_view_of_the_peer_is_carried_onto_the_candidate():
    """`avg_speed` and the free-slot count are the search answer's own account of the peer, and they order a
    peer that cannot serve behind one that can."""
    item = {"username": "peer", "filename": r"M\Kiasmos - Blurred.flac", "filesize": 12_000_000,
            "has_free_slots": False, "avg_speed": 0}

    candidate = acquire.candidate_of(item)

    assert (candidate.slots, candidate.speed) == (False, 0)


# ── the flow ─────────────────────────────────────────────────────────────────


def test_a_download_that_lands_is_recorded_and_the_run_left_open(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, already there when we look")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.status == state.DOWNLOADED
    assert record.format == "flac"
    assert record.local_path == str(landed)
    assert record.tried == ["peerA:Music\\Kiasmos - Blurred.flac"]
    assert not job.finished(), "the import finishes a run, not this stage"
    assert "download.start" in tools_called(calls)


def test_the_file_on_disk_wins_over_a_server_that_says_not_found(tmp_path):
    """The trap: the record of a finished download is dropped after a minute, so `not_found` can
    describe a download that succeeded."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD, download=f"not_found::0:{landed}")
    landed.write_bytes(b"a file that did arrive")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.downloaded == [1], quiet.lines
    assert job.records()[0].status == state.DOWNLOADED


def test_a_track_settled_by_hand_is_not_searched_again(tmp_path):
    """The decision axis is what a pass reads, so an answered question is not reopened."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    records = job.records()
    records[0].decision = state.SKIPPED
    job.write_records(records)

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.decided == [1], quiet.lines
    assert summary.in_review == [] and summary.nothing_usable == []
    assert "search" not in tools_called(calls)
    assert job.records()[0].status == state.PENDING, "left exactly as it was"


def test_a_peer_that_reported_no_free_slot_is_remembered(tmp_path):
    """The answer belongs to the candidate, so it has to survive the search result rather than being
    dropped on the way into the record."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.aiff"
    config, job, daemon, calls = setup(tmp_path, results=NO_SLOTS,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, already there when we look")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.downloaded == [1], quiet.lines
    candidate = job.records()[0].candidates[0]
    assert candidate.slots is False


def test_a_track_with_no_length_to_check_is_taken_on_its_name(tmp_path):
    """2026-10-14: it used to wait for a person. Nothing can check the length, so the file stands on its name
    and is taken — the queue is not where an unverifiable offer goes."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.aiff"
    config, job, daemon, calls = setup(tmp_path, results=NO_LENGTH,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, already there when we look")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.downloaded == [1], quiet.lines
    assert summary.in_review == []
    assert job.records()[0].status == state.DOWNLOADED
    assert "download.start" in tools_called(calls), "taken, not put in the queue"


def test_a_search_with_no_usable_candidate_says_so(tmp_path):
    config, job, daemon, calls = setup(tmp_path, results=WRONG_LENGTH)

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.nothing_usable == [1], quiet.lines
    assert "download.start" not in tools_called(calls)


def test_an_empty_search_leaves_nothing_usable(tmp_path):
    config, job, daemon, calls = setup(tmp_path, results=[])

    summary, _ = pass_once(config, job, daemon, Reference(), calls)

    assert summary.nothing_usable == [1]


def test_a_connect_stall_rotates_to_the_next_peer_and_cancels_the_first(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(
        tmp_path,
        results=TWO_PEERS,
        download=f"queued:waiting_for_peer:0:|downloading:transferring:90000:{landed}",
    )
    landed.write_bytes(b"the one that arrived")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.local_path == str(landed)
    assert "peerB:Other\\Kiasmos - Blurred.flac" in record.tried
    assert "download.cancel" in tools_called(calls), "a stalled transfer must not hold a transfer slot"


def test_a_transfer_is_asked_about_on_the_passs_own_interval(tmp_path):
    """The server asks for thirty seconds between polls, and every budget is decided *on a poll*, so obeying
    that ask makes its own grid the resolution the budgets are kept to: a peer with nothing to queue behind
    would be held for the whole of it rather than rotated when its budget is up (2026-10-14)."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download="queued:waiting_for_peer:0:")
    clock = Clock()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.tick(seconds + 0.001)     # even a sleep asked for nothing costs a little time

    summary = acquire.run(config, job, daemon, Reference(), log=lambda line: None,
                          sleep=sleep, clock=clock)

    assert summary.dropped == [1]
    assert max(slept) <= acquire.POLL_INTERVAL, "no wait longer than one poll"
    assert max(slept) < 30, "the server's own thirty-second ask is not obeyed"
    assert sum(slept) == pytest.approx(acquire.QUEUE_BUDGET, abs=0.05), \
        "the waits add up to the budget, not to a multiple of the poll"
    assert tools_called(calls).count("download.list") == len(slept)


def test_a_finished_transfer_whose_file_never_appears_is_given_its_grace(tmp_path):
    """`FINISHED_GRACE` is shorter than the ask above, so a pass that obeyed it looked fifteen seconds late the
    first time and gave up on the rename before the grace had begun."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD, download="finished::0:")
    clock = Clock()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.tick(seconds + 0.001)

    summary = acquire.run(config, job, daemon, Reference(), log=lambda line: None,
                          sleep=sleep, clock=clock)

    assert summary.dropped == [1]
    assert sum(slept) == pytest.approx(acquire.FINISHED_GRACE, abs=0.01)
    assert tools_called(calls).count("download.list") == 2, "asked again, then given up on"


def test_every_candidate_failing_closes_the_track(tmp_path):
    """2026-10-14: nothing left to try is the end of it. The track is settled rather than left waiting for
    somebody to say so, which is what a run full of unanswerable rows cost."""
    config, job, daemon, calls = setup(tmp_path, results=TWO_PEERS, download="failed::0:")
    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.dropped == [1], quiet.lines
    assert summary.failed == []
    record = job.records()[0]
    assert record.status == state.NOT_FOUND
    assert record.decision == state.DECLINED
    assert all(one.decided() for one in job.records()), "nothing is left to try, so nothing is left open"


def test_a_track_with_a_candidate_left_untried_is_left_alone(tmp_path):
    """The other half of that: an offer nobody got to — because the pass stopped at its candidate ceiling, or
    the network went quiet — is not the same as one every peer refused."""
    config, job, _daemon, _calls = setup(tmp_path, results=[])
    first = Candidate(user="a", path=r"M\Kiasmos - Blurred.flac", duration=449)
    second = Candidate(user="b", path=r"M\Kiasmos - Blurred.flac", duration=449, bitrate=320)
    records = job.records()
    records[0].status = state.RETRY_LATER
    records[0].candidates = [first, second]
    records[0].tried = [acquire.identifier(first)]
    job.write_records(records)

    assert acquire.drop_exhausted(job, log=lambda line: None) == []
    kept = job.records()[0]
    assert kept.status == state.RETRY_LATER
    assert kept.decision == state.UNDECIDED

    records = job.records()
    records[0].tried = [acquire.identifier(first), acquire.identifier(second)]
    job.write_records(records)
    assert acquire.drop_exhausted(job, log=lambda line: None) == [1]
    assert job.records()[0].status == state.NOT_FOUND


def test_the_reference_is_resolved_once_and_only_once(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"x")
    lookup = Reference()

    pass_once(config, job, daemon, lookup, calls)
    pass_once(config, job, daemon, lookup, calls)

    assert len(lookup.asked) == 1, "a resumed run must not ask MusicBrainz again"
    assert job.records()[0].duration_s == 449


def test_a_track_already_downloaded_is_not_fetched_again(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"x")

    pass_once(config, job, daemon, Reference(), calls)
    second, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert tools_called(calls).count("download.start") == 1
    assert second.already_downloaded == [1], quiet.lines
    assert job.records()[0].local_path == str(landed)


def test_a_track_this_run_already_placed_and_tagged_is_not_searched_again(tmp_path):
    """One status further on. Until the import runs the collection still points at the lossy copy
    being replaced, so the dedupe alone finds a finished track "not held" and re-downloads it, resetting the
    status — which is how a retry pass left 43 placed and tagged records unimportable on 2026-10-12."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    placed = config.lossless / "Kiasmos - Blurred.aiff"
    placed.parent.mkdir(parents=True, exist_ok=True)
    placed.write_bytes(b"audio")
    record = job.records()[0]
    record.status = state.TAGGED
    record.local_path = str(placed)
    job.write_records([record])

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.already_downloaded == [1], quiet.lines
    assert tools_called(calls) == [], "not even a search: the file it produced is right there"
    assert job.records()[0].status == state.TAGGED, "and its status is left where it was"


def test_a_later_pass_prefers_peers_with_free_slots(tmp_path):
    """Peer attributes may scope a retry pass, and never the first one: the filter is applied to what came
    back, and only when it leaves something to take."""
    busy = [{"username": "busy", "filename": "M\\busy.flac", "filesize": 10, "has_free_slots": False}]
    free = [{"username": "free", "filename": "M\\free.flac", "filesize": 10, "has_free_slots": True}]
    config, job, daemon, calls = setup(tmp_path, results=busy + free)
    record = job.records()[0]

    first = acquire.search(daemon, record, later_pass=False)
    second = acquire.search(daemon, record, later_pass=True)

    assert {candidate.user for candidate in first} == {"busy", "free"}
    assert [candidate.user for candidate in second] == ["free"]


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
        [lanes.Held("Kiasmos", "Blurred", 449, path=str(tmp_path / "Losy Tracks" / "Kiasmos - Blurred.mp3"))],
        [],
    )


def test_a_lossy_copy_held_is_an_upgrade_not_a_skip(tmp_path):
    """A lossy copy does not make a track held, when a lossless one can be had."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the lossless one")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls, shelf=held_lossy(tmp_path))

    record = job.records()[0]
    assert summary.downloaded == [1], quiet.lines
    assert record.replaces.endswith("Kiasmos - Blurred.mp3")
    assert "upgrades" in record.notes


def test_the_copy_in_the_collection_is_the_reference_when_musicbrainz_has_none(tmp_path):
    """This is not a bare tracklist, so the reference is not looked up: four of five lossy tracks in the
    collection have no MusicBrainz recording at all, and the copy in hand is the same track by definition."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the lossless one")

    summary, quiet = pass_once(config, job, daemon, Nothing(), calls, shelf=held_lossy(tmp_path))

    assert summary.downloaded == [1], quiet.lines
    assert job.records()[0].duration_s == 449, "the held file's own length"


def test_a_lossy_candidate_is_not_an_upgrade(tmp_path):
    """A lossy copy is not better than the lossy copy already held, so it is not an upgrade — and since
    2026-10-12 it is not a question either. It used to land in review, where a queue of mp3s with no
    bitrate was offered to replace mp3s already held."""
    config, job, daemon, calls = setup(tmp_path, results=LOSSY_ONLY)

    summary, quiet = pass_once(config, job, daemon, Reference(), calls, shelf=held_lossy(tmp_path))

    assert summary.nothing_usable == [1], quiet.lines
    assert summary.in_review == [], "never asked about: the copy already held plays"
    assert "download.start" not in tools_called(calls)


def test_a_track_the_library_already_holds_is_not_even_searched(tmp_path):
    """Nothing is fetched that the collection holds. It is asked before the search, so an already-held
    track costs no request at all — which is exactly what the first smoke run showed it was missing."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library(
        [lanes.Held("Kiasmos", "Blurred", 449, path="/Library/Kiasmos - Blurred.aiff")], []
    )

    summary, quiet = pass_once(config, job, daemon, Reference(), calls, shelf=shelf)

    record = job.records()[0]
    assert summary.already_held == [1], quiet.lines
    assert record.status == state.FOUND_IN_LIBRARY
    assert record.local_path == ""
    assert tools_called(calls) == [], "an already-held track should not have been searched for"
    assert not job.finished(), "the import finishes a run, not this stage"


def test_a_track_already_in_the_collection_is_not_resolved_again(tmp_path):
    """The collection does not change under a pass, so the second pass has the first pass's answer. Asking
    again costs a MusicBrainz reference per held track per pass, which on a 305-track run was five minutes of
    the pass spent re-confirming what it already knew."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library([lanes.Held("Kiasmos", "Blurred", 449, path="/Library/Kiasmos.aiff")], [])
    lookup = Reference()

    first, _ = pass_once(config, job, daemon, lookup, calls, shelf=shelf)
    asked = len(lookup.asked)
    second, _ = pass_once(config, job, daemon, lookup, calls, shelf=shelf)

    assert first.already_held == second.already_held == [1]
    assert len(lookup.asked) == asked, "the second pass asks the reference nothing"
    assert tools_called(calls).count("search") == 0


def test_a_track_with_no_reference_length_is_held_by_its_name(tmp_path):
    """MusicBrainz has nothing for a white label, so there is no length to compare — that is not a mismatch.
    The name and the version are then the evidence there is, and a bare request takes only an original, so
    the collection's copy is the same track and fetching it again would duplicate the library."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library([lanes.Held("Kiasmos", "Blurred", 449, path="/Library/Kiasmos.aiff")], [])

    summary, quiet = pass_once(config, job, daemon, Nothing(), calls, shelf=shelf)

    assert summary.already_held == [1], quiet.lines
    assert job.records()[0].status == state.FOUND_IN_LIBRARY
    assert tools_called(calls) == [], "no reference, no search, and no second copy"


def test_a_named_version_held_is_not_the_original_asked_for(tmp_path):
    """A bare request means the original. The collection holding the extended mix is not the track asked
    for, and with no length to say so the version is what says it."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library([lanes.Held("Kiasmos", "Blurred", 449, mix="Extended Mix")], [])

    summary, quiet = pass_once(config, job, daemon, Nothing(), calls, shelf=shelf)

    assert summary.already_held == []
    assert "search" in tools_called(calls)


def test_a_track_the_library_holds_at_another_length_is_still_fetched(tmp_path):
    config, job, daemon, calls = setup(tmp_path, results=GOOD)
    shelf = library.Library([lanes.Held("Kiasmos", "Blurred", 520)], [])

    summary, quiet = pass_once(config, job, daemon, Reference(), calls, shelf=shelf)

    assert summary.already_held == []
    assert "search" in tools_called(calls)


def test_a_daemon_that_refuses_is_left_to_retry_rather_than_crashing(tmp_path):
    class Refusing(Direct):
        def search(self, query, *, timeout):
            raise slsk.DaemonError("search", "the daemon is not logged in", 3)

    config, job, _daemon, calls = setup(tmp_path, results=[], tracks=1)
    daemon = Refusing(results=[])

    quiet = Quiet(Clock())
    summary = acquire.run(config, job, daemon, Reference(), log=quiet.log, sleep=quiet.sleep,
                          clock=quiet.clock)

    assert summary.failed == [1]
    assert job.records()[0].status == state.RETRY_LATER
    assert any("refused" in line for line in quiet.lines)


# ── the peers that never deliver (2026-10-16) ────────────────────────────────


# Three files, three folders, one track: the identifiers differ, so all three are asked for, and the
# basename is the same, so the rule judges all three Ready.
THREE_FROM_ONE_MIRROR = [
    {
        "id": f"mirror:Vol{index}\\Kiasmos - Blurred.flac",
        "username": "mirror",
        "filename": f"Vol{index}\\Kiasmos - Blurred.flac",
        "filesize": 82_000_000,
        "extension": "flac",
        "bitrate": 855,
        "duration_sec": 449,
        "queue_size": 0,
    }
    for index in range(3)
]


def test_a_mirror_that_never_delivers_is_ignored(tmp_path):
    """A peer that advertises a file, accepts the transfer and hands over nothing is what a paid mirror looks
    like, and three of those inside a fortnight is the count the list runs on."""
    config, job, daemon, calls = setup(tmp_path, results=THREE_FROM_ONE_MIRROR,
                                       download="not_found::0:|not_found::0:|not_found::0:",
                                       soulseek="maxretry = 3\n")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.ignored == {"mirror"}
    assert peers.Ledger.at(config).ignored() == {"mirror"}
    assert any("mirror ignored" in line for line in quiet.lines), quiet.lines


def test_a_peer_banned_mid_pass_is_not_asked_for_the_rest_of_its_files(tmp_path):
    """One mirror offered three files of one track. The first that delivers nothing bans it, and the other
    two are the same peer answering the same question — asking costs a connect budget each."""
    config, job, daemon, calls = setup(tmp_path, results=THREE_FROM_ONE_MIRROR,
                                       download="not_found::0:", soulseek="maxretry = 1\n")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.ignored == {"mirror"}
    assert tools_called(calls).count("download.start") == 1
    assert len(job.records()[0].tried) == 1
    assert any("left from ignored" in line for line in quiet.lines), quiet.lines


def test_a_file_from_an_ignored_peer_never_enters_the_record(tmp_path):
    """Dropped at the search, not at the ladder: nothing downstream — the record, the queue — should be able
    to offer a peer the account has given up on."""
    config, job, daemon, calls = setup(tmp_path, results=GOOD, soulseek="maxretry = 1\n")
    peers.Ledger.at(config).offence("peerA", "nothing arrived")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert job.records()[0].candidates == []
    assert summary.nothing_usable == [1], quiet.lines
    assert "download.start" not in tools_called(calls)


def test_a_peer_that_hands_a_file_over_is_forgiven(tmp_path):
    """The list is for peers that never deliver, so one that does has answered the question."""
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, calls = setup(tmp_path, results=GOOD,
                                       download=f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, already there when we look")
    ledger = peers.Ledger.at(config)
    ledger.offence("peerA", "not_found")
    ledger.offence("peerA", "not_found")

    summary, quiet = pass_once(config, job, daemon, Reference(), calls)

    assert summary.downloaded == [1], quiet.lines
    assert peers.Ledger.at(config).peers() == []


# ── how much of it runs at once (2026-10-12) ─────────────────────────────────────────────────────


def test_the_pass_works_on_several_tracks_at_once(tmp_path):
    """One at a time is nonsense. Two tracks have to be waiting on their own downloads at the
    same moment, which a pass that works through them in turn can never do — it would sit in its first
    poll until the barrier gave up."""
    import threading

    config, job, daemon, calls = setup(tmp_path, results=GOOD + [{
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

    summary = acquire.run(config, job, daemon, AnswersAnyTitle(), log=lambda line: None,
                          sleep=sleep, clock=clock)

    settled = (summary.downloaded + summary.already_downloaded + summary.already_held
               + summary.in_review + summary.nothing_usable + summary.failed)
    assert sorted(settled) == [1, 2]
    assert len(job.records()) == 2


# ── a daemon that stops answering ────────────────────────────────────────


def test_an_empty_answer_is_not_a_failure():
    """A search that comes back with nothing is the network answering, and nothing else in the stack can tell
    it from a rare track. So no number of them stops anything."""
    watch = acquire.Watch()

    for _ in range(200):
        watch.answered()

    assert not watch.dead.is_set(), "a playlist's rare tail is not a dead network"


def test_the_daemons_session_loss_stops_the_pass(tmp_path):
    """The daemon records a session the server took away. From inside a search that is indistinguishable from
    a rare track — every answer after it is an ordinary success with nothing in it — so the pass reads the
    daemon's own status and stops, rather than spending three hundred searches learning nothing."""
    config, job, daemon, calls = setup(tmp_path, results=[], tracks=6, session_loss=True)

    with pytest.raises(acquire.ServerStuck) as raised:
        pass_once(config, job, daemon, Reference(), calls)

    assert raised.value.lost_session is True
    assert "session" in str(raised.value)
    events = (config.state_root / job.id / "events.jsonl").read_text(encoding="utf-8")
    assert "lost_session" in events


def test_a_session_loss_that_is_not_reported_is_read_from_the_status():
    """The watch asks the daemon rather than waiting for an empty answer to mean something: `answered()` is
    the one place a lost session becomes visible."""
    daemon = Direct(results=[], session_loss=True)
    watch = acquire.Watch(daemon=daemon)

    watch.answered()

    assert watch.session_lost is True
    assert watch.dead.is_set()


def test_one_candidate_may_not_own_the_pass():
    """Every other budget is reset by a byte, so a peer slow enough never trips one: 0.107 MB/s sustained on
    a 99 MB file held a whole pass open for 764 of its 1,772 seconds, with nothing else able to run."""
    moving = acquire.Transfer(status="in_progress")

    assert acquire.move(moving, on_disk=False, stuck_for=0.5,
                        elapsed=acquire.TRANSFER_CEILING - 1) is acquire.Move.WAIT
    assert acquire.move(moving, on_disk=False, stuck_for=0.5,
                        elapsed=acquire.TRANSFER_CEILING + 1) is acquire.Move.ROTATE


def test_a_search_that_answers_resets_the_run_of_timeouts():
    """The run is *in a row*: a child that answers between two timeouts is talking, so nothing is stuck yet."""
    watch = acquire.Watch(stuck_after=3)
    for _ in range(2):
        watch.timed_out()
    watch.answered()
    for _ in range(2):
        watch.timed_out()

    assert not watch.dead.is_set()
    assert watch.hung == 0


def test_a_silent_network_is_still_searched_to_the_end(tmp_path):
    """2026-10-12: a pass spent 146 searches in a row against a silent network and stopped there, then wrote
    205 tracks down as *nothing usable* — a fact about the network recorded as a fact about the tracks — and
    left the rest unnamed. An empty answer is the network's answer about that one track and nothing more, so
    the pass asks about every track and says which of them turned up nothing."""
    total = 60
    config, job, daemon, calls = setup(tmp_path, results=[], tracks=total)

    summary, _ = pass_once(config, job, daemon, Reference(), calls)

    searched = tools_called(calls).count("search")
    assert searched == total, f"{total} tracks and {searched} searches"
    assert len(summary.unsearched) == 0, "no track is left unnamed"
    assert len(summary.nothing_usable) == total, "every track was asked about, and every one answered nothing"
    events = (config.state_root / job.id / "events.jsonl").read_text(encoding="utf-8")
    assert "network_quiet" not in events


def test_a_dead_daemon_does_not_race_the_bar_through_the_rest(tmp_path):
    """The bar counts the tracks the pass got through. When the daemon is gone the others were never searched,
    and settling them one at a time raced the count from 28 to 296 in a blink — which reads as a pass that
    finished the list, and leaves a captured stream with one line per track that was never asked about."""
    import io

    config, job, daemon, calls = setup(tmp_path, results=[], tracks=5, session_loss=True)
    lines, printed = [], io.StringIO()

    with pytest.raises(acquire.ServerStuck) as stuck:
        acquire.run(config, job, daemon, Reference(), library=None, log=lines.append, stream=printed)

    assert "5 tracks were never searched" in str(stuck.value), "the pass says it once, where it ends"
    assert printed.getvalue() == "", "a track nobody searched is not a track the pass settled"


def test_a_pass_starts_with_the_tracks_no_pass_has_asked_about(tmp_path):
    """2026-10-16: five passes over 315 tracks, between 20:54 and 22:28, searched the first 91 records and never
    reached the other 224 — every pass began at track one and every child died inside the first hundred. The
    tracks nobody has asked about go first, so each attempt makes progress on the tail instead."""
    config, job, daemon, calls = setup(tmp_path, results=[], tracks=6, soulseek="workers = 1\n")
    for idx in (1, 2, 3):
        job.append_event(state.SEARCHED_EVENT, idx=idx, added=0, offered=0)
    before = len(job.events())

    pass_once(config, job, daemon, Reference(), calls)

    asked = [event["idx"] for event in job.events()[before:]
             if event["event"] == state.SEARCHED_EVENT]
    assert asked == [4, 5, 6, 1, 2, 3], "the unasked tracks first, the ones already asked about after them"


def test_the_file_keeps_its_order_while_the_pass_works_from_the_tail(tmp_path):
    """The unasked-first order belongs to the pass, not to the file. Writing the schedule back reorders
    `tracks.jsonl` on every pass — which changes the order `review` prints its queue in and makes a run file
    that a person opens read out of track order, for no gain."""
    config, job, daemon, calls = setup(tmp_path, results=[], tracks=4, soulseek="workers = 1\n")
    for idx in (1, 2):
        job.append_event(state.SEARCHED_EVENT, idx=idx, added=0, offered=0)

    pass_once(config, job, daemon, Reference(), calls)

    assert [record.idx for record in job.records()] == [1, 2, 3, 4]


# ── the pace ─────────────────────────────────────────────────────────────


def test_a_search_in_flight_does_not_hold_the_next_one_back():
    """Twenty searches run at once, their starts one `search_interval` apart: the gap is between starts, not a
    limit on how many are running. `wait` gives the lock up before it sleeps, so a search still in flight — the
    call can take ten seconds to come back — does not hold the next one back. A lock held across the sleep
    would make the whole pass serial, and nothing would say so."""
    holding = threading.Event()
    naps = []

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 1:
            holding.set()
            time.sleep(0.2)         # the sleeper is inside its nap, with the next slot already booked

    pacer = acquire.Pacer(1.0, clock=time.monotonic, sleep=sleep)
    pacer.wait()                    # the first search starts at once and sleeps for nothing
    napper = threading.Thread(target=pacer.wait)
    napper.start()
    assert holding.wait(2.0), "the second search is waiting its turn"

    began = time.monotonic()
    pacer.wait()
    went = time.monotonic() - began
    napper.join()

    assert went < 0.05, f"the third search waited {went:.2f}s behind a sleeper: the pace is a queue"


def test_searches_are_spaced_out_rather_than_fired_as_fast_as_the_loop_can(tmp_path):
    """A pass asked for 330 searches in six minutes and everything after the first seconds came
    back empty — no error, no warning, and nothing in the daemon that keeps to a rate or
    reports one being refused. So the gap between two searches is the pass's own to keep.

    One worker, so the reservations run in a known order and the gaps are exact; a pace that only held
    with the parallelism turned off would be no pace at all, which is what the busy-wait below checks.
    """
    from dataclasses import replace

    config, job, daemon, calls = setup(tmp_path, results=[], tracks=4)
    config = replace(config, search_interval=3.0, workers=1)
    clock = Clock()
    gaps: list[float] = []

    def sleep(seconds: float) -> None:
        gaps.append(seconds)
        clock.tick(seconds)

    acquire.run(config, job, daemon, Reference(), log=lambda line: None, sleep=sleep, clock=clock)

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


def test_a_reconnecting_daemon_is_not_a_dead_network():
    """The daemon reports its server link down and logs back in on its own. A search that cannot be forwarded
    times out exactly like a dead network, and four of those were ending passes while the account was fine."""
    daemon = Direct(results=[])
    daemon.session_loss = "disconnected"
    watch = acquire.Watch(daemon=daemon, stuck_after=4)

    for _ in range(watch.RECONNECTS + 3):
        watch.timed_out()

    assert not watch.dead.is_set()
    assert watch.hung == 0


def test_a_daemon_that_stays_disconnected_still_stops_the_pass():
    """Grace is bounded: a daemon that never comes back is a dead pass, not an open one."""
    daemon = Direct(results=[])
    daemon.session_loss = "disconnected"
    watch = acquire.Watch(daemon=daemon, stuck_after=4)

    for _ in range(watch.RECONNECTS + 4):
        watch.timed_out()

    assert watch.dead.is_set()
    assert watch.hung >= 4
