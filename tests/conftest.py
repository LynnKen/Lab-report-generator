"""Shared pytest fixtures: redirect all config paths into a temp directory so
tests never touch the real reports/, work/, or build/ folders."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Points every writable config path at a fresh tmp_path tree."""
    generated_dir = tmp_path / "build" / "templates"
    reports_dir = tmp_path / "reports"
    originals_dir = tmp_path / "work" / "photos" / "originals"
    previews_dir = tmp_path / "work" / "photos" / "previews"
    final_dir = tmp_path / "work" / "photos" / "final"

    for d in (generated_dir, reports_dir, originals_dir, previews_dir, final_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(config, "COMPILED_TEMPLATE_PATH", generated_dir / "report_jinja_template.docx")
    monkeypatch.setattr(config, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(config, "PHOTOS_ORIGINALS_DIR", originals_dir)
    monkeypatch.setattr(config, "PHOTOS_PREVIEWS_DIR", previews_dir)
    monkeypatch.setattr(config, "PHOTOS_FINAL_DIR", final_dir)

    # Real page counting requires MS Word installed; keep the test suite
    # hermetic and fast by disabling it here. tests/test_word_page_counter.py
    # covers the automation logic itself with a mocked win32com.
    monkeypatch.setattr(config, "PAGE_COUNT_AUTOMATION_ENABLED", False)

    return {
        "generated_dir": generated_dir,
        "reports_dir": reports_dir,
        "originals_dir": originals_dir,
        "previews_dir": previews_dir,
        "final_dir": final_dir,
    }


@pytest.fixture
def sample_image_path(tmp_path):
    from PIL import Image

    path = tmp_path / "sample.jpg"
    Image.new("RGB", (1200, 900), (120, 60, 60)).save(path, "JPEG")
    return path
