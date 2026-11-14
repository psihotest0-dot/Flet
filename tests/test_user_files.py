"""The files the assistant keeps for the user: client, helpers, the tab and the view."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import flet as ft
import httpx
import pytest
import respx
from helpers import tab_labels, tab_view

import main as main_module
from api.client import (
    APIClient,
    APIError,
    Proposal,
    UserFile,
    UserFileInfo,
    UserFileList,
)
from views import user_files as user_files_module
from views.user_files import (
    NO_FILES_TEXT,
    NO_PREVIOUS_TEXT,
    NOT_ENABLED_TEXT,
    UserFilesView,
    file_subtitle,
    files_label,
    modified_label,
    usage_label,
)

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
BASE = "http://localhost:8080"
LISTING = {
    "files": [
        {
            "name": "списък за пазаруване.txt",
            "size": 40,
            "modified": "2026-11-14T18:15:00Z",
        },
        {"name": "plan.md", "size": 2048, "modified": "2026-11-13T09:00:00Z"},
    ],
    "total_bytes": 3000,
    "max_files": 200,
    "max_file_bytes": 65536,
    "max_total_bytes": 20971520,
}
WRITE = {
    "id": 31,
    "kind": "write_file",
    "status": "pending",
    "summary": "Нов файл „a.txt“ (5 знака): мляко",
    "content": "мляко",
    "file_name": "a.txt",
    "file_mode": "create",
    "external_sources": ["web_fetch"],
}
RULE = {"id": 17, "kind": "save_global_rule", "summary": "Правило „k“: v"}


@pytest.fixture
def client():
    return APIClient(base_url=BASE, user_id=USER_ID)


# --------------------------------------------------------------------- client
@respx.mock
@pytest.mark.asyncio
async def test_list_user_files_parses_the_backend_shape(client):
    route = respx.get(f"{BASE}/workspace/files").mock(
        return_value=httpx.Response(200, json=LISTING)
    )

    listing = await client.list_user_files()

    assert route.calls.last.request.headers["X-User-ID"] == USER_ID
    assert [f.name for f in listing.files] == ["списък за пазаруване.txt", "plan.md"]
    assert listing.files[0].size == 40
    assert listing.files[0].modified == datetime(2026, 11, 14, 18, 15, tzinfo=UTC)
    assert listing.total_bytes == 3000
    assert listing.max_total_bytes == 20971520


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_folder_is_an_empty_list(client):
    respx.get(f"{BASE}/workspace/files").mock(
        return_value=httpx.Response(200, json={"files": []})
    )

    listing = await client.list_user_files()

    assert listing.files == []
    assert listing.total_bytes == 0


@respx.mock
@pytest.mark.asyncio
async def test_a_backend_without_the_file_tools_answers_404(client):
    respx.get(f"{BASE}/workspace/files").mock(
        return_value=httpx.Response(404, text="404 page not found")
    )

    with pytest.raises(APIError) as exc_info:
        await client.list_user_files()

    assert exc_info.value.status == 404


@respx.mock
@pytest.mark.asyncio
async def test_the_name_travels_percent_encoded(client):
    route = respx.get(url__regex=rf"{BASE}/workspace/files/.*").mock(
        return_value=httpx.Response(
            200,
            json={"name": "списък за пазаруване.txt", "version": "current"},
        )
    )

    await client.get_user_file("списък за пазаруване.txt")

    raw_path = route.calls.last.request.url.raw_path.decode()
    assert raw_path == (
        "/workspace/files/%D1%81%D0%BF%D0%B8%D1%81%D1%8A%D0%BA%20"
        "%D0%B7%D0%B0%20%D0%BF%D0%B0%D0%B7%D0%B0%D1%80%D1%83%D0%B2%D0%B0%D0%BD%D0%B5.txt"
    )


@respx.mock
@pytest.mark.asyncio
async def test_a_slash_in_a_name_cannot_make_another_path(client):
    route = respx.get(url__regex=rf"{BASE}/workspace/files/.*").mock(
        return_value=httpx.Response(400, text="the file name is not allowed")
    )

    with pytest.raises(APIError):
        await client.get_user_file("../secret.txt")

    assert route.calls.last.request.url.raw_path.decode().startswith(
        "/workspace/files/..%2Fsecret.txt"
    )


@respx.mock
@pytest.mark.asyncio
async def test_the_current_and_the_previous_version(client):
    route = respx.get(f"{BASE}/workspace/files/a.txt").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "name": "a.txt",
                    "version": "current",
                    "content": "нов",
                    "size": 6,
                    "modified": "2026-11-14T18:15:00Z",
                },
            ),
            httpx.Response(
                200, json={"name": "a.txt", "version": "previous", "content": "стар"}
            ),
        ]
    )

    current = await client.get_user_file("a.txt")
    previous = await client.get_user_file("a.txt", previous=True)

    assert isinstance(current, UserFile)
    assert (current.content, current.version, current.size) == ("нов", "current", 6)
    assert (previous.content, previous.version, previous.size) == (
        "стар",
        "previous",
        None,
    )
    assert "version" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["version"] == "previous"


@respx.mock
@pytest.mark.asyncio
async def test_deleting_a_file_sends_the_user_and_logs_no_name(client, caplog):
    route = respx.delete(f"{BASE}/workspace/files/secret%20plan.txt").mock(
        return_value=httpx.Response(204)
    )
    caplog.set_level(logging.INFO, logger="api.client")

    await client.delete_user_file("secret plan.txt")

    assert route.calls.last.request.headers["X-User-ID"] == USER_ID
    assert "A file of the assistant was deleted." in caplog.text
    assert "secret" not in caplog.text, "the name is the user's own text"


@respx.mock
@pytest.mark.asyncio
async def test_deleting_a_missing_file_raises_the_backend_text(client):
    respx.delete(f"{BASE}/workspace/files/a.txt").mock(
        return_value=httpx.Response(404, text="no such file")
    )

    with pytest.raises(APIError) as exc_info:
        await client.delete_user_file("a.txt")

    assert exc_info.value.status == 404
    assert exc_info.value.message == "no such file"


# -------------------------------------------------------------------- helpers
def test_the_count_of_files_has_the_bulgarian_plural():
    assert files_label(0) == "0 файла"
    assert files_label(1) == "1 файл"
    assert files_label(2) == "2 файла"


def test_the_usage_says_what_the_files_take_of_what_they_may():
    listing = UserFileList(**LISTING)

    assert usage_label(listing) == "2 файла · 3 KB от 20,0 MB"


def test_the_subtitle_has_the_size_and_the_time_in_local_time():
    info = UserFileInfo(
        name="a.txt", size=2048, modified=datetime(2026, 11, 14, 18, 15, tzinfo=UTC)
    )

    assert file_subtitle(info) == (
        "2 KB · "
        + datetime(2026, 11, 14, 18, 15, tzinfo=UTC)
        .astimezone()
        .strftime("%d.%m.%Y %H:%M")
    )
    assert file_subtitle(UserFileInfo(name="b.txt", size=5)) == "5 B"
    assert modified_label(None) == ""


# --------------------------------------------------------------------- the view
def make_api(files: UserFileList | None = None, proposals=None):
    api_client = MagicMock(spec=APIClient)
    api_client.list_user_files = AsyncMock(return_value=files or UserFileList())
    api_client.list_proposals = AsyncMock(return_value=proposals or [])
    api_client.get_user_file = AsyncMock(
        return_value=UserFile(name="a.txt", content="мляко\nхляб")
    )
    api_client.delete_user_file = AsyncMock()
    api_client.approve_proposal = AsyncMock(return_value=Proposal(**WRITE))
    api_client.reject_proposal = AsyncMock(return_value=Proposal(**WRITE))
    return api_client


@pytest.fixture
def snackbars(monkeypatch):
    shown = MagicMock()
    monkeypatch.setattr(user_files_module, "show_snackbar", shown)
    return shown


def tile_names(view: UserFilesView) -> list[str]:
    return [
        tile.title.value
        for tile in view.list_column.controls
        if isinstance(tile, ft.ListTile)
    ]


@pytest.mark.asyncio
async def test_loading_lists_the_files_and_what_they_take(mock_page):
    view = UserFilesView(mock_page, make_api(UserFileList(**LISTING)))

    await view.load()

    assert tile_names(view) == ["списък за пазаруване.txt", "plan.md"]
    assert view.usage.value == "2 файла · 3 KB от 20,0 MB"
    assert view.notice.visible is False
    assert view.progress.visible is False
    assert view.proposals_section.visible is False


@pytest.mark.asyncio
async def test_an_empty_folder_says_how_to_start(mock_page):
    view = UserFilesView(mock_page, make_api())

    await view.load()

    assert tile_names(view) == []
    assert view.list_column.controls[0].value == NO_FILES_TEXT
    assert view.usage.value.startswith("0 файла")


@pytest.mark.asyncio
async def test_a_backend_that_does_not_serve_the_files_says_they_are_off(mock_page):
    api_client = make_api()
    api_client.list_user_files = AsyncMock(side_effect=APIError(404, "not found"))
    view = UserFilesView(mock_page, api_client)
    view.listing = UserFileList(**LISTING)
    view.list_column.controls = [ft.Text("old")]

    await view.load()

    assert view.enabled is False
    assert view.notice.value == NOT_ENABLED_TEXT
    assert view.notice.visible is True
    assert view.list_column.controls == []
    assert view.usage.value == ""
    assert view.progress.visible is False
    api_client.list_proposals.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [APIError(500, "boom"), httpx.ConnectError("refused")],
)
async def test_a_failed_load_says_so_and_keeps_the_list(mock_page, error):
    api_client = make_api(UserFileList(**LISTING))
    view = UserFilesView(mock_page, api_client)
    await view.load()
    api_client.list_user_files = AsyncMock(side_effect=error)

    await view.load()

    assert view.notice.visible is True
    assert "Неуспешно зареждане" in view.notice.value
    assert tile_names(view) == ["списък за пазаруване.txt", "plan.md"]
    assert view.progress.visible is False
    assert view.enabled is True


@pytest.mark.asyncio
async def test_a_load_that_was_overtaken_does_not_overwrite_the_newer_one(mock_page):
    view = UserFilesView(mock_page, make_api(UserFileList(**LISTING)))
    api_client = view.api_client

    async def slow(*_a):
        view._load_seq += 1  # another load started while this one waited
        return UserFileList(files=[UserFileInfo(name="old.txt", size=1)])

    api_client.list_user_files = AsyncMock(side_effect=slow)

    await view.load()

    assert tile_names(view) == [], "the answer of the older load is dropped"


@pytest.mark.asyncio
async def test_only_the_writes_to_files_are_shown_as_cards(mock_page):
    proposals = [Proposal(**RULE), Proposal(**WRITE)]
    view = UserFilesView(mock_page, make_api(proposals=proposals))

    await view.load()

    cards = view.proposals_column.controls
    assert [card.proposal.id for card in cards] == [31]
    assert view.proposals_section.visible is True


@pytest.mark.asyncio
async def test_a_failed_proposal_load_leaves_the_files_as_they_are(mock_page):
    api_client = make_api(UserFileList(**LISTING))
    api_client.list_proposals = AsyncMock(side_effect=APIError(500, "boom"))
    view = UserFilesView(mock_page, api_client)

    await view.load()

    assert tile_names(view) == ["списък за пазаруване.txt", "plan.md"]
    assert view.notice.visible is False
    assert view.proposals_section.visible is False


@pytest.mark.asyncio
async def test_an_approved_write_loads_the_files_again(mock_page):
    api_client = make_api(proposals=[Proposal(**WRITE)])
    view = UserFilesView(mock_page, api_client)
    await view.load()
    card = view.proposals_column.controls[0]
    mock_page.run_task.reset_mock()

    await card._approve(None)

    mock_page.run_task.assert_called_once_with(view.load)


@pytest.mark.asyncio
async def test_a_rejected_write_does_not_reload(mock_page):
    api_client = make_api(proposals=[Proposal(**WRITE)])
    view = UserFilesView(mock_page, api_client)
    await view.load()
    card = view.proposals_column.controls[0]
    mock_page.run_task.reset_mock()

    await card._reject(None)

    mock_page.run_task.assert_not_called()


# ------------------------------------------------------------------ the buttons
@pytest.mark.asyncio
async def test_the_row_buttons_open_and_delete_through_the_page(mock_page):
    view = UserFilesView(mock_page, make_api(UserFileList(**LISTING)))
    await view.load()
    tile = view.list_column.controls[0]
    info = view.listing.files[0]
    open_btn, delete_btn = tile.trailing.controls

    tile.on_click(None)
    mock_page.run_task.assert_called_with(view.open_file, info)
    open_btn.on_click(None)
    mock_page.run_task.assert_called_with(view.open_file, info)
    delete_btn.on_click(None)
    mock_page.run_task.assert_called_with(view.delete_file, info)


# -------------------------------------------------------------------------- open
@pytest.mark.asyncio
async def test_opening_a_file_shows_its_text(mock_page):
    api_client = make_api()
    view = UserFilesView(mock_page, api_client)

    await view.open_file(UserFileInfo(name="a.txt"))

    api_client.get_user_file.assert_awaited_once_with("a.txt")
    dialog = mock_page.show_dialog.call_args.args[0]
    assert isinstance(dialog, ft.AlertDialog)
    assert dialog.title.value == "a.txt"
    assert dialog.content.controls[0].value == "мляко\nхляб"
    assert dialog.content.controls[0].selectable is True


@pytest.mark.asyncio
async def test_a_file_that_is_gone_says_so_and_the_list_is_read_again(
    mock_page, snackbars
):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(side_effect=APIError(404, "no such file"))
    view = UserFilesView(mock_page, api_client)

    await view.open_file(UserFileInfo(name="a.txt"))

    snackbars.assert_called_once()
    assert "вече го няма" in snackbars.call_args.args[1]
    api_client.list_user_files.assert_awaited_once()
    mock_page.show_dialog.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [APIError(500, "boom"), httpx.ConnectError("x")])
async def test_a_failed_open_shows_an_error_and_no_dialog(mock_page, snackbars, error):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(side_effect=error)
    view = UserFilesView(mock_page, api_client)

    await view.open_file(UserFileInfo(name="a.txt"))

    snackbars.assert_called_once()
    mock_page.show_dialog.assert_not_called()
    api_client.list_user_files.assert_not_awaited()


def open_dialog_of(view: UserFilesView, text: str = "нов") -> ft.AlertDialog:
    view._show_text_dialog("a.txt", text)
    return view.page_ref.show_dialog.call_args.args[0]


@pytest.mark.asyncio
async def test_the_previous_version_can_be_shown_and_left(mock_page):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(
        return_value=UserFile(name="a.txt", version="previous", content="стар")
    )
    view = UserFilesView(mock_page, api_client)
    dialog = open_dialog_of(view)
    switch_btn = dialog.actions[0]
    body = dialog.content.controls[0]
    assert switch_btn.content == "Предишна версия"

    await switch_btn.on_click(None)

    api_client.get_user_file.assert_awaited_once_with("a.txt", previous=True)
    assert body.value == "стар"
    assert switch_btn.content == "Сегашна версия"
    assert "предишна версия" in dialog.title.value

    await switch_btn.on_click(None)

    assert body.value == "нов", "back to the current text, without asking again"
    assert switch_btn.content == "Предишна версия"
    assert dialog.title.value == "a.txt"
    api_client.get_user_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_file_without_a_previous_version_says_so(mock_page, snackbars):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(side_effect=APIError(404, "no such file"))
    view = UserFilesView(mock_page, api_client)
    dialog = open_dialog_of(view)

    await dialog.actions[0].on_click(None)

    snackbars.assert_called_once_with(view.page_ref, NO_PREVIOUS_TEXT)
    assert dialog.content.controls[0].value == "нов"
    assert dialog.actions[0].content == "Предишна версия"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [APIError(500, "boom"), httpx.ConnectError("x")])
async def test_a_failed_previous_version_is_an_error_and_the_text_stays(
    mock_page, snackbars, error
):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(side_effect=error)
    view = UserFilesView(mock_page, api_client)
    dialog = open_dialog_of(view)

    await dialog.actions[0].on_click(None)

    snackbars.assert_called_once()
    assert dialog.content.controls[0].value == "нов"


@pytest.mark.asyncio
async def test_copy_puts_the_shown_text_on_the_clipboard(
    mock_page, snackbars, clipboard
):
    api_client = make_api()
    api_client.get_user_file = AsyncMock(
        return_value=UserFile(name="a.txt", version="previous", content="стар")
    )
    view = UserFilesView(mock_page, api_client)
    dialog = open_dialog_of(view)
    switch_btn, copy_btn, close_btn = dialog.actions

    await copy_btn.on_click(None)
    await switch_btn.on_click(None)
    await copy_btn.on_click(None)

    assert clipboard == ["нов", "стар"]
    assert close_btn.content == "Затвори"


@pytest.mark.asyncio
async def test_a_failed_copy_says_so(mock_page, snackbars, monkeypatch):
    monkeypatch.setattr(user_files_module, "copy_text", AsyncMock(return_value=False))
    view = UserFilesView(mock_page, make_api())
    dialog = open_dialog_of(view)

    await dialog.actions[1].on_click(None)

    assert snackbars.call_args.args[1] == "Не успях да копирам."


def test_the_close_button_closes_this_dialog(mock_page):
    view = UserFilesView(mock_page, make_api())
    dialog = open_dialog_of(view)
    assert dialog.open is True

    dialog.actions[2].on_click(None)

    assert dialog.open is False


# ------------------------------------------------------------------------ delete
@pytest.mark.asyncio
async def test_deleting_asks_first_and_then_loads_again(
    mock_page, snackbars, monkeypatch
):
    confirm = AsyncMock(return_value=True)
    monkeypatch.setattr(user_files_module, "confirm_dialog", confirm)
    api_client = make_api()
    view = UserFilesView(mock_page, api_client)

    await view.delete_file(UserFileInfo(name="a.txt"))

    assert "„a.txt“" in confirm.call_args.args[2]
    assert "предишната" in confirm.call_args.args[2].lower()
    api_client.delete_user_file.assert_awaited_once_with("a.txt")
    api_client.list_user_files.assert_awaited_once()
    assert snackbars.call_args.kwargs == {"success": True}


@pytest.mark.asyncio
async def test_declining_deletes_nothing(mock_page, snackbars, monkeypatch):
    monkeypatch.setattr(
        user_files_module, "confirm_dialog", AsyncMock(return_value=False)
    )
    api_client = make_api()
    view = UserFilesView(mock_page, api_client)

    await view.delete_file(UserFileInfo(name="a.txt"))

    api_client.delete_user_file.assert_not_awaited()
    api_client.list_user_files.assert_not_awaited()
    snackbars.assert_not_called()


@pytest.mark.asyncio
async def test_a_file_that_is_already_gone_is_not_an_error(
    mock_page, snackbars, monkeypatch
):
    monkeypatch.setattr(
        user_files_module, "confirm_dialog", AsyncMock(return_value=True)
    )
    api_client = make_api()
    api_client.delete_user_file = AsyncMock(side_effect=APIError(404, "no such file"))
    view = UserFilesView(mock_page, api_client)

    await view.delete_file(UserFileInfo(name="a.txt"))

    api_client.list_user_files.assert_awaited_once()
    assert snackbars.call_args.kwargs == {"success": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [APIError(500, "boom"), httpx.ConnectError("x")])
async def test_a_failed_delete_is_an_error_and_nothing_reloads(
    mock_page, snackbars, monkeypatch, error
):
    monkeypatch.setattr(
        user_files_module, "confirm_dialog", AsyncMock(return_value=True)
    )
    api_client = make_api()
    api_client.delete_user_file = AsyncMock(side_effect=error)
    view = UserFilesView(mock_page, api_client)

    await view.delete_file(UserFileInfo(name="a.txt"))

    snackbars.assert_called_once()
    assert "success" not in snackbars.call_args.kwargs
    api_client.list_user_files.assert_not_awaited()


# --------------------------------------------------------------------- lifecycle
def test_the_view_loads_itself_when_it_is_mounted_and_stops_when_it_is_not(mock_page):
    view = UserFilesView(mock_page, make_api())

    view.did_mount()

    mock_page.run_task.assert_called_once_with(view.load)
    task = view._load_task
    view.will_unmount()
    task.cancel.assert_called_once()
    assert view._load_task is None


@pytest.mark.asyncio
async def test_refresh_and_the_refresh_button_load_again(mock_page):
    api_client = make_api()
    view = UserFilesView(mock_page, api_client)

    await view.refresh()
    await view.on_refresh_click(None)

    assert api_client.list_user_files.await_count == 2


# ------------------------------------------------------------------------ the tab
@pytest.fixture
def start_app(monkeypatch, mock_page):
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)

    def start() -> ft.Tabs:
        main_module.main(mock_page)
        return mock_page.add.call_args.args[0]

    return start


def select_tab(tabs: ft.Tabs, index: int) -> None:
    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=index)))


def test_the_files_of_the_assistant_are_the_last_tab_and_built_when_first_opened(
    start_app,
):
    tabs = start_app()

    assert tab_labels(tabs)[-1] == "Файлове на Лили"
    assert not isinstance(tab_view(tabs, 4), UserFilesView)

    select_tab(tabs, 4)

    assert isinstance(tab_view(tabs, 4), UserFilesView)


def test_the_first_visit_does_not_load_twice(start_app, mock_page):
    tabs = start_app()
    mock_page.run_task.reset_mock()

    select_tab(tabs, 4)

    # The view loads itself when it is mounted; the tab handler adds nothing.
    mock_page.run_task.assert_not_called()


def test_the_tab_is_read_again_each_time_it_is_opened_again(start_app, mock_page):
    tabs = start_app()
    select_tab(tabs, 4)
    view = tab_view(tabs, 4)
    select_tab(tabs, 0)
    mock_page.run_task.reset_mock()

    select_tab(tabs, 4)

    mock_page.run_task.assert_called_once_with(view.refresh)
    assert tab_view(tabs, 4) is view


# ------------------------------------------------------ with a real Flet session
@pytest.mark.asyncio
async def test_flet_accepts_the_view_its_list_and_its_dialog(monkeypatch):
    """The mocks cannot say whether Flet takes the controls; a real session can."""
    from helpers import real_session

    from views.ui_utils import is_mounted

    session, connection = real_session()
    page = session.page
    api_client = make_api(
        UserFileList(**LISTING), proposals=[Proposal(**RULE), Proposal(**WRITE)]
    )
    view = UserFilesView(page, api_client)
    page.add(view)
    connection.sent.clear()

    await view.load()

    # The list, the usage line and the card section reached the client as patches.
    assert connection.sent, "loading changed nothing on the screen"
    assert [tile.title.value for tile in view.list_column.controls] == [
        "списък за пазаруване.txt",
        "plan.md",
    ]
    assert view.proposals_section.visible is True

    # The dialog of a file is mounted while it is shown, switching versions reaches
    # the client, and closing it does too.
    shown = []
    real_open = user_files_module.open_dialog

    def spy(target_page, dialog):
        shown.append(dialog)
        real_open(target_page, dialog)

    monkeypatch.setattr(user_files_module, "open_dialog", spy)
    await view.open_file(UserFileInfo(name="a.txt"))
    dialog = shown[0]
    assert is_mounted(dialog) is True
    connection.sent.clear()

    await dialog.actions[0].on_click(None)

    assert dialog.actions[0].content == "Сегашна версия"
    assert connection.sent, "the switch of versions changed nothing on the screen"
    connection.sent.clear()
    user_files_module.close_dialog(dialog)
    assert dialog.open is False
    assert [message.body.id for message in connection.sent] == [dialog._i]
