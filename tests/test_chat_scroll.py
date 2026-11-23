from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import flet as ft
import pytest
from helpers import mounted

import views.chat_scroll as scroll_module
from api.stream import TextChunk
from views.chat import ChatView
from views.chat_scroll import BOTTOM_FOLLOW_THRESHOLD


@pytest.fixture
def view(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    return view


USER, UPDATE = ft.ScrollType.USER, ft.ScrollType.UPDATE
# Flutter's names: "forward" is towards the start of the list, i.e. up.
UP, DOWN = ft.ScrollDirection.FORWARD, ft.ScrollDirection.REVERSE


def scroll_event(event_type, *, direction=None, delta=None, pixels=0, max_extent=1000):
    """The event as Flet builds it: a real ``OnScrollEvent`` with its enums."""
    return ft.OnScrollEvent(
        name="scroll",
        control=None,
        event_type=event_type,
        pixels=pixels,
        min_scroll_extent=0,
        max_scroll_extent=max_extent,
        viewport_dimension=600,
        scroll_delta=delta,
        direction=direction,
    )


def list_mounted():
    """Pretend the ListView is on the page."""
    return mounted(ft.ListView)


def test_list_is_scrolled_by_us_not_by_flet(view):
    # ListView.auto_scroll animated slowly against our own scroll_to and left the
    # end of a streaming answer 1-2 lines below the edge.
    assert not view.chat_list.auto_scroll
    assert view.chat_list.on_scroll is not None
    assert view._follow_bottom is True


# ------------------------------------------------------------ following state
def test_scrolling_up_stops_following(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    assert view._follow_bottom is False


def test_dragging_the_scrollbar_up_stops_following(view):
    view._on_chat_scroll(scroll_event(UPDATE, delta=-120, pixels=700))
    assert view._follow_bottom is False


def test_scrolling_down_does_not_resume_until_the_bottom_is_reached(view):
    view._follow_bottom = False
    view._on_chat_scroll(scroll_event(UPDATE, delta=80, pixels=300))
    assert view._follow_bottom is False
    view._on_chat_scroll(scroll_event(USER, direction=DOWN, pixels=300))
    assert view._follow_bottom is False


def test_reaching_the_bottom_resumes_following(view):
    view._follow_bottom = False
    near_bottom = 1000 - BOTTOM_FOLLOW_THRESHOLD
    view._on_chat_scroll(scroll_event(UPDATE, delta=80, pixels=near_bottom))
    assert view._follow_bottom is True


def test_our_own_downward_scrolling_keeps_following(view):
    view._on_chat_scroll(scroll_event(UPDATE, delta=28, pixels=100, max_extent=1000))
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
    monkeypatch.setattr(scroll_module, "SCROLL_SETTLE_DELAY", 0)
    view.chat_list.scroll_to = AsyncMock()
    view._scroll_pending = True

    with list_mounted():
        await view._scroll_after_layout()

    # No duration: an animation lags behind the text that keeps arriving.
    view.chat_list.scroll_to.assert_awaited_once_with(offset=-1, duration=None)
    assert view._scroll_pending is False


def test_scroll_to_bottom_asks_for_one_short_animated_scroll(view, mock_page):
    with list_mounted():
        ChatView.scroll_to_bottom(view)

    mock_page.run_task.assert_any_call(view._scroll_to_end, 100)


def test_scroll_to_bottom_leaves_a_list_that_is_not_on_the_page(view, mock_page):
    ChatView.scroll_to_bottom(view)

    assert (view._scroll_to_end, 100) not in [
        c.args for c in mock_page.run_task.call_args_list
    ]


@pytest.mark.asyncio
async def test_a_list_that_left_the_page_is_not_scrolled(view):
    view.chat_list.scroll_to = AsyncMock()

    await view._scroll_to_end()

    view.chat_list.scroll_to.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [RuntimeError("Session closed"), TimeoutError("no answer")],
    ids=["closed-window", "silent-client"],
)
async def test_a_scroll_the_client_did_not_do_is_not_an_error(view, error):
    # The call waits for the client's answer. Raised out of a page task it would
    # be a traceback in app.log for every chat that was closed mid-scroll.
    view.chat_list.scroll_to = AsyncMock(side_effect=error)

    with list_mounted():
        await view._scroll_to_end()

    view.chat_list.scroll_to.assert_awaited_once()


@pytest.mark.asyncio
async def test_scroll_after_layout_leaves_a_reader_who_scrolled_up(view, monkeypatch):
    monkeypatch.setattr(scroll_module, "SCROLL_SETTLE_DELAY", 0)
    view.chat_list.scroll_to = AsyncMock()
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


# ------------------------------------------------- settling at the end of a history
def running_task():
    """Stands in for a page task that has not finished."""
    return MagicMock(done=MagicMock(return_value=False))


def test_a_scroll_to_the_bottom_is_not_settled_unless_asked(view, mock_page):
    ChatView.scroll_to_bottom(view)

    scheduled = [call.args for call in mock_page.run_task.call_args_list]
    assert (view._scroll_after_layout,) in scheduled
    assert (view._settle_at_end,) not in scheduled


def test_a_settling_scroll_to_the_bottom_replaces_the_single_deferred_one(
    view, mock_page
):
    ChatView.scroll_to_bottom(view, settle=True)

    scheduled = [call.args for call in mock_page.run_task.call_args_list]
    assert (view._settle_at_end,) in scheduled
    assert (view._scroll_after_layout,) not in scheduled
    assert view._follow_bottom is True


def test_starting_to_settle_again_cancels_the_one_that_runs(view, mock_page):
    old = running_task()
    view._settle_task = old

    view._start_settling()

    old.cancel.assert_called_once_with()
    assert view._settle_task is mock_page.run_task.return_value


def test_a_settle_task_that_is_over_is_not_cancelled(view):
    over = MagicMock(done=MagicMock(return_value=True))
    view._settle_task = over

    view._stop_settling()

    over.cancel.assert_not_called()
    assert view._settle_task is None


def test_closing_the_chat_stops_the_settling(view):
    task = running_task()
    view._settle_task = task

    view._cancel_running_tasks()

    task.cancel.assert_called_once_with()


@pytest.mark.asyncio
async def test_the_button_settles_at_the_end_too(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    view.page_ref.run_task.reset_mock()

    with list_mounted():
        await view.on_to_bottom_click()

    view.page_ref.run_task.assert_any_call(view._settle_at_end)


def jumps_that_move(view, count):
    """A ``scroll_to`` whose first ``count`` jumps move the list (and so raise events)."""
    calls = []

    async def scroll_to(**kwargs):
        calls.append(kwargs)
        if len(calls) <= count:
            view._on_chat_scroll(
                scroll_event(UPDATE, delta=500, pixels=500 * len(calls))
            )

    view.chat_list.scroll_to = AsyncMock(side_effect=scroll_to)
    return calls


@pytest.mark.asyncio
async def test_settling_jumps_until_a_jump_moves_nothing(view, monkeypatch):
    # The end the client jumped to was an estimate: it moved on twice (as it did with
    # a real history of a hundred messages) and the third jump found it where it was.
    monkeypatch.setattr(scroll_module, "SCROLL_SETTLE_DELAY", 0)
    calls = jumps_that_move(view, count=2)

    with list_mounted():
        await view._settle_at_end()

    assert calls == [{"offset": -1, "duration": None}] * 3


@pytest.mark.asyncio
async def test_settling_on_a_list_that_is_already_at_the_end_jumps_once(
    view, monkeypatch
):
    monkeypatch.setattr(scroll_module, "SCROLL_SETTLE_DELAY", 0)
    calls = jumps_that_move(view, count=0)

    with list_mounted():
        await view._settle_at_end()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_settling_gives_up_on_a_list_that_never_stops_moving(view, monkeypatch):
    monkeypatch.setattr(scroll_module, "SCROLL_SETTLE_DELAY", 0)
    calls = jumps_that_move(view, count=10_000)

    with list_mounted():
        await view._settle_at_end()

    assert len(calls) == scroll_module.SETTLE_ROUNDS


@pytest.mark.asyncio
async def test_settling_waits_before_the_first_jump(view, monkeypatch):
    # The rows were sent just now; the client lays them out on its next frame.
    import asyncio

    waited = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        waited.append((seconds, view.chat_list.scroll_to.await_count))
        await real_sleep(0)

    monkeypatch.setattr(scroll_module.asyncio, "sleep", sleep)
    jumps_that_move(view, count=0)

    with list_mounted():
        await view._settle_at_end()

    assert waited[0] == (scroll_module.SCROLL_SETTLE_DELAY, 0)


@pytest.mark.asyncio
async def test_the_reader_scrolling_up_ends_the_settling(view):
    import asyncio

    view.chat_list.scroll_to = AsyncMock()
    task = asyncio.ensure_future(view._settle_at_end())
    view._settle_task = task
    await asyncio.sleep(0)

    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert view._follow_bottom is False
    assert view._settle_task is None


def test_a_list_sliding_back_while_settling_is_not_the_reader(view):
    # Sent past its real end, the list slides back by itself in negative steps; the
    # button must not blink and the following must not stop because of that.
    view._settle_task = running_task()

    view._on_chat_scroll(
        scroll_event(UPDATE, delta=-24, pixels=11378, max_extent=10277)
    )

    assert view._follow_bottom is True
    assert view.to_bottom_btn.visible is False


def test_a_scrollbar_dragged_up_is_the_reader_once_the_settling_is_over(view):
    view._settle_task = MagicMock(done=MagicMock(return_value=True))

    view._on_chat_scroll(scroll_event(UPDATE, delta=-24, pixels=700))

    assert view._follow_bottom is False


def test_every_scroll_event_is_counted(view):
    view._on_chat_scroll(scroll_event(UPDATE, delta=10, pixels=10))
    view._on_chat_scroll(scroll_event(USER, direction=DOWN, pixels=20))

    assert view._scroll_events == 2


# --------------------------------------------------------------- streaming
def scrolls_scheduled(mock_page, view):
    return [
        call
        for call in mock_page.run_task.call_args_list
        if call.args == (view._scroll_after_layout,)
    ]


@pytest.mark.asyncio
async def test_streamed_text_keeps_the_end_in_view(view, mock_api_client, mock_page):
    async def fake_stream(**_kwargs):
        yield TextChunk("first ")
        yield TextChunk("second")

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
        yield TextChunk("text")

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: fake_stream(**kw))
    view.scroll_to_bottom = MagicMock()
    view.input_field.value = "hi"
    view._follow_bottom = False

    await view.send_message_click(None)

    # Only the end-of-stream task exists, and it skips the scroll for this reader.
    assert len(scrolls_scheduled(mock_page, view)) == 1
    view.chat_list.scroll_to = AsyncMock()
    with list_mounted(), patch.object(scroll_module, "SCROLL_SETTLE_DELAY", 0):
        await view._scroll_after_layout()
    view.chat_list.scroll_to.assert_not_called()


# ------------------------------------------------------- the "to the newest" button
def test_the_button_is_not_there_while_the_reader_follows_the_end(view):
    assert view.to_bottom_btn.visible is False

    view._on_chat_scroll(scroll_event(UPDATE, delta=28, pixels=100, max_extent=1000))

    assert view.to_bottom_btn.visible is False


def test_scrolling_up_shows_the_button_and_reaching_the_end_hides_it(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    assert view.to_bottom_btn.visible is True

    view._on_chat_scroll(
        scroll_event(UPDATE, delta=80, pixels=1000 - BOTTOM_FOLLOW_THRESHOLD)
    )
    assert view.to_bottom_btn.visible is False


def test_scrolling_down_a_little_keeps_the_button(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))

    view._on_chat_scroll(scroll_event(UPDATE, delta=80, pixels=300))

    assert view.to_bottom_btn.visible is True


def test_only_a_change_of_the_button_is_sent_to_the_client(view):
    with patch("views.chat_scroll.safe_update") as send:
        view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
        view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=800))
        view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=700))

    send.assert_called_once_with(view, view.to_bottom_btn)


@pytest.mark.asyncio
async def test_the_button_brings_the_newest_message_back(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    view.page_ref.run_task.reset_mock()

    with list_mounted():
        await view.on_to_bottom_click()

    assert view._follow_bottom is True
    assert view.to_bottom_btn.visible is False
    view.page_ref.run_task.assert_any_call(view._scroll_to_end, 100)


def test_a_new_conversation_takes_the_button_away(view):
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))

    view._reset_conversation()

    assert view.to_bottom_btn.visible is False


def test_the_button_is_wired_to_its_handler(view):
    assert view.to_bottom_btn.on_click == view.on_to_bottom_click


# --------------------------------------------------- coming back to the chat tab
def test_coming_back_to_the_chat_tab_settles_at_the_end(view, mock_page):
    # The client built the list anew, at its top: the reader went up as far as
    # the view knows, and the button had to come back with the end.
    view._on_chat_scroll(scroll_event(USER, direction=UP, pixels=900))
    view.set_active(False)
    mock_page.run_task.reset_mock()

    view.set_active(True)

    assert view._follow_bottom is True
    assert view.to_bottom_btn.visible is False
    mock_page.run_task.assert_called_once_with(
        view._settle_at_end, scroll_module.TAB_RETURN_DELAY
    )


def test_a_chat_tab_that_stays_open_is_not_scrolled(view, mock_page):
    view.set_active(True)

    mock_page.run_task.assert_not_called()


def test_leaving_the_chat_tab_does_not_scroll(view, mock_page):
    view.set_active(False)

    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_the_return_to_the_end_waits_for_the_tab_first(view, monkeypatch):
    pauses: list[float] = []

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(scroll_module.asyncio, "sleep", sleep)
    jumps_that_move(view, count=0)

    with list_mounted():
        await view._settle_at_end(scroll_module.TAB_RETURN_DELAY)

    assert pauses[0] == scroll_module.TAB_RETURN_DELAY
    assert pauses[1:] == [scroll_module.SCROLL_SETTLE_DELAY]
