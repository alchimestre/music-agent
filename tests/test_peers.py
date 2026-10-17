"""The ignore list: what earns a place on it, and what takes one off."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from music_agent import peers
from music_agent.peers import Ledger

# One fixed moment, so every window in these tests is arithmetic rather than a race with the clock.
START = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def at(days: float = 0.0) -> datetime:
    return START + timedelta(days=days)


def test_three_failures_inside_the_window_earn_an_ignore(tmp_path):
    ledger = Ledger(tmp_path / "peers.json", maxretry=3, findtime_days=14, bantime_days=30)

    assert ledger.offence("mirror", "nothing arrived", now=at()) is False
    assert ledger.offence("mirror", "nothing arrived", now=at(1)) is False
    assert ledger.offence("mirror", "nothing arrived", now=at(2)) is True

    assert ledger.ignores("mirror", now=at(2)) is True
    assert ledger.ignores("mirror", now=at(33)) is False, "the ignore is a timestamp, not a job to run"


def test_failures_outside_the_window_are_forgotten(tmp_path):
    """A peer that failed twice a month apart has not shown a pattern, so it is never one failure from a ban."""
    ledger = Ledger(tmp_path / "peers.json", maxretry=3, findtime_days=14, bantime_days=30)

    ledger.offence("slow", "nothing arrived", now=at())
    ledger.offence("slow", "nothing arrived", now=at(20))
    ledger.offence("slow", "nothing arrived", now=at(40))

    assert ledger.ignores("slow", now=at(40)) is False
    assert [peer.failures for peer in ledger.peers(now=at(40))] == [1]


def test_a_delivery_clears_the_record(tmp_path):
    """A peer that has just handed a file over is not a mirror, whatever it did before."""
    ledger = Ledger(tmp_path / "peers.json", maxretry=3)
    ledger.offence("flaky", "nothing arrived", now=at())
    ledger.offence("flaky", "nothing arrived", now=at())

    ledger.delivered("flaky")

    assert ledger.peers() == []


def test_the_list_survives_the_pass_that_wrote_it(tmp_path):
    """A pass is killed often enough; the list belongs to the account rather than to the run."""
    path = tmp_path / "peers.json"
    first = Ledger(path, maxretry=1, bantime_days=30)
    first.offence("mirror", "not_found", now=at())

    second = Ledger(path)

    assert second.ignores("mirror", now=at(1)) is True
    assert [(peer.user, peer.why) for peer in second.peers(now=at(1))] == [("mirror", "not_found")]


def test_unban_forgets_the_peer_and_its_failures(tmp_path):
    ledger = Ledger(tmp_path / "peers.json", maxretry=1, bantime_days=30)
    ledger.offence("mirror", "nothing arrived", now=at())

    assert ledger.unban("mirror") is True
    assert ledger.unban("mirror") is False
    assert ledger.peers() == [], "a peer asked for again starts from nothing, not one failure from a ban"


def test_ignored_lists_the_peers_under_a_ban(tmp_path):
    ledger = Ledger(tmp_path / "peers.json", maxretry=1, bantime_days=30)
    ledger.offence("mirror", "nothing arrived", now=at())

    assert ledger.ignored(now=at(1)) == {"mirror"}
    assert ledger.ignored(now=at(31)) == set()


def test_an_unreadable_ledger_is_an_error_rather_than_a_clean_slate(tmp_path):
    """Starting from empty would hand every junk peer a fresh account of itself."""
    path = tmp_path / "peers.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(peers.Unreadable):
        Ledger(path)


def test_a_ledger_whose_root_is_not_an_object_is_unreadable(tmp_path):
    """`[].get` raised AttributeError out of `Ledger()` instead of the one-line error the CLI prints."""
    path = tmp_path / "peers.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(peers.Unreadable):
        Ledger(path, maxretry=3, findtime_days=14, bantime_days=30)
