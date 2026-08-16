"""Read-only extraction helpers that use BOTH the original DOCX and its
matching *_layout.pdf to discover real photo-number structure for the
Reference Review screen - never invents a number or a text/photo mapping,
only reports what is verifiably present in the source documents.

Two independent, narrow signals, cross-checked:

- The DOCX (source of truth for wording) carries "numline" paragraphs - bare
  tab-separated "N.N" tokens with no other text - printed immediately before
  each "תמונה N[...]" heading. Each numline paragraph is one physical row of
  the original photo grid; walking backward from a heading and collecting
  its contiguous run of numline paragraphs (skipping blanks) gives the exact
  set of real photo numbers for that group. This never reads or alters
  prose wording.
- The layout PDF's per-page text, split into lines, contains the SAME
  numbers as their own isolated line (nothing else on that line) on the
  page that actually lays out that group's photo grid - narrative pages
  mention the same numbers only inline, embedded in a longer sentence, so
  requiring an ISOLATED line match (not just "appears somewhere on the
  page") is what distinguishes the real appendix page from a false match.

Neither signal is used to guess which specific sentence describes which
specific number - that judgement call is left to the human reviewer.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import docx
import fitz

HEADING_RE = re.compile(r"^תמונה\s+(\d+)")
_PURE_NUMLINE_TOKEN_RE = re.compile(r"^\d+\.\d+$")
_ISOLATED_LINE_RE = re.compile(r"^\d+\.\d+$")
_NUMBER_RE = re.compile(r"\d+\.\d+")


def _is_pure_photo_numline(raw_text: str) -> bool:
    """Same rule the original dataset extraction uses: a paragraph that is
    ONLY tab-separated 'N.N' tokens, e.g. '1.6\t\t1.7'."""
    tokens = [tok.strip() for tok in raw_text.split("\t") if tok.strip()]
    return bool(tokens) and all(_PURE_NUMLINE_TOKEN_RE.match(tok) for tok in tokens)


def find_layout_pdf(report_dir: Path) -> Optional[Path]:
    """Discovers the report's *_layout.pdf by pattern, never assuming an
    exact filename match to the folder/report id."""
    matches = sorted(report_dir.glob("*_layout.pdf"))
    return matches[0] if matches else None


def discover_document_photo_refs(docx_path: Path) -> Dict[str, List[str]]:
    """For every 'תמונה N[...]' heading in the DOCX, returns the group's real
    photo numbers - the contiguous run of pure-numline paragraphs found
    immediately before that heading (skipping blank paragraphs, stopping at
    the first real-content paragraph). group_id assignment (photoset_N /
    photoset_N_continued_K) matches the convention already used throughout
    data/reference_examples/, so results key directly onto existing groups.
    """
    document = docx.Document(str(docx_path))
    texts = [p.text for p in document.paragraphs]

    heading_positions = []
    for i, t in enumerate(texts):
        stripped = t.strip()
        m = HEADING_RE.match(stripped)
        if m:
            heading_positions.append((i, int(m.group(1)), stripped))

    results: Dict[str, List[str]] = {}
    seen_counts: Dict[int, int] = {}
    for start, number, heading_text in heading_positions:
        numbers: List[str] = []
        j = start - 1
        while j >= 0:
            t = texts[j].strip()
            if not t:
                j -= 1
                continue
            if _is_pure_photo_numline(texts[j]):
                numbers = _NUMBER_RE.findall(t) + numbers
                j -= 1
                continue
            break

        if number not in seen_counts:
            seen_counts[number] = 0
            group_id = f"photoset_{number}"
        else:
            seen_counts[number] += 1
            group_id = f"photoset_{number}_continued_{seen_counts[number]}"

        results[group_id] = numbers
    return results


@dataclass
class LayoutEvidence:
    pdf_filename: str
    page_number: int  # 1-indexed, as a human sees it in a viewer
    matched_count: int
    total_count: int

    @property
    def page_index(self) -> int:
        return self.page_number - 1

    def to_dict(self) -> dict:
        return {"pdf_filename": self.pdf_filename, "page_number": self.page_number}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["LayoutEvidence"]:
        if not data:
            return None
        return cls(
            pdf_filename=data["pdf_filename"],
            page_number=data["page_number"],
            matched_count=data.get("matched_count", 0),
            total_count=data.get("total_count", 0),
        )


def _page_isolated_number_tokens(pdf_path: Path) -> List[set]:
    doc = fitz.open(str(pdf_path))
    try:
        pages = []
        for i in range(doc.page_count):
            lines = doc[i].get_text("text").split("\n")
            pages.append({l.strip() for l in lines if _ISOLATED_LINE_RE.match(l.strip())})
        return pages
    finally:
        doc.close()


def find_layout_evidence(pdf_path: Path, numbers: List[str]) -> Optional[LayoutEvidence]:
    """Finds the PDF page whose isolated (own-line) number labels best match
    the given set of real photo numbers. Returns None if no page has any
    isolated-label overlap at all - this must never be guessed."""
    if not numbers:
        return None
    number_set = set(numbers)
    pages = _page_isolated_number_tokens(pdf_path)

    best_index, best_overlap = -1, 0
    for i, tokens in enumerate(pages):
        overlap = len(number_set & tokens)
        if overlap > best_overlap:
            best_index, best_overlap = i, overlap

    if best_overlap == 0:
        return None
    return LayoutEvidence(
        pdf_filename=pdf_path.name,
        page_number=best_index + 1,
        matched_count=best_overlap,
        total_count=len(number_set),
    )


def render_pdf_page_preview(pdf_path: Path, page_number: int, max_dimension: int = 900) -> bytes:
    """Renders one PDF page (1-indexed) to PNG bytes for a read-only, local
    preview in the review screen. Never writes to or modifies the PDF."""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_number - 1]
        rect = page.rect
        scale = max_dimension / max(rect.width, rect.height)
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix)
        buffer = io.BytesIO(pixmap.tobytes("png"))
        return buffer.getvalue()
    finally:
        doc.close()
