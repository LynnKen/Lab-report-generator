from __future__ import annotations

import docx

import photo_service
import report_builder
from ai_service import GeneratedReportText, ReportInputs


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
        findings_notes="נמצא סדק בבסיס החלק.",
        actions_taken="בוצעה בדיקה חזותית ומדידה.",
        comments="אין הערות נוספות.",
        photo_count=2,
    )


def _build_sample_text() -> GeneratedReportText:
    return GeneratedReportText(
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


def test_build_report_creates_expected_file(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        generated_text=_build_sample_text(),
        photos=photos,
        page_count="2",
        photo_page_count="1",
    )

    output_path = report_builder.build_report(data)

    assert output_path.exists()
    assert output_path.parent == isolated_paths["reports_dir"]
    assert output_path.name.startswith("C0000123_30.07.26_")
    assert output_path.suffix == ".docx"


def test_build_report_content_and_photo_grid(isolated_paths, tmp_path):
    photos = _make_photos(isolated_paths, tmp_path, count=3)
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        generated_text=_build_sample_text(),
        photos=photos,
        page_count="2",
        photo_page_count="1",
    )

    output_path = report_builder.build_report(data)
    document = docx.Document(str(output_path))

    all_text = "\n".join(p.text for p in document.paragraphs)
    assert "בדיקת נזק לדוגמה" in all_text
    assert "נמצא סדק בבסיס החלק" in all_text
    assert "הסדק נובע ככל הנראה מעייפות חומר" in all_text

    # The legal disclaimer table must survive untouched.
    disclaimer_texts = [t.rows[0].cells[0].text for t in document.tables if "התנאות" in t.rows[0].cells[0].text]
    assert disclaimer_texts, "legal disclaimer table missing or altered"

    # The photo grid table: 3 photos, 2 columns -> 2 rows, with captions.
    photo_tables = [
        t for t in document.tables if any("תמונה 1" in c.text for row in t.rows for c in row.cells)
    ]
    assert photo_tables, "photo grid table not found"
    grid = photo_tables[0]
    cell_texts = [c.text for row in grid.rows for c in row.cells]
    assert any("תמונה 1: כיתוב לתמונה 1" in t for t in cell_texts)
    assert any("תמונה 2: כיתוב לתמונה 2" in t for t in cell_texts)
    assert any("תמונה 3: כיתוב לתמונה 3" in t for t in cell_texts)


def test_build_report_without_photos(isolated_paths):
    data = report_builder.ReportRenderData(
        inputs=_build_sample_inputs(),
        generated_text=_build_sample_text(),
        photos=[],
    )
    output_path = report_builder.build_report(data)
    assert output_path.exists()

    document = docx.Document(str(output_path))
    marker_hits = [p for p in document.paragraphs if "PHOTOS_TABLE_MARKER" in p.text]
    assert not marker_hits, "marker paragraph should be removed even with zero photos"
