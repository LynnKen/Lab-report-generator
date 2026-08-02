"""Confirms generated Hebrew content keeps RTL direction and right/justify
alignment, both for templated free-text paragraphs and for the
python-docx-generated photo captions."""

from __future__ import annotations

import docx
from docx.oxml.ns import qn

import report_builder
from test_report_rendering import _build_sample_inputs, _build_sample_text, _make_photos


def _has_bidi(paragraph) -> bool:
    pPr = paragraph._p.find(qn("w:pPr"))
    return pPr is not None and pPr.find(qn("w:bidi")) is not None


def test_free_text_paragraphs_are_rtl(isolated_paths):
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        generated_text=_build_sample_text(),
        photos=[],
    )
    output_path = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    target_texts = {
        "ביקרתי במוסך לבדיקת הנזק.",
        "נמצא סדק בבסיס החלק (תמונה 1).",
        "הסדק נובע ככל הנראה מעייפות חומר.",
    }
    checked = 0
    for paragraph in document.paragraphs:
        if paragraph.text.strip() in target_texts:
            checked += 1
            assert _has_bidi(paragraph), f"missing w:bidi on paragraph: {paragraph.text!r}"
            assert any(run.font.rtl for run in paragraph.runs), (
                f"no run with w:rtl on paragraph: {paragraph.text!r}"
            )
    assert checked == len(target_texts)


def test_photo_captions_are_rtl(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=1)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        generated_text=_build_sample_text(),
        photos=photos,
    )
    output_path = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    caption_paragraphs = [
        p
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        for p in cell.paragraphs
        if "תמונה 1" in p.text
    ]
    assert caption_paragraphs, "photo caption paragraph not found"
    for paragraph in caption_paragraphs:
        assert _has_bidi(paragraph)
        assert any(run.font.rtl for run in paragraph.runs)
