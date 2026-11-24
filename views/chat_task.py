"""The task the server walks in the open conversation: a bar over the input (plan step 3.6).

"За всеки от добавените източници направи файл с ключовите му факти" → "да" starts a walk on the
backend, one document at a time, minutes long. While it goes on, ``ChatTaskMixin`` shows over the
input what it is and how far it got ("⏳ ключовите факти на всеки от 3 документа — 1 от 3 · сега:
…"), read from ``GET /task-plan`` every few seconds. When it ends the bar goes away and the report
the backend wrote in the conversation is drawn (``refresh_paired_chat`` adds what is newer than
the screen). Only a task of the server (``kind`` set) is shown; the chat itself still answers
"докъде стигна?" and "спри".

It is a part of ``ChatView``: it works on ``api_client``, ``session_id``, ``page_ref``,
``task_bar`` and ``_task_watch``, and calls ``refresh_paired_chat``.
"""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from api.models import PLAN_ACTIVE, TaskPlan
from views import theme
from views.ui_utils import is_unexpected, safe_update

logger = logging.getLogger(__name__)

# How often the plan is read while a task goes on (a document takes 5-10 s).
TASK_POLL_INTERVAL = 4.0


def task_line(plan: TaskPlan) -> str:
    """The text of the bar."""
    line = f"⏳ {plan.goal} — готови {plan.over} от {len(plan.steps)}"
    current = plan.current
    if current is not None:
        line += f" · сега: {current.text}"
    return line


class ChatTaskMixin:
    """The bar of the task the server walks in the open conversation."""

    def _build_task_bar(self) -> ft.Container:
        self.task_text = ft.Text(
            "", size=13, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS
        )
        self.task_progress = ft.ProgressBar(value=0, color=theme.ACCENT)
        hint = ft.Text(
            "В чата: „докъде стигна?“ или „спри“.", size=11, color=theme.MUTED
        )
        return ft.Container(
            content=ft.Column(
                [self.task_text, self.task_progress, hint], spacing=4, tight=True
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            bgcolor=theme.CARD,
            visible=False,
        )

    def _start_task_watch(self) -> None:
        """Look whether a task goes on in the open conversation (after a send, on open)."""
        if self.page_ref is None:
            return
        if self._task_watch is not None and not self._task_watch.done():
            return
        self._task_watch = self.page_ref.run_task(self._watch_task, self.session_id)

    def _stop_task_watch(self) -> None:
        if self._task_watch is not None:
            self._task_watch.cancel()
            self._task_watch = None
        self._show_task(None)

    async def _watch_task(self, session_id: str) -> None:
        """Show the task while it goes on; when it ends, draw the report and stop."""
        shown = False
        while session_id == self.session_id:
            try:
                plan = await self.api_client.get_task_plan(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # Not worth a message: the chat answers "докъде стигна?" anyway.
                logger.warning(
                    "Could not read the task of the conversation: %s",
                    ex,
                    exc_info=is_unexpected(ex),
                )
                plan = None
            if session_id != self.session_id:
                return
            running = (
                plan is not None
                and plan.kind is not None
                and plan.status == PLAN_ACTIVE
            )
            self._show_task(plan if running else None)
            if not running:
                if shown:
                    # The backend wrote the report in the conversation: draw it.
                    try:
                        await self.refresh_paired_chat()
                    except asyncio.CancelledError:
                        raise
                    except Exception as ex:
                        logger.warning(
                            "Could not draw the report of the task: %s",
                            ex,
                            exc_info=is_unexpected(ex),
                        )
                return
            if not shown and self._tail_id is None:
                # After a send the screen does not know its last message; the report comes only
                # at the end, so what is the last one now is what it will be drawn after.
                await self._learn_tail()
            shown = True
            await asyncio.sleep(TASK_POLL_INTERVAL)

    def _show_task(self, plan: TaskPlan | None) -> None:
        self.task_bar.visible = plan is not None
        if plan is not None:
            self.task_text.value = task_line(plan)
            self.task_progress.value = (
                plan.over / len(plan.steps) if plan.steps else None
            )
        safe_update(self, self.task_bar)
