"""Production packaging behavior: hiding the Reference Review tab in a
frozen build, and switching the approved-reference data source between the
live dev dataset and the derived production pack."""

from __future__ import annotations

import json

import flet as ft
import pytest

import app
import approved_reference_service as ref_service
import config


class _FakePage:
    """The minimal ft.Page surface build_reference_review_tab() touches -
    it never actually needs a running Flet session for construction, only
    for handling clicks, which these tests never trigger."""

    def pop_dialog(self):
        pass

    def show_dialog(self, dialog):
        pass

    def update(self):
        pass


# --------------------------------------------------- config flag resolution

@pytest.mark.parametrize(
    "frozen,env_value,expected",
    [
        (True, "", False),  # frozen, no override -> hidden (production default)
        (False, "", True),  # dev, no override -> shown (dev default)
        (True, "1", True),  # frozen, explicit override -> shown
        (True, "true", True),
        (False, "0", False),  # dev, explicit override -> hidden
        (False, "false", False),
    ],
)
def test_resolve_review_tab_enabled(frozen, env_value, expected):
    assert config._resolve_review_tab_enabled(frozen, env_value) is expected


# --------------------------------------------------------------- app root

def test_build_app_root_hides_review_tab_in_production_mode():
    """When review_enabled is False (the frozen/production default), the
    root control must be exactly the generator view - no Tabs, no tab bar,
    and build_reference_review_tab() must never even be called (it would
    try to read data/reference_examples/, which a production build doesn't
    bundle)."""
    generator_view = ft.Text("generator")
    root = app.build_app_root(_FakePage(), generator_view, review_enabled=False)
    assert root is generator_view


def test_build_app_root_retains_review_tab_in_dev_mode():
    """When review_enabled is True (the normal `python app.py` dev
    default), the root control is a two-tab layout including the live
    Reference Review screen, built against the real dataset."""
    generator_view = ft.Text("generator")
    root = app.build_app_root(_FakePage(), generator_view, review_enabled=True)

    assert isinstance(root, ft.Tabs)
    tab_bar = root.content.controls[0]
    labels = [tab.label for tab in tab_bar.tabs]
    assert labels == ["יצירת דוח", "בדיקת דוחות ייחוס"]
    tab_bar_view = root.content.controls[1]
    assert tab_bar_view.controls[0] is generator_view
    assert isinstance(tab_bar_view.controls[1], ft.Column)  # the review view itself


def test_build_reference_review_tab_is_directly_callable_in_dev_mode():
    """Sanity check that the extracted function works standalone (proves
    the dev/production split didn't silently break the dev path)."""
    view = app.build_reference_review_tab(_FakePage())
    assert isinstance(view, ft.Column)


# ------------------------------------------- approved_reference_service dispatch

def _write_live_report(root, category, report_id, block_images):
    report_dir = root / category / report_id
    report_dir.mkdir(parents=True)
    payload = {
        "report_id": report_id,
        "category": category,
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
                        "original_text_he": "טקסט חי.",
                        "approved_text_he": "טקסט חי.",
                        "image_instances": [
                            {
                                "instance_id": "1.1:0",
                                "source_file": name,
                                "variant_type": "full",
                                "crop": None,
                                "display_order": i,
                                "preview_path": None,
                            }
                            for i, name in enumerate(block_images)
                        ],
                        "status": "approved",
                        "extraction_confidence": "human_annotated",
                        "ambiguity_notes": "",
                        "layout_evidence": None,
                        "cannot_determine_reason": None,
                        "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
                    }
                ],
                "legacy_unmapped_images": [],
                "legacy_group_review": {},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_pack(root, entries):
    root.mkdir(parents=True, exist_ok=True)
    (root / "previews").mkdir(exist_ok=True)
    (root / "pack.json").write_text(
        json.dumps({"pack_version": 1, "generated_at": "", "entry_count": len(entries), "entries": entries}),
        encoding="utf-8",
    )


def test_dev_mode_reads_live_dataset_not_pack(tmp_path, monkeypatch):
    live_root = tmp_path / "reference_examples"
    live_root.mkdir()
    _write_live_report(live_root, "crane", "R_LIVE", ["a.jpg"])
    pack_root = tmp_path / "approved_reference_pack"
    _write_pack(pack_root, [{"category": "crane", "report_id": "R_PACK", "group_id": "g", "block_id": "9.9",
                              "document_photo_label": "9.9", "approved_text_he": "מהחבילה בלבד.",
                              "image_count": 1, "images": [{"variant_type": "full", "preview_file": None}]}])

    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", live_root)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_PACK_DIR", pack_root)
    monkeypatch.setattr(config, "USE_APPROVED_REFERENCE_PACK", False)

    blocks = ref_service.list_approved_blocks()
    assert [b.report_id for b in blocks] == ["R_LIVE"]


def test_frozen_mode_reads_pack_not_live_dataset(tmp_path, monkeypatch):
    live_root = tmp_path / "reference_examples"
    live_root.mkdir()
    _write_live_report(live_root, "crane", "R_LIVE", ["a.jpg"])
    pack_root = tmp_path / "approved_reference_pack"
    _write_pack(pack_root, [{"category": "crane", "report_id": "R_PACK", "group_id": "g", "block_id": "9.9",
                              "document_photo_label": "9.9", "approved_text_he": "מהחבילה בלבד.",
                              "image_count": 1, "images": [{"variant_type": "full", "preview_file": None}]}])

    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", live_root)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_PACK_DIR", pack_root)
    monkeypatch.setattr(config, "USE_APPROVED_REFERENCE_PACK", True)

    blocks = ref_service.list_approved_blocks()
    assert [b.report_id for b in blocks] == ["R_PACK"]


def test_pack_reader_never_touches_live_dataset_dir(tmp_path, monkeypatch):
    """A frozen build never bundles data/reference_examples/ at all - the
    pack reader must not fail or fall back to it when it's simply absent."""
    pack_root = tmp_path / "approved_reference_pack"
    _write_pack(pack_root, [{"category": "crane", "report_id": "R_PACK", "group_id": "g", "block_id": "1.1",
                              "document_photo_label": "1.1", "approved_text_he": "טקסט.",
                              "image_count": 1, "images": [{"variant_type": "full", "preview_file": None}]}])

    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(config, "APPROVED_REFERENCE_PACK_DIR", pack_root)
    monkeypatch.setattr(config, "USE_APPROVED_REFERENCE_PACK", True)

    blocks = ref_service.list_approved_blocks()
    assert [b.report_id for b in blocks] == ["R_PACK"]


def test_pack_reader_handles_missing_pack_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPROVED_REFERENCE_PACK_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(config, "USE_APPROVED_REFERENCE_PACK", True)
    assert ref_service.list_approved_blocks() == []
