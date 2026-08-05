"""Builds the final .docx report from the canonical Word template.

Renders validated Hebrew content and metadata via docxtpl (against the
compiled, Jinja-tagged copy of the template — see template_prep.py), then
post-processes with python-docx to insert a photo appendix at the marker left
by the compiler, and finally uses MS Word COM automation (word_page_counter)
to compute the *actual* rendered page count and photo-appendix page count,
since python-docx/docxtpl never lay out text and cannot paginate. The LLM is
never asked to produce Word formatting: report text is entered by the user
verbatim and dropped into paragraphs that already carry the template's own
style, RTL direction, and fonts; the only AI involvement anywhere in this
pipeline is per-photo captioning (see ai_service.py).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docxtpl import DocxTemplate
from pydantic import BaseModel, ConfigDict, Field

import config
import template_prep
import word_page_counter
from photo_service import PhotoAsset

logger = logging.getLogger(__name__)


class ReportInputs(BaseModel):
    """Report metadata, entered directly by the user (nothing here is
    AI-generated)."""

    report_number: str
    report_date: str

    client_contact_name: str = ""
    client_company: str = ""
    client_address: str = ""
    client_city: str = ""

    subject: str
    claim_number: str = ""

    item: str = ""
    assembly: str = ""
    manufacturer_model: str = ""
    material: str = ""
    inspection_nature: str = ""
    license_number: str = ""


class ReportBodyText(BaseModel):
    """The report's official free-text sections, authored directly by the
    user (not AI-generated) — matches the template's three loop sections."""

    model_config = ConfigDict(extra="forbid")

    general_paragraphs: List[str] = Field(default_factory=list)
    findings_paragraphs: List[str] = Field(default_factory=list)
    discussion_paragraphs: List[str] = Field(default_factory=list)


@dataclass
class ReportRenderData:
    inputs: ReportInputs
    body_text: ReportBodyText
    photos: List[PhotoAsset] = field(default_factory=list)


class ReportBuildError(RuntimeError):
    pass


PHOTO_APPENDIX_BOOKMARK = "PhotoAppendixStart"
_BOOKMARK_ID = "0"

_PAGE_COUNT_PLACEHOLDER = "§PAGECOUNT§"
_PHOTO_PAGE_COUNT_PLACEHOLDER = "§PHOTOPAGECOUNT§"


# OOXML (ECMA-376) fixes a strict child order for <w:pPr> and <w:tblPr>.
# Word silently IGNORES an element that appears out of order - which is what
# made the photo captions render left-to-right: w:bidi was being appended
# after w:jc instead of before it. These are the schema-mandated elements
# that must follow w:bidi / w:bidiVisual, used with python-docx's
# insert_element_before() so new elements always land in a valid position.
_PPR_AFTER_BIDI = (
    "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
    "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
    "w:pPrChange",
)
_TBLPR_AFTER_BIDIVISUAL = (
    "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc", "w:tblCellSpacing",
    "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
    "w:tblCaption", "w:tblDescription", "w:tblPrChange",
)
_TBLPR_AFTER_CELLMAR = ("w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange")

_TWIPS_PER_MM = 56.6929  # 1 mm = 1440/25.4 twentieths of a point


def _align_to_rtl_start(paragraph) -> None:
    """Aligns an RTL paragraph to the reading start, i.e. the physical RIGHT.

    Counterintuitively this must NOT be WD_ALIGN_PARAGRAPH.RIGHT: inside a
    <w:bidi/> paragraph Word reads w:jc "right" as the logical *end*, which
    for Hebrew is the physical left - verified by rendering every ST_Jc value
    through real Word. "both" (justify) is start-aligned for RTL and is also
    exactly what the lab's own reference reports use on these paragraphs."""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY


def _set_paragraph_bidi(paragraph) -> None:
    """Marks a paragraph itself as right-to-left (<w:bidi/>).

    This is the paragraph-level direction, which is what actually makes Word
    lay Hebrew out from the right and resolve digits/punctuation correctly -
    run-level w:rtl alone is not enough, and neither is alignment."""
    pPr = paragraph._p.get_or_add_pPr()
    if pPr.find(qn("w:bidi")) is None:
        pPr.insert_element_before(pPr.makeelement(qn("w:bidi"), {}), *_PPR_AFTER_BIDI)


def _set_table_rtl(table) -> None:
    tblPr = table._tbl.tblPr
    if tblPr.find(qn("w:bidiVisual")) is None:
        tblPr.insert_element_before(
            tblPr.makeelement(qn("w:bidiVisual"), {}), *_TBLPR_AFTER_BIDIVISUAL
        )


def _set_table_cell_margins(table, margin_mm: float) -> None:
    """Sets a uniform inner margin on every cell, which is what produces the
    visible gutter between neighbouring photos (two adjacent cells each
    contribute their own margin, so the gap between images is ~2x this)."""
    tblPr = table._tbl.tblPr
    existing = tblPr.find(qn("w:tblCellMar"))
    if existing is not None:
        tblPr.remove(existing)
    cell_mar = tblPr.makeelement(qn("w:tblCellMar"), {})
    value = str(int(round(margin_mm * _TWIPS_PER_MM)))
    for edge in ("top", "left", "bottom", "right"):
        node = cell_mar.makeelement(qn(f"w:{edge}"), {qn("w:w"): value, qn("w:type"): "dxa"})
        cell_mar.append(node)
    tblPr.insert_element_before(cell_mar, *_TBLPR_AFTER_CELLMAR)


def _set_row_cant_split(row) -> None:
    """Prevents a table row from splitting across a page boundary — keeps a
    row of photo+caption cells atomic, so a caption can never land on a
    different page than its photo."""
    trPr = row._tr.get_or_add_trPr()
    if trPr.find(qn("w:cantSplit")) is None:
        cant_split = trPr.makeelement(qn("w:cantSplit"), {})
        trPr.append(cant_split)


def _add_page_break_and_bookmark(paragraph, bookmark_name: str) -> None:
    """Forces a page break immediately before `paragraph` and marks its very
    start with a named bookmark, both on the SAME paragraph — so there is no
    separate anchor paragraph that could introduce a spurious blank page, and
    the bookmark's page number is, by construction, the first page of real
    content after the break."""
    paragraph.paragraph_format.page_break_before = True

    p = paragraph._p
    pPr = p.find(qn("w:pPr"))
    insert_index = list(p).index(pPr) + 1 if pPr is not None else 0

    bookmark_end = p.makeelement(qn("w:bookmarkEnd"), {qn("w:id"): _BOOKMARK_ID})
    bookmark_start = p.makeelement(
        qn("w:bookmarkStart"), {qn("w:id"): _BOOKMARK_ID, qn("w:name"): bookmark_name}
    )
    p.insert(insert_index, bookmark_end)
    p.insert(insert_index, bookmark_start)


def _build_render_context(data: ReportRenderData) -> dict:
    inputs = data.inputs
    text = data.body_text
    context = inputs.model_dump()
    context.update(
        general_paragraphs=text.general_paragraphs or [""],
        findings_paragraphs=text.findings_paragraphs or [""],
        discussion_paragraphs=text.discussion_paragraphs or [""],
        page_count=_PAGE_COUNT_PLACEHOLDER,
        photo_page_count=_PHOTO_PAGE_COUNT_PLACEHOLDER,
    )
    return context


def _usable_width_inches(document: docx.Document) -> float:
    section = document.sections[0]
    usable_emu = section.page_width - section.left_margin - section.right_margin
    return usable_emu / 914400  # EMUs per inch


def _merge_row_into_one_cell(row):
    """Merges every cell in a table row into a single wide cell (used for
    the borderless caption block spanning the full row width)."""
    merged = row.cells[0]
    for cell in row.cells[1:]:
        merged = merged.merge(cell)
    return merged


def _add_photo_number(cell, number: int):
    """Adds the large, bold, black photo number directly beneath its photo,
    hugging the right edge of the cell so it sits at the photo's lower-right
    in RTL reading order (first photo of a row is the rightmost one).

    The digit is an LTR run - it is a number, not Hebrew text - but the
    paragraph itself is bidi so that "right" means the reading-order start."""
    paragraph = cell.add_paragraph()
    _set_paragraph_bidi(paragraph)
    _align_to_rtl_start(paragraph)
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(0)

    run = paragraph.add_run(str(number))
    run.bold = True
    run.font.rtl = False
    run.font.color.rgb = RGBColor(0, 0, 0)
    _set_run_size_both(run, Pt(config.PHOTO_NUMBER_FONT_PT))
    return paragraph


def _set_run_size_both(run, size) -> None:
    """Sets a run's size for both regular (w:sz) and complex-script (w:szCs)
    text. Word picks whichever applies per character, so setting only
    font.size can leave Hebrew/complex-script glyphs at the inherited size."""
    run.font.size = size
    rPr = run._element.get_or_add_rPr()
    szCs = rPr.find(qn("w:szCs"))
    if szCs is None:
        szCs = rPr.makeelement(qn("w:szCs"), {})
        rPr.append(szCs)
    szCs.set(qn("w:val"), str(int(size.pt * 2)))  # half-points


def _add_numbered_caption_line(cell, number: int, caption: str):
    """Adds one "N. caption" line as TWO runs: a left-to-right run holding the
    "N." marker, then a right-to-left run holding the Hebrew text.

    Splitting the runs is what keeps the marker readable. In a single RTL run
    the Unicode bidi algorithm resolves the '.' after a digit to the
    paragraph's own (right-to-left) direction, so "1." is laid out with the
    period to the LEFT of the digit and the reader sees ".1". Marking just the
    marker run as LTR keeps "1." together and in the right order, while the
    paragraph as a whole still starts at the right margin. Verified against a
    real Word render, not assumed."""
    paragraph = cell.add_paragraph()
    _set_paragraph_bidi(paragraph)
    _align_to_rtl_start(paragraph)

    marker_run = paragraph.add_run(f"{number}.")
    marker_run.font.rtl = False

    if caption:
        text_run = paragraph.add_run(f" {caption}")
        text_run.font.rtl = True
    return paragraph


def _add_caption_group_block(cell, heading_text: str, photos_in_group: List[PhotoAsset], start_number: int) -> None:
    """Fills the merged caption-row cell with a bold/underlined page-level
    heading followed by one concise numbered line per photo in the group -
    the numbers match the numbers shown beside each photo on the same page
    (see _insert_photo_grid)."""
    heading_paragraph = cell.paragraphs[0]
    heading_run = heading_paragraph.add_run(heading_text)
    heading_run.bold = True
    heading_run.underline = True
    _set_paragraph_bidi(heading_paragraph)
    _align_to_rtl_start(heading_paragraph)
    heading_run.font.rtl = True

    for offset, photo in enumerate(photos_in_group):
        _add_numbered_caption_line(cell, start_number + offset, photo.caption.strip())


def _insert_photo_grid(document: docx.Document, photos: List[PhotoAsset]) -> Optional[str]:
    """Builds the photo appendix and returns the bookmark name marking its
    start (or None when there are no photos, i.e. no appendix at all).

    Photos are grouped into fixed-size pages (config.PHOTOS_PER_APPENDIX_PAGE)
    with a forced page break before each group's first row, since
    python-docx/docxtpl cannot know Word's real pagination ahead of time -
    this deterministic grouping is what guarantees a page's photos, their
    numbers, and their shared caption block always land on the same page
    together. Each group is one contiguous block of table rows (photo rows +
    one merged caption row), all marked cantSplit, so Word can never split it
    across a page boundary.
    """
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
        return None

    columns = max(1, config.PHOTO_GRID_COLUMNS)
    capacity = max(columns, config.PHOTOS_PER_APPENDIX_PAGE)

    # Each cell carries half the gutter on every side, so the visible gap
    # between two neighbouring photos is a full config.PHOTO_GUTTER_MM.
    cell_margin_mm = config.PHOTO_GUTTER_MM / 2
    gutter_in = (cell_margin_mm * 2) / 25.4
    column_width_in = _usable_width_inches(document) / columns
    image_width_in = max(0.5, (column_width_in - gutter_in) * config.PHOTO_IMAGE_SCALE)

    groups = [photos[i : i + capacity] for i in range(0, len(photos), capacity)]
    total_photo_rows = sum(math.ceil(len(group) / columns) for group in groups)
    total_rows = total_photo_rows + len(groups)  # one extra caption row per group

    table = document.add_table(rows=total_rows, cols=columns)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_rtl(table)
    _set_table_cell_margins(table, cell_margin_mm)

    row_cursor = 0
    photo_number = 1
    for group_index, group in enumerate(groups):
        group_first_row = row_cursor
        for position_in_group, photo in enumerate(group):
            r, c = divmod(position_in_group, columns)
            cell = table.rows[row_cursor + r].cells[c]

            image_paragraph = cell.paragraphs[0]
            image_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            image_paragraph.add_run().add_picture(str(photo.final_path), width=Inches(image_width_in))

            _add_photo_number(cell, photo_number)
            photo_number += 1

        photo_rows_in_group = math.ceil(len(group) / columns)
        for r in range(photo_rows_in_group):
            _set_row_cant_split(table.rows[group_first_row + r])

        first_image_paragraph = table.rows[group_first_row].cells[0].paragraphs[0]
        if group_index == 0:
            _add_page_break_and_bookmark(first_image_paragraph, PHOTO_APPENDIX_BOOKMARK)
        else:
            first_image_paragraph.paragraph_format.page_break_before = True

        row_cursor += photo_rows_in_group

        caption_row = table.rows[row_cursor]
        _set_row_cant_split(caption_row)
        caption_cell = _merge_row_into_one_cell(caption_row)
        heading_text = "תמונה 1:" if group_index == 0 else "תמונה 1: (המשך)"
        start_number = photo_number - len(group)
        _add_caption_group_block(caption_cell, heading_text, group, start_number)
        row_cursor += 1

    marker_paragraph._p.addnext(table._tbl)
    marker_paragraph._p.getparent().remove(marker_paragraph._p)

    return PHOTO_APPENDIX_BOOKMARK


def _find_placeholder_segment(document: docx.Document, placeholder: str):
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        if placeholder in run.text:
                            prefix, _, suffix = run.text.partition(placeholder)
                            return run, prefix, suffix
    raise ReportBuildError(f"Could not find page-count placeholder {placeholder!r} in rendered document")


def _set_dynamic_run_text(run, prefix: str, suffix: str, value) -> None:
    run.text = f"{prefix}{value}{suffix}"


def _finalize_page_counts(document: docx.Document, output_path: Path, bookmark_name: Optional[str]) -> Optional[str]:
    """Computes the real total/photo-appendix page counts via MS Word COM
    automation and patches them into the existing template placeholders,
    re-measuring after each write to verify convergence. Returns a Hebrew
    warning string if automation was disabled or failed (report generation
    still succeeds either way — a non-fatal degradation, not an error)."""
    page_run, p_prefix, p_suffix = _find_placeholder_segment(document, _PAGE_COUNT_PLACEHOLDER)
    photo_run, pp_prefix, pp_suffix = _find_placeholder_segment(document, _PHOTO_PAGE_COUNT_PLACEHOLDER)

    def _write(page_value, photo_value) -> None:
        _set_dynamic_run_text(page_run, p_prefix, p_suffix, page_value)
        _set_dynamic_run_text(photo_run, pp_prefix, pp_suffix, photo_value)
        document.save(str(output_path))

    if not config.PAGE_COUNT_AUTOMATION_ENABLED:
        _write("-", "-")
        return "ספירת עמודים אוטומטית מושבתת - הוצגו מקפים במקום מספרי העמודים."

    total_pages = None
    photo_pages = None
    for attempt in range(1, config.PAGE_COUNT_MAX_ATTEMPTS + 1):
        try:
            if bookmark_name:
                new_total, appendix_start_page = word_page_counter.measure_report(output_path, bookmark_name)
                # Photo-appendix PAGES (not photo count): every page from the
                # appendix's first page through the end of the document.
                new_photo_pages = new_total - appendix_start_page + 1
            else:
                new_total = word_page_counter.measure_total_pages(output_path)
                new_photo_pages = 0
        except word_page_counter.WordAutomationError as exc:
            logger.warning("Page-count automation failed (%s); using '-' placeholders.", exc)
            _write("-", "-")
            return (
                "לא ניתן היה לחשב את מספר העמודים בפועל - יש לוודא ש-Microsoft Word "
                f"מותקן על מחשב זה. הדוח נוצר בכל זאת. ({exc})"
            )

        if attempt > 1 and new_total == total_pages and new_photo_pages == photo_pages:
            return None  # converged; correct values already saved from the previous pass

        total_pages, photo_pages = new_total, new_photo_pages
        _write(total_pages, photo_pages)

    logger.warning(
        "Page count did not converge after %d attempts; using last computed values "
        "(%d total / %d photo pages).",
        config.PAGE_COUNT_MAX_ATTEMPTS,
        total_pages,
        photo_pages,
    )
    return None


def build_report(data: ReportRenderData) -> Tuple[Path, Optional[str]]:
    """Renders the full report and returns (output_path, warning).

    `warning` is None on a fully successful build, or a Hebrew message when
    real page counting was unavailable/failed — the .docx is still produced
    either way, just with "-" placeholders for the page counts.
    """
    template_path = template_prep.build_compiled_template()

    tpl = DocxTemplate(str(template_path))
    context = _build_render_context(data)
    tpl.render(context)

    rendered_path = config.GENERATED_DIR / f"_rendered_{data.inputs.report_number}.docx"
    tpl.save(str(rendered_path))

    document = docx.Document(str(rendered_path))
    bookmark_name = _insert_photo_grid(document, sorted(data.photos, key=lambda p: p.order_index))

    filename = config.build_report_filename(report_number=data.inputs.report_number)
    output_path = config.REPORTS_DIR / filename
    document.save(str(output_path))

    rendered_path.unlink(missing_ok=True)

    warning = _finalize_page_counts(document, output_path, bookmark_name)

    return output_path, warning
