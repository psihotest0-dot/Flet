import asyncio
import logging
from logging.handlers import RotatingFileHandler

import flet as ft

from api.client import APIClient
from state.settings import load_settings, save_settings
from views.chat import ChatView
from views.files import FilesView
from views.memory import MemoryView

logging.basicConfig(
    handlers=[RotatingFileHandler("app.log", maxBytes=1024 * 1024, backupCount=3)],
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def main(page: ft.Page):
    page.title = "AI Agent Client"

    settings = load_settings()

    base_url_field = ft.TextField(label="Base URL", value=settings.base_url)
    api_key_field = ft.TextField(label="API Key", value=settings.api_key or "", password=True, can_reveal_password=True)

    def close_dlg(e):
        page.close(settings_dialog)

    def save_dlg(e):
        settings.base_url = base_url_field.value
        settings.api_key = api_key_field.value or None
        save_settings(settings)
        api_client.base_url = settings.base_url
        page.close(settings_dialog)

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Settings"),
        content=ft.Column([base_url_field, api_key_field], tight=True),
        actions=[
            ft.TextButton("Cancel", on_click=close_dlg),
            ft.TextButton("Save", on_click=save_dlg),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def open_settings(e):
        page.open(settings_dialog)

    status_icon = ft.Icon(name=ft.icons.CIRCLE, color=ft.colors.RED, size=14, tooltip="Disconnected")

    page.appbar = ft.AppBar(
        title=ft.Row([ft.Text("AI Agent"), status_icon], alignment=ft.MainAxisAlignment.START),
        actions=[
            ft.IconButton(ft.icons.SETTINGS, on_click=open_settings)
        ]
    )

    api_client = APIClient(base_url=settings.base_url, user_id="123e4567-e89b-12d3-a456-426614174000")

    async def check_connection():
        while True:
            is_healthy = await api_client.check_health()
            if is_healthy:
                status_icon.color = ft.colors.GREEN
                status_icon.tooltip = "Connected"
            else:
                status_icon.color = ft.colors.RED
                status_icon.tooltip = "Disconnected"
            page.update()
            await asyncio.sleep(5)

    page.run_task(check_connection)

    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(
                text="Chat",
                content=ChatView(page, api_client)
            ),
            ft.Tab(
                text="Memory",
                content=MemoryView(page, api_client)
            ),
            ft.Tab(
                text="Files",
                content=FilesView(page, api_client)
            ),
        ],
        expand=1,
    )

    page.add(t)

if __name__ == "__main__":
    ft.app(target=main)
