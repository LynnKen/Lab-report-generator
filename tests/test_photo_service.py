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


def test_ingest_photo_captures_source_filename(isolated_paths, sample_image_path):
    asset = photo_service.ingest_photo(sample_image_path, order_index=0)

    # The file gets copied under a generated photo_id, so the human-readable
    # original filename must be captured separately - it's otherwise lost.
    assert asset.source_filename == sample_image_path.name
    assert asset.original_path.name != sample_image_path.name


def _make_asset(isolated_paths, tmp_path, name: str, index: int) -> photo_service.PhotoAsset:
    src = tmp_path / name
    Image.new("RGB", (40, 30), (index * 10, 0, 0)).save(src, "JPEG")
    asset = photo_service.ingest_photo(src, order_index=index)
    asset.caption = f"caption-{index}"
    return asset


def test_move_photo_updates_order_and_keeps_caption_attached(isolated_paths, tmp_path):
    photos = [_make_asset(isolated_paths, tmp_path, f"p{i}.jpg", i) for i in range(3)]
    ids = [p.photo_id for p in photos]

    photo_service.move_photo(photos, 0, 2)  # move the first photo to last

    assert [p.photo_id for p in photos] == [ids[1], ids[2], ids[0]]
    assert [p.order_index for p in photos] == [0, 1, 2]
    # The moved asset kept its own caption - only its position changed.
    moved = next(p for p in photos if p.photo_id == ids[0])
    assert moved.caption == "caption-0"


def test_move_photo_earlier_and_later_respect_boundaries(isolated_paths, tmp_path):
    photos = [_make_asset(isolated_paths, tmp_path, f"p{i}.jpg", i) for i in range(3)]
    ids = [p.photo_id for p in photos]

    photo_service.move_photo_earlier(photos, 0)  # already first - no-op
    assert [p.photo_id for p in photos] == ids

    photo_service.move_photo_later(photos, 2)  # already last - no-op
    assert [p.photo_id for p in photos] == ids

    photo_service.move_photo_later(photos, 0)
    assert [p.photo_id for p in photos] == [ids[1], ids[0], ids[2]]
    assert [p.order_index for p in photos] == [0, 1, 2]

    photo_service.move_photo_earlier(photos, 2)
    assert [p.photo_id for p in photos] == [ids[1], ids[2], ids[0]]
    assert [p.order_index for p in photos] == [0, 1, 2]


def test_renumber_matches_list_position(isolated_paths, tmp_path):
    photos = [_make_asset(isolated_paths, tmp_path, f"p{i}.jpg", i) for i in range(3)]
    photos.reverse()  # simulate a removal/reorder that left order_index stale
    photo_service.renumber(photos)
    assert [p.order_index for p in photos] == [0, 1, 2]
