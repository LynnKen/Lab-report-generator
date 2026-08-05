"""Status/warning area behaviour.

The status area is the single authoritative place the app reports validation
problems and build progress, and it is rendered next to the generate button
at the end of the form (see app.StatusArea's docstring) so the user never has
to scroll back to the top of a long form to find out what went wrong.
"""

from __future__ import annotations

import flet as ft
import pytest

from app import StatusArea


@pytest.fixture
def status() -> StatusArea:
    return StatusArea()


def test_starts_empty_and_quiet(status):
    assert status.text.value == ""
    assert status.icon.visible is False
    assert status.spinner.visible is False


def test_every_state_has_a_distinct_colour(status):
    seen = {}
    for kind in ("info", "progress", "success", "warning", "error"):
        status.set(f"הודעה {kind}", kind)
        seen[kind] = status.text.color
    assert seen["error"] == ft.Colors.RED_700
    assert seen["warning"] == ft.Colors.ORANGE_800
    assert seen["success"] == ft.Colors.GREEN_800
    # A warning must never be indistinguishable from a hard error.
    assert seen["warning"] != seen["error"]
    assert seen["success"] != seen["error"]


def test_progress_shows_spinner_instead_of_icon(status):
    status.set("יוצר קובץ Word...", "progress")
    assert status.spinner.visible is True
    assert status.icon.visible is False
    assert status.text.value == "יוצר קובץ Word..."


def test_finishing_replaces_spinner_with_result_icon(status):
    status.set("יוצר קובץ Word...", "progress")
    status.set("הדוח נשמר בהצלחה", "success")
    assert status.spinner.visible is False
    assert status.icon.visible is True
    assert status.icon.icon == ft.Icons.CHECK_CIRCLE_OUTLINE


def test_error_state_is_shown_with_error_icon(status):
    status.set("שדות חסרים: מספר בדיקה", "error")
    assert status.icon.icon == ft.Icons.ERROR_OUTLINE
    assert status.icon.visible is True
    assert status.spinner.visible is False


def test_unknown_kind_falls_back_to_info(status):
    status.set("הודעה", "not-a-kind")
    assert status.text.color == ft.Colors.BLUE_700


def test_status_control_is_a_single_row_of_spinner_icon_text(status):
    assert isinstance(status.control, ft.Row)
    assert status.control.controls == [status.spinner, status.icon, status.text]
