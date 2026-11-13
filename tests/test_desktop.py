"""The queue's progress cell.

The widget exists because a bare QProgressBar reads as an empty cell at 0% under the Windows style,
so what matters here is that the number is always in the cell and always fits.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from music_agent.desktop import ProgressCell  # noqa: E402


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
