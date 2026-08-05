from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ai_service
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

    def _call_model(self, system_prompt: str, user_message: str, image_bytes: bytes, media_type: str) -> str:
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

    def _call_model(self, system_prompt: str, user_message: str, image_bytes: bytes, media_type: str) -> str:
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
