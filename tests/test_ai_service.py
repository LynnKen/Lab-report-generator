from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ai_service
import approved_reference_service
import config


def test_photo_caption_accepts_valid_payload():
    caption = ai_service.PhotoCaption(caption="מבט כללי על הרכיב הנבדק.")
    assert caption.caption == "מבט כללי על הרכיב הנבדק."


def test_photo_caption_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ai_service.PhotoCaption(caption="כיתוב", unexpected_field=123)


def test_build_caption_prompt_includes_style_guide(tmp_path, monkeypatch):
    style_guide = tmp_path / "style_guide.md"
    style_guide.write_text("סגנון: כתוב בגוף ראשון.", encoding="utf-8")
    prompt_file = tmp_path / "caption_prompt.txt"
    prompt_file.write_text("הנחיות מערכת:\n{{style_guide}}\n", encoding="utf-8")

    monkeypatch.setattr(config, "STYLE_GUIDE_PATH", style_guide)
    monkeypatch.setattr(config, "CAPTION_PROMPT_PATH", prompt_file)

    system_prompt, user_message = ai_service.build_caption_prompt()

    assert "כתוב בגוף ראשון" in system_prompt
    assert user_message  # a fixed instruction, no per-report facts needed


def _write_approved_report(root, category: str, report_id: str, *, text: str, images) -> None:
    report_dir = root / category / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "report_id": report_id,
        "category": category,
        "source_report_path": "",
        "caption_groups": [
            {
                "group_id": "g1",
                "original_text_he": text,
                "notes": "",
                "source_blocks": [
                    {
                        "block_id": "1.1",
                        "document_photo_label": "1.1",
                        "document_photo_refs": ["1.1"],
                        "original_text_he": text,
                        "approved_text_he": text,
                        "image_instances": [
                            {
                                "instance_id": f"1.1:{i}",
                                "source_file": name,
                                "variant_type": "full",
                                "crop": None,
                                "display_order": i,
                                "preview_path": None,
                            }
                            for i, name in enumerate(images)
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
                "legacy_group_review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_build_caption_prompt_includes_approved_reference_context_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)
    _write_approved_report(tmp_path, "crane", "R1", text="שרוול מס 1 - מבט כללי.", images=["a.jpg"])

    system_prompt, _user_message = ai_service.build_caption_prompt()

    assert "שרוול מס 1 - מבט כללי." in system_prompt
    assert "אלו אינן עדות לגבי התמונה הנוכחית" in system_prompt


def test_build_caption_prompt_omits_reference_context_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", False)
    _write_approved_report(tmp_path, "crane", "R1", text="שרוול מס 1 - מבט כללי.", images=["a.jpg"])

    system_prompt, _user_message = ai_service.build_caption_prompt()

    assert "שרוול מס 1 - מבט כללי." not in system_prompt


def test_build_caption_prompt_survives_reference_scan_failure(monkeypatch):
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk problem")

    monkeypatch.setattr(approved_reference_service, "select_reference_examples", _boom)

    # Must not raise - a broken/unavailable reference layer can never block
    # captioning, which remains the core, safety-critical feature.
    system_prompt, user_message = ai_service.build_caption_prompt()
    assert system_prompt
    assert user_message


def test_build_caption_prompt_output_contract_unaffected_by_reference_context(tmp_path, monkeypatch):
    """Requirement: the live captioning API still returns only the existing
    valid JSON structure with one `caption` field, regardless of whether
    approved-reference context was included in the prompt."""
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)
    _write_approved_report(tmp_path, "crane", "R1", text="שרוול מס 1 - מבט כללי.", images=["a.jpg"])

    ai_service.build_caption_prompt()  # exercises the reference-context path

    assert set(ai_service.PHOTO_CAPTION_JSON_SCHEMA["properties"]) == {"caption"}
    assert ai_service.PHOTO_CAPTION_JSON_SCHEMA["required"] == ["caption"]
    assert ai_service.PHOTO_CAPTION_JSON_SCHEMA["additionalProperties"] is False
    result = ai_service._RetryingAIService._parse_response(json.dumps({"caption": "מבט כללי."}))
    assert result.model_dump() == {"caption": "מבט כללי."}


# -------------------------------------------------------- visual reference mode

def _write_index(root, entries) -> None:
    (root / "index.json").write_text(
        json.dumps({"reports": [{"report_id": e, "category": "crane", "source_report_path": ""} for e in entries]}),
        encoding="utf-8",
    )


def _write_visual_report(root, report_id: str, *, text: str, image_count: int) -> None:
    """A report with `image_count` distinct single-image approved blocks
    (block_id "1.1", "1.2", ...), each with its own real JPEG under images/,
    so end-to-end visual-mode rendering can be exercised without mocks."""
    from PIL import Image

    report_dir = root / "crane" / report_id
    images_dir = report_dir / "images"
    images_dir.mkdir(parents=True)
    blocks = []
    for i in range(image_count):
        name = f"p{i}.jpg"
        Image.new("RGB", (200, 150), (10 * i, 50, 200)).save(images_dir / name, "JPEG")
        blocks.append(
            {
                "block_id": f"1.{i + 1}",
                "document_photo_label": f"1.{i + 1}",
                "document_photo_refs": [f"1.{i + 1}"],
                "original_text_he": f"{text} {i}",
                "approved_text_he": f"{text} {i}",
                "image_instances": [
                    {
                        "instance_id": f"1.{i + 1}:0",
                        "source_file": name,
                        "variant_type": "full",
                        "crop": None,
                        "display_order": 0,
                        "preview_path": None,
                    }
                ],
                "status": "approved",
                "extraction_confidence": "human_annotated",
                "ambiguity_notes": "",
                "layout_evidence": None,
                "cannot_determine_reason": None,
                "review": {"reviewed_at": None, "reviewer_name": None, "audit_log": []},
            }
        )
    payload = {
        "report_id": report_id,
        "category": "crane",
        "source_report_path": "",
        "caption_groups": [
            {
                "group_id": "g1",
                "original_text_he": text,
                "notes": "",
                "source_blocks": blocks,
                "legacy_unmapped_images": [],
                "legacy_group_review": {},
            }
        ],
    }
    (report_dir / "captions.he.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_visual_mode_builds_real_bounded_image_examples_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MODE", "visual")
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MAX_EXAMPLES", 2)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_PREVIEW_MAX_DIMENSION_PX", 100)
    _write_index(tmp_path, ["R1"])
    _write_visual_report(tmp_path, "R1", text="כיתוב מאושר", image_count=5)

    examples = ai_service._build_visual_reference_examples()

    assert len(examples) == 2  # bounded by APPROVED_REFERENCE_MAX_EXAMPLES
    for data, media_type, caption in examples:
        assert media_type == "image/jpeg"
        assert data[:2] == b"\xff\xd8"  # real JPEG bytes, not a stub
        assert caption.startswith("כיתוב מאושר")


def test_visual_mode_selection_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MODE", "visual")
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MAX_EXAMPLES", 3)
    _write_index(tmp_path, ["R1"])
    _write_visual_report(tmp_path, "R1", text="כיתוב", image_count=5)

    first = [caption for _d, _m, caption in ai_service._build_visual_reference_examples()]
    second = [caption for _d, _m, caption in ai_service._build_visual_reference_examples()]
    assert first == second
    assert len(first) == 3


def test_visual_mode_disabled_returns_no_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", True)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MODE", "text")  # not visual
    _write_index(tmp_path, ["R1"])
    _write_visual_report(tmp_path, "R1", text="כיתוב", image_count=2)

    assert ai_service._build_visual_reference_examples() == []


def test_visual_mode_off_when_references_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_REFERENCES_ENABLED", False)
    monkeypatch.setattr(config, "APPROVED_REFERENCE_MODE", "visual")
    _write_index(tmp_path, ["R1"])
    _write_visual_report(tmp_path, "R1", text="כיתוב", image_count=2)

    assert ai_service._build_visual_reference_examples() == []


def test_multi_image_block_is_never_used_as_a_visual_example():
    """A multi-image group caption must never be presented as one image's
    exact label - build_visual_reference_payload only ever draws visual
    examples from single-image blocks."""
    single = approved_reference_service.ApprovedReferenceBlock(
        category="crane",
        report_id="R1",
        group_id="g1",
        block_id="1.1",
        document_photo_label="1.1",
        approved_text_he="כיתוב לתמונה בודדת.",
        image_instances=(approved_reference_service.ApprovedImageRef(variant_type="full"),),
        source_path="",
    )
    multi = approved_reference_service.ApprovedReferenceBlock(
        category="crane",
        report_id="R1",
        group_id="g1",
        block_id="general",
        document_photo_label="general",
        approved_text_he="כיתוב קבוצתי לכמה תמונות.",
        image_instances=(
            approved_reference_service.ApprovedImageRef(variant_type="full"),
            approved_reference_service.ApprovedImageRef(variant_type="full"),
        ),
        source_path="",
    )

    visual_examples, text_context = approved_reference_service.build_visual_reference_payload(
        [single, multi], max_examples=5
    )

    assert visual_examples == (single,)
    assert "כיתוב קבוצתי לכמה תמונות." in text_context  # kept, but as text only
    assert all(b.is_single_image for b in visual_examples)


def test_call_model_receives_bounded_reference_images_and_target_last(monkeypatch, tmp_path):
    """Verifies generate_photo_caption threads the visual examples through
    to _call_model alongside the target image, in one call."""
    monkeypatch.setattr(
        ai_service,
        "_build_visual_reference_examples",
        lambda: [(b"ref-bytes-1", "image/jpeg", "כיתוב א"), (b"ref-bytes-2", "image/jpeg", "כיתוב ב")],
    )
    image_path = tmp_path / "target.jpg"
    image_path.write_bytes(b"target-bytes")

    captured = {}

    class _CapturingService(ai_service._RetryingAIService):
        provider_name = "fake"

        def __init__(self):
            self.max_retries = 0
            self.timeout = 1

        def _call_model(self, system_prompt, user_message, image_bytes, media_type, reference_images=()):
            captured["reference_images"] = reference_images
            captured["image_bytes"] = image_bytes
            return json.dumps({"caption": "כיתוב חדש."})

    service = _CapturingService()
    caption = service.generate_photo_caption(image_path)

    assert caption == "כיתוב חדש."
    assert captured["reference_images"] == [
        (b"ref-bytes-1", "image/jpeg", "כיתוב א"),
        (b"ref-bytes-2", "image/jpeg", "כיתוב ב"),
    ]
    assert captured["image_bytes"] == b"target-bytes"  # the target photo is what gets captioned


def test_anthropic_call_model_places_target_image_after_reference_examples(monkeypatch):
    """Reference examples (with their captions) must appear before the
    target image and its instruction, framed so the model can't confuse one
    for the other - one request, clearly ordered."""
    service = ai_service.AnthropicAIService.__new__(ai_service.AnthropicAIService)

    captured_kwargs = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)

            class _Block:
                type = "tool_use"
                name = "submit_photo_caption"
                input = {"caption": "כיתוב."}

            class _Response:
                content = [_Block()]

            return _Response()

    class _FakeClient:
        messages = _FakeMessages()

    service._client = _FakeClient()
    service.model = "claude-sonnet-4-5"

    service._call_model(
        "system prompt",
        "user message",
        b"target-image-bytes",
        "image/jpeg",
        reference_images=[(b"ref-bytes", "image/jpeg", "כיתוב מאושר לדוגמה")],
    )

    content = captured_kwargs["messages"][0]["content"]
    types = [c["type"] for c in content]
    # intro text, ref image, ref caption text, target intro text, target image, user message
    assert types == ["text", "image", "text", "text", "image", "text"]
    assert content[-2]["source"]["data"]  # target image is the LAST image block
    import base64

    assert base64.b64decode(content[-2]["source"]["data"]) == b"target-image-bytes"


def test_build_ai_service_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    with pytest.raises(ai_service.AIServiceError):
        ai_service.build_ai_service()


def test_build_ai_service_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "not-a-real-provider")
    monkeypatch.setattr(config, "AI_API_KEY", "dummy-key")
    with pytest.raises(ai_service.AIServiceError):
        ai_service.build_ai_service()


def _valid_payload() -> str:
    return json.dumps({"caption": "מבט כללי על הרכיב."})


class _FlakyService(ai_service._RetryingAIService):
    provider_name = "fake"

    def __init__(self, fail_times: int, payload: str):
        self.fail_times = fail_times
        self.payload = payload
        self.calls = 0
        self.received_image_bytes = None
        self.max_retries = fail_times + 1  # always enough attempts to eventually succeed
        self.timeout = 1

    def _call_model(self, system_prompt, user_message, image_bytes, media_type, reference_images=()) -> str:
        self.calls += 1
        self.received_image_bytes = image_bytes
        if self.calls <= self.fail_times:
            raise TimeoutError("simulated transient failure")
        return self.payload


class _AlwaysFailingService(ai_service._RetryingAIService):
    provider_name = "fake"

    def __init__(self, max_retries: int):
        self.calls = 0
        self.max_retries = max_retries
        self.timeout = 1

    def _call_model(self, system_prompt, user_message, image_bytes, media_type, reference_images=()) -> str:
        self.calls += 1
        raise TimeoutError("simulated permanent failure")


def test_retry_succeeds_after_transient_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_service.time, "sleep", lambda _seconds: None)
    image_path = tmp_path / "photo.jpg"
    image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    image_path.write_bytes(image_bytes)

    service = _FlakyService(fail_times=2, payload=_valid_payload())
    caption = service.generate_photo_caption(image_path)

    assert caption == "מבט כללי על הרכיב."
    assert service.calls == 3
    assert service.received_image_bytes == image_bytes


def test_retry_exhausted_raises_ai_service_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_service.time, "sleep", lambda _seconds: None)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fake-jpeg-bytes")

    service = _AlwaysFailingService(max_retries=2)
    with pytest.raises(ai_service.AIServiceError):
        service.generate_photo_caption(image_path)
    assert service.calls == 3  # initial attempt + 2 retries


def test_parse_response_rejects_invalid_json():
    with pytest.raises(ai_service.AIServiceError):
        ai_service._RetryingAIService._parse_response("not json at all")


def test_parse_response_rejects_schema_mismatch():
    with pytest.raises(ai_service.AIServiceError):
        ai_service._RetryingAIService._parse_response(json.dumps({"caption": ["not", "a", "string"]}))


def test_parse_response_accepts_valid_payload():
    result = ai_service._RetryingAIService._parse_response(_valid_payload())
    assert result.caption == "מבט כללי על הרכיב."
