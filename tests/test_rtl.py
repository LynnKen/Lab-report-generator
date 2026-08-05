"""Confirms generated Hebrew content keeps RTL direction and right/justify
alignment, both for templated free-text paragraphs and for the
python-docx-generated photo captions."""

from __future__ import annotations

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

import config
import report_builder
from test_report_rendering import _build_sample_inputs, _build_sample_text, _make_photos


def _has_bidi(paragraph) -> bool:
    pPr = paragraph._p.find(qn("w:pPr"))
    return pPr is not None and pPr.find(qn("w:bidi")) is not None


def _child_tags(element) -> list[str]:
    return [child.tag.split("}")[-1] for child in element]


def _caption_cell(document):
    grid = [
        t for t in document.tables
        if any("תמונה 1" in c.text for row in t.rows for c in row.cells)
    ][0]
    for row in grid.rows:
        if "תמונה 1" in row.cells[0].text:
            return grid, row.cells[0]
    raise AssertionError("caption cell not found")


def test_bidi_precedes_jc_in_every_generated_paragraph(isolated_paths, tmp_path):
    """OOXML fixes a strict child order for <w:pPr>: w:bidi must come before
    w:jc. Word silently IGNORES an out-of-order w:bidi, which is exactly what
    made the photo captions render left-to-right, so assert the order itself
    rather than merely the presence of both elements."""
    photos = _make_photos(isolated_paths, tmp_path, count=3)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=photos,
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    checked = 0
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    pPr = paragraph._p.find(qn("w:pPr"))
                    if pPr is None:
                        continue
                    tags = _child_tags(pPr)
                    if "bidi" in tags and "jc" in tags:
                        checked += 1
                        assert tags.index("bidi") < tags.index("jc"), (
                            f"w:bidi must precede w:jc, got {tags} on {paragraph.text!r}"
                        )
    assert checked >= 4, f"expected several bidi+jc paragraphs, found {checked}"


def test_table_bidi_visual_is_in_schema_order(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=2)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(), body_text=_build_sample_text(), photos=photos
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))
    grid, _cell = _caption_cell(document)

    tags = _child_tags(grid._tbl.tblPr)
    assert "bidiVisual" in tags
    for later in ("tblW", "jc", "tblCellMar", "tblLook"):
        if later in tags:
            assert tags.index("bidiVisual") < tags.index(later), tags


def test_caption_number_marker_is_a_separate_ltr_run(isolated_paths, tmp_path):
    """The "1." marker must live in its own left-to-right run. In a single RTL
    run the bidi algorithm puts the period to the LEFT of the digit and the
    reader sees ".1" - the exact bug reported from the generated report."""
    photos = _make_photos(isolated_paths, tmp_path, count=2)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(), body_text=_build_sample_text(), photos=photos
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))
    _grid, cell = _caption_cell(document)

    line = next(p for p in cell.paragraphs if p.text.strip().startswith("1."))
    assert len(line.runs) == 2, [r.text for r in line.runs]

    marker, text = line.runs
    assert marker.text == "1."
    assert marker.font.rtl is False, "marker run must be LTR so it renders as '1.' not '.1'"
    assert text.text.startswith(" ")
    assert text.font.rtl is True

    # And the paragraph itself must be RTL + aligned to the reading start.
    # NB: for a w:bidi paragraph that is JUSTIFY ("both"), NOT RIGHT - Word
    # renders w:jc "right" at the physical LEFT in RTL (verified in Word).
    assert _has_bidi(line)
    assert line.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY


def test_photo_number_is_large_bold_black_and_right_aligned(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=2)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(), body_text=_build_sample_text(), photos=photos
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))
    grid, _cell = _caption_cell(document)

    number_paragraph = next(
        p for p in grid.rows[0].cells[0].paragraphs if p.text.strip() == "1"
    )
    assert number_paragraph.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    assert _has_bidi(number_paragraph)

    run = number_paragraph.runs[0]
    assert run.bold is True
    assert run.font.color.rgb == RGBColor(0, 0, 0)
    assert run.font.size == Pt(config.PHOTO_NUMBER_FONT_PT)
    # complex-script size must match too, or Word can fall back for it
    szCs = run._element.rPr.find(qn("w:szCs"))
    assert szCs is not None
    assert int(szCs.get(qn("w:val"))) == int(config.PHOTO_NUMBER_FONT_PT * 2)


def test_photo_grid_has_gutters_and_smaller_images(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=2)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(), body_text=_build_sample_text(), photos=photos
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))
    grid, _cell = _caption_cell(document)

    cell_mar = grid._tbl.tblPr.find(qn("w:tblCellMar"))
    assert cell_mar is not None, "photo grid must define cell margins (the gutter)"
    expected = int(round((config.PHOTO_GUTTER_MM / 2) * report_builder._TWIPS_PER_MM))
    for edge in ("top", "left", "bottom", "right"):
        node = cell_mar.find(qn(f"w:{edge}"))
        assert node is not None and int(node.get(qn("w:w"))) == expected

    # Images must be scaled down relative to the bare column width.
    usable_in = report_builder._usable_width_inches(document)
    column_in = usable_in / config.PHOTO_GRID_COLUMNS
    picture = grid.rows[0].cells[0].paragraphs[0].runs[0]._element.find(
        ".//" + qn("wp:extent")
    )
    assert picture is not None
    width_in = int(picture.get("cx")) / 914400
    assert width_in < column_in * 0.95, (width_in, column_in)


def test_free_text_paragraphs_are_rtl(isolated_paths):
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=[],
    )
    output_path, _warning = report_builder.build_report(data)
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
        body_text=_build_sample_text(),
        photos=photos,
    )
    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    caption_paragraphs = [
        p
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        for p in cell.paragraphs
        if "תמונה 1" in p.text
    ]
    assert caption_paragraphs, "photo caption heading paragraph not found"
    for paragraph in caption_paragraphs:
        assert _has_bidi(paragraph)
        assert any(run.font.rtl for run in paragraph.runs)

    caption_line_paragraphs = [
        p
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        for p in cell.paragraphs
        if p.text.strip() == "1. כיתוב לתמונה 1"
    ]
    assert caption_line_paragraphs, "photo caption line paragraph not found"
    for paragraph in caption_line_paragraphs:
        assert _has_bidi(paragraph)
        assert any(run.font.rtl for run in paragraph.runs)
