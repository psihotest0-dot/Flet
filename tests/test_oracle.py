from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import flet as ft
import httpx
import pytest
import respx
from helpers import tab_labels, tab_view
from tenacity import wait_none

import main as main_module
from api.client import APIClient, APIError, OracleDecision, OracleDecisionPage
from views import oracle as oracle_module
from views.oracle import (
    AVAILABILITY_TARGET,
    EVALUATION_START,
    PAGE_SIZE,
    PROGRESS_PAGE_SIZE,
    OracleView,
    Progress,
    count_progress,
    decision_details,
    format_latency,
    format_time,
    mode_cell,
    reason_text,
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


def signal(id: int, verdict: str | None = "VETO", **fields) -> OracleDecision:
    """A shadow-mode question after the start of the evaluation, one signal per row."""
    values = {
        "created_at": EVALUATION_START + timedelta(minutes=id),
        "mode": "shadow",
        "verdict": verdict,
        "signal_id": f"signal-{id}",
    }
    values.update(fields)
    return decision(id, **values)


class FakeJournal:
    """The journal as the backend serves it: newest first, paged by ``before_id``."""

    def __init__(self, *rows: OracleDecision):
        self.rows = sorted(rows, key=lambda row: -row.id)
        self.calls: list[dict] = []

    async def __call__(self, limit=50, before_id=None, symbol=None):
        self.calls.append({"limit": limit, "before_id": before_id, "symbol": symbol})
        matching = [
            row
            for row in self.rows
            if (not before_id or row.id < before_id)
            and (not symbol or row.symbol == symbol)
        ]
        shown = matching[:limit]
        next_id = shown[-1].id if len(shown) == limit else 0
        return OracleDecisionPage(decisions=shown, next_before_id=next_id)

    def progress_calls(self) -> list[dict]:
        return [call for call in self.calls if call["limit"] == PROGRESS_PAGE_SIZE]


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
    assert (first.id, first.verdict, first.symbol, first.latency_ms) == (
        7,
        "VETO",
        "AAPL",
        1050,
    )
    assert first.created_at == datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC)


@respx.mock
@pytest.mark.asyncio
async def test_the_version_of_the_signal_and_of_the_prompt_are_read_when_present(
    client,
):
    respx.get(f"{BASE_URL}/oracle/decisions").mock(
        return_value=httpx.Response(
            200,
            json={
                "decisions": [
                    {
                        "id": 10,
                        "signal_version": "oracle-probe",
                        "prompt_version": "8a4f4388a9c7",
                    },
                    {"id": 9, "signal_version": "1.0.0"},
                    {"id": 8},
                ],
                "next_before_id": 0,
            },
        )
    )

    result = await client.list_oracle_decisions()

    assert [(d.signal_version, d.prompt_version) for d in result.decisions] == [
        ("oracle-probe", "8a4f4388a9c7"),
        ("1.0.0", None),
        (None, None),
    ]


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
async def test_a_failing_backend_is_asked_again_before_giving_up(
    client, no_retry_delay
):
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


def test_details_name_the_version_of_the_prompt_when_the_backend_sends_it():
    assert "Промпт: 3f9a1c0b2d4e" in decision_details(
        decision(5, prompt_version="3f9a1c0b2d4e")
    )
    assert "Промпт" not in decision_details(decision(5))


# -------------------------------------------------------------- the counters
def test_an_empty_journal_has_no_progress_and_no_availability():
    progress = count_progress([])

    assert progress == Progress()
    assert progress.availability is None
    assert progress.reached is False


def test_each_signal_counts_once_by_its_latest_final_answer():
    rows = [
        signal(1, "HOLD", signal_id="a"),
        signal(2, "HOLD", signal_id="a"),
        signal(3, "APPROVE", signal_id="a"),
        signal(4, "VETO", signal_id="b"),
        signal(5, "APPROVE", signal_id="c"),
        signal(6, "VETO", signal_id="c"),
    ]

    progress = count_progress(rows)

    # a: HOLD, HOLD, then APPROVE; b: VETO; c: APPROVE and then VETO - the later one.
    assert (progress.approve, progress.veto) == (1, 2)
    assert (progress.asked, progress.answered) == (3, 3)


def test_a_hold_after_a_final_answer_does_not_take_it_back():
    rows = [signal(1, "VETO", signal_id="a"), signal(2, "HOLD", signal_id="a")]

    progress = count_progress(rows)

    assert (progress.veto, progress.answered) == (1, 1)


def test_the_order_of_the_rows_does_not_matter():
    rows = [signal(1, "APPROVE", signal_id="a"), signal(2, "VETO", signal_id="a")]

    assert count_progress(rows) == count_progress(list(reversed(rows)))
    assert count_progress(rows).veto == 1


def test_a_signal_that_never_got_a_final_answer_is_asked_but_not_answered():
    rows = [
        signal(1, "HOLD", signal_id="a"),
        failed(
            2,
            mode="shadow",
            signal_id="b",
            created_at=EVALUATION_START + timedelta(hours=1),
        ),
        signal(3, "APPROVE", signal_id="c"),
    ]

    progress = count_progress(rows)

    assert (progress.asked, progress.answered, progress.approve) == (3, 1, 1)
    assert progress.availability == pytest.approx(1 / 3)


def test_a_failed_row_is_never_a_final_answer_even_with_a_verdict_on_it():
    rows = [failed(1, mode="shadow", verdict="VETO", created_at=EVALUATION_START)]

    progress = count_progress(rows)

    assert (progress.asked, progress.answered, progress.veto) == (1, 0, 0)


def test_only_shadow_mode_counts():
    rows = [signal(1, "VETO", mode="active"), signal(2, "VETO", mode="shadow")]

    assert count_progress(rows).asked == 1


def test_rows_before_the_start_of_the_evaluation_do_not_count():
    before = EVALUATION_START - timedelta(seconds=1)
    rows = [
        signal(1, "VETO", created_at=before),
        signal(2, "VETO", created_at=EVALUATION_START),
    ]

    progress = count_progress(rows)

    assert (progress.asked, progress.veto) == (1, 1)


def test_a_row_without_a_time_or_a_signal_cannot_be_counted():
    rows = [signal(1, "VETO", created_at=None), signal(2, "VETO", signal_id=None)]

    assert count_progress(rows) == Progress()


def test_a_probe_is_left_out_once_the_backend_says_which_rows_are_probes():
    rows = [
        signal(1, "VETO", signal_version="oracle-probe"),
        signal(2, "VETO", signal_version="1.0.0"),
        signal(3, "APPROVE", signal_version="1.0.0"),
    ]

    progress = count_progress(rows)

    assert (progress.asked, progress.veto, progress.approve) == (2, 1, 1)
    assert progress.probes_unmarked is False


def test_probes_are_not_guessed_while_the_backend_does_not_say():
    # Two VETO rows of the same stock, one of them a probe in reality - nothing in
    # the row tells which, so both count and the block says so.
    rows = [signal(1, "VETO"), signal(2, "VETO")]

    progress = count_progress(rows)

    assert progress.veto == 2
    assert progress.probes_unmarked is True


def test_the_probes_note_is_not_needed_before_there_is_a_signal():
    assert count_progress([]).probes_unmarked is False


def test_the_minimum_needs_all_three_bars():
    def many(approve, veto, silent):
        rows = [
            signal(i, "APPROVE", signal_version="1.0.0") for i in range(1, approve + 1)
        ]
        rows += [signal(1000 + i, "VETO", signal_version="1.0.0") for i in range(veto)]
        rows += [
            signal(2000 + i, "HOLD", signal_version="1.0.0") for i in range(silent)
        ]
        return count_progress(rows)

    assert many(30, 30, 0).reached is True
    assert many(29, 30, 0).reached is False
    assert many(30, 29, 0).reached is False
    # 60 answered and 14 never answered: 81 %.
    assert many(30, 30, 14).availability == pytest.approx(60 / 74)
    assert many(30, 30, 14).reached is True
    # 60 answered and 16 never answered: 79 %.
    assert many(30, 30, 16).availability < AVAILABILITY_TARGET
    assert many(30, 30, 16).reached is False


# --------------------------------------------------------------------- loading
@pytest.mark.asyncio
async def test_opening_the_tab_loads_the_newest_page(mock_page, mock_api_client):
    view = make_view(
        mock_page,
        mock_api_client,
        decision(3, verdict="APPROVE"),
        decision(2),
        failed(1),
        next_before_id=0,
    )

    view.did_mount()
    mock_page.run_task.assert_any_call(view.refresh)
    await view.load()

    mock_api_client.list_oracle_decisions.assert_awaited_once_with(
        limit=PAGE_SIZE, symbol=None
    )
    assert ids(view) == [3, 2, 1]
    assert verdict_labels(view) == ["APPROVE", "VETO", "Неуспех"]
    assert view.more_btn.visible is False
    assert view.empty_text.visible is False
    assert view.error_text.visible is False


@pytest.mark.asyncio
async def test_a_journal_without_decisions_says_why_it_is_empty(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)

    await view.load()

    assert view.data_table.rows == []
    assert view.empty_text.visible is True
    assert "Още няма решения" in view.empty_text.value


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
async def test_a_reason_the_model_wrote_is_shown_as_plain_text(
    mock_page, mock_api_client
):
    # The reason comes from the model, which read news headlines; markup in it
    # must not be rendered or followed.
    view = make_view(
        mock_page,
        mock_api_client,
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
    mock_api_client.list_oracle_decisions = AsyncMock(
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await view.load()


# ---------------------------------------------------------------------- filter
@pytest.mark.asyncio
async def test_the_symbol_is_upper_cased_and_sent_to_the_backend(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "  aapl "

    await view.on_filter_submit(None)

    mock_api_client.list_oracle_decisions.assert_awaited_once_with(
        limit=PAGE_SIZE, symbol="AAPL"
    )
    assert view.symbol_field.value == "AAPL"
    assert view.symbol_field.error is None


@pytest.mark.asyncio
async def test_a_symbol_the_backend_would_refuse_is_not_sent(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    view.symbol_field.value = "AAPL; DROP"

    await view.on_filter_submit(None)

    mock_api_client.list_oracle_decisions.assert_not_awaited()
    assert view.symbol_field.error
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
    view = make_view(
        mock_page, mock_api_client, decision(9), decision(8), next_before_id=8
    )
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(decision(5, symbol="NVDA"))
    )
    view.symbol_field.value = "NVDA"

    await view.on_filter_submit(None)

    assert ids(view) == [5]
    assert view.more_btn.visible is False


# ------------------------------------------- the filter, the field and "Обнови"
def two_symbols(mock_page, mock_api_client):
    """A journal with two symbols: ids 3 and 1 are AAPL, id 2 is MSFT."""
    journal = FakeJournal(
        signal(1, "APPROVE", symbol="AAPL"),
        signal(2, "VETO", symbol="MSFT"),
        signal(3, "VETO", symbol="AAPL"),
    )
    mock_api_client.list_oracle_decisions = journal
    return OracleView(mock_page, mock_api_client), journal


async def filtered_on_aapl(mock_page, mock_api_client):
    view, journal = two_symbols(mock_page, mock_api_client)
    await view.refresh()
    view.symbol_field.value = "AAPL"
    await view.on_filter_submit(None)
    assert ids(view) == [3, 1]
    return view, journal


@pytest.mark.asyncio
async def test_refresh_after_the_field_was_emptied_shows_every_symbol_again(
    mock_page, mock_api_client
):
    # The reported bug: the filter lived on after the text was deleted, and "Обнови"
    # kept asking for the old symbol while the field looked empty.
    view, _journal = await filtered_on_aapl(mock_page, mock_api_client)
    view.symbol_field.value = ""

    await view.on_refresh_click(None)

    assert view._symbol is None
    assert ids(view) == [3, 2, 1]


@pytest.mark.asyncio
async def test_emptying_the_field_shows_every_symbol_without_pressing_anything(
    mock_page, mock_api_client
):
    view, _journal = await filtered_on_aapl(mock_page, mock_api_client)
    view.symbol_field.value = "  "

    await view.on_symbol_change(None)

    assert view._symbol is None
    assert ids(view) == [3, 2, 1]


@pytest.mark.asyncio
async def test_typing_into_the_field_does_not_ask_the_backend(
    mock_page, mock_api_client
):
    view, journal = await filtered_on_aapl(mock_page, mock_api_client)
    calls = len(journal.calls)

    view.symbol_field.value = "AAP"
    await view.on_symbol_change(None)
    view.symbol_field.value = "MS"
    await view.on_symbol_change(None)

    assert len(journal.calls) == calls
    assert view._symbol == "AAPL"


@pytest.mark.asyncio
async def test_emptying_a_field_that_filtered_nothing_asks_nothing(
    mock_page, mock_api_client
):
    view, journal = two_symbols(mock_page, mock_api_client)
    await view.refresh()
    calls = len(journal.calls)
    view.symbol_field.value = ""

    await view.on_symbol_change(None)

    assert len(journal.calls) == calls


@pytest.mark.asyncio
async def test_emptying_the_field_takes_the_complaint_about_a_bad_symbol_away(
    mock_page, mock_api_client
):
    view, _journal = two_symbols(mock_page, mock_api_client)
    view.symbol_field.value = "AAPL; DROP"
    await view.on_filter_submit(None)
    assert view.symbol_field.error
    view.symbol_field.value = ""

    await view.on_symbol_change(None)

    # Flet hands back "" for an error text that was set and then cleared.
    assert not view.symbol_field.error


@pytest.mark.asyncio
async def test_refresh_uses_the_symbol_that_stands_in_the_field(
    mock_page, mock_api_client
):
    view, _journal = two_symbols(mock_page, mock_api_client)
    await view.refresh()
    view.symbol_field.value = " msft "

    await view.on_refresh_click(None)

    assert view._symbol == "MSFT"
    assert view.symbol_field.value == "MSFT"
    assert ids(view) == [2]


@pytest.mark.asyncio
async def test_refresh_with_a_symbol_the_backend_would_refuse_asks_nothing(
    mock_page, mock_api_client
):
    view, journal = await filtered_on_aapl(mock_page, mock_api_client)
    calls = len(journal.calls)
    view.symbol_field.value = "AAPL; DROP"

    await view.on_refresh_click(None)

    assert len(journal.calls) == calls
    assert view.symbol_field.error
    assert view._symbol == "AAPL"
    assert ids(view) == [3, 1]


@pytest.mark.asyncio
async def test_the_counters_do_not_follow_the_symbol_filter(mock_page, mock_api_client):
    # The evaluation counts every signal of the bot, whatever the table shows.
    view, journal = two_symbols(mock_page, mock_api_client)
    await view.refresh()
    whole = (
        view.approve_value.value,
        view.veto_value.value,
        view.availability_value.value,
    )
    assert whole[:2] == ("1 / 30", "2 / 30")

    view.symbol_field.value = "MSFT"
    await view.on_filter_submit(None)
    await view.on_refresh_click(None)
    during = (
        view.approve_value.value,
        view.veto_value.value,
        view.availability_value.value,
    )
    view.symbol_field.value = ""
    await view.on_refresh_click(None)
    after = (
        view.approve_value.value,
        view.veto_value.value,
        view.availability_value.value,
    )

    assert during == whole
    assert after == whole
    assert all(call["symbol"] is None for call in journal.progress_calls())


# ----------------------------------------------------------------- older pages
@pytest.mark.asyncio
async def test_older_decisions_are_added_below_and_asked_for_by_the_last_id(
    mock_page, mock_api_client
):
    view = make_view(
        mock_page, mock_api_client, decision(4), decision(3), next_before_id=3
    )
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
    view = make_view(
        mock_page, mock_api_client, decision(3), decision(2), next_before_id=2
    )
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
    view = make_view(
        mock_page, mock_api_client, decision(3), decision(2), next_before_id=2
    )
    await view.load()
    mock_api_client.list_oracle_decisions = AsyncMock(
        return_value=page(
            decision(5), decision(4), decision(3), decision(2), next_before_id=2
        )
    )

    await view.refresh_newest()

    assert ids(view) == [5, 4, 3, 2]
    assert len(view.data_table.rows) == 4
    # The way back to the older pages is the one the first load found.
    assert view._next_before_id == 2


@pytest.mark.asyncio
async def test_a_poll_with_nothing_new_leaves_the_table_alone(
    mock_page, mock_api_client
):
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
    view = make_view(
        mock_page, mock_api_client, decision(3), decision(2), next_before_id=2
    )
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
async def test_the_first_decisions_arrive_in_an_empty_journal(
    mock_page, mock_api_client
):
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
    # One round: the newest rows of the table, then the counters.
    limits = [
        call.kwargs["limit"]
        for call in mock_api_client.list_oracle_decisions.await_args_list
    ]
    assert limits == [PAGE_SIZE, PROGRESS_PAGE_SIZE]


@pytest.mark.asyncio
async def test_a_failing_poll_is_logged_once_and_shown_until_it_works_again(
    mock_page, mock_api_client, monkeypatch, caplog
):
    view = make_view(mock_page, mock_api_client, decision(1))
    await view.load()
    table_reads = 0

    async def journal(limit=50, before_id=None, symbol=None):
        nonlocal table_reads
        if limit == PAGE_SIZE:
            table_reads += 1
            if table_reads <= 2:
                raise httpx.ConnectError("down")
        return page(decision(1))

    mock_api_client.list_oracle_decisions = journal
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


# ---------------------------------------------------- the counters on screen
def bars(view: OracleView) -> dict[str, tuple[float, str]]:
    return {
        "approve": (view.approve_bar.value, view.approve_value.value),
        "veto": (view.veto_bar.value, view.veto_value.value),
        "availability": (view.availability_bar.value, view.availability_value.value),
    }


@pytest.mark.asyncio
async def test_the_block_shows_the_counts_toward_the_minimum(
    mock_page, mock_api_client
):
    journal = FakeJournal(
        signal(1, "APPROVE", signal_version="1.0.0"),
        signal(2, "APPROVE", signal_version="1.0.0"),
        signal(3, "VETO", signal_version="1.0.0"),
        signal(4, "HOLD", signal_version="1.0.0"),
        signal(5, "VETO", signal_version="oracle-probe"),
    )
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)

    await view.refresh()

    shown = bars(view)
    assert shown["approve"] == (pytest.approx(2 / 30), "2 / 30")
    assert shown["veto"] == (pytest.approx(1 / 30), "1 / 30")
    assert shown["availability"][0] == pytest.approx(0.75)
    assert "75 % (3 от 4 сигнала)" in shown["availability"][1]
    assert "Под минимума няма присъда" in view.progress_status.value
    assert view.probes_note.visible is False


@pytest.mark.asyncio
async def test_the_block_says_so_when_the_probes_cannot_be_told_apart(
    mock_page, mock_api_client
):
    mock_api_client.list_oracle_decisions = FakeJournal(
        signal(1, "VETO"), signal(2, "APPROVE")
    )
    view = OracleView(mock_page, mock_api_client)

    await view.refresh_progress(rebuild=True)

    assert view.probes_note.visible is True
    assert view.veto_value.value == "1 / 30"


@pytest.mark.asyncio
async def test_before_any_signal_the_block_says_there_are_none(
    mock_page, mock_api_client
):
    mock_api_client.list_oracle_decisions = FakeJournal()
    view = OracleView(mock_page, mock_api_client)

    await view.refresh_progress(rebuild=True)

    assert bars(view)["availability"] == (0, "още няма сигнали")
    assert view.approve_value.value == "0 / 30"
    assert view.probes_note.visible is False


@pytest.mark.asyncio
async def test_the_block_announces_the_minimum_but_leaves_the_verdict_to_the_judge(
    mock_page, mock_api_client
):
    rows = [signal(i, "APPROVE", signal_version="1.0.0") for i in range(1, 31)]
    rows += [signal(100 + i, "VETO", signal_version="1.0.0") for i in range(30)]
    mock_api_client.list_oracle_decisions = FakeJournal(*rows)
    view = OracleView(mock_page, mock_api_client)

    await view.refresh_progress(rebuild=True)

    assert view.approve_bar.value == 1 and view.veto_bar.value == 1
    assert "достигнат" in view.progress_status.value
    assert "judge-oracle" in view.progress_status.value


def test_the_block_names_the_judge_as_the_authority_and_the_start_of_the_period(
    mock_page, mock_api_client
):
    view = OracleView(mock_page, mock_api_client)

    caption = view.progress_block.content.controls[1].value

    assert "judge-oracle" in caption
    assert "06.11.2026 16:00 UTC" in caption
    assert "без проби" in caption


@pytest.mark.asyncio
async def test_the_journal_is_read_back_to_the_start_of_the_evaluation_in_big_pages(
    mock_page, mock_api_client
):
    # 450 rows of the evaluation (ids 10-459), then 3 older ones (ids 1-3) that
    # must not be read past.
    old = [
        decision(
            i,
            signal_id=f"old-{i}",
            mode="shadow",
            created_at=EVALUATION_START - timedelta(days=1),
        )
        for i in (1, 2, 3)
    ]
    rows = [
        signal(i + 10, "VETO", signal_id=f"s{i}", signal_version="1.0.0")
        for i in range(450)
    ]
    journal = FakeJournal(*old, *rows)
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)

    await view.refresh_progress(rebuild=True)

    calls = journal.progress_calls()
    assert [call["limit"] for call in calls] == [PROGRESS_PAGE_SIZE] * 3
    assert [call["before_id"] for call in calls] == [None, 260, 60]
    assert all(call["symbol"] is None for call in calls)
    assert view.veto_value.value == "450 / 30"
    assert view.veto_bar.value == 1


@pytest.mark.asyncio
async def test_the_counters_ignore_the_symbol_filter_of_the_table(
    mock_page, mock_api_client
):
    journal = FakeJournal(
        signal(1, "VETO", symbol="AAPL", signal_version="1.0.0"),
        signal(2, "APPROVE", symbol="NVDA", signal_version="1.0.0"),
    )
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)
    view.symbol_field.value = "AAPL"
    await view.on_filter_submit(None)

    await view.refresh()

    assert [row.cells[1].content.value for row in view.data_table.rows] == ["AAPL"]
    assert view.veto_value.value == "1 / 30"
    assert view.approve_value.value == "1 / 30"


@pytest.mark.asyncio
async def test_a_poll_reads_only_the_rows_it_has_not_seen(mock_page, mock_api_client):
    rows = [signal(i, "VETO", signal_version="1.0.0") for i in range(1, 6)]
    journal = FakeJournal(*rows)
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)
    await view.refresh_progress(rebuild=True)
    assert view.veto_value.value == "5 / 30"
    journal.calls.clear()
    journal.rows.insert(0, signal(6, "APPROVE", signal_version="1.0.0"))

    await view.refresh_progress()

    assert len(journal.progress_calls()) == 1
    assert view.veto_value.value == "5 / 30"
    assert view.approve_value.value == "1 / 30"


@pytest.mark.asyncio
async def test_reading_stops_at_the_newest_row_that_is_already_known(
    mock_page, mock_api_client
):
    mock_api_client.list_oracle_decisions = FakeJournal(
        *[signal(i, "VETO", signal_version="1.0.0") for i in range(1, 6)]
    )
    view = OracleView(mock_page, mock_api_client)

    fresh = await view._read_journal_since(3)

    # Row 3 is known; it is neither read again nor are the older ones.
    assert [row.id for row in fresh] == [5, 4]


@pytest.mark.asyncio
async def test_a_signal_answered_later_changes_the_count_it_was_in(
    mock_page, mock_api_client
):
    journal = FakeJournal(signal(1, "HOLD", signal_id="a", signal_version="1.0.0"))
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)
    await view.refresh_progress(rebuild=True)
    assert (view.approve_value.value, view.veto_value.value) == ("0 / 30", "0 / 30")

    # The same signal is asked again on the next tick and now gets an answer.
    journal.rows.insert(0, signal(2, "VETO", signal_id="a", signal_version="1.0.0"))
    await view.refresh_progress()

    assert view.veto_value.value == "1 / 30"
    assert "100 % (1 от 1 сигнала)" in view.availability_value.value


@pytest.mark.asyncio
async def test_a_full_refresh_forgets_rows_the_backend_no_longer_has(
    mock_page, mock_api_client
):
    journal = FakeJournal(
        signal(1, "VETO", signal_version="1.0.0"),
        signal(2, "VETO", signal_version="1.0.0"),
    )
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)
    await view.refresh_progress(rebuild=True)
    journal.rows = [row for row in journal.rows if row.id != 2]

    await view.refresh_progress(rebuild=True)

    assert view.veto_value.value == "1 / 30"


@pytest.mark.asyncio
async def test_a_page_that_does_not_go_back_ends_the_reading(
    mock_page, mock_api_client
):
    rows = [signal(i, "VETO", signal_version="1.0.0") for i in (5, 4)]
    calls = []

    async def stuck(limit=50, before_id=None, symbol=None):
        calls.append(before_id)
        # Always the same page, always pointing at the same "next" page.
        return page(*rows, next_before_id=4)

    mock_api_client.list_oracle_decisions = stuck
    view = OracleView(mock_page, mock_api_client)

    await asyncio.wait_for(view.refresh_progress(rebuild=True), timeout=5)

    assert calls == [None, 4]


@pytest.mark.asyncio
async def test_counters_that_cannot_be_read_keep_the_old_numbers_and_say_why(
    mock_page, mock_api_client, caplog
):
    journal = FakeJournal(signal(1, "VETO", signal_version="1.0.0"))
    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)
    await view.refresh_progress(rebuild=True)
    mock_api_client.list_oracle_decisions = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )
    caplog.set_level(logging.WARNING)

    await view.refresh_progress()
    await view.refresh_progress()

    assert view.progress_error.visible is True
    assert "не могат да се обновят" in view.progress_error.value
    assert view.veto_value.value == "1 / 30"
    # Logged once when it starts, not once per poll.
    assert len([r for r in caplog.records if "for the counters" in r.getMessage()]) == 1

    mock_api_client.list_oracle_decisions = journal
    await view.refresh_progress()

    assert view.progress_error.visible is False


@pytest.mark.asyncio
async def test_a_failing_counter_read_does_not_break_the_table(
    mock_page, mock_api_client
):
    async def journal(limit=50, before_id=None, symbol=None):
        if limit == PROGRESS_PAGE_SIZE:
            raise APIError(500, "boom")
        return page(decision(2), decision(1))

    mock_api_client.list_oracle_decisions = journal
    view = OracleView(mock_page, mock_api_client)

    await view.refresh()

    assert ids(view) == [2, 1]
    assert view.error_text.visible is False
    assert view.progress_error.visible is True


@pytest.mark.asyncio
async def test_a_cancelled_counter_read_is_not_swallowed(mock_page, mock_api_client):
    mock_api_client.list_oracle_decisions = AsyncMock(
        side_effect=asyncio.CancelledError
    )
    view = OracleView(mock_page, mock_api_client)

    with pytest.raises(asyncio.CancelledError):
        await view.refresh_progress(rebuild=True)


@pytest.mark.asyncio
async def test_the_block_shows_no_trade_outcome(mock_page, mock_api_client):
    # "No peeking" (docs/oracle-provider-task.md, section 5): until the minimum is
    # reached only counts and availability are shown. Everything the block says
    # is a count, a target or an explanation.
    rows = [signal(i, "VETO", signal_version="1.0.0") for i in range(1, 4)]
    mock_api_client.list_oracle_decisions = FakeJournal(*rows)
    view = OracleView(mock_page, mock_api_client)
    await view.refresh_progress(rebuild=True)

    texts = [
        control.value
        for control in view.progress_block.content.controls
        if isinstance(control, ft.Text)
    ]
    texts += [
        view.approve_value.value,
        view.veto_value.value,
        view.availability_value.value,
    ]
    joined = " ".join(texts).lower()

    for word in (
        "доход",
        "печалб",
        "загуба",
        "p&l",
        "profit",
        "return",
        "резултат на сдел",
    ):
        assert word not in joined


# ------------------------------------------------------------------- lifecycle
def test_polling_runs_only_while_the_tab_is_visible(mock_page, mock_api_client):
    view = OracleView(mock_page, mock_api_client)
    view.did_mount()
    assert [
        c for c in mock_page.run_task.call_args_list if c.args[0] == view.poll
    ] == []

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

    mock_page.run_task.assert_any_call(view.refresh)
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

    def start() -> ft.Tabs:
        main_module.main(mock_page)
        return mock_page.add.call_args.args[0]

    return start


def select_tab(tabs: ft.Tabs, index: int) -> None:
    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=index)))


def test_the_bot_is_the_fourth_tab_and_is_built_when_first_opened(start_app):
    tabs = start_app()

    assert tab_labels(tabs) == [
        "Чат",
        "Памет",
        "Файлове",
        "Бот",
        "Файлове на Лили",
    ]
    # Until it is opened, an empty container holds its place.
    assert not isinstance(tab_view(tabs, 3), OracleView)

    select_tab(tabs, 3)

    assert isinstance(tab_view(tabs, 3), OracleView)


def test_opening_the_bot_tab_the_first_time_starts_its_poll_but_does_not_load_twice(
    start_app, mock_page
):
    tabs = start_app()
    mock_page.run_task.reset_mock()

    select_tab(tabs, 3)
    view = tab_view(tabs, 3)

    # The first load is the view's own (when it is mounted).
    mock_page.run_task.assert_called_once_with(view.poll)


def test_the_bot_tab_is_read_again_each_time_it_is_opened_again(start_app, mock_page):
    tabs = start_app()
    select_tab(tabs, 3)
    view = tab_view(tabs, 3)
    select_tab(tabs, 0)
    mock_page.run_task.reset_mock()

    select_tab(tabs, 3)

    assert mock_page.run_task.call_args_list[0].args == (view.refresh,)
    assert tab_view(tabs, 3) is view


def test_the_bot_polls_only_while_its_tab_is_open(start_app):
    tabs = start_app()
    select_tab(tabs, 3)
    view = tab_view(tabs, 3)
    assert view._polling is True

    select_tab(tabs, 0)

    assert view._polling is False


def test_switching_between_files_and_the_bot_swaps_the_pollers(start_app):
    tabs = start_app()
    select_tab(tabs, 2)
    files_view = tab_view(tabs, 2)
    assert files_view._polling is True

    select_tab(tabs, 3)
    bot_view = tab_view(tabs, 3)

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
    view = tab_view(tabs, 3)
    previous = view.api_client

    captured["on_save"]("http://127.0.0.1:9000", "Ти")

    # The tab keeps its client, which now talks to the new address.
    assert view.api_client is previous
    assert view.api_client.base_url == "http://127.0.0.1:9000"
