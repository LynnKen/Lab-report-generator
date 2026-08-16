from __future__ import annotations

import docx
from docx.oxml.ns import qn

import config
import photo_service
import report_builder
from report_builder import PHOTO_APPENDIX_BOOKMARK, ReportBodyText, ReportInputs


def _build_sample_inputs() -> ReportInputs:
    return ReportInputs(
        report_number="C0000123",
        report_date="30.07.26",
        client_contact_name="ישראל ישראלי",
        client_company='חברת בדיקה בע"מ',
        client_address="רחוב הבדיקה 1",
        client_city="תל אביב",
        subject="בדיקת נזק לדוגמה",
        claim_number="123456",
        item="פריט בדיקה",
        assembly="מכלול בדיקה",
        manufacturer_model="יצרן א' דגם ב'",
        material="פלדה",
        inspection_nature="חזותית",
        license_number="000000",
    )


def _build_sample_text() -> ReportBodyText:
    return ReportBodyText(
        general_paragraphs=["ביקרתי במוסך לבדיקת הנזק.", "התקבלו צילומים נוספים."],
        findings_paragraphs=["נמצא סדק בבסיס החלק (תמונה 1).", "החלק תקין פרט לסדק זה (תמונה 2)."],
        discussion_paragraphs=["הסדק נובע ככל הנראה מעייפות חומר."],
    )


def _make_photos(isolated_paths, tmp_path, count=2):
    from PIL import Image

    photos = []
    for i in range(count):
        src = tmp_path / f"photo_{i}.jpg"
        Image.new("RGB", (800, 600), (10 * i, 50, 200)).save(src, "JPEG")
        asset = photo_service.ingest_photo(src, order_index=i)
        asset.caption = f"כיתוב לתמונה {i + 1}"
        photos.append(asset)
    return photos


def _bookmark_names(document: docx.Document) -> set[str]:
    names = set()
    for bm in document.element.body.iter(qn("w:bookmarkStart")):
        names.add(bm.get(qn("w:name")))
    return names


def test_build_report_creates_expected_file(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=photos,
    )

    output_path, warning = report_builder.build_report(data)

    assert output_path.exists()
    assert output_path.parent == isolated_paths["reports_dir"]
    assert output_path.name == "C0000123pub.docx"
    # Automation is disabled in tests (see conftest.isolated_paths), so a
    # warning is expected, and the doc still needs to exist and be valid.
    assert warning is not None


def test_build_report_content_and_photo_grid(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=3)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=photos,
    )

    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    all_text = "\n".join(p.text for p in document.paragraphs)
    assert "בדיקת נזק לדוגמה" in all_text
    assert "נמצא סדק בבסיס החלק" in all_text
    assert "הסדק נובע ככל הנראה מעייפות חומר" in all_text

    # The report number must appear in the header, not a literal placeholder.
    header_text = "\n".join(p.text for p in document.sections[0].header.paragraphs)
    assert "C0000123" in header_text
    assert "*מספר בדיקה*" not in header_text
    assert "מספר בדיקה" not in header_text  # no leftover literal label either

    # Word must silently recompute the header's PAGE/NUMPAGES fields the
    # instant the file is opened, rather than showing whatever was cached
    # the last time a human edited the canonical template.
    update_fields = document.settings.element.find(qn("w:updateFields"))
    assert update_fields is not None
    assert update_fields.get(qn("w:val")) == "true"

    # The legal disclaimer table must survive untouched.
    disclaimer_texts = [t.rows[0].cells[0].text for t in document.tables if "התנאות" in t.rows[0].cells[0].text]
    assert disclaimer_texts, "legal disclaimer table missing or altered"

    # The photo grid table: 3 photos, one group (capacity 4 by default) ->
    # 2 photo rows + 1 merged caption row.
    photo_tables = [
        t for t in document.tables if any("תמונה 1" in c.text for row in t.rows for c in row.cells)
    ]
    assert photo_tables, "photo grid table not found"
    grid = photo_tables[0]
    assert len(grid.rows) == 3

    # Bare, bold numbers directly beneath each photo (not full captions).
    for n in ("1", "2", "3"):
        assert any(
            p.text.strip() == n for row in grid.rows[:2] for cell in row.cells for p in cell.paragraphs
        ), f"photo number {n} not found beneath its photo"

    # One aggregated, numbered caption block beneath the whole group.
    caption_cell_text = grid.rows[2].cells[0].text
    assert "תמונה 1:" in caption_cell_text
    assert "1. כיתוב לתמונה 1" in caption_cell_text
    assert "2. כיתוב לתמונה 2" in caption_cell_text
    assert "3. כיתוב לתמונה 3" in caption_cell_text
    heading_paragraph = grid.rows[2].cells[0].paragraphs[0]
    heading_run = heading_paragraph.runs[0]
    assert heading_run.bold is True
    assert heading_run.underline is True

    # Every row (photo rows + the merged caption row) must be non-splittable.
    for row in grid.rows:
        trPr = row._tr.find(qn("w:trPr"))
        assert trPr is not None and trPr.find(qn("w:cantSplit")) is not None

    # The first photo's own paragraph carries the forced page break and the
    # appendix bookmark - no separate blank anchor paragraph exists.
    first_image_paragraph = grid.rows[0].cells[0].paragraphs[0]
    assert first_image_paragraph.paragraph_format.page_break_before is True
    assert PHOTO_APPENDIX_BOOKMARK in _bookmark_names(document)

    # Page-count automation is disabled in tests -> "-" placeholders, and a
    # non-fatal warning explaining why.
    case_info_table = document.tables[0]
    pages_text = case_info_table.rows[1].cells[0].text
    assert "עמודים" in pages_text and "-" in pages_text
    assert "עמודי תמונות" in pages_text and "-" in pages_text


def test_build_report_photo_grid_spans_multiple_pages(isolated_paths, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHOTOS_PER_APPENDIX_PAGE", 4)
    photos = _make_photos(isolated_paths, tmp_path, count=6)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=photos,
    )

    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    photo_tables = [
        t for t in document.tables if any("תמונה 1" in c.text for row in t.rows for c in row.cells)
    ]
    assert photo_tables
    grid = photo_tables[0]
    # Group 1: 4 photos / 2 columns -> 2 photo rows + 1 caption row.
    # Group 2: 2 photos / 2 columns -> 1 photo row + 1 caption row.
    assert len(grid.rows) == 2 + 1 + 1 + 1

    first_group_caption = grid.rows[2].cells[0].text
    assert "תמונה 1:" in first_group_caption and "(המשך)" not in first_group_caption
    assert "4. כיתוב לתמונה 4" in first_group_caption
    assert "5. כיתוב לתמונה 5" not in first_group_caption

    second_group_caption = grid.rows[4].cells[0].text
    assert "תמונה 1: (המשך)" in second_group_caption
    assert "5. כיתוב לתמונה 5" in second_group_caption
    assert "6. כיתוב לתמונה 6" in second_group_caption

    # The second group's first row also gets its own forced page break, so
    # it can never end up sharing a page with the first group.
    second_group_first_image_paragraph = grid.rows[3].cells[0].paragraphs[0]
    assert second_group_first_image_paragraph.paragraph_format.page_break_before is True

    for row in grid.rows:
        trPr = row._tr.find(qn("w:trPr"))
        assert trPr is not None and trPr.find(qn("w:cantSplit")) is not None


def test_build_report_uses_order_index_not_list_position(isolated_paths, tmp_path):
    """The report photo order is a manual reviewer decision (order_index),
    never derived from insertion order, filename, or upload time - so a
    photos list that is NOT already sorted must still render in order_index
    order."""
    from PIL import Image

    def make(name: str, order_index: int, caption: str) -> photo_service.PhotoAsset:
        src = tmp_path / name
        Image.new("RGB", (800, 600), (10, 50, 200)).save(src, "JPEG")
        asset = photo_service.ingest_photo(src, order_index=order_index)
        asset.caption = caption
        return asset

    # Appended in one order, but assigned to different final positions -
    # exactly what happens after a reviewer drags/moves a photo.
    photo_a = make("a.jpg", order_index=2, caption="כיתוב א")
    photo_b = make("b.jpg", order_index=0, caption="כיתוב ב")
    photo_c = make("c.jpg", order_index=1, caption="כיתוב ג")
    photos = [photo_a, photo_b, photo_c]

    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=photos,
    )

    output_path, _warning = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    photo_tables = [
        t for t in document.tables if any("תמונה 1" in c.text for row in t.rows for c in row.cells)
    ]
    caption_text = photo_tables[0].rows[2].cells[0].text

    assert "1. כיתוב ב" in caption_text  # order_index=0
    assert "2. כיתוב ג" in caption_text  # order_index=1
    assert "3. כיתוב א" in caption_text  # order_index=2


def test_build_report_without_photos(isolated_paths):
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        body_text=_build_sample_text(),
        photos=[],
    )
    output_path, warning = report_builder.build_report(data)
    assert output_path.exists()
    assert warning is not None  # automation disabled in tests

    document = docx.Document(str(output_path))
    marker_hits = [p for p in document.paragraphs if "PHOTOS_TABLE_MARKER" in p.text]
    assert not marker_hits, "marker paragraph should be removed even with zero photos"
    assert PHOTO_APPENDIX_BOOKMARK not in _bookmark_names(document)


# --- photo-appendix vertical budget ----------------------------------------
# python-docx cannot paginate, so a group of photos plus its caption block is
# made to fit one page by construction. Portrait photos are ~1.8x taller than
# landscape ones at the same column width; before the height budget existed
# they overflowed the page and orphaned the caption block onto a page of its
# own. Assert the geometry invariant directly rather than needing Word.

_EMU_PER_INCH = 914400


def _make_photos_sized(tmp_path, size, count, prefix="p"):
    from PIL import Image

    photos = []
    for i in range(count):
        src = tmp_path / f"{prefix}_{i}.jpg"
        Image.new("RGB", size, (10 * i, 60, 180)).save(src, "JPEG")
        asset = photo_service.ingest_photo(src, order_index=i)
        asset.caption = f"כיתוב לתמונה {i + 1}"
        photos.append(asset)
    return photos


def _picture_sizes_inches(document):
    sizes = []
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for extent in paragraph._p.iter(qn("wp:extent")):
                        sizes.append(
                            (int(extent.get("cx")) / _EMU_PER_INCH, int(extent.get("cy")) / _EMU_PER_INCH)
                        )
    return sizes


def _build_with(photos):
    return report_builder.build_report(
        report_builder.ReportRenderData(
            inputs=_build_sample_inputs(), body_text=_build_sample_text(), photos=photos
        )
    )


def _budget(document):
    columns = config.PHOTO_GRID_COLUMNS
    gutter_in = (config.PHOTO_GUTTER_MM / 2 * 2) / 25.4
    return report_builder._photo_area_budget(
        document, config.PHOTOS_PER_APPENDIX_PAGE, columns, gutter_in
    ), gutter_in


def test_portrait_photos_are_capped_to_the_height_budget(isolated_paths, tmp_path):
    photos = _make_photos_sized(tmp_path, (1200, 1600), config.PHOTOS_PER_APPENDIX_PAGE)
    output_path, _ = _build_with(photos)
    document = docx.Document(str(output_path))

    max_height_in, _gutter = _budget(document)
    sizes = _picture_sizes_inches(document)
    assert len(sizes) == config.PHOTOS_PER_APPENDIX_PAGE

    for width_in, height_in in sizes:
        assert height_in <= max_height_in + 1e-6, (height_in, max_height_in)
        # aspect ratio preserved - never stretched or re-cropped
        assert abs((height_in / width_in) - (1600 / 1200)) < 0.01


def test_a_full_portrait_page_plus_its_caption_block_fits_one_page(isolated_paths, tmp_path):
    """The regression that orphaned the caption: a full group of portrait
    photos plus the caption block must not exceed the usable page height."""
    photos = _make_photos_sized(tmp_path, (1200, 1600), config.PHOTOS_PER_APPENDIX_PAGE)
    output_path, _ = _build_with(photos)
    document = docx.Document(str(output_path))

    max_h, gutter_in = _budget(document)
    columns = config.PHOTO_GRID_COLUMNS
    capacity = config.PHOTOS_PER_APPENDIX_PAGE
    rows = -(-capacity // columns)

    tallest = max(h for _w, h in _picture_sizes_inches(document))
    per_row = tallest + report_builder._PHOTO_NUMBER_LINE_IN + gutter_in
    caption_block = (
        1 + capacity * report_builder._CAPTION_LINES_PER_PHOTO
    ) * report_builder._CAPTION_LINE_HEIGHT_IN + gutter_in

    total = rows * per_row + caption_block
    assert total <= report_builder._usable_height_inches(document), total


def test_landscape_photos_stay_width_limited(isolated_paths, tmp_path):
    """The height budget must not shrink photos that already fit - landscape
    images keep filling the full column width as before."""
    photos = _make_photos_sized(tmp_path, (1600, 1200), 2)
    output_path, _ = _build_with(photos)
    document = docx.Document(str(output_path))

    columns = config.PHOTO_GRID_COLUMNS
    gutter_in = (config.PHOTO_GUTTER_MM / 2 * 2) / 25.4
    expected_width = max(
        0.5,
        (report_builder._usable_width_inches(document) / columns - gutter_in)
        * config.PHOTO_IMAGE_SCALE,
    )
    for width_in, _height_in in _picture_sizes_inches(document):
        assert abs(width_in - expected_width) < 0.01, (width_in, expected_width)


def test_mixed_orientations_each_respect_their_own_aspect(isolated_paths, tmp_path):
    portrait = _make_photos_sized(tmp_path, (1200, 1600), 2, prefix="tall")
    landscape = _make_photos_sized(tmp_path, (1600, 1200), 2, prefix="wide")
    photos = [portrait[0], landscape[0], portrait[1], landscape[1]]
    for i, a in enumerate(photos):
        a.order_index = i

    output_path, _ = _build_with(photos)
    document = docx.Document(str(output_path))

    max_height_in, _gutter = _budget(document)
    ratios = sorted(round(h / w, 2) for w, h in _picture_sizes_inches(document))
    assert ratios == [0.75, 0.75, 1.33, 1.33], ratios
    for _w, h in _picture_sizes_inches(document):
        assert h <= max_height_in + 1e-6
