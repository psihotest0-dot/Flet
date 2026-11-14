"""The proposals the assistant makes and the user confirms: client, card, chat, memory tab."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from helpers import mounted

from api.client import APIClient, APIError, Proposal, Rule
from views.chat import ChatView
from views.chat_message import ChatMessage
from views.memory import MemoryView
from views.proposals import ProposalCard, kind_label, origin_warning

BASE = "http://localhost:8080"
RULE = {
    "id": 17,
    "kind": "save_global_rule",
    "status": "pending",
    "session_id": "s1",
    "summary": "Правило „language“: Говори на български",
    "key": "language",
    "value": "Говори на български",
    "external_sources": ["web_fetch"],
    "created_at": "2026-11-01T10:00:00Z",
}
MEMORY = {
    "id": 18,
    "kind": "create_memory",
    "status": "pending",
    "summary": "Спомен: Обича джаз",
    "content": "Обича джаз",
    "importance": 6,
    "external_sources": [],
}


@pytest.fixture
def client():
    return APIClient(base_url=BASE, user_id="123e4567-e89b-12d3-a456-426614174000")


def attached(control_class):
    """Pretend the controls of this class are on a page."""
    return mounted(control_class)


# --------------------------------------------------------------------- client
@respx.mock
@pytest.mark.asyncio
async def test_list_proposals_parses_the_backend_shape(client):
    route = respx.get(f"{BASE}/proposals").mock(
        return_value=httpx.Response(200, json=[RULE, MEMORY])
    )

    proposals = await client.list_proposals()

    assert route.calls.last.request.headers["X-User-ID"] == client.user_id
    assert [p.id for p in proposals] == [17, 18]
    assert proposals[0].key == "language"
    assert proposals[0].external_sources == ["web_fetch"]
    assert proposals[1].content == "Обича джаз"
    assert proposals[1].importance == 6


@respx.mock
@pytest.mark.asyncio
async def test_list_proposals_tolerates_an_empty_list(client):
    respx.get(f"{BASE}/proposals").mock(return_value=httpx.Response(200, json=[]))
    assert await client.list_proposals() == []


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_deciding_posts_to_the_proposal(client, verb):
    route = respx.post(f"{BASE}/proposals/17/{verb}").mock(
        return_value=httpx.Response(200, json=RULE)
    )

    decide = getattr(client, f"{verb}_proposal")
    result = await decide(17)

    assert route.called
    assert route.calls.last.request.headers["X-User-ID"] == client.user_id
    assert isinstance(result, Proposal)
    assert result.id == 17


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, "proposal not found"),
        (409, "proposal already decided"),
        (422, "too many global rules"),
    ],
)
async def test_deciding_raises_the_backend_text(client, status, body):
    respx.post(f"{BASE}/proposals/17/approve").mock(
        return_value=httpx.Response(status, text=body)
    )

    with pytest.raises(APIError) as exc_info:
        await client.approve_proposal(17)

    assert exc_info.value.status == status
    assert exc_info.value.message == body


@respx.mock
@pytest.mark.asyncio
async def test_rules_list_and_delete(client):
    respx.get(f"{BASE}/rules").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 3,
                    "key": "tone",
                    "value": "Бъди кратка",
                    "enabled": True,
                    "updated_at": "2026-11-01T10:00:00Z",
                }
            ],
        )
    )
    delete = respx.delete(f"{BASE}/rules/3").mock(return_value=httpx.Response(204))

    rules = await client.list_rules()
    await client.delete_rule(3)

    assert rules == [
        Rule(
            id=3,
            key="tone",
            value="Бъди кратка",
            enabled=True,
            updated_at=rules[0].updated_at,
        )
    ]
    assert delete.calls.last.request.headers["X-User-ID"] == client.user_id


@respx.mock
@pytest.mark.asyncio
async def test_delete_rule_raises_for_a_missing_rule(client):
    respx.delete(f"{BASE}/rules/9").mock(
        return_value=httpx.Response(404, text="rule not found")
    )

    with pytest.raises(APIError) as exc_info:
        await client.delete_rule(9)

    assert exc_info.value.status == 404


def sse(*frames: bytes):
    async def body():
        for frame in frames:
            yield frame

    return httpx.Response(
        200, content=body(), headers={"Content-Type": "text/event-stream"}
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "args"),
    [
        ("stream_chat", "/chat/stream", {"session_id": "s", "content": "hi"}),
        ("regenerate_chat", "/chat/regenerate", {"session_id": "s"}),
    ],
)
async def test_streams_deliver_the_proposals_event(client, method, path, args):
    payload = b'event: proposals\ndata: [{"id": 17, "summary": "x"}]\n\n'
    respx.post(f"{BASE}{path}").mock(
        return_value=sse(
            'data: {"content": "Предлагам"}\n\n'.encode(), payload, b"data: [DONE]\n\n"
        )
    )

    results = [item async for item in getattr(client, method)(**args)]

    assert results == [
        {"content": "Предлагам"},
        {"type": "proposals", "data": [{"id": 17, "summary": "x"}]},
    ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["not json", '{"id": 1}'])
async def test_a_malformed_proposals_event_is_skipped(client, data):
    respx.post(f"{BASE}/chat/stream").mock(
        return_value=sse(
            f"event: proposals\ndata: {data}\n\n".encode(),
            b'data: {"content": "ok"}\n\n',
            b"data: [DONE]\n\n",
        )
    )

    results = [item async for item in client.stream_chat(session_id="s", content="hi")]

    assert results == [{"content": "ok"}], (
        "the answer survives a broken proposals event"
    )


# ----------------------------------------------------------------------- card
def make_card(api_client, proposal=None, **kwargs):
    proposal = proposal or Proposal(**RULE)
    return ProposalCard(proposal, api_client, page=kwargs.pop("page", None), **kwargs)


def card_api():
    api_client = MagicMock(spec=APIClient)
    api_client.approve_proposal = AsyncMock(return_value=Proposal(**RULE))
    api_client.reject_proposal = AsyncMock(return_value=Proposal(**RULE))
    return api_client


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([], None),
        (None, None),
        (["web_fetch"], "интернет"),
        (["web_search", "web_fetch"], "интернет"),
        (["search_documents"], "документи"),
        (["retrieved_documents"], "документи"),
        (["web_search", "search_documents"], "интернет и документи"),
    ],
)
def test_origin_warning_names_where_the_text_came_from(sources, expected):
    warning = origin_warning(sources)
    if expected is None:
        assert warning is None
    else:
        assert warning is not None
        assert f"след четене на {expected} —" in warning


def test_card_shows_the_text_and_the_warning():
    card = make_card(card_api())

    assert card.body.value == "Правило „language“: Говори на български"
    assert card.warning.visible is True
    assert "интернет" in card.warning.value
    assert kind_label(Proposal(**MEMORY)) == "Спомен"
    assert "Правило" in card.title.value


def test_card_of_a_plain_turn_has_no_warning():
    card = make_card(card_api(), Proposal(**MEMORY))

    assert card.warning.visible is False
    assert "Спомен" in card.title.value


@pytest.mark.asyncio
async def test_approve_calls_the_backend_and_reports():
    api_client = card_api()
    decided = []
    card = make_card(
        api_client, on_decided=lambda p, approved: decided.append((p.id, approved))
    )

    await card._approve(None)

    api_client.approve_proposal.assert_awaited_once_with(17)
    api_client.reject_proposal.assert_not_awaited()
    assert decided == [(17, True)]
    assert card.buttons.visible is False
    assert card.result.visible is True
    assert "Запазено" in card.result.value


@pytest.mark.asyncio
async def test_reject_calls_the_backend_and_reports():
    api_client = card_api()
    decided = []
    card = make_card(
        api_client, on_decided=lambda p, approved: decided.append((p.id, approved))
    )

    await card._reject(None)

    api_client.reject_proposal.assert_awaited_once_with(17)
    api_client.approve_proposal.assert_not_awaited()
    assert decided == [(17, False)]
    assert "Отхвърлено" in card.result.value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 409])
async def test_a_proposal_closed_elsewhere_ends_the_card_quietly(status, mock_page):
    api_client = card_api()
    api_client.approve_proposal = AsyncMock(
        side_effect=APIError(status, "proposal already decided")
    )
    decided = []
    card = make_card(
        api_client,
        page=mock_page,
        on_decided=lambda p, approved: decided.append(approved),
    )

    await card._approve(None)

    assert card.buttons.visible is False
    assert "Вече е решено" in card.result.value
    mock_page.show_dialog.assert_not_called()
    assert decided == [False], (
        "nothing was saved, so the list must not reload as if it was"
    )


@pytest.mark.asyncio
async def test_too_many_rules_keeps_the_card_open(mock_page):
    api_client = card_api()
    api_client.approve_proposal = AsyncMock(
        side_effect=APIError(422, "too many global rules")
    )
    card = make_card(api_client, page=mock_page)

    await card._approve(None)

    assert card.buttons.visible is True
    assert card.approve_btn.disabled is False and card.reject_btn.disabled is False
    snackbar = mock_page.show_dialog.call_args.args[0]
    assert "твърде много глобални правила" in snackbar.content.value


@pytest.mark.asyncio
async def test_a_dead_backend_keeps_the_card_open(mock_page):
    api_client = card_api()
    api_client.approve_proposal = AsyncMock(side_effect=httpx.ConnectError("refused"))
    card = make_card(api_client, page=mock_page)

    await card._approve(None)

    assert card.buttons.visible is True
    assert card.approve_btn.disabled is False
    assert (
        "Бекендът не е достъпен"
        in mock_page.show_dialog.call_args.args[0].content.value
    )


@pytest.mark.asyncio
async def test_a_double_tap_decides_once():
    api_client = card_api()
    release = asyncio.Event()

    async def slow(_id):
        await release.wait()
        return Proposal(**RULE)

    api_client.approve_proposal = AsyncMock(side_effect=slow)
    card = make_card(api_client)

    first = asyncio.create_task(card._approve(None))
    await asyncio.sleep(0)
    assert card.approve_btn.disabled is True
    # Both return at once while the first call is still waiting; without the guard
    # they would wait for the backend too, and the timeout turns that into a failure.
    await asyncio.wait_for(card._approve(None), timeout=1)
    await asyncio.wait_for(card._reject(None), timeout=1)
    release.set()
    await first

    api_client.approve_proposal.assert_awaited_once()
    api_client.reject_proposal.assert_not_awaited()


# ----------------------------------------------------------------------- chat
def test_chat_message_shows_a_card_per_proposal(mock_page):
    api_client = card_api()
    message = ChatMessage(
        "assistant", "Предлагам", page=mock_page, api_client=api_client
    )

    message.add_proposals([RULE, MEMORY])

    cards = message.proposals_column.controls
    assert [card.proposal.id for card in cards] == [17, 18]
    assert message.proposals_column.visible is True
    assert message.proposals_column in message.controls


def test_chat_message_does_not_repeat_a_proposal(mock_page):
    message = ChatMessage("assistant", "x", page=mock_page, api_client=card_api())

    message.add_proposals([RULE])
    message.add_proposals([RULE, MEMORY])

    assert [card.proposal.id for card in message.proposals_column.controls] == [17, 18]


def test_chat_message_ignores_malformed_proposals(mock_page):
    message = ChatMessage("assistant", "x", page=mock_page, api_client=card_api())

    message.add_proposals(["text", 5, {"summary": "no id"}, {"id": "abc"}, RULE])

    assert [card.proposal.id for card in message.proposals_column.controls] == [17]


def test_chat_message_without_a_client_shows_nothing():
    message = ChatMessage("assistant", "x")

    message.add_proposals([RULE])

    assert message.proposals_column.controls == []
    assert message.proposals_column.visible is False


@pytest.mark.asyncio
async def test_the_stream_puts_the_proposals_under_the_answer(mock_page):
    api_client = card_api()
    api_client.get_messages = AsyncMock(return_value=[])
    api_client.list_memories = AsyncMock(return_value=[])

    async def stream(**_kwargs):
        yield {"content": "Предлагам да го запомня."}
        yield {"type": "proposals", "data": [RULE]}

    api_client.stream_chat = MagicMock(side_effect=lambda **kwargs: stream(**kwargs))
    view = ChatView(mock_page, api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    view.input_field.value = "Винаги ми говори на български"

    await view.send_message_click(None)

    answer = view.chat_list.controls[-1].controls[0].content
    assert answer.md.value == "Предлагам да го запомня."
    assert [card.proposal.id for card in answer.proposals_column.controls] == [17]
    assert answer.error_text.visible is False


# --------------------------------------------------------------- memory tab
def memory_view(mock_page, proposals=None, rules=None, **overrides):
    api_client = MagicMock(spec=APIClient)
    api_client.list_memories = AsyncMock(return_value=[])
    api_client.list_proposals = AsyncMock(
        return_value=[Proposal(**p) for p in (proposals or [])], **overrides
    )
    api_client.list_rules = AsyncMock(
        return_value=[
            Rule(id=r[0], key=r[1], value=r[2], kind=r[3] if len(r) > 3 else "rule")
            for r in (rules or [])
        ]
    )
    api_client.delete_rule = AsyncMock()
    return MemoryView(mock_page, api_client), api_client


@pytest.mark.asyncio
async def test_memory_tab_lists_what_waits_for_an_answer(mock_page):
    view, _ = memory_view(mock_page, proposals=[RULE, MEMORY])

    await view.load_proposals_and_rules()

    assert view.proposals_section.visible is True
    assert [card.proposal.id for card in view.proposals_column.controls] == [17, 18]


@pytest.mark.asyncio
async def test_memory_tab_hides_the_sections_when_there_is_nothing(mock_page):
    view, _ = memory_view(mock_page)
    view.proposals_section.visible = True
    view.rules_tile.visible = True

    await view.load_proposals_and_rules()

    assert view.proposals_section.visible is False
    assert view.rules_tile.visible is False


@pytest.mark.asyncio
async def test_memory_tab_lists_the_rules_with_a_count(mock_page):
    view, _ = memory_view(
        mock_page, rules=[(3, "tone", "Бъди кратка"), (4, "language", "Български")]
    )

    await view.load_proposals_and_rules()

    assert view.rules_tile.visible is True
    assert view.rules_tile.title.value == "Глобални правила (2)"
    assert [tile.title.value for tile in view.rules_column.controls] == [
        "tone",
        "language",
    ]
    assert view.rules_column.controls[0].subtitle.value == "Бъди кратка"


@pytest.mark.asyncio
async def test_memory_tab_survives_a_backend_without_these_endpoints(mock_page):
    view, api_client = memory_view(mock_page)
    api_client.list_proposals = AsyncMock(
        side_effect=APIError(404, "404 page not found")
    )
    api_client.list_rules = AsyncMock(side_effect=httpx.ConnectError("refused"))

    await view.load_proposals_and_rules()

    assert view.proposals_section.visible is False
    assert view.rules_tile.visible is False
    mock_page.show_dialog.assert_not_called()


@pytest.mark.asyncio
async def test_approving_in_the_tab_reloads_memories_and_rules(mock_page):
    view, api_client = memory_view(mock_page, proposals=[RULE])
    api_client.approve_proposal = AsyncMock(return_value=Proposal(**RULE))
    await view.load_proposals_and_rules()
    card = view.proposals_column.controls[0]

    await card._approve(None)

    mock_page.run_task.assert_called_once_with(view._reload_after_approval)
    api_client.list_memories.reset_mock()
    api_client.list_rules.reset_mock()
    await view._reload_after_approval()
    api_client.list_memories.assert_awaited_once()
    api_client.list_rules.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejecting_in_the_tab_does_not_reload(mock_page):
    view, api_client = memory_view(mock_page, proposals=[RULE])
    api_client.reject_proposal = AsyncMock(return_value=Proposal(**RULE))
    await view.load_proposals_and_rules()

    await view.proposals_column.controls[0]._reject(None)

    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_deleting_a_rule_asks_first(mock_page):
    view, api_client = memory_view(mock_page, rules=[(3, "tone", "Бъди кратка")])
    await view.load_proposals_and_rules()
    rule = Rule(id=3, key="tone", value="Бъди кратка")

    with patch("views.memory.confirm_dialog", AsyncMock(return_value=False)):
        await view.delete_rule(rule)
    api_client.delete_rule.assert_not_awaited()

    api_client.list_rules = AsyncMock(return_value=[])
    with patch("views.memory.confirm_dialog", AsyncMock(return_value=True)):
        await view.delete_rule(rule)

    api_client.delete_rule.assert_awaited_once_with(3)
    assert view.rules_tile.visible is False, "the list is read again after the delete"


@pytest.mark.asyncio
async def test_a_failed_rule_delete_keeps_the_rule_listed(mock_page):
    view, api_client = memory_view(mock_page, rules=[(3, "tone", "Бъди кратка")])
    await view.load_proposals_and_rules()
    api_client.delete_rule = AsyncMock(side_effect=APIError(500, "boom"))

    with patch("views.memory.confirm_dialog", AsyncMock(return_value=True)):
        await view.delete_rule(Rule(id=3, key="tone", value="Бъди кратка"))

    assert [tile.title.value for tile in view.rules_column.controls] == ["tone"]
    assert (
        "Грешка при изтриване" in mock_page.show_dialog.call_args.args[0].content.value
    )


@pytest.mark.asyncio
async def test_deleting_a_rule_that_is_gone_just_refreshes(mock_page):
    view, api_client = memory_view(mock_page, rules=[(3, "tone", "Бъди кратка")])
    await view.load_proposals_and_rules()
    api_client.delete_rule = AsyncMock(side_effect=APIError(404, "rule not found"))
    api_client.list_rules = AsyncMock(return_value=[])

    with patch("views.memory.confirm_dialog", AsyncMock(return_value=True)):
        await view.delete_rule(Rule(id=3, key="tone", value="Бъди кратка"))

    assert view.rules_tile.visible is False


@pytest.mark.asyncio
async def test_memory_tab_loads_the_sections_on_mount(mock_page):
    view, _ = memory_view(mock_page)

    view.did_mount()

    mock_page.run_task.assert_any_call(view.load_memories)
    mock_page.run_task.assert_any_call(view.load_proposals_and_rules)


# ------------------------------------------------- notes on how to talk (P42)
def test_a_style_note_proposal_has_its_own_label():
    note = dict(RULE, kind="save_style_note", summary="Как говорим: Шегува се")
    assert kind_label(Proposal(**note)) == "Как говорим"
    assert kind_label(Proposal(**RULE)) == "Правило"


def test_a_rule_from_an_older_backend_is_a_rule():
    assert Rule(id=1, key="tone", value="Бъди кратка").kind == "rule"


@pytest.mark.asyncio
async def test_memory_tab_lists_the_notes_apart_from_the_rules(mock_page):
    view, _ = memory_view(
        mock_page,
        rules=[
            (3, "tone", "Бъди кратка"),
            (5, "humour", "Шегува се — отговаряй с шега", "style"),
        ],
    )

    await view.load_proposals_and_rules()

    assert view.rules_tile.title.value == "Глобални правила (1)"
    assert [tile.title.value for tile in view.rules_column.controls] == ["tone"]
    assert view.style_tile.visible is True
    assert view.style_tile.title.value == "Как говорим ние двамата (1)"
    assert [tile.title.value for tile in view.style_column.controls] == ["humour"]


@pytest.mark.asyncio
async def test_memory_tab_hides_the_notes_when_there_are_none(mock_page):
    view, _ = memory_view(mock_page, rules=[(3, "tone", "Бъди кратка")])
    view.style_tile.visible = True

    await view.load_proposals_and_rules()

    assert view.style_tile.visible is False


@pytest.mark.asyncio
async def test_deleting_a_note_says_note(mock_page):
    note = Rule(id=5, key="humour", value="Шегува се", kind="style")
    view, api_client = memory_view(
        mock_page, rules=[(5, "humour", "Шегува се", "style")]
    )
    await view.load_proposals_and_rules()
    api_client.list_rules = AsyncMock(return_value=[])
    confirm = AsyncMock(return_value=True)

    with patch("views.memory.confirm_dialog", confirm):
        await view.delete_rule(note)

    api_client.delete_rule.assert_awaited_once_with(5)
    assert "бележката „humour“" in confirm.call_args.args[2]
    assert view.style_tile.visible is False, "the list is read again after the delete"


# ------------------------------------------------------- writes to files (P44)
FILE_WRITE = {
    "id": 31,
    "kind": "write_file",
    "status": "pending",
    "session_id": "s1",
    "summary": "Замяна на файла „списък.txt“ (12 знака): мляко и хляб",
    "content": "мляко и хляб",
    "file_name": "списък.txt",
    "file_mode": "replace",
    "external_sources": ["web_fetch", "user_files"],
    "created_at": "2026-11-14T20:00:00Z",
}


def test_a_file_write_is_labelled_as_one():
    assert kind_label(Proposal(**FILE_WRITE)) == "Запис на файл"


def test_the_proposal_keeps_the_name_and_the_mode_of_a_file_write():
    proposal = Proposal(**FILE_WRITE)

    assert proposal.file_name == "списък.txt"
    assert proposal.file_mode == "replace"
    assert proposal.content == "мляко и хляб"
    assert Proposal(**RULE).file_name is None


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (["user_files"], "файлове"),
        (["attachments"], "файлове"),
        (["web_fetch", "user_files"], "интернет и файлове"),
        (["search_documents", "attachments"], "документи и файлове"),
        (
            ["web_search", "search_documents", "user_files"],
            "интернет, документи и файлове",
        ),
    ],
)
def test_origin_warning_names_the_files_too(sources, expected):
    warning = origin_warning(sources)

    assert warning is not None
    assert f"след четене на {expected} —" in warning


def test_the_card_of_a_file_write_says_what_it_does():
    card = make_card(card_api(), Proposal(**FILE_WRITE))

    assert "Запис на файл" in card.title.value
    assert card.body.value == FILE_WRITE["summary"]
    assert card.hint.visible is True
    assert "предишна версия" in card.hint.value
    assert card.show_text_btn.visible is True
    assert card.full_text.visible is False
    assert "интернет и файлове" in card.warning.value


@pytest.mark.parametrize(
    ("mode", "word"),
    [("create", "нов файл"), ("replace", "предишна версия"), ("append", "в края")],
)
def test_every_way_of_writing_has_its_hint(mode, word):
    card = make_card(card_api(), Proposal(**{**FILE_WRITE, "file_mode": mode}))

    assert word in card.hint.value


def test_a_rule_and_a_memory_have_no_file_controls():
    for raw in (RULE, MEMORY):
        card = make_card(card_api(), Proposal(**raw))

        assert card.hint.visible is False
        assert card.show_text_btn.visible is False
        assert card.full_text.visible is False


@pytest.mark.asyncio
async def test_the_whole_text_can_be_shown_and_hidden():
    card = make_card(card_api(), Proposal(**FILE_WRITE))
    shown = card.full_text.content.controls[0]
    assert shown.value == "мляко и хляб"

    await card._toggle_text(None)
    assert card.full_text.visible is True
    assert card.show_text_btn.content == "Скрий текста"

    await card._toggle_text(None)
    assert card.full_text.visible is False
    assert card.show_text_btn.content == "Покажи целия текст"


@pytest.mark.asyncio
async def test_a_file_write_that_cannot_be_done_shows_the_reason_and_stays_open(
    mock_page,
):
    reason = (
        "Файлът „списък.txt“ вече съществува. Отхвърли предложението и поискай замяна."
    )
    api_client = card_api()
    api_client.approve_proposal = AsyncMock(side_effect=APIError(422, reason))
    card = make_card(api_client, Proposal(**FILE_WRITE), page=mock_page)

    await card._approve(None)

    assert card.buttons.visible is True, "the user can still reject it"
    assert card.approve_btn.disabled is False
    snackbar = mock_page.show_dialog.call_args.args[0]
    assert snackbar.content.value == reason, "the backend's reason, not a rules message"


@pytest.mark.asyncio
async def test_a_file_write_can_be_approved_and_rejected():
    for approve in (True, False):
        api_client = card_api()
        decided = []
        card = make_card(
            api_client,
            Proposal(**FILE_WRITE),
            on_decided=lambda p, approved, d=decided: d.append((p.id, approved)),
        )

        await (card._approve if approve else card._reject)(None)

        assert decided == [(31, approve)]
        assert card.result.visible is True
