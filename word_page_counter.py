"""Word COM automation for exact page counts (Windows + MS Word required).

python-docx/docxtpl never lay out text, so they cannot report an actual page
count. This module launches a hidden, throwaway MS Word instance via
late-bound win32com to measure the true, final pagination of a saved .docx
file. It always closes the document without saving and always quits Word,
even on error, to avoid orphaned WINWORD.EXE processes — and it always starts
a brand-new Word instance (DispatchEx, not Dispatch) so it can never attach
to, hide, or otherwise interfere with a Word window the user already has open
interactively.

Note: during teardown (Quit/proxy release), pywin32's own crash guard can
print a "Windows fatal exception" stack trace to stderr even though nothing
actually crashed - verified in practice with real Word: the correct page
count is still returned, no orphaned WINWORD.EXE process is left behind, and
execution continues normally. This is cosmetic RPC teardown noise, not a
functional failure; harmless enough to leave as-is rather than chase further.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Optional, Tuple

_WD_STATISTIC_PAGES = 2  # wdStatisticPages
_WD_ACTIVE_END_PAGE_NUMBER = 3  # wdActiveEndPageNumber
# A cold MS Word start alone was measured at ~28s on a normal machine, so a
# 30s budget silently degraded real reports to "-" page counts. Keep enough
# headroom that only a genuinely stuck Word trips this.
_COM_CALL_TIMEOUT_SECONDS = 90


class WordAutomationError(RuntimeError):
    """Raised when MS Word COM automation is unavailable or fails."""


def measure_total_pages(docx_path: Path) -> int:
    """Returns the actual, rendered total page count of a .docx file."""
    total_pages, _ = _measure_with_timeout(docx_path, bookmark_name=None)
    return total_pages


def measure_report(docx_path: Path, bookmark_name: str) -> Tuple[int, int]:
    """Returns (total_pages, bookmark_page) for a .docx file, where
    `bookmark_page` is the page number where `bookmark_name` starts."""
    total_pages, bookmark_page = _measure_with_timeout(docx_path, bookmark_name=bookmark_name)
    if bookmark_page is None:
        raise WordAutomationError(f"Bookmark {bookmark_name!r} not found in document")
    return total_pages, bookmark_page


def _measure_with_timeout(docx_path: Path, bookmark_name: Optional[str]) -> Tuple[int, Optional[int]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_measure, docx_path, bookmark_name)
        try:
            return future.result(timeout=_COM_CALL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            raise WordAutomationError(
                f"MS Word did not respond within {_COM_CALL_TIMEOUT_SECONDS}s while measuring "
                f"{docx_path.name}."
            ) from exc


def _dispatch_word():
    try:
        import win32com.client
    except ImportError as exc:
        raise WordAutomationError(
            "pywin32 is not installed; cannot compute exact page counts."
        ) from exc
    try:
        word = win32com.client.DispatchEx("Word.Application")
    except Exception as exc:
        raise WordAutomationError(f"Could not start MS Word via COM: {exc}") from exc
    word.Visible = False
    word.DisplayAlerts = 0
    return word


def _measure(docx_path: Path, bookmark_name: Optional[str]) -> Tuple[int, Optional[int]]:
    # This runs in its own worker thread (see _measure_with_timeout), and COM
    # requires every thread that uses it to initialize its own apartment
    # first - skipping this can crash the process at the native level rather
    # than raising a catchable Python exception.
    try:
        import pythoncom
    except ImportError as exc:
        raise WordAutomationError(
            "pywin32 is not installed; cannot compute exact page counts."
        ) from exc

    pythoncom.CoInitialize()
    try:
        return _measure_inner(docx_path, bookmark_name)
    finally:
        # All COM proxy objects (word, doc, ranges, ...) must be released
        # BEFORE the apartment is uninitialized, or releasing them afterwards
        # can fault at the native level. _measure_inner already clears its
        # own references in its own finally block; gc.collect() forces any
        # remaining proxy objects (e.g. intermediate Range/Bookmarks COM
        # wrappers) to actually be released now, while the apartment is
        # still valid, instead of at some later, unpredictable GC pass.
        import gc

        gc.collect()
        pythoncom.CoUninitialize()


def _measure_inner(docx_path: Path, bookmark_name: Optional[str]) -> Tuple[int, Optional[int]]:
    word = None
    doc = None
    try:
        word = _dispatch_word()
        doc = word.Documents.Open(str(docx_path), ReadOnly=True, AddToRecentFiles=False)
        total_pages = int(doc.ComputeStatistics(_WD_STATISTIC_PAGES))
        bookmark_page = None
        if bookmark_name is not None and doc.Bookmarks.Exists(bookmark_name):
            bookmark_range = doc.Bookmarks(bookmark_name).Range
            bookmark_page = int(bookmark_range.Information(_WD_ACTIVE_END_PAGE_NUMBER))
            bookmark_range = None
        return total_pages, bookmark_page
    except WordAutomationError:
        raise
    except Exception as exc:
        raise WordAutomationError(f"Word automation failed while measuring {docx_path.name}: {exc}") from exc
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=False)
            except Exception:
                pass
            doc = None
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
            word = None
