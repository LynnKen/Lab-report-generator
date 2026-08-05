"""Unit tests for word_page_counter.py using a mocked win32com.client, so
this suite runs without MS Word installed. A separate, explicitly opt-in
integration test at the bottom exercises real MS Word."""

from __future__ import annotations

import os
import time

import pytest

import word_page_counter


class _FakeRange:
    def __init__(self, page: int):
        self._page = page

    def Information(self, const):
        assert const == word_page_counter._WD_ACTIVE_END_PAGE_NUMBER
        return self._page


class _FakeBookmark:
    def __init__(self, page: int):
        self.Range = _FakeRange(page)


class _FakeBookmarks:
    def __init__(self, pages_by_name: dict):
        self._pages = pages_by_name

    def Exists(self, name: str) -> bool:
        return name in self._pages

    def __call__(self, name: str) -> _FakeBookmark:
        return _FakeBookmark(self._pages[name])


class _FakeDocument:
    def __init__(self, total_pages: int, bookmark_pages: dict, raise_on_compute: bool = False, sleep: float = 0.0):
        self._total_pages = total_pages
        self._raise_on_compute = raise_on_compute
        self._sleep = sleep
        self.Bookmarks = _FakeBookmarks(bookmark_pages)
        self.closed_with = "not-closed"

    def ComputeStatistics(self, const):
        assert const == word_page_counter._WD_STATISTIC_PAGES
        if self._sleep:
            time.sleep(self._sleep)
        if self._raise_on_compute:
            raise RuntimeError("simulated Word failure")
        return self._total_pages

    def Close(self, SaveChanges=None):
        self.closed_with = SaveChanges


class _FakeDocuments:
    def __init__(self, document: _FakeDocument):
        self._document = document

    def Open(self, path, ReadOnly=None, AddToRecentFiles=None):
        return self._document


class _FakeWordApp:
    def __init__(self, document: _FakeDocument):
        self.Documents = _FakeDocuments(document)
        self.Visible = None
        self.DisplayAlerts = None
        self.quit_called = False

    def Quit(self):
        self.quit_called = True


def _install_fake_word(monkeypatch, document: _FakeDocument) -> _FakeWordApp:
    import win32com.client

    app = _FakeWordApp(document)
    monkeypatch.setattr(win32com.client, "DispatchEx", lambda _name: app)
    return app


def test_measure_total_pages_happy_path(monkeypatch, tmp_path):
    doc = _FakeDocument(total_pages=7, bookmark_pages={})
    app = _install_fake_word(monkeypatch, doc)

    total = word_page_counter.measure_total_pages(tmp_path / "report.docx")

    assert total == 7
    assert app.Visible is False
    assert app.DisplayAlerts == 0
    assert doc.closed_with is False  # never saves
    assert app.quit_called is True


def test_measure_report_happy_path(monkeypatch, tmp_path):
    doc = _FakeDocument(total_pages=9, bookmark_pages={"PhotoAppendixStart": 5})
    _install_fake_word(monkeypatch, doc)

    total, appendix_start = word_page_counter.measure_report(tmp_path / "report.docx", "PhotoAppendixStart")

    assert (total, appendix_start) == (9, 5)


def test_measure_report_raises_when_bookmark_missing(monkeypatch, tmp_path):
    doc = _FakeDocument(total_pages=9, bookmark_pages={})
    _install_fake_word(monkeypatch, doc)

    with pytest.raises(word_page_counter.WordAutomationError):
        word_page_counter.measure_report(tmp_path / "report.docx", "PhotoAppendixStart")


def test_close_and_quit_always_called_even_on_failure(monkeypatch, tmp_path):
    doc = _FakeDocument(total_pages=0, bookmark_pages={}, raise_on_compute=True)
    app = _install_fake_word(monkeypatch, doc)

    with pytest.raises(word_page_counter.WordAutomationError):
        word_page_counter.measure_total_pages(tmp_path / "report.docx")

    assert doc.closed_with is False
    assert app.quit_called is True


def test_missing_pywin32_raises_clear_error(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "win32com.client", None)
    with pytest.raises(word_page_counter.WordAutomationError):
        word_page_counter.measure_total_pages(tmp_path / "report.docx")


def test_timeout_raises_word_automation_error(monkeypatch, tmp_path):
    monkeypatch.setattr(word_page_counter, "_COM_CALL_TIMEOUT_SECONDS", 0.05)
    doc = _FakeDocument(total_pages=1, bookmark_pages={}, sleep=0.3)
    _install_fake_word(monkeypatch, doc)

    with pytest.raises(word_page_counter.WordAutomationError):
        word_page_counter.measure_total_pages(tmp_path / "report.docx")


@pytest.mark.skipif(
    os.getenv("RUN_WORD_INTEGRATION_TESTS") != "1",
    reason="requires a real MS Word installation; set RUN_WORD_INTEGRATION_TESTS=1 to run",
)
def test_real_word_measures_a_real_document_and_leaves_no_process(tmp_path):
    import docx

    doc_path = tmp_path / "integration.docx"
    document = docx.Document()
    document.add_paragraph("Integration test document.")
    document.save(str(doc_path))

    total = word_page_counter.measure_total_pages(doc_path)
    assert total >= 1
