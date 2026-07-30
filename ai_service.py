"""Provider-agnostic AI text-generation service.

Turns the user's short, factual inputs into structured Hebrew report text
(the "כללי" / "ממצאים" / "דיון ומסקנות" sections) written in the lab's house
style. The model is instructed, repeatedly and explicitly, to never invent
facts: it may only rephrase/organize what the user actually typed.

Usage:
    service = build_ai_service()  # reads AI_PROVIDER from .env
    result = service.generate_report_text(inputs)
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import List

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import config


class AIServiceError(RuntimeError):
    """Raised for configuration problems, provider errors, or invalid responses."""


class ReportInputs(BaseModel):
    """Raw, user-supplied facts. Nothing here is invented by the AI — it is
    only reorganized and written up in fluent Hebrew report style."""

    report_number: str
    report_date: str

    client_contact_name: str = ""
    client_company: str = ""
    client_address: str = ""
    client_city: str = ""

    subject: str
    claim_number: str = ""

    item: str = ""
    assembly: str = ""
    manufacturer_model: str = ""
    material: str = ""
    inspection_nature: str = ""
    license_number: str = ""

    findings_notes: str = Field(..., description="User's raw findings, in their own words")
    actions_taken: str = Field("", description="User's raw notes on actions taken during inspection")
    comments: str = Field("", description="Any additional free-form comments from the user")

    photo_count: int = 0


class GeneratedReportText(BaseModel):
    """Structured Hebrew report text, matching the template's free-text sections."""

    model_config = ConfigDict(extra="forbid")

    general_paragraphs: List[str] = Field(default_factory=list)
    findings_paragraphs: List[str] = Field(default_factory=list)
    discussion_paragraphs: List[str] = Field(default_factory=list)


REPORT_TEXT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "general_paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 Hebrew paragraphs for the 'כללי' (General/circumstances) section.",
        },
        "findings_paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hebrew paragraphs for the 'ממצאים' (Findings) section.",
        },
        "discussion_paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hebrew paragraphs for the 'דיון ומסקנות' (Discussion & Conclusions) section.",
        },
    },
    "required": ["general_paragraphs", "findings_paragraphs", "discussion_paragraphs"],
    "additionalProperties": False,
}


def _load_text(path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def build_prompt(inputs: ReportInputs) -> tuple[str, str]:
    """Builds (system_prompt, user_message) for the AI call."""
    style_guide = _load_text(config.STYLE_GUIDE_PATH)
    template_text = _load_text(config.REPORT_PROMPT_PATH)

    facts = {
        "מספר בדיקה": inputs.report_number,
        "תאריך": inputs.report_date,
        "לקוח/מזמין": inputs.client_contact_name,
        "חברה": inputs.client_company,
        "נושא": inputs.subject,
        "מספר תביעה": inputs.claim_number,
        "פריט": inputs.item,
        "מכלול": inputs.assembly,
        "יצרן/דגם": inputs.manufacturer_model,
        "חומר": inputs.material,
        "מהות הבדיקה": inputs.inspection_nature,
        "מספר רישוי": inputs.license_number,
        "ממצאים (הערות המשתמש)": inputs.findings_notes,
        "פעולות שבוצעו": inputs.actions_taken,
        "הערות נוספות": inputs.comments,
        "מספר תמונות מצורפות": inputs.photo_count,
    }
    facts_text = "\n".join(f"- {key}: {value}" for key, value in facts.items() if value not in ("", None))

    system_prompt = template_text or _DEFAULT_PROMPT
    system_prompt = system_prompt.replace("{{style_guide}}", style_guide or "(אין מדריך סגנון זמין)")

    user_message = (
        "להלן העובדות שמסר המשתמש. ארגן וכתוב אותן בעברית, בסגנון הדוחות של המעבדה, "
        "בפורמט JSON התואם את הסכימה שסופקה. אסור להוסיף כל עובדה, ממצא, מדידה, תאריך "
        "או מסקנה שלא נמסרו במפורש להלן.\n\n" + facts_text
    )
    return system_prompt, user_message


_DEFAULT_PROMPT = """\
אתה כותב הנדסי המסייע למעבדה הנדסית לחקר כשלים לנסח דוחות בעברית תקנית וברורה.

מדריך הסגנון של המעבדה:
{{style_guide}}

חוקים מחייבים:
1. כתוב עברית בלבד, בעברית תקנית, בגוף ראשון יחיד (״ביקרתי״, ״מצאתי״ וכו׳) בהתאם לסגנון המעבדה.
2. אסור בהחלט להמציא ממצאים, מדידות, תאריכים, פעולות או מסקנות שלא נמסרו על ידי המשתמש.
   אם מידע חסר, אין להשלים אותו - יש פשוט לנסח בקצרה את מה שכן נמסר.
3. אין להתייחס לתוכן חזותי של תמונות שלא תואר במפורש בטקסט שנמסר.
4. החזר אך ורק JSON תקני התואם את הסכימה שסופקה, ללא טקסט נוסף.
"""


class AIService(ABC):
    """Provider-independent interface for generating structured report text."""

    @abstractmethod
    def generate_report_text(self, inputs: ReportInputs) -> GeneratedReportText:
        raise NotImplementedError


def _validate_api_key(provider_name: str) -> str:
    if not config.AI_API_KEY:
        raise AIServiceError(
            f"AI_API_KEY is not set in .env (required for provider {provider_name!r}). "
            "Copy .env.example to .env and fill in a real API key."
        )
    return config.AI_API_KEY


class _RetryingAIService(AIService):
    """Shared retry/timeout/error-handling wrapper around a provider call."""

    provider_name = "base"

    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def generate_report_text(self, inputs: ReportInputs) -> GeneratedReportText:
        system_prompt, user_message = build_prompt(inputs)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw_json = self._call_model(system_prompt, user_message)
                return self._parse_response(raw_json)
            except (AIServiceError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
            except Exception as exc:  # provider SDK errors (timeouts, HTTP errors, etc.)
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 8))

        raise AIServiceError(
            f"{self.provider_name} provider failed after {self.max_retries + 1} attempt(s): {last_error}"
        ) from last_error

    def _call_model(self, system_prompt: str, user_message: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _parse_response(raw_json: str) -> GeneratedReportText:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise AIServiceError(f"Model did not return valid JSON: {exc}") from exc
        try:
            return GeneratedReportText.model_validate(data)
        except ValidationError as exc:
            raise AIServiceError(f"Model JSON did not match the expected schema: {exc}") from exc


class AnthropicAIService(_RetryingAIService):
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        super().__init__(api_key, model, timeout, max_retries)
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def _call_model(self, system_prompt: str, user_message: str) -> str:
        tool_name = "submit_report_text"
        response = self._client.messages.create(
            model=self.model or "claude-sonnet-4-5",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            tools=[
                {
                    "name": tool_name,
                    "description": "Submit the structured Hebrew report text.",
                    "input_schema": REPORT_TEXT_JSON_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return json.dumps(block.input)
        raise AIServiceError("Anthropic response did not include the expected tool call")


class OpenAIAIService(_RetryingAIService):
    provider_name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        super().__init__(api_key, model, timeout, max_retries)
        import openai

        self._client = openai.OpenAI(api_key=api_key, timeout=timeout)

    def _call_model(self, system_prompt: str, user_message: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "report_text",
                    "schema": REPORT_TEXT_JSON_SCHEMA,
                    "strict": True,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise AIServiceError("OpenAI response had no content")
        return content


_PROVIDERS = {
    "anthropic": AnthropicAIService,
    "openai": OpenAIAIService,
}


def build_ai_service() -> AIService:
    """Builds the AIService configured via .env (AI_PROVIDER / AI_API_KEY / AI_MODEL)."""
    provider_name = config.AI_PROVIDER
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise AIServiceError(
            f"Unknown AI_PROVIDER={provider_name!r}. Supported providers: {sorted(_PROVIDERS)}"
        )
    api_key = _validate_api_key(provider_name)
    return provider_cls(
        api_key=api_key,
        model=config.AI_MODEL,
        timeout=config.AI_TIMEOUT_SECONDS,
        max_retries=config.AI_MAX_RETRIES,
    )
