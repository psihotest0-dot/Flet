from __future__ import annotations

"""Memory view component for managing AI agent memory."""

import asyncio
import logging

import flet as ft

from api.client import APIClient
from views.ui_utils import confirm_dialog, safe_update, show_snackbar

logger = logging.getLogger(__name__)

FILTER_ALL = "Всички"
FILTER_DEBOUNCE = 0.2


class MemoryRow(ft.DataRow):
    """A single memory row that owns its inline edit state.

    Editing or saving a memory used to rebuild the whole ``DataTable`` - every
    row, dropdown and handler was recreated and the entire table was re-sent to
    the client. The row is a component now: it mutates only its own cells and
    re-sends just those controls.
    """

    def __init__(self, memory, view: MemoryView):
        super().__init__(cells=[])
        self.memory = memory
        self.view = view
        self._saving = False

        self.content_text = ft.Text(memory.content or "")
        self.content_text_container = ft.Container(
            content=self.content_text,
            expand=True,
            alignment=ft.alignment.top_left,
        )

        self.content_field = ft.TextField(
            value=memory.content or "",
            multiline=True,
            min_lines=2,
            max_lines=5,
        )
        self.content_field_container = ft.Container(
            content=self.content_field,
            expand=True,
            visible=False,
            alignment=ft.alignment.top_left,
        )

        self.edit_btn = ft.IconButton(icon=ft.icons.EDIT, tooltip="Редактирай", on_click=self._edit)
        self.save_btn = ft.IconButton(icon=ft.icons.SAVE, tooltip="Запази", visible=False, on_click=self._save)
        self.cancel_btn = ft.IconButton(icon=ft.icons.CANCEL, tooltip="Отказ", visible=False, on_click=self._cancel)
        self.delete_btn = ft.IconButton(icon=ft.icons.DELETE, tooltip="Изтрий", on_click=self._delete)
        self.activate_btn = ft.IconButton(
            icon=ft.icons.CHECK,
            icon_color=ft.colors.GREEN,
            tooltip="Активирай",
            visible=memory.status == "candidate",
            on_click=self._activate,
        )
        self.status_text = ft.Text(memory.status or "")

        self.actions_row = ft.Row(
            [self.activate_btn, self.edit_btn, self.save_btn, self.cancel_btn, self.delete_btn]
        )

        self.cells = [
            ft.DataCell(
                ft.Row(
                    [self.content_text_container, self.content_field_container],
                    expand=True,
                    scroll=ft.ScrollMode.AUTO,
                )
            ),
            ft.DataCell(self.status_text),
            ft.DataCell(self.actions_row),
        ]

    # ------------------------------------------------------------- edit state
    def _set_editing(self, editing: bool):
        self.content_text_container.visible = not editing
        self.content_field_container.visible = editing
        self.edit_btn.visible = not editing
        self.save_btn.visible = editing
        self.cancel_btn.visible = editing
        safe_update(
            self.view,
            self.content_text_container,
            self.content_field_container,
            self.edit_btn,
            self.save_btn,
            self.cancel_btn,
        )

    def _edit(self, e):
        self.content_field.value = self.memory.content or ""
        self._set_editing(True)

    def _cancel(self, e):
        self.content_field.value = self.memory.content or ""
        self._set_editing(False)

    def apply(self, memory):
        """Refresh the row from a server response without rebuilding the table."""
        self.memory = memory
        self.content_text.value = memory.content or ""
        self.content_field.value = memory.content or ""
        self.status_text.value = memory.status or ""
        self.activate_btn.visible = memory.status == "candidate"
        safe_update(self.view, self.content_text, self.status_text, self.activate_btn)

    async def _save(self, e):
        if self._saving or not self.memory.id:
            return

        new_content = (self.content_field.value or "").strip()
        if not new_content or new_content == (self.memory.content or ""):
            # Nothing changed, or the user tried to save an empty memory.
            self.content_field.value = self.memory.content or ""
            self._set_editing(False)
            return

        self._saving = True
        self.save_btn.disabled = True
        safe_update(self.view, self.save_btn)
        try:
            updated = await self.view.api_client.update_memory(
                memory_id=self.memory.id,
                content=new_content,
                status=self.memory.status,
                importance=self.memory.importance if self.memory.importance is not None else 0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to update memory: %s", ex)
            show_snackbar(self.view.page, f"Грешка при запазване на спомена: {ex}")
            return
        finally:
            self._saving = False
            self.save_btn.disabled = False
            safe_update(self.view, self.save_btn)

        self.view.replace_memory(updated)
        self.apply(updated)
        self._set_editing(False)

    async def _activate(self, e):
        if not self.memory.id:
            return
        try:
            updated = await self.view.api_client.update_memory(
                memory_id=self.memory.id,
                content=self.memory.content,
                status="active",
                importance=self.memory.importance if self.memory.importance is not None else 0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to activate memory: %s", ex)
            show_snackbar(self.view.page, f"Грешка при активиране на спомена: {ex}")
            return

        self.view.replace_memory(updated)
        self.apply(updated)
        show_snackbar(self.view.page, "Споменът е активиран!", success=True)

    async def _delete(self, e):
        if not self.memory.id:
            return
        confirmed = await confirm_dialog(
            self.view.page,
            "Изтриване на спомен",
            "Сигурни ли сте, че искате да изтриете този спомен?",
        )
        if not confirmed:
            return

        try:
            await self.view.api_client.delete_memory(self.memory.id)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to delete memory: %s", ex)
            show_snackbar(self.view.page, f"Грешка при изтриване на спомена: {ex}")
            return

        self.view.remove_memory_row(self)


class MemoryView(ft.Container):
    """The main view for listing, editing, and deleting agent memories."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.memories = []
        self.alignment = ft.alignment.top_center
        self.expand = True

        self.status_dropdown = ft.Dropdown(
            label="Филтър по статус",
            options=[
                ft.dropdown.Option(FILTER_ALL),
                ft.dropdown.Option("active"),
                ft.dropdown.Option("candidate"),
                ft.dropdown.Option("superseded"),
            ],
            value=FILTER_ALL,
            on_change=self.on_filter_change,
            width=200,
        )

        self.refresh_btn = ft.ElevatedButton(
            "Обнови",
            icon=ft.icons.REFRESH,
            on_click=self.on_refresh_click,
        )

        self.progress = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Съдържание")),
                ft.DataColumn(ft.Text("Статус")),
                ft.DataColumn(ft.Text("Действия")),
            ],
            rows=[],
        )

        self.empty_hint = ft.Text(
            "Няма намерени спомени.", color=ft.colors.GREY_500, visible=False
        )

        self.content = ft.Column(
            controls=[
                ft.Row(
                    [self.status_dropdown, self.refresh_btn, self.progress],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.empty_hint,
                ft.Column(
                    [ft.Row([self.data_table], scroll=ft.ScrollMode.ADAPTIVE)],
                    scroll=ft.ScrollMode.ADAPTIVE,
                    expand=True,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

        self._filter_task = None
        self._load_task = None
        # Monotonic token: a response is applied only if it is still the newest
        # request, so a slow "all" request cannot overwrite a newer "active" one.
        self._load_seq = 0

    def did_mount(self):
        self._load_task = self.page.run_task(self.load_memories)

    def will_unmount(self):
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        if self._filter_task is not None:
            self._filter_task.cancel()
            self._filter_task = None

    async def load_memories(self, *_args):
        self._load_seq += 1
        sequence = self._load_seq

        status = self.status_dropdown.value
        if status == FILTER_ALL:
            status = None

        self.progress.visible = True
        safe_update(self, self.progress)
        try:
            memories = await self.api_client.list_memories(status=status)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load memories: %s", e)
            show_snackbar(self.page, "Неуспешно зареждане на спомените.")
            return
        finally:
            if sequence == self._load_seq:
                self.progress.visible = False
                safe_update(self, self.progress)

        if sequence != self._load_seq:
            return

        self.memories = memories
        self.update_table()

    async def on_filter_change(self, e):
        if self._filter_task is not None and not self._filter_task.done():
            self._filter_task.cancel()

        async def debounced_load():
            try:
                await asyncio.sleep(FILTER_DEBOUNCE)
                await self.load_memories()
            except asyncio.CancelledError:
                pass

        self._filter_task = self.page.run_task(debounced_load)

    async def on_refresh_click(self, e):
        await self.load_memories()

    def update_table(self):
        self.data_table.rows.clear()
        for memory in self.memories:
            self.data_table.rows.append(MemoryRow(memory, self))
        self.empty_hint.visible = not self.memories
        # Only the table and the hint are re-sent, not the whole view.
        safe_update(self, self.data_table, self.empty_hint)

    def replace_memory(self, memory):
        """Keep ``self.memories`` in sync without re-rendering anything."""
        for i, existing in enumerate(self.memories):
            if existing.id == memory.id:
                self.memories[i] = memory
                return
        self.memories.append(memory)

    def remove_memory_row(self, row: MemoryRow):
        memory_id = row.memory.id
        self.memories = [m for m in self.memories if m.id != memory_id]
        try:
            self.data_table.rows.remove(row)
        except ValueError:
            pass
        self.empty_hint.visible = not self.memories
        safe_update(self, self.data_table, self.empty_hint)


