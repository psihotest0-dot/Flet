"""Memory view component for managing AI agent memory."""

from __future__ import annotations

import asyncio
import logging

import flet as ft
import httpx

from api.client import (
    DEFAULT_IMPORTANCE,
    MAX_CONTENT_BYTES,
    MAX_IMPORTANCE,
    MIN_IMPORTANCE,
    APIClient,
    APIError,
    content_size,
)
from views.proposals import ProposalCard
from views.ui_utils import (
    close_dialog,
    confirm_dialog,
    error_detail,
    open_dialog,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)

FILTER_ALL = "Всички"
FILTER_DEBOUNCE = 0.2
# Width of the proposal cards and the rule list above the table.
SECTION_WIDTH = 680

# Status of a memory the user types in by hand. It states a fact on purpose, so
# it takes effect right away instead of waiting as a "candidate" for activation.
NEW_MEMORY_STATUS = "active"
IMPORTANCE_OPTIONS = [str(value) for value in range(MIN_IMPORTANCE, MAX_IMPORTANCE + 1)]


def importance_or_default(value: int | None) -> int:
    """The importance to send for a memory.

    The backend answers 400 to anything outside 1-10, so a missing value (or a
    legacy 0) falls back to the default instead of being sent as is.
    """
    if value is not None and MIN_IMPORTANCE <= value <= MAX_IMPORTANCE:
        return value
    return DEFAULT_IMPORTANCE


def too_long_message(text: str) -> str | None:
    """Why ``text`` cannot be saved as a memory, or ``None`` when it fits.

    The backend measures bytes, not letters, and answers with "at most 32000
    characters" - which reads as if a Cyrillic text of 20000 letters were fine.
    """
    size = content_size(text)
    if size <= MAX_CONTENT_BYTES:
        return None
    return (
        f"Текстът е твърде дълъг: {size} от най-много {MAX_CONTENT_BYTES} байта "
        "(една кирилска буква е 2 байта). Съкрати го."
    )


def fact_key_tooltip(memory) -> str | None:
    """Tooltip naming the fact a memory belongs to (set by the consolidation job)."""
    fact_key = (memory.metadata or {}).get("fact_key")
    return f"fact_key: {fact_key}" if fact_key else None


def importance_dropdown(value: int | None, **kwargs) -> ft.Dropdown:
    return ft.Dropdown(
        options=[ft.DropdownOption(option) for option in IMPORTANCE_OPTIONS],
        value=str(importance_or_default(value)),
        dense=True,
        **kwargs,
    )


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

        self.content_text = ft.Text(
            memory.content or "", tooltip=fact_key_tooltip(memory)
        )
        self.content_text_container = ft.Container(
            content=self.content_text,
            expand=True,
            alignment=ft.Alignment.TOP_LEFT,
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
            alignment=ft.Alignment.TOP_LEFT,
        )

        self.importance_text = ft.Text(self._importance_label(memory))
        self.importance_dropdown = importance_dropdown(
            memory.importance, width=90, visible=False
        )

        self.edit_btn = ft.IconButton(
            icon=ft.Icons.EDIT, tooltip="Редактирай", on_click=self._edit
        )
        self.save_btn = ft.IconButton(
            icon=ft.Icons.SAVE, tooltip="Запази", visible=False, on_click=self._save
        )
        self.cancel_btn = ft.IconButton(
            icon=ft.Icons.CANCEL, tooltip="Отказ", visible=False, on_click=self._cancel
        )
        self.delete_btn = ft.IconButton(
            icon=ft.Icons.DELETE, tooltip="Изтрий", on_click=self._delete
        )
        self.activate_btn = ft.IconButton(
            icon=ft.Icons.CHECK,
            icon_color=ft.Colors.GREEN,
            tooltip="Активирай",
            visible=memory.status == "candidate",
            on_click=self._activate,
        )
        self.status_text = ft.Text(memory.status or "")

        self.actions_row = ft.Row(
            [
                self.activate_btn,
                self.edit_btn,
                self.save_btn,
                self.cancel_btn,
                self.delete_btn,
            ]
        )

        self.cells = [
            ft.DataCell(
                ft.Row(
                    [self.content_text_container, self.content_field_container],
                    expand=True,
                    scroll=ft.ScrollMode.AUTO,
                )
            ),
            ft.DataCell(ft.Row([self.importance_text, self.importance_dropdown])),
            ft.DataCell(self.status_text),
            ft.DataCell(self.actions_row),
        ]

    @staticmethod
    def _importance_label(memory) -> str:
        return "" if memory.importance is None else str(memory.importance)

    # ------------------------------------------------------------- edit state
    def _set_editing(self, editing: bool):
        self.content_text_container.visible = not editing
        self.content_field_container.visible = editing
        self.importance_text.visible = not editing
        self.importance_dropdown.visible = editing
        self.edit_btn.visible = not editing
        self.save_btn.visible = editing
        self.cancel_btn.visible = editing
        safe_update(
            self.view,
            self.content_text_container,
            self.content_field_container,
            self.importance_text,
            self.importance_dropdown,
            self.edit_btn,
            self.save_btn,
            self.cancel_btn,
        )

    def _reset_fields(self):
        """Put the editors back to what the server last returned."""
        self.content_field.value = self.memory.content or ""
        self.importance_dropdown.value = str(
            importance_or_default(self.memory.importance)
        )

    def _edit(self, e):
        self._reset_fields()
        self._set_editing(True)

    def _cancel(self, e):
        self._reset_fields()
        self._set_editing(False)

    def apply(self, memory):
        """Refresh the row from a server response without rebuilding the table."""
        self.memory = memory
        self.content_text.value = memory.content or ""
        self.content_text.tooltip = fact_key_tooltip(memory)
        self.importance_text.value = self._importance_label(memory)
        self._reset_fields()
        self.status_text.value = memory.status or ""
        self.activate_btn.visible = memory.status == "candidate"
        safe_update(
            self.view,
            self.content_text,
            self.importance_text,
            self.status_text,
            self.activate_btn,
        )

    async def _save(self, e):
        if self._saving or not self.memory.id:
            return

        new_content = (self.content_field.value or "").strip()
        new_importance = int(
            self.importance_dropdown.value
            or importance_or_default(self.memory.importance)
        )
        unchanged = new_content == (self.memory.content or "") and (
            new_importance == importance_or_default(self.memory.importance)
        )
        if not new_content or unchanged:
            # Nothing changed, or the user tried to save an empty memory.
            self._reset_fields()
            self._set_editing(False)
            return

        too_long = too_long_message(new_content)
        if too_long:
            # Stay in edit mode so the text can be shortened.
            show_snackbar(self.view.page_ref, too_long)
            return

        self._saving = True
        self.save_btn.disabled = True
        safe_update(self.view, self.save_btn)
        try:
            updated = await self.view.api_client.update_memory(
                memory_id=self.memory.id,
                content=new_content,
                status=self.memory.status,
                importance=new_importance,
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to update memory: %s", ex)
            show_snackbar(
                self.view.page_ref,
                f"Грешка при запазване на спомена: {error_detail(ex)}",
            )
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
                importance=importance_or_default(self.memory.importance),
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to activate memory: %s", ex)
            show_snackbar(
                self.view.page_ref,
                f"Грешка при активиране на спомена: {error_detail(ex)}",
            )
            return

        self.view.replace_memory(updated)
        self.apply(updated)
        show_snackbar(self.view.page_ref, "Споменът е активиран!", success=True)

    async def _delete(self, e):
        if not self.memory.id:
            return
        confirmed = await confirm_dialog(
            self.view.page_ref,
            "Изтриване на спомен",
            "Сигурни ли сте, че искате да изтриете този спомен?",
        )
        if not confirmed:
            return

        try:
            await self.view.api_client.delete_memory(self.memory.id)
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            # 404: the memory is gone already (deleted from another client). What the
            # user wanted is true, so the row goes too.
            if ex.status != 404:
                logger.error("Failed to delete memory: %s", ex)
                show_snackbar(
                    self.view.page_ref, f"Грешка при изтриване на спомена: {ex}"
                )
                return
            logger.info("Memory %s was already deleted on the backend.", self.memory.id)
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to delete memory: %s", ex)
            show_snackbar(
                self.view.page_ref,
                f"Грешка при изтриване на спомена: {error_detail(ex)}",
            )
            return

        self.view.remove_memory_row(self)


class MemoryView(ft.Container):
    """The main view for listing, editing, and deleting agent memories."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        # ``Control.page`` is read-only and raises until the view is on the page;
        # the page the app was started with is kept under its own name.
        self.page_ref = page
        self.api_client = api_client
        self.memories = []
        self.alignment = ft.Alignment.TOP_CENTER
        self.expand = True

        self.status_dropdown = ft.Dropdown(
            label="Филтър по статус",
            options=[
                ft.DropdownOption(FILTER_ALL),
                ft.DropdownOption("active"),
                ft.DropdownOption("candidate"),
                ft.DropdownOption("superseded"),
            ],
            value=FILTER_ALL,
            on_select=self.on_filter_change,
            width=200,
        )

        self.refresh_btn = ft.Button(
            "Обнови",
            icon=ft.Icons.REFRESH,
            on_click=self.on_refresh_click,
        )

        self.new_btn = ft.Button(
            "Нов спомен",
            icon=ft.Icons.ADD,
            on_click=self.on_new_click,
        )

        self.progress = ft.ProgressRing(
            width=16, height=16, stroke_width=2, visible=False
        )

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Съдържание")),
                ft.DataColumn(
                    ft.Text("Важност", tooltip="От 1 (дреболия) до 10 (критично)")
                ),
                ft.DataColumn(ft.Text("Статус")),
                ft.DataColumn(ft.Text("Действия")),
            ],
            rows=[],
        )

        self.empty_hint = ft.Text(
            "Няма намерени спомени.", color=ft.Colors.GREY_500, visible=False
        )

        # What the assistant proposed to remember and waits for the user's answer.
        self.proposals_column = ft.Column(spacing=4)
        self.proposals_section = ft.Column(
            [
                ft.Text("Чакат потвърждение", weight=ft.FontWeight.BOLD),
                self.proposals_column,
            ],
            width=SECTION_WIDTH,
            visible=False,
        )

        # Global rules: always in the prompt, so the user can see and delete them.
        self.rules_column = ft.Column(spacing=0)
        self.rules_tile = ft.ExpansionTile(
            title=ft.Text("Глобални правила"),
            controls=[self.rules_column],
            visible=False,
        )

        self.content = ft.Column(
            controls=[
                ft.Row(
                    [
                        self.status_dropdown,
                        self.refresh_btn,
                        self.new_btn,
                        self.progress,
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.proposals_section,
                ft.Container(content=self.rules_tile, width=SECTION_WIDTH),
                self.empty_hint,
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

        self.new_content_field = ft.TextField(
            label="Спомен",
            hint_text="Напр. Предпочитам кратки отговори.",
            multiline=True,
            min_lines=3,
            max_lines=8,
            autofocus=True,
        )
        self.new_importance = importance_dropdown(None, label="Важност", width=120)
        self.new_save_btn = ft.TextButton("Запази", on_click=self.on_new_save)
        self.new_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Нов спомен"),
            content=ft.Column(
                [self.new_content_field, self.new_importance],
                tight=True,
                width=480,
            ),
            actions=[
                ft.TextButton("Отказ", on_click=self.on_new_cancel),
                self.new_save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._creating = False

        self._filter_task = None
        self._load_task = None
        self._extras_task = None
        # Monotonic token: a response is applied only if it is still the newest
        # request, so a slow "all" request cannot overwrite a newer "active" one.
        self._load_seq = 0

    def did_mount(self):
        self._load_task = self.page_ref.run_task(self.load_memories)
        self._extras_task = self.page_ref.run_task(self.load_proposals_and_rules)

    def will_unmount(self):
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        if self._extras_task is not None:
            self._extras_task.cancel()
            self._extras_task = None
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
            show_snackbar(self.page_ref, "Неуспешно зареждане на спомените.")
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

        self._filter_task = self.page_ref.run_task(debounced_load)

    async def on_refresh_click(self, e):
        await self.load_memories()
        await self.load_proposals_and_rules()

    # ------------------------------------------- proposals and global rules
    async def load_proposals_and_rules(self, *_args):
        """Fill the two sections above the table.

        They are an addition to the table: a failure (an older backend without
        these endpoints, or no connection) is logged and leaves them as they
        were, instead of an error on top of the memories.
        """
        await self.load_proposals()
        await self.load_rules()

    async def load_proposals(self):
        try:
            proposals = await self.api_client.list_proposals()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning("Failed to load proposals: %s", ex)
            return

        self.proposals_column.controls = [
            ProposalCard(
                proposal,
                self.api_client,
                page=self.page_ref,
                on_decided=self._on_proposal_decided,
            )
            for proposal in proposals
        ]
        self.proposals_section.visible = bool(proposals)
        safe_update(self, self.proposals_section)

    def _on_proposal_decided(self, proposal, approved: bool):
        """A saved proposal changes the table or the rules; the card keeps its result."""
        if approved and self.page_ref is not None:
            self.page_ref.run_task(self._reload_after_approval)

    async def _reload_after_approval(self):
        await self.load_memories()
        await self.load_rules()

    async def load_rules(self):
        try:
            rules = await self.api_client.list_rules()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning("Failed to load global rules: %s", ex)
            return

        self.rules_column.controls = [self._rule_tile(rule) for rule in rules]
        self.rules_tile.title = ft.Text(f"Глобални правила ({len(rules)})")
        self.rules_tile.visible = bool(rules)
        safe_update(self, self.rules_tile)

    def _rule_tile(self, rule) -> ft.ListTile:
        return ft.ListTile(
            title=ft.Text(rule.key),
            subtitle=ft.Text(rule.value, selectable=True),
            trailing=ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                tooltip="Изтрий правилото",
                on_click=lambda e, rule=rule: self.page_ref.run_task(
                    self.delete_rule, rule
                ),
            ),
        )

    async def delete_rule(self, rule):
        confirmed = await confirm_dialog(
            self.page_ref,
            "Изтриване на правило",
            f"Да изтрия ли правилото „{rule.key}“? Лили вече няма да го спазва.",
        )
        if not confirmed:
            return

        try:
            await self.api_client.delete_rule(rule.id)
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            if ex.status != 404:  # 404: already gone, the reload below shows it
                logger.error("Failed to delete rule: %s", ex)
                show_snackbar(self.page_ref, f"Грешка при изтриване на правилото: {ex}")
                return
            logger.info("Rule %s was already deleted on the backend.", rule.id)
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to delete rule: %s", ex)
            show_snackbar(
                self.page_ref, f"Неуспешно изтриване на правилото: {error_detail(ex)}"
            )
            return

        show_snackbar(self.page_ref, "Правилото е изтрито.", success=True)
        await self.load_rules()

    # ------------------------------------------------------------ new memory
    def on_new_click(self, e):
        self.new_content_field.value = ""
        self.new_content_field.error = None
        self.new_importance.value = str(DEFAULT_IMPORTANCE)
        open_dialog(self.page_ref, self.new_dialog)

    def on_new_cancel(self, e):
        close_dialog(self.new_dialog)

    def _show_new_error(self, message: str):
        self.new_content_field.error = message
        safe_update(self.new_dialog, self.new_content_field)

    async def on_new_save(self, e):
        if self._creating:
            return
        content = (self.new_content_field.value or "").strip()
        if not content:
            self._show_new_error("Въведи текст на спомена.")
            return
        too_long = too_long_message(content)
        if too_long:
            self._show_new_error(too_long)
            return

        self._creating = True
        self.new_save_btn.disabled = True
        safe_update(self.new_dialog, self.new_save_btn)
        try:
            await self.api_client.create_memory(
                content=content,
                status=NEW_MEMORY_STATUS,
                importance=int(self.new_importance.value or DEFAULT_IMPORTANCE),
            )
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            logger.error("Failed to create memory: %s", ex)
            # 409: the backend already holds a memory with exactly this text.
            self._show_new_error(
                "Такъв спомен вече съществува." if ex.status == 409 else str(ex)
            )
            return
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to create memory: %s", ex)
            self._show_new_error(
                "Бекендът не е достъпен."
                if isinstance(ex, httpx.TransportError)
                else "Неуспешно запазване на спомена."
            )
            return
        finally:
            self._creating = False
            self.new_save_btn.disabled = False
            safe_update(self.new_dialog, self.new_save_btn)

        close_dialog(self.new_dialog)
        show_snackbar(self.page_ref, "Споменът е добавен!", success=True)
        # Re-read instead of inserting locally: the active status filter decides
        # whether the new memory belongs in the table at all.
        await self.load_memories()

    # ----------------------------------------------------------------- table
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
