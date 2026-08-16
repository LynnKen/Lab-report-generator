"""Read-only view over human-approved reference caption blocks, for both a
development run and a frozen/production build.

Two data sources, chosen automatically via config.USE_APPROVED_REFERENCE_PACK
(True in a frozen build, see config.py):

- DEV (live dataset): scans data/reference_examples/<category>/<report-id>/
  captions.he.json at CALL TIME. There is no static/cached index - a block
  approved five minutes ago (via reference_review_service.py's review
  screen) is available immediately, with no rebuild step. Visual previews
  are rendered on demand from the original photo/crop.
- PRODUCTION (derived pack): reads the small, pre-built
  data/approved_reference_pack/pack.json + previews/*.jpg produced by
  build_approved_reference_pack.py. A frozen build never bundles the raw
  dataset (PDFs, full-size originals, unapproved blocks, review audit
  history) - see ReportGenerator.spec - so it must never try to read it.

This module is a read-only CONSUMER used by ai_service.py to build the
"approved reference" section of the photo-captioning prompt (text and,
optionally, visual few-shot examples), and by app.py to show a small
transparency note. It never edits captions.he.json - reviewing/editing stays
reference_review_service.py's job - and it never overwrites, normalizes,
shortens, or rewrites approved_text_he; it is copied through byte-for-byte.

Safety notes (see also ai_service.py's prompt rules, which remain the actual
enforcement point):
- A multi-image block's approved_text_he describes several photos together
  and must never be presented as an exact caption for one specific image -
  callers must check `is_single_image` / `is_multi_image`. Only single-image
  blocks are ever offered as visual (image+caption) examples - see
  build_visual_reference_payload().
- Only status == "approved" blocks are ever considered; draft, needs_edit,
  cannot_determine, and not_reviewed blocks are always skipped, in both data
  sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import config

APPROVED_STATUS = "approved"

_PreviewLoader = Callable[[int], Optional[bytes]]


def _no_preview(_max_dimension: int) -> Optional[bytes]:
    return None


@dataclass(frozen=True)
class ApprovedImageRef:
    """One image backing an approved block. variant_type matters for any
    image-aware captioning: a "crop" instance is the labeled visual
    reference for its own framed region, never the full source photo it was
    cropped from.

    How the actual preview bytes are produced is an implementation detail
    hidden behind load_preview_bytes() - dev mode renders on demand from the
    original photo/crop, production mode reads a pre-built file from the
    pack. Callers never need to know which."""

    variant_type: str  # "full" | "crop"
    _loader: _PreviewLoader = field(default=_no_preview, repr=False, compare=False)

    def load_preview_bytes(self, max_dimension: int) -> Optional[bytes]:
        """Returns JPEG bytes for this image, bounded to max_dimension on its
        longest side, or None if unavailable (missing/unreadable source,
        multi-image block with no rendered preview, etc.) - never raises."""
        try:
            return self._loader(max_dimension)
        except Exception:
            return None


@dataclass(frozen=True)
class ApprovedReferenceBlock:
    """One human-approved caption block."""

    category: str
    report_id: str
    group_id: str
    block_id: str
    document_photo_label: str
    approved_text_he: str
    image_instances: Tuple[ApprovedImageRef, ...]
    source_path: str  # for traceability/logging only - never shown to end users

    @property
    def image_count(self) -> int:
        return len(self.image_instances)

    @property
    def is_single_image(self) -> bool:
        return self.image_count == 1

    @property
    def is_multi_image(self) -> bool:
        return self.image_count > 1


def _safe_load(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------- dev (live dataset)

def _iter_live_caption_files() -> Iterator[Path]:
    root = config.REFERENCE_EXAMPLES_DIR
    if not root.exists():
        return
    # data/reference_examples/<category>/<report_id>/captions.he.json
    yield from sorted(root.glob("*/*/captions.he.json"))


def _relative_source_path(path: Path) -> str:
    try:
        return str(path.relative_to(config.BASE_DIR))
    except ValueError:
        return str(path)


def _make_live_loader(report_id: str, source_file: str, crop_dict: Optional[dict]) -> _PreviewLoader:
    def _load(max_dimension: int) -> Optional[bytes]:
        import reference_review_service as rrs  # local import: dev-only dependency

        if crop_dict:
            crop = rrs.CropBox(
                left=float(crop_dict["left"]),
                top=float(crop_dict["top"]),
                width=float(crop_dict["width"]),
                height=float(crop_dict["height"]),
            )
            return rrs.render_crop_preview_bytes(report_id, source_file, crop, max_dimension=max_dimension)
        return rrs.render_source_image_preview(report_id, source_file, max_dimension=max_dimension)

    return _load


def _iter_live_approved_blocks() -> Iterator[ApprovedReferenceBlock]:
    """Yields every currently-approved reference block from the live,
    developer-only dataset, re-scanning from disk on every call. Silently
    skips files/blocks that are missing, malformed, or below the approval
    bar - this is a best-effort read layer, never a validator
    (reference_review_service.py already enforces integrity when a human
    saves a review). Also used directly by build_approved_reference_pack.py
    to source the production pack - always the LIVE dataset, regardless of
    config.USE_APPROVED_REFERENCE_PACK."""
    for path in _iter_live_caption_files():
        data = _safe_load(path)
        if not isinstance(data, dict):
            continue
        category = data.get("category") or path.parent.parent.name
        report_id = data.get("report_id") or path.parent.name
        for group in data.get("caption_groups", []) or []:
            if not isinstance(group, dict):
                continue
            group_id = group.get("group_id", "")
            for block in group.get("source_blocks", []) or []:
                if not isinstance(block, dict):
                    continue
                if block.get("status") != APPROVED_STATUS:
                    continue
                text = block.get("approved_text_he") or ""
                if not text.strip():
                    continue
                raw_instances = block.get("image_instances")
                if raw_instances is None:
                    # Backward-compat for any not-yet-migrated file: treat a
                    # legacy plain filename list as full-image instances.
                    raw_instances = [
                        {"source_file": name, "variant_type": "full"}
                        for name in (block.get("image_files") or [])
                        if isinstance(name, str)
                    ]
                images = tuple(
                    ApprovedImageRef(
                        variant_type=inst.get("variant_type", "full"),
                        _loader=_make_live_loader(report_id, inst["source_file"], inst.get("crop")),
                    )
                    for inst in raw_instances
                    if isinstance(inst, dict)
                    and isinstance(inst.get("source_file"), str)
                    and inst["source_file"].strip()
                )
                if not images:
                    continue
                yield ApprovedReferenceBlock(
                    category=category,
                    report_id=report_id,
                    group_id=group_id,
                    block_id=block.get("block_id", ""),
                    document_photo_label=block.get("document_photo_label", block.get("block_id", "")),
                    approved_text_he=text,
                    image_instances=images,
                    source_path=_relative_source_path(path),
                )


# ------------------------------------------------ production (derived pack)

def _make_pack_loader(preview_path: Path) -> _PreviewLoader:
    def _load(_max_dimension: int) -> Optional[bytes]:
        # Already bounded to APPROVED_REFERENCE_PREVIEW_MAX_DIMENSION_PX at
        # build time (see build_approved_reference_pack.py) - just read it.
        try:
            return preview_path.read_bytes()
        except OSError:
            return None

    return _load


def _iter_pack_approved_blocks() -> Iterator[ApprovedReferenceBlock]:
    """Yields every block listed in the derived production pack. The pack
    itself is the enforcement point for "approved only, no PDFs, no
    full-size originals, no unapproved blocks" - this reader trusts it and
    only guards against a missing/malformed pack (never crashes captioning)."""
    pack_dir = config.APPROVED_REFERENCE_PACK_DIR
    pack_path = pack_dir / "pack.json"
    data = _safe_load(pack_path)
    if not isinstance(data, dict):
        return
    previews_dir = pack_dir / "previews"
    for entry in data.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get("approved_text_he") or ""
        if not text.strip():
            continue
        raw_images = entry.get("images") or []
        images = tuple(
            ApprovedImageRef(
                variant_type=img.get("variant_type", "full"),
                _loader=(
                    _make_pack_loader(previews_dir / img["preview_file"])
                    if img.get("preview_file")
                    else _no_preview
                ),
            )
            for img in raw_images
            if isinstance(img, dict)
        )
        if not images:
            continue
        yield ApprovedReferenceBlock(
            category=entry.get("category", ""),
            report_id=entry.get("report_id", ""),
            group_id=entry.get("group_id", ""),
            block_id=entry.get("block_id", ""),
            document_photo_label=entry.get("document_photo_label", entry.get("block_id", "")),
            approved_text_he=text,
            image_instances=images,
            source_path="approved_reference_pack/pack.json",
        )


# --------------------------------------------------------------- dispatcher

def iter_approved_blocks() -> Iterator[ApprovedReferenceBlock]:
    """Yields every currently-approved reference block from whichever data
    source is active (config.USE_APPROVED_REFERENCE_PACK)."""
    if config.USE_APPROVED_REFERENCE_PACK:
        yield from _iter_pack_approved_blocks()
    else:
        yield from _iter_live_approved_blocks()


def list_approved_blocks() -> List[ApprovedReferenceBlock]:
    return list(iter_approved_blocks())


@dataclass(frozen=True)
class ReferenceSelection:
    """A bounded, deterministic pick of approved examples for one caption
    request. The two pools are kept separate (never merged into one
    undifferentiated list) so callers can never accidentally present a
    multi-image group caption as a single-image ground truth."""

    single_image_examples: Tuple[ApprovedReferenceBlock, ...]
    multi_image_examples: Tuple[ApprovedReferenceBlock, ...]

    @property
    def all(self) -> Tuple[ApprovedReferenceBlock, ...]:
        return self.single_image_examples + self.multi_image_examples

    @property
    def count(self) -> int:
        return len(self.single_image_examples) + len(self.multi_image_examples)


def _sort_key(block: ApprovedReferenceBlock) -> tuple:
    return (block.category, block.report_id, block.group_id, block.block_id)


def _round_robin_by_category(
    blocks: Sequence[ApprovedReferenceBlock],
) -> List[ApprovedReferenceBlock]:
    """Deterministically reorders blocks so categories interleave (blocks
    within a category keep their relative order) - gives category diversity
    among the first N picks without any randomness, so the same approved
    dataset always yields the same selection."""
    by_category: dict[str, list[ApprovedReferenceBlock]] = {}
    category_order: list[str] = []
    for block in blocks:
        if block.category not in by_category:
            by_category[block.category] = []
            category_order.append(block.category)
        by_category[block.category].append(block)

    result: list[ApprovedReferenceBlock] = []
    while any(by_category[c] for c in category_order):
        for c in category_order:
            bucket = by_category[c]
            if bucket:
                result.append(bucket.pop(0))
    return result


def select_reference_examples(
    blocks: Optional[Sequence[ApprovedReferenceBlock]] = None,
    *,
    max_examples: Optional[int] = None,
) -> ReferenceSelection:
    """Deterministic, bounded selection of approved examples for one new
    caption request.

    - Single-image blocks are preferred first, since their approved_text_he
      genuinely describes one photo and can stand as a direct caption-style
      example.
    - Multi-image blocks only ever fill remaining budget, and only ever as
      group-level style/context examples - never as an exact label for any
      one image.
    - Category diversity is applied within each pool via round-robin, so one
      heavily-annotated report doesn't crowd out the others.
    - Selection is fully deterministic (sorted by category/report/group/
      block before the round-robin) - no randomness - which keeps this easy
      to test and easy to extend later: once explicit per-photo tags exist,
      they can filter/rank this same candidate list before selection runs,
      instead of replacing this function.
    """
    if blocks is None:
        blocks = list_approved_blocks()
    if max_examples is None:
        max_examples = config.APPROVED_REFERENCE_MAX_EXAMPLES
    max_examples = max(0, max_examples)

    singles = sorted((b for b in blocks if b.is_single_image), key=_sort_key)
    multi = sorted((b for b in blocks if b.is_multi_image), key=_sort_key)
    singles = _round_robin_by_category(singles)
    multi = _round_robin_by_category(multi)

    chosen_singles = tuple(singles[:max_examples])
    remaining = max(0, max_examples - len(chosen_singles))
    chosen_multi = tuple(multi[:remaining])

    return ReferenceSelection(single_image_examples=chosen_singles, multi_image_examples=chosen_multi)


def build_visual_reference_payload(
    blocks: Optional[Sequence[ApprovedReferenceBlock]] = None,
    *,
    max_examples: Optional[int] = None,
) -> Tuple[Tuple[ApprovedReferenceBlock, ...], str]:
    """For APPROVED_REFERENCE_MODE=visual: returns (visual_examples,
    text_context_he).

    - visual_examples are the same bounded, category-diverse selection
      select_reference_examples() would pick as single-image examples -
      meant to be sent as real image+caption few-shot pairs alongside the
      target photo. Multi-image blocks are NEVER included here.
    - text_context_he is a short supplementary text block built ONLY from
      whichever multi-image blocks would also have been selected in text
      mode (filling the same budget) - their stylistic value isn't wasted,
      but they never become an image. It never repeats the visual examples'
      captions as text, since those already appear alongside their own
      image in the request.
    """
    if blocks is None:
        blocks = list_approved_blocks()
    if max_examples is None:
        max_examples = config.APPROVED_REFERENCE_MAX_EXAMPLES

    full_selection = select_reference_examples(blocks, max_examples=max_examples)
    text_only_selection = ReferenceSelection(
        single_image_examples=(), multi_image_examples=full_selection.multi_image_examples
    )
    text_context = build_reference_context_he(text_only_selection)
    return full_selection.single_image_examples, text_context


_REFERENCE_CONTEXT_HEADER_HE = (
    "להלן דוגמאות כיתוב מאושרות מדוחות קודמים, לצורך סגנון כתיבה ומינוח הנדסי "
    "בלבד. אלו אינן עדות לגבי התמונה הנוכחית: אין להעתיק מהן פרט טכני, נזק, "
    "מדידה, מנגנון כשל, סיבה או מסקנה, אלא אם כללי הבטיחות שהוגדרו למעלה "
    "מתירים זאת במפורש (הם אינם מתירים זאת)."
)


def _format_example_he(block: ApprovedReferenceBlock, index: int) -> str:
    if block.is_single_image:
        kind = "כיתוב מאושר לתמונה בודדת"
    else:
        kind = f"כיתוב מאושר לקבוצה של {block.image_count} תמונות (הקשר סגנוני בלבד - לא תווית מדויקת לתמונה בודדת)"
    return f'{index}. [{kind}] "{block.approved_text_he}"'


def build_reference_context_he(
    selection: ReferenceSelection,
    *,
    max_chars: Optional[int] = None,
) -> str:
    """Renders the selection into a compact Hebrew text block for the prompt.

    Never truncates an individual approved_text_he mid-string (that would be
    a subtle rewrite of approved text) - instead it greedily includes whole
    examples until the next one would exceed max_chars, then stops. Returns
    "" if the selection is empty or none fit."""
    if max_chars is None:
        max_chars = config.APPROVED_REFERENCE_MAX_CONTEXT_CHARS
    if selection.count == 0 or max_chars <= 0:
        return ""

    lines = [_REFERENCE_CONTEXT_HEADER_HE]
    budget = max_chars - len(_REFERENCE_CONTEXT_HEADER_HE)
    included = 0
    for i, block in enumerate(selection.all, start=1):
        line = _format_example_he(block, i)
        addition = len(line) + 1  # +1 for the joining newline
        if addition > budget:
            break
        lines.append(line)
        budget -= addition
        included += 1

    if included == 0:
        return ""
    return "\n".join(lines)
