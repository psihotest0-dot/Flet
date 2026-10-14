from __future__ import annotations

import logging
import uuid

import flet as ft

from api.client import APIClient

logger = logging.getLogger(__name__)


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

    def did_mount(self):
        super().did_mount()
        _md, s_row = self.add_message("assistant", "Здравейте! Аз съм Лили. Как мога да ви помогна днес?")
        mock_sources = [
            {"source_type": "Уикипедия", "text_snippet": "Това е примерен текст от Уикипедия...", "score": 0.99},
            {"source_type": "PDF Документ", "text_snippet": "Това е примерен текст от PDF...", "score": 0.85},
            {"source_type": "Уебсайт", "text_snippet": "Това е примерен текст от уебсайт...", "score": 0.75},
            {"source_type": "Блог", "text_snippet": "Допълнителен източник...", "score": 0.50}
        ]
        self.update_sources_ui(s_row, mock_sources)

    def show_sources_drawer(self, sources: list):
        list_view = ft.ListView(spacing=10, padding=20, expand=True)
        for src in sources:
            src_type = src.get("source_type", "Unknown")
            src_snippet = src.get("text_snippet", "")
            src_score = src.get("score", "N/A")

            list_view.controls.append(
                ft.Column(
                    controls=[
                        ft.Text(f"{src_type} (Score: {src_score})", size=16, weight=ft.FontWeight.BOLD),
                        ft.Text("Документ", size=12, color=ft.colors.GREY_400),
                        ft.Text(src_snippet, size=14, selectable=True),
                        ft.Divider()
                    ],
                    spacing=5
                )
            )

        drawer = ft.NavigationDrawer(
            position=ft.NavigationDrawerPosition.END,
            controls=[
                ft.Container(
                    content=ft.Text("Източници", size=20, weight=ft.FontWeight.BOLD),
                    padding=ft.padding.only(top=20, left=20, bottom=10)
                ),
                list_view
            ]
        )
        self.page.open(drawer)

    def update_sources_ui(self, sources_row: ft.Row, sources: list):
        sources_row.controls.clear()

        if not sources:
            sources_row.visible = False
            sources_row.update()
            return

        def open_drawer_click(e):
            self.show_sources_drawer(sources)

        sources_row.controls.append(ft.Text("Източници:", size=12, color=ft.colors.GREY_500))

        for i, src in enumerate(sources[:3]):
            src_type = src.get("source_type", "Unknown")
            # We can use a Chip or Container with an icon
            chip = ft.Chip(
                label=ft.Text(src_type, size=12),
                leading=ft.Icon(ft.icons.INSERT_DRIVE_FILE, size=14),
                on_click=open_drawer_click
            )
            sources_row.controls.append(chip)

        if len(sources) > 3:
            more_text = ft.Text(f"+{len(sources) - 3}", size=12, color=ft.colors.BLUE_200)
            # Wrap in Container for clickability
            more_btn = ft.Container(
                content=more_text,
                on_click=open_drawer_click,
                padding=ft.padding.all(5)
            )
            sources_row.controls.append(more_btn)

        sources_row.visible = True
        sources_row.update()

    def add_message(self, role: str, text: str):
        is_user = role == "user"

        text_color = ft.colors.LIGHT_BLUE_200 if is_user else ft.colors.PINK_200
        sender_name = "Ивайло" if is_user else "Лили"

        # We need text to wrap
        md = ft.Markdown(
            value=text,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="atom-one-dark",
            md_style_sheet=ft.MarkdownStyleSheet(
                p_text_style=ft.TextStyle(color=text_color),
            )
        )

        # Put it in a container for styling
        msg_container = ft.Container(
            content=md,
            padding=10,
            border_radius=10,
            bgcolor=ft.colors.BLUE_GREY_800 if is_user else ft.colors.BLUE_GREY_900,
        )

        sender_label = ft.Text(
            value=sender_name,
            size=12,
            color=ft.colors.GREY_500,
        )

        message_column = ft.Column(
            controls=[sender_label, msg_container],
            spacing=2,
            horizontal_alignment=ft.CrossAxisAlignment.END if is_user else ft.CrossAxisAlignment.START,
        )

        container_wrapper = ft.Container(
            content=message_column,
            margin=ft.margin.only(left=50 if is_user else 0, right=0 if is_user else 50),
        )


        sources_row = None
        if not is_user:
            sources_row = ft.Row(
                controls=[],
                visible=False,
                wrap=True,
                alignment=ft.MainAxisAlignment.START,
            )
            # Wrap in a column to put the sources below the message
            column = ft.Column(
                controls=[container_wrapper, sources_row],
                spacing=5,
            )
            row = ft.Row(
                controls=[column],
                alignment=ft.MainAxisAlignment.START,
            )
        else:
            row = ft.Row(
                controls=[container_wrapper],
                alignment=ft.MainAxisAlignment.END,
            )

        self.chat_list.controls.append(row)
        self.update()
        return md, sources_row

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

        md_control, sources_row = self.add_message("assistant", "")
        self._stop_generation = False
        accumulated_sources = []

        try:
            async for chunk in self.api_client.stream_chat(session_id=self.session_id, content=user_text):
                if self._stop_generation:
                    break

                if isinstance(chunk, dict):
                    if "text" in chunk:
                        md_control.value += chunk["text"]
                        md_control.update()

                    if "content" in chunk:
                        md_control.value += chunk["content"]
                        md_control.update()

                    if "error" in chunk:
                        md_control.value += f"\n\n**Error:** {chunk['error']}"
                        md_control.update()

                    has_new_source = False
                    for source_key in ("facts", "chunks"):
                        if source_key in chunk and isinstance(chunk[source_key], list):
                            for src in chunk[source_key]:
                                if isinstance(src, dict):
                                    accumulated_sources.append(src)
                                    has_new_source = True

                    if has_new_source:
                        self.update_sources_ui(sources_row, accumulated_sources)

                elif isinstance(chunk, str):
                    md_control.value += chunk
                    md_control.update()

                self.update()

        except Exception as ex:  # noqa: BLE001
            logger.error(f"Грешка: {ex}")
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
