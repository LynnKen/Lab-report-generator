"""Lab Report Generator - main Flet application (Hebrew, RTL).

Collects report metadata, manually-authored report text (כללי/ממצאים/דיון
ומסקנות), and photos from the user, then builds the final .docx via
report_builder.py. The only AI involvement anywhere in this app is
per-photo caption suggestions (ai_service.py) — report text itself is
always written directly by the user, never AI-generated.
"""

from __future__ import annotations

import asyncio
import datetime
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

# Crop dialog geometry (on-screen pixels).
CROP_CANVAS_MAX_PX = 420  # longest side of the photo inside the crop canvas
CROP_PREVIEW_PX = 220
HANDLE_PX = 14
MIN_CROP_PX = 24  # smallest frame the user can drag down to


def _today_str() -> str:
    return datetime.date.today().strftime("%d.%m.%y")


def _split_paragraphs(text: str) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


class StatusArea:
    """The single authoritative status/warning area.

    It is rendered next to the generate button at the END of the form, so
    validation errors, progress and completion messages all appear exactly
    where the user is looking when they submit - never at the top of a long
    scrolled page. Each state gets its own colour + icon so warnings are
    clearly distinct from hard errors and from success.
    """

    STYLES = {
        "info": (ft.Colors.BLUE_700, ft.Icons.INFO_OUTLINE),
        "progress": (ft.Colors.BLUE_700, ft.Icons.HOURGLASS_TOP),
        "success": (ft.Colors.GREEN_800, ft.Icons.CHECK_CIRCLE_OUTLINE),
        "warning": (ft.Colors.ORANGE_800, ft.Icons.WARNING_AMBER_ROUNDED),
        "error": (ft.Colors.RED_700, ft.Icons.ERROR_OUTLINE),
    }

    def __init__(self) -> None:
        self.icon = ft.Icon(None, size=18, visible=False)
        self.text = ft.Text("", size=14, weight=ft.FontWeight.BOLD, selectable=True)
        self.spinner = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        self.control = ft.Row(
            controls=[self.spinner, self.icon, self.text],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def set(self, message: str, kind: str = "info") -> None:
        """kind: info | progress | success | warning | error."""
        color, icon = self.STYLES.get(kind, self.STYLES["info"])
        self.text.value = message
        self.text.color = color
        self.icon.icon = icon
        self.icon.color = color
        # The spinner replaces the icon while work is in flight.
        self.icon.visible = bool(message) and kind != "progress"
        self.spinner.visible = kind == "progress"


async def main(page: ft.Page):
    page.title = config.APP_TITLE_HE
    page.rtl = True
    page.padding = 24
    page.window.width = 1150
    page.window.height = 900
    page.theme_mode = ft.ThemeMode.LIGHT

    photos: list[photo_service.PhotoAsset] = []

    # ---------------------------------------------------------------- status
    status = StatusArea()

    def set_status(message: str, kind: str = "info") -> None:
        status.set(message, kind)
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

    # ---------------------------------------------------- report text (manual)
    general_review_field = ft.TextField(
        label="כללי", multiline=True, min_lines=3, max_lines=10, width=WIDE_FIELD_WIDTH
    )
    findings_review_field = ft.TextField(
        label="ממצאים", multiline=True, min_lines=4, max_lines=14, width=WIDE_FIELD_WIDTH
    )
    discussion_review_field = ft.TextField(
        label="דיון ומסקנות", multiline=True, min_lines=3, max_lines=10, width=WIDE_FIELD_WIDTH
    )

    # ------------------------------------------------------------- photos UI
    photos_list_view = ft.ReorderableListView(controls=[], width=WIDE_FIELD_WIDTH + 40)

    def _photo_row_index(photo_id: str) -> int:
        for idx, asset in enumerate(photos):
            if asset.photo_id == photo_id:
                return idx
        return -1

    def remove_photo(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photos.pop(idx)
        for i, asset in enumerate(photos):
            asset.order_index = i
        rebuild_photo_rows()

    async def maybe_generate_caption(asset: photo_service.PhotoAsset) -> None:
        """Auto-generates/regenerates a caption from the current final image,
        unless the user has manually edited the caption for this photo."""
        if asset.caption_is_manual:
            return
        try:
            service = await asyncio.to_thread(ai_service.build_ai_service)
            caption = await asyncio.to_thread(service.generate_photo_caption, asset.final_path)
        except ai_service.AIServiceError:
            return  # best-effort only - never blocks the workflow
        except Exception:
            traceback.print_exc()
            return
        if not asset.caption_is_manual:  # user may have started typing meanwhile
            asset.caption = caption

    async def approve_crop(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        asset = photos[idx]
        photo_service.apply_crop(asset, asset.suggested_crop_box)
        rebuild_photo_rows()
        await maybe_generate_caption(asset)
        rebuild_photo_rows()

    async def keep_original(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        asset = photos[idx]
        photo_service.discard_crop(asset)
        rebuild_photo_rows()
        await maybe_generate_caption(asset)
        rebuild_photo_rows()

    def make_caption_handler(photo_id: str):
        def _handler(e: ft.ControlEvent) -> None:
            idx = _photo_row_index(photo_id)
            if idx >= 0:
                photos[idx].caption = e.control.value
                photos[idx].caption_is_manual = True

        return _handler

    async def open_crop_dialog(photo_id: str) -> None:
        """Word-like visual crop: the full photo with a draggable, resizable
        frame over it, everything outside the frame dimmed, and a live preview
        of exactly what will end up in the report.

        The frame is always drawn over the UNCROPPED original, and the crop is
        only ever applied by re-cutting from that original, so cropping stays
        non-destructive and "reset" genuinely restores the whole image."""
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        asset = photos[idx]

        disp_w, disp_h = photo_service.display_size_for(
            asset.width, asset.height, CROP_CANVAS_MAX_PX
        )
        base_bytes = await asyncio.to_thread(
            photo_service.render_uncropped_preview, asset, CROP_CANVAS_MAX_PX
        )

        # Crop rect in *display* pixels: [left, top, right, bottom]. Seeded
        # from any crop the user already applied, otherwise the whole frame.
        if asset.user_crop_box is not None:
            sx, sy = disp_w / asset.width, disp_h / asset.height
            left, top, right, bottom = asset.user_crop_box
            rect = [left * sx, top * sy, right * sx, bottom * sy]
        else:
            rect = [0.0, 0.0, float(disp_w), float(disp_h)]

        def clamped_rect() -> list[float]:
            left = max(0.0, min(rect[0], disp_w - MIN_CROP_PX))
            top = max(0.0, min(rect[1], disp_h - MIN_CROP_PX))
            right = max(left + MIN_CROP_PX, min(rect[2], float(disp_w)))
            bottom = max(top + MIN_CROP_PX, min(rect[3], float(disp_h)))
            return [left, top, right, bottom]

        def current_box() -> photo_service.BoundingBox:
            left, top, right, bottom = clamped_rect()
            return photo_service.display_box_to_original(
                (round(left), round(top), round(right), round(bottom)),
                disp_w,
                disp_h,
                asset.width,
                asset.height,
            )

        base_image = ft.Image(src=base_bytes, width=disp_w, height=disp_h, fit=ft.BoxFit.FILL)
        preview_image = ft.Image(
            src=base_bytes, width=CROP_PREVIEW_PX, height=CROP_PREVIEW_PX, fit=ft.BoxFit.CONTAIN
        )
        size_label = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def dim(**kwargs) -> ft.Container:
            return ft.Container(bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK), **kwargs)

        shade_top, shade_bottom = dim(), dim()
        shade_left, shade_right = dim(), dim()

        frame = ft.Container(
            border=ft.Border.all(2, ft.Colors.WHITE),
            bgcolor=ft.Colors.TRANSPARENT,
        )

        def make_handle(name: str, cursor) -> ft.GestureDetector:
            return ft.GestureDetector(
                mouse_cursor=cursor,
                on_pan_update=lambda e, n=name: resize_by(n, e),
                on_pan_end=lambda e: page.run_task(refresh_preview),
                content=ft.Container(
                    width=HANDLE_PX,
                    height=HANDLE_PX,
                    bgcolor=ft.Colors.WHITE,
                    border=ft.Border.all(1, ft.Colors.BLACK54),
                    border_radius=2,
                ),
            )

        handle_specs = [
            ("nw", ft.MouseCursor.RESIZE_UP_LEFT), ("n", ft.MouseCursor.RESIZE_UP),
            ("ne", ft.MouseCursor.RESIZE_UP_RIGHT), ("w", ft.MouseCursor.RESIZE_LEFT),
            ("e", ft.MouseCursor.RESIZE_RIGHT), ("sw", ft.MouseCursor.RESIZE_DOWN_LEFT),
            ("s", ft.MouseCursor.RESIZE_DOWN), ("se", ft.MouseCursor.RESIZE_DOWN_RIGHT),
        ]
        handles = {name: make_handle(name, cursor) for name, cursor in handle_specs}

        move_detector = ft.GestureDetector(
            mouse_cursor=ft.MouseCursor.MOVE,
            on_pan_update=lambda e: move_by(e),
            on_pan_end=lambda e: page.run_task(refresh_preview),
            content=ft.Container(bgcolor=ft.Colors.TRANSPARENT),
        )

        def layout() -> None:
            left, top, right, bottom = clamped_rect()
            rect[:] = [left, top, right, bottom]
            width, height = right - left, bottom - top

            shade_top.left, shade_top.top = 0, 0
            shade_top.width, shade_top.height = disp_w, top
            shade_bottom.left, shade_bottom.top = 0, bottom
            shade_bottom.width, shade_bottom.height = disp_w, disp_h - bottom
            shade_left.left, shade_left.top = 0, top
            shade_left.width, shade_left.height = left, height
            shade_right.left, shade_right.top = right, top
            shade_right.width, shade_right.height = disp_w - right, height

            for box in (frame, move_detector):
                box.left, box.top, box.width, box.height = left, top, width, height

            half = HANDLE_PX / 2
            positions = {
                "nw": (left - half, top - half), "n": (left + width / 2 - half, top - half),
                "ne": (right - half, top - half), "w": (left - half, top + height / 2 - half),
                "e": (right - half, top + height / 2 - half), "sw": (left - half, bottom - half),
                "s": (left + width / 2 - half, bottom - half), "se": (right - half, bottom - half),
            }
            for name, (hx, hy) in positions.items():
                handles[name].left, handles[name].top = hx, hy

            box = current_box()
            size_label.value = f"אזור נבחר: {box[2] - box[0]} × {box[3] - box[1]} פיקסלים"
            page.update()

        def move_by(e) -> None:
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            new_left = min(max(0.0, rect[0] + e.local_delta.x), disp_w - width)
            new_top = min(max(0.0, rect[1] + e.local_delta.y), disp_h - height)
            rect[:] = [new_left, new_top, new_left + width, new_top + height]
            layout()

        def resize_by(name: str, e) -> None:
            dx, dy = e.local_delta.x, e.local_delta.y
            if "w" in name:
                rect[0] = min(rect[0] + dx, rect[2] - MIN_CROP_PX)
            if "e" in name:
                rect[2] = max(rect[2] + dx, rect[0] + MIN_CROP_PX)
            if "n" in name:
                rect[1] = min(rect[1] + dy, rect[3] - MIN_CROP_PX)
            if "s" in name:
                rect[3] = max(rect[3] + dy, rect[1] + MIN_CROP_PX)
            layout()

        async def refresh_preview() -> None:
            preview_bytes = await asyncio.to_thread(
                photo_service.render_crop_preview, asset, current_box()
            )
            preview_image.src = preview_bytes
            page.update()

        def reset_clicked(e: ft.ControlEvent) -> None:
            # Frame-only reset: nothing on disk changes until Apply, and the
            # untouched original is what Apply re-cuts from.
            rect[:] = [0.0, 0.0, float(disp_w), float(disp_h)]
            layout()
            page.run_task(refresh_preview)

        async def apply_clicked(e: ft.ControlEvent) -> None:
            await asyncio.to_thread(photo_service.apply_crop, asset, current_box())
            page.pop_dialog()
            rebuild_photo_rows()
            await maybe_generate_caption(asset)
            rebuild_photo_rows()

        def cancel_clicked(e: ft.ControlEvent) -> None:
            page.pop_dialog()

        canvas = ft.Stack(
            controls=[
                base_image,
                shade_top, shade_bottom, shade_left, shade_right,
                frame, move_detector, *handles.values(),
            ],
            width=disp_w,
            height=disp_h,
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("חיתוך תמונה"),
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Container(content=canvas, border=ft.Border.all(1, ft.Colors.OUTLINE)),
                            ft.Text("גררו את המסגרת להזזה, ואת הריבועים הלבנים לשינוי הגודל.", size=12),
                            size_label,
                        ],
                        tight=True,
                        spacing=6,
                    ),
                    ft.Column(
                        controls=[ft.Text("תצוגה מקדימה", size=12, weight=ft.FontWeight.BOLD), preview_image],
                        tight=True,
                        spacing=6,
                    ),
                ],
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.START,
                tight=True,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.TextButton("אפס חיתוך", on_click=reset_clicked),
                ft.ElevatedButton("החל חיתוך", on_click=apply_clicked),
            ],
        )
        page.show_dialog(dialog)
        layout()
        await refresh_preview()

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
                            on_click=lambda e, pid=asset.photo_id: page.run_task(approve_crop, pid),
                        ),
                        ft.TextButton(
                            "השאר תמונה מקורית",
                            on_click=lambda e, pid=asset.photo_id: page.run_task(keep_original, pid),
                        ),
                    ]
                )
            )
        elif asset.approved_crop:
            crop_controls.append(ft.Text("נבחר חיתוך מותאם ✓", size=12, color=ft.Colors.GREEN_700))

        crop_button = ft.TextButton(
            "חיתוך",
            icon=ft.Icons.CROP,
            on_click=lambda e, pid=asset.photo_id: page.run_task(open_crop_dialog, pid),
        )

        info_column = ft.Column(
            controls=[caption_field, crop_button, *crop_controls],
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

        set_status("מעבד תמונות...", "progress")
        new_assets = await asyncio.to_thread(ingest_all)
        photos.extend(new_assets)
        rebuild_photo_rows()

        if new_assets:
            set_status("יוצר כיתובים אוטומטיים...", "progress")
            for asset in new_assets:
                await maybe_generate_caption(asset)
            rebuild_photo_rows()

        if errors:
            set_status(" | ".join(errors), "error")
        else:
            set_status(
                f"נוספו {len(new_assets)} תמונות." if new_assets else "לא נוספו תמונות.",
                "success" if new_assets else "info",
            )

    add_photos_button = ft.ElevatedButton(
        "הוסף תמונות", icon=ft.Icons.ADD_PHOTO_ALTERNATE, on_click=add_photos_clicked
    )

    # ---------------------------------------------------------- required-field check
    def collect_report_inputs() -> report_builder.ReportInputs | None:
        missing = []
        if not report_number_field.value:
            missing.append("מספר בדיקה")
        if not report_date_field.value:
            missing.append("תאריך")
        if not client_company_field.value and not client_contact_name_field.value:
            missing.append("שם לקוח/חברה")
        if not subject_field.value:
            missing.append("נושא הדוח")

        if missing:
            set_status("שדות חסרים: " + ", ".join(missing), "error")
            return None

        return report_builder.ReportInputs(
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
        )

    # ------------------------------------------------------------ final build
    async def generate_report_clicked(e: ft.ControlEvent) -> None:
        inputs = collect_report_inputs()
        if inputs is None:
            return

        general_paragraphs = _split_paragraphs(general_review_field.value)
        findings_paragraphs = _split_paragraphs(findings_review_field.value)
        discussion_paragraphs = _split_paragraphs(discussion_review_field.value)

        if not (general_paragraphs and findings_paragraphs and discussion_paragraphs):
            set_status("יש למלא טקסט בכל שלושת הפרקים: כללי, ממצאים ודיון ומסקנות.", "error")
            return

        body_text = report_builder.ReportBodyText(
            general_paragraphs=general_paragraphs,
            findings_paragraphs=findings_paragraphs,
            discussion_paragraphs=discussion_paragraphs,
        )

        render_data = report_builder.ReportRenderData(
            inputs=inputs,
            body_text=body_text,
            photos=list(photos),
        )

        set_status("יוצר קובץ Word...", "progress")
        generate_report_button.disabled = True
        page.update()
        try:
            output_path, warning = await asyncio.to_thread(report_builder.build_report, render_data)
            message = f"הדוח נשמר בהצלחה: {output_path.name}"
            if warning:
                # The .docx was still produced - a warning, not a failure.
                message += f" ({warning})"
            set_status(message, "warning" if warning else "success")
            try:
                os.startfile(str(config.REPORTS_DIR))  # noqa: S606 - Windows-only app
            except OSError:
                pass
        except Exception as exc:
            set_status(f"שגיאה ביצירת הדוח: {exc}", "error")
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
                    ],
                ),
                section(
                    "טקסט הדוח - כללי, ממצאים ודיון ומסקנות",
                    [general_review_field, findings_review_field, discussion_review_field],
                ),
                section(
                    "תמונות",
                    [add_photos_button, photos_list_view],
                ),
                # Status sits with the button, not at the top of the page, so
                # the user never has to scroll up to see why a build failed.
                ft.Row(
                    [status.control, generate_report_button],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
        )
    )


if __name__ == "__main__":
    ft.run(main)
