from __future__ import annotations

"""Files view component for uploading and managing files."""

import asyncio
import logging
from pathlib import Path

import flet as ft

from api.client import SUPPORTED_EXTENSIONS, APIClient, IngestedFile, is_supported_file
from views.ui_utils import confirm_dialog, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# Mirrors the limit enforced by APIClient.upload_file; checked before the file is
# read into memory so a huge file is not loaded just to be rejected.
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
POLL_INTERVAL = 3.0
FINAL_STATUSES = ("completed", "failed")

# Backend `embedding_status` values (see internal/api/dto.go) mapped to the label
# and colour shown in the "Вектори" column. A file can finish ingestion with no
# chunks at all (scanned PDF, empty document) - the API reports that as "empty"
# so it is not confused with a searchable "completed" file.
EMBEDDING_STATES = {
    "pending": ("Изчаква", ft.colors.AMBER),
    "processing": ("Обработва се", ft.colors.BLUE),
    "completed": ("Готово", ft.colors.GREEN),
    "empty": ("Няма текст", ft.colors.ORANGE),
    "failed": ("Грешка", ft.colors.RED),
}

EMBEDDING_TOOLTIPS = {
    "pending": "Чака обработка.",
    "processing": "Извлича се текстът и се създават части за търсене.",
    "completed": "Файлът участва в търсенето в чата.",
    "empty": (
        "Обработен, но без части (празен или неразпознат текст). "
        "Файлът няма да бъде намерен при търсене."
    ),
    "failed": "Обработката е неуспешна - файлът не участва в търсенето.",
}
UNKNOWN_EMBEDDING_TOOLTIP = "Няма информация за индексирането на файла."


def chunk_label(count: int | None) -> str:
    """Human readable chunk count using the Bulgarian plural forms."""
    if count is None:
        return ""
    if count == 0:
        return "няма части"
    if count == 1:
        return "1 част"
    return f"{count} части"


def embedding_state(file: IngestedFile) -> tuple[str, str, str]:
    """Return ``(label, color, tooltip)`` describing the file's vectors.

    The backend derives ``embedding_status`` from the ingest status plus the
    stored chunk count. Payloads from an older backend only carry ``status``,
    so they are mapped through the same table instead of showing a blank cell.
    """
    status = file.embedding_status or file.status or ""
    if status == "completed" and file.has_embeddings is False:
        # Completed, but nothing was stored to search through.
        status = "empty"

    label, color = EMBEDDING_STATES.get(
        status, (status or "Неизвестно", ft.colors.GREY)
    )
    tooltip = EMBEDDING_TOOLTIPS.get(status, UNKNOWN_EMBEDDING_TOOLTIP)

    if status == "completed" and file.chunk_count:
        return f"{label} · {chunk_label(file.chunk_count)}", color, tooltip
    return label, color, tooltip


class FilesView(ft.Container):
    """The view for uploading, viewing, and managing files."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.files = []
        self.alignment = ft.alignment.top_center
        self.expand = True

        # Components
        self.file_picker = ft.FilePicker(on_result=self.on_file_picker_result)

        self.upload_btn = ft.ElevatedButton(
            "Качи файл",
            icon=ft.icons.UPLOAD_FILE,
            on_click=lambda _: self.file_picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=list(SUPPORTED_EXTENSIONS),
            ),
        )

        self.upload_progress = ft.ProgressRing(
            width=16, height=16, stroke_width=2, visible=False
        )

        self.refresh_btn = ft.ElevatedButton(
            "Обнови",
            icon=ft.icons.REFRESH,
            on_click=self.on_refresh_click,
        )

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Име на файл")),
                ft.DataColumn(ft.Text("Статус")),
                ft.DataColumn(
                    ft.Text(
                        "Вектори",
                        tooltip="Части, по които файлът се търси в чата",
                    )
                ),
                ft.DataColumn(ft.Text("Категория")),
                ft.DataColumn(ft.Text("Действия")),
            ],
            rows=[],
        )

        self.content = ft.Column(
            controls=[
                ft.Row(
                    [self.upload_btn, self.upload_progress, self.refresh_btn],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Column(
                    [ft.Row([self.data_table], scroll=ft.ScrollMode.ADAPTIVE)],
                    scroll=ft.ScrollMode.ADAPTIVE,
                    expand=True,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

        self._polling = False
        self._active = False
        self._load_task = None
        self._poll_task = None
        # file_id -> status Text control, so a status change re-sends one cell
        # instead of rebuilding the whole table.
        self._status_cells: dict[str, ft.Text] = {}
        self._embedding_cells: dict[str, ft.Text] = {}
        self._rows: dict[str, ft.DataRow] = {}

    # -------------------------------------------------------------- lifecycle
    def did_mount(self):
        if self.file_picker not in self.page.overlay:
            self.page.overlay.append(self.file_picker)
            self.page.update()
        if self._load_task is None:
            self._load_task = self.page.run_task(self.initial_load)
        if self._active:
            self.set_active(True)

    def set_active(self, active: bool):
        """Poll file statuses only while this tab is the visible one."""
        self._active = active
        if active:
            if self._poll_task is None and self.page is not None:
                self._polling = True
                self._poll_task = self.page.run_task(self.poll_statuses)
        else:
            self._polling = False
            if self._poll_task is not None:
                self._poll_task.cancel()
                self._poll_task = None

    def will_unmount(self):
        self._polling = False
        self._active = False
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self.page is not None and self.file_picker in self.page.overlay:
            self.page.overlay.remove(self.file_picker)
            self.page.update()

    # ------------------------------------------------------------------- data
    async def on_refresh_click(self, e):
        await self.initial_load()

    async def initial_load(self, *_args):
        try:
            files = await self.api_client.list_files()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load files: %s", e)
            show_snackbar(self.page, "Неуспешно зареждане на файловете.")
            return
        self.files = files
        self.update_table()

    def _apply_status(self, file_id: str, updated) -> None:
        """Refresh the cells of a single row after a polled status response."""
        for i, file in enumerate(self.files):
            if file.id == file_id:
                self.files[i] = updated
                break

        dirty: list[ft.Control] = []

        cell = self._status_cells.get(file_id)
        status = updated.status or "Unknown"
        if cell is not None and cell.value != status:
            cell.value = status
            dirty.append(cell)

        # The chunk count travels with the same payload, so the "Вектори" cell is
        # refreshed in the same round trip instead of a second request.
        embedding_cell = self._embedding_cells.get(file_id)
        label, color, tooltip = embedding_state(updated)
        if embedding_cell is not None and (
            embedding_cell.value != label
            or embedding_cell.color != color
            or embedding_cell.tooltip != tooltip
        ):
            embedding_cell.value = label
            embedding_cell.color = color
            embedding_cell.tooltip = tooltip
            dirty.append(embedding_cell)

        if dirty:
            safe_update(self, *dirty)

    async def poll_statuses(self, *_args):
        while self._polling:
            await asyncio.sleep(POLL_INTERVAL)
            if not self._polling:
                break

            pending_files = [
                file for file in self.files
                if file.id and file.status not in FINAL_STATUSES
            ]
            if not pending_files:
                continue

            # Sequential requests used to block the loop for len(pending) * RTT.
            results = await asyncio.gather(
                *(self.api_client.get_file_status(file.id) for file in pending_files),
                return_exceptions=True,
            )
            for file, result in zip(pending_files, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.warning("Failed to refresh status of %s: %s", file.id, result)
                    continue
                self._apply_status(file.id, result)

    async def on_file_picker_result(self, e: ft.FilePickerResultEvent):
        if not e.files:
            return

        self.upload_btn.disabled = True
        self.upload_progress.visible = True
        safe_update(self, self.upload_btn, self.upload_progress)

        sem = asyncio.Semaphore(3)
        errors: list[str] = []
        duplicates: list[str] = []

        async def upload_single_file(f):
            async with sem:
                try:
                    if not f.path:
                        return
                    if not is_supported_file(f.name):
                        # The picker is filtered, but a file can still arrive from
                        # elsewhere; the backend would accept it and fail later.
                        errors.append(f"{f.name}: форматът не се поддържа")
                        return
                    size = await asyncio.to_thread(lambda: Path(f.path).stat().st_size)
                    if size > MAX_UPLOAD_SIZE:
                        errors.append(f"{f.name}: файлът е по-голям от 100 MB")
                        return
                    # Reading is blocking, the upload itself is not.
                    content = await asyncio.to_thread(Path(f.path).read_bytes)
                    result = await self.api_client.upload_file(content, f.name)
                    if result.duplicate:
                        duplicates.append(f.name)
                except asyncio.CancelledError:
                    raise
                except Exception as ex:  # noqa: BLE001
                    logger.error("Failed to upload %s: %s", f.name, ex)
                    errors.append(f"{f.name}: {ex}")

        try:
            await asyncio.gather(*(upload_single_file(f) for f in e.files))
        finally:
            self.upload_btn.disabled = False
            self.upload_progress.visible = False
            safe_update(self, self.upload_btn, self.upload_progress)

        # A single snackbar for the batch instead of one per file; a snackbar
        # replaces the previous one, so errors and duplicates share it.
        notes = []
        if errors:
            notes.append("Неуспешно качване: " + "; ".join(errors))
        if duplicates:
            notes.append("Файлът вече е качен: " + ", ".join(duplicates))
        if notes:
            show_snackbar(self.page, "\n".join(notes), success=not errors)

        await self.initial_load()

    def update_table(self):
        self.data_table.rows.clear()
        self._status_cells.clear()
        self._embedding_cells.clear()
        self._rows.clear()

        for file in self.files:
            status_text = ft.Text(file.status or "Unknown")
            label, color, tooltip = embedding_state(file)
            embedding_text = ft.Text(label, size=12, color=color, tooltip=tooltip)
            del_btn = ft.IconButton(
                icon=ft.icons.DELETE,
                tooltip="Изтрий",
                on_click=self.create_delete_handler(file.id),
            )
            category_dropdown = ft.Dropdown(
                value=file.category,
                options=[
                    ft.dropdown.Option("medicine", "Медицина"),
                    ft.dropdown.Option("economics", "Икономика"),
                    ft.dropdown.Option("programming", "Програмиране"),
                    ft.dropdown.Option("law", "Право"),
                    ft.dropdown.Option("psychology", "Психология"),
                    ft.dropdown.Option("literature", "Литература"),
                    ft.dropdown.Option("philosophy", "Философия"),
                    ft.dropdown.Option("self_help", "Личностно развитие"),
                    ft.dropdown.Option("history", "История и биографии"),
                    ft.dropdown.Option("general", "Общи"),
                ],
                width=160,
                dense=True,
                on_change=self.create_category_change_handler(file.id),
            )
            row = ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(file.file_name or "Unknown")),
                    ft.DataCell(status_text),
                    ft.DataCell(embedding_text),
                    ft.DataCell(category_dropdown),
                    ft.DataCell(del_btn),
                ]
            )
            self.data_table.rows.append(row)
            if file.id:
                self._status_cells[file.id] = status_text
                self._embedding_cells[file.id] = embedding_text
                self._rows[file.id] = row

        # Only the table is re-sent, not the whole view.
        safe_update(self, self.data_table)

    def remove_file_row(self, file_id: str):
        self.files = [file for file in self.files if file.id != file_id]
        self._status_cells.pop(file_id, None)
        self._embedding_cells.pop(file_id, None)
        row = self._rows.pop(file_id, None)
        if row is not None:
            try:
                self.data_table.rows.remove(row)
            except ValueError:
                pass
            safe_update(self, self.data_table)

    def create_category_change_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            new_category = e.control.value
            saved_category = next((f.category for f in self.files if f.id == file_id), None)
            try:
                updated_file = await self.api_client.update_file_category(file_id, new_category)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                logger.error("Failed to update category: %s", ex)
                # The dropdown already shows the value that was just refused. Put the
                # saved one back, so the screen never shows a category the database
                # does not have.
                e.control.value = saved_category
                safe_update(self, e.control)
                show_snackbar(self.page, f"Грешка при обновяване на категорията: {ex}")
                return

            # The dropdown already shows the new value; no reload needed.
            for i, file in enumerate(self.files):
                if file.id == file_id:
                    self.files[i] = updated_file
                    break
            show_snackbar(self.page, "Категорията е обновена!", success=True)

        return handler

    def create_delete_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            confirmed = await confirm_dialog(
                self.page,
                "Изтриване на файл",
                "Сигурни ли сте, че искате да изтриете този файл?",
            )
            if not confirmed:
                return

            try:
                await self.api_client.delete_file(file_id)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                logger.error("Failed to delete file: %s", ex)
                show_snackbar(self.page, f"Грешка при изтриване на файла: {ex}")
                return

            self.remove_file_row(file_id)

        return handler

