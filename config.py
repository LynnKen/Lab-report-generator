"""Application settings and file paths for Lab Report Generator."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # PyInstaller's "contents directory" layout (6.x+) extracts bundled
    # read-only data (templates/prompts/data/icons, from the .spec's
    # `datas`) into a support folder - sys._MEIPASS - which sits INSIDE
    # _internal, not next to ReportGenerator.exe.
    RESOURCES_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    # sys.executable is ReportGenerator.exe itself; its parent is the folder
    # the user actually sees, one level above _internal, and where .env must
    # be placed so it's never bundled/overwritten by a repackage.
    APP_DIR = Path(sys.executable).resolve().parent
    # The packaged app must never write inside its own bundle (often
    # reinstalled/overwritten wholesale) - generated reports and working
    # files go to a normal per-user Documents folder instead.
    USER_DATA_DIR = Path.home() / "Documents" / "Report Generator"
else:
    RESOURCES_DIR = BASE_DIR
    APP_DIR = BASE_DIR
    USER_DATA_DIR = BASE_DIR

load_dotenv(APP_DIR / ".env")

# --- Template paths ---------------------------------------------------

TEMPLATES_DIR = RESOURCES_DIR / "templates"
CANONICAL_TEMPLATE_PATH = TEMPLATES_DIR / "empty_report_template.docx"
REFERENCE_REPORTS_DIR = TEMPLATES_DIR / "reference_reports"
ICONS_DIR = RESOURCES_DIR / "icons"

# The templates/ folder holds original, human-authored files only (canonical
# template + reference reports) and must never be written to by the app. The
# compiled, Jinja-tagged working copy of the template is a generated
# artifact, not a bundled resource, so - when packaged - it lives under the
# writable work dir below, not inside the read-only RESOURCES_DIR.
if FROZEN:
    GENERATED_DIR = USER_DATA_DIR / "work" / "templates"
else:
    GENERATED_DIR = BASE_DIR / "build" / "templates"
COMPILED_TEMPLATE_PATH = GENERATED_DIR / "report_jinja_template.docx"

# --- Prompt / style guide ----------------------------------------------

PROMPTS_DIR = RESOURCES_DIR / "prompts"
CAPTION_PROMPT_PATH = PROMPTS_DIR / "caption_prompt.txt"

DATA_DIR = RESOURCES_DIR / "data"
STYLE_GUIDE_PATH = DATA_DIR / "style_guide.md"
REFERENCE_EXAMPLES_DIR = DATA_DIR / "reference_examples"
# Small, derived, read-only asset pack built by build_approved_reference_pack.py
# from the raw dataset above - see approved_reference_service.py. Frozen
# (production) builds bundle ONLY this pack (ReportGenerator.spec), never the
# raw reference_examples/ tree (PDFs, full-size images, unapproved/draft
# blocks, review audit history).
APPROVED_REFERENCE_PACK_DIR = DATA_DIR / "approved_reference_pack"

# --- Reference Review ("בדיקת דוחות ייחוס") tab visibility --------------
# Dev/annotation tooling only - must never appear in the build given to an
# end user (it reads/writes the raw dataset, which a frozen build doesn't
# even bundle). Default: on in a normal `python app.py` dev run, off in a
# frozen/production build; explicitly overridable (e.g. to sanity-check a
# packaged build without rebuilding it).


def _resolve_review_tab_enabled(frozen: bool, env_value: str = "") -> bool:
    env_value = env_value.strip().lower()
    if env_value in ("1", "true"):
        return True
    if env_value in ("0", "false"):
        return False
    return not frozen


REFERENCE_REVIEW_ENABLED = _resolve_review_tab_enabled(
    FROZEN, os.getenv("REFERENCE_REVIEW_ENABLED", "")
)

# A frozen build never bundles the raw, constantly-growing reference_examples/
# dataset (see ReportGenerator.spec) - it reads the small derived pack instead.
# A dev run keeps reading the live dataset directly, so a block approved a
# moment ago in the review tab is available immediately, with no build step.
USE_APPROVED_REFERENCE_PACK = FROZEN

# --- Output / working directories --------------------------------------

if FROZEN:
    REPORTS_DIR = USER_DATA_DIR / "Reports"
    PHOTOS_WORK_DIR = USER_DATA_DIR / "work" / "photos"
else:
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

# --- Approved-reference context (see approved_reference_service.py) -----
# A local, dynamically re-scanned layer over data/reference_examples/ that
# lets photo captioning use human-approved terminology and sentence style
# without fine-tuning. Text mode sends a small bounded selection of approved
# caption text; visual mode can also send selected approved preview images.
# Disable entirely with APPROVED_REFERENCES_ENABLED=0.
APPROVED_REFERENCES_ENABLED = os.getenv("APPROVED_REFERENCES_ENABLED", "1").strip() not in (
    "0",
    "false",
    "False",
)
# Bounded example count keeps prompt size (and therefore API cost) constant
# even as the approved dataset keeps growing.
APPROVED_REFERENCE_MAX_EXAMPLES = int(os.getenv("APPROVED_REFERENCE_MAX_EXAMPLES", "4"))
# Hard cap (characters) on the rendered reference-context text block.
APPROVED_REFERENCE_MAX_CONTEXT_CHARS = int(os.getenv("APPROVED_REFERENCE_MAX_CONTEXT_CHARS", "1200"))

# "text" (default): only a compact Hebrew text block of approved wording is
#   added to the prompt - no reference images are ever sent.
# "visual": in addition, a bounded number of approved SINGLE-image blocks are
#   sent as real image+caption few-shot examples alongside the target photo,
#   in the same request. Multi-image blocks are still only ever used as text
#   (see approved_reference_service.build_visual_reference_payload).
# "off": equivalent to APPROVED_REFERENCES_ENABLED=0 - kept as an explicit
#   mode value so it can be set/toggled independently in .env.
APPROVED_REFERENCE_MODE = os.getenv("APPROVED_REFERENCE_MODE", "text").strip().lower()
# Longest side (pixels) of each visual reference example sent to the model -
# keeps "visual" mode's request size/cost bounded regardless of how large the
# approved dataset's source photos are.
APPROVED_REFERENCE_PREVIEW_MAX_DIMENSION_PX = int(
    os.getenv("APPROVED_REFERENCE_PREVIEW_MAX_DIMENSION_PX", "480")
)

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
