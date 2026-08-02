"""Compiles the canonical, human-authored Word template into a Jinja-tagged
docxtpl template.

The original template (``templates/empty_report_template.docx``) must never be
modified on disk. This module copies it and edits ONLY the copy, replacing the
literal "*" fill-in placeholders (and label lines that end with nothing to
fill in) with Jinja tags that ``report_builder.py`` renders via docxtpl.

Free-text sections (general/findings/discussion) can hold a variable number of
paragraphs, so each is turned into a docxtpl paragraph-level loop
(``{%p for %} ... {%p endfor %}``) anchored on one of the template's existing
placeholder paragraphs, which keeps that paragraph's exact style, RTL
direction, and font for every generated paragraph. The extra now-redundant
placeholder paragraphs are removed.

The template's own example photo skeleton (tab-spaced photo numbers and
"תמונה N:" captions) is replaced by a single marker paragraph. It cannot be
reproduced reliably as floating/anchored images via docxtpl (see README /
data/style_guide.md for the reasoning); ``report_builder.py`` finds the marker
after rendering and swaps it for a python-docx-built photo grid table.

This is a one-time build step tied to the exact structure of the canonical
template. Every edit is guarded by an assertion on the expected existing text,
so if the template file is ever replaced with a different layout, this script
fails loudly instead of silently producing a corrupt document.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import docx
from docx.text.paragraph import Paragraph

import config

PHOTOS_TABLE_MARKER = "##PHOTOS_TABLE_MARKER##"


class TemplateStructureError(RuntimeError):
    """Raised when the canonical template no longer matches the expected structure."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise TemplateStructureError(message)


def _clear_runs_keep_first(paragraph: Paragraph, new_text: str) -> None:
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for extra in runs[1:]:
        extra._element.getparent().remove(extra._element)


def _append_jinja_tag(paragraph: Paragraph, tag_text: str) -> None:
    """Append a new run at the end of a paragraph, cloning formatting from the
    paragraph's last existing run so RTL/font/size survive untouched."""
    runs = paragraph.runs
    last_rpr = runs[-1]._element.rPr if runs else None
    new_run = paragraph.add_run(tag_text)
    if last_rpr is not None:
        new_run._element.get_or_add_rPr()
        new_run._element.replace(new_run._element.rPr, copy.deepcopy(last_rpr))


def _replace_exact_run(paragraph: Paragraph, exact_text: str, new_text: str) -> None:
    for run in paragraph.runs:
        if run.text == exact_text:
            run.text = new_text
            return
    raise TemplateStructureError(
        f"Expected a run with text {exact_text!r} in paragraph {paragraph.text!r}"
    )


def _duplicate_paragraph_after(paragraph: Paragraph) -> Paragraph:
    new_p = copy.deepcopy(paragraph._p)
    paragraph._p.addnext(new_p)
    return Paragraph(new_p, paragraph._parent)


def _duplicate_paragraph_before(paragraph: Paragraph) -> Paragraph:
    new_p = copy.deepcopy(paragraph._p)
    paragraph._p.addprevious(new_p)
    return Paragraph(new_p, paragraph._parent)


def _make_paragraph_loop(
    content_paragraph: Paragraph,
    list_var: str,
    item_var: str,
    redundant_paragraphs: list[Paragraph],
) -> Paragraph:
    """Turn `content_paragraph` into a docxtpl paragraph-level for-loop and
    delete the redundant placeholder paragraphs that follow it. Returns the
    inserted ``{%p endfor %}`` paragraph (useful as a position anchor)."""
    endfor_p = _duplicate_paragraph_after(content_paragraph)
    _clear_runs_keep_first(endfor_p, "{%p endfor %}")

    for_p = _duplicate_paragraph_before(content_paragraph)
    _clear_runs_keep_first(for_p, f"{{%p for {item_var} in {list_var} %}}")

    _clear_runs_keep_first(content_paragraph, f"{{{{ {item_var} }}}}")

    for extra in redundant_paragraphs:
        extra._element.getparent().remove(extra._element)

    return endfor_p


def _edit_client_and_case_fields(doc: docx.Document) -> None:
    paras = doc.paragraphs

    subject_p = paras[8]
    _expect(subject_p.text == "הנדון: ", f"Unexpected subject paragraph: {subject_p.text!r}")
    _append_jinja_tag(subject_p, "{{ subject }}")

    claim_p = paras[9]
    _expect(claim_p.text == "תביעה:", f"Unexpected claim paragraph: {claim_p.text!r}")
    _append_jinja_tag(claim_p, " {{ claim_number }}")

    name_p = paras[12]
    _expect(
        "*שם המזמין*" in name_p.text,
        f"Unexpected client-name paragraph: {name_p.text!r}",
    )
    _replace_exact_run(name_p, "*שם המזמין*", "{{ client_contact_name }}")

    company_p = paras[13]
    _replace_exact_run(company_p, "*חברה/עסק*", "{{ client_company }}")

    address_p = paras[14]
    _replace_exact_run(address_p, "*כתובת מלאה*", "{{ client_address }}")

    city_p = paras[15]
    _replace_exact_run(city_p, "*עיר*", "{{ client_city }}")


def _edit_free_text_sections(doc: docx.Document) -> Paragraph:
    paras = doc.paragraphs

    general_heading = paras[28]
    _expect(general_heading.text == "כללי", f"Unexpected heading: {general_heading.text!r}")
    _make_paragraph_loop(
        content_paragraph=paras[29],
        list_var="general_paragraphs",
        item_var="general_p",
        redundant_paragraphs=[paras[30]],
    )

    findings_heading = paras[32]
    _expect(findings_heading.text == "ממצאים", f"Unexpected heading: {findings_heading.text!r}")
    _make_paragraph_loop(
        content_paragraph=paras[34],
        list_var="findings_paragraphs",
        item_var="findings_p",
        redundant_paragraphs=[paras[36], paras[37], paras[39], paras[40], paras[41]],
    )

    discussion_heading = paras[43]
    _expect(
        discussion_heading.text.strip() == "דיון ומסקנות",
        f"Unexpected heading: {discussion_heading.text!r}",
    )
    discussion_endfor = _make_paragraph_loop(
        content_paragraph=paras[46],
        list_var="discussion_paragraphs",
        item_var="discussion_p",
        redundant_paragraphs=[paras[48], paras[49]],
    )
    return discussion_endfor


def _replace_photo_skeleton_with_marker(doc: docx.Document, after_paragraph: Paragraph) -> None:
    """Delete every body element between `after_paragraph` and the trailing
    section properties (the example photo skeleton: tab-spaced photo numbers
    and "תמונה N:" captions), replacing it with one marker paragraph.

    Note: the legal disclaimer table sits EARLY in this template (right
    before the "כללי" heading), not at the end — the document body simply
    ends with plain paragraphs followed by the section properties element.
    """
    node = after_paragraph._p.getnext()
    while node is not None and not node.tag.endswith("}sectPr"):
        following = node.getnext()
        node.getparent().remove(node)
        node = following

    _expect(node is not None, "Could not locate the trailing section properties element")

    marker_p = _duplicate_paragraph_after(after_paragraph)
    _clear_runs_keep_first(marker_p, PHOTOS_TABLE_MARKER)


def _edit_case_info_tables(doc: docx.Document) -> None:
    table0 = doc.tables[0]
    number_p = table0.rows[0].cells[0].paragraphs[0]
    _expect("מס' בדיקה" in number_p.text, f"Unexpected table0 cell: {number_p.text!r}")
    _append_jinja_tag(number_p, "{{ report_number }}")

    date_p = table0.rows[0].cells[0].paragraphs[1]
    _expect("תאריך" in date_p.text, f"Unexpected table0 cell: {date_p.text!r}")
    _append_jinja_tag(date_p, "{{ report_date }}")

    pages_p = table0.rows[1].cells[0].paragraphs[0]
    _replace_exact_run(pages_p, "ל * ", "ל {{ page_count }} ")

    photo_pages_p = table0.rows[1].cells[0].paragraphs[1]
    _replace_exact_run(photo_pages_p, ", * ", ", {{ photo_page_count }} ")

    table1 = doc.tables[1]
    item_p, assembly_p, model_p = table1.rows[0].cells[0].paragraphs
    _append_jinja_tag(item_p, "{{ item }}")
    _append_jinja_tag(assembly_p, "{{ assembly }}")
    _append_jinja_tag(model_p, "{{ manufacturer_model }}")

    material_p, nature_p, license_p = table1.rows[0].cells[1].paragraphs
    _append_jinja_tag(material_p, "{{ material }}")
    _append_jinja_tag(nature_p, "{{ inspection_nature }}")
    _append_jinja_tag(license_p, "{{ license_number }}")


def build_compiled_template(force: bool = False) -> Path:
    """Build (or reuse a cached copy of) the Jinja-tagged template.

    Returns the path to the compiled template. The original canonical
    template file is only ever read, never written.
    """
    source = config.CANONICAL_TEMPLATE_PATH
    target = config.COMPILED_TEMPLATE_PATH

    if not force and target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    config.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    doc = docx.Document(str(target))
    _edit_case_info_tables(doc)
    _edit_client_and_case_fields(doc)
    discussion_endfor = _edit_free_text_sections(doc)
    _replace_photo_skeleton_with_marker(doc, after_paragraph=discussion_endfor)
    doc.save(str(target))

    return target


if __name__ == "__main__":
    path = build_compiled_template(force=True)
    print(f"Compiled template written to: {path}")
