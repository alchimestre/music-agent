"""The review command's own module: the decisions a person makes, and nothing else.

Two axes are kept apart, so the tests are too — what a candidate *is* is the rules' business, and what the
user *did* about a track is the only thing written here. The download an acceptance needs is exercised
end to end against the stand-in server, with no Soulseek and no waiting.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import review, state
from music_agent.mcp import StdioServer
from music_agent.rules import Candidate

FAKE = str(Path(__file__).parent / "fake_server.py")

# Two files, both plausible and neither verifiable: the length is unknown, so the rule will not choose.
TWO_UNVERIFIABLE = [
    {
        "id": "mole:Music\\Ntogn - Mines.aiff",
        "username": "mole",
        "filename": "Music\\Ntogn - Mines.aiff",
        "extension": "aiff",
        "duration_sec": None,
        "queue_size": 0,
    },
    {
        "id": "crate:Other\\Ntogn - Mines.flac",
        "username": "crate",
        "filename": "Other\\Ntogn - Mines.flac",
        "extension": "flac",
        "duration_sec": None,
        "queue_size": 2,
    },
]
# One offer, and it is the wrong length — so it is not offered at all.
WRONG_LENGTH = [
    {
        "id": "mole:Music\\Ntogn - Mines (Extended Mix).aiff",
        "username": "mole",
        "filename": "Music\\Ntogn - Mines (Extended Mix).aiff",
        "extension": "aiff",
        "duration_sec": 700,
        "queue_size": 0,
    }
]


def a_waiting_track(idx: int = 2) -> state.Record:
    """A track the rule will not settle: the mix matches and no length can be verified."""
    return state.Record(idx=idx, artist="Ntogn", title="Mines", duration_s=528,
                        candidates=[Candidate(user="mole", path=r"M\Ntogn - Mines.aiff",
                                              duration=None)])


def setup(tmp_path: Path) -> tuple[config_module.Config, state.Job, list[str]]:
    config_path = tmp_path / "config.toml"
    staging = tmp_path / "staging"
    config_path.write_text(f'[paths]\nstaging = "{staging}"\n'
                           f'lossless = "{tmp_path / "Loseless Tracks"}"\n'
                           f'lossy = "{tmp_path / "Losy Tracks"}"\n'
                           'state = "state"\n'
                           "[soulseek]\nsearch_interval = 0.0\n", encoding="utf-8")
    staging.mkdir()
    config = config_module.load(config_path)
    job = state.create(config, "run")
    job.write_records([a_waiting_track()])
    return config, job, []


def server_for(tmp_path: Path, download: str) -> StdioServer:
    env = dict(os.environ)
    env["FAKE_RESULTS"] = json.dumps(TWO_UNVERIFIABLE)
    env["FAKE_DOWNLOAD"] = download
    env["FAKE_LOG"] = str(tmp_path / "calls.jsonl")
    return StdioServer([sys.executable, FAKE, "normal"], env=env)


def factory(server: StdioServer):
    """A `server_factory` that hands over the one stand-in, as the CLI's lambda does the real one."""
    return lambda: server


# ── choosing, without any network at all ─────────────────────────────────────


def test_one_candidate_on_offer_needs_no_number():
    verdict = review.choose(a_waiting_track())
    assert verdict.candidate.user == "mole"


def test_several_candidates_must_be_chosen_between():
    """A person picking is the whole point of the command, so it will not guess."""
    record = a_waiting_track()
    record.candidates.append(Candidate(user="crate", path=r"O\Ntogn - Mines.flac", duration=None))

    with pytest.raises(review.NoSuchCandidate, match="say which one"):
        review.choose(record)
    assert review.choose(record, 2).candidate.user == "crate"


def test_a_track_with_nothing_on_offer_cannot_be_accepted():
    record = state.Record(idx=3, artist="Ntogn", title="Mines", duration_s=528,
                          candidates=[Candidate(user="mole", path=r"M\Ntogn - Mines.aiff",
                                                duration=700)])
    with pytest.raises(review.NoSuchCandidate, match="no candidate on offer"):
        review.choose(record)


def test_a_number_outside_the_offers_is_refused():
    with pytest.raises(review.NoSuchCandidate, match="so 2 is not one of them"):
        review.choose(a_waiting_track(), 2)


def test_a_track_that_is_not_in_the_run_is_refused(tmp_path):
    config, job, _ = setup(tmp_path)
    with pytest.raises(review.NotInTheQueue, match="not in this run"):
        review.skip(job, 99)


# ── the decisions that need nothing fetched ──────────────────────────────────


def test_skipping_writes_the_decision_and_nothing_else(tmp_path):
    config, job, _ = setup(tmp_path)

    settled = review.skip(job, 2)

    assert settled.decision == state.SKIPPED
    record = job.records()[0]
    assert record.decision == state.SKIPPED
    assert record.status == state.PENDING, "passed over for now, not settled"
    assert job.events()[-1]["event"] == "skipped"


def test_not_found_settles_the_track_for_good(tmp_path):
    config, job, _ = setup(tmp_path)

    review.not_found(job, 2)

    record = job.records()[0]
    assert record.decision == state.DECLINED
    assert record.status == state.NOT_FOUND
    assert "no usable copy exists" in record.notes


# ── taking a candidate ───────────────────────────────────────────────────────


def test_accepting_fetches_the_candidate_and_hands_it_to_process(tmp_path):
    config, job, _ = setup(tmp_path)
    landed = tmp_path / "staging" / "Ntogn - Mines.aiff"
    server = server_for(tmp_path, f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the file, there when we look")

    with server:
        settled = review.accept(job, 2, server_factory=factory(server))

    assert settled.decision == state.ACCEPTED
    assert settled.from_disk is False
    record = job.records()[0]
    assert record.status == state.DOWNLOADED
    assert record.decision == state.ACCEPTED
    assert record.local_path == str(landed)
    assert record.format == "aiff"
    assert record.tried == ["mole:M\\Ntogn - Mines.aiff"]
    # `reconcile` rebuilds a record from its own `downloaded` event, so an acceptance has to write one.
    assert [event["event"] for event in job.events()][-2:] == ["downloaded", "accepted"]


def test_the_candidate_he_pointed_at_is_the_one_fetched(tmp_path):
    config, job, _ = setup(tmp_path)
    records = job.records()
    records[0].candidates.append(Candidate(user="crate", path=r"O\Ntogn - Mines.flac", duration=None))
    job.write_records(records)
    landed = tmp_path / "staging" / "Ntogn - Mines.flac"
    server = server_for(tmp_path, f"downloading:transferring:90000:{landed}")
    landed.write_bytes(b"the second peer's copy")

    with server:
        settled = review.accept(job, 2, number=2, server_factory=factory(server))

    assert settled.candidate.user == "crate"
    assert job.records()[0].tried == ["crate:O\\Ntogn - Mines.flac"]


def test_a_file_already_in_staging_is_not_fetched_again(tmp_path):
    """A download from an earlier pass is not worth repeating, so the network is never asked."""
    config, job, _ = setup(tmp_path)
    here = tmp_path / "staging" / "Ntogn - Mines.aiff"
    here.write_bytes(b"already here")
    records = job.records()
    records[0].local_path = str(here)
    records[0].format = "aiff"
    job.write_records(records)

    def never():
        raise AssertionError("a server was opened for a file that is already on disk")

    settled = review.accept(job, 2, server_factory=never)

    assert settled.from_disk is True
    assert job.records()[0].decision == state.ACCEPTED
    assert job.records()[0].status == state.DOWNLOADED


def test_a_peer_that_does_not_hand_it_over_decides_nothing(tmp_path):
    config, job, _ = setup(tmp_path)
    server = server_for(tmp_path, "failed::0:")

    with server, pytest.raises(review.NotAccepted, match="did not hand the file over"):
        review.accept(job, 2, server_factory=factory(server))

    record = job.records()[0]
    assert record.decision == state.UNDECIDED, "nothing was decided, so nothing is recorded as decided"
    assert record.status == state.PENDING
    assert record.local_path == ""
    assert record.tried == ["mole:M\\Ntogn - Mines.aiff"], "and the peer is not asked again"


def test_accepting_without_a_server_says_so(tmp_path):
    config, job, _ = setup(tmp_path)
    with pytest.raises(review.NotAccepted, match="no server was given"):
        review.accept(job, 2)


def test_an_accepted_track_leaves_the_queue(tmp_path):
    config, job, _ = setup(tmp_path)
    here = tmp_path / "staging" / "Ntogn - Mines.aiff"
    here.write_bytes(b"already here")
    records = job.records()
    records[0].local_path = str(here)
    job.write_records(records)

    review.accept(job, 2, server_factory=lambda: pytest.fail("no server needed"))

    waiting = state.queue(job)
    assert waiting.needs_you == []
    assert [record.idx for record in waiting.decided] == [2]


def test_the_queue_is_split_by_why_a_track_is_in_it(tmp_path):
    config, job, _ = setup(tmp_path)
    job.write_records([a_waiting_track(),
                       state.Record(idx=1, artist="Sven Wunder", title="Lotus")])
    job.append_event("searched", idx=1, added=0, offered=0)     # asked, and the network held nothing

    waiting = state.queue(job)
    assert [record.idx for record in waiting.needs_you] == [2]
    assert [record.idx for record in waiting.nothing_offered] == [1]
    assert waiting.not_searched == []
