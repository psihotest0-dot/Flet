from __future__ import annotations

"""Files view component for uploading and managing files."""

import asyncio
import logging
from pathlib import Path

import flet as ft

from api.client import APIClient
from state.files import FilesState

logger = logging.getLogger(__name__)


class FilesView(ft.Container):
    """The view for uploading, viewing, and managing files."""
    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.state = FilesState()
        self.alignment = ft.alignment.top_center
        self.expand = True

        # Subscribe to state changes
        self.state.add_listener(self.update_table)

        # Components
        self.file_picker = ft.FilePicker(on_result=self.on_file_picker_result)

        self.upload_btn = ft.ElevatedButton(
            "Upload File",
            icon=ft.icons.UPLOAD_FILE,
            on_click=lambda _: self.file_picker.pick_files()
        )

        self.refresh_btn = ft.ElevatedButton(
            "Refresh",
            icon=ft.icons.REFRESH,
            on_click=self.on_refresh_click
        )

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("File Name")),
                ft.DataColumn(ft.Text("Status")),
                ft.DataColumn(ft.Text("Категория")),
                ft.DataColumn(ft.Text("Actions")),
            ],
            rows=[]
        )

        self.content = ft.Column(
            controls=[
                ft.Row([self.upload_btn, self.refresh_btn], alignment=ft.MainAxisAlignment.CENTER),
                self.data_table
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER
        )

        self._polling = False

    def did_mount(self):
        if self.file_picker not in self.page.overlay:
            self.page.overlay.append(self.file_picker)
            self.page.update()
        self._polling = True
        self.page.run_task(self.initial_load)
        self.page.run_task(self.poll_statuses)

    def will_unmount(self):
        if self.file_picker in self.page.overlay:
            self.page.overlay.remove(self.file_picker)
            self.page.update()
        self._polling = False

    async def on_refresh_click(self, e):
        await self.initial_load()

    async def initial_load(self, *_args):
        try:
            files = await self.api_client.list_files()
            self.state.set_files(files)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Грешка: {e}")
            self.page.open(
                ft.SnackBar(
                    ft.Text("Failed to load files.", color=ft.colors.WHITE),
                    bgcolor=ft.colors.RED
                )
            )

    async def poll_statuses(self, *_args):
        while self._polling:
            await asyncio.sleep(3)
            if not self.state:
                continue
            active_files = [f for f in self.state.files if f.status not in ("completed", "failed")]
            for file in active_files:
                if not self._polling:
                    break
                if file.id is None:
                    continue
                try:
                    updated_file = await self.api_client.get_file_status(file.id)
                    self.state.update_file(updated_file)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Грешка: {e}")

    async def on_file_picker_result(self, e: ft.FilePickerResultEvent):
        if not e.files:
            return

        for f in e.files:
            try:
                if f.path:
                    # Using asyncio.to_thread for reading file to avoid blocking
                    content = await asyncio.to_thread(Path(f.path).read_bytes)
                    await self.api_client.upload_file(content, f.name)
            except Exception as ex:  # noqa: BLE001
                logger.error(f"Грешка: {ex}")
                self.page.open(
                    ft.SnackBar(
                        ft.Text(f"Failed to upload {f.name}: {ex}", color=ft.colors.WHITE),
                        bgcolor=ft.colors.RED
                    )
                )

        # Refresh the list
        await self.initial_load()

    def update_table(self):
        self.data_table.rows.clear()
        for file in self.state.files:
            del_btn = ft.IconButton(
                icon=ft.icons.DELETE,
                tooltip="Delete",
                on_click=self.create_delete_handler(file.id)
            )
            category_dropdown = ft.Dropdown(
                value=file.category,
                options=[
                    ft.dropdown.Option("medicine", "Медицина"),
                    ft.dropdown.Option("economics", "Икономика"),
                    ft.dropdown.Option("programming", "Програмиране"),
                    ft.dropdown.Option("law", "Право"),
                    ft.dropdown.Option("general", "Общи"),
                ],
                width=150,
                dense=True,
                on_change=self.create_category_change_handler(file.id)
            )
            self.data_table.rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(file.file_name or "Unknown")),
                        ft.DataCell(ft.Text(file.status or "Unknown")),
                        ft.DataCell(category_dropdown),
                        ft.DataCell(del_btn),
                    ]
                )
            )
        self.update()
        self.page.update()

    def create_category_change_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            try:
                await self.api_client.update_file_category(file_id, e.control.value)
                await self.initial_load()
            except Exception as ex:  # noqa: BLE001
                logger.error(f"Грешка: {ex}")
                self.page.open(
                    ft.SnackBar(
                        ft.Text(f"Failed to update category: {ex}", color=ft.colors.WHITE),
                        bgcolor=ft.colors.RED
                    )
                )
        return handler

    def create_delete_handler(self, file_id: str | None):
        async def handler(e):
            if not file_id:
                return
            try:
                await self.api_client.delete_file(file_id)
                await self.initial_load()
            except Exception as ex:  # noqa: BLE001
                logger.error(f"Грешка: {ex}")
                self.page.open(
                    ft.SnackBar(
                        ft.Text(f"Failed to delete file: {ex}", color=ft.colors.WHITE),
                        bgcolor=ft.colors.RED
                    )
                )
        return handler
