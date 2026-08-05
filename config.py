"""Application settings and file paths for Lab Report Generator."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# --- Template paths ---------------------------------------------------

TEMPLATES_DIR = BASE_DIR / "templates"
CANONICAL_TEMPLATE_PATH = TEMPLATES_DIR / "empty_report_template.docx"

# The templates/ folder holds original, human-authored files only (canonical
# template + reference reports) and must never be written to by the app.
# The compiled, Jinja-tagged working copy of the template lives here instead.
GENERATED_DIR = BASE_DIR / "build" / "templates"
COMPILED_TEMPLATE_PATH = GENERATED_DIR / "report_jinja_template.docx"
REFERENCE_REPORTS_DIR = TEMPLATES_DIR / "reference_reports"

# --- Prompt / style guide ----------------------------------------------

PROMPTS_DIR = BASE_DIR / "prompts"
CAPTION_PROMPT_PATH = PROMPTS_DIR / "caption_prompt.txt"

DATA_DIR = BASE_DIR / "data"
STYLE_GUIDE_PATH = DATA_DIR / "style_guide.md"

# --- Output / working directories --------------------------------------

REPORTS_DIR = BASE_DIR / "reports"
PHOTOS_WORK_DIR = BASE_DIR / "work" / "photos"
PHOTOS_ORIGINALS_DIR = PHOTOS_WORK_DIR / "originals"
PHOTOS_PREVIEWS_DIR = PHOTOS_WORK_DIR / "previews"
PHOTOS_FINAL_DIR = PHOTOS_WORK_DIR / "final"

for _dir in (
    GENERATED_DIR,
    REPORTS_DIR,
    PHOTOS_ORIGINALS_DIR,
    PHOTOS_PREVIEWS_DIR,
    PHOTOS_FINAL_DIR,
):
    _dir.mkdir(parents=True, exist_ok=True)

# --- AI provider settings (read from .env) ------------------------------

AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "2"))

# Vision-capable provider for photo region-of-interest suggestions.
# Left empty by default: photo_service only attempts crop/ROI suggestions
# when this is explicitly configured.
IMAGE_ANALYSIS_PROVIDER = os.getenv("IMAGE_ANALYSIS_PROVIDER", "").strip().lower()

# --- Photo processing settings ------------------------------------------

PHOTO_MAX_DIMENSION_PX = int(os.getenv("PHOTO_MAX_DIMENSION_PX", "1600"))
PHOTO_PREVIEW_MAX_DIMENSION_PX = int(os.getenv("PHOTO_PREVIEW_MAX_DIMENSION_PX", "480"))
PHOTO_JPEG_QUALITY = int(os.getenv("PHOTO_JPEG_QUALITY", "88"))
PHOTO_GRID_COLUMNS = int(os.getenv("PHOTO_GRID_COLUMNS", "2"))
# Photos per photo-appendix page. python-docx/docxtpl cannot know Word's real
# pagination ahead of time, so appendix pages are grouped deterministically
# at this fixed capacity (forced page breaks between groups) rather than
# left to natural reflow - this is what lets each page's photos stay glued
# to their own caption block. Kept conservative (2 rows of 2) so a full
# group + its caption text reliably fits on one page.
PHOTOS_PER_APPENDIX_PAGE = int(os.getenv("PHOTOS_PER_APPENDIX_PAGE", "4"))

# Photo-appendix layout. Images are scaled down slightly and given a real
# gutter so the page reads as a set of distinct photos rather than one dense
# block; the number beside each photo is enlarged so it stays legible.
PHOTO_IMAGE_SCALE = float(os.getenv("PHOTO_IMAGE_SCALE", "0.88"))
PHOTO_GUTTER_MM = float(os.getenv("PHOTO_GUTTER_MM", "8"))
PHOTO_NUMBER_FONT_PT = float(os.getenv("PHOTO_NUMBER_FONT_PT", "14"))
ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
MAX_PHOTO_FILE_SIZE_MB = int(os.getenv("MAX_PHOTO_FILE_SIZE_MB", "25"))

# --- Page-count automation (MS Word COM, see word_page_counter.py) ------

PAGE_COUNT_AUTOMATION_ENABLED = os.getenv("PAGE_COUNT_AUTOMATION_ENABLED", "1").strip() not in (
    "0",
    "false",
    "False",
)
PAGE_COUNT_MAX_ATTEMPTS = int(os.getenv("PAGE_COUNT_MAX_ATTEMPTS", "3"))

# --- Misc -----------------------------------------------------------------

APP_TITLE_HE = "מחולל דוחות מעבדה"

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename_component(text: str) -> str:
    """Strip characters that are illegal in Windows filenames, keep Hebrew intact."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", text or "")
    cleaned = cleaned.strip().strip(".")
    return cleaned or "ללא_שם"


def build_report_filename(report_number: str) -> str:
    """Matches the lab's real naming convention observed in its reference
    reports (e.g. "C2501508pub.docx"), not an invented format."""
    return f"{sanitize_filename_component(report_number)}pub.docx"
