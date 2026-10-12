from __future__ import annotations

import time
import uuid

import flet as ft

from api.client import APIClient


class ChatView(ft.Container):
    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.session_id = str(uuid.uuid4())
        self._stop_generation = False

        self.expand = True

        self.chat_list = ft.ListView(
            expand=True,
            spacing=10,
            auto_scroll=True,
        )

        self.input_field = ft.TextField(
            hint_text="Type a message...",
            autofocus=True,
            shift_enter=True,
            min_lines=1,
            max_lines=5,
            expand=True,
            on_submit=self.send_message_click,
        )

        self.send_btn = ft.IconButton(
            icon=ft.icons.SEND,
            tooltip="Send",
            on_click=self.send_message_click,
        )

        self.stop_btn = ft.IconButton(
            icon=ft.icons.STOP_CIRCLE,
            icon_color=ft.colors.RED,
            tooltip="Stop",
            visible=False,
            on_click=self.stop_generation_click,
        )

        input_row = ft.Row(
            controls=[self.input_field, self.stop_btn, self.send_btn],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )

        self.content = ft.Column(
            controls=[
                self.chat_list,
                input_row,
            ],
            expand=True,
        )

    def add_message(self, role: str, text: str):
        is_user = role == "user"

        # We need text to wrap
        md = ft.Markdown(
            value=text,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="atom-one-dark",
        )

        # Put it in a container for styling
        msg_container = ft.Container(
            content=md,
            padding=10,
            border_radius=10,
            bgcolor=ft.colors.BLUE_GREY_800 if is_user else ft.colors.BLUE_GREY_900,
            margin=ft.margin.only(left=50 if is_user else 0, right=0 if is_user else 50),
        )

        expansion_tile = None
        if not is_user:
            expansion_tile = ft.ExpansionTile(
                title=ft.Text("Sources"),
                controls=[],
                visible=False,
            )
            # Wrap in a column to put the tile below the message
            column = ft.Column(
                controls=[msg_container, expansion_tile],
                spacing=5,
            )
            row = ft.Row(
                controls=[column],
                alignment=ft.MainAxisAlignment.START,
            )
        else:
            row = ft.Row(
                controls=[msg_container],
                alignment=ft.MainAxisAlignment.END,
            )

        self.chat_list.controls.append(row)
        self.update()
        return md, expansion_tile

    async def send_message_click(self, e):
        if not self.input_field.value:
            return

        user_text = self.input_field.value
        self.input_field.value = ""
        self.input_field.focus()

        self.send_btn.visible = False
        self.stop_btn.visible = True
        self.update()

        self.add_message("user", user_text)

        md_control, expansion_tile = self.add_message("assistant", "")
        self._stop_generation = False

        last_update_time = time.time()

        try:
            async for chunk in self.api_client.stream_chat(session_id=self.session_id, content=user_text):
                if self._stop_generation:
                    break

                if isinstance(chunk, dict):
                    if "text" in chunk:
                        md_control.value += chunk["text"]

                    has_new_source = False
                    for source_key in ("facts", "chunks"):
                        if source_key in chunk and isinstance(chunk[source_key], list):
                            for src in chunk[source_key]:
                                if isinstance(src, dict):
                                    src_type = src.get("source_type", "Unknown")
                                    src_snippet = src.get("text_snippet", "")
                                    src_score = src.get("score", "N/A")

                                    tile_title = f"{src_type} (Score: {src_score})"
                                    tile_subtitle = ft.Text(src_snippet, no_wrap=False, size=12, color=ft.colors.GREY_400)

                                    list_tile = ft.ListTile(
                                        title=ft.Text(tile_title, size=14, weight=ft.FontWeight.BOLD),
                                        subtitle=tile_subtitle,
                                    )
                                    expansion_tile.controls.append(list_tile)
                                    has_new_source = True

                    if has_new_source and not expansion_tile.visible:
                        expansion_tile.visible = True

                elif isinstance(chunk, str):
                    md_control.value += chunk

                # Batch updates every 50ms
                current_time = time.time()
                if current_time - last_update_time >= 0.05:
                    self.update()
                    last_update_time = current_time

        except Exception as ex:  # noqa: BLE001
            print(f"Error during streaming: {ex}")
            md_control.value += "\n\n**Error getting response.**"
            self.page.open(
                ft.SnackBar(
                    ft.Text(f"Failed to stream response: {ex}", color=ft.colors.WHITE),
                    bgcolor=ft.colors.RED
                )
            )

        self.update()

        self.send_btn.visible = True
        self.stop_btn.visible = False
        self.update()

    async def stop_generation_click(self, e):
        self._stop_generation = True
        self.stop_btn.visible = False
        self.send_btn.visible = True
        self.update()
