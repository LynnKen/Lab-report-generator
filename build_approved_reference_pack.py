"""Builds the derived, read-only production asset pack under
data/approved_reference_pack/ from the live, human-approved reference
dataset (data/reference_examples/).

This is the ONLY reference-dataset artifact a frozen/production build ever
bundles (see ReportGenerator.spec). It contains only what live captioning
needs, and specifically never:
- an unapproved, draft, needs_edit, or cannot_determine block;
- review audit data (reviewer names, timestamps, audit logs);
- a PDF or a full-size original photo;
- an absolute local filesystem path.

A crop instance's preview is rendered from its approved crop region, never
the whole source photo. Only single-image blocks ever get a rendered visual
preview - a multi-image block's caption must never become a visual few-shot
label for one specific photo (see approved_reference_service.py), so there
is nothing worth rendering for one.

Deterministic: given the same approved dataset, regenerating always produces
the same entries and byte-identical previews (aside from the "generated_at"
timestamp) - safe to run repeatedly, and safe to run before every packaged
build.

Usage:
    python build_approved_reference_pack.py
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Optional

import approved_reference_service as ref_service
import config

PACK_VERSION = 1


def build_pack(output_dir: Optional[Path] = None) -> dict:
    """Regenerates the pack from the current approved dataset (always reads
    the LIVE dataset directly via _iter_live_approved_blocks(), never the
    pack itself - regenerating from a stale pack would be meaningless).
    Returns a summary dict for reporting."""
    output_dir = output_dir or config.APPROVED_REFERENCE_PACK_DIR
    previews_dir = output_dir / "previews"

    # Deterministic rebuild: wipe and recreate rather than merge, so a block
    # that lost its approval (or its image) never leaves an orphaned preview
    # behind in an old pack.
    if output_dir.exists():
        shutil.rmtree(output_dir)
    previews_dir.mkdir(parents=True)

    max_dimension = config.APPROVED_REFERENCE_PREVIEW_MAX_DIMENSION_PX

    total_seen = 0
    excluded = 0
    entries = []
    preview_index = 0

    for block in ref_service._iter_live_approved_blocks():
        total_seen += 1
        is_single = block.is_single_image
        images_out = []
        block_has_missing_preview = False

        for instance in block.image_instances:
            preview_file = None
            if is_single:
                # Only single-image blocks ever get a rendered visual
                # preview - a multi-image block's shared caption must never
                # become a visual few-shot label for one specific photo.
                data = instance.load_preview_bytes(max_dimension)
                if data:
                    preview_file = f"{preview_index:04d}.jpg"
                    (previews_dir / preview_file).write_bytes(data)
                    preview_index += 1
                else:
                    block_has_missing_preview = True
            images_out.append({"variant_type": instance.variant_type, "preview_file": preview_file})

        if is_single and block_has_missing_preview:
            # An approved single-image block whose source photo is currently
            # missing or unreadable - exclude rather than ship a broken
            # visual entry with no image behind it.
            excluded += 1
            continue

        entries.append(
            {
                "category": block.category,
                "report_id": block.report_id,
                "group_id": block.group_id,
                "block_id": block.block_id,
                "document_photo_label": block.document_photo_label,
                "approved_text_he": block.approved_text_he,
                "image_count": block.image_count,
                "images": images_out,
            }
        )

    pack = {
        "pack_version": PACK_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "entry_count": len(entries),
        "entries": entries,
    }
    (output_dir / "pack.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    return {
        "total_approved_blocks_seen": total_seen,
        "included": len(entries),
        "excluded": excluded,
        "preview_images_written": preview_index,
        "output_dir": str(output_dir),
    }


def main() -> None:
    summary = build_pack()
    print(f"Approved reference pack rebuilt at: {summary['output_dir']}")
    print(f"  approved blocks seen:  {summary['total_approved_blocks_seen']}")
    print(f"  included in pack:      {summary['included']}")
    print(f"  excluded:               {summary['excluded']}")
    print(f"  preview images written: {summary['preview_images_written']}")


if __name__ == "__main__":
    main()
