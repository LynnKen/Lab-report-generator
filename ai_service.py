"""Provider-agnostic AI photo-captioning service.

Given a single (already resized/cropped) photo, asks a vision-capable model
to write one short Hebrew caption describing only what is generically visible
in the image (component type, camera angle, general/close-up view). The model
is instructed, repeatedly and explicitly, to never state or imply a lab
finding, measurement, cause, or conclusion — those belong exclusively to the
user-authored "ממצאים"/"דיון ומסקנות" report sections.

Usage:
    service = build_ai_service()  # reads AI_PROVIDER from .env
    caption = service.generate_photo_caption(photo_path)
"""

from __future__ import annotations

import base64
import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

import config


class AIServiceError(RuntimeError):
    """Raised for configuration problems, provider errors, or invalid responses."""


class PhotoCaption(BaseModel):
    """A single AI-suggested Hebrew caption for one photo."""

    model_config = ConfigDict(extra="forbid")

    caption: str


PHOTO_CAPTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {
            "type": "string",
            "description": (
                "One short Hebrew caption describing only what is generically visible in "
                "the photo (component type, camera angle, general/close-up view). Must "
                "never state or imply a lab finding, damage, measurement, cause, or "
                "conclusion."
            ),
        },
    },
    "required": ["caption"],
    "additionalProperties": False,
}


def _load_text(path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def build_caption_prompt() -> tuple[str, str]:
    """Builds (system_prompt, user_message) for a photo-captioning call."""
    style_guide = _load_text(config.STYLE_GUIDE_PATH)
    template_text = _load_text(config.CAPTION_PROMPT_PATH)

    system_prompt = template_text or _DEFAULT_PROMPT
    system_prompt = system_prompt.replace("{{style_guide}}", style_guide or "(אין מדריך סגנון זמין)")

    user_message = (
        "כתוב כיתוב קצר אחד בעברית לתמונה המצורפת, בהתאם לחוקים שהוגדרו. "
        "החזר אך ורק JSON התואם את הסכימה שסופקה."
    )
    return system_prompt, user_message


_DEFAULT_PROMPT = """\
אתה כותב הנדסי המסייע למעבדה הנדסית לחקר כשלים לכתוב כיתובי תמונות קצרים בעברית.

מדריך הסגנון של המעבדה:
{{style_guide}}

חוקים מחייבים:
1. כתוב כיתוב קצר אחד בעברית תקנית (עד משפט קצר) המתאר אך ורק מה שנראה גנרית
   בתמונה (למשל: סוג המכלול/הרכיב המצולם, זווית הצילום, מבט כללי/מבט מקרוב).
2. אסור בהחלט לקבוע, לרמוז או להמציא ממצא, נזק, מדידה, מנגנון כשל, סיבה או
   מסקנה על סמך התמונה - אלו שייכים אך ורק למשתמש בפרקי "ממצאים" ו"דיון ומסקנות".
3. אם אינך בטוח מה מוצג בתמונה, כתוב תיאור כללי וזהיר בלבד, ולעולם אל תנחש פרטים טכניים.
4. החזר אך ורק JSON תקני התואם את הסכימה שסופקה, ללא טקסט נוסף.
"""


class AIService(ABC):
    """Provider-independent interface for generating a photo caption."""

    @abstractmethod
    def generate_photo_caption(self, image_path: Path) -> str:
        raise NotImplementedError


def _validate_api_key(provider_name: str) -> str:
    if not config.AI_API_KEY:
        raise AIServiceError(
            f"AI_API_KEY is not set in .env (required for provider {provider_name!r}). "
            "Copy .env.example to .env and fill in a real API key."
        )
    return config.AI_API_KEY


class _RetryingAIService(AIService):
    """Shared retry/timeout/error-handling wrapper around a provider call.

    The only provider-specific piece any adapter must implement is
    `_call_model(system_prompt, user_message, image_bytes, media_type)`,
    keeping this interface easy to extend with further providers (e.g. a
    future Gemini adapter) without touching the retry/parsing logic.
    """

    provider_name = "base"

    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def generate_photo_caption(self, image_path: Path) -> str:
        system_prompt, user_message = build_caption_prompt()
        image_bytes = image_path.read_bytes()
        media_type = "image/jpeg"  # photo_service always saves final/cropped images as JPEG

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw_json = self._call_model(system_prompt, user_message, image_bytes, media_type)
                return self._parse_response(raw_json).caption.strip()
            except (AIServiceError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
            except Exception as exc:  # provider SDK errors (timeouts, HTTP errors, etc.)
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 8))

        raise AIServiceError(
            f"{self.provider_name} provider failed after {self.max_retries + 1} attempt(s): {last_error}"
        ) from last_error

    def _call_model(self, system_prompt: str, user_message: str, image_bytes: bytes, media_type: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _parse_response(raw_json: str) -> PhotoCaption:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise AIServiceError(f"Model did not return valid JSON: {exc}") from exc
        try:
            return PhotoCaption.model_validate(data)
        except ValidationError as exc:
            raise AIServiceError(f"Model JSON did not match the expected schema: {exc}") from exc


class AnthropicAIService(_RetryingAIService):
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float, max_retries: int):
        super().__init__(api_key, model, timeout, max_retries)
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def _call_model(self, system_prompt: str, user_message: str, image_bytes: bytes, media_type: str) -> str:
        tool_name = "submit_photo_caption"
        response = self._client.messages.create(
            model=self.model or "claude-sonnet-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": user_message},
                    ],
                }
            ],
            tools=[
                {
                    "name": tool_name,
                    "description": "Submit the suggested Hebrew photo caption.",
                    "input_schema": PHOTO_CAPTION_JSON_SCHEMA,
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

    def _call_model(self, system_prompt: str, user_message: str, image_bytes: bytes, media_type: str) -> str:
        data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        response = self._client.chat.completions.create(
            model=self.model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_message},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "photo_caption",
                    "schema": PHOTO_CAPTION_JSON_SCHEMA,
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
    # A future Gemini adapter only needs its own _call_model implementation
    # (via google-generativeai's multimodal generate_content) registered here
    # under "gemini" — no other changes required.
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
