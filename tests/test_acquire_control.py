from __future__ import annotations

from threading import Event

from music_agent import acquire, state, store
from test_acquire import Reference, setup


def test_stop_event_leaves_unstarted_records_unchanged(tmp_path):
    config, job, daemon, _calls = setup(tmp_path, results=[], tracks=3, soulseek="workers = 1\n")
    stop_event = Event()
    original_search = daemon.search

    def stop_after_first_search(query, *, timeout):
        result = original_search(query, timeout=timeout)
        stop_event.set()
        return result

    daemon.search = stop_after_first_search
    summary = acquire.run(config, job, daemon, Reference(), stop_event=stop_event,
                          log=lambda _line: None, sleep=lambda _seconds: None)

    assert summary.unsearched == [2, 3]
    assert [event["idx"] for event in job.events()
            if event["event"] == state.SEARCHED_EVENT] == [1]
    assert len([name for name, _params in daemon.calls if name == "search"]) == 1


def test_pause_event_holds_new_search_until_cleared(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep

    config, job, daemon, _calls = setup(tmp_path, results=[])
    pause_event = Event()
    pause_event.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(acquire.run, config, job, daemon, Reference(),
                             pause_event=pause_event, log=lambda _line: None,
                             sleep=lambda _seconds: None)
        sleep(0.05)
        assert not future.done()
        assert not any(name == "search" for name, _params in daemon.calls)
        pause_event.clear()
        summary = future.result(timeout=2)

    assert summary.nothing_usable == [1]


def test_a_pass_under_the_callers_writer_lock_does_not_start_pool_workers(tmp_path, monkeypatch):
    config, job, daemon, _calls = setup(tmp_path, results=[], tracks=1)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a pass under the caller's writer lock must run in that thread")

    monkeypatch.setattr(acquire, "ThreadPoolExecutor", forbidden)
    with store.writer_lock(config.state_root):
        summary = acquire.run(config, job, daemon, Reference(),
                              log=lambda _line: None, sleep=lambda _seconds: None)

    assert summary.nothing_usable == [1]


def test_stop_during_download_preserves_transfer_and_candidate_provenance(tmp_path):
    landed = tmp_path / "staging" / "Kiasmos - Blurred.flac"
    config, job, daemon, _calls = setup(
        tmp_path, results=[{"username": "peer", "filename": "Music\\Kiasmos - Blurred.flac",
                           "filesize": 82_000_000, "extension": "flac", "bitrate": 855,
                           "duration_sec": 449, "queue_size": 0}],
        download=f"downloading:transferring:10:{landed}")
    daemon.directory = str(landed.parent)
    stop_event = Event()
    original_downloads = daemon.downloads

    def stop_after_transfer_snapshot():
        transfers = original_downloads()
        if any(name == "download.start" for name, _params in daemon.calls):
            stop_event.set()
        return transfers

    daemon.downloads = stop_after_transfer_snapshot
    rows = []
    summary = acquire.run(config, job, daemon, Reference(), stop_event=stop_event,
                          progress=rows.append, log=lambda _line: None,
                          sleep=lambda _seconds: None)
    record = job.records()[0]

    assert summary.interrupted == [1]
    assert record.tried
    assert not landed.exists()
    assert ("peer", "Music\\Kiasmos - Blurred.flac") in daemon.downloads_by_key
    assert not any(name == "download.cancel" for name, _params in daemon.calls)
    assert rows[0]["idx"] == 1
    assert rows[0]["peer"] == "peer"
    assert rows[0]["local_path"] == str(landed)
    assert rows[0]["received_bytes"] == 10
    assert rows[0]["total_bytes"] == 82_000_000
    started = [event for event in job.events() if event["event"] == "transfer_started"]
    assert started[-1]["idx"] == 1
    assert started[-1]["local_path"] == str(landed)
