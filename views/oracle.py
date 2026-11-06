"""The "Бот" tab: the journal of what the trading bot asked the Oracle.

The bot asks the backend for a verdict on every signal (``APPROVE``, ``VETO`` or
``HOLD``) and the backend writes the question and the answer to a journal. This
tab only reads that journal: it neither calls the bot nor the model. In shadow
mode the bot ignores the verdicts, so the journal is the whole result.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import flet as ft

from api.client import APIClient, OracleDecision, OracleDecisionPage
from views.ui_utils import error_detail, safe_update

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
# How often the newest decisions are fetched while the tab is visible.
POLL_INTERVAL = 10.0
# The backend's own pattern for a stock symbol (internal/oracle/request.go); a
# symbol that does not match would only come back as a 400.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,14}$")
REASON_WIDTH = 340

STATUS_FAILED = "failed"

# Verdict -> (label, colour, what it means).
VERDICTS = {
    "APPROVE": ("APPROVE", ft.colors.GREEN, "Моделът би пуснал сделката."),
    "VETO": ("VETO", ft.colors.RED_400, "Моделът би спрял сделката."),
    "HOLD": (
        "HOLD",
        ft.colors.AMBER,
        "Моделът не дава окончателен отговор; ботът пита пак на следващия такт.",
    ),
}
FAILED_VERDICT = (
    "Неуспех",
    ft.colors.ORANGE,
    "Няма вердикт: заявката към модела не е завършила с годен отговор.",
)

MODES = {
    "shadow": ("сенчест", "Ботът само записва отговора и не го следва."),
    "active": ("активен", "Ботът следва отговора на Oracle."),
}

LENGTH_FINISH_REASON = "length"


def verdict_cell(decision: OracleDecision) -> tuple[str, str, str]:
    """``(label, colour, tooltip)`` of the verdict column.

    A failed question has no verdict at all; the label says so instead of
    leaving the cell blank. A word that is not one of the three is shown as it
    came, so a verdict added to the backend later is not hidden.
    """
    if decision.status == STATUS_FAILED:
        return FAILED_VERDICT
    verdict = (decision.verdict or "").strip()
    if verdict in VERDICTS:
        return VERDICTS[verdict]
    return verdict or "—", ft.colors.GREY, "Непознат вердикт."


def mode_cell(mode: str | None) -> tuple[str, str]:
    """``(label, tooltip)`` of the mode column; an unknown mode is shown as it came."""
    key = (mode or "").strip()
    if key in MODES:
        return MODES[key]
    return key, ""


def format_time(value: datetime | None) -> str:
    """The time of a decision in the reader's own time zone."""
    if value is None:
        return ""
    return value.astimezone().strftime("%d.%m %H:%M:%S")


def format_latency(milliseconds: int | None) -> str:
    if milliseconds is None:
        return ""
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    return f"{milliseconds / 1000:.1f} s"


def reason_text(decision: OracleDecision) -> str:
    """What the reason column says: the model's reason, or why there is no verdict."""
    if decision.status == STATUS_FAILED:
        return decision.error or "Неизвестна грешка"
    return decision.reason or ""


def decision_details(decision: OracleDecision) -> str:
    """The tooltip of a row: the ids to find it in the logs, and how it was made."""
    lines = []
    if decision.signal_id:
        lines.append(f"Сигнал: {decision.signal_id}")
    if decision.request_id:
        lines.append(f"Заявка: {decision.request_id}")
    if decision.model:
        lines.append(f"Модел: {decision.model}")
    if decision.attempts:
        lines.append(f"Опити: {decision.attempts}")
    if decision.prompt_tokens is not None or decision.completion_tokens is not None:
        lines.append(
            f"Токени: {decision.prompt_tokens or 0} вход, "
            f"{decision.completion_tokens or 0} изход"
        )
    if decision.finish_reason:
        cut = " (отговорът е отрязан)" if decision.finish_reason == LENGTH_FINISH_REASON else ""
        lines.append(f"Край на отговора: {decision.finish_reason}{cut}")
    if decision.status == STATUS_FAILED and decision.error:
        lines.append(f"Грешка: {decision.error}")
    return "\n".join(lines)


def summary_line(decisions: list[OracleDecision]) -> str:
    """How the listed decisions split by verdict - only what is loaded, not the whole journal."""
    if not decisions:
        return ""
    failed = sum(1 for d in decisions if d.status == STATUS_FAILED)
    counts = {
        verdict: sum(
            1 for d in decisions if d.status != STATUS_FAILED and d.verdict == verdict
        )
        for verdict in VERDICTS
    }
    parts = [f"В списъка: {len(decisions)}"]
    parts += [f"{verdict} {count}" for verdict, count in counts.items()]
    parts.append(f"неуспешни {failed}")
    return " · ".join(parts)


class OracleView(ft.Container):
    """The journal of the Oracle: a filterable table, newest decision first."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.expand = True
        self.alignment = ft.alignment.top_center

        # Newest first, like the backend sends them.
        self.decisions: list[OracleDecision] = []
        self._next_before_id = 0
        self._symbol: str | None = None
        # Raised by every full reload. A response that started before a reload
        # (another symbol, the button) belongs to a list that is gone.
        self._generation = 0
        self._loading_more = False
        self._polling = False
        self._active = False
        self._poll_failed = False
        self._load_task = None
        self._poll_task = None

        self.symbol_field = ft.TextField(
            label="Символ",
            hint_text="напр. AAPL",
            width=150,
            dense=True,
            capitalization=ft.TextCapitalization.CHARACTERS,
            on_submit=self.on_filter_submit,
        )
        self.filter_btn = ft.IconButton(
            icon=ft.icons.SEARCH,
            tooltip="Покажи само този символ (празно = всички)",
            on_click=self.on_filter_submit,
        )
        self.refresh_btn = ft.ElevatedButton(
            "Обнови",
            icon=ft.icons.REFRESH,
            on_click=self.on_refresh_click,
        )
        self.progress = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

        self.intro_text = ft.Text(
            "Решенията на Oracle за сигналите на трейдинг бота. "
            "В сенчест режим ботът само записва отговорите и не ги следва.",
            size=12,
            color=ft.colors.GREY_400,
        )
        self.summary_text = ft.Text("", size=12)
        self.updated_text = ft.Text("", size=12, color=ft.colors.GREY_400)
        self.error_text = ft.Text("", size=12, color=ft.colors.RED_300, visible=False)
        self.empty_text = ft.Text("", color=ft.colors.GREY_400, visible=False)

        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Време")),
                ft.DataColumn(ft.Text("Символ")),
                ft.DataColumn(ft.Text("Вердикт")),
                ft.DataColumn(ft.Text("Причина")),
                ft.DataColumn(ft.Text("Режим")),
                ft.DataColumn(ft.Text("Отговор след")),
            ],
            rows=[],
        )
        self.more_btn = ft.TextButton(
            "Покажи по-стари",
            icon=ft.icons.EXPAND_MORE,
            visible=False,
            on_click=self.on_more_click,
        )

        self.content = ft.Column(
            controls=[
                ft.Row(
                    [self.symbol_field, self.filter_btn, self.refresh_btn, self.progress],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.intro_text,
                ft.Row([self.summary_text, self.updated_text], wrap=True, spacing=16),
                self.error_text,
                ft.Column(
                    [
                        ft.Row([self.data_table], scroll=ft.ScrollMode.ADAPTIVE),
                        self.empty_text,
                        self.more_btn,
                    ],
                    scroll=ft.ScrollMode.ADAPTIVE,
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

    # -------------------------------------------------------------- lifecycle
    def did_mount(self):
        if self._load_task is None:
            self._load_task = self.page.run_task(self.load)
        if self._active:
            self.set_active(True)

    def set_active(self, active: bool):
        """Fetch new decisions only while this tab is the visible one."""
        self._active = active
        if active:
            if self._poll_task is None and self.page is not None:
                self._polling = True
                self._poll_task = self.page.run_task(self.poll)
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

    # ----------------------------------------------------------------- events
    async def on_refresh_click(self, e):
        await self.load()

    async def on_filter_submit(self, e):
        symbol = (self.symbol_field.value or "").strip().upper()
        if symbol and not SYMBOL_PATTERN.match(symbol):
            self.symbol_field.error_text = "Невалиден символ (напр. AAPL, BRK.B)"
            safe_update(self, self.symbol_field)
            return
        self.symbol_field.error_text = None
        self.symbol_field.value = symbol
        safe_update(self, self.symbol_field)
        self._symbol = symbol or None
        await self.load()

    async def on_more_click(self, e):
        await self.load_more()

    # ------------------------------------------------------------------- data
    async def load(self, *_args):
        """Read the newest page again for the current symbol and start the list over."""
        self._generation += 1
        generation = self._generation
        self._loading_more = False
        self._set_loading(True)
        try:
            page = await self.api_client.list_oracle_decisions(
                limit=PAGE_SIZE, symbol=self._symbol
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            if generation == self._generation:
                logger.error("Failed to load the Oracle journal: %s", ex)
                self._set_error(f"Грешка при зареждане на журнала: {error_detail(ex)}")
            return
        finally:
            if generation == self._generation:
                self._set_loading(False)

        if generation != self._generation:
            return
        self._show_first_page(page)

    async def load_more(self, *_args):
        """Add the page of older decisions after the ones on screen."""
        if self._loading_more or not self._next_before_id:
            return
        generation = self._generation
        self._loading_more = True
        self.more_btn.disabled = True
        safe_update(self, self.more_btn)
        try:
            page = await self.api_client.list_oracle_decisions(
                limit=PAGE_SIZE, before_id=self._next_before_id, symbol=self._symbol
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            if generation == self._generation:
                logger.error("Failed to load older Oracle decisions: %s", ex)
                self._set_error(f"Грешка при зареждане на журнала: {error_detail(ex)}")
            return
        finally:
            if generation == self._generation:
                self._loading_more = False
                self.more_btn.disabled = False
                safe_update(self, self.more_btn)

        if generation != self._generation:
            return
        known = {decision.id for decision in self.decisions}
        self.decisions.extend(d for d in page.decisions if d.id not in known)
        self._next_before_id = page.next_before_id
        self._show_loaded()

    async def poll(self, *_args):
        while self._polling:
            await asyncio.sleep(POLL_INTERVAL)
            if not self._polling:
                break
            try:
                await self.refresh_newest()
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                # One line when it starts to fail, not one per poll while the
                # backend is down; the tab itself says that it is stale.
                if not self._poll_failed:
                    logger.warning("Could not refresh the Oracle journal: %s", ex)
                self._poll_failed = True
                self._set_error(f"Журналът не може да се обнови: {error_detail(ex)}")
            else:
                self._poll_failed = False

    async def refresh_newest(self):
        """Put the decisions that arrived since the last look on top of the list.

        Older pages the reader has opened stay where they are. If a whole page of
        new decisions arrived, the list would have a hole between them and the
        old ones, so it starts over from the newest page instead.
        """
        generation = self._generation
        page = await self.api_client.list_oracle_decisions(
            limit=PAGE_SIZE, symbol=self._symbol
        )
        if generation != self._generation:
            return

        newest = self.decisions[0].id if self.decisions else 0
        fresh = [d for d in page.decisions if d.id > newest]
        if not fresh:
            # Nothing new, but the backend answered: the tab is not stale any more.
            self._set_error(None)
            self._touch()
            return

        if not self.decisions or (
            len(fresh) == len(page.decisions) and page.next_before_id
        ):
            self._show_first_page(page)
            return

        self.decisions = fresh + self.decisions
        self._show_loaded()

    # ----------------------------------------------------------------- screen
    def _show_first_page(self, page: OracleDecisionPage) -> None:
        self.decisions = list(page.decisions)
        self._next_before_id = page.next_before_id
        self._show_loaded()

    def _show_loaded(self) -> None:
        """Draw ``self.decisions`` and everything around the table."""
        self._poll_failed = False
        self._set_error(None)
        self.update_table()
        self.more_btn.visible = bool(self._next_before_id)
        self.summary_text.value = summary_line(self.decisions)
        if self.decisions:
            self.empty_text.visible = False
        else:
            self.empty_text.value = (
                f"Няма решения за {self._symbol}."
                if self._symbol
                else "Още няма решения. Журналът се пълни, когато ботът пита Oracle."
            )
            self.empty_text.visible = True
        self._touch(
            self.more_btn, self.summary_text, self.empty_text, self.data_table
        )

    def _touch(self, *dirty: ft.Control) -> None:
        self.updated_text.value = f"Обновено в {datetime.now():%H:%M:%S}"
        safe_update(self, self.updated_text, *dirty)

    def _set_loading(self, loading: bool) -> None:
        self.progress.visible = loading
        safe_update(self, self.progress)

    def _set_error(self, message: str | None) -> None:
        self.error_text.value = message or ""
        self.error_text.visible = bool(message)
        safe_update(self, self.error_text)

    def update_table(self) -> None:
        self.data_table.rows = [self._row(decision) for decision in self.decisions]

    def _row(self, decision: OracleDecision) -> ft.DataRow:
        label, color, meaning = verdict_cell(decision)
        details = decision_details(decision)
        verdict = ft.Text(
            label,
            color=color,
            weight=ft.FontWeight.BOLD,
            tooltip=f"{meaning}\n{details}" if details else meaning,
        )
        reason = reason_text(decision)
        reason_cell = ft.Text(
            reason,
            width=REASON_WIDTH,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
            color=ft.colors.ORANGE_200 if decision.status == STATUS_FAILED else None,
            tooltip=reason or None,
        )
        mode_label, mode_meaning = mode_cell(decision.mode)
        return ft.DataRow(
            cells=[
                ft.DataCell(ft.Text(format_time(decision.created_at))),
                ft.DataCell(ft.Text(decision.symbol or "")),
                ft.DataCell(verdict),
                ft.DataCell(reason_cell),
                ft.DataCell(ft.Text(mode_label, tooltip=mode_meaning or None)),
                ft.DataCell(ft.Text(format_latency(decision.latency_ms))),
            ]
        )
