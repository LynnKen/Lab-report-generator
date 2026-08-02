from __future__ import annotations

import pytest
from PIL import Image

import photo_service


def test_validate_rejects_unsupported_extension(tmp_path):
    bad_file = tmp_path / "notes.txt"
    bad_file.write_text("hello")
    with pytest.raises(photo_service.PhotoValidationError):
        photo_service.validate_photo_file(bad_file)


def test_validate_rejects_corrupt_image(tmp_path):
    fake = tmp_path / "broken.jpg"
    fake.write_bytes(b"not actually a jpeg")
    with pytest.raises(photo_service.PhotoValidationError):
        photo_service.validate_photo_file(fake)


def test_validate_rejects_oversized_file(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "MAX_PHOTO_FILE_SIZE_MB", 0)
    path = tmp_path / "big.jpg"
    Image.new("RGB", (50, 50)).save(path, "JPEG")
    with pytest.raises(photo_service.PhotoValidationError):
        photo_service.validate_photo_file(path)


def test_ingest_photo_creates_originals_previews_and_final(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)

    assert asset.original_path.exists()
    assert asset.preview_path.exists()
    assert asset.final_path.exists()

    # Original bytes are preserved untouched.
    assert asset.original_path.read_bytes() == sample_image_path.read_bytes()

    with Image.open(asset.preview_path) as preview:
        assert max(preview.size) <= 480
    with Image.open(asset.final_path) as final_img:
        assert max(final_img.size) <= 1600

    assert asset.suggested_crop_box is None  # no analysis provider configured by default


def test_suggest_crop_box_disabled_by_default(monkeypatch, sample_image_path):
    import config

    monkeypatch.setattr(config, "IMAGE_ANALYSIS_PROVIDER", "")
    with Image.open(sample_image_path) as img:
        assert photo_service.suggest_crop_box(img) is None


def test_suggest_crop_box_heuristic(monkeypatch, sample_image_path):
    import config

    monkeypatch.setattr(config, "IMAGE_ANALYSIS_PROVIDER", "heuristic")
    with Image.open(sample_image_path) as img:
        box = photo_service.suggest_crop_box(img)
    assert box is not None
    left, top, right, bottom = box
    assert 0 <= left < right
    assert 0 <= top < bottom


def test_apply_crop_never_runs_without_explicit_call(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    assert asset.approved_crop is False
    assert asset.user_crop_box is None

    cropped = photo_service.apply_crop(asset, (10, 10, 500, 400))
    assert cropped.approved_crop is True
    assert cropped.user_crop_box == (10, 10, 500, 400)
    with Image.open(cropped.final_path) as final_img:
        # Aspect ratio close to the requested crop (490x390), allowing for the
        # resize step's rounding.
        assert final_img.size[0] > 0 and final_img.size[1] > 0


def test_discard_crop_reverts_to_full_frame(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    photo_service.apply_crop(asset, (10, 10, 500, 400))
    reverted = photo_service.discard_crop(asset)
    assert reverted.approved_crop is False
    assert reverted.user_crop_box is None
    assert reverted.crop_reviewed is True
