from __future__ import annotations

"""Memory view component for managing AI agent memory."""

import asyncio
import logging

import flet as ft

from api.client import APIClient

logger = logging.getLogger(__name__)


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
                ft.dropdown.Option("Всички"),
                ft.dropdown.Option("active"),
                ft.dropdown.Option("candidate"),
                ft.dropdown.Option("superseded")
            ],
            value="Всички",
            on_change=self.on_filter_change,
            width=200
        )

        self.refresh_btn = ft.ElevatedButton(
            "Обнови",
            icon=ft.icons.REFRESH,
            on_click=self.on_refresh_click
        )

        self._filter_task = None

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Съдържание")),
                ft.DataColumn(ft.Text("Статус")),
                ft.DataColumn(ft.Text("Действия")),
            ],
            rows=[]
        )

        self.content = ft.Column(
            controls=[
                ft.Row([self.status_dropdown, self.refresh_btn], alignment=ft.MainAxisAlignment.CENTER),
                ft.Column([ft.Row([self.data_table], scroll=ft.ScrollMode.ADAPTIVE)], scroll=ft.ScrollMode.ADAPTIVE, expand=True)
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True
        )

    def did_mount(self):
        self._load_task = self.page.run_task(self.load_memories)

    def will_unmount(self):
        if hasattr(self, "_load_task") and self._load_task:
            self._load_task.cancel()
        if hasattr(self, "_filter_task") and self._filter_task:
            self._filter_task.cancel()

    async def load_memories(self, *_args):
        try:
            status = self.status_dropdown.value
            if status == "Всички":
                status = None
            memories = await self.api_client.list_memories(status=status)
            self.memories = memories
            if getattr(self.page, "session_id", None) is not None:
                self.update_table()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"Грешка: {e}")
            if getattr(self.page, "session_id", None) is not None:
                self.page.open(
                    ft.SnackBar(
                        ft.Text("Failed to load memories.", color=ft.colors.WHITE),
                        bgcolor=ft.colors.RED
                    )
                )

    async def on_filter_change(self, e):
        if self._filter_task and not self._filter_task.done():
            self._filter_task.cancel()

        async def debounced_load():
            try:
                await asyncio.sleep(0.2)
                await self.load_memories()
            except asyncio.CancelledError:
                pass

        self._filter_task = self.page.run_task(debounced_load)

    async def on_refresh_click(self, e):
        await self.load_memories()

    def update_table(self):
        self.data_table.rows.clear()
        for memory in self.memories:

            # Inline edit state for each row

            content_text = ft.Container(
                content=ft.Text(memory.content or ""),
                expand=True,
                alignment=ft.alignment.top_left
            )
            content_field = ft.Container(
                content=ft.TextField(value=memory.content or "", multiline=True, min_lines=2, max_lines=5),
                expand=True,
                visible=False,
                alignment=ft.alignment.top_left
            )

            edit_btn = ft.IconButton(
                icon=ft.icons.EDIT,
                tooltip="Edit",
            )
            save_btn = ft.IconButton(
                icon=ft.icons.SAVE,
                tooltip="Save",
                visible=False,
            )
            cancel_btn = ft.IconButton(
                icon=ft.icons.CANCEL,
                tooltip="Cancel",
                visible=False,
            )

            delete_btn = ft.IconButton(
                icon=ft.icons.DELETE,
                tooltip="Delete",
            )

            activate_btn = ft.IconButton(
                icon=ft.icons.CHECK,
                icon_color=ft.colors.GREEN,
                tooltip="Activate",
                visible=memory.status == "candidate"
            )

            actions_row = ft.Row([activate_btn, edit_btn, save_btn, cancel_btn, delete_btn])

            # Capture variables for closures
            def create_edit_handler(c_text, c_field, e_btn, s_btn, ca_btn):
                def on_edit(e):
                    c_text.visible = False
                    c_field.visible = True
                    e_btn.visible = False
                    s_btn.visible = True
                    ca_btn.visible = True
                    if getattr(self.page, "session_id", None) is not None:
                        self.update()
                return on_edit

            def create_cancel_handler(c_text, c_field, e_btn, s_btn, ca_btn, mem):
                def on_cancel(e):
                    c_text.visible = True
                    c_field.visible = False
                    c_field.content.value = mem.content or "" # Reset value
                    e_btn.visible = True
                    s_btn.visible = False
                    ca_btn.visible = False
                    if getattr(self.page, "session_id", None) is not None:
                        self.update()
                return on_cancel

            def create_save_handler(c_text, c_field, e_btn, s_btn, ca_btn, mem):
                async def on_save(e):
                    new_content = c_field.content.value
                    try:
                        # Assuming importance defaults to 0 if none
                        importance = mem.importance if mem.importance is not None else 0
                        updated_mem = await self.api_client.update_memory(
                            memory_id=mem.id,
                            content=new_content,
                            status=mem.status,
                            importance=importance
                        )
                        # Find and update the memory in the local list
                        for i, m in enumerate(self.memories):
                            if m.id == updated_mem.id:
                                self.memories[i] = updated_mem
                                break
                        if getattr(self.page, "session_id", None) is not None:
                            self.update_table()
                    except asyncio.CancelledError:
                        pass
                    except Exception as ex:  # noqa: BLE001
                        logger.error(f"Грешка: {ex}")
                        if getattr(self.page, "session_id", None) is not None:
                            self.page.open(
                                ft.SnackBar(
                                    ft.Text(f"Failed to update memory: {ex}", color=ft.colors.WHITE),
                                    bgcolor=ft.colors.RED
                                )
                            )
                return on_save

            def create_delete_handler(mem):
                def on_delete_click(e):
                    # Close dialog handler
                    def close_dlg(e):
                        if getattr(self.page, "session_id", None) is not None:
                            self.page.close(confirm_dialog)

                    # Confirm delete handler
                    async def confirm_delete(e):
                        try:
                            await self.api_client.delete_memory(mem.id)
                            self.memories = [m for m in self.memories if m.id != mem.id]
                            if getattr(self.page, "session_id", None) is not None:
                                self.update_table()
                        except asyncio.CancelledError:
                            pass
                        except Exception as ex:  # noqa: BLE001
                            logger.error(f"Грешка: {ex}")
                            if getattr(self.page, "session_id", None) is not None:
                                self.page.open(
                                    ft.SnackBar(
                                        ft.Text(f"Failed to delete memory: {ex}", color=ft.colors.WHITE),
                                        bgcolor=ft.colors.RED
                                    )
                                )
                        finally:
                            if getattr(self.page, "session_id", None) is not None:
                                self.page.close(confirm_dialog)

                    confirm_dialog = ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Confirm Delete"),
                        content=ft.Text("Are you sure you want to delete this memory?"),
                        actions=[
                            ft.TextButton("Cancel", on_click=close_dlg),
                            ft.TextButton("Delete", on_click=confirm_delete, style=ft.ButtonStyle(color=ft.colors.RED)),
                        ],
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                    if getattr(self.page, "session_id", None) is not None:
                        self.page.open(confirm_dialog)
                return on_delete_click

            def create_activate_handler(mem, a_btn):
                async def on_activate(e):
                    try:
                        importance = mem.importance if mem.importance is not None else 0
                        updated_mem = await self.api_client.update_memory(
                            memory_id=mem.id,
                            content=mem.content,
                            status="active",
                            importance=importance
                        )
                        for i, m in enumerate(self.memories):
                            if m.id == updated_mem.id:
                                self.memories[i] = updated_mem
                                break
                        if getattr(self.page, "session_id", None) is not None:
                            self.update_table()
                            self.page.open(
                                ft.SnackBar(
                                    ft.Text("Споменът е активиран!", color=ft.colors.WHITE),
                                    bgcolor=ft.colors.GREEN
                                )
                            )
                    except asyncio.CancelledError:
                        pass
                    except Exception as ex:  # noqa: BLE001
                        logger.error(f"Грешка: {ex}")
                        if getattr(self.page, "session_id", None) is not None:
                            self.page.open(
                                ft.SnackBar(
                                    ft.Text(f"Failed to activate memory: {ex}", color=ft.colors.WHITE),
                                    bgcolor=ft.colors.RED
                                )
                            )
                return on_activate

            edit_btn.on_click = create_edit_handler(content_text, content_field, edit_btn, save_btn, cancel_btn)
            cancel_btn.on_click = create_cancel_handler(content_text, content_field, edit_btn, save_btn, cancel_btn, memory)
            save_btn.on_click = create_save_handler(content_text, content_field, edit_btn, save_btn, cancel_btn, memory)
            delete_btn.on_click = create_delete_handler(memory)
            activate_btn.on_click = create_activate_handler(memory, activate_btn)

            self.data_table.rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Row([content_text, content_field], expand=True, scroll=ft.ScrollMode.AUTO)),
                        ft.DataCell(ft.Text(memory.status or "")),
                        ft.DataCell(actions_row),
                    ]
                )
            )
        if getattr(self.page, "session_id", None) is not None:
            self.update()
