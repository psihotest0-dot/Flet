from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import flet as ft
import httpx
import pytest
import respx
from tenacity import wait_none

import main as main_module
from api.client import APIClient, APIError, OracleDecision, OracleDecisionPage
from views import oracle as oracle_module
from views.oracle import (
    PAGE_SIZE,
    OracleView,
    decision_details,
    format_latency,
    format_time,
    mode_cell,
    reason_text,
    summary_line,
    verdict_cell,
)

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
BASE_URL = "http://localhost:8080"


def decision(id: int, **fields) -> OracleDecision:
    values = {
        "created_at": datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC),
        "signal_id": f"sig-{id}",
        "symbol": "AAPL",
        "mode": "shadow",
        "status": "answered",
        "verdict": "VETO",
        "reason": "Лоша новина за компанията.",
        "model": "Gemma-4-12B-it-GGUF",
        "attempts": 1,
        "finish_reason": "stop",
        "prompt_tokens": 480,
        "completion_tokens": 20,
        "latency_ms": 1100,
    }
    values.update(fields)
    return OracleDecision(id=id, **values)


def failed(id: int, **fields) -> OracleDecision:
    values = {
        "status": "failed",
        "verdict": None,
        "reason": "",
        "error": "model unavailable: connection refused",
    }
    values.update(fields)
    return decision(id, **values)


def page(*decisions: OracleDecision, next_before_id: int = 0) -> OracleDecisionPage:
    return OracleDecisionPage(decisions=list(decisions), next_before_id=next_before_id)


def make_view(mock_page, mock_api_client, *decisions, next_before_id: int = 0):
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(*decisions, next_before_id=next_before_id)
    )
    return OracleView(mock_page, mock_api_client)


def ids(view: OracleView) -> list[int]:
    return [d.id for d in view.decisions]


def verdict_labels(view: OracleView) -> list[str]:
    """The text of the verdict cell of every row on screen."""
    return [row.cells[2].content.value for row in view.data_table.rows]


@pytest.fixture
def client():
    return APIClient(base_url=BASE_URL, user_id=USER_ID)


@pytest.fixture
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(APIClient._get.retry, "wait", wait_none())


# ------------------------------------------------------------------ the client
@respx.mock
@pytest.mark.asyncio
async def test_journal_is_read_for_the_user_and_parsed(client):
    route = respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(
            200,
            json={
                "decisions": [
                    {
                        "id": 7,
                        "created_at": "2026-11-06T15:00:00Z",
                        "request_id": "req-7",
                        "signal_id": "oracle-probe",
                        "symbol": "AAPL",
                        "mode": "shadow",
                        "status": "answered",
                        "verdict": "VETO",
                        "reason": "изкуствен тест",
                        "model": "Gemma",
                        "attempts": 1,
                        "finish_reason": "stop",
                        "prompt_tokens": 480,
                        "completion_tokens": 20,
                        "latency_ms": 1050,
                    }
                ],
                "next_before_id": 7,
            },
        )
    )

    result = await client.list_oracle_decisions()

    request = route.calls.last.request
    assert request.headers["X-User-ID"] == USER_ID
    assert dict(request.url.params) == {"limit": "50"}
    assert result.next_before_id == 7
    [first] = result.decisions
    assert (first.id, first.verdict, first.symbol, first.latency_ms) == (7, "VETO", "AAPL", 1050)
    assert first.created_at == datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC)


@respx.mock
@pytest.mark.asyncio
async def test_older_page_and_symbol_are_sent_as_query_parameters(client):
    route = respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(200, json={"decisions": [], "next_before_id": 0})
    )

    await client.list_oracle_decisions(limit=20, before_id=41, symbol="NVDA")

    assert dict(route.calls.last.request.url.params) == {
        "limit": "20",
        "before_id": "41",
        "symbol": "NVDA",
    }


@respx.mock
@pytest.mark.asyncio
async def test_no_before_id_and_no_symbol_are_left_out(client):
    route = respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(200, json={"decisions": [], "next_before_id": 0})
    )

    await client.list_oracle_decisions(before_id=0, symbol="")

    # 0 and "" mean "from the newest" and "every symbol"; the backend need not hear them.
    assert dict(route.calls.last.request.url.params) == {"limit": "50"}


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_journal_may_come_as_null(client):
    respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(200, json={"decisions": None, "next_before_id": 0})
    )

    result = await client.list_oracle_decisions()

    assert result.decisions == []
    assert result.next_before_id == 0


@respx.mock
@pytest.mark.asyncio
async def test_a_refused_journal_request_is_final_and_keeps_the_backend_text(
    client, no_retry_delay
):
    route = respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(400, text="symbol is not a stock symbol\n")
    )

    with pytest.raises(APIError) as exc_info:
        await client.list_oracle_decisions(symbol="???")

    assert route.call_count == 1
    assert exc_info.value.status == 400
    assert exc_info.value.message == "symbol is not a stock symbol"


@respx.mock
@pytest.mark.asyncio
async def test_a_failing_backend_is_asked_again_before_giving_up(client, no_retry_delay):
    route = respx.get(f"{BASE_URL}/oracle/decisions").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"decisions": []})]
    )

    result = await client.list_oracle_decisions()

    assert route.call_count == 2
    assert result.decisions == []


@respx.mock
@pytest.mark.asyncio
async def test_reading_the_journal_leaves_no_trace_in_the_log(client, caplog):
    respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(
            200, json={"decisions": [{"id": 1, "reason": "тайна причина"}]}
        )
    )
    caplog.set_level(logging.INFO)

    await client.list_oracle_decisions()

    # Asked every few seconds; and the reason is the model's text about a trade.
    assert "тайна причина" not in caplog.text
    assert not [r for r in caplog.records if r.name == "api.client"]


# ----------------------------------------------------------------- the formats
def test_each_verdict_has_its_own_label_and_colour():
    cells = {
        word: verdict_cell(decision(1, verdict=word))
        for word in ("APPROVE", "VETO", "HOLD")
    }

    assert [cells[word][0] for word in cells] == ["APPROVE", "VETO", "HOLD"]
    assert len({cell[1] for cell in cells.values()}) == 3
    assert all(cell[2] for cell in cells.values())


def test_a_failed_question_says_it_has_no_verdict():
    label, _colour, tooltip = verdict_cell(failed(1))

    assert label == "Неуспех"
    assert "Няма вердикт" in tooltip


def test_a_verdict_the_tab_does_not_know_is_shown_as_it_came():
    assert verdict_cell(decision(1, verdict="MAYBE"))[0] == "MAYBE"
    assert verdict_cell(decision(1, verdict=None))[0] == "—"


def test_the_mode_is_named_in_bulgarian_and_an_unknown_one_stays_as_it_is():
    assert mode_cell("shadow")[0] == "сенчест"
    assert mode_cell("active")[0] == "активен"
    assert mode_cell("confirm") == ("confirm", "")
    assert mode_cell(None) == ("", "")


def test_the_reason_column_of_a_failed_question_shows_the_error():
    assert reason_text(decision(1)) == "Лоша новина за компанията."
    assert reason_text(failed(1)) == "model unavailable: connection refused"
    assert reason_text(failed(1, error=None)) == "Неизвестна грешка"


def test_latency_is_milliseconds_below_a_second_and_seconds_above():
    assert format_latency(None) == ""
    assert format_latency(740) == "740 ms"
    assert format_latency(1050) == "1.1 s"
    assert format_latency(0) == "0 ms"


def test_time_is_shown_in_the_local_time_zone():
    moment = datetime(2026, 11, 6, 15, 0, 5, tzinfo=UTC)

    assert format_time(moment) == moment.astimezone().strftime("%d.%m %H:%M:%S")
    assert format_time(None) == ""


def test_details_carry_the_ids_that_find_the_row_in_the_logs():
    text = decision_details(decision(5, request_id="req-5"))

    assert "Сигнал: sig-5" in text
    assert "Заявка: req-5" in text
    assert "Модел: Gemma-4-12B-it-GGUF" in text
    assert "Токени: 480 вход, 20 изход" in text
    assert "отрязан" not in text


def test_details_say_when_the_answer_was_cut_off():
    text = decision_details(decision(5, finish_reason="length"))

    assert "Край на отговора: length (отговорът е отрязан)" in text


def test_details_of_a_failed_question_name_the_error_and_the_attempts():
    text = decision_details(failed(5, attempts=2))

    assert "Грешка: model unavailable: connection refused" in text
    assert "Опити: 2" in text


def test_the_summary_counts_the_verdicts_and_the_failures_separately():
    rows = [
        decision(1, verdict="APPROVE"),
        decision(2, verdict="VETO"),
        decision(3, verdict="VETO"),
        decision(4, verdict="HOLD"),
        failed(5),
    ]

    assert summary_line(rows) == (
        "В списъка: 5 · APPROVE 1 · VETO 2 · HOLD 1 · неуспешни 1"
    )
    assert summary_line([]) == ""


def test_a_failed_question_is_never_counted_as_a_verdict():
    # The backend leaves the verdict out of a failed row; if one ever carried a
    # word, the question still has no answer.
    rows = [decision(1, verdict="VETO"), failed(2, verdict="VETO")]

    assert summary_line(rows) == (
        "В списъка: 2 · APPROVE 0 · VETO 1 · HOLD 0 · неуспешни 1"
    )


# --------------------------------------------------------------------- loading
@pytest.mark.asyncio
async def test_opening_the_tab_loads_the_newest_page(mock_page, mock_api_client):
    view = make_view(
        mock_page, mock_api_client,
        decision(3, verdict="APPROVE"), decision(2), failed(1),
        next_before_id=0,
    )

    view.did_mount()
    mock_page.run_task.assert_any_call(view.load)
    await view.load()

    mock_api_client.list_oracle_decisions.assert_awaited_once_with(
        limit=PAGE_SIZE, symbol=None
    )
    assert ids(view) == [3, 2, 1]
    assert verdict_labels(view) == ["APPROVE", "VETO", "Неуспех"]
    assert view.summary_text.value.startswith("В списъка: 3")
    assert view.more_btn.visible is False
    assert view.empty_text.visible is False
    assert view.error_text.visible is False


@pytest.mark.asyncio
async def test_a_journal_without_decisions_says_why_it_is_empty(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    await view.load()

    assert view.data_table.rows == []
    assert view.empty_text.visible is True
    assert "Още няма решения" in view.empty_text.value
    assert view.summary_text.value == ""


@pytest.mark.asyncio
async def test_each_row_shows_the_time_symbol_reason_mode_and_latency(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(1, symbol="MSFT"))

    await view.load()

    [row] = view.data_table.rows
    time_cell, symbol, verdict, reason, mode, latency = (c.content for c in row.cells)
    assert time_cell.value == format_time(datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC))
    assert symbol.value == "MSFT"
    assert verdict.value == "VETO"
    assert "Сигнал: sig-1" in verdict.tooltip
    assert reason.value == "Лоша новина за компанията."
    assert reason.tooltip == "Лоша новина за компанията."
    assert mode.value == "сенчест"
    assert latency.value == "1.1 s"


@pytest.mark.asyncio
async def test_a_reason_the_model_wrote_is_shown_as_plain_text(mock_page, mock_api_client):
    # The reason comes from the model, which read news headlines; markup in it
    # must not be rendered or followed.
    view = make_view(
        mock_page, mock_api_client,
        decision(1, reason="**важно** [линк](http://evil.example) <b>x</b>"),
    )

    await view.load()

    reason = view.data_table.rows[0].cells[3].content
    assert isinstance(reason, ft.Text)
    assert reason.value == "**важно** [линк](http://evil.example) <b>x</b>"


@pytest.mark.asyncio
async def test_a_failed_load_keeps_the_rows_and_says_what_went_wrong(
    mock_page, mock_api_client, caplog
):
    view = make_view(mock_page, mock_api_client, decision(2), decision(1))
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        side_effect=httpx.ConnectError("refused")
    )
    caplog.set_level(logging.ERROR)

    await view.load()

    assert ids(view) == [2, 1]
    assert len(view.data_table.rows) == 2
    assert view.error_text.visible is True
    assert "бекендът не е достъпен" in view.error_text.value
    assert "Failed to load the Oracle journal" in caplog.text
    # The spinner is not left turning.
    assert view.progress.visible is False


@pytest.mark.asyncio
async def test_a_load_that_works_again_clears_the_error(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(1))
    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=APIError(500, "boom"))
    await view.load()
    assert view.error_text.visible is True

    mock_api_client.list_oracle_decisions = AsyncMock(return_value=page(decision(1)))
    await view.load()

    assert view.error_text.visible is False
    assert view.error_text.value == ""


@pytest.mark.asyncio
async def test_an_older_load_that_finishes_last_does_not_overwrite_the_newer_one(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return page(decision(1))

    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=slow)
    older = asyncio.create_task(view.load())
    await asyncio.sleep(0)
    mock_api_client.list_oracle_decisions = AsyncMock(return_value=page(decision(2)))
    await view.load()
    release.set()
    await older

    assert ids(view) == [2]


@pytest.mark.asyncio
async def test_a_cancelled_load_is_not_swallowed(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await view.load()


# ---------------------------------------------------------------------- filter
@pytest.mark.asyncio
async def test_the_symbol_is_upper_cased_and_sent_to_the_backend(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "  aapl "

    await view.on_filter_submit(None)

    mock_api_client.list_oracle_decisions.assert_awaited_once_with(
        limit=PAGE_SIZE, symbol="AAPL"
    )
    assert view.symbol_field.value == "AAPL"
    assert view.symbol_field.error_text is None


@pytest.mark.asyncio
async def test_a_symbol_the_backend_would_refuse_is_not_sent(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "AAPL; DROP"

    await view.on_filter_submit(None)

    mock_api_client.list_oracle_decisions.assert_not_awaited()
    assert view.symbol_field.error_text
    assert view._symbol is None


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "BF-B", "X"])
async def test_ordinary_symbols_pass_the_check(mock_page, mock_api_client, symbol):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = symbol

    await view.on_filter_submit(None)

    assert view._symbol == symbol


@pytest.mark.asyncio
async def test_an_empty_field_shows_every_symbol_again(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "AAPL"
    await view.on_filter_submit(None)
    view.symbol_field.value = "   "

    await view.on_filter_submit(None)

    assert view._symbol is None
    assert mock_api_client.list_oracle_decisions.await_args.kwargs["symbol"] is None


@pytest.mark.asyncio
async def test_the_empty_message_names_the_symbol_that_was_asked_for(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "tsla"

    await view.on_filter_submit(None)

    assert view.empty_text.value == "Няма решения за TSLA."


@pytest.mark.asyncio
async def test_a_new_symbol_starts_the_list_over(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(9), decision(8), next_before_id=8)
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(5, symbol="NVDA"))
    )
    view.symbol_field.value = "NVDA"

    await view.on_filter_submit(None)

    assert ids(view) == [5]
    assert view.more_btn.visible is False


# ----------------------------------------------------------------- older pages
@pytest.mark.asyncio
async def test_older_decisions_are_added_below_and_asked_for_by_the_last_id(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(4), decision(3), next_before_id=3)
    await view.load()
    assert view.more_btn.visible is True
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(2), decision(1), next_before_id=0)
    )

    await view.on_more_click(None)

    mock_api_client.list_oracle_decisions.assert_awaited_once_with(
        limit=PAGE_SIZE, before_id=3, symbol=None
    )
    assert ids(view) == [4, 3, 2, 1]
    assert len(view.data_table.rows) == 4
    assert view.more_btn.visible is False


@pytest.mark.asyncio
async def test_older_decisions_keep_the_symbol_filter(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(4), next_before_id=4)
    view.symbol_field.value = "AAPL"
    await view.on_filter_submit(None)
    mock_api_client.list_oracle_decisions = AsyncMock(return_value=page())

    await view.load_more()

    assert mock_api_client.list_oracle_decisions.await_args.kwargs["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_nothing_is_asked_when_there_is_no_older_page(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(1), next_before_id=0)
    await view.load()
    mock_api_client.list_oracle_decisions.reset_mock()

    await view.load_more()

    mock_api_client.list_oracle_decisions.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_decision_that_is_already_listed_is_not_added_twice(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(3), decision(2), next_before_id=2)
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(2), decision(1))
    )

    await view.load_more()

    assert ids(view) == [3, 2, 1]


@pytest.mark.asyncio
async def test_two_clicks_on_the_button_ask_once(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(3), next_before_id=3)
    await view.load()
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return page(decision(2))

    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=slow)
    first = asyncio.create_task(view.load_more())
    await asyncio.sleep(0)
    await view.load_more()
    release.set()
    await first

    assert mock_api_client.list_oracle_decisions.await_count == 1
    assert ids(view) == [3, 2]
    assert view.more_btn.disabled is False


@pytest.mark.asyncio
async def test_older_decisions_that_arrive_after_a_reload_are_dropped(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(10), next_before_id=10)
    await view.load()
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return page(decision(9))

    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=slow)
    older = asyncio.create_task(view.load_more())
    await asyncio.sleep(0)
    # The reader picks another symbol while the older page is on its way.
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(5, symbol="NVDA"))
    )
    view.symbol_field.value = "NVDA"
    await view.on_filter_submit(None)
    release.set()
    await older

    assert ids(view) == [5]


# ------------------------------------------------------------- new decisions
@pytest.mark.asyncio
async def test_new_decisions_go_on_top_and_older_pages_stay(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(3), decision(2), next_before_id=2)
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(5), decision(4), decision(3), decision(2), next_before_id=2)
    )

    await view.refresh_newest()

    assert ids(view) == [5, 4, 3, 2]
    assert len(view.data_table.rows) == 4
    # The way back to the older pages is the one the first load found.
    assert view._next_before_id == 2


@pytest.mark.asyncio
async def test_a_poll_with_nothing_new_leaves_the_table_alone(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client, decision(2), decision(1))
    await view.load()
    rows = list(view.data_table.rows)

    await view.refresh_newest()

    assert view.data_table.rows == rows
    assert all(a is b for a, b in zip(view.data_table.rows, rows, strict=True))
    assert ids(view) == [2, 1]


@pytest.mark.asyncio
async def test_a_page_of_new_decisions_starts_the_list_over_instead_of_leaving_a_hole(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(3), decision(2), next_before_id=2)
    await view.load()
    # Every row of the page is newer than everything on screen and more follow:
    # the decisions between 3 and 10 were never seen.
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(12), decision(11), decision(10), next_before_id=10)
    )

    await view.refresh_newest()

    assert ids(view) == [12, 11, 10]
    assert view._next_before_id == 10


@pytest.mark.asyncio
async def test_the_first_decisions_arrive_in_an_empty_journal(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    await view.load()
    assert view.empty_text.visible is True
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(2), decision(1))
    )

    await view.refresh_newest()

    assert ids(view) == [2, 1]
    assert view.empty_text.visible is False


@pytest.mark.asyncio
async def test_a_poll_that_started_before_a_reload_does_not_overwrite_it(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client, decision(1))
    await view.load()
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return page(decision(9, symbol="OLD"), decision(1))

    mock_api_client.list_oracle_decisions = AsyncMock(side_effect=slow)
    poll = asyncio.create_task(view.refresh_newest())
    await asyncio.sleep(0)
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(5, symbol="NVDA"))
    )
    view.symbol_field.value = "NVDA"
    await view.on_filter_submit(None)
    release.set()
    await poll

    assert ids(view) == [5]


@pytest.mark.asyncio
async def test_the_poll_sleeps_then_refreshes_while_the_tab_is_visible(
    mock_page, mock_api_client, monkeypatch
):
    view = make_view(mock_page, mock_api_client, decision(1))
    await view.load()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            view._polling = False

    monkeypatch.setattr(oracle_module.asyncio, "sleep", fake_sleep)
    view._polling = True
    mock_api_client.list_oracle_decisions.reset_mock()

    await view.poll()

    assert sleeps == [oracle_module.POLL_INTERVAL] * 2
    assert mock_api_client.list_oracle_decisions.await_count == 1


@pytest.mark.asyncio
async def test_a_failing_poll_is_logged_once_and_shown_until_it_works_again(
    mock_page, mock_api_client, monkeypatch, caplog
):
    view = make_view(mock_page, mock_api_client, decision(1))
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        side_effect=[httpx.ConnectError("down"), httpx.ConnectError("down"), page(decision(1))]
    )
    rounds = 0

    async def fake_sleep(_seconds):
        nonlocal rounds
        rounds += 1
        if rounds > 3:
            view._polling = False

    monkeypatch.setattr(oracle_module.asyncio, "sleep", fake_sleep)
    view._polling = True
    caplog.set_level(logging.WARNING)
    shown = []
    original = view._set_error
    view._set_error = lambda message: (shown.append(message), original(message))[1]

    await view.poll()

    warnings = [r for r in caplog.records if "Could not refresh" in r.getMessage()]
    assert len(warnings) == 1
    assert shown[0] and "не може да се обнови" in shown[0]
    # The third poll worked, so the line is gone.
    assert shown[-1] is None
    assert view.error_text.visible is False
    assert ids(view) == [1]


# ------------------------------------------------------------------- lifecycle
def test_polling_runs_only_while_the_tab_is_visible(mock_page, mock_api_client):
    view = OracleView(mock_page, mock_api_client)
    view.did_mount()
    assert [c for c in mock_page.run_task.call_args_list if c.args[0] == view.poll] == []

    view.set_active(True)
    mock_page.run_task.assert_any_call(view.poll)
    assert view._polling is True

    task = view._poll_task
    view.set_active(False)
    assert view._polling is False
    assert view._poll_task is None
    task.cancel.assert_called_once()


def test_activating_twice_starts_one_poll(mock_page, mock_api_client):
    view = OracleView(mock_page, mock_api_client)

    view.set_active(True)
    view.set_active(True)

    polls = [c for c in mock_page.run_task.call_args_list if c.args[0] == view.poll]
    assert len(polls) == 1


def test_a_view_that_was_made_visible_before_it_was_mounted_starts_polling_on_mount(
    mock_page, mock_api_client
):
    view = OracleView(mock_page, mock_api_client)
    view._active = True

    view.did_mount()

    mock_page.run_task.assert_any_call(view.load)
    mock_page.run_task.assert_any_call(view.poll)


def test_leaving_stops_everything(mock_page, mock_api_client):
    # One handle per started task, as the real run_task gives.
    mock_page.run_task.side_effect = lambda *_args: MagicMock()
    view = OracleView(mock_page, mock_api_client)
    view.did_mount()
    view.set_active(True)
    load_task, poll_task = view._load_task, view._poll_task

    view.will_unmount()

    load_task.cancel.assert_called_once()
    poll_task.cancel.assert_called_once()
    assert view._polling is False
    assert view._poll_task is None


# ---------------------------------------------------------------------- the tab
@pytest.fixture
def start_app(monkeypatch, mock_page):
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)
    monkeypatch.setattr(main_module, "wrap_run_task", lambda page: None)
    monkeypatch.setattr(ft.Tabs, "update", lambda self: None)

    def start() -> ft.Tabs:
        main_module.main(mock_page)
        return mock_page.add.call_args.args[0]

    return start


def select_tab(tabs: ft.Tabs, index: int) -> None:
    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=index)))


def test_the_bot_is_the_fourth_tab_and_is_built_when_first_opened(start_app):
    tabs = start_app()

    assert [tab.text for tab in tabs.tabs] == ["Чат", "Памет", "Файлове", "Бот"]
    assert tabs.tabs[3].content is None

    select_tab(tabs, 3)

    assert isinstance(tabs.tabs[3].content, OracleView)


def test_opening_the_bot_tab_the_first_time_starts_its_poll_but_does_not_load_twice(
    start_app, mock_page
):
    tabs = start_app()
    mock_page.run_task.reset_mock()

    select_tab(tabs, 3)
    view = tabs.tabs[3].content

    # The first load is the view's own (when it is mounted).
    mock_page.run_task.assert_called_once_with(view.poll)


def test_the_bot_tab_is_read_again_each_time_it_is_opened_again(start_app, mock_page):
    tabs = start_app()
    select_tab(tabs, 3)
    view = tabs.tabs[3].content
    select_tab(tabs, 0)
    mock_page.run_task.reset_mock()

    select_tab(tabs, 3)

    assert mock_page.run_task.call_args_list[0].args == (view.load,)
    assert tabs.tabs[3].content is view


def test_the_bot_polls_only_while_its_tab_is_open(start_app):
    tabs = start_app()
    select_tab(tabs, 3)
    view = tabs.tabs[3].content
    assert view._polling is True

    select_tab(tabs, 0)

    assert view._polling is False


def test_switching_between_files_and_the_bot_swaps_the_pollers(start_app):
    tabs = start_app()
    select_tab(tabs, 2)
    files_view = tabs.tabs[2].content
    assert files_view._polling is True

    select_tab(tabs, 3)
    bot_view = tabs.tabs[3].content

    assert files_view._polling is False
    assert bot_view._polling is True

    select_tab(tabs, 2)

    assert files_view._polling is True
    assert bot_view._polling is False


def test_a_new_base_url_reaches_the_bot_tab(start_app, monkeypatch):
    captured = {}

    class Spy:
        def __init__(self, page, user_id, base_url, on_save, display_name="Ти"):
            captured["on_save"] = on_save

        def open(self, base_url, display_name="Ти"):
            pass

    monkeypatch.setattr(main_module, "SettingsDialog", Spy)
    tabs = start_app()
    select_tab(tabs, 3)
    view = tabs.tabs[3].content
    previous = view.api_client

    captured["on_save"]("http://127.0.0.1:9000", "Ти")

    assert view.api_client is not previous
    assert view.api_client.base_url == "http://127.0.0.1:9000"
