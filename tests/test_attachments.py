from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import flet as ft
import httpx
import pytest
import respx
from helpers import mounted

from api.client import APIClient, APIError, ChatAttachment
from views.attachments import (
    MAX_FILE_BYTES,
    MESSAGE_TOKEN_LIMIT,
    PASTE_THRESHOLD_CHARS,
    NotAttachable,
    estimate_message_tokens,
    guess_extension,
    inserted_text,
    paste_file_name,
    read_text_file,
)
from views.chat import ChatView

BASE_URL = "http://localhost:8080"
USER_ID = "123e4567-e89b-12d3-a456-426614174000"


# ------------------------------------------------------------------ the helpers
def test_a_message_is_counted_the_way_the_backend_counts_it():
    assert estimate_message_tokens("") == 0
    assert estimate_message_tokens("abcd") == 1
    assert estimate_message_tokens("abcde") == 2
    assert estimate_message_tokens("я" * 4) == 2, "Cyrillic is two bytes a letter"
    limit_text = "a" * (MESSAGE_TOKEN_LIMIT * 4)
    assert estimate_message_tokens(limit_text) == MESSAGE_TOKEN_LIMIT
    assert estimate_message_tokens(limit_text + "a") == MESSAGE_TOKEN_LIMIT + 1


def test_utf8_with_or_without_a_byte_order_mark_is_read():
    assert read_text_file("печат\n".encode(), "a.txt") == "печат\n"
    assert read_text_file(b"\xef\xbb\xbfx = 1\n", "a.py") == "x = 1\n"


def test_an_old_windows_1251_file_is_read_too():
    assert (
        read_text_file("Здравей, свят".encode("cp1251"), "old.txt") == "Здравей, свят"
    )


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"MZ\x90\x00\x03", "не е текстов"),
        (b"   \n\t ", "празен"),
        (b"", "празен"),
        pytest.param(
            b"a" * (MAX_FILE_BYTES + 1), "по-голям", id="one byte over the limit"
        ),
        (b"\x98\xff\xfe", "не е текст в UTF-8"),  # 0x98 is not defined in Windows-1251
    ],
)
def test_what_is_not_text_is_refused_with_a_reason(raw, why):
    with pytest.raises(NotAttachable) as info:
        read_text_file(raw, "x.bin")
    assert why in str(info.value)
    assert str(info.value).startswith("x.bin:")


def test_a_file_of_exactly_the_limit_is_accepted():
    assert len(read_text_file(b"a" * MAX_FILE_BYTES, "big.txt")) == MAX_FILE_BYTES


@pytest.mark.parametrize(
    ("previous", "current", "inserted"),
    [
        ("", "pasted", "pasted"),
        ("question: ", "question: code", "code"),
        ("tail", "code tail", "code "),
        ("head tail", "head CODE tail", "CODE "),
        ("aaa", "aaaaaa", "aaa"),
        ("same", "same", ""),
        ("abc", "", ""),
    ],
)
def test_the_inserted_text_is_what_was_added(previous, current, inserted):
    assert inserted_text(previous, current) == inserted


def test_what_was_inserted_plus_the_old_text_is_the_new_text():
    previous = "Виж това: \nкрай"
    pasted = "def f():\n    return 1\n" * 200
    current = previous[:10] + pasted + previous[10:]
    assert inserted_text(previous, current) == pasted


@pytest.mark.parametrize(
    ("text", "extension"),
    [
        ("package main\n\nfunc main() {}\n", ".go"),
        ("import os\n\ndef run():\n    pass\n", ".py"),
        ('{"a": [1, 2, 3]}', ".json"),
        ("[1, 2", ".txt"),
        ("<!DOCTYPE html><html></html>", ".html"),
        ("<?xml version='1.0'?><a/>", ".xml"),
        ("просто текст, нищо особено", ".txt"),
        ("   \n\n", ".txt"),
    ],
)
def test_a_pasted_text_gets_the_extension_it_looks_like(text, extension):
    assert guess_extension(text) == extension


def test_pasted_files_are_numbered_without_clashing_with_the_ones_there():
    assert paste_file_name("x = 1", set()) == "paste_1.txt"
    assert paste_file_name("package a\nfunc f() {}", {"paste_1.go"}) == "paste_2.go"
    assert (
        paste_file_name("def f():\n  pass", {"paste_1.py", "paste_2.py", "main.py"})
        == "paste_3.py"
    )
    assert paste_file_name("text", {"paste_1.go"}) == "paste_2.txt", (
        "the number is not reused for another extension"
    )


# ------------------------------------------------------------------ the client
@pytest.fixture
def client():
    return APIClient(base_url=BASE_URL, user_id=USER_ID)


ATTACHMENT_JSON = {
    "id": "a1",
    "name": "main.go",
    "bytes": 120,
    "lines": 9,
    "tokens": 40,
    "version": 1,
    "created_at": "2026-11-07T20:00:00Z",
    "updated_at": "2026-11-07T20:00:00Z",
    "created": True,
    "replaced": False,
    "unchanged": False,
}


@respx.mock
@pytest.mark.asyncio
async def test_the_files_of_a_conversation_are_listed_for_that_session(client):
    route = respx.get(f"{BASE_URL}/chat/attachments").mock(
        return_value=httpx.Response(200, json=[ATTACHMENT_JSON])
    )

    found = await client.list_attachments("chat-1")

    assert [(a.name, a.lines, a.tokens) for a in found] == [("main.go", 9, 40)]
    assert route.calls[0].request.url.params["session_id"] == "chat-1"
    assert route.calls[0].request.headers["X-User-ID"] == USER_ID


@respx.mock
@pytest.mark.asyncio
async def test_a_file_is_sent_as_json_and_the_answer_says_what_happened(client):
    route = respx.post(f"{BASE_URL}/chat/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT_JSON)
    )

    result = await client.upload_attachment("chat-1", "main.go", "package main\n")

    assert isinstance(result, ChatAttachment)
    assert result.created is True and result.replaced is False
    assert json.loads(route.calls[0].request.content) == {
        "session_id": "chat-1",
        "name": "main.go",
        "content": "package main\n",
    }
    assert route.calls[0].request.headers["X-User-ID"] == USER_ID


@respx.mock
@pytest.mark.asyncio
async def test_a_refused_file_raises_with_the_status_and_the_text(client):
    respx.post(f"{BASE_URL}/chat/attachments").mock(
        return_value=httpx.Response(
            422, text="this conversation already has 20 attached files\n"
        )
    )

    with pytest.raises(APIError) as info:
        await client.upload_attachment("chat-1", "x.txt", "x")

    assert info.value.status == 422
    assert "20 attached files" in info.value.message


@respx.mock
@pytest.mark.asyncio
async def test_a_file_is_removed_by_its_id(client):
    route = respx.delete(f"{BASE_URL}/chat/attachments/a1").mock(
        return_value=httpx.Response(204)
    )

    await client.delete_attachment("a1")

    assert route.called
    with respx.mock:
        respx.delete(f"{BASE_URL}/chat/attachments/gone").mock(
            return_value=httpx.Response(404, text="attachment not found")
        )
        with pytest.raises(APIError) as info:
            await client.delete_attachment("gone")
        assert info.value.status == 404


@respx.mock
@pytest.mark.asyncio
async def test_the_text_of_a_file_never_reaches_the_log(client, caplog):
    respx.post(f"{BASE_URL}/chat/attachments").mock(
        return_value=httpx.Response(201, json=ATTACHMENT_JSON)
    )

    with caplog.at_level("DEBUG"):
        await client.upload_attachment("chat-1", "secret.py", "PASSWORD = 'hunter2'\n")

    assert "hunter2" not in caplog.text
    assert "secret.py" not in caplog.text, "only the id and the sizes are logged"
    assert "a1" in caplog.text


# ------------------------------------------------------------------ the view
def attachment(
    name="main.go", id="a1", lines=9, tokens=40, version=1
) -> ChatAttachment:
    return ChatAttachment(
        id=id, name=name, bytes=120, lines=lines, tokens=tokens, version=version
    )


@pytest.fixture
def view(mock_page, mock_api_client):
    mock_api_client.list_attachments = AsyncMock(return_value=[])
    mock_api_client.upload_attachment = AsyncMock(
        side_effect=lambda session, name, text: attachment(name)
    )
    mock_api_client.delete_attachment = AsyncMock()
    v = ChatView(mock_page, mock_api_client)
    v.update = MagicMock()
    v.scroll_to_bottom = MagicMock()
    return v


def chips(v: ChatView) -> list[str]:
    return [c.label.value for c in v.attachments_row.controls]


def confirming(answer: bool):
    return patch("views.chat.confirm_dialog", new=AsyncMock(return_value=answer))


def snackbars():
    return patch("views.chat.show_snackbar")


def test_the_view_has_a_paperclip_a_row_for_the_files_and_a_picker(view):
    assert view.attach_btn.icon is not None
    assert view.attachments_row.visible is False
    assert view.input_field.on_change is not None


def test_did_mount_reads_the_files_before_the_history(mock_page, mock_api_client):
    mock_api_client.list_attachments = AsyncMock(return_value=[])
    v = ChatView(mock_page, mock_api_client)

    v.did_mount()

    calls = [c.args[0] for c in mock_page.run_task.call_args_list]
    assert calls == [v.load_attachments, v.load_history], (
        "the history is what the view waits for, so it is last"
    )


@pytest.mark.asyncio
async def test_the_paperclip_opens_the_dialog_and_attaches_what_was_chosen(
    view, mock_api_client, tmp_path
):
    chosen = [picked(tmp_path, "main.go", b"package main\n")]
    view.file_picker.pick_files = AsyncMock(return_value=chosen)

    with snackbars():
        await view.attach_btn.on_click(None)

    kwargs = view.file_picker.pick_files.await_args.kwargs
    assert kwargs["allow_multiple"] is True
    assert kwargs["file_type"] == ft.FilePickerFileType.ANY
    assert [c.args[1] for c in mock_api_client.upload_attachment.await_args_list] == [
        "main.go"
    ]


@pytest.mark.asyncio
async def test_a_dialog_closed_without_a_choice_attaches_nothing(view, mock_api_client):
    view.file_picker.pick_files = AsyncMock(return_value=[])

    with snackbars() as snackbar:
        await view.attach_btn.on_click(None)

    mock_api_client.upload_attachment.assert_not_awaited()
    snackbar.assert_not_called()


@pytest.mark.asyncio
async def test_a_dialog_that_does_not_open_is_said_and_logged(
    view, mock_api_client, caplog
):
    # The call waits for the client; a closed session answers with an error.
    view.file_picker.pick_files = AsyncMock(side_effect=RuntimeError("Session closed"))

    with snackbars() as snackbar:
        await view.attach_btn.on_click(None)

    mock_api_client.upload_attachment.assert_not_awaited()
    assert "не се отвори" in snackbar.call_args.args[1]
    assert "The file dialog failed: Session closed" in caplog.text


@pytest.mark.asyncio
async def test_the_files_of_the_open_conversation_are_shown_as_chips(
    view, mock_api_client
):
    mock_api_client.list_attachments = AsyncMock(
        return_value=[
            attachment("main.go", lines=120),
            attachment("b.py", "a2", lines=7),
        ]
    )

    await view.load_attachments()

    assert chips(view) == ["main.go · 120 реда", "b.py · 7 реда"]
    assert view.attachments_row.visible is True
    assert [c.data for c in view.attachments_row.controls] == ["a1", "a2"]
    mock_api_client.list_attachments.assert_awaited_once_with(view.session_id)

    mock_api_client.list_attachments = AsyncMock(return_value=[])
    await view.load_attachments()
    assert chips(view) == []
    assert view.attachments_row.visible is False


@pytest.mark.asyncio
async def test_an_answer_for_another_conversation_is_ignored(view, mock_api_client):
    async def slow(session_id):
        view.session_id = "someone-else"  # the user moved on while the request was out
        return [attachment("old-chat-file.py")]

    mock_api_client.list_attachments = slow

    await view.load_attachments()

    assert chips(view) == []


@pytest.mark.asyncio
async def test_a_failing_list_is_not_shown_to_the_user_and_keeps_what_there_was(
    view, mock_api_client
):
    mock_api_client.list_attachments = AsyncMock(return_value=[attachment()])
    await view.load_attachments()
    mock_api_client.list_attachments = AsyncMock(side_effect=httpx.ConnectError("down"))

    with snackbars() as snackbar:
        await view.load_attachments()

    snackbar.assert_not_called()
    assert chips(view) == ["main.go · 9 реда"]


def test_changing_the_conversation_clears_the_chips_at_once(view):
    view.attachments = [attachment()]
    view._render_attachments()
    assert chips(view)

    view._activate_session("another-session")

    assert chips(view) == []
    assert view.attachments_row.visible is False


def test_a_new_empty_session_does_not_ask_for_files(view, mock_page):
    mock_page.run_task.reset_mock()

    view._activate_session("brand-new", reload_history=False)

    mock_page.run_task.assert_not_called()


def picked(tmp_path, name, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return ft.FilePickerFile(id=0, name=name, size=len(data), path=str(path))


@pytest.mark.asyncio
async def test_chosen_files_are_attached_to_the_open_conversation(
    view, mock_api_client, tmp_path
):
    files = [
        picked(tmp_path, "main.go", b"package main\n"),
        picked(tmp_path, "stari.txt", "Здравей".encode("cp1251")),
    ]

    with snackbars() as snackbar:
        await view.attach_files(files)

    sent = [c.args for c in mock_api_client.upload_attachment.await_args_list]
    assert sent == [
        (view.session_id, "main.go", "package main\n"),
        (view.session_id, "stari.txt", "Здравей"),
    ]
    mock_api_client.list_attachments.assert_awaited()
    message = snackbar.call_args.args[1]
    assert "main.go" in message and "stari.txt" in message
    assert snackbar.call_args.kwargs == {"success": True}


@pytest.mark.asyncio
async def test_what_cannot_be_attached_is_reported_and_does_not_stop_the_others(
    view, mock_api_client, tmp_path
):
    files = [
        picked(tmp_path, "program.exe", b"MZ\x00\x01"),
        picked(tmp_path, "huge.log", b"x" * (MAX_FILE_BYTES + 1)),
        picked(tmp_path, "empty.txt", b""),
        picked(tmp_path, "ok.py", b"x = 1\n"),
    ]

    with snackbars() as snackbar:
        await view.attach_files(files)

    assert [c.args[1] for c in mock_api_client.upload_attachment.await_args_list] == [
        "ok.py"
    ]
    reports = [c.args[1] for c in snackbar.call_args_list]
    problems = next(r for r in reports if r.startswith("Не са прикачени"))
    assert "program.exe: не е текстов файл" in problems
    assert "huge.log: файлът е по-голям от 256 KB" in problems
    assert "empty.txt: файлът е празен" in problems
    assert any("ok.py" in r for r in reports), "and the good one is confirmed"


@pytest.mark.asyncio
async def test_a_refused_upload_says_why_in_bulgarian(view, mock_api_client, tmp_path):
    mock_api_client.upload_attachment = AsyncMock(
        side_effect=APIError(422, "this conversation already has 20 attached files")
    )

    with snackbars() as snackbar:
        await view.attach_files([picked(tmp_path, "a.txt", b"x")])

    message = snackbar.call_args.args[1]
    assert "20 файла" in message and "1 MB" in message
    assert "attached files" not in message


@pytest.mark.asyncio
async def test_a_file_that_vanished_or_has_no_path_is_handled(
    view, mock_api_client, tmp_path
):
    gone = ft.FilePickerFile(
        id=0, name="gone.txt", size=1, path=str(tmp_path / "gone.txt")
    )
    web = ft.FilePickerFile(id=1, name="web.txt", size=1, path=None)

    with snackbars() as snackbar:
        await view.attach_files([gone, web])
        await view.attach_files(None)

    mock_api_client.upload_attachment.assert_not_awaited()
    assert "gone.txt: файлът не може да се прочете" in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_the_cross_on_a_chip_removes_the_file_and_reads_the_list_again(
    view, mock_api_client
):
    mock_api_client.list_attachments = AsyncMock(return_value=[attachment()])
    await view.load_attachments()
    mock_api_client.list_attachments = AsyncMock(return_value=[])

    await view.on_attachment_delete(
        SimpleNamespace(control=view.attachments_row.controls[0])
    )

    mock_api_client.delete_attachment.assert_awaited_once_with("a1")
    assert chips(view) == []


@pytest.mark.asyncio
async def test_a_file_that_was_gone_already_is_not_an_error(view, mock_api_client):
    mock_api_client.delete_attachment = AsyncMock(
        side_effect=APIError(404, "attachment not found")
    )
    mock_api_client.list_attachments = AsyncMock(return_value=[])

    with snackbars() as snackbar:
        await view.on_attachment_delete(
            SimpleNamespace(control=SimpleNamespace(data="a1"))
        )

    snackbar.assert_not_called()
    mock_api_client.list_attachments.assert_awaited()


@pytest.mark.asyncio
async def test_a_failed_removal_says_so_and_keeps_the_chip(view, mock_api_client):
    mock_api_client.list_attachments = AsyncMock(return_value=[attachment()])
    await view.load_attachments()
    mock_api_client.delete_attachment = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )

    with snackbars() as snackbar:
        await view.on_attachment_delete(
            SimpleNamespace(control=view.attachments_row.controls[0])
        )

    assert "не беше махнат" in snackbar.call_args.args[1]
    assert chips(view) == ["main.go · 9 реда"]


# ------------------------------------------------------------------ a long paste
def long_code() -> str:
    return "def f():\n    return 1\n" * 400  # about 8 800 characters, 2 200 tokens


async def type_text(view: ChatView, text: str):
    """What the user does: the field now holds ``text`` and the change event comes."""
    view.input_field.value = text
    await view.on_input_change(None)


@pytest.mark.asyncio
async def test_typing_never_asks(view, mock_api_client):
    with confirming(True) as dialog:
        text = ""
        for letter in "Здравей, как си днес?":
            text += letter
            await type_text(view, text)

    dialog.assert_not_awaited()
    mock_api_client.upload_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_big_paste_can_become_a_file_and_the_field_goes_back(
    view, mock_api_client
):
    await type_text(view, "Намери грешката: ")

    with confirming(True) as dialog, snackbars():
        await type_text(view, "Намери грешката: " + long_code())

    dialog.assert_awaited_once()
    assert "токена" in dialog.await_args.args[2]
    assert dialog.await_args.kwargs["destructive"] is False
    mock_api_client.upload_attachment.assert_awaited_once_with(
        view.session_id, "paste_1.py", long_code()
    )
    assert view.input_field.value == "Намери грешката: ", (
        "the question stays, the code is in the file"
    )
    assert view._input_text == "Намери грешката: "


@pytest.mark.asyncio
async def test_a_paste_in_the_middle_is_taken_out_of_the_middle(view, mock_api_client):
    await type_text(view, "преди след")
    pasted = "x = 1\n" * 700

    with confirming(True), snackbars():
        await type_text(view, "преди " + pasted + "след")

    assert mock_api_client.upload_attachment.await_args.args[2] == pasted
    assert view.input_field.value == "преди след"


@pytest.mark.asyncio
async def test_a_paste_that_stays_in_the_message_is_not_asked_about_again(
    view, mock_api_client
):
    with confirming(False) as dialog:
        await type_text(view, long_code())
        await type_text(view, long_code() + " и още малко")

    assert dialog.await_count == 1, "one question for one paste"
    mock_api_client.upload_attachment.assert_not_awaited()
    assert view.input_field.value.endswith("и още малко")


@pytest.mark.asyncio
async def test_if_the_file_cannot_be_stored_the_text_stays_in_the_field(
    view, mock_api_client
):
    mock_api_client.upload_attachment = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )

    with confirming(True), snackbars() as snackbar:
        await type_text(view, long_code())

    assert view.input_field.value == long_code()
    assert "не беше прикачен" in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_the_name_of_a_pasted_file_does_not_clash_with_a_file_that_is_there(
    view, mock_api_client
):
    view.attachments = [attachment("paste_1.py")]

    with confirming(True), snackbars():
        await type_text(view, long_code())

    assert mock_api_client.upload_attachment.await_args.args[1] == "paste_2.py"


def test_the_threshold_is_far_above_anything_typed():
    assert PASTE_THRESHOLD_CHARS >= 2000


# ------------------------------------------------------- a message over the limit
def too_long() -> str:
    return "ред от програмата\n" * 1500  # about 27 000 bytes - over 4 000 tokens


@pytest.mark.asyncio
async def test_a_message_over_the_limit_is_not_sent_but_offered_as_a_file(
    view, mock_api_client
):
    view.input_field.value = too_long()
    assert estimate_message_tokens(view.input_field.value.strip()) > MESSAGE_TOKEN_LIMIT

    with confirming(True) as dialog, snackbars():
        await view.send_message_click(None)

    dialog.assert_awaited_once()
    assert "най-много се пращат 4000" in dialog.await_args.args[2]
    mock_api_client.stream_chat.assert_not_called()
    assert (
        mock_api_client.upload_attachment.await_args.args[2] == too_long().strip()
        or mock_api_client.upload_attachment.await_args.args[2] == too_long()
    )
    assert view.input_field.value == "Виж прикачения файл paste_1.txt."
    assert view._is_streaming is False


@pytest.mark.asyncio
async def test_a_message_over_the_limit_that_the_user_keeps_is_not_sent_either(
    view, mock_api_client
):
    view.input_field.value = too_long()

    with confirming(False):
        await view.send_message_click(None)

    mock_api_client.stream_chat.assert_not_called()
    mock_api_client.upload_attachment.assert_not_awaited()
    assert view.input_field.value == too_long(), "nothing is lost"
    assert view._is_streaming is False


@pytest.mark.asyncio
async def test_a_message_of_exactly_the_limit_is_sent_as_it_is(view, mock_api_client):
    async def stream(**_kwargs):
        yield "ok"

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "a" * (MESSAGE_TOKEN_LIMIT * 4)

    with confirming(True) as dialog, mounted(ChatView):
        await view.send_message_click(None)

    dialog.assert_not_awaited()
    mock_api_client.stream_chat.assert_called_once()
    assert view.input_field.value == ""
    assert view._input_text == "", (
        "the remembered text follows the field, or the next paste is measured against old text"
    )


@pytest.mark.asyncio
async def test_a_paste_right_after_sending_is_measured_against_an_empty_field(
    view, mock_api_client
):
    async def stream(**_kwargs):
        yield "ok"

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "ред\n" * 1500  # a long message that was sent
    view._input_text = view.input_field.value
    with mounted(ChatView):
        await view.send_message_click(None)

    with confirming(True) as dialog, snackbars():
        await type_text(view, long_code())

    dialog.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unsent_text_comes_back_with_the_remembered_text(
    view, mock_api_client
):
    async def broken(**_kwargs):
        raise httpx.ConnectError("down")
        yield  # makes this an async generator, like the real stream

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: broken(**kw))
    view.input_field.value = "важен въпрос"

    with mounted(ChatView), snackbars():
        await view.send_message_click(None)

    assert view.input_field.value == "важен въпрос"
    assert view._input_text == "важен въпрос"


@pytest.mark.asyncio
async def test_a_second_big_change_while_the_question_is_open_does_not_open_another(
    view, mock_api_client
):
    release = asyncio.Event()

    async def slow_dialog(*_args, **_kwargs):
        await release.wait()
        return False

    with patch(
        "views.chat.confirm_dialog", new=AsyncMock(side_effect=slow_dialog)
    ) as dialog:
        first = asyncio.create_task(type_text(view, long_code()))
        await asyncio.sleep(0)  # the first change has reached its question
        await type_text(view, long_code() + long_code())
        release.set()
        await first

    assert dialog.await_count == 1, "one question at a time"


def test_leaving_a_conversation_stops_reading_its_files(view):
    task = MagicMock()
    view._attachments_task = task

    view._cancel_running_tasks()

    task.cancel.assert_called_once()
    assert view._attachments_task is None
