from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ai_service
import config


def test_report_inputs_requires_findings_notes():
    with pytest.raises(ValidationError):
        ai_service.ReportInputs(
            report_number="C1",
            report_date="30.07.26",
            subject="נושא",
        )


def test_report_inputs_accepts_minimal_valid_data():
    inputs = ai_service.ReportInputs(
        report_number="C1",
        report_date="30.07.26",
        subject="נושא",
        findings_notes="ממצא כלשהו",
    )
    assert inputs.claim_number == ""
    assert inputs.photo_count == 0


def test_generated_report_text_defaults_to_empty_lists():
    text = ai_service.GeneratedReportText()
    assert text.general_paragraphs == []
    assert text.findings_paragraphs == []
    assert text.discussion_paragraphs == []


def test_generated_report_text_rejects_non_list_field():
    with pytest.raises(ValidationError):
        ai_service.GeneratedReportText(general_paragraphs="not a list")


def test_build_prompt_includes_style_guide_and_user_facts(tmp_path, monkeypatch):
    style_guide = tmp_path / "style_guide.md"
    style_guide.write_text("סגנון: כתוב בגוף ראשון.", encoding="utf-8")
    prompt_file = tmp_path / "report_prompt.txt"
    prompt_file.write_text("הנחיות מערכת:\n{{style_guide}}\n", encoding="utf-8")

    monkeypatch.setattr(config, "STYLE_GUIDE_PATH", style_guide)
    monkeypatch.setattr(config, "REPORT_PROMPT_PATH", prompt_file)

    inputs = ai_service.ReportInputs(
        report_number="C1",
        report_date="30.07.26",
        subject="נזק לדוגמה",
        findings_notes="נמצא סדק",
    )
    system_prompt, user_message = ai_service.build_prompt(inputs)

    assert "כתוב בגוף ראשון" in system_prompt
    assert "נזק לדוגמה" in user_message
    assert "נמצא סדק" in user_message


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
    return json.dumps(
        {
            "general_paragraphs": ["פסקה כללית."],
            "findings_paragraphs": ["ממצא ראשון."],
            "discussion_paragraphs": ["מסקנה."],
        }
    )


class _FlakyService(ai_service._RetryingAIService):
    provider_name = "fake"

    def __init__(self, fail_times: int, payload: str):
        self.fail_times = fail_times
        self.payload = payload
        self.calls = 0
        self.max_retries = fail_times + 1  # always enough attempts to eventually succeed
        self.timeout = 1

    def _call_model(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("simulated transient failure")
        return self.payload


class _AlwaysFailingService(ai_service._RetryingAIService):
    provider_name = "fake"

    def __init__(self, max_retries: int):
        self.calls = 0
        self.max_retries = max_retries
        self.timeout = 1

    def _call_model(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        raise TimeoutError("simulated permanent failure")


def test_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr(ai_service.time, "sleep", lambda _seconds: None)
    service = _FlakyService(fail_times=2, payload=_valid_payload())
    inputs = ai_service.ReportInputs(
        report_number="C1", report_date="30.07.26", subject="נושא", findings_notes="ממצא"
    )
    result = service.generate_report_text(inputs)
    assert result.general_paragraphs == ["פסקה כללית."]
    assert service.calls == 3


def test_retry_exhausted_raises_ai_service_error(monkeypatch):
    monkeypatch.setattr(ai_service.time, "sleep", lambda _seconds: None)
    service = _AlwaysFailingService(max_retries=2)
    inputs = ai_service.ReportInputs(
        report_number="C1", report_date="30.07.26", subject="נושא", findings_notes="ממצא"
    )
    with pytest.raises(ai_service.AIServiceError):
        service.generate_report_text(inputs)
    assert service.calls == 3  # initial attempt + 2 retries


def test_parse_response_rejects_invalid_json():
    with pytest.raises(ai_service.AIServiceError):
        ai_service._RetryingAIService._parse_response("not json at all")


def test_parse_response_rejects_schema_mismatch():
    with pytest.raises(ai_service.AIServiceError):
        ai_service._RetryingAIService._parse_response(json.dumps({"unexpected_field": 123}))


def test_parse_response_accepts_valid_payload():
    result = ai_service._RetryingAIService._parse_response(_valid_payload())
    assert result.findings_paragraphs == ["ממצא ראשון."]
