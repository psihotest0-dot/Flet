import flet as ft

from state.settings import load_settings, save_settings


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

    page.appbar = ft.AppBar(
        title=ft.Text("AI Agent"),
        actions=[
            ft.IconButton(ft.icons.SETTINGS, on_click=open_settings)
        ]
    )

    t = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(
                text="Chat",
                content=ft.Container(
                    content=ft.Text("Chat Tab content"), alignment=ft.alignment.center
                ),
            ),
            ft.Tab(
                text="Memory",
                content=ft.Container(
                    content=ft.Text("Memory Tab content"), alignment=ft.alignment.center
                ),
            ),
            ft.Tab(
                text="Files",
                content=ft.Container(
                    content=ft.Text("Files Tab content"), alignment=ft.alignment.center
                ),
            ),
        ],
        expand=1,
    )

    page.add(t)

if __name__ == "__main__":
    ft.app(target=main)
