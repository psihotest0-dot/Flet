from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import flet as ft
import pytest

import views.chat as chat_module
from views.chat import BOTTOM_FOLLOW_THRESHOLD, ChatView


@pytest.fixture
def view(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    return view


def scroll_event(event_type, *, direction=None, delta=None, pixels=0, max_extent=1000):
    return SimpleNamespace(
        event_type=event_type,
        direction=direction,
        scroll_delta=delta,
        pixels=pixels,
        max_scroll_extent=max_extent,
    )


def list_mounted():
    """Pretend the ListView is attached (``Control.uid`` is read-only)."""
    return patch.object(
        ft.ListView, "uid", new_callable=PropertyMock, return_value="uid-1"
    )


def test_list_is_scrolled_by_us_not_by_flet(view):
    # ListView.auto_scroll animated slowly against our own scroll_to and left the
    # end of a streaming answer 1-2 lines below the edge.
    assert not view.chat_list.auto_scroll
    assert view.chat_list.on_scroll is not None
    assert view._follow_bottom is True


# ------------------------------------------------------------ following state
def test_scrolling_up_stops_following(view):
    view._on_chat_scroll(scroll_event("user", direction="forward", pixels=900))
    assert view._follow_bottom is False


def test_dragging_the_scrollbar_up_stops_following(view):
    view._on_chat_scroll(scroll_event("update", delta=-120, pixels=700))
    assert view._follow_bottom is False


def test_scrolling_down_does_not_resume_until_the_bottom_is_reached(view):
    view._follow_bottom = False
    view._on_chat_scroll(scroll_event("update", delta=80, pixels=300))
    assert view._follow_bottom is False
    view._on_chat_scroll(scroll_event("user", direction="reverse", pixels=300))
    assert view._follow_bottom is False


def test_reaching_the_bottom_resumes_following(view):
    view._follow_bottom = False
    near_bottom = 1000 - BOTTOM_FOLLOW_THRESHOLD
    view._on_chat_scroll(scroll_event("update", delta=80, pixels=near_bottom))
    assert view._follow_bottom is True


def test_our_own_downward_scrolling_keeps_following(view):
    view._on_chat_scroll(scroll_event("update", delta=28, pixels=100, max_extent=1000))
    assert view._follow_bottom is True


def test_scroll_to_bottom_resumes_following(view):
    view._follow_bottom = False
    ChatView.scroll_to_bottom(view)
    assert view._follow_bottom is True


def test_new_conversation_resumes_following(view):
    view._follow_bottom = False
    view._reset_conversation()
    assert view._follow_bottom is True


# -------------------------------------------------------- deferred scrolling
def test_follow_stream_schedules_a_single_scroll(view, mock_page):
    view._follow_stream()
    view._follow_stream()
    view._follow_stream()
    mock_page.run_task.assert_called_once_with(view._scroll_after_layout)


def test_follow_stream_does_nothing_after_the_reader_scrolled_up(view, mock_page):
    view._follow_bottom = False
    view._follow_stream()
    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_scroll_after_layout_jumps_to_the_end(view, monkeypatch):
    monkeypatch.setattr(chat_module, "SCROLL_SETTLE_DELAY", 0)
    view.chat_list.scroll_to = MagicMock()
    view._scroll_pending = True

    with list_mounted():
        await view._scroll_after_layout()

    # No duration: an animation lags behind the text that keeps arriving.
    view.chat_list.scroll_to.assert_called_once_with(offset=-1)
    assert view._scroll_pending is False


@pytest.mark.asyncio
async def test_scroll_after_layout_leaves_a_reader_who_scrolled_up(view, monkeypatch):
    monkeypatch.setattr(chat_module, "SCROLL_SETTLE_DELAY", 0)
    view.chat_list.scroll_to = MagicMock()
    view._follow_bottom = False
    view._scroll_pending = True

    with list_mounted():
        await view._scroll_after_layout()

    view.chat_list.scroll_to.assert_not_called()
    assert view._scroll_pending is False


@pytest.mark.asyncio
async def test_scroll_after_layout_clears_the_flag_when_cancelled(view):
    # A cancelled task must not leave the view believing a scroll is pending.
    import asyncio

    view._scroll_pending = True
    task = asyncio.ensure_future(view._scroll_after_layout())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert view._scroll_pending is False


# --------------------------------------------------------------- streaming
def scrolls_scheduled(mock_page, view):
    return [
        call for call in mock_page.run_task.call_args_list
        if call.args == (view._scroll_after_layout,)
    ]


@pytest.mark.asyncio
async def test_streamed_text_keeps_the_end_in_view(view, mock_api_client, mock_page):
    async def fake_stream(**_kwargs):
        yield "first "
        yield "second"

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: fake_stream(**kw))
    view.scroll_to_bottom = MagicMock()
    view.input_field.value = "hi"

    await view.send_message_click(None)

    # One scroll for the streamed text and one more for the finished answer,
    # once the client has laid it out.
    assert len(scrolls_scheduled(mock_page, view)) == 2


@pytest.mark.asyncio
async def test_stream_does_not_pull_back_a_reader_who_went_up(
    view, mock_api_client, mock_page
):
    async def fake_stream(**_kwargs):
        yield "text"

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: fake_stream(**kw))
    view.scroll_to_bottom = MagicMock()
    view.input_field.value = "hi"
    view._follow_bottom = False

    await view.send_message_click(None)

    # Only the end-of-stream task exists, and it skips the scroll for this reader.
    assert len(scrolls_scheduled(mock_page, view)) == 1
    view.chat_list.scroll_to = MagicMock()
    with list_mounted(), patch.object(chat_module, "SCROLL_SETTLE_DELAY", 0):
        await view._scroll_after_layout()
    view.chat_list.scroll_to.assert_not_called()
