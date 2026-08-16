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
import approved_reference_service
import config
import photo_service
import reference_review_service
import report_builder

FIELD_WIDTH = 240
WIDE_FIELD_WIDTH = 540

# Crop dialog geometry (on-screen pixels). The crop frame over the photo is
# the only preview, so the canvas gets the full width of the dialog.
CROP_CANVAS_MAX_PX = 620  # longest side of the photo inside the crop canvas
HANDLE_PX = 14
MIN_CROP_PX = 24  # smallest frame the user can drag down to


def _today_str() -> str:
    return datetime.date.today().strftime("%d.%m.%y")


def _split_paragraphs(text: str) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def section(title: str, controls: list[ft.Control]) -> ft.Container:
    """Shared bordered/titled section container - used by both the Report
    Creation screen and the Reference Review tab."""
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


def build_reference_review_tab(page: ft.Page) -> ft.Column:
    """Builds the local-reviewer "בדיקת דוחות ייחוס" screen and returns its
    root control. Only ever called when config.REFERENCE_REVIEW_ENABLED is
    True (see main()) - never in a frozen/production build, so the dev-only
    data/reference_examples/ dataset never needs to exist there, and none
    of this labeling machinery runs or is shown to a production user.
    """
    # ============================================================
    # Reference Review screen — local reviewer workflow over the curated
    # dataset (data/reference_examples/, see README.he.md there). Maps each
    # individual document photo-number block to its real photo file(s), lets
    # a human correct the matching wording, and records an explicit review
    # decision PER BLOCK. This is not part of the report-generation flow and
    # touches no report/template code.
    # ============================================================

    STATUS_COLORS_HE = {
        "not_reviewed": ft.Colors.ON_SURFACE_VARIANT,
        "approved": ft.Colors.GREEN_800,
        "needs_edit": ft.Colors.ORANGE_800,
        "cannot_determine": ft.Colors.RED_700,
    }

    # active_block_id: which source-number block is currently in "select
    # images for me" mode - clicking a candidate thumbnail only ever affects
    # this one block, never the whole group, so a single caption can map
    # several distinct document photo-numbers to distinct photos.
    # pending_block_text: block_id -> live-edited (unsaved) approved_text_he,
    # since several block cards with their own text fields are on screen
    # together.
    review_state: dict = {"report": None, "selected_group_id": None, "active_block_id": None}
    pending_block_text: dict = {}
    pdf_preview_cache: dict = {}

    review_reviewer_name_field = ft.TextField(label="שם הבודק (אופציונלי)", width=220)
    review_report_dropdown = ft.Dropdown(label="דוח ייחוס לבדיקה", width=380, options=[])
    review_report_progress = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT)

    review_group_list = ft.ListView(spacing=4, height=420)

    review_group_id_text = ft.Text("", size=15, weight=ft.FontWeight.BOLD)
    review_group_status_text = ft.Text("", size=12)
    review_hints_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    review_original_text = ft.Text(
        "בחרו דוח וקבוצת כיתוב מהרשימה כדי להתחיל בדיקה.", size=13, selectable=True
    )
    review_original_box = ft.Container(
        content=review_original_text,
        padding=10,
        bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.BLACK),
        border_radius=6,
    )
    review_legacy_hint = ft.Text("", size=12, color=ft.Colors.ORANGE_800)
    review_blocks_column = ft.Column(spacing=10)
    review_candidates_hint = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    review_candidates_grid = ft.GridView(
        controls=[], max_extent=155, child_aspect_ratio=0.62, spacing=8, run_spacing=8, height=420
    )
    review_status = StatusArea()

    def _report_dropdown_options() -> list[ft.dropdown.Option]:
        return [
            ft.dropdown.Option(key=s["report_id"], text=f'{s["report_id"]} — {s["progress_text_he"]}')
            for s in reference_review_service.list_review_reports()
        ]

    def refresh_review_report_dropdown() -> None:
        review_report_dropdown.options = _report_dropdown_options()

    def update_review_progress_label() -> None:
        report = review_state["report"]
        review_report_progress.value = (
            reference_review_service.progress_text_he(report) if report else ""
        )

    def current_review_group():
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        if report is None or group_id is None:
            return None
        return report.find_group(group_id)

    def rebuild_review_group_list() -> None:
        report = review_state["report"]
        if report is None:
            review_group_list.controls = [
                ft.Text("בחרו דוח מהרשימה למעלה כדי להציג את קבוצות הכיתוב שלו.", size=12)
            ]
            return

        selected = review_state["selected_group_id"]
        rows: list[ft.Control] = []
        for group in report.caption_groups:
            is_selected = group.group_id == selected
            summary = group.status_summary()
            rows.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(group.group_id, size=13, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                reference_review_service.STATUS_LABELS_HE[summary],
                                size=11,
                                color=STATUS_COLORS_HE[summary],
                            ),
                        ],
                        spacing=2,
                    ),
                    padding=8,
                    border_radius=6,
                    ink=True,
                    bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.BLUE) if is_selected else None,
                    on_click=lambda e, gid=group.group_id: select_review_group(gid),
                )
            )
        review_group_list.controls = rows

    def open_image_preview(path: Path) -> None:
        if not path.exists():
            return
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(path.name),
            content=ft.Container(
                content=ft.Image(src=path.read_bytes(), fit=ft.BoxFit.CONTAIN),
                width=760,
                height=760,
            ),
            actions=[ft.TextButton("סגור", on_click=lambda e: page.pop_dialog())],
        )
        page.show_dialog(dialog)

    def open_pdf_page_preview(report_id: str, page_number: int) -> None:
        key = (report_id, page_number)
        if key not in pdf_preview_cache:
            try:
                pdf_preview_cache[key] = reference_review_service.render_layout_page_preview(
                    report_id, page_number, max_dimension=1100
                )
            except reference_review_service.ReferenceReviewError as exc:
                review_status.set(str(exc), "error")
                page.update()
                return
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"עמוד פריסה {page_number}"),
            content=ft.Container(
                content=ft.Image(src=pdf_preview_cache[key], fit=ft.BoxFit.CONTAIN),
                width=820,
                height=1000,
            ),
            actions=[ft.TextButton("סגור", on_click=lambda e: page.pop_dialog())],
        )
        page.show_dialog(dialog)

    def make_block_text_changed(block_id: str):
        def handler(e: ft.ControlEvent) -> None:
            pending_block_text[block_id] = e.control.value

        return handler

    def make_block_decision_handler(block_id: str, decision: str):
        def handler(e: ft.ControlEvent) -> None:
            report = review_state["report"]
            group_id = review_state["selected_group_id"]
            if report is None or group_id is None:
                return
            group = report.find_group(group_id)
            block = group.find_block(block_id)
            text = pending_block_text.get(block_id, block.approved_text_he)
            try:
                reference_review_service.apply_block_decision(
                    report,
                    group_id,
                    block_id,
                    approved_text_he=text,
                    decision=decision,
                    reviewer_name=review_reviewer_name_field.value or "",
                )
            except reference_review_service.ReviewValidationError as exc:
                review_status.set(str(exc), "error")
                page.update()
                return
            reference_review_service.save_report(report)
            pending_block_text.pop(block_id, None)
            update_review_progress_label()
            refresh_review_report_dropdown()
            select_review_group(group_id)  # re-renders everything from saved state
            review_status.set("הבלוק נשמר.", "success")
            page.update()

        return handler

    def enter_block_selection_mode(block_id: str) -> None:
        review_state["active_block_id"] = block_id
        rebuild_review_blocks()
        rebuild_review_candidates()
        page.update()

    def block_cannot_determine_clicked(block_id: str) -> None:
        """Opens a small dialog to pick a structured reason - a missing
        source image, an ambiguous mapping, an unreadable file, or other -
        instead of silently leaving the block unmapped. The block and its
        immutable original_text_he are always preserved; the reviewer can
        reopen it later simply by assigning an image."""
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        if report is None or group_id is None:
            return

        reason_dropdown = ft.Dropdown(
            label="סיבה",
            width=380,
            value="missing_source_image",
            options=[
                ft.dropdown.Option(key=r, text=reference_review_service.CANNOT_DETERMINE_REASON_LABELS_HE[r])
                for r in reference_review_service.CANNOT_DETERMINE_REASONS
            ],
        )

        def confirm_clicked(e: ft.ControlEvent) -> None:
            reference_review_service.mark_block_cannot_determine(
                report,
                group_id,
                block_id,
                reason=reason_dropdown.value or "other",
                reviewer_name=review_reviewer_name_field.value or "",
            )
            reference_review_service.save_report(report)
            page.pop_dialog()
            if review_state["active_block_id"] == block_id:
                review_state["active_block_id"] = None
            update_review_progress_label()
            refresh_review_report_dropdown()
            rebuild_review_group_list()
            rebuild_review_blocks()
            rebuild_review_candidates()
            review_status.set(f"סומן 'לא ניתן לקבוע' עבור {block_id}.", "info")
            page.update()

        def cancel_clicked(e: ft.ControlEvent) -> None:
            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("לא ניתן לקבוע - מה הסיבה?"),
            content=ft.Column(
                [
                    ft.Text(
                        "הבלוק והטקסט המקורי שלו יישמרו כפי שהם. ניתן לחזור לבלוק "
                        "בהמשך ולשייך תמונה שנמצאה, מה שיבטל סימון זה אוטומטית.",
                        size=12,
                    ),
                    reason_dropdown,
                ],
                tight=True,
                spacing=10,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.ElevatedButton("אשר", on_click=confirm_clicked),
            ],
        )
        page.show_dialog(dialog)

    def open_split_block_dialog(block_id: str) -> None:
        """Lets the reviewer partition a block's known document photo
        numbers into several new blocks - each new block starts with the
        SAME shared text as the source (never invented per number), so
        splitting only narrows which numbers a block covers."""
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        if report is None or group_id is None:
            return
        group = report.find_group(group_id)
        block = group.find_block(block_id)

        split_field = ft.TextField(
            label="שורה לכל בלוק חדש (למספרים שיישארו יחד, רשמו אותם באותה שורה מופרדים בפסיק)",
            value="\n".join(block.document_photo_refs),
            multiline=True,
            min_lines=3,
            max_lines=10,
            width=520,
        )
        error_text = ft.Text("", size=12, color=ft.Colors.RED_700)

        def parse_groups() -> list[list[str]]:
            groups = []
            for line in (split_field.value or "").split("\n"):
                numbers = [n.strip() for n in line.split(",") if n.strip()]
                if numbers:
                    groups.append(numbers)
            return groups

        def confirm_clicked(e: ft.ControlEvent) -> None:
            try:
                reference_review_service.split_block(report, group_id, block_id, parse_groups())
            except reference_review_service.ReferenceReviewError as exc:
                error_text.value = str(exc)
                page.update()
                return
            reference_review_service.save_report(report)
            page.pop_dialog()
            update_review_progress_label()
            refresh_review_report_dropdown()
            select_review_group(group_id)
            review_status.set("הבלוק פוצל בהצלחה.", "success")
            page.update()

        def cancel_clicked(e: ft.ControlEvent) -> None:
            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"פיצול בלוק {block.document_photo_label}"),
            content=ft.Column(
                [
                    ft.Text(
                        "כל שורה הופכת לבלוק נפרד. הטקסט המקורי מועתק כלשונו לכל בלוק "
                        "חדש - ניתן לערוך כל אחד בנפרד לאחר הפיצול.",
                        size=12,
                    ),
                    split_field,
                    error_text,
                ],
                tight=True,
                spacing=8,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.ElevatedButton("פצל", on_click=confirm_clicked),
            ],
        )
        page.show_dialog(dialog)

    def open_reference_crop_dialog(source_file: str, *, existing_instance=None) -> None:
        """Natural photo-framing crop tool for the reference-review image
        pool: one movable/resizable frame over the whole original photo,
        dimmed outside the frame, live preview, reset/cancel/save - never
        separate left/right/top/bottom cut fields. Saving stores only a
        normalized (0..1) rectangle; the original file is never touched.
        Creating a NEW crop always targets the active block; editing an
        EXISTING crop (existing_instance given) keeps it in its own block
        and position."""
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        active_id = review_state["active_block_id"]
        target_block_id = existing_instance[0] if existing_instance else active_id
        if report is None or group_id is None or target_block_id is None:
            review_status.set("יש לבחור בלוק פעיל לפני יצירת חיתוך.", "warning")
            page.update()
            return

        try:
            orig_w, orig_h = reference_review_service.image_pixel_size(report.report_id, source_file)
            base_bytes = reference_review_service.render_source_image_preview(
                report.report_id, source_file, CROP_CANVAS_MAX_PX
            )
        except reference_review_service.ReferenceReviewError as exc:
            review_status.set(str(exc), "error")
            page.update()
            return

        disp_w, disp_h = photo_service.display_size_for(orig_w, orig_h, CROP_CANVAS_MAX_PX)

        existing_crop = existing_instance[1].crop if existing_instance else None
        if existing_crop is not None:
            rect = [
                existing_crop.left * disp_w,
                existing_crop.top * disp_h,
                (existing_crop.left + existing_crop.width) * disp_w,
                (existing_crop.top + existing_crop.height) * disp_h,
            ]
        else:
            rect = [0.0, 0.0, float(disp_w), float(disp_h)]

        aspect_locked = {"on": False, "ratio": 1.0}

        def clamped_rect() -> list[float]:
            left = max(0.0, min(rect[0], disp_w - MIN_CROP_PX))
            top = max(0.0, min(rect[1], disp_h - MIN_CROP_PX))
            right = max(left + MIN_CROP_PX, min(rect[2], float(disp_w)))
            bottom = max(top + MIN_CROP_PX, min(rect[3], float(disp_h)))
            return [left, top, right, bottom]

        def normalized_crop() -> reference_review_service.CropBox:
            left, top, right, bottom = clamped_rect()
            return reference_review_service.CropBox(
                left=round(left / disp_w, 4),
                top=round(top / disp_h, 4),
                width=round((right - left) / disp_w, 4),
                height=round((bottom - top) / disp_h, 4),
            )

        base_image = ft.Image(src=base_bytes, width=disp_w, height=disp_h, fit=ft.BoxFit.FILL)
        size_label = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def dim(**kwargs) -> ft.Container:
            return ft.Container(bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK), **kwargs)

        shade_top, shade_bottom = dim(), dim()
        shade_left, shade_right = dim(), dim()
        frame = ft.Container(border=ft.Border.all(2, ft.Colors.WHITE), bgcolor=ft.Colors.TRANSPARENT)

        def make_handle(name: str, cursor) -> ft.GestureDetector:
            return ft.GestureDetector(
                mouse_cursor=cursor,
                on_pan_update=lambda e, n=name: resize_by(n, e),
                content=ft.Container(
                    width=HANDLE_PX, height=HANDLE_PX, bgcolor=ft.Colors.WHITE,
                    border=ft.Border.all(1, ft.Colors.BLACK54), border_radius=2,
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
                # With the aspect ratio locked, only corner handles give an
                # unambiguous resize - single-edge handles are hidden so the
                # reviewer can't accidentally break the lock.
                handles[name].visible = not aspect_locked["on"] or name in ("nw", "ne", "sw", "se")

            # Cheap arithmetic only while dragging - the crop frame over the
            # full photo already IS the preview, so no image is re-rendered
            # (cropped/resized/JPEG-encoded) until "שמור חיתוך" is clicked.
            crop = normalized_crop()
            px_w, px_h = round(crop.width * orig_w), round(crop.height * orig_h)
            size_label.value = f"אזור נבחר: {px_w} × {px_h} פיקסלים מתוך המקור"
            page.update()

        def move_by(e) -> None:
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            new_left = min(max(0.0, rect[0] + e.local_delta.x), disp_w - width)
            new_top = min(max(0.0, rect[1] + e.local_delta.y), disp_h - height)
            rect[:] = [new_left, new_top, new_left + width, new_top + height]
            layout()

        def resize_by(name: str, e) -> None:
            dx, dy = e.local_delta.x, e.local_delta.y
            if aspect_locked["on"] and name in ("nw", "ne", "sw", "se"):
                ratio = aspect_locked["ratio"] or 1.0
                cur_w = rect[2] - rect[0]
                sign = 1 if name in ("ne", "se") else -1
                new_w = max(MIN_CROP_PX, cur_w + sign * dx)
                new_h = new_w / ratio
                if name == "se":
                    rect[2], rect[3] = rect[0] + new_w, rect[1] + new_h
                elif name == "sw":
                    rect[0], rect[3] = rect[2] - new_w, rect[1] + new_h
                elif name == "ne":
                    rect[2], rect[1] = rect[0] + new_w, rect[3] - new_h
                else:  # nw
                    rect[0], rect[1] = rect[2] - new_w, rect[3] - new_h
                layout()
                return
            if "w" in name:
                rect[0] = min(rect[0] + dx, rect[2] - MIN_CROP_PX)
            if "e" in name:
                rect[2] = max(rect[2] + dx, rect[0] + MIN_CROP_PX)
            if "n" in name:
                rect[1] = min(rect[1] + dy, rect[3] - MIN_CROP_PX)
            if "s" in name:
                rect[3] = max(rect[3] + dy, rect[1] + MIN_CROP_PX)
            layout()

        def reset_clicked(e: ft.ControlEvent) -> None:
            rect[:] = [0.0, 0.0, float(disp_w), float(disp_h)]
            layout()

        def aspect_lock_changed(e: ft.ControlEvent) -> None:
            aspect_locked["on"] = e.control.value
            if aspect_locked["on"]:
                left, top, right, bottom = clamped_rect()
                width, height = right - left, bottom - top
                aspect_locked["ratio"] = width / height if height else 1.0
            layout()

        error_text = ft.Text("", size=12, color=ft.Colors.RED_700)

        def save_clicked(e: ft.ControlEvent) -> None:
            crop = normalized_crop()
            try:
                if existing_instance:
                    reference_review_service.update_crop_instance(
                        report, group_id, target_block_id, existing_instance[1].instance_id, crop,
                        reviewer_name=review_reviewer_name_field.value or "",
                    )
                else:
                    reference_review_service.add_crop_instance(
                        report, group_id, target_block_id, source_file, crop,
                        reviewer_name=review_reviewer_name_field.value or "",
                    )
            except reference_review_service.ReferenceReviewError as exc:
                error_text.value = str(exc)
                page.update()
                return
            reference_review_service.save_report(report)
            page.pop_dialog()
            update_review_progress_label()
            refresh_review_report_dropdown()
            rebuild_review_group_list()
            rebuild_review_blocks()
            rebuild_review_candidates()
            review_status.set("החיתוך נשמר.", "success")
            page.update()

        def cancel_clicked(e: ft.ControlEvent) -> None:
            page.pop_dialog()

        canvas = ft.Stack(
            controls=[base_image, shade_top, shade_bottom, shade_left, shade_right, frame, move_detector, *handles.values()],
            width=disp_w,
            height=disp_h,
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"חיתוך תמונה - {source_file}"),
            content=ft.Column(
                controls=[
                    ft.Container(content=canvas, border=ft.Border.all(1, ft.Colors.OUTLINE)),
                    ft.Text("גררו את המסגרת להזזה, ואת הריבועים הלבנים לשינוי הגודל.", size=12),
                    ft.Row(
                        [
                            ft.Checkbox(label="נעילת יחס רוחב-גובה", value=False, on_change=aspect_lock_changed),
                            size_label,
                        ]
                    ),
                    error_text,
                ],
                tight=True,
                spacing=6,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.TextButton("אפס לתמונה מלאה", on_click=reset_clicked),
                ft.ElevatedButton("שמור חיתוך", on_click=save_clicked),
            ],
        )
        page.show_dialog(dialog)
        layout()

    def remove_instance_clicked(block_id: str, instance_id: str) -> None:
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        if report is None or group_id is None:
            return
        reference_review_service.remove_image_instance(
            report, group_id, block_id, instance_id, reviewer_name=review_reviewer_name_field.value or ""
        )
        reference_review_service.save_report(report)
        update_review_progress_label()
        refresh_review_report_dropdown()
        rebuild_review_group_list()
        rebuild_review_blocks()
        rebuild_review_candidates()
        review_status.set("התמונה הוסרה מהבלוק (הקובץ המקורי לא נפגע).", "info")
        page.update()

    def move_instance_clicked(block_id: str, instance_id: str, delta: int) -> None:
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        if report is None or group_id is None:
            return
        group = report.find_group(group_id)
        block = group.find_block(block_id)
        ordered = sorted(block.image_instances, key=lambda i: i.display_order)
        idx = next(i for i, inst in enumerate(ordered) if inst.instance_id == instance_id)
        reference_review_service.reorder_image_instance(report, group_id, block_id, instance_id, idx + delta)
        reference_review_service.save_report(report)
        rebuild_review_blocks()
        page.update()

    def rebuild_review_blocks() -> None:
        report = review_state["report"]
        group = current_review_group()
        if report is None or group is None:
            review_blocks_column.controls = []
            return

        images_dir = reference_review_service.images_dir_for(report.report_id)
        active_id = review_state["active_block_id"]
        cards: list[ft.Control] = []
        for block in group.source_blocks:
            is_active = block.block_id == active_id
            status_color = STATUS_COLORS_HE[block.status]
            status_label = reference_review_service.STATUS_LABELS_HE[block.status]
            if block.status == "cannot_determine" and block.cannot_determine_reason:
                status_label += (
                    f" ({reference_review_service.CANNOT_DETERMINE_REASON_LABELS_HE[block.cannot_determine_reason]})"
                )
            confidence_label = reference_review_service.CONFIDENCE_LABELS_HE[block.extraction_confidence]

            text_field = ft.TextField(
                label="טקסט מאושר (ניתן לעריכה)",
                value=pending_block_text.get(block.block_id, block.approved_text_he),
                multiline=True,
                min_lines=3,
                max_lines=8,
                width=520,
                on_change=make_block_text_changed(block.block_id),
            )

            ordered_instances = sorted(block.image_instances, key=lambda i: i.display_order)
            if ordered_instances:
                instance_tiles: list[ft.Control] = []
                for idx, instance in enumerate(ordered_instances):
                    try:
                        if instance.variant_type == "crop" and instance.crop is not None:
                            thumb_bytes = reference_review_service.render_crop_preview_bytes(
                                report.report_id, instance.source_file, instance.crop, max_dimension=100
                            )
                        else:
                            thumb_bytes = reference_review_service.render_source_image_preview(
                                report.report_id, instance.source_file, max_dimension=100
                            )
                    except reference_review_service.ReferenceReviewError:
                        thumb_bytes = None

                    thumb_control = (
                        ft.Image(src=thumb_bytes, width=72, height=72, fit=ft.BoxFit.COVER, border_radius=6)
                        if thumb_bytes
                        else ft.Container(width=72, height=72, bgcolor=ft.Colors.OUTLINE_VARIANT)
                    )
                    is_crop = instance.variant_type == "crop"

                    tools: list[ft.Control] = []
                    if is_crop:
                        tools.append(
                            ft.IconButton(
                                icon=ft.Icons.CROP,
                                icon_size=14,
                                tooltip="ערוך חיתוך",
                                on_click=lambda e, bid=block.block_id, inst=instance: open_reference_crop_dialog(
                                    inst.source_file, existing_instance=(bid, inst)
                                ),
                            )
                        )
                    if idx > 0:
                        tools.append(
                            ft.IconButton(
                                icon=ft.Icons.ARROW_UPWARD,
                                icon_size=14,
                                tooltip="הזז מוקדם יותר בבלוק",
                                on_click=lambda e, bid=block.block_id, iid=instance.instance_id: move_instance_clicked(
                                    bid, iid, -1
                                ),
                            )
                        )
                    if idx < len(ordered_instances) - 1:
                        tools.append(
                            ft.IconButton(
                                icon=ft.Icons.ARROW_DOWNWARD,
                                icon_size=14,
                                tooltip="הזז מאוחר יותר בבלוק",
                                on_click=lambda e, bid=block.block_id, iid=instance.instance_id: move_instance_clicked(
                                    bid, iid, 1
                                ),
                            )
                        )
                    tools.append(
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            tooltip="הסר תמונה זו מהבלוק (הקובץ המקורי לא נפגע)",
                            on_click=lambda e, bid=block.block_id, iid=instance.instance_id: remove_instance_clicked(
                                bid, iid
                            ),
                        )
                    )

                    instance_tiles.append(
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    thumb_control,
                                    ft.Text(
                                        "חיתוך" if is_crop else "תמונה מלאה",
                                        size=9,
                                        weight=ft.FontWeight.BOLD,
                                        color=ft.Colors.BLUE_700 if is_crop else ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                    ft.Text(instance.source_file, size=9, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Row(tools, spacing=0),
                                ],
                                spacing=1,
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            width=92,
                            padding=4,
                            border=ft.Border.all(1, ft.Colors.BLUE_200 if is_crop else ft.Colors.OUTLINE_VARIANT),
                            border_radius=6,
                        )
                    )
                images_display = ft.Row(instance_tiles, wrap=True, spacing=6)
            else:
                images_display = ft.Text("טרם שויכו תמונות.", size=12)

            select_button = ft.TextButton(
                "מצב בחירה פעיל ✓" if is_active else f"בחר תמונות עבור {block.document_photo_label}",
                icon=ft.Icons.CHECK if is_active else ft.Icons.ADD_PHOTO_ALTERNATE,
                disabled=is_active,
                on_click=lambda e, bid=block.block_id: enter_block_selection_mode(bid),
            )

            action_buttons = [
                select_button,
                ft.ElevatedButton(
                    "אישור",
                    bgcolor=ft.Colors.GREEN_700,
                    color=ft.Colors.WHITE,
                    on_click=make_block_decision_handler(block.block_id, "approved"),
                ),
                ft.OutlinedButton(
                    "דרושה עריכה", on_click=make_block_decision_handler(block.block_id, "needs_edit")
                ),
                ft.OutlinedButton(
                    "לא ניתן לקבוע",
                    on_click=lambda e, bid=block.block_id: block_cannot_determine_clicked(bid),
                ),
                ft.TextButton("שמור", on_click=make_block_decision_handler(block.block_id, "save")),
            ]
            if len(block.document_photo_refs) > 1:
                action_buttons.append(
                    ft.TextButton(
                        "פצל בלוק זה",
                        icon=ft.Icons.CALL_SPLIT,
                        on_click=lambda e, bid=block.block_id: open_split_block_dialog(bid),
                    )
                )
            if block.layout_evidence is not None:
                action_buttons.append(
                    ft.TextButton(
                        f"הצג עמוד פריסה ({block.layout_evidence.page_number})",
                        icon=ft.Icons.PICTURE_AS_PDF,
                        on_click=lambda e, rid=report.report_id, pg=block.layout_evidence.page_number: open_pdf_page_preview(
                            rid, pg
                        ),
                    )
                )

            card_controls: list[ft.Control] = [
                ft.Row(
                    [
                        ft.Text(
                            f"מספרי תמונות בדוח: {block.document_photo_label}",
                            size=14,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(status_label, size=12, weight=ft.FontWeight.BOLD, color=status_color),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Text(f"רמת ודאות המיפוי: {confidence_label}", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ]
            if block.ambiguity_notes:
                card_controls.append(ft.Text(block.ambiguity_notes, size=11, color=ft.Colors.ORANGE_800))
            card_controls.extend(
                [
                    ft.Text("הטקסט המתאים בדוח (קבוע, לא ניתן לעריכה):", size=11, weight=ft.FontWeight.BOLD),
                    ft.Container(
                        content=ft.Text(block.original_text_he, size=12, selectable=True),
                        padding=8,
                        bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.BLACK),
                        border_radius=6,
                    ),
                    text_field,
                    ft.Text("תמונות משויכות לבלוק זה:", size=11, weight=ft.FontWeight.BOLD),
                    images_display,
                    ft.Row(action_buttons, wrap=True, spacing=6),
                ]
            )

            cards.append(
                ft.Container(
                    content=ft.Column(card_controls, spacing=6),
                    padding=10,
                    border=ft.Border.all(
                        2 if is_active else 1,
                        ft.Colors.BLUE if is_active else ft.Colors.OUTLINE_VARIANT,
                    ),
                    border_radius=8,
                )
            )
        review_blocks_column.controls = cards

    def open_view_uses_dialog(source_file: str) -> None:
        """Read-only - always available, regardless of any active block."""
        report = review_state["report"]
        if report is None:
            return
        uses = reference_review_service.find_source_file_usages(report, source_file)
        if uses:
            rows = [
                ft.Text(
                    f"• קבוצה {u.group_id} / בלוק {u.document_photo_label} - "
                    + ("חיתוך" if u.variant_type == "crop" else "תמונה מלאה"),
                    size=12,
                )
                for u in uses
            ]
        else:
            rows = [ft.Text("תמונה זו אינה משויכת עדיין לאף בלוק.", size=12)]
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"שימושים קיימים - {source_file}"),
            content=ft.Column(rows, tight=True, spacing=4, width=420),
            actions=[ft.TextButton("סגור", on_click=lambda e: page.pop_dialog())],
        )
        page.show_dialog(dialog)

    def _after_pool_instance_change(message: str) -> None:
        report = review_state["report"]
        reference_review_service.save_report(report)
        update_review_progress_label()
        refresh_review_report_dropdown()
        rebuild_review_group_list()
        rebuild_review_blocks()
        rebuild_review_candidates()
        review_status.set(message, "success")
        page.update()

    def open_confirm_full_image_dialog(source_file: str, existing_uses) -> None:
        """The image already has at least one existing use elsewhere - never
        assign a second full-image instance silently."""
        rows = [
            ft.Text(
                f"• קבוצה {u.group_id} / בלוק {u.document_photo_label} - "
                + ("חיתוך" if u.variant_type == "crop" else "תמונה מלאה"),
                size=12,
            )
            for u in existing_uses
        ]

        def confirm_clicked(e: ft.ControlEvent) -> None:
            report = review_state["report"]
            group_id = review_state["selected_group_id"]
            active_id = review_state["active_block_id"]
            reference_review_service.assign_full_image(
                report,
                group_id,
                active_id,
                source_file,
                reviewer_name=review_reviewer_name_field.value or "",
                confirm_existing_use=True,
            )
            page.pop_dialog()
            _after_pool_instance_change(f"התמונה המלאה '{source_file}' שויכה גם לבלוק זה.")

        def cancel_clicked(e: ft.ControlEvent) -> None:
            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("התמונה כבר בשימוש במקום אחר"),
            content=ft.Column(
                [
                    ft.Text(f"התמונה '{source_file}' כבר משויכת ל:", size=13),
                    *rows,
                    ft.Text("לשייך אותה גם לבלוק הפעיל, כתמונה מלאה נוספת?", size=13),
                ],
                tight=True,
                spacing=8,
                width=420,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.ElevatedButton("אשר שיוך נוסף", on_click=confirm_clicked),
            ],
        )
        page.show_dialog(dialog)

    def assign_full_image_clicked(source_file: str) -> None:
        report = review_state["report"]
        group_id = review_state["selected_group_id"]
        active_id = review_state["active_block_id"]
        if report is None or group_id is None or active_id is None:
            review_status.set("יש ללחוץ תחילה על 'בחר תמונות עבור...' באחד הבלוקים.", "warning")
            page.update()
            return
        try:
            reference_review_service.assign_full_image(
                report, group_id, active_id, source_file, reviewer_name=review_reviewer_name_field.value or ""
            )
        except reference_review_service.FullImageAlreadyAssignedError as exc:
            open_confirm_full_image_dialog(source_file, exc.existing_uses)
            return
        _after_pool_instance_change(f"התמונה המלאה '{source_file}' שויכה לבלוק.")

    def create_crop_clicked(source_file: str) -> None:
        active_id = review_state["active_block_id"]
        if active_id is None:
            review_status.set("יש ללחוץ תחילה על 'בחר תמונות עבור...' באחד הבלוקים.", "warning")
            page.update()
            return
        open_reference_crop_dialog(source_file)

    def rebuild_review_candidates() -> None:
        report = review_state["report"]
        if report is None:
            review_candidates_grid.controls = []
            review_candidates_hint.value = ""
            return

        images = reference_review_service.list_candidate_images(report.report_id)
        if not images:
            review_candidates_grid.controls = []
            review_candidates_hint.value = (
                "טרם נוספו תמונות מקוריות לדוח זה. יש להעתיק ידנית קבצי תמונה באיכות "
                "מלאה לתיקיית ה-images של הדוח (לחצו 'פתח תיקיית תמונות'), ולאחר מכן "
                "ללחוץ 'רענן רשימת תמונות'."
            )
            return

        active_id = review_state["active_block_id"]
        review_candidates_hint.value = (
            "כל תמונת מקור מוצגת פעם אחת - ניתן להשתמש באותה תמונה במספר בלוקים "
            "(תמונה מלאה במקום אחד, חיתוך במקום אחר). 'שיוך מלא' ו'חיתוך' פועלים "
            "על הבלוק הפעיל; 'שימושים קיימים' זמין תמיד."
            + ("" if active_id else " יש ללחוץ תחילה על 'בחר תמונות עבור...' באחד הבלוקים למעלה.")
        )

        tiles: list[ft.Control] = []
        for path in images:
            usages = reference_review_service.find_source_file_usages(report, path.name)
            used_by_active = active_id is not None and any(u.block_id == active_id for u in usages)
            if used_by_active:
                border_color, border_width = ft.Colors.GREEN_700, 3
            elif usages:
                border_color, border_width = ft.Colors.BLUE_400, 2
            else:
                border_color, border_width = ft.Colors.OUTLINE_VARIANT, 1
            usage_text = f"בשימוש ב-{len(usages)} בלוקים" if usages else "לא בשימוש עדיין"

            tiles.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Container(
                                content=ft.Image(
                                    src=path.read_bytes(),
                                    width=110,
                                    height=85,
                                    fit=ft.BoxFit.COVER,
                                    border_radius=6,
                                ),
                                on_click=lambda e, p=path: open_image_preview(p),
                            ),
                            ft.Text(path.name, size=9, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(usage_text, size=9, weight=ft.FontWeight.BOLD, color=border_color),
                            ft.Row(
                                [
                                    ft.TextButton(
                                        "שיוך מלא",
                                        icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                                        tooltip=None if active_id else "יש לבחור בלוק פעיל תחילה (בחר תמונות עבור...)",
                                        on_click=lambda e, name=path.name: assign_full_image_clicked(name),
                                    ),
                                    ft.TextButton(
                                        "צור חיתוך",
                                        icon=ft.Icons.CROP,
                                        tooltip=None if active_id else "יש לבחור בלוק פעיל תחילה (בחר תמונות עבור...)",
                                        on_click=lambda e, name=path.name: create_crop_clicked(name),
                                    ),
                                ],
                                spacing=0,
                                wrap=True,
                            ),
                            ft.TextButton(
                                "הצג שימושים קיימים",
                                icon=ft.Icons.INFO_OUTLINE,
                                on_click=lambda e, name=path.name: open_view_uses_dialog(name),
                            ),
                        ],
                        spacing=1,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=4,
                    border=ft.Border.all(border_width, border_color),
                    border_radius=8,
                    width=150,
                )
            )
        review_candidates_grid.controls = tiles

    def select_review_group(group_id: str) -> None:
        report = review_state["report"]
        if report is None:
            return
        group = report.find_group(group_id)
        review_state["selected_group_id"] = group_id
        review_state["active_block_id"] = None
        pending_block_text.clear()

        summary = group.status_summary()
        review_group_id_text.value = group.group_id
        review_group_status_text.value = f"סטטוס מסכם: {reference_review_service.STATUS_LABELS_HE[summary]}"
        review_group_status_text.color = STATUS_COLORS_HE[summary]
        review_hints_text.value = group.notes or "אין הערות מיפוי מהחילוץ המקורי."
        review_original_text.value = group.original_text_he
        if group.legacy_unmapped_images:
            review_legacy_hint.value = (
                "תמונות שנבחרו במסך הישן (לפני הפיצול לפי בלוקים, טרם שויכו לבלוק ספציפי): "
                + ", ".join(group.legacy_unmapped_images)
                + " — יש לשייך אותן מחדש לבלוק המתאים למטה."
            )
        else:
            review_legacy_hint.value = ""
        review_status.set("", "info")

        rebuild_review_group_list()
        rebuild_review_blocks()
        rebuild_review_candidates()
        page.update()

    def load_review_report(report_id: str) -> None:
        try:
            review_state["report"] = reference_review_service.load_report(report_id)
        except reference_review_service.ReferenceReviewError as exc:
            review_status.set(str(exc), "error")
            page.update()
            return
        report = review_state["report"]
        first_group_id = report.caption_groups[0].group_id if report.caption_groups else None
        review_state["selected_group_id"] = None
        update_review_progress_label()
        if first_group_id:
            select_review_group(first_group_id)
        else:
            rebuild_review_group_list()
            rebuild_review_blocks()
            rebuild_review_candidates()
            page.update()

    def review_report_dropdown_changed(e: ft.ControlEvent) -> None:
        if review_report_dropdown.value:
            load_review_report(review_report_dropdown.value)

    review_report_dropdown.on_select = review_report_dropdown_changed
    review_report_dropdown.options = _report_dropdown_options()

    def open_review_images_folder(e: ft.ControlEvent) -> None:
        report = review_state["report"]
        if report is None:
            return
        folder = reference_review_service.images_dir_for(report.report_id)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # noqa: S606 - Windows-only app
        except OSError:
            pass

    def refresh_review_images_clicked(e: ft.ControlEvent) -> None:
        rebuild_review_candidates()
        review_status.set("רשימת התמונות רועננה.", "info")
        page.update()

    rebuild_review_group_list()
    rebuild_review_blocks()
    rebuild_review_candidates()

    review_view = ft.Column(
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        controls=[
            ft.Text("בדיקת דוחות ייחוס", size=22, weight=ft.FontWeight.BOLD),
            ft.Text(
                "מיפוי מספרי תמונה בודדים מדוחות קיימים לתמונות המקוריות שלהם, "
                "ואישור או תיקון הניסוח על ידי בודק מוסמך - לכל מספר תמונה בנפרד. "
                "זהו כלי עבודה מקומי לבודק אנושי בלבד, ואינו חלק מזרימת יצירת הדוח.",
                size=13,
            ),
            section(
                "בחירת דוח",
                [
                    ft.Row([review_report_dropdown, review_reviewer_name_field], wrap=True),
                    review_report_progress,
                ],
            ),
            ft.Row(
                controls=[
                    ft.Container(
                        width=260,
                        content=ft.Column(
                            controls=[
                                ft.Text("קבוצות כיתוב (תמונה 1 / תמונה 1 המשך...)", size=14, weight=ft.FontWeight.BOLD),
                                review_group_list,
                            ],
                            spacing=8,
                        ),
                    ),
                    ft.VerticalDivider(),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            controls=[
                                ft.Row([review_group_id_text, review_group_status_text], spacing=16),
                                ft.Text("טקסט מקורי של הקבוצה כולה (קבוע, לא ניתן לעריכה):", size=12, weight=ft.FontWeight.BOLD),
                                review_original_box,
                                review_hints_text,
                                ft.Divider(),
                                ft.Text("בלוקים לפי מספר תמונה", size=16, weight=ft.FontWeight.BOLD),
                                review_legacy_hint,
                                review_blocks_column,
                                ft.Divider(),
                                ft.Text("תמונות מקור (כל תמונה מוצגת פעם אחת, ויכולה לשמש כמה בלוקים):", size=12, weight=ft.FontWeight.BOLD),
                                ft.Row(
                                    [
                                        ft.TextButton(
                                            "פתח תיקיית תמונות",
                                            icon=ft.Icons.FOLDER_OPEN,
                                            on_click=open_review_images_folder,
                                        ),
                                        ft.TextButton(
                                            "רענן רשימת תמונות",
                                            icon=ft.Icons.REFRESH,
                                            on_click=refresh_review_images_clicked,
                                        ),
                                    ]
                                ),
                                review_candidates_hint,
                                review_candidates_grid,
                                review_status.control,
                            ],
                            spacing=10,
                        ),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        ],
    )

    # Load the first report immediately so the review tab isn't blank until
    # the user manually touches the dropdown.
    if review_report_dropdown.options:
        review_report_dropdown.value = review_report_dropdown.options[0].key
        load_review_report(review_report_dropdown.value)

    return review_view


def build_app_root(page: ft.Page, generator_view: ft.Control, *, review_enabled: bool) -> ft.Control:
    """Returns the root control tree for the app window.

    review_enabled is False by default in a frozen/production build (see
    config.REFERENCE_REVIEW_ENABLED) - in that case the Reference Review
    ("בדיקת דוחות ייחוס") tab is never built and never shown: just the
    Report Creation screen, alone, with no tab bar at all. It is False
    specifically because a production build never bundles the raw
    data/reference_examples/ dataset that tab reads (ReportGenerator.spec) -
    merely hiding a built tab would still leave that code path reachable."""
    if not review_enabled:
        return generator_view
    review_view = build_reference_review_tab(page)
    return ft.Tabs(
        length=2,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="יצירת דוח"),
                        ft.Tab(label="בדיקת דוחות ייחוס"),
                    ]
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[generator_view, review_view],
                ),
            ],
        ),
    )


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
        photo_service.renumber(photos)
        rebuild_photo_rows()

    def move_photo_earlier(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photo_service.move_photo_earlier(photos, idx)
        rebuild_photo_rows()

    def move_photo_later(photo_id: str) -> None:
        idx = _photo_row_index(photo_id)
        if idx < 0:
            return
        photo_service.move_photo_later(photos, idx)
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

        def reset_clicked(e: ft.ControlEvent) -> None:
            # Frame-only reset: nothing on disk changes until Apply, and the
            # untouched original is what Apply re-cuts from.
            rect[:] = [0.0, 0.0, float(disp_w), float(disp_h)]
            layout()

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
            # Single centred canvas: the frame over the photo IS the preview,
            # so there is no second copy of the image to keep in sync.
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=canvas,
                        border=ft.Border.all(1, ft.Colors.OUTLINE),
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text("גררו את המסגרת להזזה, ואת הריבועים הלבנים לשינוי הגודל.", size=12),
                    size_label,
                ],
                tight=True,
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            actions=[
                ft.TextButton("ביטול", on_click=cancel_clicked),
                ft.TextButton("אפס חיתוך", on_click=reset_clicked),
                ft.ElevatedButton("החל חיתוך", on_click=apply_clicked),
            ],
        )
        page.show_dialog(dialog)
        layout()

    def build_photo_row(asset: photo_service.PhotoAsset, index: int, total: int) -> ft.Control:
        thumbnail = ft.Image(
            src=asset.preview_path.read_bytes(),
            width=110,
            height=110,
            fit=ft.BoxFit.COVER,
            border_radius=6,
        )

        # The final report sequence number - the only thing that changes when
        # a photo moves. Shown big and separate from the (editable) caption
        # and the (fixed) original filename so the three are never confused.
        sequence_badge = ft.Container(
            content=ft.Text(str(index + 1), size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
            width=32,
            height=32,
            bgcolor=ft.Colors.BLUE_700,
            border_radius=16,
            alignment=ft.Alignment.CENTER,
            tooltip="מיקום בסדר התמונות בדוח",
        )

        filename_text = ft.Text(
            asset.source_filename or "(שם קובץ לא ידוע)",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
            italic=True,
            selectable=True,
        )

        move_earlier_button = ft.IconButton(
            icon=ft.Icons.ARROW_UPWARD,
            tooltip="הזז מוקדם יותר בדוח",
            disabled=index == 0,
            on_click=lambda e, pid=asset.photo_id: move_photo_earlier(pid),
        )
        move_later_button = ft.IconButton(
            icon=ft.Icons.ARROW_DOWNWARD,
            tooltip="הזז מאוחר יותר בדוח",
            disabled=index == total - 1,
            on_click=lambda e, pid=asset.photo_id: move_photo_later(pid),
        )
        move_controls = ft.Column(
            controls=[move_earlier_button, move_later_button],
            spacing=0,
            alignment=ft.MainAxisAlignment.CENTER,
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
            controls=[filename_text, caption_field, crop_button, *crop_controls],
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
                controls=[remove_button, move_controls, sequence_badge, info_column, thumbnail],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=8,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            margin=ft.Margin.only(bottom=6),
        )

    def rebuild_photo_rows() -> None:
        total = len(photos)
        photos_list_view.controls = [
            build_photo_row(asset, i, total) for i, asset in enumerate(photos)
        ]
        rebuild_order_preview()
        page.update()

    def handle_reorder(e: ft.OnReorderEvent) -> None:
        photo_service.move_photo(photos, e.old_index, e.new_index)
        rebuild_photo_rows()

    photos_list_view.on_reorder = handle_reorder

    # ---------------------------------------------------- photo order preview
    # A compact, read-focused view of exactly the sequence the report builder
    # will use (report_builder sorts by order_index, never by filename or
    # pick order) - lets the reviewer double-check/adjust it in one place
    # before generating, separate from the editing-focused list above.
    order_preview_grid = ft.GridView(max_extent=160, child_aspect_ratio=0.6, spacing=8, run_spacing=8, height=420)

    def build_order_preview_tile(asset: photo_service.PhotoAsset, index: int, total: int) -> ft.Control:
        thumb = ft.Image(
            src=asset.preview_path.read_bytes(), width=140, height=100, fit=ft.BoxFit.COVER, border_radius=6
        )
        badge = ft.Container(
            content=ft.Text(str(index + 1), size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
            width=26,
            height=26,
            bgcolor=ft.Colors.BLUE_700,
            border_radius=13,
            alignment=ft.Alignment.CENTER,
            top=4,
            right=4,
        )
        stack = ft.Stack(controls=[thumb, badge], width=140, height=100)
        filename_line = ft.Text(
            asset.source_filename or "(שם קובץ לא ידוע)",
            size=10,
            italic=True,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        caption_line = ft.Text(
            asset.caption or "(ללא כיתוב)",
            size=10,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        move_row = ft.Row(
            controls=[
                ft.IconButton(
                    icon=ft.Icons.ARROW_UPWARD,
                    icon_size=16,
                    tooltip="הזז מוקדם יותר",
                    disabled=index == 0,
                    on_click=lambda e, pid=asset.photo_id: move_photo_earlier(pid),
                ),
                ft.IconButton(
                    icon=ft.Icons.ARROW_DOWNWARD,
                    icon_size=16,
                    tooltip="הזז מאוחר יותר",
                    disabled=index == total - 1,
                    on_click=lambda e, pid=asset.photo_id: move_photo_later(pid),
                ),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=0,
        )
        return ft.Container(
            content=ft.Column(
                controls=[stack, filename_line, caption_line, move_row],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=6,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
        )

    def rebuild_order_preview() -> None:
        total = len(photos)
        order_preview_grid.controls = [
            build_order_preview_tile(asset, i, total) for i, asset in enumerate(photos)
        ]

    order_preview_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("תצוגה מקדימה של סדר התמונות בדוח"),
        content=ft.Column(
            controls=[
                ft.Text(
                    "כך התמונות יופיעו בדוח הסופי, לפי המספר שמעל כל תמונה. "
                    "ניתן לשנות את הסדר גם כאן, באמצעות החצים.",
                    size=12,
                ),
                order_preview_grid,
            ],
            tight=True,
            spacing=8,
            width=640,
        ),
        actions=[ft.TextButton("סגור", on_click=lambda e: page.pop_dialog())],
    )

    def open_order_preview_clicked(e: ft.ControlEvent) -> None:
        if not photos:
            set_status("לא נבחרו תמונות עדיין.", "warning")
            return
        rebuild_order_preview()
        page.show_dialog(order_preview_dialog)

    order_preview_button = ft.OutlinedButton(
        "תצוגה מקדימה של סדר התמונות",
        icon=ft.Icons.VIEW_LIST,
        on_click=open_order_preview_clicked,
    )

    # ---------------------------------------------- approved-reference status
    # Small, non-intrusive transparency note - never shows paths, filenames,
    # raw captions, or review data. A production (frozen) build shows a
    # fixed, simpler sentence with no count (matches what a production
    # end-user should see); a dev run shows the live count, refreshed
    # whenever captions are (re)generated, since the approved dataset can
    # grow between app launches, or even mid-session if more blocks get
    # approved in the "בדיקת דוחות ייחוס" tab (dev-only).
    reference_status_text = ft.Text("", size=12, italic=True, color=ft.Colors.ON_SURFACE_VARIANT)
    _PRODUCTION_REFERENCE_NOTE_HE = "הצעת הכיתוב מסתייעת בדוגמאות מאושרות לצורך מינוח וסגנון בלבד."

    def refresh_reference_status() -> None:
        if not config.APPROVED_REFERENCES_ENABLED or config.APPROVED_REFERENCE_MODE == "off":
            reference_status_text.value = ""
            return
        try:
            selection = approved_reference_service.select_reference_examples()
        except Exception:
            reference_status_text.value = ""
            return
        if selection.count == 0:
            reference_status_text.value = ""
        elif config.FROZEN:
            reference_status_text.value = _PRODUCTION_REFERENCE_NOTE_HE
        else:
            reference_status_text.value = (
                f"נעשה שימוש ב-{selection.count} דוגמאות מאושרות לצורך סגנון ומינוח בלבד."
            )

    refresh_reference_status()

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
            refresh_reference_status()
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

    generator_view = ft.Column(
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
                [
                    ft.Row([add_photos_button, order_preview_button]),
                    ft.Text(
                        "סדר התמונות למטה נקבע לפי סדר הבחירה בסייר הקבצים של המחשב, "
                        "שאינו תמיד מדויק - יש לבדוק ולתקן את המספור (בעזרת החצים) לפני יצירת הדוח.",
                        size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    reference_status_text,
                    photos_list_view,
                ],
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

    page.add(build_app_root(page, generator_view, review_enabled=config.REFERENCE_REVIEW_ENABLED))


if __name__ == "__main__":
    ft.run(main)
