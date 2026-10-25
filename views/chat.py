from __future__ import annotations

"""Chat view components for interacting with the AI agent."""

import logging
import time
import uuid

import flet as ft

from api.client import APIClient
from state.settings import load_settings, save_settings

logger = logging.getLogger(__name__)

DEBUG_MOCK_SOURCES = False


def _get_mock_sources():
    return [
        {"title": "guidelines.pdf", "type": "Документ", "score": "95", "text_snippet": "Това е първият пример за намерен текст от документа guidelines.pdf."},
        {"title": "receipt_123.jpg", "type": "OCR", "score": "82", "text_snippet": "Извлечен текст от касова бележка. Сума: 12.50 BGN"},
        {"title": "manual.docx", "type": "Документ", "score": "78", "text_snippet": "Важно упътване от файла manual.docx относно инсталацията."},
        {"title": "notes.txt", "type": "Документ", "score": "60", "text_snippet": "Някакви случайни записки, които са намерени от RAG системата."},
        {"title": "screenshot.png", "type": "OCR", "score": "45", "text_snippet": "Текст от екранна снимка на браузър."},
    ]


class ChatMessage(ft.Column):
    """A component representing a single chat message in the UI."""
    def __init__(self, role: str, text: str, sources: list | None = None, page: ft.Page | None = None):
        super().__init__()
        self.role = role
        self.text_value = text
        self.sources = sources or []
        self.page_ref = page
        self.spacing = 2
        self.horizontal_alignment = ft.CrossAxisAlignment.END if role == "user" else ft.CrossAxisAlignment.START

        self.text_color = ft.colors.LIGHT_BLUE_200 if role == "user" else ft.colors.PINK_200
        self.sender_name = "Ивайло" if role == "user" else "Лили"
        self.bgcolor = ft.colors.BLUE_GREY_800 if role == "user" else ft.colors.BLUE_GREY_900

        self.md = ft.Markdown(
            value=self.text_value,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="atom-one-dark",
            md_style_sheet=ft.MarkdownStyleSheet(
                p_text_style=ft.TextStyle(color=self.text_color),
            )
        )

        self.msg_container = ft.Container(
            content=self.md,
            padding=10,
            border_radius=10,
            bgcolor=self.bgcolor,
        )

        self.sender_label = ft.Text(
            value=self.sender_name,
            size=12,
            color=ft.colors.GREY_500,
        )

        top_controls = [self.sender_label]
        if role != "user":
            self.copy_btn = ft.IconButton(
                icon=ft.icons.COPY,
                icon_size=14,
                tooltip="Копирай",
                on_click=self.copy_to_clipboard,
            )
            top_controls.append(self.copy_btn)

        self.top_row = ft.Row(
            controls=top_controls,
            alignment=ft.MainAxisAlignment.START,
        )

        self.sources_row = ft.Row(wrap=True, visible=False)
        self.bottom_sheet = None

        self.controls = [
            self.top_row,
            self.msg_container,
            self.sources_row
        ]

        if self.sources:
            self.add_sources(self.sources)

    async def copy_to_clipboard(self, e):
        if self.page_ref:
            self.page_ref.set_clipboard(self.md.value)
            self.page_ref.open(
                ft.SnackBar(
                    content=ft.Text("Текстът е копиран!"),
                    duration=2000,
                )
            )

    def append_text(self, text: str):
        self.md.value += text
        if self.md.uid is not None:
            self.md.update()

    def add_sources(self, sources: list):
        if not sources:
            return

        self.sources.extend(sources)
        self.sources_row.controls.clear()

        self.sources_row.controls.append(ft.Text("Източници:", size=12, color=ft.colors.GREY_400))

        # Group sources by filename
        grouped_sources = {}
        for src in self.sources:
            title = src.get("title", src.get("file_name", "Unknown"))
            if title not in grouped_sources:
                grouped_sources[title] = []
            grouped_sources[title].append(src)

        def create_chip_callback(file_sources):
            return lambda e: self.open_sources_panel(file_sources)

        for title, file_sources in grouped_sources.items():
            count = len(file_sources)
            label_text = f"{title} ({count})" if count > 1 else title
            short_label = (label_text[:20] + "..") if len(label_text) > 20 else label_text

            chip = ft.Chip(
                label=ft.Text(short_label, size=12),
                leading=ft.Icon(ft.icons.INSERT_DRIVE_FILE, size=14),
                on_click=create_chip_callback(file_sources)
            )
            self.sources_row.controls.append(chip)

        self.sources_row.visible = True
        if self.uid is not None:
            self.update()

    def open_sources_panel(self, sources_to_display: list | None = None):
        if not self.page_ref:
            return

        display_list = sources_to_display if sources_to_display is not None else self.sources

        list_view = ft.ListView(expand=True, spacing=10, padding=20)

        list_view.controls.append(ft.Text("Източници (RAG)", size=20, weight=ft.FontWeight.BOLD))

        for src in display_list:
            title = src.get("title", src.get("file_name", "Unknown"))
            src_type = src.get("type", src.get("source_type", "Документ"))
            score = src.get("score", "N/A")
            snippet = src.get("content_snippet", "")

            # If score is a number, format it, otherwise leave as is
            try:
                score_float = float(score)
                # Ensure it looks like a percentage if it's 0-1 or 0-100
                if score_float <= 1.0:
                    score_str = f"{score_float*100:.1f}%"
                else:
                    score_str = f"{score_float:.1f}%"
            except (ValueError, TypeError) as e:
                logger.error(f"Failed to convert score '{score}' to float: {e}")
                score_str = str(score)

            item = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(title, weight=ft.FontWeight.BOLD, size=16),
                        ft.Text(f"• {src_type} • Сходство: {score_str}", size=12, color=ft.colors.GREY_400)
                    ]),
                    ft.Text(snippet, selectable=True, size=14, color=ft.colors.GREY_300)
                ]),
                bgcolor=ft.colors.BLUE_GREY_900,
                padding=15,
                border_radius=10,
                border=ft.border.all(1, ft.colors.BLUE_GREY_700)
            )
            list_view.controls.append(item)

        if self.bottom_sheet is not None:
            self.page_ref.close(self.bottom_sheet)

        self.bottom_sheet = ft.BottomSheet(
            content=ft.Container(
                content=list_view,
                padding=10,
                height=400,
            ),
            open=True
        )
        self.page_ref.open(self.bottom_sheet)


class ChatView(ft.Container):
    """The main view for the chat interface."""
    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client

        settings = load_settings()
        if settings.session_id is None:
            settings.session_id = str(uuid.uuid4())
            save_settings(settings)
        self.session_id = settings.session_id

        self._stop_generation = False

        self.expand = True

        self.chat_list = ft.ListView(
            expand=True,
            spacing=10,
            auto_scroll=True,
            padding=ft.padding.only(left=20, right=20),
        )

        self.new_chat_btn = ft.IconButton(
            icon=ft.icons.ADD_COMMENT,
            tooltip="Нов чат",
            on_click=self.on_new_chat_click,
        )

        self.top_row = ft.Row(
            controls=[self.new_chat_btn],
            alignment=ft.MainAxisAlignment.END,
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
                self.top_row,
                self.chat_list,
                input_row,
            ],
            expand=True,
        )

    async def on_new_chat_click(self, e):
        self.chat_list.controls.clear()

        settings = load_settings()
        new_session = str(uuid.uuid4())
        settings.session_id = new_session
        save_settings(settings)

        self.session_id = new_session
        if self.uid is not None:
            self.update()


    def scroll_to_bottom(self):
        if self.chat_list.uid is not None:
            self.chat_list.scroll_to(offset=-1, duration=100)

    def did_mount(self):
        if DEBUG_MOCK_SOURCES:
            self.add_message("assistant", "Здравейте! Аз съм Лили. Как мога да ви помогна днес?", sources=_get_mock_sources())
        self._load_task = self.page.run_task(self.load_history)

    def will_unmount(self):
        if hasattr(self, "_load_task") and self._load_task:
            self._load_task.cancel()

    async def load_history(self, *_args):
        try:
            messages = await self.api_client.get_messages(session_id=self.session_id)
            self.chat_list.controls.clear()
            for msg in messages:
                role = msg.role or "user"
                content = msg.content or ""
                self.add_message(role, content)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to load chat history: {e}")

    def add_message(self, role: str, text: str, sources: list | None = None) -> ChatMessage:
        is_user = role == "user"

        chat_message = ChatMessage(role=role, text=text, sources=sources, page=self.page)

        container_wrapper = ft.Container(
            content=chat_message,
            margin=ft.margin.only(left=50 if is_user else 0, right=0 if is_user else 50),
            expand=True,
            alignment=ft.alignment.top_right if is_user else ft.alignment.top_left,
        )

        row = ft.Row(
            controls=[container_wrapper],
            alignment=ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START,
        )

        self.chat_list.controls.append(row)
        if self.uid is not None:
            self.update()
            self.scroll_to_bottom()
        return chat_message

    async def send_message_click(self, e):
        if not self.input_field.value:
            return

        user_text = self.input_field.value
        self.input_field.value = ""
        self.input_field.focus()

        self.send_btn.visible = False
        self.stop_btn.visible = True
        if self.uid is not None:
            self.update()

        self.add_message("user", user_text)

        chat_message = self.add_message("assistant", "")
        self._stop_generation = False

        try:
            last_update_time = time.time()
            pending_text = ""

            async for chunk in self.api_client.stream_chat(session_id=self.session_id, content=user_text):
                if self._stop_generation:
                    break

                if isinstance(chunk, dict):
                    if chunk.get("type") == "sources":
                        data = chunk.get("data", {})
                        if isinstance(data, list):
                            chat_message.add_sources(data)
                        elif isinstance(data, dict):
                            for key in ("facts", "chunks", "sources"):
                                if key in data and isinstance(data[key], list):
                                    chat_message.add_sources(data[key])
                        self.scroll_to_bottom()
                        continue

                    if "text" in chunk:
                        pending_text += chunk["text"]

                    if "content" in chunk:
                        pending_text += chunk["content"]

                    if "error" in chunk:
                        pending_text += f"\n\n**Error:** {chunk['error']}"

                    # Handle legacy sources in payload just in case
                    for source_key in ("facts", "chunks", "sources"):
                        if source_key in chunk and isinstance(chunk[source_key], list):
                            chat_message.add_sources(chunk[source_key])

                elif isinstance(chunk, str):
                    pending_text += chunk

                current_time = time.time()
                if current_time - last_update_time >= 0.05:
                    if pending_text:
                        chat_message.append_text(pending_text)
                        pending_text = ""
                    if self.uid is not None:
                        self.update()
                        self.scroll_to_bottom()
                    last_update_time = current_time

            # Final flush
            if pending_text:
                chat_message.append_text(pending_text)
                if self.uid is not None:
                    self.update()
                    self.scroll_to_bottom()

        except Exception as ex:  # noqa: BLE001
            logger.error(f"Грешка: {ex}")
            chat_message.append_text("\n\n**Error getting response.**")
            self.input_field.value = user_text
            self.page.open(
                ft.SnackBar(
                    ft.Text(f"Failed to stream response: {ex}", color=ft.colors.WHITE),
                    bgcolor=ft.colors.RED
                )
            )

        if self.uid is not None:
            self.update()

        self.send_btn.visible = True
        self.stop_btn.visible = False
        if self.uid is not None:
            self.update()

    async def stop_generation_click(self, e):
        self._stop_generation = True
        self.stop_btn.visible = False
        self.send_btn.visible = True
        if self.uid is not None:
            self.update()
