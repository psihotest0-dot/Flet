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
from dataclasses import dataclass
from datetime import UTC, datetime

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

# The evaluation of the Oracle is pre-registered in the bot's repo
# (docs/oracle-provider-task.md, section 5): only shadow-mode signals from the
# day the Oracle was switched on count, probes do not, and there is no verdict
# before 30 APPROVE and 30 VETO with a final answer and 80 % availability. A new
# model or prompt starts a new period - change EVALUATION_START then, by hand.
EVALUATION_START = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
SHADOW_MODE = "shadow"
PROBE_VERSION = "oracle-probe"
FINAL_VERDICTS = ("APPROVE", "VETO")
APPROVE_TARGET = 30
VETO_TARGET = 30
AVAILABILITY_TARGET = 0.8
# The most the backend sends in one page; the progress block reads the journal
# from the newest row back to EVALUATION_START.
PROGRESS_PAGE_SIZE = 200
PROGRESS_LABEL_WIDTH = 90
PROGRESS_BAR_WIDTH = 260

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
    if decision.prompt_version:
        lines.append(f"Промпт: {decision.prompt_version}")
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


@dataclass(frozen=True)
class Progress:
    """Where the evaluation stands: counts only, never how a trade ended.

    ``asked`` is the number of signals the model was asked about, ``answered``
    those that got a final answer (``APPROVE`` or ``VETO``). ``probes_unmarked``
    is true while no row says what version of signal it was, so the probes of the
    first day cannot be told from real signals yet.
    """
    asked: int = 0
    answered: int = 0
    approve: int = 0
    veto: int = 0
    probes_unmarked: bool = False

    @property
    def availability(self) -> float | None:
        """Share of the signals that got a final answer; ``None`` before any signal."""
        return self.answered / self.asked if self.asked else None

    @property
    def reached(self) -> bool:
        """Whether every bar of the pre-registered minimum is met."""
        availability = self.availability
        return (
            self.approve >= APPROVE_TARGET
            and self.veto >= VETO_TARGET
            and availability is not None
            and availability >= AVAILABILITY_TARGET
        )


def count_progress(decisions) -> Progress:
    """Count the journal the way the bot's judge does (``judge-oracle``).

    The unit is the signal, not the question: the model is asked again about a
    ``HOLD`` on every tick. A signal counts once, by its latest *final* answer
    (so ``HOLD`` then ``APPROVE`` is an ``APPROVE``, and a later ``HOLD`` does not
    take a final answer back). Only shadow-mode rows from ``EVALUATION_START``
    count, and the probes (``signal_version`` ``oracle-probe``) do not. A signal
    that never got a final answer still counts as asked.
    """
    asked: set[str] = set()
    final: dict[str, OracleDecision] = {}
    versions_known = False
    for decision in decisions:
        if (
            decision.mode != SHADOW_MODE
            or not decision.signal_id
            or decision.created_at is None
            or decision.created_at < EVALUATION_START
        ):
            continue
        if decision.signal_version == PROBE_VERSION:
            versions_known = True
            continue
        if decision.signal_version:
            versions_known = True
        asked.add(decision.signal_id)
        if decision.status == STATUS_FAILED or decision.verdict not in FINAL_VERDICTS:
            continue
        latest = final.get(decision.signal_id)
        if latest is None or decision.id > latest.id:
            final[decision.signal_id] = decision
    verdicts = [decision.verdict for decision in final.values()]
    return Progress(
        asked=len(asked),
        answered=len(final),
        approve=verdicts.count("APPROVE"),
        veto=verdicts.count("VETO"),
        probes_unmarked=bool(asked) and not versions_known,
    )


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
        self._progress_failed = False
        self._load_task = None
        self._poll_task = None
        # Every journal row since EVALUATION_START, by id: the evaluation counts
        # signals, so it needs more than the 50 rows of the table.
        self._progress_rows: dict[int, OracleDecision] = {}
        self._progress_lock = asyncio.Lock()

        self.symbol_field = ft.TextField(
            label="Символ",
            hint_text="напр. AAPL",
            width=150,
            dense=True,
            capitalization=ft.TextCapitalization.CHARACTERS,
            on_submit=self.on_filter_submit,
            on_change=self.on_symbol_change,
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
        self.approve_bar, self.approve_value, approve_row = self._progress_row("APPROVE")
        self.veto_bar, self.veto_value, veto_row = self._progress_row("VETO")
        self.availability_bar, self.availability_value, availability_row = (
            self._progress_row("Наличност")
        )
        self.progress_status = ft.Text("", size=12)
        self.probes_note = ft.Text(
            "Пробните сигнали още не се изключват (бекендът не праща версията на "
            "сигнала) — числата може да са завишени.",
            size=12,
            color=ft.colors.ORANGE_300,
            visible=False,
        )
        self.progress_error = ft.Text("", size=12, color=ft.colors.RED_300, visible=False)
        self.progress_block = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Към минимума на оценката", weight=ft.FontWeight.BOLD),
                    ft.Text(
                        "От журнала на бекенда: сенчест режим от "
                        f"{EVALUATION_START:%d.%m.%Y %H:%M} UTC, без проби. Присъдата е "
                        "на judge-oracle на бота, не на този панел: тук числата могат да "
                        "са по-високи, защото ботът записва „failed“ след своя таймаут, "
                        "а бекендът го води като „answered“.",
                        size=12,
                        color=ft.colors.GREY_400,
                    ),
                    approve_row,
                    veto_row,
                    availability_row,
                    self.progress_status,
                    self.probes_note,
                    self.progress_error,
                ],
                spacing=4,
                tight=True,
            ),
            padding=10,
            border_radius=8,
            border=ft.border.all(1, ft.colors.OUTLINE_VARIANT),
        )
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
                self.progress_block,
                self.updated_text,
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
            self._load_task = self.page.run_task(self.refresh)
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
        # What stands in the field is what the table shows: a filter that was kept
        # after its text was deleted would make "Обнови" look broken.
        if not self._apply_symbol_field():
            return
        await self.refresh()

    async def on_filter_submit(self, e):
        if not self._apply_symbol_field():
            return
        await self.load()

    async def on_symbol_change(self, e):
        """Emptying the field is "every symbol": there is no need to press search for it.

        Typing a symbol does not ask the backend on every key; that waits for Enter
        or the search button.
        """
        if (self.symbol_field.value or "").strip():
            return
        self.symbol_field.error_text = None
        safe_update(self, self.symbol_field)
        if self._symbol is not None:
            self._symbol = None
            await self.load()

    def _apply_symbol_field(self) -> bool:
        """Make the field the filter. ``False`` (and a complaint) if it is not a symbol."""
        symbol = (self.symbol_field.value or "").strip().upper()
        if symbol and not SYMBOL_PATTERN.match(symbol):
            self.symbol_field.error_text = "Невалиден символ (напр. AAPL, BRK.B)"
            safe_update(self, self.symbol_field)
            return False
        self.symbol_field.error_text = None
        self.symbol_field.value = symbol
        safe_update(self, self.symbol_field)
        self._symbol = symbol or None
        return True

    async def on_more_click(self, e):
        await self.load_more()

    # ------------------------------------------------------------------- data
    async def refresh(self, *_args):
        """Read the whole tab again: the table and the progress block."""
        await asyncio.gather(self.load(), self.refresh_progress(rebuild=True))

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
                await self.refresh_progress()
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

    async def refresh_progress(self, rebuild: bool = False):
        """Bring the evaluation counters up to date.

        ``rebuild`` reads the journal back to ``EVALUATION_START``; otherwise only
        the rows newer than the newest one already known are read. A failure is
        shown in the block (the old numbers stay) and never raised: the table has
        its own error line.
        """
        async with self._progress_lock:
            newest = 0 if rebuild or not self._progress_rows else max(self._progress_rows)
            try:
                fresh = await self._read_journal_since(newest)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                if not self._progress_failed:
                    logger.warning("Could not read the Oracle journal for the counters: %s", ex)
                self._progress_failed = True
                self.progress_error.value = f"Броячите не могат да се обновят: {error_detail(ex)}"
                self.progress_error.visible = True
                safe_update(self, self.progress_error)
                return
            self._progress_failed = False
            if rebuild:
                self._progress_rows = {}
            self._progress_rows.update({decision.id: decision for decision in fresh})
            self.progress_error.visible = False
            self._show_progress(count_progress(self._progress_rows.values()))

    async def _read_journal_since(self, newest_known_id: int) -> list[OracleDecision]:
        """The journal rows after ``newest_known_id`` and not before ``EVALUATION_START``."""
        rows: list[OracleDecision] = []
        before_id: int | None = None
        while True:
            page = await self.api_client.list_oracle_decisions(
                limit=PROGRESS_PAGE_SIZE, before_id=before_id
            )
            for decision in page.decisions:
                if decision.id <= newest_known_id or (
                    decision.created_at is not None
                    and decision.created_at < EVALUATION_START
                ):
                    return rows
                rows.append(decision)
            next_id = page.next_before_id
            # A page id that does not go back would read the same page forever.
            if not next_id or (before_id is not None and next_id >= before_id):
                return rows
            before_id = next_id

    # ----------------------------------------------------------------- screen
    @staticmethod
    def _progress_row(label: str) -> tuple[ft.ProgressBar, ft.Text, ft.Row]:
        bar = ft.ProgressBar(value=0, width=PROGRESS_BAR_WIDTH, color=ft.colors.BLUE)
        value = ft.Text("", size=12)
        row = ft.Row(
            [ft.Text(label, width=PROGRESS_LABEL_WIDTH), bar, value],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        return bar, value, row

    def _show_progress(self, progress: Progress) -> None:
        """Draw the counters. Only counts - never how a vetoed trade ended."""
        for bar, value, count, target in (
            (self.approve_bar, self.approve_value, progress.approve, APPROVE_TARGET),
            (self.veto_bar, self.veto_value, progress.veto, VETO_TARGET),
        ):
            bar.value = min(count / target, 1)
            bar.color = ft.colors.GREEN if count >= target else ft.colors.BLUE
            value.value = f"{count} / {target}"

        availability = progress.availability
        if availability is None:
            self.availability_bar.value = 0
            self.availability_bar.color = ft.colors.BLUE
            self.availability_value.value = "още няма сигнали"
        else:
            self.availability_bar.value = availability
            self.availability_bar.color = (
                ft.colors.GREEN if availability >= AVAILABILITY_TARGET else ft.colors.AMBER
            )
            self.availability_value.value = (
                f"{availability * 100:.0f} % ({progress.answered} от {progress.asked} "
                f"сигнала) · цел поне {AVAILABILITY_TARGET * 100:.0f} %"
            )

        self.progress_status.value = (
            "Минимумът е достигнат; присъда дава judge-oracle на бота."
            if progress.reached
            else "Под минимума няма присъда: събирането продължава."
        )
        self.probes_note.visible = progress.probes_unmarked
        safe_update(
            self,
            self.approve_bar, self.approve_value,
            self.veto_bar, self.veto_value,
            self.availability_bar, self.availability_value,
            self.progress_status, self.probes_note, self.progress_error,
        )

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
        if self.decisions:
            self.empty_text.visible = False
        else:
            self.empty_text.value = (
                f"Няма решения за {self._symbol}."
                if self._symbol
                else "Още няма решения. Журналът се пълни, когато ботът пита Oracle."
            )
            self.empty_text.visible = True
        self._touch(self.more_btn, self.empty_text, self.data_table)

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
