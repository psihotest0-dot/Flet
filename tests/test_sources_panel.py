from __future__ import annotations

import flet as ft
import pytest

from state.settings import Settings, save_settings
from views.chat import (
    ChatMessage,
    ChatView,
    format_source_score,
    source_details,
    source_type_label,
)


def texts_in(control) -> list[str]:
    """Every text value found under ``control``, in display order."""
    found: list[str] = []
    if isinstance(control, ft.Text):
        found.append(control.value)
    for child in getattr(control, "controls", None) or []:
        found.extend(texts_in(child))
    content = getattr(control, "content", None)
    if content is not None:
        found.extend(texts_in(content))
    return found


def opened_panel_texts(mock_page) -> list[str]:
    sheet = mock_page.open.call_args.args[0]
    assert isinstance(sheet, ft.BottomSheet)
    return texts_in(sheet.content)


# ----------------------------------------------------------------- type labels
@pytest.mark.parametrize(
    ("source_type", "label"),
    [
        ("memory", "Памет"),
        ("concept", "Концепция"),
        ("text", "Текст"),
        ("ocr", "OCR"),
        ("OCR", "OCR"),
        (" text ", "Текст"),
    ],
)
def test_source_types_have_their_own_labels(source_type, label):
    assert source_type_label(source_type) == label


def test_unknown_and_missing_source_types():
    assert source_type_label("table") == "table"  # shown as the backend named it
    assert source_type_label(None) == "Документ"
    assert source_type_label("") == "Документ"


# ----------------------------------------------------------------------- score
def test_score_is_shown_as_sent_without_guessing_the_scale():
    # The backend sends 0-100. The former heuristic turned every value <= 1.0
    # into a fraction, so a real 0.8 % was displayed as 80 %.
    assert format_source_score(0.8) == "0.8%"
    assert format_source_score(1.0) == "1.0%"
    assert format_source_score(87.54) == "87.5%"
    assert format_source_score(100) == "100.0%"


def test_score_type_is_named_next_to_the_score():
    assert format_source_score(87.5, "cosine") == "87.5% (косинусово)"
    assert format_source_score(45.0, "rrf") == "45.0% (RRF)"
    assert format_source_score(45.0, "bm25") == "45.0% (bm25)"
    assert format_source_score(45.0, "") == "45.0%"
    assert format_source_score(45.0, None) == "45.0%"


def test_score_that_is_not_a_number_is_shown_as_text():
    assert format_source_score("N/A") == "N/A"
    assert format_source_score(None) == "None"
    # Older mocks and hand-written sources used numeric strings.
    assert format_source_score("95") == "95.0%"


# --------------------------------------------------------------------- details
def test_details_line_has_type_page_and_score():
    src = {
        "source_type": "text",
        "page_number": 12,
        "score": 61.25,
        "score_type": "rrf",
    }

    assert source_details(src) == "• Текст • стр. 12 • Сходство: 61.2% (RRF)"


def test_details_line_leaves_out_a_page_that_is_not_known():
    memory = {"source_type": "memory", "score": 70, "score_type": "cosine"}
    page_zero_is_not_missing = {"source_type": "ocr", "page_number": 0, "score": 1}

    assert source_details(memory) == "• Памет • Сходство: 70.0% (косинусово)"
    assert "стр. 0" in source_details(page_zero_is_not_missing)
    assert "стр." not in source_details({**memory, "page_number": None})


def test_details_line_still_understands_the_old_type_key():
    assert source_details({"type": "OCR", "score": "82"}) == "• OCR • Сходство: 82.0%"


# ----------------------------------------------------------------------- panel
def test_panel_shows_label_page_and_score_type_for_every_source(mock_page):
    message = ChatMessage("assistant", "отговор", page=mock_page)
    sources = [
        {
            "file_name": "Примерна книга.pdf",
            "source_type": "text",
            "page_number": 7,
            "content_snippet": "Първият откъс",
            "score": 83.3,
            "score_type": "rrf",
        },
        {
            "file_name": "Памет (Memory)",
            "source_type": "memory",
            "content_snippet": "Обича кафе",
            "score": 0.5,
            "score_type": "cosine",
        },
    ]

    message.open_sources_panel(sources)

    shown = opened_panel_texts(mock_page)
    assert "Примерна книга.pdf" in shown
    assert "• Текст • стр. 7 • Сходство: 83.3% (RRF)" in shown
    assert "Първият откъс" in shown
    # 0.5 stays 0.5 %: the scale is no longer guessed.
    assert "• Памет • Сходство: 0.5% (косинусово)" in shown


def test_panel_survives_a_source_that_is_not_a_dict(mock_page):
    message = ChatMessage("assistant", "отговор", page=mock_page)

    message.open_sources_panel(["просто текст"])

    shown = opened_panel_texts(mock_page)
    assert "просто текст" in shown
    assert "• Документ • Сходство: N/A" in shown


# ----------------------------------------------------------------- sender name
def test_user_messages_default_to_you_and_the_assistant_keeps_its_name(mock_page):
    mine = ChatMessage("user", "здравей", page=mock_page)
    answer = ChatMessage("assistant", "здрасти", page=mock_page)

    assert mine.sender_label.value == "Ти"
    assert answer.sender_label.value == "Лили"


def test_user_messages_use_the_given_name(mock_page):
    mine = ChatMessage("user", "здравей", page=mock_page, user_name="Иван")
    answer = ChatMessage("assistant", "здрасти", page=mock_page, user_name="Иван")

    assert mine.sender_label.value == "Иван"
    assert mine.sender_name == "Иван"
    assert answer.sender_label.value == "Лили"


def test_chat_view_labels_messages_with_the_saved_display_name(
    mock_page, mock_api_client
):
    save_settings(Settings(display_name="Иван"))

    view = ChatView(mock_page, mock_api_client)
    first = view.add_message("user", "здравей", notify=False)
    reply = view.add_message("assistant", "здрасти", notify=False)

    assert first.sender_label.value == "Иван"
    assert reply.sender_label.value == "Лили"


def test_chat_view_without_a_saved_name_uses_the_default(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)

    assert view.add_message("user", "здравей", notify=False).sender_label.value == "Ти"
