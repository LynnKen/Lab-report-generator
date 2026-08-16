"""Local reviewer workflow over the curated reference dataset
(data/reference_examples/ - see README.he.md there).

Lets a qualified human map each individual document photo-number (e.g.
"1.1", "1.4") - or a document-confirmed contiguous range like "1.1-1.3" a
prior reviewer already split out - to its real photo file(s), correct the
matching wording if needed, and record a review decision. This module never
invents or infers a mapping, a caption, or an engineering conclusion - it
only loads, edits in memory, validates, and persists exactly what a human
reviewer decided, using layout_extraction.py's DOCX/PDF cross-checked
evidence only as a starting hint, never as an automatic answer.

Data lives in each report's captions.he.json (schema:
data/reference_examples/schemas/example.schema.json). Both the group's own
`original_text_he` and every source_blocks entry's own `original_text_he`
are immutable - the review screen only ever writes to a block's
`approved_text_he`, `image_files`, and `status`.
"""

from __future__ import annotations

import datetime
import io
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

import config
import layout_extraction
import photo_service
from layout_extraction import LayoutEvidence

STATUS_LABELS_HE: Dict[str, str] = {
    "not_reviewed": "טרם נבדק",
    "approved": "אושר",
    "needs_edit": "דרושה עריכה",
    "cannot_determine": "לא ניתן לקבוע",
}

CONFIDENCE_LABELS_HE: Dict[str, str] = {
    "human_annotated": "פוצל ואומת ידנית על ידי בודק",
    "numbers_confirmed_text_unsplit": "מספרי התמונה אומתו מול המסמך, אך הטקסט טרם פוצל לפיהם",
    "uncertain": "לא נמצא אימות אמין למספור",
}

DECISIONS = ("approved", "needs_edit", "cannot_determine", "save")

VARIANT_TYPES = ("full", "crop")

CANNOT_DETERMINE_REASONS = (
    "missing_source_image",
    "mapping_ambiguous",
    "source_image_unreadable",
    "other",
)

CANNOT_DETERMINE_REASON_LABELS_HE: Dict[str, str] = {
    "missing_source_image": "קובץ התמונה המקורי חסר בתיקיית images",
    "mapping_ambiguous": "לא ניתן לקבוע שיוך חד-משמעי בין המספור לתמונה",
    "source_image_unreadable": "קובץ התמונה פגום או לא קריא",
    "other": "אחר",
}


class ReferenceReviewError(ValueError):
    """Raised with a Hebrew-friendly message for any workflow error."""


class ReviewValidationError(ReferenceReviewError):
    """Raised when a decision would violate a data-integrity rule (e.g.
    approving a block with no assigned image and no explicit
    'cannot_determine')."""


class FullImageAlreadyAssignedError(ReferenceReviewError):
    """Raised by assign_full_image when the source file already has at least
    one existing use elsewhere in the report and the caller has not passed
    confirm_existing_use=True. Carries the existing uses so the UI can show
    them and ask the reviewer to explicitly confirm before proceeding - a
    full-image assignment must never silently duplicate or shadow another
    block's mapping."""

    def __init__(self, source_file: str, existing_uses: List["SourceUsage"]):
        self.source_file = source_file
        self.existing_uses = existing_uses
        labels = ", ".join(f"{u.document_photo_label} ({u.variant_type})" for u in existing_uses)
        super().__init__(
            f"התמונה '{source_file}' כבר משויכת ל: {labels}. יש לאשר במפורש שיוך תמונה מלאה נוספת."
        )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _new_instance_id(block_id: str) -> str:
    return f"{block_id}:{uuid.uuid4().hex[:8]}"


@dataclass
class CropBox:
    """Normalized (0.0-1.0) crop rectangle, relative to the orientation-
    corrected source image - never pixel coordinates, so it stays valid
    regardless of any future re-export/resize of the source file itself."""

    left: float
    top: float
    width: float
    height: float

    def validate(self) -> None:
        for name, value in (
            ("left", self.left),
            ("top", self.top),
            ("width", self.width),
            ("height", self.height),
        ):
            if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                raise ReferenceReviewError(f"ערך חיתוך לא תקין ({name}={value!r}) - חייב להיות בין 0 ל-1.")
        if self.width <= 0 or self.height <= 0:
            raise ReferenceReviewError("רוחב וגובה מסגרת החיתוך חייבים להיות גדולים מאפס.")
        if self.left + self.width > 1.0 + 1e-6:
            raise ReferenceReviewError("מסגרת החיתוך חורגת מגבול התמונה המקורית (רוחב).")
        if self.top + self.height > 1.0 + 1e-6:
            raise ReferenceReviewError("מסגרת החיתוך חורגת מגבול התמונה המקורית (גובה).")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["CropBox"]:
        if not data:
            return None
        return cls(
            left=float(data["left"]),
            top=float(data["top"]),
            width=float(data["width"]),
            height=float(data["height"]),
        )

    def to_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


@dataclass
class ImageInstance:
    """One image AS IT APPEARS in the report - either the full source photo,
    or one non-destructive crop of it. The original file in images/ is never
    modified by creating, editing, or deleting an instance; a crop is purely
    a stored rectangle interpreted at render time."""

    instance_id: str
    source_file: str
    variant_type: str  # "full" | "crop"
    crop: Optional[CropBox] = None
    display_order: int = 0
    preview_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ImageInstance":
        return cls(
            instance_id=data["instance_id"],
            source_file=data["source_file"],
            variant_type=data.get("variant_type", "full"),
            crop=CropBox.from_dict(data.get("crop")),
            display_order=int(data.get("display_order", 0)),
            preview_path=data.get("preview_path"),
        )

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "source_file": self.source_file,
            "variant_type": self.variant_type,
            "crop": self.crop.to_dict() if self.crop else None,
            "display_order": self.display_order,
            "preview_path": self.preview_path,
        }


@dataclass
class SourceUsage:
    """One place a source_file is currently used across a report - used to
    answer 'view existing uses' and to gate re-assigning a full image."""

    group_id: str
    block_id: str
    document_photo_label: str
    instance_id: str
    variant_type: str


@dataclass
class SourceBlock:
    block_id: str
    document_photo_label: str
    document_photo_refs: List[str]
    original_text_he: str
    approved_text_he: str
    image_instances: List[ImageInstance] = field(default_factory=list)
    status: str = "not_reviewed"
    extraction_confidence: str = "uncertain"
    ambiguity_notes: str = ""
    layout_evidence: Optional[LayoutEvidence] = None
    cannot_determine_reason: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewer_name: Optional[str] = None
    audit_log: List[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "SourceBlock":
        review = data.get("review") or {}
        block_id = data["block_id"]
        raw_instances = data.get("image_instances")
        if raw_instances is not None:
            instances = [ImageInstance.from_dict(i) for i in raw_instances]
        else:
            # Legacy migration: an old plain image_files list becomes one
            # "full" instance per filename, in the same order - no crop data
            # existed before, so nothing is lost or guessed.
            instances = [
                ImageInstance(
                    instance_id=f"{block_id}:legacy-{i}",
                    source_file=name,
                    variant_type="full",
                    crop=None,
                    display_order=i,
                )
                for i, name in enumerate(data.get("image_files", []))
            ]
        return cls(
            block_id=block_id,
            document_photo_label=data.get("document_photo_label", block_id),
            document_photo_refs=list(data.get("document_photo_refs", [])),
            original_text_he=data.get("original_text_he", ""),
            approved_text_he=data.get("approved_text_he", data.get("original_text_he", "")),
            image_instances=instances,
            status=data.get("status", "not_reviewed"),
            extraction_confidence=data.get("extraction_confidence", "uncertain"),
            ambiguity_notes=data.get("ambiguity_notes", ""),
            layout_evidence=LayoutEvidence.from_dict(data.get("layout_evidence")),
            cannot_determine_reason=data.get("cannot_determine_reason"),
            reviewed_at=review.get("reviewed_at"),
            reviewer_name=review.get("reviewer_name"),
            audit_log=list(review.get("audit_log", [])),
        )

    def to_dict(self) -> dict:
        ordered = sorted(self.image_instances, key=lambda i: i.display_order)
        return {
            "block_id": self.block_id,
            "document_photo_label": self.document_photo_label,
            "document_photo_refs": list(self.document_photo_refs),
            "original_text_he": self.original_text_he,
            "approved_text_he": self.approved_text_he,
            "image_instances": [i.to_dict() for i in ordered],
            "status": self.status,
            "extraction_confidence": self.extraction_confidence,
            "ambiguity_notes": self.ambiguity_notes,
            "layout_evidence": self.layout_evidence.to_dict() if self.layout_evidence else None,
            "cannot_determine_reason": self.cannot_determine_reason,
            "review": {
                "reviewed_at": self.reviewed_at,
                "reviewer_name": self.reviewer_name,
                "audit_log": list(self.audit_log),
            },
        }

    def is_resolved(self) -> bool:
        return bool(self.image_instances) or self.status == "cannot_determine"

    def find_instance(self, instance_id: str) -> ImageInstance:
        for instance in self.image_instances:
            if instance.instance_id == instance_id:
                return instance
        raise ReferenceReviewError(f"תמונה לא נמצאה בבלוק: {instance_id}")


@dataclass
class CaptionGroup:
    group_id: str
    original_text_he: str
    notes: str
    source_blocks: List[SourceBlock]
    legacy_unmapped_images: List[str] = field(default_factory=list)
    legacy_group_review: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "CaptionGroup":
        blocks = [SourceBlock.from_dict(b) for b in data.get("source_blocks", [])]
        return cls(
            group_id=data["group_id"],
            original_text_he=data.get("original_text_he", ""),
            notes=data.get("notes", ""),
            source_blocks=blocks,
            legacy_unmapped_images=list(data.get("legacy_unmapped_images", [])),
            legacy_group_review=dict(data.get("legacy_group_review", {})),
        )

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "original_text_he": self.original_text_he,
            "notes": self.notes,
            "source_blocks": [b.to_dict() for b in self.source_blocks],
            "legacy_unmapped_images": list(self.legacy_unmapped_images),
            "legacy_group_review": dict(self.legacy_group_review),
        }

    def find_block(self, block_id: str) -> SourceBlock:
        for block in self.source_blocks:
            if block.block_id == block_id:
                return block
        raise ReferenceReviewError(f"בלוק מספור לא נמצא: {block_id}")

    def status_summary(self) -> str:
        """A derived (never stored) rollup of this group's block statuses,
        for display only - approval is always tracked per block."""
        statuses = [b.status for b in self.source_blocks]
        if not statuses:
            return "not_reviewed"
        if all(s == "approved" for s in statuses):
            return "approved"
        if all(s == "cannot_determine" for s in statuses):
            return "cannot_determine"
        if all(s == "not_reviewed" for s in statuses):
            return "not_reviewed"
        return "needs_edit"


@dataclass
class ReferenceReport:
    report_id: str
    category: str
    source_report_path: str
    caption_groups: List[CaptionGroup]
    file_path: Path
    report_dir: Path

    def find_group(self, group_id: str) -> CaptionGroup:
        for group in self.caption_groups:
            if group.group_id == group_id:
                return group
        raise ReferenceReviewError(f"קבוצת כיתוב לא נמצאה: {group_id}")

    def all_blocks(self):
        for group in self.caption_groups:
            for block in group.source_blocks:
                yield group, block


def _index() -> dict:
    index_path = config.REFERENCE_EXAMPLES_DIR / "index.json"
    return json.loads(index_path.read_text(encoding="utf-8"))


def _index_entry(report_id: str) -> dict:
    for entry in _index()["reports"]:
        if entry["report_id"] == report_id:
            return entry
    raise ReferenceReviewError(f"דוח ייחוס לא נמצא באינדקס: {report_id}")


def _report_dir(report_id: str) -> Path:
    entry = _index_entry(report_id)
    return config.REFERENCE_EXAMPLES_DIR / entry["category"] / report_id


def report_progress(report: ReferenceReport) -> tuple[int, int]:
    """(approved_block_count, total_block_count) across the whole report -
    approval is tracked per photo-number block, not per merged group."""
    blocks = [b for _, b in report.all_blocks()]
    approved = sum(1 for b in blocks if b.status == "approved")
    return approved, len(blocks)


def progress_text_he(report: ReferenceReport) -> str:
    approved, total = report_progress(report)
    return f"{approved} מתוך {total} בלוקים אושרו"


def list_review_reports() -> List[dict]:
    """Summary rows for the report-selection step: id, category, and
    progress, for every report listed in index.json, in index order."""
    summaries: List[dict] = []
    for entry in _index()["reports"]:
        report = load_report(entry["report_id"])
        approved, total = report_progress(report)
        summaries.append(
            {
                "report_id": entry["report_id"],
                "category": report.category,
                "block_count": total,
                "approved_count": approved,
                "progress_text_he": progress_text_he(report),
            }
        )
    return summaries


def load_report(report_id: str) -> ReferenceReport:
    entry = _index_entry(report_id)
    report_dir = _report_dir(report_id)
    captions_path = report_dir / "captions.he.json"
    if not captions_path.exists():
        raise ReferenceReviewError(f"קובץ כיתובים לא נמצא עבור {report_id}")
    data = json.loads(captions_path.read_text(encoding="utf-8"))
    groups = [CaptionGroup.from_dict(g) for g in data.get("caption_groups", [])]
    return ReferenceReport(
        report_id=data.get("report_id", report_id),
        category=data.get("category", entry["category"]),
        source_report_path=data.get("source_report_path", entry.get("source_report_path", "")),
        caption_groups=groups,
        file_path=captions_path,
        report_dir=report_dir,
    )


def save_report(report: ReferenceReport) -> None:
    payload = {
        "report_id": report.report_id,
        "category": report.category,
        "source_report_path": report.source_report_path,
        "caption_groups": [g.to_dict() for g in report.caption_groups],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    report.file_path.write_text(text, encoding="utf-8", newline="\n")


def list_candidate_images(report_id: str) -> List[Path]:
    """Original high-resolution photos available for mapping. Empty until a
    human has manually copied files into the report's images/ folder - see
    README.he.md - this never fails, it just returns an empty list."""
    images_dir = _report_dir(report_id) / "images"
    if not images_dir.exists():
        return []
    return sorted(
        p
        for p in images_dir.iterdir()
        if p.is_file() and p.suffix.lower() in config.ALLOWED_PHOTO_EXTENSIONS
    )


def images_dir_for(report_id: str) -> Path:
    return _report_dir(report_id) / "images"


def _source_image_path(report_id: str, source_file: str) -> Path:
    path = images_dir_for(report_id) / source_file
    if not path.exists():
        raise ReferenceReviewError(f"קובץ תמונת מקור לא נמצא: {source_file}")
    return path


def image_pixel_size(report_id: str, source_file: str) -> tuple[int, int]:
    """Size (width, height) of the orientation-corrected source image - the
    same pixel space normalized crop coordinates are relative to."""
    img = photo_service.open_corrected_image(_source_image_path(report_id, source_file))
    return img.size


def render_source_image_preview(report_id: str, source_file: str, max_dimension: int = 700) -> bytes:
    """Read-only rendering for the crop/preview UI: orientation-corrected,
    resized, JPEG-encoded bytes. Never writes to disk, never touches the
    original file."""
    img = photo_service.open_corrected_image(_source_image_path(report_id, source_file))
    scale = min(1.0, max_dimension / max(img.width, img.height))
    if scale < 1.0:
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS
        )
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def render_crop_preview_bytes(
    report_id: str, source_file: str, crop: CropBox, max_dimension: int = 400
) -> bytes:
    """Renders just the cropped region as JPEG bytes, purely for on-screen
    preview - the crop rectangle is never baked into any file on disk."""
    img = photo_service.open_corrected_image(_source_image_path(report_id, source_file))
    width, height = img.size
    left = max(0, min(width - 1, round(crop.left * width)))
    top = max(0, min(height - 1, round(crop.top * height)))
    right = max(left + 1, min(width, round((crop.left + crop.width) * width)))
    bottom = max(top + 1, min(height, round((crop.top + crop.height) * height)))
    cropped = img.crop((left, top, right, bottom))
    scale = min(1.0, max_dimension / max(cropped.width, cropped.height))
    if scale < 1.0:
        cropped = cropped.resize(
            (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale))), Image.LANCZOS
        )
    buffer = io.BytesIO()
    cropped.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def find_layout_pdf(report_id: str) -> Optional[Path]:
    return layout_extraction.find_layout_pdf(_report_dir(report_id))


def render_layout_page_preview(report_id: str, page_number: int, max_dimension: int = 900) -> bytes:
    pdf_path = find_layout_pdf(report_id)
    if pdf_path is None:
        raise ReferenceReviewError("לא נמצא קובץ פריסה (layout PDF) עבור דוח זה.")
    return layout_extraction.render_pdf_page_preview(pdf_path, page_number, max_dimension)


def apply_block_decision(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    *,
    approved_text_he: str,
    decision: str,
    reviewer_name: str = "",
) -> SourceBlock:
    """Applies one reviewer decision to a single photo-number block, in
    memory (call save_report to persist). Never overwrites the block's
    original_text_he. Raises ReviewValidationError if 'approved' is chosen
    with no assigned image and no explicit 'cannot_determine'."""
    if decision not in DECISIONS:
        raise ReferenceReviewError(f"פעולת בדיקה לא מוכרת: {decision}")

    group = report.find_group(group_id)
    block = group.find_block(block_id)

    normalized_text = (approved_text_he or "").strip()
    changed_text = normalized_text != block.approved_text_he
    block.approved_text_he = normalized_text

    if decision == "approved" and not block.is_resolved():
        raise ReviewValidationError(
            f"לא ניתן לאשר את בלוק {block.document_photo_label} ללא תמונה משויכת "
            "או סימון מפורש 'לא ניתן לקבוע'."
        )

    if decision != "save":
        block.status = decision

    clean_reviewer = (reviewer_name or "").strip()
    if clean_reviewer:
        block.reviewer_name = clean_reviewer

    now = _now_iso()
    block.reviewed_at = now
    block.audit_log.append(
        {
            "at": now,
            "reviewer_name": clean_reviewer or block.reviewer_name,
            "decision": decision,
            "changed_text": changed_text,
        }
    )
    return block


def find_source_file_usages(report: ReferenceReport, source_file: str) -> List[SourceUsage]:
    """Every place source_file is currently used ANYWHERE in the report - one
    photo can legitimately back several blocks (a full view in one block, a
    crop of the same photo in another), so this is informational, never
    exclusive."""
    usages: List[SourceUsage] = []
    for group in report.caption_groups:
        for block in group.source_blocks:
            for instance in block.image_instances:
                if instance.source_file == source_file:
                    usages.append(
                        SourceUsage(
                            group_id=group.group_id,
                            block_id=block.block_id,
                            document_photo_label=block.document_photo_label,
                            instance_id=instance.instance_id,
                            variant_type=instance.variant_type,
                        )
                    )
    return usages


def _touch_instance_change(block: SourceBlock, reviewer_name: str) -> None:
    """Shared bookkeeping after any instance add/remove/edit: having (or not
    having) an image is never set automatically by these calls - only the
    reviewer's explicit decision buttons set status to approved - a change
    always leaves the block at needs_edit/not_reviewed until re-confirmed."""
    if block.image_instances:
        block.status = "needs_edit"
        block.cannot_determine_reason = None
    elif block.status != "cannot_determine":
        block.status = "not_reviewed"

    clean_reviewer = (reviewer_name or "").strip()
    if clean_reviewer:
        block.reviewer_name = clean_reviewer
    block.reviewed_at = _now_iso()


def assign_full_image(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    source_file: str,
    *,
    reviewer_name: str = "",
    confirm_existing_use: bool = False,
) -> ImageInstance:
    """Adds a new "full image" instance of source_file to this block. Never
    removes it from any other block - a source photo may legitimately back
    several blocks (see find_source_file_usages). If the file already has
    any existing use anywhere in the report, raises
    FullImageAlreadyAssignedError unless confirm_existing_use=True, so a
    full-image assignment is never silent."""
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    name = (source_file or "").strip()
    if not name:
        raise ReferenceReviewError("שם קובץ תמונה ריק.")

    existing_uses = find_source_file_usages(report, name)
    if existing_uses and not confirm_existing_use:
        raise FullImageAlreadyAssignedError(name, existing_uses)

    instance = ImageInstance(
        instance_id=_new_instance_id(block_id),
        source_file=name,
        variant_type="full",
        crop=None,
        display_order=len(block.image_instances),
    )
    block.image_instances.append(instance)
    _touch_instance_change(block, reviewer_name)
    return instance


def add_crop_instance(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    source_file: str,
    crop: CropBox,
    *,
    reviewer_name: str = "",
) -> ImageInstance:
    """Adds a new non-destructive crop instance. The same source_file may
    already have other crops (or a full instance) in this or other blocks -
    that is expected and never blocked; each crop is distinct visual
    evidence of its own. Never touches the original file on disk."""
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    name = (source_file or "").strip()
    if not name:
        raise ReferenceReviewError("שם קובץ תמונה ריק.")
    crop.validate()

    instance = ImageInstance(
        instance_id=_new_instance_id(block_id),
        source_file=name,
        variant_type="crop",
        crop=crop,
        display_order=len(block.image_instances),
    )
    block.image_instances.append(instance)
    _touch_instance_change(block, reviewer_name)
    return instance


def update_crop_instance(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    instance_id: str,
    crop: CropBox,
    *,
    reviewer_name: str = "",
) -> ImageInstance:
    """Edits an existing crop instance's rectangle in place (same
    instance_id, same position in the block). Never touches the original
    file."""
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    instance = block.find_instance(instance_id)
    if instance.variant_type != "crop":
        raise ReferenceReviewError("ניתן לערוך רק חיתוך - לא תמונה מלאה.")
    crop.validate()
    instance.crop = crop
    _touch_instance_change(block, reviewer_name)
    return instance


def remove_image_instance(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    instance_id: str,
    *,
    reviewer_name: str = "",
) -> None:
    """Removes exactly one instance from this block. Never affects the
    original source file, and never affects any other instance of the same
    source_file in this or any other block."""
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    block.find_instance(instance_id)  # raises if not found
    block.image_instances = [i for i in block.image_instances if i.instance_id != instance_id]
    for i, instance in enumerate(block.image_instances):
        instance.display_order = i
    _touch_instance_change(block, reviewer_name)


def reorder_image_instance(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    instance_id: str,
    new_index: int,
) -> None:
    """Moves one instance to a new position within its own block only."""
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    instances = sorted(block.image_instances, key=lambda i: i.display_order)
    positions = {i.instance_id: idx for idx, i in enumerate(instances)}
    if instance_id not in positions:
        raise ReferenceReviewError(f"תמונה לא נמצאה בבלוק: {instance_id}")
    current_index = positions[instance_id]
    new_index = max(0, min(new_index, len(instances) - 1))
    if new_index == current_index:
        return
    moved = instances.pop(current_index)
    instances.insert(new_index, moved)
    for i, instance in enumerate(instances):
        instance.display_order = i
    block.image_instances = instances


def mark_block_cannot_determine(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    *,
    reason: str = "other",
    reviewer_name: str = "",
) -> SourceBlock:
    """Records that a block's photo(s) cannot be reliably identified, with a
    structured reason, instead of leaving it silently unmapped or guessing.
    The block (and its immutable original_text_he) is preserved - a reviewer
    can reopen it later by simply assigning an image, which clears this
    status and reason automatically (see _touch_instance_change)."""
    if reason not in CANNOT_DETERMINE_REASONS:
        raise ReferenceReviewError(f"סיבה לא מוכרת ל'לא ניתן לקבוע': {reason}")
    group = report.find_group(group_id)
    block = group.find_block(block_id)
    block.image_instances = []
    block.status = "cannot_determine"
    block.cannot_determine_reason = reason
    clean_reviewer = (reviewer_name or "").strip()
    if clean_reviewer:
        block.reviewer_name = clean_reviewer
    block.reviewed_at = _now_iso()
    return block


def _format_document_photo_label(numbers: List[str]) -> str:
    if len(numbers) == 1:
        return numbers[0]
    try:
        parsed = [(n.split(".")[0], int(n.split(".")[1])) for n in numbers]
        majors = {major for major, _ in parsed}
        minors_sorted = sorted(minor for _, minor in parsed)
        if len(majors) == 1 and minors_sorted == list(
            range(minors_sorted[0], minors_sorted[0] + len(minors_sorted))
        ):
            major = next(iter(majors))
            return f"{major}.{minors_sorted[0]}–{major}.{minors_sorted[-1]}"
    except (ValueError, IndexError):
        pass
    return ", ".join(numbers)


def split_block(
    report: ReferenceReport,
    group_id: str,
    block_id: str,
    groups: List[List[str]],
) -> List[SourceBlock]:
    """Splits one block into several new blocks, each covering a subset of
    the source block's own document_photo_refs - a pure re-partition of
    already-known numbers, never inventing or dropping one.

    Every new block starts with the SAME text as the source block
    (original_text_he and approved_text_he both copied verbatim) - there is
    still no reliable way to know which sentence belongs to which specific
    number, so splitting narrows the number-scope per block without
    pretending to know the wording split too. A reviewer can edit each new
    block's approved_text_he down independently afterward.

    Raises ReferenceReviewError if `groups` is not an exact partition of the
    source block's document_photo_refs (missing, duplicated, or unknown
    numbers), or if there is nothing to split (a single-number block)."""
    group = report.find_group(group_id)
    source = group.find_block(block_id)

    if len(source.document_photo_refs) <= 1:
        raise ReferenceReviewError(
            f"בלוק {source.document_photo_label} מכיל מספר תמונה אחד בלבד - אין מה לפצל."
        )

    original_set = list(source.document_photo_refs)
    flattened = [n for grp in groups if grp for n in grp]
    if not all(grp for grp in groups):
        raise ReferenceReviewError("לא ניתן לכלול קבוצה ריקה בפיצול.")
    missing = [n for n in original_set if n not in flattened]
    extra = [n for n in flattened if n not in original_set]
    duplicated = [n for n in set(flattened) if flattened.count(n) > 1]
    if missing or extra or duplicated:
        problems = []
        if missing:
            problems.append("חסרים: " + ", ".join(missing))
        if extra:
            problems.append("לא שייכים לבלוק המקורי: " + ", ".join(extra))
        if duplicated:
            problems.append("מופיעים יותר מפעם אחת: " + ", ".join(duplicated))
        raise ReferenceReviewError(
            "הפיצול חייב לכלול בדיוק את מספרי התמונה המקוריים, כל אחד פעם אחת. "
            + "; ".join(problems)
        )

    new_blocks: List[SourceBlock] = []
    for numbers in groups:
        block_id_new = "-".join(numbers)
        now = _now_iso()
        new_blocks.append(
            SourceBlock(
                block_id=block_id_new,
                document_photo_label=_format_document_photo_label(numbers),
                document_photo_refs=list(numbers),
                original_text_he=source.original_text_he,
                approved_text_he=source.approved_text_he,
                image_instances=[],
                status="not_reviewed",
                extraction_confidence=source.extraction_confidence,
                ambiguity_notes=source.ambiguity_notes,
                layout_evidence=source.layout_evidence,
                audit_log=[
                    {
                        "at": now,
                        "reviewer_name": None,
                        "action": "split_from_block",
                        "source_block_id": block_id,
                    }
                ],
            )
        )

    if source.image_instances:
        # A split block's images can't be auto-distributed across the new
        # numbered blocks - their source_files are surfaced as a hint
        # instead (any crop framing is deliberately dropped here, since it
        # was implicitly framed for the OLD merged block).
        source_files = {i.source_file for i in source.image_instances}
        group.legacy_unmapped_images = list(group.legacy_unmapped_images) + [
            n for n in source_files if n not in group.legacy_unmapped_images
        ]

    index = group.source_blocks.index(source)
    group.source_blocks[index : index + 1] = new_blocks
    return new_blocks
