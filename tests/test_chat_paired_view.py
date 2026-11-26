"""The chat screen and a conversation shared with Telegram (task 4, step 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import flet as ft
import httpx
import pytest
from helpers import mounted

from api.errors import APIError
from api.models import Message, TelegramSession
from api.stream import Queued, Sources, TextChunk
from state.chat_sessions import normalize_chat_sessions
from state.settings import Settings, load_settings, save_settings
from views.chat import ChatView
from views.chat_message import ChatMessage
from views.chat_paired import (
    EMPTY_TAIL,
    HISTORY_PAGE_SIZE,
    HISTORY_REQUEST_LIMIT,
    OLDER_ANCHOR_KEY,
    OLDER_BUTTON_TEXT,
    OLDER_HINT,
    PAIRED_POLL_INTERVAL,
    PAIRED_POLL_MAX_INTERVAL,
    after_own_turns,
)
from views.chat_turns import ACTIVITY_QUEUED_TEXT, ACTIVITY_WORKING_TEXT

CHAT_1 = "11111111-1111-1111-1111-111111111111"
TG = "tg-1607252727"


def seed(session_id: str = CHAT_1, *chats: tuple[str, str]) -> None:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=session_id,
            chat_sessions=[
                {"id": chat_id, "name": name}
                for chat_id, name in (chats or ((CHAT_1, "Чат 1"),))
            ],
        )
    )


@pytest.fixture
def api(mock_api_client):
    mock_api_client.list_telegram_sessions = AsyncMock(return_value=[])
    return mock_api_client


def make_view(mock_page, api, session_id: str = CHAT_1, *chats) -> ChatView:
    seed(session_id, *chats)
    view = ChatView(mock_page, api)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    mock_page.run_task.reset_mock()
    return view


def make_tg_view(mock_page, api) -> ChatView:
    return make_view(mock_page, api, TG, (CHAT_1, "Чат 1"), (TG, "Telegram"))


def msgs(first: int, last: int, role=None) -> list[Message]:
    """Messages ``m<first>`` to ``m<last>``, oldest first, with ids like the backend's."""
    out = []
    for n in range(first, last + 1):
        out.append(
            Message(
                id=f"m{n}",
                role=role or ("user" if n % 2 else "assistant"),
                content=f"text {n}",
            )
        )
    return out


def texts(view: ChatView) -> list[str]:
    return [
        row.controls[0].content.md.value
        for row in view.chat_list.controls
        if isinstance(row, ft.Row) and isinstance(row.controls[0], ft.Container)
    ]


def dropdown_keys(view: ChatView) -> list[str]:
    return [option.key for option in view.session_dropdown.options]


async def load(view: ChatView, api, messages: list[Message]) -> None:
    api.get_messages = AsyncMock(return_value=messages)
    with mounted(ChatView):
        await view.load_history()
    api.get_messages.reset_mock()


# ============================================================ the chats of the list
@pytest.mark.asyncio
async def test_the_telegram_conversations_join_the_dropdown_and_are_kept(
    mock_page, api
):
    view = make_view(mock_page, api)
    api.list_telegram_sessions = AsyncMock(
        return_value=[TelegramSession(session_id=TG, name="Telegram")]
    )

    with mounted(ChatView):
        await view.load_paired_sessions()

    assert dropdown_keys(view) == [CHAT_1, TG]
    assert view.session_dropdown.value == CHAT_1, "the open chat stays the open chat"
    assert [(c.id, c.name) for c in normalize_chat_sessions(load_settings())] == [
        (CHAT_1, "Чат 1"),
        (TG, "Telegram"),
    ]


@pytest.mark.asyncio
async def test_a_conversation_the_list_has_is_not_added_again(mock_page, api):
    view = make_tg_view(mock_page, api)
    api.list_telegram_sessions = AsyncMock(
        return_value=[TelegramSession(session_id=TG, name="Telegram")]
    )

    with patch("views.chat_paired.save_settings") as save:
        await view.load_paired_sessions()

    save.assert_not_called()
    assert dropdown_keys(view) == [CHAT_1, TG]


@pytest.mark.asyncio
async def test_no_telegram_conversation_changes_nothing(mock_page, api):
    view = make_view(mock_page, api)

    with patch("views.chat_paired.save_settings") as save:
        await view.load_paired_sessions()

    save.assert_not_called()
    assert dropdown_keys(view) == [CHAT_1]


@pytest.mark.parametrize(
    ("error", "level"),
    [
        (APIError(404, "not found"), "INFO"),
        (APIError(500, "boom"), "WARNING"),
        (httpx.ConnectError("refused"), "WARNING"),
        (RuntimeError("a bug"), "WARNING"),
    ],
    ids=["older-backend", "server-error", "no-connection", "bug"],
)
@pytest.mark.asyncio
async def test_a_failed_list_is_in_the_log_and_not_on_the_screen(
    mock_page, api, caplog, error, level
):
    view = make_view(mock_page, api)
    api.list_telegram_sessions = AsyncMock(side_effect=error)

    with (
        patch("views.chat_paired.show_snackbar") as snackbar,
        caplog.at_level("INFO"),
    ):
        await view.load_paired_sessions()

    snackbar.assert_not_called()
    assert dropdown_keys(view) == [CHAT_1], "the chats there are still work"
    assert [r.levelname for r in caplog.records if "Telegram" in r.getMessage()] == [
        level
    ]


@pytest.mark.asyncio
async def test_when_the_settings_cannot_be_saved_the_list_stays_as_it_was(
    mock_page, api
):
    view = make_view(mock_page, api)
    api.list_telegram_sessions = AsyncMock(
        return_value=[TelegramSession(session_id=TG, name="Telegram")]
    )

    with (
        patch("views.chat_paired.save_settings", return_value=False),
        patch("views.chat_paired.show_snackbar") as snackbar,
    ):
        await view.load_paired_sessions()

    snackbar.assert_called_once()
    assert "Неуспешно запазване" in snackbar.call_args.args[1]
    assert dropdown_keys(view) == [CHAT_1]
    assert TG not in [c.id for c in normalize_chat_sessions(view.settings)]


def test_the_view_asks_for_the_telegram_chats_and_starts_watching_when_it_is_mounted(
    mock_page, api
):
    view = make_view(mock_page, api)

    view.did_mount()

    calls = [c.args[0] for c in mock_page.run_task.call_args_list]
    assert view.load_paired_sessions in calls
    assert view._poll_paired in calls
    assert calls[-1] == view.load_history, "the history stays the last"
    view.will_unmount()
    assert view._paired_task is None


# ================================================================ "waits" on the wheel
async def run_with_the_wheel(view, mock_page, api, monkeypatch, stream, done):
    monkeypatch.setattr("views.chat_turns.ACTIVITY_INDICATOR_DELAY", 0.35)
    monkeypatch.setattr(
        ChatMessage,
        "show_activity_indicator",
        lambda self, label: done.append(("show", label)),
    )
    monkeypatch.setattr(
        ChatMessage, "hide_activity_indicator", lambda self: done.append(("hide", None))
    )
    mock_page.run_task.side_effect = lambda fn, *args: (
        asyncio.ensure_future(fn(*args))
        if fn.__name__ == "show_activity_after_delay"
        else MagicMock()
    )
    api.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "hi"
    await view.send_message_click(None)


@pytest.mark.asyncio
async def test_a_request_that_waits_says_so_at_once_and_keeps_saying_it(
    mock_page, api, monkeypatch
):
    view = make_view(mock_page, api)
    done = []
    at_once = []

    async def stream(**_kwargs):
        yield Queued()
        at_once.extend(done)
        # Longer than the delay plus a look of the wheel (every 0.25 s): it would
        # say "Лили работи…" here if it did not know that the request waits.
        await asyncio.sleep(0.9)
        yield TextChunk("answer")

    await run_with_the_wheel(view, mock_page, api, monkeypatch, stream, done)

    assert at_once == [("show", ACTIVITY_QUEUED_TEXT)], "before anything else"
    shows = [label for what, label in done if what == "show"]
    assert set(shows) == {ACTIVITY_QUEUED_TEXT}, shows
    assert not any(label.startswith(ACTIVITY_WORKING_TEXT) for label in shows)
    # The first real frame ends the waiting.
    assert done[-1] == ("hide", None) or ("hide", None) in done[len(at_once) :]


@pytest.mark.asyncio
async def test_after_the_waiting_the_wheel_is_the_usual_one(
    mock_page, api, monkeypatch
):
    view = make_view(mock_page, api)
    done = []

    async def stream(**_kwargs):
        yield Queued()
        yield Sources([{"file_name": "book.pdf"}])  # the turn has begun
        await asyncio.sleep(0.9)  # and then nothing for a while
        yield TextChunk("answer")

    await run_with_the_wheel(view, mock_page, api, monkeypatch, stream, done)

    shows = [label for what, label in done if what == "show"]
    assert shows[0] == ACTIVITY_QUEUED_TEXT
    assert any(label.startswith(ACTIVITY_WORKING_TEXT) for label in shows[1:]), shows
    assert ACTIVITY_QUEUED_TEXT not in shows[1:]


# ================================================================== deleting a chat
def confirming(answer: bool):
    return patch("views.chat.confirm_dialog", new=AsyncMock(return_value=answer))


@pytest.mark.asyncio
async def test_deleting_the_telegram_chat_says_it_goes_in_telegram_and_keeps_its_id(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    api.delete_session = AsyncMock()
    await load(view, api, msgs(1, 4))

    with (
        mounted(ChatView),
        confirming(True) as confirm,
        patch("views.chat.show_snackbar"),
    ):
        await view.on_delete_chat_click(None)

    # The name of the chat is also "Telegram": it is the sentence that must be there.
    assert "ще се изтрие и там" in confirm.await_args.args[2]
    api.delete_session.assert_awaited_once_with(TG)
    assert view.session_id == TG, "the id is the key to both channels"
    assert view.settings.session_id == TG
    assert dropdown_keys(view) == [CHAT_1, TG]
    assert view.chat_list.controls == []
    assert view.empty_state.visible is True
    assert view._tail_id == EMPTY_TAIL, "known to be empty"


@pytest.mark.asyncio
async def test_deleting_an_ordinary_chat_does_not_mention_telegram_and_changes_its_id(
    mock_page, api
):
    view = make_view(mock_page, api)
    api.delete_session = AsyncMock()

    with (
        mounted(ChatView),
        confirming(True) as confirm,
        patch("views.chat.show_snackbar"),
    ):
        await view.on_delete_chat_click(None)

    assert "Telegram" not in confirm.await_args.args[2]
    assert view.session_id != CHAT_1


@pytest.mark.asyncio
async def test_refusing_the_deletion_of_the_telegram_chat_deletes_nothing(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    api.delete_session = AsyncMock()

    with confirming(False):
        await view.on_delete_chat_click(None)

    api.delete_session.assert_not_awaited()


# ====================================================================== older messages
@pytest.mark.asyncio
async def test_a_long_history_has_a_button_for_the_older_messages(mock_page, api):
    view = make_view(mock_page, api)

    await load(view, api, msgs(1, HISTORY_REQUEST_LIMIT))

    first = view.chat_list.controls[0]
    assert isinstance(first, ft.Row)
    assert first.controls[0].content == OLDER_BUTTON_TEXT
    assert view._older_row is first
    assert view._oldest_id == "m2", "the extra, oldest message is not shown"
    assert view._tail_id == f"m{HISTORY_REQUEST_LIMIT}"
    assert texts(view)[0] == "text 2"
    assert len(view.chat_list.controls) == HISTORY_PAGE_SIZE + 1


@pytest.mark.asyncio
async def test_a_history_that_fits_has_no_button(mock_page, api):
    view = make_view(mock_page, api)

    await load(view, api, msgs(1, HISTORY_PAGE_SIZE))

    assert view._older_row is None
    assert len(view.chat_list.controls) == HISTORY_PAGE_SIZE
    assert view._oldest_id == "m1"


@pytest.mark.asyncio
async def test_without_ids_the_old_hint_stays(mock_page, api):
    view = make_view(mock_page, api)
    nameless = [Message(role="user", content="x") for _ in range(HISTORY_REQUEST_LIMIT)]

    await load(view, api, nameless)

    hint = view.chat_list.controls[0]
    assert isinstance(hint, ft.Text)
    assert hint.value == OLDER_HINT
    assert view._older_row is None


@pytest.mark.asyncio
async def test_the_older_messages_come_above_the_shown_ones_and_the_reader_keeps_the_place(
    mock_page, api
):
    view = make_view(mock_page, api)
    await load(view, api, msgs(101, 201))  # m101 is the extra; m102..m201 are shown
    first_shown_row = view.chat_list.controls[1]
    api.get_messages = AsyncMock(return_value=msgs(96, 100))

    with mounted(ChatView):
        await view.on_load_older_click()

    api.get_messages.assert_awaited_once_with(
        view.session_id, limit=HISTORY_REQUEST_LIMIT, before="m102"
    )
    assert texts(view)[:6] == [f"text {n}" for n in (96, 97, 98, 99, 100, 102)]
    assert view._oldest_id == "m96"
    assert first_shown_row.key == OLDER_ANCHOR_KEY, "the place to scroll back to"
    mock_page.run_task.assert_any_call(view._keep_place, first_shown_row)
    assert view._follow_bottom is False
    # Fewer than a page came: the start of the conversation is on the screen.
    assert view._older_row is None
    assert not any(
        isinstance(c, ft.Row) and isinstance(c.controls[0], ft.TextButton)
        for c in view.chat_list.controls
    )
    assert view._history_truncated is False


@pytest.mark.asyncio
async def test_a_full_page_of_older_messages_leaves_the_button(mock_page, api):
    view = make_view(mock_page, api)
    await load(view, api, msgs(1000, 1100 + HISTORY_PAGE_SIZE))
    older_row = view._older_row
    api.get_messages = AsyncMock(return_value=msgs(1, HISTORY_REQUEST_LIMIT))

    with mounted(ChatView):
        await view.on_load_older_click()

    assert view.chat_list.controls[0] is older_row
    assert view._older_button.disabled is False
    assert view._oldest_id == "m2", "the extra one is the next page's first"
    assert view._history_truncated is True


@pytest.mark.asyncio
async def test_a_failed_load_of_older_messages_says_so_and_keeps_the_button(
    mock_page, api
):
    view = make_view(mock_page, api)
    await load(view, api, msgs(1, HISTORY_REQUEST_LIMIT))
    api.get_messages = AsyncMock(side_effect=httpx.ConnectError("refused"))
    before = len(view.chat_list.controls)

    with mounted(ChatView), patch("views.chat_paired.show_snackbar") as snackbar:
        await view.on_load_older_click()

    snackbar.assert_called_once()
    assert "По-старите съобщения не се заредиха" in snackbar.call_args.args[1]
    assert len(view.chat_list.controls) == before
    assert view._older_button.disabled is False
    assert view._loading_older is False


@pytest.mark.asyncio
async def test_a_second_click_while_older_messages_are_coming_is_ignored(
    mock_page, api
):
    view = make_view(mock_page, api)
    await load(view, api, msgs(1, HISTORY_REQUEST_LIMIT))
    release = asyncio.Event()

    async def slow(*_args, **_kwargs):
        await release.wait()
        return []

    api.get_messages = AsyncMock(side_effect=slow)

    with mounted(ChatView):
        first = asyncio.ensure_future(view.on_load_older_click())
        await asyncio.sleep(0)
        await view.on_load_older_click()
        assert api.get_messages.await_count == 1
        release.set()
        await first


@pytest.mark.asyncio
async def test_older_messages_of_a_chat_that_was_left_are_not_put_into_the_next_one(
    mock_page, api
):
    view = make_view(mock_page, api)
    await load(view, api, msgs(1, HISTORY_REQUEST_LIMIT))

    async def other_chat_opened(*_args, **_kwargs):
        view.session_id = "another"
        return msgs(500, 509)

    api.get_messages = AsyncMock(side_effect=other_chat_opened)

    with mounted(ChatView):
        await view.on_load_older_click()

    assert "text 500" not in texts(view)
    assert len(view.chat_list.controls) == HISTORY_PAGE_SIZE + 1


@pytest.mark.asyncio
async def test_there_is_nothing_to_ask_for_when_the_oldest_id_is_not_known(
    mock_page, api
):
    view = make_view(mock_page, api)

    await view.on_load_older_click()

    api.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_reader_is_scrolled_back_to_the_message_they_were_at(mock_page, api):
    view = make_view(mock_page, api)
    anchor = ft.Row()
    anchor.key = OLDER_ANCHOR_KEY
    view.chat_list.scroll_to = AsyncMock()

    with patch("views.chat_paired.is_mounted", return_value=True):
        await view._keep_place(anchor)

    view.chat_list.scroll_to.assert_awaited_once_with(
        scroll_key=OLDER_ANCHOR_KEY, duration=0
    )
    assert anchor.key is None, "the key was only for this scroll"


@pytest.mark.asyncio
async def test_a_message_can_be_added_at_a_place(mock_page, api):
    view = make_view(mock_page, api)

    with mounted(ChatView):
        view.add_message("user", "last", notify=False)
        view.add_message("user", "first", notify=False, index=0)

    assert texts(view) == ["first", "last"]


# ============================================================ what arrives from Telegram
@pytest.mark.parametrize(
    ("change", "wanted"),
    [
        ({}, True),
        ({"_paired_active": False}, False),
        ({"session_id": CHAT_1}, False),
        ({"_is_streaming": True}, False),
        ({"_load_task": MagicMock(done=lambda: False)}, False),
        ({"_load_task": MagicMock(done=lambda: True)}, True),
    ],
    ids=["open", "other-tab", "ordinary-chat", "answering", "loading", "loaded"],
)
def test_an_open_telegram_conversation_is_looked_at_only_when_it_is_quiet(
    mock_page, api, change, wanted
):
    view = make_tg_view(mock_page, api)
    for name, value in change.items():
        setattr(view, name, value)

    assert view._paired_wanted() is wanted


@pytest.mark.asyncio
async def test_the_first_look_only_learns_where_the_conversation_stands(mock_page, api):
    view = make_tg_view(mock_page, api)
    view._tail_id = None
    api.get_messages = AsyncMock(return_value=msgs(7, 7))

    drawn = await view.refresh_paired_chat()

    assert drawn is False
    assert view._tail_id == "m7"
    api.get_messages.assert_awaited_once_with(TG, limit=1)
    assert view.chat_list.controls == []


@pytest.mark.asyncio
async def test_when_nothing_is_new_one_small_request_is_all_it_costs(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))
    api.get_messages = AsyncMock(return_value=msgs(4, 4))

    drawn = await view.refresh_paired_chat()

    assert drawn is False
    api.get_messages.assert_awaited_once_with(TG, limit=1)


@pytest.mark.asyncio
async def test_what_came_from_telegram_is_added_at_the_end_and_only_that(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))  # the last one, m4, is an answer
    old_answer = view._last_assistant_msg
    assert old_answer is not None
    calls = [msgs(5, 5), msgs(1, 6)]
    api.get_messages = AsyncMock(side_effect=calls)

    with mounted(ChatView):
        drawn = await view.refresh_paired_chat()

    assert drawn is True
    assert texts(view) == [f"text {n}" for n in range(1, 7)]
    assert view._tail_id == "m6"
    assert api.get_messages.await_args_list[1].kwargs == {
        "limit": HISTORY_REQUEST_LIMIT
    }
    # "Опитай отново" moves to the newest answer.
    assert view._last_assistant_msg is not old_answer
    assert view._last_assistant_msg.md.value == "text 6"
    assert view._last_assistant_row is view.chat_list.controls[-1]
    assert old_answer.regenerate_btn.visible is False, "only the newest has it"
    assert view._last_assistant_msg.regenerate_btn.visible is True


@pytest.mark.asyncio
async def test_a_question_that_has_no_answer_yet_leaves_no_retry_button(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))
    api.get_messages = AsyncMock(side_effect=[msgs(5, 5), msgs(1, 5)])

    with mounted(ChatView):
        await view.refresh_paired_chat()

    assert texts(view)[-1] == "text 5"
    assert view._last_assistant_msg is None


@pytest.mark.asyncio
async def test_if_the_known_message_is_gone_the_conversation_is_read_again(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))
    api.get_messages = AsyncMock(side_effect=[msgs(50, 50), msgs(40, 50)])
    view.load_history = AsyncMock()

    drawn = await view.refresh_paired_chat()

    assert drawn is True
    view.load_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_first_message_of_an_empty_conversation_shows(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, [])
    assert view._tail_id == EMPTY_TAIL
    api.get_messages = AsyncMock(side_effect=[msgs(1, 1), msgs(1, 1)])
    view.load_history = AsyncMock()

    drawn = await view.refresh_paired_chat()

    assert drawn is True
    view.load_history.assert_awaited_once()


@pytest.mark.parametrize(
    "latest", [[], [Message(content="old backend")]], ids=["empty", "no-id"]
)
@pytest.mark.asyncio
async def test_nothing_is_done_without_a_message_to_compare(mock_page, api, latest):
    view = make_tg_view(mock_page, api)
    view._tail_id = "m1"
    api.get_messages = AsyncMock(return_value=latest)

    assert await view.refresh_paired_chat() is False
    assert view._tail_id == "m1"


@pytest.mark.asyncio
async def test_a_look_that_meets_an_answer_of_the_app_draws_nothing(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))

    async def the_app_starts_answering(*_args, **_kwargs):
        view._is_streaming = True
        return msgs(5, 5)

    api.get_messages = AsyncMock(side_effect=the_app_starts_answering)

    assert await view.refresh_paired_chat() is False
    assert len(view.chat_list.controls) == 4


@pytest.mark.asyncio
async def test_a_look_at_a_chat_that_was_left_draws_nothing(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 4))

    async def another_chat_opened(*_args, **_kwargs):
        view.session_id = CHAT_1
        return msgs(5, 5)

    api.get_messages = AsyncMock(side_effect=another_chat_opened)

    assert await view.refresh_paired_chat() is False
    assert view._tail_id == "m4"


def said(id: str, role: str, content: str) -> Message:
    return Message(id=id, role=role, content=content)


def answer_on_screen(view: ChatView, question: str, answer: str) -> None:
    """The app asked and was answered: two messages the screen has no ids for."""
    with mounted(ChatView):
        view.add_message("user", question, notify=False)
        view.add_message("assistant", answer, notify=False)


def backend_has(api, history: list[Message]) -> None:
    """``get_messages`` answers like the backend: the newest ``limit``, oldest first."""

    async def get_messages(session_id, limit=None, before=None):
        return history[-limit:] if limit else history

    api.get_messages = AsyncMock(side_effect=get_messages)


@pytest.mark.asyncio
async def test_after_a_turn_of_the_app_its_own_messages_are_not_drawn_a_second_time(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    answer_on_screen(view, "asked here", "answered here")
    backend_has(
        api,
        [
            *msgs(1, 2),
            said("m3", "user", "asked here"),
            said("m4", "assistant", "answered here"),
        ],
    )

    view._forget_tail("asked here")
    assert view._tail_id is None
    mock_page.run_task.assert_called_once_with(view._learn_tail)
    with mounted(ChatView):
        assert await view._learn_tail() is False
    assert view._tail_id == "m4"

    assert await view.refresh_paired_chat() is False
    assert texts(view) == ["text 1", "text 2", "asked here", "answered here"]


@pytest.mark.asyncio
async def test_what_telegram_wrote_right_after_a_turn_of_the_app_is_drawn(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    answer_on_screen(view, "asked here", "answered here")
    # The person also wrote from Telegram the moment the app's turn ended.
    backend_has(
        api,
        [
            *msgs(1, 2),
            said("m3", "user", "asked here"),
            said("m4", "assistant", "answered here"),
            said("m5", "user", "from telegram"),
        ],
    )

    view._forget_tail("asked here")
    with mounted(ChatView):
        assert await view._learn_tail() is True

    assert texts(view)[-3:] == ["asked here", "answered here", "from telegram"]
    assert view._tail_id == "m5"
    assert await view.refresh_paired_chat() is False, "nothing is drawn twice"
    assert len(texts(view)) == 5


@pytest.mark.asyncio
async def test_a_look_before_the_end_was_learned_recognises_the_turn_too(
    mock_page, api
):
    # The poll can come before ``_learn_tail`` (or after it failed).
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    answer_on_screen(view, "asked here", "answered here")
    view._forget_tail("asked here")
    backend_has(
        api,
        [
            *msgs(1, 2),
            said("m3", "user", "asked here"),
            said("m4", "assistant", "answered here"),
            said("m5", "user", "from telegram"),
        ],
    )

    with mounted(ChatView):
        assert await view.refresh_paired_chat() is True

    assert texts(view)[-1] == "from telegram"
    assert view._tail_id == "m5"


@pytest.mark.asyncio
async def test_two_turns_in_a_row_are_both_recognised(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    answer_on_screen(view, "first", "answer 1")
    view._forget_tail("first")
    answer_on_screen(view, "second", "answer 2")
    view._forget_tail("second")
    backend_has(
        api,
        [
            *msgs(1, 2),
            said("m3", "user", "first"),
            said("m4", "assistant", "answer 1"),
            said("m5", "user", "second"),
            said("m6", "assistant", "answer 2"),
        ],
    )

    with mounted(ChatView):
        assert await view._learn_tail() is False

    assert view._tail_id == "m6"
    assert len(texts(view)) == 6


@pytest.mark.asyncio
async def test_a_refused_message_leaves_the_conversation_as_it_was(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    view._forget_tail("not taken")
    backend_has(api, msgs(1, 2))
    view.load_history = AsyncMock()

    assert await view._learn_tail() is False

    view.load_history.assert_not_awaited()
    assert view._tail_id == "m2"


@pytest.mark.parametrize(
    "history",
    [
        # A regeneration: the old answer has a new id.
        pytest.param(None, id="regenerated"),
        # The end the screen knew is not there any more.
        pytest.param([said("m9", "user", "asked here")], id="known-end-gone"),
        # Something came before the app's own message.
        pytest.param(
            [
                *msgs(1, 2),
                said("m3", "user", "other"),
                said("m4", "user", "asked here"),
            ],
            id="other-first",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_turn_that_cannot_be_recognised_reads_the_history_again(
    mock_page, api, history
):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    if history is None:
        view._forget_tail()
        history = [*msgs(1, 1), said("m7", "assistant", "regenerated")]
    else:
        view._forget_tail("asked here")
    backend_has(api, history)
    view.load_history = AsyncMock()

    assert await view._learn_tail() is True

    view.load_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_learning_waits_while_a_new_answer_is_written(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    view._forget_tail("asked here")
    backend_has(api, [*msgs(1, 2), said("m3", "user", "asked here")])
    view._is_streaming = True

    assert await view._learn_tail() is False

    assert view._tail_id is None
    assert view._unseen_turns == ("m2", ["asked here"]), (
        "the next turn's end tries again"
    )


@pytest.mark.asyncio
async def test_a_history_drawn_again_forgets_the_turns(mock_page, api):
    view = make_tg_view(mock_page, api)
    view._forget_tail("asked here")

    await load(view, api, msgs(1, 4))

    assert view._unseen_turns is None
    assert view._tail_id == "m4"


@pytest.mark.parametrize(
    "page, before, sent, new",
    [
        ([], EMPTY_TAIL, [], []),
        (
            [said("m1", "user", "hi"), said("m2", "assistant", "hello")],
            EMPTY_TAIL,
            ["hi"],
            [],
        ),
        # A stopped turn: the backend kept the question only.
        (
            [
                said("m1", "user", "hi"),
                said("m2", "user", "tg"),
                said("m3", "assistant", "a"),
            ],
            EMPTY_TAIL,
            [" hi "],
            ["m2", "m3"],
        ),
        ([said("m1", "user", "hi")], None, ["hi"], None),
        ([said("m1", "assistant", "hi")], EMPTY_TAIL, ["hi"], None),
    ],
    ids=["empty", "answered", "stopped-then-telegram", "end-unknown", "not-a-question"],
)
def test_after_own_turns(page, before, sent, new):
    result = after_own_turns(page, before, sent)
    assert (None if result is None else [m.id for m in result]) == new


@pytest.mark.asyncio
async def test_learning_the_end_does_not_overwrite_what_a_look_already_learned(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    view._tail_id = "m9"
    api.get_messages = AsyncMock(return_value=msgs(4, 4))

    await view._learn_tail()

    assert view._tail_id == "m9"


@pytest.mark.asyncio
async def test_a_failed_learning_of_the_end_is_not_an_error(mock_page, api):
    view = make_tg_view(mock_page, api)
    view._tail_id = None
    api.get_messages = AsyncMock(side_effect=httpx.ConnectError("refused"))

    await view._learn_tail()

    assert view._tail_id is None, "the next look learns it"


@pytest.mark.asyncio
async def test_an_ordinary_chat_does_not_ask_the_backend_for_its_end(mock_page, api):
    view = make_view(mock_page, api)
    view._tail_id = "m1"

    view._forget_tail()

    assert view._tail_id is None
    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_sending_and_regenerating_forget_the_end(mock_page, api):
    view = make_tg_view(mock_page, api)
    await load(view, api, msgs(1, 2))
    view._forget_tail = MagicMock()
    api.stream_chat = MagicMock(side_effect=lambda **kw: _stream(TextChunk("new")))
    api.regenerate_chat = MagicMock(
        side_effect=lambda **kw: _stream(TextChunk("newer"))
    )
    view.input_field.value = "hi"

    with mounted(ChatView):
        await view.send_message_click(None)
        assert view._forget_tail.call_count == 1
        await view.regenerate_last()

    assert view._forget_tail.call_count == 2


async def _stream(*events):
    for event in events:
        yield event


# ------------------------------------------------------------------- the poll itself
class StopAfter:
    """An ``asyncio.sleep`` that records its delays and ends the loop after ``count``."""

    def __init__(self, count: int):
        self.count = count
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.count:
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_the_poll_waits_longer_after_a_failure_and_goes_back_after_a_success(
    mock_page, api, caplog
):
    view = make_tg_view(mock_page, api)
    view.refresh_paired_chat = AsyncMock(
        side_effect=[RuntimeError("down"), httpx.ConnectError("down"), False]
    )
    sleeper = StopAfter(4)

    with (
        patch("views.chat_paired.asyncio.sleep", sleeper),
        caplog.at_level("WARNING"),
        pytest.raises(asyncio.CancelledError),
    ):
        await view._poll_paired()

    assert sleeper.delays == [
        PAIRED_POLL_INTERVAL,
        PAIRED_POLL_INTERVAL * 2,
        PAIRED_POLL_INTERVAL * 4,
        PAIRED_POLL_INTERVAL,
    ]
    warnings = [r for r in caplog.records if "Telegram conversation" in r.getMessage()]
    assert len(warnings) == 1, "one line when it starts to fail, not one per try"


@pytest.mark.asyncio
async def test_the_pause_after_failures_has_a_ceiling(mock_page, api):
    view = make_tg_view(mock_page, api)
    view.refresh_paired_chat = AsyncMock(side_effect=RuntimeError("down"))
    sleeper = StopAfter(8)

    with (
        patch("views.chat_paired.asyncio.sleep", sleeper),
        pytest.raises(asyncio.CancelledError),
    ):
        await view._poll_paired()

    assert max(sleeper.delays) == PAIRED_POLL_MAX_INTERVAL
    assert sleeper.delays[-1] == PAIRED_POLL_MAX_INTERVAL


@pytest.mark.asyncio
async def test_the_poll_does_not_look_while_it_is_not_wanted(mock_page, api):
    view = make_tg_view(mock_page, api)
    view._paired_active = False
    view.refresh_paired_chat = AsyncMock()
    sleeper = StopAfter(3)

    with (
        patch("views.chat_paired.asyncio.sleep", sleeper),
        pytest.raises(asyncio.CancelledError),
    ):
        await view._poll_paired()

    view.refresh_paired_chat.assert_not_awaited()
    assert sleeper.delays == [PAIRED_POLL_INTERVAL] * 3


def test_the_poll_starts_once_and_stops(mock_page, api):
    view = make_tg_view(mock_page, api)

    view._start_paired_poll()
    view._start_paired_poll()

    mock_page.run_task.assert_called_once_with(view._poll_paired)
    task = view._paired_task
    view._stop_paired_poll()
    task.cancel.assert_called_once()
    assert view._paired_task is None
    view._stop_paired_poll()  # harmless twice


def test_set_active_says_whether_the_tab_is_the_visible_one(mock_page, api):
    view = make_tg_view(mock_page, api)

    view.set_active(False)
    assert view._paired_active is False
    view.set_active(True)
    assert view._paired_active is True


# ----------------------------------------------------------------- switching chats
@pytest.mark.asyncio
async def test_an_emptied_chat_knows_it_is_empty_and_an_opened_one_does_not_yet(
    mock_page, api
):
    view = make_tg_view(mock_page, api)
    view._tail_id = "m5"

    with mounted(ChatView):
        view._activate_session(CHAT_1)
    assert view._tail_id is None, "until its history is loaded"

    with mounted(ChatView):
        view._activate_session(CHAT_1, reload_history=False)
    assert view._tail_id == EMPTY_TAIL
