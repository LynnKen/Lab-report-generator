from __future__ import annotations

import json

import approved_reference_service as ref_service
import config


def _write_report(root, category: str, report_id: str, groups: list[dict]) -> None:
    report_dir = root / category / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "report_id": report_id,
        "category": category,
        "source_report_path": "",
        "caption_groups": groups,
    }
    (report_dir / "captions.he.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _block(
    block_id: str,
    *,
    status: str = "approved",
    text: str = "טקסט מאושר לדוגמה.",
    images: list[str] | None = None,
) -> dict:
    images = ["a.jpg"] if images is None else images
    return {
        "block_id": block_id,
        "document_photo_label": block_id,
        "document_photo_refs": [block_id],
        "original_text_he": text,
        "approved_text_he": text,
        "image_instances": [
            {
                "instance_id": f"{block_id}:{i}",
                "source_file": name,
                "variant_type": "full",
                "crop": None,
                "display_order": i,
                "preview_path": None,
            }
            for i, name in enumerate(images)
        ],
        "status": status,
        "extraction_confidence": "human_annotated",
        "ambiguity_notes": "",
        "layout_evidence": None,
        "cannot_determine_reason": None,
        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
    }


def _group(group_id: str, blocks: list[dict]) -> dict:
    return {
        "group_id": group_id,
        "original_text_he": "טקסט מקורי של הקבוצה.",
        "notes": "",
        "source_blocks": blocks,
        "legacy_unmapped_images": [],
        "legacy_group_review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
    }


def test_only_approved_blocks_with_images_are_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    _write_report(
        tmp_path,
        "crane",
        "R1",
        [
            _group(
                "g1",
                [
                    _block("1.1", status="approved", images=["p1.jpg"]),
                    _block("1.2", status="not_reviewed"),
                    _block("1.3", status="needs_edit"),
                    _block("1.4", status="cannot_determine"),
                    _block("1.5", status="approved", images=[]),  # no images -> unusable
                ],
            )
        ],
    )

    blocks = ref_service.list_approved_blocks()

    assert [b.block_id for b in blocks] == ["1.1"]
    assert blocks[0].image_count == 1
    assert blocks[0].image_instances[0].variant_type == "full"


def test_malformed_and_empty_text_blocks_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    report_dir = tmp_path / "crane" / "R2"
    report_dir.mkdir(parents=True)
    (report_dir / "captions.he.json").write_text("{ not valid json", encoding="utf-8")

    _write_report(
        tmp_path,
        "crane",
        "R3",
        [
            _group(
                "g1",
                [
                    _block("2.1", status="approved", text="   "),  # blank text -> unusable
                    _block("2.2", status="approved", text="כיתוב תקין"),
                ],
            )
        ],
    )

    blocks = ref_service.list_approved_blocks()

    assert [b.block_id for b in blocks] == ["2.2"]


def test_approved_text_is_never_modified(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    original_text = "  שרוול מס 1 - סידוק היקפי בגבול הריתוך.  \nשורה שנייה."
    _write_report(
        tmp_path, "crane", "R4", [_group("g1", [_block("1.1", text=original_text)])]
    )

    blocks = ref_service.list_approved_blocks()

    assert blocks[0].approved_text_he == original_text  # byte-for-byte, no strip/normalize


def test_newly_approved_block_appears_without_any_rebuild_step(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    _write_report(tmp_path, "crane", "R5", [_group("g1", [_block("1.1")])])

    assert len(ref_service.list_approved_blocks()) == 1

    # Simulate a reviewer approving a new block later the same session -
    # nothing but editing the JSON file on disk, no cache to invalidate.
    _write_report(
        tmp_path,
        "crane",
        "R5",
        [_group("g1", [_block("1.1"), _block("1.2", text="בלוק חדש שאושר.")])],
    )

    blocks = ref_service.list_approved_blocks()
    assert sorted(b.block_id for b in blocks) == ["1.1", "1.2"]


def test_multi_image_block_is_not_a_single_image_label(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    _write_report(
        tmp_path,
        "crane",
        "R6",
        [
            _group(
                "g1",
                [
                    _block("1.1", images=["a.jpg"]),
                    _block("general", text="כיתוב קבוצתי למספר תמונות.", images=["b.jpg", "c.jpg", "d.jpg"]),
                ],
            )
        ],
    )

    blocks = {b.block_id: b for b in ref_service.list_approved_blocks()}
    assert blocks["1.1"].is_single_image is True
    assert blocks["1.1"].is_multi_image is False
    assert blocks["general"].is_single_image is False
    assert blocks["general"].is_multi_image is True
    assert blocks["general"].image_count == 3

    selection = ref_service.select_reference_examples(list(blocks.values()), max_examples=5)
    assert [b.block_id for b in selection.single_image_examples] == ["1.1"]
    assert [b.block_id for b in selection.multi_image_examples] == ["general"]

    context = ref_service.build_reference_context_he(selection)
    assert "כיתוב מאושר לתמונה בודדת" in context
    assert "הקשר סגנוני בלבד - לא תווית מדויקת לתמונה בודדת" in context
    # The multi-image caption text must never be framed as a single-photo label.
    general_line = next(line for line in context.splitlines() if "כיתוב קבוצתי" in line)
    assert "בודדת]" not in general_line


def test_selection_is_bounded_and_prefers_single_image_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    blocks = [
        _block(f"single-{i}", images=[f"s{i}.jpg"]) for i in range(3)
    ] + [_block("multi-1", images=["m1.jpg", "m2.jpg"])]
    _write_report(tmp_path, "crane", "R7", [_group("g1", blocks)])

    selection = ref_service.select_reference_examples(max_examples=2)

    assert selection.count == 2
    assert len(selection.single_image_examples) == 2
    assert len(selection.multi_image_examples) == 0  # budget used up by singles first


def test_selection_fills_remaining_budget_with_multi_image_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    blocks = [_block("single-1", images=["s1.jpg"]), _block("multi-1", images=["m1.jpg", "m2.jpg"])]
    _write_report(tmp_path, "crane", "R8", [_group("g1", blocks)])

    selection = ref_service.select_reference_examples(max_examples=5)

    assert len(selection.single_image_examples) == 1
    assert len(selection.multi_image_examples) == 1


def test_selection_uses_category_diversity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    _write_report(tmp_path, "crane", "RA", [_group("g1", [_block("1.1"), _block("1.2")])])
    _write_report(tmp_path, "chassis", "RB", [_group("g1", [_block("2.1")])])

    selection = ref_service.select_reference_examples(max_examples=2)

    categories = {b.category for b in selection.single_image_examples}
    assert categories == {"crane", "chassis"}  # not both slots from the same category


def test_reference_context_stays_under_configured_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    long_text = "מילה " * 200  # deliberately larger than any reasonable budget
    blocks = [_block(f"b{i}", text=long_text) for i in range(5)]
    _write_report(tmp_path, "crane", "R9", [_group("g1", blocks)])

    selection = ref_service.select_reference_examples(max_examples=5)
    context = ref_service.build_reference_context_he(selection, max_chars=300)

    assert len(context) <= 300


def test_reference_context_empty_when_no_approved_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    selection = ref_service.select_reference_examples([])
    assert ref_service.build_reference_context_he(selection) == ""


def test_missing_reference_examples_dir_yields_no_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path / "does-not-exist")
    assert ref_service.list_approved_blocks() == []
