"""Lab Report Generator - main Flet application (Hebrew, RTL).

Collects a short set of facts and photos from the user, asks the AI service
to write them up in the lab's Hebrew style, lets the user review/edit that
text and the photos, then builds the final .docx via report_builder.py.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
import traceback
from pathlib import Path

import flet as ft

import ai_service
import config
import photo_service
import report_builder

FIELD_WIDTH = 240
WIDE_FIELD_WIDTH = 540


def _today_str() -> str:
    return datetime.date.today().strftime("%d.%m.%y")


def _split_paragraphs(text: str) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def _join_paragraphs(paragraphs: list[str]) -> str:
    return "\n".join(paragraphs)


async def main(page: ft.Page):
    page.title = config.APP_TITLE_HE
    page.rtl = True
    page.padding = 24
    page.window.width = 1150
    page.window.height = 900
    page.theme_mode = ft.ThemeMode.LIGHT

    photos: list[photo_service.PhotoAsset] = []

    # ---------------------------------------------------------------- status
    status_text = ft.Text("", size=14, weight=ft.FontWeight.BOLD)

    def set_status(message: str, error: bool = False) -> None:
        status_text.value = message
        status_text.color = ft.Colors.RED_700 if error else ft.Colors.GREEN_800
        page.update()

    # ---------------------------------------------------------- report fields
    report_number_field = ft.TextField(label="מספר בדיקה", width=FIELD_WIDTH)
    report_date_field = ft.TextField(
        label="תאריך", width=FIELD_WIDTH, value=_today_str(), hint_text="לדוגמה: 30.07.26"
    )
    claim_number_field = ft.TextField(label="מספר תביעה (אופציונלי)", width=FIELD_WIDTH)

    client_contact_name_field = ft.TextField(label="איש קשר אצל הלקוח", width=FIELD_WIDTH)
    client_company_field = ft.TextField(label="שם החברה / הלקוח", width=FIELD_WIDTH)
    client_address_field = ft.TextField(label="כתובת", width=FIELD_WIDTH)
    client_city_field = ft.TextField(label="עיר", width=FIELD_WIDTH)

    subject_field = ft.TextField(label="נושא הדוח", width=WIDE_FIELD_WIDTH)

    item_field = ft.TextField(label="פריט (אופציונלי)", width=FIELD_WIDTH)
    assembly_field = ft.TextField(label="מכלול (אופציונלי)", width=FIELD_WIDTH)
    manufacturer_model_field = ft.TextField(label="יצרן/דגם (אופציונלי)", width=FIELD_WIDTH)
    material_field = ft.TextField(label="חומר (אופציונלי)", width=FIELD_WIDTH)
    inspection_nature_field = ft.TextField(label="מהות הבדיקה (אופציונלי)", width=FIELD_WIDTH)
    license_number_field = ft.TextField(label="מספר רישוי (אופציונלי)", width=FIELD_WIDTH)

    findings_notes_field = ft.TextField(
        label="ממצאים עיקריים - במילים שלך, בקצרה",
        multiline=True,
        min_lines=3,
        max_lines=8,
        width=WIDE_FIELD_WIDTH,
    )
    actions_taken_field = ft.TextField(
        label="פעולות שבוצעו", multiline=True, min_lines=2, max_lines=6, width=WIDE_FIELD_WIDTH
    )
    comments_field = ft.TextField(
        label="הערות נוספות (אופציונלי)", multiline=True, min_lines=2, max_lines=5, width=WIDE_FIELD_WIDTH
    )

    page_count_field = ft.TextField(label="מספר עמודים משוער", width=FIELD_WIDTH)
    photo_page_count_field = ft.TextField(label="מתוכם, עמודי תמונות", width=FIELD_WIDTH)

    # -------------------------------------------------------------- review UI
    general_review_field = ft.TextField(
        label="כללי - ניתן לערוך",
        multiline=True,
        min_lines=3,
        max_lines=10,
        width=WIDE_FIELD_WIDTH,
    )
    findings_review_field = ft.TextField(
        label="ממצאים - ניתן לערוך",
        multiline=True,
        min_lines=4,
        max_lines=14,
        width=WIDE_FIELD_WIDTH,
    )
    discussion_review_field = ft.TextField(
        label="דיון ומסקנות - ניתן לערוך",
        multiline=True,
        min_lines=3,
        max_lines=10,
        width=WIDE_FIELD_WIDTH,
    )

    # ------------------------------------------------------------- photos UI
    photos_list_view = ft.ReorderableListView(controls=[], width=WIDE_FIELD_WIDTH + 40)

    def _photo_row_index(photo_id: str) -> int:
        for idx, asset in enumerate(photos):
            if asset.photo_id == photo_id:
                return idx
        return -1

    def refresh_photo_page_count_hint() -> None:
        if not photo_page_count_field.value:
            estimate = max(1, math.ceil(len(photos) / (config.PHOTO_GRID_COLUMNS * 2))) if photos else 0
            photo_page_count_field.value = str(estimate)

    def remove_photo(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photos.pop(idx)
        for i, asset in enumerate(photos):
            asset.order_index = i
        rebuild_photo_rows()

    def approve_crop(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photo_service.apply_crop(photos[idx], photos[idx].suggested_crop_box)
        rebuild_photo_rows()

    def keep_original(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photo_service.discard_crop(photos[idx])
        rebuild_photo_rows()

    def make_caption_handler(photo_id: str):
        def _handler(e: ft.ControlEvent) -> None:
            idx = _photo_row_index(photo_id)
            if idx >= 0:
                photos[idx].caption = e.control.value

        return _handler

    def build_photo_row(asset: photo_service.PhotoAsset) -> ft.Control:
        thumbnail = ft.Image(
            src=asset.preview_path.read_bytes(),
            width=110,
            height=110,
            fit=ft.BoxFit.COVER,
            border_radius=6,
        )

        caption_field = ft.TextField(
            label="כיתוב לתמונה (אופציונלי)",
            value=asset.caption,
            width=360,
            on_change=make_caption_handler(asset.photo_id),
        )

        crop_controls: list[ft.Control] = []
        if asset.suggested_crop_box is not None and not asset.crop_reviewed:
            crop_controls.append(
                ft.Row(
                    controls=[
                        ft.Text("קיימת הצעת חיתוך לתמונה זו:", size=12),
                        ft.TextButton(
                            "אשר חיתוך מוצע",
                            on_click=lambda e, pid=asset.photo_id: approve_crop(pid),
                        ),
                        ft.TextButton(
                            "השאר תמונה מקורית",
                            on_click=lambda e, pid=asset.photo_id: keep_original(pid),
                        ),
                    ]
                )
            )
        elif asset.approved_crop:
            crop_controls.append(ft.Text("נבחר חיתוך מותאם ✓", size=12, color=ft.Colors.GREEN_700))

        info_column = ft.Column(
            controls=[caption_field, *crop_controls],
            spacing=4,
        )

        remove_button = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            tooltip="הסר תמונה",
            on_click=lambda e, pid=asset.photo_id: remove_photo(pid),
        )

        return ft.Container(
            key=asset.photo_id,
            content=ft.Row(
                controls=[remove_button, info_column, thumbnail],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=8,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            margin=ft.Margin.only(bottom=6),
        )

    def rebuild_photo_rows() -> None:
        photos_list_view.controls = [build_photo_row(asset) for asset in photos]
        refresh_photo_page_count_hint()
        page.update()

    def handle_reorder(e: ft.OnReorderEvent) -> None:
        moved = photos.pop(e.old_index)
        photos.insert(e.new_index, moved)
        for i, asset in enumerate(photos):
            asset.order_index = i
        rebuild_photo_rows()

    photos_list_view.on_reorder = handle_reorder

    file_picker = ft.FilePicker()

    async def add_photos_clicked(e: ft.ControlEvent) -> None:
        result = await file_picker.pick_files(
            dialog_title="בחר תמונות לדוח",
            file_type=ft.FilePickerFileType.IMAGE,
            allow_multiple=True,
        )
        if not result:
            return

        errors: list[str] = []

        def ingest_all():
            new_assets = []
            for picked in result:
                if not picked.path:
                    errors.append(f"לא ניתן לגשת לקובץ {picked.name}")
                    continue
                try:
                    asset = photo_service.ingest_photo(Path(picked.path), order_index=len(photos) + len(new_assets))
                    new_assets.append(asset)
                except photo_service.PhotoValidationError as exc:
                    errors.append(str(exc))
            return new_assets

        set_status("מעבד תמונות...")
        new_assets = await asyncio.to_thread(ingest_all)
        photos.extend(new_assets)
        rebuild_photo_rows()

        if errors:
            set_status(" | ".join(errors), error=True)
        else:
            set_status(f"נוספו {len(new_assets)} תמונות." if new_assets else "לא נוספו תמונות.")

    add_photos_button = ft.ElevatedButton(
        "הוסף תמונות", icon=ft.Icons.ADD_PHOTO_ALTERNATE, on_click=add_photos_clicked
    )

    # ---------------------------------------------------------- required-field check
    def collect_raw_inputs() -> ai_service.ReportInputs | None:
        missing = []
        if not report_number_field.value:
            missing.append("מספר בדיקה")
        if not report_date_field.value:
            missing.append("תאריך")
        if not client_company_field.value and not client_contact_name_field.value:
            missing.append("שם לקוח/חברה")
        if not subject_field.value:
            missing.append("נושא הדוח")
        if not findings_notes_field.value:
            missing.append("ממצאים עיקריים")

        if missing:
            set_status("שדות חסרים: " + ", ".join(missing), error=True)
            return None

        return ai_service.ReportInputs(
            report_number=report_number_field.value or "",
            report_date=report_date_field.value or "",
            client_contact_name=client_contact_name_field.value or "",
            client_company=client_company_field.value or "",
            client_address=client_address_field.value or "",
            client_city=client_city_field.value or "",
            subject=subject_field.value or "",
            claim_number=claim_number_field.value or "",
            item=item_field.value or "",
            assembly=assembly_field.value or "",
            manufacturer_model=manufacturer_model_field.value or "",
            material=material_field.value or "",
            inspection_nature=inspection_nature_field.value or "",
            license_number=license_number_field.value or "",
            findings_notes=findings_notes_field.value or "",
            actions_taken=actions_taken_field.value or "",
            comments=comments_field.value or "",
            photo_count=len(photos),
        )

    # ------------------------------------------------------------ AI draft
    async def generate_draft_clicked(e: ft.ControlEvent) -> None:
        inputs = collect_raw_inputs()
        if inputs is None:
            return

        set_status("שולח בקשה למנוע ה-AI...")
        generate_draft_button.disabled = True
        page.update()
        try:
            service = await asyncio.to_thread(ai_service.build_ai_service)
            result = await asyncio.to_thread(service.generate_report_text, inputs)
            general_review_field.value = _join_paragraphs(result.general_paragraphs)
            findings_review_field.value = _join_paragraphs(result.findings_paragraphs)
            discussion_review_field.value = _join_paragraphs(result.discussion_paragraphs)
            set_status("טיוטה נוצרה. ניתן לערוך את הטקסט למטה לפני יצירת הדוח הסופי.")
        except ai_service.AIServiceError as exc:
            set_status(f"שגיאת AI: {exc}", error=True)
        except Exception:
            set_status("שגיאה בלתי צפויה ביצירת הטיוטה. פרטים בקונסולה.", error=True)
            traceback.print_exc()
        finally:
            generate_draft_button.disabled = False
            page.update()

    generate_draft_button = ft.ElevatedButton(
        "צור טיוטת טקסט עם AI", icon=ft.Icons.AUTO_AWESOME, on_click=generate_draft_clicked
    )

    # ------------------------------------------------------------ final build
    async def generate_report_clicked(e: ft.ControlEvent) -> None:
        inputs = collect_raw_inputs()
        if inputs is None:
            return

        general_paragraphs = _split_paragraphs(general_review_field.value)
        findings_paragraphs = _split_paragraphs(findings_review_field.value)
        discussion_paragraphs = _split_paragraphs(discussion_review_field.value)

        if not (general_paragraphs and findings_paragraphs):
            set_status("יש למלא לפחות טקסט ל'כללי' ול'ממצאים' (ניתן ללחוץ קודם על יצירת טיוטה).", error=True)
            return

        generated_text = ai_service.GeneratedReportText(
            general_paragraphs=general_paragraphs,
            findings_paragraphs=findings_paragraphs,
            discussion_paragraphs=discussion_paragraphs,
        )

        render_data = report_builder.ReportRenderData(
            inputs=inputs,
            generated_text=generated_text,
            photos=list(photos),
            page_count=page_count_field.value or "",
            photo_page_count=photo_page_count_field.value or "",
        )

        set_status("יוצר קובץ Word...")
        generate_report_button.disabled = True
        page.update()
        try:
            output_path = await asyncio.to_thread(report_builder.build_report, render_data)
            set_status(f"הדוח נשמר בהצלחה: {output_path.name}")
            try:
                os.startfile(str(config.REPORTS_DIR))  # noqa: S606 - Windows-only app
            except OSError:
                pass
        except Exception as exc:
            set_status(f"שגיאה ביצירת הדוח: {exc}", error=True)
            traceback.print_exc()
        finally:
            generate_report_button.disabled = False
            page.update()

    generate_report_button = ft.ElevatedButton(
        "צור דוח Word",
        icon=ft.Icons.DESCRIPTION,
        on_click=generate_report_clicked,
        bgcolor=ft.Colors.BLUE_700,
        color=ft.Colors.WHITE,
    )

    # ------------------------------------------------------------- layout
    def section(title: str, controls: list[ft.Control]) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                controls=[ft.Text(title, size=18, weight=ft.FontWeight.BOLD), *controls],
                spacing=10,
            ),
            padding=16,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=10,
            margin=ft.Margin.only(bottom=16),
        )

    page.add(
        ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            controls=[
                ft.Text(config.APP_TITLE_HE, size=26, weight=ft.FontWeight.BOLD),
                ft.Text("מחולל דוחות מעבדה הנדסית - הזן פרטים קצרים וצרף תמונות", size=14),
                status_text,
                section(
                    "פרטי הדוח",
                    [
                        ft.Row([report_number_field, report_date_field, claim_number_field], wrap=True),
                        ft.Row(
                            [
                                client_contact_name_field,
                                client_company_field,
                                client_address_field,
                                client_city_field,
                            ],
                            wrap=True,
                        ),
                        subject_field,
                        ft.Row(
                            [
                                item_field,
                                assembly_field,
                                manufacturer_model_field,
                                material_field,
                                inspection_nature_field,
                                license_number_field,
                            ],
                            wrap=True,
                        ),
                        ft.Row([page_count_field, photo_page_count_field], wrap=True),
                    ],
                ),
                section(
                    "תיאור המקרה (במילים שלך - ה-AI לא ימציא עובדות)",
                    [findings_notes_field, actions_taken_field, comments_field],
                ),
                section(
                    "תמונות",
                    [add_photos_button, photos_list_view],
                ),
                section(
                    "טקסט הדוח - ניתן ליצור טיוטה ואז לערוך",
                    [generate_draft_button, general_review_field, findings_review_field, discussion_review_field],
                ),
                ft.Row([generate_report_button], alignment=ft.MainAxisAlignment.END),
            ],
        )
    )


if __name__ == "__main__":
    ft.run(main)
