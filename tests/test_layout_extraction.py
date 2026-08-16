"""layout_extraction.py - DOCX numline discovery and PDF layout-evidence
matching. Uses small synthetic DOCX/PDF fixtures built in tmp_path, plus one
read-only check against the real C2501508pub_ref1 report (never writes to
it) to confirm the acceptance case.
"""

from __future__ import annotations

from pathlib import Path

import docx
import fitz
import pytest

import layout_extraction as le


@pytest.fixture
def sample_docx(tmp_path) -> Path:
    """A minimal DOCX matching the real reports' structure: a numline
    paragraph (bare tab-separated 'N.N' tokens) immediately before each
    'תמונה N[...]' heading."""
    document = docx.Document()
    document.add_paragraph("כללי")
    document.add_paragraph("טקסט כלשהו שאינו רלוונטי.")
    document.add_paragraph("\t1.1\t\t1.2\t\t1.3")
    document.add_paragraph("תמונה 1: כותרת לדוגמה.")
    document.add_paragraph("משפט ראשון.")
    document.add_paragraph("משפט שני.")
    document.add_paragraph("\t2.1")
    document.add_paragraph("תמונה 2: כותרת שנייה.")
    document.add_paragraph("משפט אחר.")
    path = tmp_path / "sample.docx"
    document.save(path)
    return path


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """A 2-page PDF: page 0 is a 'narrative' page mentioning '1.1' inline
    among other text (must NOT count as a match), page 1 has '1.1', '1.2',
    '1.3' each as their own isolated line (the real appendix-page shape)."""
    doc = fitz.open()
    page0 = doc.new_page()
    page0.insert_text((72, 72), "some narrative mentioning 1.1 inline in a sentence")
    page1 = doc.new_page()
    page1.insert_text((72, 72), "1.1")
    page1.insert_text((72, 100), "1.2")
    page1.insert_text((72, 128), "1.3")
    path = tmp_path / "sample_layout.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_discover_document_photo_refs_reads_numline_paragraphs(sample_docx):
    result = le.discover_document_photo_refs(sample_docx)
    assert result == {
        "photoset_1": ["1.1", "1.2", "1.3"],
        "photoset_2": ["2.1"],
    }


def test_find_layout_pdf_discovers_by_suffix_pattern(tmp_path):
    report_dir = tmp_path / "crane" / "C_TEST_ref1"
    report_dir.mkdir(parents=True)
    pdf_path = report_dir / "C_TEST_layout.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    found = le.find_layout_pdf(report_dir)
    assert found == pdf_path


def test_find_layout_pdf_returns_none_when_absent(tmp_path):
    report_dir = tmp_path / "crane" / "C_TEST_ref1"
    report_dir.mkdir(parents=True)
    assert le.find_layout_pdf(report_dir) is None


def test_find_layout_evidence_prefers_isolated_labels_over_inline_mentions(sample_pdf):
    """The real bug this guards against: a narrative page mentioning a
    number inline must never outrank the true appendix page just because
    the appendix page's number set is a subset."""
    evidence = le.find_layout_evidence(sample_pdf, ["1.1", "1.2", "1.3"])
    assert evidence is not None
    assert evidence.page_number == 2  # 1-indexed -> page index 1
    assert evidence.matched_count == 3
    assert evidence.total_count == 3


def test_find_layout_evidence_returns_none_without_any_isolated_match(sample_pdf):
    assert le.find_layout_evidence(sample_pdf, ["9.9"]) is None


def test_render_pdf_page_preview_returns_png_bytes(sample_pdf):
    png = le.render_pdf_page_preview(sample_pdf, page_number=2, max_dimension=400)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_discover_document_photo_refs_against_real_ref1_report():
    """Read-only check against the real templates/ DOCX (never modifies it)
    confirming the exact acceptance-case numbers for C2501508pub_ref1."""
    docx_path = Path("templates/crane_refernces/C2501508pub_ref1.docx")
    result = le.discover_document_photo_refs(docx_path)
    assert result["photoset_1"] == ["1.1", "1.2", "1.3", "1.4", "1.5"]
    assert result["photoset_1_continued_1"] == [
        "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12",
    ]
    assert result["photoset_2"] == ["2.1"]
