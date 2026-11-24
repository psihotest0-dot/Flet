"""The client and the models against the contract in ``docs/openapi.yaml``.

AGENTS.md rule 6 says the contract is in that file; these tests make it so.

* What the client sends: every method of ``APIClient`` is run against a fake
  backend that answers as the contract says, and each request it makes is checked
  against the operation it hits (path, query, headers, body).
* What the models read: each model has the fields of its schema, and anything
  the schema allows - every optional field present or missing, null where it is
  allowed, dates as Go writes them - is read without loss.
* What the screens know by hand: the lists kept in the views (categories, states,
  modes, limits) are the ones the contract names, so a new value there is noticed
  here and not by a user.

``tests/contract.py`` holds the machinery; its own checks are at the end.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC

import httpx
import pytest
import respx
from contract import (
    PATHS,
    SCHEMAS,
    Backend,
    check_request,
    enum_of,
    example,
    instances,
    problems,
    properties_of,
    resolve,
    schema_named,
    sse,
    utc,
)
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import api.limits as limits
from api.client import APIClient
from api.errors import APIError, StreamError
from api.models import (
    MEMORY_ACTIVE,
    MEMORY_CANDIDATE,
    RULE_KIND,
    STYLE_KIND,
    WRITE_FILE_KIND,
    ChatAttachment,
    IngestedFile,
    Memory,
    Message,
    OracleDecision,
    OracleDecisionPage,
    Proposal,
    Rule,
    TelegramSession,
    TelegramSessionList,
    UserFile,
    UserFileInfo,
    UserFileList,
)
from api.stream import (
    ModelUsed,
    Proposals,
    Queued,
    Remembered,
    Sources,
    TextChunk,
    Working,
)
from views.files import CATEGORIES, EMBEDDING_STATES, FINAL_STATUSES
from views.oracle import MODES, SERVED, STATUS_FAILED, VERDICTS
from views.proposals import FILE_MODE_HINTS

BASE = "http://localhost:8080"
USER = "123e4567-e89b-12d3-a456-426614174000"
ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SESSION = "5f3a9c1e-7b2d-4c8a-9e61-0d4b7a2c8f13"


# ------------------------------------------------------- what the client sends
async def drain(events):
    return [event async for event in events]


def an_upload(client, folder):
    path = folder / "book.pdf"
    path.write_bytes(b"%PDF-1.4 a book")
    return client.upload_file(path, "книга.pdf", "application/pdf")


def a_bill(client, folder):
    path = folder / "bill.pdf"
    path.write_bytes(b"%PDF-1.4 a bill")
    return client.upload_bill(SESSION, path, "сметка.pdf")


def case(method, variant, call):
    """One way to call ``APIClient.method``; ``call(client, folder)`` makes the call."""
    return pytest.param(method, call, id=f"{method}-{variant}")


CASES = [
    case("check_health", "plain", lambda c, d: c.check_health()),
    case("upload_file", "plain", an_upload),
    case("upload_bill", "plain", a_bill),
    case("delete_file", "plain", lambda c, d: c.delete_file(ID)),
    case("list_files", "plain", lambda c, d: c.list_files()),
    case("get_file_status", "plain", lambda c, d: c.get_file_status(ID)),
    case(
        "update_file_category",
        "law",
        lambda c, d: c.update_file_category(ID, "law"),
    ),
    case("list_attachments", "plain", lambda c, d: c.list_attachments(SESSION)),
    case("get_task_plan", "plain", lambda c, d: c.get_task_plan(SESSION)),
    case(
        "upload_attachment",
        "plain",
        lambda c, d: c.upload_attachment(SESSION, "лог.txt", "ред 1\nред 2\n"),
    ),
    case("delete_attachment", "plain", lambda c, d: c.delete_attachment(ID)),
    case("delete_session", "plain", lambda c, d: c.delete_session(SESSION)),
    case("delete_session", "odd-id", lambda c, d: c.delete_session("a/b c")),
    case("create_memory", "minimal", lambda c, d: c.create_memory("спомен")),
    case(
        "create_memory",
        "everything",
        lambda c, d: c.create_memory(
            "спомен",
            status="active",
            source_message_id=ID,
            importance=9,
            metadata={"from": "чат"},
        ),
    ),
    case("get_messages", "all", lambda c, d: c.get_messages(SESSION)),
    case("get_messages", "limited", lambda c, d: c.get_messages(SESSION, limit=200)),
    case(
        "get_messages",
        "before",
        lambda c, d: c.get_messages(SESSION, limit=101, before=ID),
    ),
    case("list_telegram_sessions", "plain", lambda c, d: c.list_telegram_sessions()),
    case("list_memories", "all", lambda c, d: c.list_memories()),
    case("list_memories", "active", lambda c, d: c.list_memories("active")),
    case("list_memories", "candidate", lambda c, d: c.list_memories("candidate")),
    case(
        "update_memory", "plain", lambda c, d: c.update_memory(ID, "текст", "active", 3)
    ),
    case(
        "update_memory",
        "with-metadata",
        lambda c, d: c.update_memory(ID, "текст", "candidate", 10, {"k": 1}),
    ),
    case("delete_memory", "plain", lambda c, d: c.delete_memory(ID)),
    case("list_proposals", "plain", lambda c, d: c.list_proposals()),
    case("approve_proposal", "plain", lambda c, d: c.approve_proposal(7)),
    case("reject_proposal", "plain", lambda c, d: c.reject_proposal(7)),
    case("list_user_files", "plain", lambda c, d: c.list_user_files()),
    case("get_user_file", "current", lambda c, d: c.get_user_file("бележки.md")),
    case(
        "get_user_file",
        "previous-odd-name",
        lambda c, d: c.get_user_file("списък [1] а/б.txt", previous=True),
    ),
    case("delete_user_file", "plain", lambda c, d: c.delete_user_file("бележки.md")),
    case(
        "add_user_file_to_library",
        "plain",
        lambda c, d: c.add_user_file_to_library("статия.md"),
    ),
    case("list_rules", "plain", lambda c, d: c.list_rules()),
    case("delete_rule", "plain", lambda c, d: c.delete_rule(4)),
    case("list_oracle_decisions", "newest", lambda c, d: c.list_oracle_decisions()),
    case(
        "list_oracle_decisions",
        "everything",
        lambda c, d: c.list_oracle_decisions(limit=200, before_id=7, symbol="AAPL"),
    ),
    case(
        "regenerate_chat",
        "default",
        lambda c, d: drain(c.regenerate_chat(SESSION)),
    ),
    case(
        "regenerate_chat",
        "paid",
        lambda c, d: drain(c.regenerate_chat(SESSION, use_paid=True)),
    ),
    case(
        "stream_chat",
        "default",
        lambda c, d: drain(c.stream_chat(SESSION, "въпрос")),
    ),
    case(
        "stream_chat",
        "paid",
        lambda c, d: drain(c.stream_chat(SESSION, "въпрос", use_paid=True)),
    ),
    case(
        "stream_chat",
        "local",
        lambda c, d: drain(c.stream_chat(SESSION, "въпрос", use_paid=False)),
    ),
]
# Not requests to the backend.
NOT_CALLS = {"aclose", "set_base_url"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "call"), CASES)
async def test_what_the_client_sends_is_what_the_contract_takes(method, call, tmp_path):
    backend = Backend()
    client = APIClient(BASE, USER)
    refused = None
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=backend)
        try:
            await call(client, tmp_path)
        except APIError as error:
            refused = error
        finally:
            await client.aclose()

    assert backend.requests, f"{method} sent nothing"
    assert not backend.problems(), "\n".join(backend.problems())
    # The answers are built from the contract, so the models read them: a call
    # that fails here fails on something a real backend would also send.
    assert refused is None, f"the backend answered as the contract says: {refused}"


def test_every_method_of_the_client_is_checked_against_the_contract():
    public = {
        name
        for name, _ in inspect.getmembers(APIClient, inspect.isfunction)
        if not name.startswith("_")
    }

    unchecked = public - NOT_CALLS - {param.values[0] for param in CASES}

    assert not unchecked, f"add a case for {sorted(unchecked)} to CASES"


@pytest.mark.asyncio
async def test_the_frames_the_contract_describes_become_events():
    source = example(schema_named("SourceInfo"))
    proposal = example(schema_named("Proposal"))
    who = {"provider": "deepseek", "model": "deepseek-flash"}
    body = sse(
        ("queued", {}),
        ("sources", [source]),
        (None, {"reasoning_content": "мисля"}),
        (None, {"content": "Дунав "}),
        (None, {"content": "е граница."}),
        ("proposals", [proposal]),
        ("model", who),
        ("plan", {"remembered": ["почива на дивана"]}),
        (None, "[DONE]"),
    )

    with respx.mock as router:
        router.post(f"{BASE}/chat/stream").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        )
        async with APIClient(BASE, USER) as client:
            events = await drain(client.stream_chat(SESSION, "въпрос"))

    assert events == [
        Queued(),
        Sources([source]),
        Working(),
        TextChunk("Дунав "),
        TextChunk("е граница."),
        Proposals([proposal]),
        ModelUsed(who),
        Remembered(["почива на дивана"]),
    ]
    assert Proposal(**proposal).id == proposal["id"]


@pytest.mark.asyncio
async def test_the_error_frame_the_contract_describes_ends_the_stream():
    body = sse((None, {"content": "Дун"}), ("error", {"error": "моделът не отговаря"}))

    with respx.mock as router:
        router.post(f"{BASE}/chat/regenerate").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        )
        async with APIClient(BASE, USER) as client:
            with pytest.raises(StreamError, match="моделът не отговаря"):
                await drain(client.regenerate_chat(SESSION))


# --------------------------------------------------------- what the models read
PAIRS = [
    (Message, "Message"),
    (Memory, "Memory"),
    (Proposal, "Proposal"),
    (UserFileInfo, "UserFileInfo"),
    (UserFileList, "UserFileList"),
    (TelegramSession, "TelegramSession"),
    (TelegramSessionList, "TelegramSessionList"),
    (UserFile, "UserFile"),
    (Rule, "Rule"),
    (OracleDecision, "OracleEvaluation"),
    (OracleDecisionPage, "OracleDecisionList"),
    (IngestedFile, "IngestedFile"),
    (ChatAttachment, "ChatAttachment"),
]
PAIR_IDS = [f"{model.__name__}-{schema}" for model, schema in PAIRS]

# Fields a model asks for although the contract does not call them required, by
# the name of the schema: a row without one is no use to the screen (no id to act
# on), so a payload that lacks it fails to read. Everything else the contract
# leaves open is optional in the model too, so one odd item cannot fail a whole
# list. A new entry here is a decision: say why in the review.
STRICTER = {
    "Proposal": {"id"},
    "Rule": {"id", "key", "value"},
    "OracleEvaluation": {"id"},
    "ChatAttachment": {"id", "name"},
}


def assert_kept(value, given, schema, where="$"):
    """``value``, read by a model from ``given``, says what ``given`` says."""
    schema = resolve(schema)
    if given is None:
        assert value is None, where
    elif schema.get("format") == "date-time":
        # To the second: the model and the standard library may cut a fraction
        # longer than six digits differently.
        assert value.astimezone(UTC).replace(microsecond=0) == utc(given), where
    elif isinstance(value, BaseModel):
        for name, item in given.items():
            assert_kept(
                getattr(value, name),
                item,
                schema["properties"][name],
                f"{where}.{name}",
            )
    elif schema.get("type") == "array":
        assert len(value) == len(given), where
        for i, (kept, item) in enumerate(zip(value, given, strict=True)):
            assert_kept(kept, item, schema["items"], f"{where}[{i}]")
    else:
        assert value == given, where


@pytest.mark.parametrize(("model", "schema"), PAIRS, ids=PAIR_IDS)
def test_the_model_has_the_fields_of_its_schema(model, schema):
    in_contract = set(properties_of(schema))
    in_model = set(model.model_fields)

    assert in_model == in_contract, (
        f"only in the contract (the screen never sees them): "
        f"{sorted(in_contract - in_model)}; only in the model (the backend never "
        f"sends them): {sorted(in_model - in_contract)}"
    )


@pytest.mark.parametrize(("model", "schema"), PAIRS, ids=PAIR_IDS)
def test_the_model_asks_for_no_more_than_the_contract_promises(model, schema):
    promised = set(resolve(schema_named(schema)).get("required", []))
    asked = {name for name, field in model.model_fields.items() if field.is_required()}

    assert asked - promised == STRICTER.get(schema, set())


@pytest.mark.parametrize(("model", "schema"), PAIRS, ids=PAIR_IDS)
@given(data=st.data())
def test_anything_the_contract_allows_is_read_without_loss(model, schema, data):
    reference = {"$ref": f"#/components/schemas/{schema}"}
    given_value = data.draw(instances(reference, STRICTER))

    assert_kept(model(**given_value), given_value, reference)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
@given(data=st.data())
def test_the_generator_makes_only_what_the_checker_accepts(name, data):
    # Without this the test above could pass over values the contract does not
    # allow, and the checker could be too strict to ever be used.
    value = data.draw(instances(schema_named(name)))

    assert not problems(schema_named(name), value)


# --------------------------------------------- what the screens know by hand
def test_the_categories_offered_are_the_ones_of_the_contract():
    assert [key for key, _label in CATEGORIES] == resolve(schema_named("FileCategory"))[
        "enum"
    ]


def test_every_vector_state_of_a_file_has_a_label():
    assert set(EMBEDDING_STATES) == set(enum_of("IngestedFile", "embedding_status"))


def test_the_final_statuses_of_a_file_exist_in_the_contract():
    assert set(FINAL_STATUSES) <= set(enum_of("IngestedFile", "status"))


def test_every_file_mode_of_a_proposal_has_a_hint():
    assert set(FILE_MODE_HINTS) == set(enum_of("Proposal", "file_mode"))


def test_the_proposal_kinds_are_the_ones_the_card_knows():
    # A new kind: teach ``views.proposals.kind_label`` and the card, then add it here.
    assert set(enum_of("Proposal", "kind")) == {
        "save_global_rule",
        "save_style_note",
        "create_memory",
        WRITE_FILE_KIND,
    }


def test_the_memory_statuses_and_rule_kinds_are_the_contracts():
    assert {MEMORY_ACTIVE, MEMORY_CANDIDATE} == set(enum_of("Memory", "status"))
    assert {RULE_KIND, STYLE_KIND} == set(enum_of("Rule", "kind"))


def test_the_oracle_tab_knows_every_verdict_status_and_mode():
    assert set(VERDICTS) == set(enum_of("OracleEvaluation", "verdict"))
    assert STATUS_FAILED in enum_of("OracleEvaluation", "status")
    assert set(MODES) == set(enum_of("OracleRequest", "mode")) - {""}


def test_the_oracle_tab_knows_every_source_of_a_row_that_did_not_reach_the_model():
    assert set(SERVED) == set(enum_of("OracleEvaluation", "served_from"))


def stated(text: str, pattern: str) -> tuple[int, ...]:
    """The numbers a sentence of the contract gives, by ``pattern``."""
    found = re.search(pattern, text)
    assert found, f"the contract no longer says {pattern!r} in: {text!r}"
    return tuple(int(number) for number in found.groups())


def body_schema(path: str, method: str = "post") -> dict:
    operation = PATHS[path][method]
    return operation["requestBody"]["content"]["application/json"]["schema"]


def test_the_limits_of_the_client_are_the_ones_the_contract_states():
    memory = body_schema("/memories")["properties"]
    chat = body_schema("/chat/stream")["properties"]
    attachment = PATHS["/chat/attachments"]["post"]["description"]
    upload = PATHS["/v1/files"]["post"]["description"]

    assert (
        limits.MAX_CONTENT_BYTES
        == stated(memory["content"]["description"], r"at most (\d+) bytes")[0]
    )
    assert (limits.MIN_IMPORTANCE, limits.MAX_IMPORTANCE) == (
        memory["importance"]["minimum"],
        memory["importance"]["maximum"],
    )
    assert (
        limits.MESSAGE_TOKEN_LIMIT
        == stated(chat["content"]["description"], r"At most (\d+) tokens")[0]
    )
    assert limits.MAX_UPLOAD_MB == stated(upload, r"limit is (\d+) MB")[0]
    per_file, files, total = stated(
        attachment, r"(\d+) KB a file, (\d+) files and (\d+) MB a conversation"
    )
    assert limits.MAX_ATTACHMENT_BYTES == per_file * 1024
    assert limits.MAX_ATTACHMENTS == files
    assert limits.MAX_ATTACHMENTS_TOTAL_BYTES == total * 1024 * 1024


# --------------------------------------- the checker itself: it must not be blind
HEADERS = {"X-User-ID": USER}


def a_request(method, path, *, headers=HEADERS, **kwargs):
    request = httpx.Request(method, f"{BASE}{path}", headers=headers, **kwargs)
    request.read()
    return request


FINE = [
    a_request("GET", "/memories"),
    a_request("GET", "/memories?status=active"),
    a_request("POST", "/memories", json={"content": "a", "importance": 5}),
    a_request(
        "POST", "/memories", json={"content": "a", "importance": 5, "metadata": None}
    ),
    a_request("GET", f"/messages?session_id={SESSION}&limit=200"),
    a_request("DELETE", f"/memories/{ID}"),
    a_request("POST", "/proposals/7/approve"),
    a_request("GET", "/workspace/files/%D0%B1%D0%B5%D0%BB%D0%B5%D0%B6%D0%BA%D0%B8.md"),
    a_request("POST", "/v1/files", files={"file": ("a.txt", b"x", "text/plain")}),
]
WRONG = [
    ("GET", "/nowhere", {}, "no such operation"),
    ("PATCH", "/memories", {}, "no such operation"),
    ("POST", "/memories", {"json": {"content": "a"}}, "importance: required"),
    (
        "POST",
        "/memories",
        {"json": {"content": "a", "importance": 11}},
        "is above 10",
    ),
    (
        "POST",
        "/memories",
        {"json": {"content": "a", "importance": "5"}},
        "is not integer",
    ),
    (
        "POST",
        "/memories",
        {"json": {"content": "a", "importance": 5, "colour": "red"}},
        "no such property",
    ),
    (
        "POST",
        "/memories",
        {"json": {"content": "a", "importance": 5, "status": "done"}},
        "is not one of",
    ),
    ("GET", "/memories?status=gone", {}, "is not one of"),
    ("GET", "/messages?limit=0", {}, "is below 1"),
    ("GET", "/messages?colour=1", {}, "no query parameter"),
    ("GET", "/proposals", {"headers": {}}, "header X-User-ID is missing"),
    ("DELETE", "/memories/not-a-uuid", {}, "is not a UUID"),
    ("POST", "/proposals/7/approve", {"json": {"x": 1}}, "sends a body"),
    ("POST", "/proposals/seven/approve", {}, "is not integer"),
    (
        "POST",
        "/v1/files",
        {"files": {"doc": ("a.txt", b"x", "text/plain")}},
        "form field 'doc' is not in the contract",
    ),
    ("POST", "/v1/files", {"json": {"file": "x"}}, "the contract takes"),
]


@pytest.mark.parametrize("request_", FINE, ids=lambda r: f"{r.method} {r.url.path}")
def test_the_checker_accepts_what_the_contract_allows(request_):
    assert check_request(request_) == []


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "complaint"),
    WRONG,
    ids=[f"{method} {path} {complaint}" for method, path, _, complaint in WRONG],
)
def test_the_checker_names_what_the_contract_does_not_allow(
    method, path, kwargs, complaint
):
    found = check_request(a_request(method, path, **kwargs))

    assert any(complaint in line for line in found), found


def test_a_keyword_the_checker_does_not_know_is_an_error_not_a_pass():
    with pytest.raises(NotImplementedError, match="multipleOf"):
        problems({"type": "integer", "multipleOf": 5}, 10)
