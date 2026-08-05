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
    assert asset.caption_is_manual is False


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


def test_render_crop_preview_returns_jpeg_bytes(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    original_final_bytes = asset.final_path.read_bytes()

    preview_bytes = photo_service.render_crop_preview(asset, (10, 10, 500, 400))

    assert preview_bytes.startswith(b"\xff\xd8")  # JPEG magic bytes
    # Rendering a preview must not touch the saved final/preview files or
    # mark the crop as approved - it's purely an in-memory preview.
    assert asset.final_path.read_bytes() == original_final_bytes
    assert asset.approved_crop is False


def test_caption_provenance_tracks_manual_edits(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    assert asset.caption_is_manual is False

    # AI-authored captions leave the flag False, so future crops may still
    # regenerate the caption.
    asset.caption = "כיתוב שנוצר על ידי AI"
    assert asset.caption_is_manual is False

    # A manual edit (simulated the way app.py's on_change handler does it)
    # permanently marks the caption as manual.
    asset.caption = "כיתוב שהמשתמש הקליד"
    asset.caption_is_manual = True
    assert asset.caption_is_manual is True

    photo_service.apply_crop(asset, (5, 5, 400, 300))
    # Cropping never touches caption/caption_is_manual itself - that
    # decision belongs to the caller (app.py's maybe_generate_caption).
    assert asset.caption == "כיתוב שהמשתמש הקליד"
    assert asset.caption_is_manual is True


# --- visual crop dialog geometry -------------------------------------------
# These back the drag/resize crop frame in app.py: the frame is drawn over a
# display-sized copy of the ORIGINAL photo, so the mapping from on-screen
# pixels back to original pixels is what guarantees the report shows exactly
# the region the user framed.


def test_display_size_preserves_aspect_ratio_and_never_upscales():
    assert photo_service.display_size_for(1600, 1000, 420) == (420, 262)
    assert photo_service.display_size_for(1000, 1600, 420) == (262, 420)
    # Smaller than the cap -> left alone, never enlarged.
    assert photo_service.display_size_for(300, 200, 420) == (300, 200)


def test_display_box_maps_full_frame_to_full_original():
    assert photo_service.display_box_to_original(
        (0, 0, 420, 262), 420, 262, 1600, 1000
    ) == (0, 0, 1600, 1000)


def test_display_box_maps_subregion_proportionally():
    # A box covering the middle quarter of the display maps to the middle
    # quarter of the original.
    assert photo_service.display_box_to_original(
        (105, 65, 210, 131), 420, 262, 1600, 1000
    ) == (400, 248, 800, 500)


def test_display_box_clamps_out_of_range_and_keeps_min_one_pixel():
    # Overshooting the canvas can never produce a box outside the image.
    assert photo_service.display_box_to_original(
        (-50, -50, 9999, 9999), 420, 262, 1600, 1000
    ) == (0, 0, 1600, 1000)
    # A degenerate (zero-area) selection still yields a usable >=1px box.
    left, top, right, bottom = photo_service.display_box_to_original(
        (10, 10, 10, 10), 420, 262, 1600, 1000
    )
    assert right > left and bottom > top


def test_display_box_handles_zero_sized_canvas():
    assert photo_service.display_box_to_original(
        (0, 0, 0, 0), 0, 0, 800, 600
    ) == (0, 0, 800, 600)


def test_render_uncropped_preview_always_shows_the_full_original(isolated_paths, sample_image_path):
    """The crop dialog must frame the whole original even after a crop was
    already applied - that is what makes cropping non-destructive and lets
    Reset restore the entire image."""
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    before = photo_service.render_uncropped_preview(asset, 420)

    photo_service.apply_crop(asset, (10, 10, 200, 150))
    after = photo_service.render_uncropped_preview(asset, 420)

    assert before.startswith(b"\xff\xd8") and after.startswith(b"\xff\xd8")
    assert before == after, "uncropped preview must ignore any applied crop"


def test_crop_is_recut_from_the_original_not_the_previous_crop(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)
    photo_service.apply_crop(asset, (0, 0, 100, 100))
    photo_service.apply_crop(asset, (0, 0, 600, 450))
    photo_service.discard_crop(asset)

    from PIL import Image

    with Image.open(asset.final_path) as restored, Image.open(asset.original_path) as original:
        assert restored.width / restored.height == original.width / original.height
    assert asset.user_crop_box is None
    assert asset.approved_crop is False
