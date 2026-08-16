"""build_approved_reference_pack.py - the production pack derived from the
live, human-approved reference dataset."""

from __future__ import annotations

import json

from PIL import Image

import build_approved_reference_pack as builder
import config


def _write_report(root, category, report_id, blocks):
    # reference_review_service's rendering functions (used by the pack
    # builder to actually produce preview bytes) resolve a report's images/
    # directory via index.json, not just by globbing captions.he.json - so
    # the index must exist and be kept in sync for previews to render.
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"reports": []}
    if not any(e["report_id"] == report_id for e in index["reports"]):
        index["reports"].append({"report_id": report_id, "category": category, "source_report_path": ""})
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    report_dir = root / category / report_id
    report_dir.mkdir(parents=True)
    (report_dir / "images").mkdir()
    payload = {
        "report_id": report_id,
        "category": category,
        "source_report_path": "",
        "caption_groups": [
            {
                "group_id": "g1",
                "original_text_he": "טקסט קבוצה.",
                "notes": "",
                "source_blocks": blocks,
                "legacy_unmapped_images": [],
                "legacy_group_review": {},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_image(report_dir, name, size=(300, 200)):
    Image.new("RGB", size, (40, 80, 120)).save(report_dir / "images" / name, "JPEG")


def _instance(source_file, *, variant_type="full", crop=None, order=0):
    return {
        "instance_id": f"i{order}",
        "source_file": source_file,
        "variant_type": variant_type,
        "crop": crop,
        "display_order": order,
        "preview_path": None,
    }


def _block(block_id, *, status="approved", text="טקסט מאושר.", instances=None, reason=None):
    instances = instances or []
    return {
        "block_id": block_id,
        "document_photo_label": block_id,
        "document_photo_refs": [block_id],
        "original_text_he": text,
        "approved_text_he": text,
        "image_instances": instances,
        "status": status,
        "extraction_confidence": "human_annotated",
        "ambiguity_notes": "",
        "layout_evidence": None,
        "cannot_determine_reason": reason,
        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
    }


def test_pack_includes_only_approved_blocks_with_usable_images(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R1"
    _write_report(
        root,
        "crane",
        "R1",
        [
            _block("1.1", status="approved", instances=[_instance("a.jpg")]),
            _block("1.2", status="not_reviewed", instances=[_instance("b.jpg")]),
            _block("1.3", status="needs_edit", instances=[_instance("c.jpg")]),
        ],
    )
    _make_image(report_dir, "a.jpg")

    output_dir = tmp_path / "pack_out"
    summary = builder.build_pack(output_dir)

    assert summary["included"] == 1
    pack = json.loads((output_dir / "pack.json").read_text(encoding="utf-8"))
    assert [e["block_id"] for e in pack["entries"]] == ["1.1"]


def test_cannot_determine_and_missing_source_blocks_are_excluded(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R2"
    _write_report(
        root,
        "crane",
        "R2",
        [
            _block("1.1", status="cannot_determine", instances=[], reason="missing_source_image"),
            # "approved" but the source file was never actually added under images/ -
            # must be excluded, never shipped as a broken visual entry.
            _block("1.2", status="approved", instances=[_instance("missing.jpg")]),
            _block("1.3", status="approved", instances=[_instance("real.jpg")]),
        ],
    )
    _make_image(report_dir, "real.jpg")

    output_dir = tmp_path / "pack_out"
    summary = builder.build_pack(output_dir)

    pack = json.loads((output_dir / "pack.json").read_text(encoding="utf-8"))
    assert [e["block_id"] for e in pack["entries"]] == ["1.3"]
    # "1.1" (cannot_determine) is never even counted as "seen" - it isn't
    # status=="approved" to begin with. Only "1.2" (approved but missing its
    # source photo) is seen-and-excluded.
    assert summary["total_approved_blocks_seen"] == 2
    assert summary["excluded"] == 1


def test_crop_instance_pack_preview_uses_crop_region_not_full_photo(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R3"
    crop = {"left": 0.25, "top": 0.25, "width": 0.5, "height": 0.5}
    _write_report(
        root,
        "crane",
        "R3",
        [_block("1.1", instances=[_instance("full.jpg", variant_type="crop", crop=crop)])],
    )
    _make_image(report_dir, "full.jpg", size=(400, 400))

    output_dir = tmp_path / "pack_out"
    builder.build_pack(output_dir)

    pack = json.loads((output_dir / "pack.json").read_text(encoding="utf-8"))
    preview_file = pack["entries"][0]["images"][0]["preview_file"]
    assert preview_file is not None
    with Image.open(output_dir / "previews" / preview_file) as img:
        # The crop is half the width/height of the 400x400 source - the
        # rendered preview must reflect the CROPPED region, not the full
        # 400x400 photo.
        assert img.width < 400 and img.height < 400


def test_multi_image_block_gets_no_preview_file(tmp_path, monkeypatch):
    """A multi-image block's shared caption must never become a visual
    few-shot label for one specific photo - so nothing is rendered for it."""
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R4"
    _write_report(
        root,
        "crane",
        "R4",
        [_block("general", instances=[_instance("x.jpg", order=0), _instance("y.jpg", order=1)])],
    )
    _make_image(report_dir, "x.jpg")
    _make_image(report_dir, "y.jpg")

    output_dir = tmp_path / "pack_out"
    summary = builder.build_pack(output_dir)

    pack = json.loads((output_dir / "pack.json").read_text(encoding="utf-8"))
    entry = pack["entries"][0]
    assert entry["image_count"] == 2
    assert all(img["preview_file"] is None for img in entry["images"])
    assert summary["preview_images_written"] == 0


def test_no_original_source_photo_is_modified(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R5"
    _write_report(root, "crane", "R5", [_block("1.1", instances=[_instance("a.jpg")])])
    _make_image(report_dir, "a.jpg")
    original_bytes = (report_dir / "images" / "a.jpg").read_bytes()

    builder.build_pack(tmp_path / "pack_out")

    assert (report_dir / "images" / "a.jpg").read_bytes() == original_bytes


def test_pack_never_contains_pdfs_paths_or_review_data(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R6"
    _write_report(root, "crane", "R6", [_block("1.1", instances=[_instance("a.jpg")])])
    _make_image(report_dir, "a.jpg")
    (report_dir / "R6_layout.pdf").write_bytes(b"%PDF-fake")

    output_dir = tmp_path / "pack_out"
    builder.build_pack(output_dir)

    for path in output_dir.rglob("*"):
        assert path.suffix.lower() != ".pdf"
    raw = (output_dir / "pack.json").read_text(encoding="utf-8")
    assert "source_file" not in raw
    assert "reviewer_name" not in raw
    assert "audit_log" not in raw
    assert str(root) not in raw  # no absolute local path leaks in


def test_pack_build_is_deterministic(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    report_dir = root / "crane" / "R7"
    _write_report(root, "crane", "R7", [_block("1.1", instances=[_instance("a.jpg")])])
    _make_image(report_dir, "a.jpg")

    out1, out2 = tmp_path / "pack1", tmp_path / "pack2"
    builder.build_pack(out1)
    builder.build_pack(out2)

    def normalize(pack_path):
        data = json.loads(pack_path.read_text(encoding="utf-8"))
        for e in data["entries"]:
            pass
        del data["generated_at"]
        return data

    assert normalize(out1 / "pack.json") == normalize(out2 / "pack.json")
    for name in ("0000.jpg",):
        assert (out1 / "previews" / name).read_bytes() == (out2 / "previews" / name).read_bytes()
