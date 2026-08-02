"""Builds the final .docx report from the canonical Word template.

Renders validated Hebrew content and metadata via docxtpl (against the
compiled, Jinja-tagged copy of the template — see template_prep.py), then
post-processes with python-docx to insert a photo grid at the marker left by
the compiler. The LLM is never asked to produce Word formatting: it only ever
supplies plain Hebrew paragraph strings, which are dropped into paragraphs
that already carry the template's own style, RTL direction, and fonts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches
from docxtpl import DocxTemplate

import config
import template_prep
from ai_service import GeneratedReportText, ReportInputs
from photo_service import PhotoAsset


@dataclass
class ReportRenderData:
    inputs: ReportInputs
    generated_text: GeneratedReportText
    photos: List[PhotoAsset] = field(default_factory=list)
    page_count: str = ""
    photo_page_count: str = ""


class ReportBuildError(RuntimeError):
    pass


def _set_rtl_paragraph(paragraph) -> None:
    """Force RTL direction + right-ish alignment on a paragraph and its runs.
    python-docx has no high-level property for w:bidi/w:rtl, so this edits
    the underlying XML directly."""
    pPr = paragraph._p.get_or_add_pPr()
    if pPr.find(qn("w:bidi")) is None:
        bidi = pPr.makeelement(qn("w:bidi"), {})
        pPr.append(bidi)
    for run in paragraph.runs:
        rPr = run._element.get_or_add_rPr()
        if rPr.find(qn("w:rtl")) is None:
            rtl = rPr.makeelement(qn("w:rtl"), {})
            rPr.append(rtl)


def _set_table_rtl(table) -> None:
    tblPr = table._tbl.tblPr
    if tblPr.find(qn("w:bidiVisual")) is None:
        bidi_visual = tblPr.makeelement(qn("w:bidiVisual"), {})
        tblPr.append(bidi_visual)


def _build_render_context(data: ReportRenderData) -> dict:
    inputs = data.inputs
    text = data.generated_text
    context = inputs.model_dump()
    context.update(
        general_paragraphs=text.general_paragraphs or [""],
        findings_paragraphs=text.findings_paragraphs or [""],
        discussion_paragraphs=text.discussion_paragraphs or [""],
        page_count=data.page_count or "-",
        photo_page_count=data.photo_page_count or "-",
    )
    return context


def _usable_width_inches(document: docx.Document) -> float:
    section = document.sections[0]
    usable_emu = section.page_width - section.left_margin - section.right_margin
    return usable_emu / 914400  # EMUs per inch


def _insert_photo_grid(document: docx.Document, photos: List[PhotoAsset]) -> None:
    marker_paragraph = None
    for paragraph in document.paragraphs:
        if paragraph.text.strip() == template_prep.PHOTOS_TABLE_MARKER:
            marker_paragraph = paragraph
            break

    if marker_paragraph is None:
        raise ReportBuildError(
            "Compiled template is missing the photo-table marker paragraph; "
            "rebuild it with template_prep.build_compiled_template(force=True)."
        )

    if not photos:
        marker_paragraph._p.getparent().remove(marker_paragraph._p)
        return

    columns = max(1, config.PHOTO_GRID_COLUMNS)
    rows = math.ceil(len(photos) / columns)
    column_width_in = _usable_width_inches(document) / columns
    image_width_in = max(0.5, column_width_in - 0.25)

    table = document.add_table(rows=rows, cols=columns)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_rtl(table)

    for position, photo in enumerate(photos):
        row_idx, col_idx = divmod(position, columns)
        cell = table.rows[row_idx].cells[col_idx]

        image_paragraph = cell.paragraphs[0]
        image_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        image_paragraph.add_run().add_picture(str(photo.final_path), width=Inches(image_width_in))

        caption_label = f"תמונה {position + 1}"
        if photo.caption.strip():
            caption_label += f": {photo.caption.strip()}"

        caption_paragraph = cell.add_paragraph(caption_label)
        caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_rtl_paragraph(caption_paragraph)

    marker_paragraph._p.addnext(table._tbl)
    marker_paragraph._p.getparent().remove(marker_paragraph._p)


def build_report(data: ReportRenderData) -> Path:
    """Renders the full report and returns the path to the saved .docx file."""
    template_path = template_prep.build_compiled_template()

    tpl = DocxTemplate(str(template_path))
    context = _build_render_context(data)
    tpl.render(context)

    rendered_path = config.GENERATED_DIR / f"_rendered_{data.inputs.report_number}.docx"
    tpl.save(str(rendered_path))

    document = docx.Document(str(rendered_path))
    _insert_photo_grid(document, sorted(data.photos, key=lambda p: p.order_index))

    client_name = data.inputs.client_company.strip() or data.inputs.client_contact_name.strip()
    filename = config.build_report_filename(
        report_number=data.inputs.report_number,
        report_date=data.inputs.report_date,
        client_name=client_name,
    )
    output_path = config.REPORTS_DIR / filename
    document.save(str(output_path))

    rendered_path.unlink(missing_ok=True)

    return output_path
