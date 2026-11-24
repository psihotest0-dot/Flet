"""The bar of the task the server walks in the open conversation (plan step 3.6)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

import views.chat_task as chat_task
from api.client import APIClient
from api.models import TaskPlan
from state.settings import Settings, save_settings
from views.chat import ChatView
from views.chat_task import task_line

CHAT_1 = "11111111-1111-1111-1111-111111111111"


def seed() -> None:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=CHAT_1,
            chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}],
        )
    )


def make_view(mock_page, mock_api_client) -> ChatView:
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


def plan(
    status="active", kind="key_facts", steps=("done", "doing", "pending")
) -> TaskPlan:
    return TaskPlan(
        id=1,
        goal="ключовите факти на всеки от 3 документа",
        status=status,
        kind=kind,
        steps=[
            {"id": i + 1, "text": f"doc-{i + 1}.pdf", "status": s}
            for i, s in enumerate(steps)
        ],
    )


def test_the_line_says_how_far_and_what_now():
    assert (
        task_line(plan())
        == "⏳ ключовите факти на всеки от 3 документа — готови 1 от 3 · сега: doc-2.pdf"
    )
    p = plan(steps=("done", "failed", "pending"))
    assert p.over == 2
    assert p.current.text == "doc-3.pdf"
    assert plan(steps=("done", "done")).current is None


@pytest.mark.asyncio
async def test_the_bar_shows_the_task_and_the_report_is_drawn_at_the_end(
    mock_page, mock_api_client, monkeypatch
):
    seed()
    monkeypatch.setattr(chat_task, "TASK_POLL_INTERVAL", 0)
    view = make_view(mock_page, mock_api_client)
    view.refresh_paired_chat = AsyncMock(return_value=True)
    view._learn_tail = AsyncMock()
    seen = []
    plans = iter(
        [
            plan(),
            plan(steps=("done", "done", "doing")),
            plan(status="done", steps=("done",) * 3),
        ]
    )

    async def next_plan(_session):
        p = next(plans)
        seen.append(view.task_bar.visible)
        return p

    mock_api_client.get_task_plan = AsyncMock(side_effect=next_plan)
    await view._watch_task(CHAT_1)

    assert mock_api_client.get_task_plan.await_count == 3
    assert seen == [False, True, True], "shown while it goes on"
    assert not view.task_bar.visible, "gone at the end"
    view.refresh_paired_chat.assert_awaited_once()
    view._learn_tail.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "found",
    [None, plan(status="done"), plan(kind=None)],
    ids=["no plan", "a finished one", "a plan of the assistant"],
)
async def test_nothing_is_shown_without_a_task_of_the_server(
    mock_page, mock_api_client, found
):
    seed()
    view = make_view(mock_page, mock_api_client)
    view.refresh_paired_chat = AsyncMock()
    mock_api_client.get_task_plan = AsyncMock(return_value=found)
    await view._watch_task(CHAT_1)
    assert not view.task_bar.visible
    view.refresh_paired_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_read_hides_the_bar_and_stops(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)
    mock_api_client.get_task_plan = AsyncMock(side_effect=httpx.ConnectError("down"))
    await view._watch_task(CHAT_1)
    assert not view.task_bar.visible


@pytest.mark.asyncio
async def test_another_conversation_stops_the_watch(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)

    async def switch(_session):
        view.session_id = "another"
        return plan()

    mock_api_client.get_task_plan = AsyncMock(side_effect=switch)
    await view._watch_task(CHAT_1)
    assert not view.task_bar.visible


@pytest.mark.asyncio
async def test_a_send_looks_for_a_task(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)
    view._start_task_watch = MagicMock()

    async def stream(**_kwargs):
        if False:
            yield None

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "да"
    await view.send_message_click(None)
    view._start_task_watch.assert_called_once()


@respx.mock
@pytest.mark.asyncio
async def test_the_client_reads_the_plan_or_none():
    client = APIClient(base_url="http://localhost:8080", user_id="u1")
    route = respx.get("http://localhost:8080/task-plan").mock(
        side_effect=[
            httpx.Response(200, json={"plan": None}),
            httpx.Response(200, json={"plan": json.loads(plan().model_dump_json())}),
        ]
    )
    assert await client.get_task_plan("s") is None
    got = await client.get_task_plan("s")
    assert got.kind == "key_facts" and len(got.steps) == 3
    assert route.calls.last.request.url.params["session_id"] == "s"
