"""Reference-dataset review workflow (reference_review_service.py) - the
per-photo-number-block model, with reusable source photos / non-destructive
crops as image instances.

Uses a small fake data/reference_examples/ tree under tmp_path so tests never
touch the real curated dataset.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

import config
import reference_review_service as review


def _block(
    block_id,
    label,
    refs,
    text,
    *,
    images=None,
    status="not_reviewed",
    confidence="human_annotated",
    notes="",
    layout_evidence=None,
    cannot_determine_reason=None,
):
    images = images or []
    return {
        "block_id": block_id,
        "document_photo_label": label,
        "document_photo_refs": refs,
        "original_text_he": text,
        "approved_text_he": text,
        "image_instances": [
            {
                "instance_id": f"{block_id}:legacy-{i}",
                "source_file": name,
                "variant_type": "full",
                "crop": None,
                "display_order": i,
                "preview_path": None,
            }
            for i, name in enumerate(images)
        ],
        "status": status,
        "extraction_confidence": confidence,
        "ambiguity_notes": notes,
        "layout_evidence": layout_evidence,
        "cannot_determine_reason": cannot_determine_reason,
        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
    }


@pytest.fixture
def fake_dataset(tmp_path, monkeypatch):
    root = tmp_path / "reference_examples"
    report_dir = root / "crane" / "C_TEST_ref1"
    images_dir = report_dir / "images"
    images_dir.mkdir(parents=True)

    # Real, readable image files - several tests crop/render them for real.
    for name in ("a.jpg", "b.jpg", "shared.jpg", "1.jpg", "x.jpg", "orphan.jpg"):
        Image.new("RGB", (400, 300), (10, 20, 30)).save(images_dir / name, "JPEG")

    index = {
        "reports": [
            {
                "report_id": "C_TEST_ref1",
                "category": "crane",
                "source_report_path": "templates/crane_refernces/C_TEST_ref1.docx",
                "output_folder": "data/reference_examples/crane/C_TEST_ref1",
            }
        ]
    }
    (root / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    captions = {
        "report_id": "C_TEST_ref1",
        "category": "crane",
        "source_report_path": "templates/crane_refernces/C_TEST_ref1.docx",
        "caption_groups": [
            {
                "group_id": "photoset_1",
                "original_text_he": "תמונה 1: כותרת.\nהרכב במבט מלפנים.\nפרישת הזרוע.",
                "notes": "1.1, 1.4",
                "source_blocks": [
                    _block("1.1-1.3", "1.1–1.3", ["1.1", "1.2", "1.3"], "הרכב במבט מלפנים."),
                    _block("1.4", "1.4", ["1.4"], "פרישת הזרוע."),
                ],
                "legacy_unmapped_images": [],
                "legacy_group_review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
            },
            {
                "group_id": "photoset_2",
                "original_text_he": "תמונה 2: קבוצה שנייה.",
                "notes": "",
                "source_blocks": [
                    _block(
                        "general",
                        "כלל הקבוצה (טרם פוצל לפי מספור)",
                        ["2.1", "2.2"],
                        "תמונה 2: קבוצה שנייה.",
                        confidence="numbers_confirmed_text_unsplit",
                    )
                ],
                "legacy_unmapped_images": [],
                "legacy_group_review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
            },
        ],
    }
    (report_dir / "captions.he.json").write_text(
        json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)
    return {"root": root, "report_dir": report_dir, "images_dir": images_dir}


def test_loading_caption_groups_and_blocks(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    assert [g.group_id for g in report.caption_groups] == ["photoset_1", "photoset_2"]
    group = report.find_group("photoset_1")
    assert [b.block_id for b in group.source_blocks] == ["1.1-1.3", "1.4"]
    block = group.find_block("1.1-1.3")
    assert block.document_photo_refs == ["1.1", "1.2", "1.3"]
    assert block.original_text_he == "הרכב במבט מלפנים."
    assert block.status == "not_reviewed"
    assert block.cannot_determine_reason is None


def test_list_review_reports_reports_block_level_progress(fake_dataset):
    summaries = review.list_review_reports()
    assert len(summaries) == 1
    assert summaries[0]["report_id"] == "C_TEST_ref1"
    assert summaries[0]["approved_count"] == 0
    assert summaries[0]["block_count"] == 3  # 2 in photoset_1 + 1 fallback in photoset_2
    assert "0 מתוך 3" in summaries[0]["progress_text_he"]


# --------------------------------------------------------- block workflow

def test_saving_approved_block_persists_to_disk(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.1-1.3", "a.jpg")
    review.apply_block_decision(
        report,
        "photoset_1",
        "1.1-1.3",
        approved_text_he="הרכב במבט מלפנים.",
        decision="approved",
        reviewer_name="בודק לדוגמה",
    )
    review.save_report(report)

    reloaded = review.load_report("C_TEST_ref1")
    block = reloaded.find_group("photoset_1").find_block("1.1-1.3")
    assert block.status == "approved"
    assert [i.source_file for i in block.image_instances] == ["a.jpg"]
    assert block.image_instances[0].variant_type == "full"
    assert block.reviewer_name == "בודק לדוגמה"
    assert block.reviewed_at is not None
    assert len(block.audit_log) == 1


def test_saving_corrections_without_approval(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.apply_block_decision(
        report, "photoset_1", "1.4", approved_text_he="נוסח מתוקן.", decision="needs_edit"
    )
    review.save_report(report)

    reloaded = review.load_report("C_TEST_ref1")
    block = reloaded.find_group("photoset_1").find_block("1.4")
    assert block.status == "needs_edit"
    assert block.approved_text_he == "נוסח מתוקן."
    # Original wording stays untouched even though approved_text_he changed.
    assert block.original_text_he == "פרישת הזרוע."


def test_save_decision_does_not_change_status(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.apply_block_decision(
        report, "photoset_1", "1.4", approved_text_he="טיוטת עריכה.", decision="save"
    )
    block = report.find_group("photoset_1").find_block("1.4")
    assert block.status == "not_reviewed"
    assert block.approved_text_he == "טיוטת עריכה."


def test_validation_prevents_approval_without_image_or_cannot_determine(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    with pytest.raises(review.ReviewValidationError):
        review.apply_block_decision(
            report,
            "photoset_1",
            "1.1-1.3",
            approved_text_he="הרכב במבט מלפנים.",
            decision="approved",
        )
    assert report.find_group("photoset_1").find_block("1.1-1.3").status == "not_reviewed"


def test_cannot_determine_allows_approval_without_image(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.mark_block_cannot_determine(report, "photoset_1", "1.4", reason="missing_source_image")
    block = review.apply_block_decision(
        report, "photoset_1", "1.4", approved_text_he="פרישת הזרוע.", decision="approved"
    )
    assert block.status == "approved"


def test_original_text_immutable_across_multiple_block_edits(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    block = report.find_group("photoset_1").find_block("1.4")
    original = block.original_text_he

    review.apply_block_decision(report, "photoset_1", "1.4", approved_text_he="נוסח א.", decision="needs_edit")
    review.assign_full_image(report, "photoset_1", "1.4", "b.jpg")
    review.apply_block_decision(report, "photoset_1", "1.4", approved_text_he="נוסח ב.", decision="approved")
    review.save_report(report)

    reloaded = review.load_report("C_TEST_ref1")
    block = reloaded.find_group("photoset_1").find_block("1.4")
    assert block.original_text_he == original
    assert block.approved_text_he == "נוסח ב."
    assert len(block.audit_log) == 2


# ---------------------------------------------- reusable source photos (new)

def test_assign_full_image_adds_an_instance(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "1.jpg")
    block = report.find_group("photoset_1").find_block("1.4")
    assert len(block.image_instances) == 1
    assert block.image_instances[0].source_file == "1.jpg"
    assert block.image_instances[0].variant_type == "full"
    assert block.status == "needs_edit"


def test_assigning_same_file_to_another_block_no_longer_removes_it(fake_dataset):
    """The old exclusivity rule (assigning moves the file away from any
    other block) is gone: the same source photo may legitimately back
    several blocks (e.g. a full view in one, a crop of it in another)."""
    report = review.load_report("C_TEST_ref1")
    group = report.find_group("photoset_1")

    review.assign_full_image(report, "photoset_1", "1.4", "shared.jpg")
    assert [i.source_file for i in group.find_block("1.4").image_instances] == ["shared.jpg"]

    # Assigning the SAME file as a full image to a different block must be
    # explicitly confirmed (never silent) - and once confirmed, it stays in
    # BOTH blocks, it is not moved.
    with pytest.raises(review.FullImageAlreadyAssignedError):
        review.assign_full_image(report, "photoset_1", "1.1-1.3", "shared.jpg")

    review.assign_full_image(report, "photoset_1", "1.1-1.3", "shared.jpg", confirm_existing_use=True)
    assert [i.source_file for i in group.find_block("1.4").image_instances] == ["shared.jpg"]  # still there
    assert [i.source_file for i in group.find_block("1.1-1.3").image_instances] == ["shared.jpg"]

    # And it must not touch the other group's block at all.
    other_group = report.find_group("photoset_2")
    assert other_group.find_block("general").image_instances == []


def test_full_image_confirmation_error_lists_existing_uses(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "shared.jpg")
    with pytest.raises(review.FullImageAlreadyAssignedError) as exc_info:
        review.assign_full_image(report, "photoset_1", "1.1-1.3", "shared.jpg")
    uses = exc_info.value.existing_uses
    assert len(uses) == 1
    assert uses[0].block_id == "1.4"
    assert uses[0].variant_type == "full"


def test_one_source_image_creates_multiple_crop_instances_in_different_blocks(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    crop_a = review.CropBox(left=0.0, top=0.0, width=0.5, height=0.5)
    crop_b = review.CropBox(left=0.5, top=0.5, width=0.5, height=0.5)

    inst_a = review.add_crop_instance(report, "photoset_1", "1.4", "shared.jpg", crop_a)
    inst_b = review.add_crop_instance(report, "photoset_1", "1.1-1.3", "shared.jpg", crop_b)

    assert inst_a.instance_id != inst_b.instance_id
    group = report.find_group("photoset_1")
    assert group.find_block("1.4").image_instances[0].crop.width == 0.5
    assert group.find_block("1.1-1.3").image_instances[0].crop.left == 0.5

    usages = review.find_source_file_usages(report, "shared.jpg")
    assert {u.block_id for u in usages} == {"1.4", "1.1-1.3"}
    assert all(u.variant_type == "crop" for u in usages)


def test_removing_one_crop_does_not_remove_source_image_or_other_crops(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    crop = review.CropBox(left=0.0, top=0.0, width=0.4, height=0.4)
    inst1 = review.add_crop_instance(report, "photoset_1", "1.4", "shared.jpg", crop)
    inst2 = review.add_crop_instance(report, "photoset_1", "1.4", "shared.jpg", crop)
    review.add_crop_instance(report, "photoset_1", "1.1-1.3", "shared.jpg", crop)

    review.remove_image_instance(report, "photoset_1", "1.4", inst1.instance_id)

    block = report.find_group("photoset_1").find_block("1.4")
    assert [i.instance_id for i in block.image_instances] == [inst2.instance_id]
    other_block = report.find_group("photoset_1").find_block("1.1-1.3")
    assert len(other_block.image_instances) == 1  # untouched


def test_no_original_source_file_is_ever_modified(fake_dataset):
    images_dir = fake_dataset["images_dir"]
    original_bytes = (images_dir / "shared.jpg").read_bytes()

    report = review.load_report("C_TEST_ref1")
    crop = review.CropBox(left=0.1, top=0.1, width=0.3, height=0.3)
    inst = review.add_crop_instance(report, "photoset_1", "1.4", "shared.jpg", crop)
    review.update_crop_instance(
        report, "photoset_1", "1.4", inst.instance_id, review.CropBox(left=0.2, top=0.2, width=0.2, height=0.2)
    )
    review.assign_full_image(report, "photoset_1", "1.1-1.3", "shared.jpg", confirm_existing_use=True)
    review.remove_image_instance(report, "photoset_1", "1.4", inst.instance_id)
    review.save_report(report)

    assert (images_dir / "shared.jpg").read_bytes() == original_bytes


def test_normalized_crop_coordinates_are_valid_and_stable_after_save_reload(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    crop = review.CropBox(left=0.125, top=0.25, width=0.5, height=0.375)
    review.add_crop_instance(report, "photoset_1", "1.4", "shared.jpg", crop)
    review.save_report(report)

    reloaded = review.load_report("C_TEST_ref1")
    instance = reloaded.find_group("photoset_1").find_block("1.4").image_instances[0]
    assert instance.variant_type == "crop"
    assert instance.crop.left == 0.125
    assert instance.crop.top == 0.25
    assert instance.crop.width == 0.5
    assert instance.crop.height == 0.375
    # Every coordinate is within the required [0, 1] range.
    for value in (instance.crop.left, instance.crop.top, instance.crop.width, instance.crop.height):
        assert 0.0 <= value <= 1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"left": -0.1, "top": 0.0, "width": 0.5, "height": 0.5},
        {"left": 0.0, "top": 1.5, "width": 0.5, "height": 0.5},
        {"left": 0.8, "top": 0.0, "width": 0.5, "height": 0.5},  # left+width > 1
        {"left": 0.0, "top": 0.8, "width": 0.5, "height": 0.5},  # top+height > 1
        {"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.5},  # zero width
    ],
)
def test_crop_box_validate_rejects_out_of_bounds(kwargs):
    with pytest.raises(review.ReferenceReviewError):
        review.CropBox(**kwargs).validate()


def test_crop_box_validate_accepts_full_frame_and_corners():
    review.CropBox(left=0.0, top=0.0, width=1.0, height=1.0).validate()
    review.CropBox(left=0.5, top=0.5, width=0.5, height=0.5).validate()


def test_update_crop_instance_rejects_editing_a_full_instance(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "shared.jpg")
    instance = report.find_group("photoset_1").find_block("1.4").image_instances[0]
    with pytest.raises(review.ReferenceReviewError):
        review.update_crop_instance(
            report, "photoset_1", "1.4", instance.instance_id, review.CropBox(0, 0, 0.5, 0.5)
        )


def test_reorder_image_instance_moves_within_its_own_block(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "a.jpg")
    review.assign_full_image(report, "photoset_1", "1.4", "b.jpg", confirm_existing_use=True)
    review.assign_full_image(report, "photoset_1", "1.4", "shared.jpg", confirm_existing_use=True)
    block = report.find_group("photoset_1").find_block("1.4")
    assert [i.source_file for i in block.image_instances] == ["a.jpg", "b.jpg", "shared.jpg"]

    instance_id = block.image_instances[2].instance_id  # shared.jpg
    review.reorder_image_instance(report, "photoset_1", "1.4", instance_id, 0)
    assert [i.source_file for i in block.image_instances] == ["shared.jpg", "a.jpg", "b.jpg"]
    assert [i.display_order for i in block.image_instances] == [0, 1, 2]


def test_removing_last_instance_downgrades_a_previously_approved_block(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "x.jpg")
    review.apply_block_decision(
        report, "photoset_1", "1.4", approved_text_he="פרישת הזרוע.", decision="approved"
    )
    block = report.find_group("photoset_1").find_block("1.4")
    assert block.status == "approved"

    instance_id = block.image_instances[0].instance_id
    review.remove_image_instance(report, "photoset_1", "1.4", instance_id)
    assert block.image_instances == []
    assert block.status == "not_reviewed"


# ------------------------------------------------------ legacy migration

def test_legacy_image_files_migrate_to_full_instances_on_load():
    legacy_block = {
        "block_id": "1.1",
        "document_photo_label": "1.1",
        "document_photo_refs": ["1.1"],
        "original_text_he": "טקסט.",
        "approved_text_he": "טקסט.",
        "image_files": ["first.jpg", "second.jpg"],  # old shape - no image_instances key
        "status": "approved",
        "extraction_confidence": "human_annotated",
        "ambiguity_notes": "",
        "layout_evidence": None,
        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
    }
    block = review.SourceBlock.from_dict(legacy_block)

    assert [i.source_file for i in block.image_instances] == ["first.jpg", "second.jpg"]
    assert all(i.variant_type == "full" for i in block.image_instances)
    assert all(i.crop is None for i in block.image_instances)
    assert [i.display_order for i in block.image_instances] == [0, 1]
    # Nothing else about the legacy block was altered.
    assert block.status == "approved"
    assert block.original_text_he == "טקסט."
    assert block.cannot_determine_reason is None


def test_legacy_migration_is_lossless_for_real_dataset_shape(tmp_path, monkeypatch):
    """A minimal end-to-end check: writing a v3-shape (image_files) file to
    disk and loading it through the real service produces the same visible
    mapping as the already-migrated v4 shape."""
    root = tmp_path / "reference_examples"
    report_dir = root / "crane" / "C_LEGACY"
    report_dir.mkdir(parents=True)
    (root / "index.json").write_text(
        json.dumps({"reports": [{"report_id": "C_LEGACY", "category": "crane", "source_report_path": ""}]}),
        encoding="utf-8",
    )
    captions = {
        "report_id": "C_LEGACY",
        "category": "crane",
        "source_report_path": "",
        "caption_groups": [
            {
                "group_id": "photoset_1",
                "original_text_he": "טקסט קבוצה.",
                "notes": "",
                "source_blocks": [
                    {
                        "block_id": "1.1",
                        "document_photo_label": "1.1",
                        "document_photo_refs": ["1.1"],
                        "original_text_he": "טקסט.",
                        "approved_text_he": "טקסט מאושר.",
                        "image_files": ["old.jpg"],
                        "status": "approved",
                        "extraction_confidence": "human_annotated",
                        "ambiguity_notes": "",
                        "layout_evidence": None,
                        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
                    }
                ],
                "legacy_unmapped_images": [],
                "legacy_group_review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(json.dumps(captions, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)

    report = review.load_report("C_LEGACY")
    block = report.find_group("photoset_1").find_block("1.1")
    assert block.status == "approved"
    assert block.approved_text_he == "טקסט מאושר."
    assert [i.source_file for i in block.image_instances] == ["old.jpg"]


# --------------------------------------------------------- splitting blocks

def test_split_block_into_one_block_per_number(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    group = report.find_group("photoset_2")

    new_blocks = review.split_block(report, "photoset_2", "general", [["2.1"], ["2.2"]])

    assert [b.block_id for b in new_blocks] == ["2.1", "2.2"]
    assert [b.document_photo_label for b in new_blocks] == ["2.1", "2.2"]
    assert [b.block_id for b in group.source_blocks] == ["2.1", "2.2"]
    for b in new_blocks:
        assert b.original_text_he == "תמונה 2: קבוצה שנייה."
        assert b.approved_text_he == "תמונה 2: קבוצה שנייה."
        assert b.status == "not_reviewed"
        assert b.image_instances == []


def test_split_block_with_merged_group_gets_range_label(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    new_blocks = review.split_block(report, "photoset_1", "1.1-1.3", [["1.1", "1.2"], ["1.3"]])

    assert new_blocks[0].block_id == "1.1-1.2"
    assert new_blocks[0].document_photo_label == "1.1–1.2"
    assert new_blocks[0].document_photo_refs == ["1.1", "1.2"]
    assert new_blocks[1].block_id == "1.3"
    assert new_blocks[1].document_photo_label == "1.3"


def test_split_block_preserves_source_position_among_siblings(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    group = report.find_group("photoset_1")
    review.split_block(report, "photoset_1", "1.1-1.3", [["1.1"], ["1.2"], ["1.3"]])
    # The original second block ("1.4") must still come after the 3 new ones.
    assert [b.block_id for b in group.source_blocks] == ["1.1", "1.2", "1.3", "1.4"]


def test_split_block_moves_existing_images_to_legacy_not_a_new_block(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_2", "general", "orphan.jpg")

    review.split_block(report, "photoset_2", "general", [["2.1"], ["2.2"]])

    group = report.find_group("photoset_2")
    assert "orphan.jpg" in group.legacy_unmapped_images
    for b in group.source_blocks:
        assert not any(i.source_file == "orphan.jpg" for i in b.image_instances)


def test_split_block_rejects_missing_number(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    group = report.find_group("photoset_2")
    with pytest.raises(review.ReferenceReviewError):
        review.split_block(report, "photoset_2", "general", [["2.1"]])  # missing 2.2
    assert [b.block_id for b in group.source_blocks] == ["general"]  # unchanged


def test_split_block_rejects_duplicate_number(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    with pytest.raises(review.ReferenceReviewError):
        review.split_block(report, "photoset_2", "general", [["2.1", "2.1"], ["2.2"]])


def test_split_block_rejects_unknown_number(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    with pytest.raises(review.ReferenceReviewError):
        review.split_block(report, "photoset_2", "general", [["2.1"], ["2.2"], ["9.9"]])


def test_split_single_number_block_is_rejected(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    with pytest.raises(review.ReferenceReviewError):
        review.split_block(report, "photoset_1", "1.4", [["1.4"]])


# ---------------------------------------------------- cannot_determine + reasons

def test_mark_block_cannot_determine_clears_any_selected_images(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "x.jpg")
    review.mark_block_cannot_determine(report, "photoset_1", "1.4", reason="mapping_ambiguous")
    block = report.find_group("photoset_1").find_block("1.4")
    assert block.status == "cannot_determine"
    assert block.image_instances == []
    assert block.cannot_determine_reason == "mapping_ambiguous"


def test_mark_block_cannot_determine_rejects_unknown_reason(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    with pytest.raises(review.ReferenceReviewError):
        review.mark_block_cannot_determine(report, "photoset_1", "1.4", reason="not_a_real_reason")


def test_reopening_cannot_determine_block_clears_reason_on_new_assignment(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.mark_block_cannot_determine(report, "photoset_1", "1.4", reason="missing_source_image")
    block = report.find_group("photoset_1").find_block("1.4")
    assert block.status == "cannot_determine"
    assert block.cannot_determine_reason == "missing_source_image"

    # A newly found file gets assigned later - this must clear the reason
    # and move the block back into the normal review flow.
    review.assign_full_image(report, "photoset_1", "1.4", "x.jpg")
    assert block.status == "needs_edit"
    assert block.cannot_determine_reason is None


def test_cannot_determine_block_never_counts_as_approved_reference(tmp_path, monkeypatch):
    """approved_reference_service must never surface a cannot_determine
    block, structured reason or not - status alone gates eligibility."""
    import approved_reference_service as ref_service

    root = tmp_path / "reference_examples"
    report_dir = root / "crane" / "R_CD"
    report_dir.mkdir(parents=True)
    data = {
        "report_id": "R_CD",
        "category": "crane",
        "source_report_path": "",
        "caption_groups": [
            {
                "group_id": "g1",
                "original_text_he": "טקסט.",
                "notes": "",
                "source_blocks": [
                    {
                        "block_id": "1.1",
                        "document_photo_label": "1.1",
                        "document_photo_refs": ["1.1"],
                        "original_text_he": "טקסט.",
                        "approved_text_he": "טקסט.",
                        "image_instances": [],
                        "status": "cannot_determine",
                        "extraction_confidence": "uncertain",
                        "ambiguity_notes": "",
                        "layout_evidence": None,
                        "cannot_determine_reason": "missing_source_image",
                        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
                    }
                ],
                "legacy_unmapped_images": [],
                "legacy_group_review": {},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", root)

    assert ref_service.list_approved_blocks() == []


# ----------------------------------------------------------------- images

def test_list_candidate_images_lists_added_photos(fake_dataset):
    images_dir = fake_dataset["images_dir"]
    (images_dir / ".gitkeep").write_text("")

    names = {p.name for p in review.list_candidate_images("C_TEST_ref1")}
    assert "a.jpg" in names
    assert ".gitkeep" not in names


def test_find_source_file_usages_across_groups(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    review.assign_full_image(report, "photoset_1", "1.4", "shared.jpg")
    review.assign_full_image(report, "photoset_2", "general", "shared.jpg", confirm_existing_use=True)

    usages = review.find_source_file_usages(report, "shared.jpg")
    assert {(u.group_id, u.block_id) for u in usages} == {("photoset_1", "1.4"), ("photoset_2", "general")}


def test_find_source_file_usages_empty_for_unused_file(fake_dataset):
    report = review.load_report("C_TEST_ref1")
    assert review.find_source_file_usages(report, "a.jpg") == []


# --------------------------------------------------------- rendering (no writes)

def test_render_source_image_preview_and_crop_preview_return_bytes(fake_dataset):
    png_or_jpeg = review.render_source_image_preview("C_TEST_ref1", "shared.jpg", max_dimension=100)
    assert png_or_jpeg[:2] == b"\xff\xd8"  # JPEG magic bytes

    crop = review.CropBox(left=0.1, top=0.1, width=0.4, height=0.4)
    cropped = review.render_crop_preview_bytes("C_TEST_ref1", "shared.jpg", crop, max_dimension=100)
    assert cropped[:2] == b"\xff\xd8"


def test_image_pixel_size_matches_real_file(fake_dataset):
    width, height = review.image_pixel_size("C_TEST_ref1", "shared.jpg")
    assert (width, height) == (400, 300)


# ----------------------------------------------------------- real dataset

def test_all_eight_indexed_reports_appear_and_load():
    """Runs against the real data/reference_examples/ dataset (read-only -
    no fake_dataset fixture, no monkeypatch) to confirm every report listed
    in index.json is reviewable, and that C2501508pub_ref1's photoset_1
    produces exactly the acceptance-test blocks."""
    summaries = review.list_review_reports()
    report_ids = [s["report_id"] for s in summaries]

    assert report_ids == [
        "C2501508pub_ref1",
        "C2502511pub_ref2",
        "C2506545pub_ref3",
        "C2409576pub_ref4",
        "C2604531pub_ref5",
        "C2606542pub_ref6",
        "C2303520pub_ref7",
        "C2403525pub_ref8",
    ]

    for report_id in report_ids:
        report = review.load_report(report_id)
        assert report.caption_groups, f"{report_id} loaded with no caption groups"
        for group in report.caption_groups:
            assert group.source_blocks, f"{report_id}/{group.group_id} has no source_blocks"

    ref1 = review.load_report("C2501508pub_ref1")
    labels = [b.document_photo_label for b in ref1.find_group("photoset_1").source_blocks]
    assert labels == ["1.1–1.3", "1.4", "1.5"]

    # This report's human-approved work (done through the app before the
    # image-instance migration) must have survived: real images still
    # assigned as "full" instances, status still approved.
    block = ref1.find_group("photoset_1").find_block("1.1-1.3")
    assert block.status == "approved"
    assert block.image_instances
    assert all(i.variant_type == "full" for i in block.image_instances)
