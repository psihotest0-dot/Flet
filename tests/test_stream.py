"""``parse_frame``: one frame of the chat stream becomes typed events."""

import logging

import pytest

from api.errors import StreamError
from api.stream import (
    ModelUsed,
    Proposals,
    Queued,
    Sources,
    TextChunk,
    Working,
    parse_frame,
)


def test_a_data_frame_with_content_is_a_piece_of_the_answer():
    assert parse_frame(None, '{"content": "Hello"}') == [TextChunk("Hello")]


def test_the_older_text_key_is_a_piece_of_the_answer_too():
    assert parse_frame("message", '{"text": "Hello"}') == [TextChunk("Hello")]


def test_both_keys_in_one_frame_give_both_pieces_in_order():
    assert parse_frame(None, '{"text": "a", "content": "b"}') == [
        TextChunk("a"),
        TextChunk("b"),
    ]


def test_a_json_string_is_text():
    assert parse_frame(None, '"text"') == [TextChunk("text")]


def test_reasoning_is_not_shown_but_the_stream_is_alive():
    # ``LLM_ENABLE_THINKING``: the chat hides it, yet it must reset the "..." timer.
    assert parse_frame(None, '{"reasoning_content": "hmm"}') == [Working()]


@pytest.mark.parametrize("data", ["123", "null", "[1, 2]", "true", "{}"])
def test_a_frame_with_nothing_to_show_is_working(data):
    assert parse_frame(None, data) == [Working()]


def test_a_frame_that_is_not_json_is_taken_as_text_and_logged_by_size(caplog):
    with caplog.at_level(logging.ERROR, logger="api.stream"):
        events = parse_frame(None, "private words")

    assert events == [TextChunk("private words")]
    assert "Could not decode a stream chunk (13 characters)" in caplog.text
    assert "private words" not in caplog.text


def test_a_data_frame_with_an_error_key_raises():
    with pytest.raises(StreamError, match="model is down"):
        parse_frame(None, '{"error": "model is down"}')


def test_an_empty_error_is_still_an_error():
    with pytest.raises(StreamError, match="Unknown error"):
        parse_frame(None, '{"error": ""}')


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ('{"error": "Failed (request_id: abc)"}', "Failed (request_id: abc)"),
        ("Something went wrong", "Something went wrong"),
        ('{"other": 1}', '{"other": 1}'),
    ],
)
def test_an_error_frame_raises_with_the_text_of_the_backend(data, text):
    with pytest.raises(StreamError) as exc_info:
        parse_frame("error", data)

    assert str(exc_info.value) == text


def test_a_sources_frame_is_a_list_of_sources():
    sources = [{"title": "a.pdf"}, {"file_name": "b.pdf"}]

    assert parse_frame("sources", '[{"title": "a.pdf"}, {"file_name": "b.pdf"}]') == [
        Sources(sources)
    ]


def test_an_empty_sources_frame_is_working():
    assert parse_frame("sources", "[]") == [Working()]


def test_an_older_sources_object_gives_the_lists_it_holds():
    data = '{"facts": [{"title": "f"}], "chunks": [{"title": "c"}], "other": [1]}'

    assert parse_frame("sources", data) == [Sources([{"title": "f"}, {"title": "c"}])]


def test_sources_inside_a_data_frame_come_after_its_text():
    data = '{"content": "Hi", "sources": [{"title": "s"}]}'

    assert parse_frame(None, data) == [TextChunk("Hi"), Sources([{"title": "s"}])]


@pytest.mark.parametrize("data", ['"text"', "5", "not json"])
def test_a_sources_frame_that_is_not_a_list_is_working(data, caplog):
    with caplog.at_level(logging.ERROR, logger="api.stream"):
        assert parse_frame("sources", data) == [Working()]


def test_an_undecodable_sources_frame_is_logged_by_size_only(caplog):
    with caplog.at_level(logging.ERROR, logger="api.stream"):
        parse_frame("sources", "private passage")

    assert "Could not decode the sources frame (15 characters)" in caplog.text
    assert "private passage" not in caplog.text


def test_a_proposals_frame_is_a_list():
    assert parse_frame("proposals", '[{"id": 1}]') == [Proposals([{"id": 1}])]


@pytest.mark.parametrize("data", ["not json", '{"id": 1}', "5"])
def test_a_proposals_frame_that_is_not_a_list_gives_nothing(data, caplog):
    with caplog.at_level(logging.ERROR, logger="api.stream"):
        assert parse_frame("proposals", data) == []


def test_a_model_frame_says_who_answered():
    who = '{"provider": "deepseek", "model": "deepseek-flash"}'

    assert parse_frame("model", who) == [
        ModelUsed({"provider": "deepseek", "model": "deepseek-flash"})
    ]


@pytest.mark.parametrize("data", ["not json", '["x"]', '"local"'])
def test_a_model_frame_this_client_cannot_read_gives_nothing(data, caplog):
    with caplog.at_level(logging.WARNING, logger="api.stream"):
        assert parse_frame("model", data) == []

    # Not worth a line in the log.
    assert not caplog.records


def test_the_waiting_frame_is_queued_whatever_its_data_is():
    # ``event: queued`` carries ``{}``; an older client would have taken it for text.
    assert parse_frame("queued", "{}") == [Queued()]
    assert parse_frame("queued", "") == [Queued()]
    assert parse_frame("queued", "not json at all") == [Queued()]


def test_data_without_the_event_name_is_not_the_waiting_frame():
    assert parse_frame(None, "{}") == [Working()]
    assert parse_frame("message", "{}") == [Working()]
