"""Photo validation, resizing, preview generation, and crop suggestion.

Never infers technical or laboratory facts from photo content, and never
applies a crop automatically — `suggested_crop_box` is only ever a proposal
the UI shows the user; the final crop is whatever the user approves via
`apply_crop`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

import config

BoundingBox = Tuple[int, int, int, int]  # (left, top, right, bottom) in pixels


class PhotoValidationError(ValueError):
    """Raised with a Hebrew-friendly message when an uploaded file is unusable."""


@dataclass
class PhotoAsset:
    """Everything the app knows about one uploaded photo."""

    photo_id: str
    original_path: Path
    preview_path: Path
    final_path: Path
    width: int
    height: int
    suggested_crop_box: Optional[BoundingBox] = None
    user_crop_box: Optional[BoundingBox] = None
    suggested_caption: Optional[str] = None
    caption: str = ""
    order_index: int = 0
    approved_crop: bool = False
    crop_reviewed: bool = False


def validate_photo_file(source_path: Path) -> None:
    """Raises PhotoValidationError (Hebrew message) if the file can't be used."""
    if not source_path.exists() or not source_path.is_file():
        raise PhotoValidationError(f"הקובץ לא נמצא: {source_path.name}")

    extension = source_path.suffix.lower()
    if extension not in config.ALLOWED_PHOTO_EXTENSIONS:
        raise PhotoValidationError(
            f"סוג הקובץ '{extension}' אינו נתמך. יש להעלות תמונה מסוג: "
            + ", ".join(sorted(config.ALLOWED_PHOTO_EXTENSIONS))
        )

    size_mb = source_path.stat().st_size / (1024 * 1024)
    if size_mb > config.MAX_PHOTO_FILE_SIZE_MB:
        raise PhotoValidationError(
            f"הקובץ '{source_path.name}' גדול מדי ({size_mb:.1f}MB). "
            f"הגודל המרבי המותר הוא {config.MAX_PHOTO_FILE_SIZE_MB}MB."
        )

    try:
        with Image.open(source_path) as img:
            img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise PhotoValidationError(
            f"לא ניתן לפתוח את הקובץ '{source_path.name}' כתמונה תקינה."
        ) from exc


def _open_corrected(path: Path) -> Image.Image:
    """Open an image with EXIF rotation applied, converted to RGB."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _resized_copy(img: Image.Image, max_dimension: int) -> Image.Image:
    width, height = img.size
    scale = min(1.0, max_dimension / max(width, height))
    if scale >= 1.0:
        return img.copy()
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def suggest_crop_box(img: Image.Image) -> Optional[BoundingBox]:
    """Suggests a region of interest, only when an image-analysis provider is
    configured (config.IMAGE_ANALYSIS_PROVIDER). Never inspects or reasons
    about the photo's lab/technical content — purely a generic visual-salience
    heuristic (or, in the future, a pluggable vision provider).

    Returns None when no provider is configured, meaning "use the full image".
    """
    provider = config.IMAGE_ANALYSIS_PROVIDER
    if not provider:
        return None
    if provider == "heuristic":
        return _heuristic_center_crop(img)
    return None


def _heuristic_center_crop(img: Image.Image) -> BoundingBox:
    """A simple, content-agnostic suggestion: a centered crop retaining ~85%
    of the frame (trims uninformative borders phones often leave). This is a
    generic visual heuristic, not an interpretation of what is depicted."""
    width, height = img.size
    keep_ratio = 0.85
    new_w, new_h = round(width * keep_ratio), round(height * keep_ratio)
    left = (width - new_w) // 2
    top = (height - new_h) // 2
    return (left, top, left + new_w, top + new_h)


def ingest_photo(source_path: Path, order_index: int = 0) -> PhotoAsset:
    """Validates, then copies the original untouched and generates a preview
    and a Word-ready resized copy. Does not apply any crop."""
    validate_photo_file(source_path)

    photo_id = uuid.uuid4().hex[:12]
    extension = source_path.suffix.lower()

    original_dest = config.PHOTOS_ORIGINALS_DIR / f"{photo_id}{extension}"
    original_dest.write_bytes(source_path.read_bytes())

    img = _open_corrected(original_dest)
    width, height = img.size

    preview_img = _resized_copy(img, config.PHOTO_PREVIEW_MAX_DIMENSION_PX)
    preview_path = config.PHOTOS_PREVIEWS_DIR / f"{photo_id}.jpg"
    preview_img.save(preview_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    final_img = _resized_copy(img, config.PHOTO_MAX_DIMENSION_PX)
    final_path = config.PHOTOS_FINAL_DIR / f"{photo_id}.jpg"
    final_img.save(final_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    suggested_crop = suggest_crop_box(img)

    return PhotoAsset(
        photo_id=photo_id,
        original_path=original_dest,
        preview_path=preview_path,
        final_path=final_path,
        width=width,
        height=height,
        suggested_crop_box=suggested_crop,
        order_index=order_index,
    )


def apply_crop(asset: PhotoAsset, crop_box: BoundingBox) -> PhotoAsset:
    """Regenerates the final (Word-ready) image using a user-approved crop box.
    Never called automatically — only in response to explicit user approval."""
    img = _open_corrected(asset.original_path)
    left, top, right, bottom = crop_box
    left = max(0, min(left, img.width - 1))
    top = max(0, min(top, img.height - 1))
    right = max(left + 1, min(right, img.width))
    bottom = max(top + 1, min(bottom, img.height))

    cropped = img.crop((left, top, right, bottom))
    final_img = _resized_copy(cropped, config.PHOTO_MAX_DIMENSION_PX)
    final_img.save(asset.final_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    preview_img = _resized_copy(cropped, config.PHOTO_PREVIEW_MAX_DIMENSION_PX)
    preview_img.save(asset.preview_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    asset.user_crop_box = crop_box
    asset.approved_crop = True
    asset.crop_reviewed = True
    return asset


def discard_crop(asset: PhotoAsset) -> PhotoAsset:
    """Reverts to the uncropped, full-frame resized image."""
    img = _open_corrected(asset.original_path)

    final_img = _resized_copy(img, config.PHOTO_MAX_DIMENSION_PX)
    final_img.save(asset.final_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    preview_img = _resized_copy(img, config.PHOTO_PREVIEW_MAX_DIMENSION_PX)
    preview_img.save(asset.preview_path, "JPEG", quality=config.PHOTO_JPEG_QUALITY)

    asset.user_crop_box = None
    asset.approved_crop = False
    asset.crop_reviewed = True
    return asset
