"""The queue's progress cell.

The widget exists because a bare QProgressBar reads as an empty cell at 0% under the Windows style,
so what matters here is that the number is always in the cell and always fits.
"""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QPushButton  # noqa: E402

from music_agent import state  # noqa: E402
from music_agent.config import DEFAULTS  # noqa: E402
from music_agent.desktop import AUTO_FIRST_DELAY, DesktopWindow, ProgressCell  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_the_cell_shows_the_percentage_and_the_bar_value(qt_app):
    cell = ProgressCell("Kiasmos - Blurred", 45, "45%")

    assert cell.readout.text() == "45%"
    assert cell.bar.value() == 45


def test_a_settled_row_replaces_the_number_with_its_word(qt_app):
    cell = ProgressCell("Kiasmos - Blurred", 100, "done")

    cell.set_readout(100, "stopped")

    assert cell.readout.text() == "stopped"
    assert cell.bar.value() == 100


def test_the_readout_fits_a_full_percentage_in_the_current_font(qt_app):
    cell = ProgressCell("Kiasmos - Blurred", 100, "100%")

    assert cell.readout.minimumWidth() >= cell.readout.fontMetrics().horizontalAdvance("100%")


def test_the_cell_names_itself_for_a_screen_reader(qt_app):
    cell = ProgressCell("Kiasmos - Blurred", 0, "0%")

    assert cell.bar.accessibleName() == "Progress for Kiasmos - Blurred"


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


def test_a_row_change_keeps_other_progress_widgets_and_tracks_progress_by_key(window):
    window._set_queue([queue_row("first"), queue_row("second")])
    first_bar = window._queue_bars[("first", 1)]
    window._set_job_queue(("second", [queue_row("second", state.DOWNLOADED)]))

    assert window._queue_bars[("first", 1)] is first_bar
    assert window.queue_table.item(1, 2).text() == "Downloaded"
    window._on_progress(SimpleNamespace(job_id="first", idx=1, fraction=0.5))
    assert first_bar.bar.value() > 0
    assert window._queue_bars[("second", 1)].readout.text() == "25%"
    window.queue_filter.setCurrentText("Active")
    assert window.queue_table.rowCount() == 2

    window._set_job_queue(("first", [queue_row("first", state.TAGGED)]))

    assert window.queue_table.rowCount() == 1
    assert window.queue_table.item(0, 0).text() == "second"
    assert window.queue_table.item(0, 2).text() == "Downloaded"


def test_saving_the_form_does_not_enable_default_shares_over_explicit_empty_shares(window, tmp_path):
    window._config = replace(DEFAULTS, shared_dirs=(), staging=tmp_path / "staging",
                             lossless=tmp_path / "lossless", lossy=tmp_path / "lossy")
    window._set_settings(window._config)

    assert window._build_config().shared_dirs == ()


def test_approval_delivers_the_previewed_job_without_starting_an_auto_pass(window, monkeypatch):
    window._set_queue([queue_row("first"), queue_row("second")])
    window.queue_table.selectRow(0)
    commands = []
    window.command_requested.connect(commands.append)
    plan = SimpleNamespace(target="rekordbox", destination="database", changes=1,
                           requires_confirmation=False, error=None)

    def interact(dialog):
        window.queue_table.selectRow(1)
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
