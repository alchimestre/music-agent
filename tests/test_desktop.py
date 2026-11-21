from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QPushButton  # noqa: E402

from music_agent import state  # noqa: E402
from music_agent.config import DEFAULTS  # noqa: E402
from music_agent.desktop import AUTO_FIRST_DELAY, QUEUE_ROLES, DesktopWindow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(DesktopWindow, "_start_worker", lambda self: None)
    window = DesktopWindow()
    window._auto_timer.stop()
    window._config = object()
    window._busy = False
    return window


def queue_row(job_id, status=state.PENDING):
    return SimpleNamespace(job_id=job_id, idx=1, status=status, label=job_id, decision="")


def test_each_job_gets_a_turn_and_processing_resets_the_backoff(window):
    window._set_queue([queue_row("first"), queue_row("second", state.DOWNLOADED)])
    passes = []
    window.command_requested.connect(lambda request: passes.append(request[1]["job_id"]))

    window._auto_advance()
    window._auto_after(failed=False)
    assert window._auto_delay > AUTO_FIRST_DELAY

    window._busy = False
    window._auto_next = 0
    window._auto_advance()
    assert passes == ["first", "second"]

    window._set_job_queue(("second", [queue_row("second", state.TAGGED)]))
    window._auto_after(failed=False)
    assert window._auto_delay == AUTO_FIRST_DELAY


def test_live_progress_and_selection_follow_the_track_through_filtering(window):
    stage_role = next(role for role, name in QUEUE_ROLES.items() if name == b"stage")
    readout_role = next(role for role, name in QUEUE_ROLES.items() if name == b"readout")
    window._set_queue([queue_row("first"), queue_row("second")])
    window.queue_root.setProperty("selectedIndex", 1)
    window._set_job_queue(("second", [queue_row("second", state.DOWNLOADED)]))
    window._on_progress(SimpleNamespace(job_id="first", idx=1, fraction=0.5))

    assert window.queue_model.item(0).data(stage_role) == "Downloading"
    assert window.queue_model.item(0).data(readout_role) == "15%"
    assert window.queue_model.item(1).data(stage_role) == "Downloaded"
    window.queue_filter.setCurrentText("Active")
    window._set_job_queue(("first", [queue_row("first", state.TAGGED)]))

    assert window.queue_model.rowCount() == 1
    assert window.queue_model.item(0).data(readout_role) == "25%"
    assert window._selected_key() == ("second", 1)


def test_saving_the_form_does_not_enable_default_shares_over_explicit_empty_shares(window, tmp_path):
    window._config = replace(DEFAULTS, shared_dirs=(), staging=tmp_path / "staging",
                             lossless=tmp_path / "lossless", lossy=tmp_path / "lossy")
    window._set_settings(window._config)

    assert window._build_config().shared_dirs == ()


def test_approval_delivers_the_previewed_job_without_starting_an_auto_pass(window, monkeypatch):
    window._set_queue([queue_row("first"), queue_row("second")])
    window.queue_root.setProperty("selectedIndex", 0)
    commands = []
    window.command_requested.connect(commands.append)
    plan = SimpleNamespace(target="rekordbox", destination="database", changes=1,
                           requires_confirmation=False, error=None)

    def interact(dialog):
        window.queue_root.setProperty("selectedIndex", 1)
        window._auto_advance()
        for button in dialog.findChildren(QPushButton):
            if button.text() == "Deliver configured targets":
                button.click()

    monkeypatch.setattr(QDialog, "exec", interact)
    window._show_delivery_plans("first", [plan])
    assert commands == [("deliver", {"job_id": "first"})]
    assert not window._plan_open


def test_a_blocked_plan_leaves_the_authorization_unchecked(window, monkeypatch):
    blocked = SimpleNamespace(target="traktor", destination="collection", changes=0,
                              requires_confirmation=False, error="collection missing", blocked=True)
    ready = SimpleNamespace(target="rekordbox", destination="database", changes=1,
                            requires_confirmation=False, error=None)
    states = []

    def interact(dialog):
        states.extend(box.isChecked() for box in dialog.findChildren(QCheckBox))
        dialog.reject()

    monkeypatch.setattr(QDialog, "exec", interact)
    window._show_delivery_plans("first", [blocked, ready])
    assert states == [False]


def test_preview_distinguishes_the_work_a_playlist_links_to_itself(window):
    records = [SimpleNamespace(idx=1, label="A"), SimpleNamespace(idx=2, label="A")]
    first = SimpleNamespace(idx=1, work_id="work-1", existing=False, created=True)
    repeated = SimpleNamespace(idx=2, work_id="work-1", existing=False, created=False)
    preview = SimpleNamespace(name="playlist", source="deezer", records=records,
                              matches=[first, repeated], held={})

    window._show_preview(preview)

    summary = window.preview_summary.text()
    assert "New work accepted: 1" in summary
    assert "linked to existing work: 0" in summary
    assert "linked within this submission: 1" in summary
    assert "admission not reported: 0" in summary


def test_a_waiting_track_states_its_label_and_what_happens_next(window):
    row = queue_row("first")
    row.queue_reason = "nothing_usable"

    assert window._stage_text(row) == "No usable offers"
    assert "search again" in window._queue_next_step(row, ("first", 1))
    assert window._stage_text(queue_row("second")) == "Searching"
    assert window._queue_next_step(queue_row("second"), ("second", 1)) == ""
