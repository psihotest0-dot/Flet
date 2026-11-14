"""Files view component for uploading and managing files."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import flet as ft

from api.client import SUPPORTED_EXTENSIONS, APIClient, IngestedFile, is_supported_file
from views.ui_utils import confirm_dialog, error_detail, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# Mirrors the limit enforced by APIClient.upload_file; checked before the file is
# read into memory so a huge file is not loaded just to be rejected.
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
POLL_INTERVAL = 3.0
FINAL_STATUSES = ("completed", "failed")
# Above this many unfinished files one GET /v1/files replaces one GET per file:
# after a batch upload of 30 books that was 10 requests a second.
BULK_POLL_THRESHOLD = 5

# The categories the backend accepts, in its order (config.FileCategories in
# go-monolith, internal/config/config.go). Kept by hand: a new category there needs
# a line here.
CATEGORIES = (
    ("medicine", "Медицина"),
    ("economics", "Икономика"),
    ("programming", "Програмиране"),
    ("law", "Право"),
    ("psychology", "Психология"),
    ("literature", "Литература"),
    ("philosophy", "Философия"),
    ("self_help", "Личностно развитие"),
    ("history", "История и биографии"),
    ("general", "Общи"),
)

# The value of "no filter" in the filter dropdowns.
FILTER_ALL = "all"

# The sortable columns, by their index in the table.
SORT_NAME, SORT_SIZE, SORT_UPLOADED = 0, 1, 2

# Backend `embedding_status` values (see internal/api/dto.go) mapped to the label
# and colour shown in the "Вектори" column. A file can finish ingestion with no
# chunks at all (scanned PDF, empty document) - the API reports that as "empty"
# so it is not confused with a searchable "completed" file.
EMBEDDING_STATES = {
    "pending": ("Изчаква", ft.Colors.AMBER),
    "processing": ("Обработва се", ft.Colors.BLUE),
    "completed": ("Готово", ft.Colors.GREEN),
    "empty": ("Няма текст", ft.Colors.ORANGE),
    "failed": ("Грешка", ft.Colors.RED),
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


def embedding_key(file: IngestedFile) -> str:
    """The key of the file's vector state in :data:`EMBEDDING_STATES`.

    The backend derives ``embedding_status`` from the ingest status plus the
    stored chunk count. Payloads from an older backend only carry ``status``,
    so they are mapped through the same table instead of showing a blank cell.
    """
    status = file.embedding_status or file.status or ""
    if status == "completed" and file.has_embeddings is False:
        # Completed, but nothing was stored to search through.
        status = "empty"
    return status


def embedding_state(file: IngestedFile) -> tuple[str, str, str]:
    """Return ``(label, color, tooltip)`` describing the file's vectors."""
    status = embedding_key(file)
    label, color = EMBEDDING_STATES.get(
        status, (status or "Неизвестно", ft.Colors.GREY)
    )
    tooltip = EMBEDDING_TOOLTIPS.get(status, UNKNOWN_EMBEDDING_TOOLTIP)

    if status == "completed" and file.chunk_count:
        return f"{label} · {chunk_label(file.chunk_count)}", color, tooltip
    return label, color, tooltip


def size_label(size: int | None) -> str:
    """A file size the way the table shows it: "820 B", "14 KB", "2,4 MB"."""
    if size is None or size < 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB".replace(".", ",")


def uploaded_label(created_at: datetime | None) -> str:
    """The upload date in local time, day first: "14.11.2026"."""
    if created_at is None:
        return ""
    return created_at.astimezone().strftime("%d.%m.%Y")


def visible_files(
    files: list[IngestedFile],
    query: str = "",
    category: str = FILTER_ALL,
    state: str = FILTER_ALL,
    sort_column: int | None = SORT_UPLOADED,
    ascending: bool = False,
) -> list[IngestedFile]:
    """The files the table shows: filtered by name, category and vector state, sorted.

    The name matches anywhere and without regard to case. A file without the field
    a sort uses (no size, no date) goes last whatever the direction.
    """
    needle = query.strip().casefold()
    shown = [
        f
        for f in files
        if (not needle or needle in (f.file_name or "").casefold())
        and (category == FILTER_ALL or (f.category or "general") == category)
        and (state == FILTER_ALL or embedding_key(f) == state)
    ]
    if sort_column == SORT_NAME:
        shown.sort(key=lambda f: (f.file_name or "").casefold(), reverse=not ascending)
        return shown
    if sort_column not in (SORT_SIZE, SORT_UPLOADED):
        return shown

    def value(f: IngestedFile) -> float | None:
        if sort_column == SORT_SIZE:
            return None if f.file_size is None else float(f.file_size)
        return None if f.created_at is None else f.created_at.timestamp()

    keyed = [(value(f), f) for f in shown]
    known = sorted(
        ((v, f) for v, f in keyed if v is not None),
        key=lambda pair: pair[0],
        reverse=not ascending,
    )
    return [f for _v, f in known] + [f for v, f in keyed if v is None]


class FilesView(ft.Container):
    """The view for uploading, viewing, and managing files."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        # ``Control.page`` is read-only and raises until the view is on the page;
        # the page the app was started with is kept under its own name.
        self.page_ref = page
        self.api_client = api_client
        self.files = []
        self.alignment = ft.Alignment.TOP_CENTER
        self.expand = True

        # Components
        # A service, not a control: Flet registers it with the page by itself.
        self.file_picker = ft.FilePicker()

        self.upload_btn = ft.Button(
            "Качи файл",
            icon=ft.Icons.UPLOAD_FILE,
            on_click=self.on_upload_click,
        )

        self.upload_progress = ft.ProgressRing(
            width=16, height=16, stroke_width=2, visible=False
        )

        self.refresh_btn = ft.Button(
            "Обнови",
            icon=ft.Icons.REFRESH,
            on_click=self.on_refresh_click,
        )

        # Search and filters work on the list already loaded: no request per key.
        self.search_field = ft.TextField(
            label="Търси по име",
            prefix_icon=ft.Icons.SEARCH,
            width=260,
            dense=True,
            on_change=self._on_filter_change,
        )
        self.category_filter = ft.Dropdown(
            label="Категория",
            value=FILTER_ALL,
            options=[ft.DropdownOption(FILTER_ALL, "Всички")]
            + [ft.DropdownOption(key, label) for key, label in CATEGORIES],
            width=220,
            dense=True,
            on_select=self._on_filter_change,
        )
        self.state_filter = ft.Dropdown(
            label="Вектори",
            value=FILTER_ALL,
            options=[ft.DropdownOption(FILTER_ALL, "Всички")]
            + [
                ft.DropdownOption(key, label)
                for key, (label, _c) in EMBEDDING_STATES.items()
            ],
            width=180,
            dense=True,
            on_select=self._on_filter_change,
        )
        self.count_text = ft.Text("", size=12, color=ft.Colors.GREY)

        self.data_table = ft.DataTable(
            # Newest first, as the backend sends them.
            sort_column_index=SORT_UPLOADED,
            sort_ascending=False,
            columns=[
                ft.DataColumn(ft.Text("Име на файл"), on_sort=self._on_sort),
                ft.DataColumn(ft.Text("Размер"), numeric=True, on_sort=self._on_sort),
                ft.DataColumn(ft.Text("Качен"), on_sort=self._on_sort),
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
                ft.Row(
                    [
                        self.search_field,
                        self.category_filter,
                        self.state_filter,
                        self.count_text,
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                ft.Column(
                    [
                        ft.Row(
                            [self.data_table],
                            scroll=ft.ScrollMode.ADAPTIVE,
                            # A scrollable row is as wide as the tab; without this the
                            # table sits at its left edge instead of in the middle.
                            alignment=ft.MainAxisAlignment.CENTER,
                        ),
                    ],
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
        self._sort_column: int | None = SORT_UPLOADED
        self._sort_ascending = False

    # -------------------------------------------------------------- lifecycle
    def did_mount(self):
        if self._load_task is None:
            self._load_task = self.page_ref.run_task(self.initial_load)
        if self._active:
            self.set_active(True)

    def set_active(self, active: bool):
        """Poll file statuses only while this tab is the visible one."""
        self._active = active
        if active:
            if self._poll_task is None and self.page_ref is not None:
                self._polling = True
                self._poll_task = self.page_ref.run_task(self.poll_statuses)
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
            show_snackbar(self.page_ref, "Неуспешно зареждане на файловете.")
            return
        self.files = files
        self.update_table()

    # ---------------------------------------------------------- filter, sort
    def _shown(self) -> list[IngestedFile]:
        return visible_files(
            self.files,
            self.search_field.value or "",
            self.category_filter.value or FILTER_ALL,
            self.state_filter.value or FILTER_ALL,
            self._sort_column,
            self._sort_ascending,
        )

    def _filtering(self) -> bool:
        return bool(
            (self.search_field.value or "").strip()
            or (self.category_filter.value or FILTER_ALL) != FILTER_ALL
            or (self.state_filter.value or FILTER_ALL) != FILTER_ALL
        )

    def _count_label(self, shown: int) -> str:
        total = len(self.files)
        if self._filtering():
            return f"Показани {shown} от {total}"
        return f"{total} файла" if total != 1 else "1 файл"

    def _on_filter_change(self, _e=None):
        self.update_table()

    def _on_sort(self, e):
        self._sort_column = e.column_index
        self._sort_ascending = e.ascending
        self.data_table.sort_column_index = e.column_index
        self.data_table.sort_ascending = e.ascending
        self.update_table()

    def _apply_status(self, file_id: str, updated) -> None:
        """Refresh the cells of a single row after a polled status response."""
        previous = None
        for i, file in enumerate(self.files):
            if file.id == file_id:
                previous = file
                self.files[i] = updated
                break

        # Under a "Вектори" filter a file whose state changed may no longer belong
        # in the table (or now does): show the filter's answer, not a stale row.
        if (
            (self.state_filter.value or FILTER_ALL) != FILTER_ALL
            and previous is not None
            and embedding_key(previous) != embedding_key(updated)
        ):
            self.update_table()
            return

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
                file
                for file in self.files
                if file.id and file.status not in FINAL_STATUSES
            ]
            if not pending_files:
                continue

            if len(pending_files) > BULK_POLL_THRESHOLD:
                await self._poll_with_one_list(pending_files)
                continue

            # Sequential requests used to block the loop for len(pending) * RTT.
            results = await asyncio.gather(
                *(self.api_client.get_file_status(file.id) for file in pending_files),
                return_exceptions=True,
            )
            for file, result in zip(pending_files, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.warning(
                        "Failed to refresh status of %s: %s", file.id, result
                    )
                    continue
                self._apply_status(file.id, result)

    async def _poll_with_one_list(self, pending_files: list[IngestedFile]) -> None:
        """Refresh many unfinished files with one GET /v1/files."""
        try:
            fresh = await self.api_client.list_files()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to refresh %d file statuses: %s", len(pending_files), e
            )
            return
        by_id = {f.id: f for f in fresh if f.id}
        for file in pending_files:
            updated = by_id.get(file.id)
            # A file missing from the list was deleted elsewhere; the next full
            # load drops its row.
            if updated is not None:
                self._apply_status(file.id, updated)

    async def on_upload_click(self, e):
        """Open the system dialog and upload what the user chose in it."""
        try:
            files = await self.file_picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=list(SUPPORTED_EXTENSIONS),
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("The file dialog failed: %s", ex)
            show_snackbar(self.page_ref, "Диалогът за избор на файл не се отвори.")
            return
        await self.upload_files(files)

    async def upload_files(self, files: list[ft.FilePickerFile] | None):
        if not files:
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
                        logger.warning(
                            "Skipped %s: the format is not supported.", f.name
                        )
                        errors.append(f"{f.name}: форматът не се поддържа")
                        return
                    size = await asyncio.to_thread(lambda: Path(f.path).stat().st_size)
                    if size > MAX_UPLOAD_SIZE:
                        logger.warning(
                            "Skipped %s: %d bytes is over the 100 MB limit.",
                            f.name,
                            size,
                        )
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
                    errors.append(f"{f.name}: {error_detail(ex)}")

        try:
            await asyncio.gather(*(upload_single_file(f) for f in files))
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
            show_snackbar(self.page_ref, "\n".join(notes), success=not errors)

        await self.initial_load()

    def update_table(self):
        self.data_table.rows.clear()
        self._status_cells.clear()
        self._embedding_cells.clear()
        self._rows.clear()

        shown = self._shown()
        for file in shown:
            status_text = ft.Text(file.status or "Unknown")
            label, color, tooltip = embedding_state(file)
            embedding_text = ft.Text(label, size=12, color=color, tooltip=tooltip)
            del_btn = ft.IconButton(
                icon=ft.Icons.DELETE,
                tooltip="Изтрий",
                on_click=self.create_delete_handler(file.id),
            )
            category_dropdown = ft.Dropdown(
                value=file.category,
                options=[ft.DropdownOption(key, label) for key, label in CATEGORIES],
                # Wide enough for the longest name ("История и биографии"): the
                # menu cuts a label that does not fit instead of letting it spill.
                width=240,
                dense=True,
                on_select=self.create_category_change_handler(file.id),
            )
            row = ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(file.file_name or "Unknown")),
                    ft.DataCell(ft.Text(size_label(file.file_size), size=12)),
                    ft.DataCell(ft.Text(uploaded_label(file.created_at), size=12)),
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

        self.count_text.value = self._count_label(len(shown))
        # Only the table and the count are re-sent, not the whole view.
        safe_update(self, self.data_table, self.count_text)

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
        self.count_text.value = self._count_label(len(self.data_table.rows))
        safe_update(self, self.data_table, self.count_text)

    def create_category_change_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            new_category = e.control.value
            saved_category = next(
                (f.category for f in self.files if f.id == file_id), None
            )
            try:
                updated_file = await self.api_client.update_file_category(
                    file_id, new_category
                )
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                logger.error("Failed to update category: %s", ex)
                # The dropdown already shows the value that was just refused. Put the
                # saved one back, so the screen never shows a category the database
                # does not have.
                e.control.value = saved_category
                safe_update(self, e.control)
                show_snackbar(
                    self.page_ref,
                    f"Грешка при обновяване на категорията: {error_detail(ex)}",
                )
                return

            # The dropdown already shows the new value; no reload needed.
            for i, file in enumerate(self.files):
                if file.id == file_id:
                    self.files[i] = updated_file
                    break
            show_snackbar(self.page_ref, "Категорията е обновена!", success=True)

        return handler

    def create_delete_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            confirmed = await confirm_dialog(
                self.page_ref,
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
                show_snackbar(
                    self.page_ref, f"Грешка при изтриване на файла: {error_detail(ex)}"
                )
                return

            self.remove_file_row(file_id)

        return handler
