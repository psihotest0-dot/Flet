"""``docs/openapi.yaml`` as the tests use it.

The contract is the one place that says what the backend takes and sends. This
module reads it and gives the tests three things:

* ``problems`` - a small checker for the part of OpenAPI the file uses. A keyword
  it does not know raises instead of passing: a constraint added to the contract
  must not be silently ignored by the checks that rest on it.
* ``example`` and ``instances`` - the one plain example, and a Hypothesis strategy
  for any valid value, of a schema.
* ``check_request`` and ``Backend`` - a fake backend that answers from the
  contract and keeps every request, and the check of one request against the
  operation it hits (the path, the query, the headers and the body).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from hypothesis import find
from hypothesis import strategies as st
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CONTRACT: dict = YAML(typ="safe").load(
    (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
)
PATHS: dict = CONTRACT["paths"]
SCHEMAS: dict = CONTRACT["components"]["schemas"]

# What the checker reads from a schema, and what it may skip because it only
# describes. Anything else raises ``NotImplementedError``.
ANNOTATIONS = {"description", "example", "default", "title"}
CHECKED = {
    "type",
    "nullable",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "items",
    "properties",
    "required",
    "additionalProperties",
    "oneOf",
}
FORMATS = {"uuid", "date-time", "int64", "double", "binary"}

UUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
EXAMPLE_UUID = "123e4567-e89b-12d3-a456-426614174000"
EXAMPLE_DATE_TIME = "2026-11-20T10:30:00Z"
INT64 = 2**63


def resolve(node: Any) -> Any:
    """Follow ``$ref`` links (``#/components/...``) to the node they name."""
    while isinstance(node, dict) and "$ref" in node:
        target: Any = CONTRACT
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def schema_named(name: str) -> dict:
    return SCHEMAS[name]


def properties_of(name: str) -> dict:
    return resolve(schema_named(name))["properties"]


def enum_of(name: str, field: str) -> list:
    """The values the contract allows in ``field`` of the schema ``name``."""
    return resolve(properties_of(name)[field])["enum"]


# ------------------------------------------------------------------- checking
def _is(kind: str, value: Any) -> bool:
    match kind:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
    raise NotImplementedError(f"the checker does not know the type {kind!r}")


def problems(schema: Any, value: Any, where: str = "$", *, strict: bool = False):
    """What is wrong with ``value`` against ``schema``, one line each; ``[]`` if fine.

    ``strict`` also refuses a property the schema does not list: for what the
    client sends, where an unknown field is a mistake. What it reads may carry
    more than the contract says.
    """
    schema = resolve(schema)
    if unknown := set(schema) - ANNOTATIONS - CHECKED:
        raise NotImplementedError(
            f"{where}: the checker does not know {sorted(unknown)}; "
            "teach tests/contract.py before the contract uses it"
        )
    if value is None:
        return [] if schema.get("nullable") else [f"{where}: null is not allowed"]
    if "oneOf" in schema:
        if any(not problems(o, value, where, strict=strict) for o in schema["oneOf"]):
            return []
        return [f"{where}: {value!r} fits none of the options"]
    kind = schema.get("type")
    if kind and not _is(kind, value):
        return [f"{where}: {value!r} is not {kind}"]

    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            found.append(f"{where}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            found.append(f"{where}: {value} is above {schema['maximum']}")
        if schema.get("format") == "int64" and not -INT64 <= value < INT64:
            found.append(f"{where}: {value} does not fit int64")
    elif kind == "string":
        found += _string_problems(schema, value, where)
    elif kind == "array":
        for i, item in enumerate(value):
            found += problems(schema.get("items", {}), item, f"{where}[{i}]")
    elif kind == "object":
        found += _object_problems(schema, value, where, strict)
    if (fmt := schema.get("format")) and fmt not in FORMATS:
        raise NotImplementedError(f"{where}: the checker does not know format {fmt!r}")
    return found


def _string_problems(schema: dict, value: str, where: str) -> list[str]:
    found = []
    if len(value) < schema.get("minLength", 0):
        found.append(f"{where}: shorter than {schema['minLength']}")
    if len(value) > schema.get("maxLength", len(value)):
        found.append(f"{where}: longer than {schema['maxLength']}")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        found.append(f"{where}: {value!r} does not fit {schema['pattern']}")
    if schema.get("format") == "uuid" and not UUID.match(value):
        found.append(f"{where}: {value!r} is not a UUID")
    if schema.get("format") == "date-time" and not DATE_TIME.match(value):
        found.append(f"{where}: {value!r} is not an RFC 3339 date-time")
    return found


def _object_problems(schema: dict, value: dict, where: str, strict: bool) -> list[str]:
    found = [
        f"{where}.{name}: required, but missing"
        for name in schema.get("required", [])
        if name not in value
    ]
    properties = schema.get("properties", {})
    for name, item in value.items():
        if name in properties:
            found += problems(properties[name], item, f"{where}.{name}", strict=strict)
        elif strict and properties:
            found.append(f"{where}.{name}: the contract has no such property")
    return found


# ------------------------------------------------------------------- examples
def example(schema: Any) -> Any:
    """The plain example of a schema: every property present, the first enum value."""
    schema = resolve(schema)
    if "enum" in schema:
        return schema["enum"][0]
    if "oneOf" in schema:
        return example(schema["oneOf"][0])
    match schema.get("type"):
        case "object":
            return {k: example(v) for k, v in schema.get("properties", {}).items()}
        case "array":
            return [example(schema["items"])]
        case "string":
            return _string_example(schema)
        case "integer":
            return schema.get("minimum", 1)
        case "number":
            return 0.5
        case "boolean":
            return True
    return "x"


def _string_example(schema: dict) -> str:
    if schema.get("format") == "uuid":
        return EXAMPLE_UUID
    if schema.get("format") == "date-time":
        return EXAMPLE_DATE_TIME
    if "pattern" in schema:
        return find(st.from_regex(schema["pattern"], fullmatch=True), lambda _: True)
    return "x"


def rfc3339() -> st.SearchStrategy[str]:
    """Dates as a Go backend writes them: any fraction up to nanoseconds, ``Z`` or an offset."""
    moment = st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31)
    ).map(lambda d: d.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"))
    fraction = st.one_of(
        st.just(""),
        st.text(alphabet="0123456789", min_size=1, max_size=9).map(".".__add__),
    )
    zone = st.one_of(
        st.just("Z"),
        st.timedeltas(timedelta(hours=-12), timedelta(hours=14)).map(_offset),
    )
    return st.builds("".join, st.tuples(moment, fraction, zone))


def _offset(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def instances(
    schema: Any, also_required: dict[str, set[str]] | None = None
) -> st.SearchStrategy:
    """Any value that fits ``schema``: optional properties come and go, null where allowed.

    ``also_required`` names properties to always include, by the name of the
    schema they belong to (``{"Rule": {"id"}}``): for a reader that is stricter
    than the contract, which should be given what it needs and not only what
    is allowed. It reaches schemas nested in the one asked for.
    """
    also_required = also_required or {}
    name = None
    if isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
    schema = resolve(schema)
    base = _base_strategy(schema, also_required, also_required.get(name, set()))
    return st.one_of(st.none(), base) if schema.get("nullable") else base


def _base_strategy(
    schema: dict, also_required: dict[str, set[str]], extra: set[str]
) -> st.SearchStrategy:
    if "enum" in schema:
        return st.sampled_from(schema["enum"])
    if "oneOf" in schema:
        options = (instances(option, also_required) for option in schema["oneOf"])
        return st.one_of(*options)
    match schema.get("type"):
        case "string":
            return _string_strategy(schema)
        case "integer":
            return st.integers(
                schema.get("minimum", -(2**53)), schema.get("maximum", 2**53)
            )
        case "number":
            return st.floats(allow_nan=False, allow_infinity=False)
        case "boolean":
            return st.booleans()
        case "array":
            return st.lists(instances(schema["items"], also_required), max_size=3)
        case "object":
            return _object_strategy(schema, also_required, extra)
    return st.one_of(st.text(max_size=5), st.integers(-1000, 1000))


def _string_strategy(schema: dict) -> st.SearchStrategy[str]:
    fmt = schema.get("format")
    if fmt == "uuid":
        return st.uuids().map(str)
    if fmt == "date-time":
        return rfc3339()
    if "pattern" in schema:
        return st.from_regex(schema["pattern"], fullmatch=True)
    if fmt:
        raise NotImplementedError(f"no strategy for the string format {fmt!r}")
    return st.text(
        min_size=schema.get("minLength", 0), max_size=schema.get("maxLength", 40)
    )


def _object_strategy(
    schema: dict, also_required: dict[str, set[str]], extra: set[str]
) -> st.SearchStrategy[dict]:
    properties = schema.get("properties")
    if not properties:
        return st.dictionaries(
            st.text(max_size=5),
            st.one_of(st.integers(-9, 9), st.text(max_size=5), st.booleans()),
            max_size=3,
        )
    required = set(schema.get("required", [])) | extra
    return st.fixed_dictionaries(
        {
            name: instances(item, also_required)
            for name, item in properties.items()
            if name in required
        },
        optional={
            name: instances(item, also_required)
            for name, item in properties.items()
            if name not in required
        },
    )


# ------------------------------------------------------- operations and requests
def _template_regex(template: str) -> re.Pattern:
    parts = re.split(r"\{(\w+)\}", template)
    # Literal text and parameter names alternate; a name takes one path segment.
    body = "".join(
        re.escape(part) if i % 2 == 0 else f"(?P<{part}>[^/]+)"
        for i, part in enumerate(parts)
    )
    return re.compile(f"^{body}$")


_TEMPLATES = {template: _template_regex(template) for template in PATHS}


def find_operation(method: str, raw_path: str):
    """``(path item, operation, path values)`` of the route a request hits, or ``None``.

    ``raw_path`` is the path as it went on the wire, still percent-encoded, so a
    name with a slash in it cannot be mistaken for two segments. A path without a
    name in it wins over one with.
    """
    hits = [
        (template, match)
        for template, regex in _TEMPLATES.items()
        if (match := regex.match(raw_path))
    ]
    for template, match in sorted(hits, key=lambda hit: len(hit[1].groupdict())):
        item = PATHS[template]
        if method.lower() in item:
            return item, item[method.lower()], match.groupdict()
    return None


def _coerce(schema: Any, text: str) -> Any:
    """A path or query value as the type the schema says, when the text allows."""
    kind = resolve(schema).get("type")
    if kind == "integer" and re.fullmatch(r"-?\d+", text):
        return int(text)
    if kind == "boolean" and text in ("true", "false"):
        return text == "true"
    return text


def raw_path_of(request: httpx.Request) -> str:
    return request.url.raw_path.split(b"?")[0].decode("ascii")


def check_request(request: httpx.Request) -> list[str]:
    """What a request does that the contract does not allow, one line each."""
    label = f"{request.method} {raw_path_of(request)}"
    found = find_operation(request.method, raw_path_of(request))
    if found is None:
        return [f"{label}: the contract has no such operation"]
    item, operation, path_values = found
    declared = [
        resolve(p) for p in item.get("parameters", []) + operation.get("parameters", [])
    ]
    problem_lines: list[str] = []

    for parameter in (p for p in declared if p["in"] == "path"):
        value = _coerce(parameter["schema"], unquote(path_values[parameter["name"]]))
        problem_lines += problems(
            parameter["schema"], value, f"{label} {parameter['name']}"
        )

    for parameter in (p for p in declared if p["in"] == "header"):
        sent = request.headers.get(parameter["name"])
        if sent is None:
            if parameter.get("required"):
                problem_lines.append(f"{label}: header {parameter['name']} is missing")
        else:
            problem_lines += problems(
                parameter["schema"], sent, f"{label} {parameter['name']}"
            )

    queries = {p["name"]: p for p in declared if p["in"] == "query"}
    sent_names = {name for name, _ in request.url.params.multi_items()}
    for name, text in request.url.params.multi_items():
        if name not in queries:
            problem_lines.append(
                f"{label}: the contract has no query parameter {name!r}"
            )
            continue
        value = _coerce(queries[name]["schema"], text)
        problem_lines += problems(queries[name]["schema"], value, f"{label} ?{name}")
    problem_lines += [
        f"{label}: the query parameter {name!r} is required"
        for name, parameter in queries.items()
        if parameter.get("required") and name not in sent_names
    ]

    return problem_lines + _body_problems(label, operation, request)


def _body_problems(label: str, operation: dict, request: httpx.Request) -> list[str]:
    declared = operation.get("requestBody")
    if declared is None:
        if request.content:
            return [f"{label}: sends a body the contract does not know"]
        return []
    media = declared["content"]
    sent = request.headers.get("content-type", "").split(";")[0]
    if sent not in media:
        taken = sorted(media)
        return [
            f"{label}: sends {sent or 'no content type'}, the contract takes {taken}"
        ]
    schema = resolve(media[sent]["schema"])
    if sent == "application/json":
        body = json.loads(request.content)
        return problems(schema, body, f"{label} body", strict=True)
    if sent == "multipart/form-data":
        return _form_problems(label, schema, request)
    raise NotImplementedError(f"{label}: the checker does not read {sent}")


def _form_problems(label: str, schema: dict, request: httpx.Request) -> list[str]:
    """The fields of a multipart body against the properties of its schema."""
    sent = {
        name.decode()
        for name in re.findall(
            rb'Content-Disposition: form-data; name="([^"]*)"', request.content
        )
    }
    found = [
        f"{label}: form field {name!r} is not in the contract"
        for name in sorted(sent - set(schema["properties"]))
    ]
    found += [
        f"{label}: form field {name!r} is required, but missing"
        for name in schema.get("required", [])
        if name not in sent
    ]
    return found


class Backend:
    """A backend that answers every request as the contract says, and keeps them.

    A route the contract does not have gets a 404, like a real backend; the
    request is kept all the same, so a test can say what was asked.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        found = find_operation(request.method, raw_path_of(request))
        if found is None:
            return httpx.Response(404, text="404 page not found")
        return self.answer(found[1])

    @staticmethod
    def answer(operation: dict) -> httpx.Response:
        """The success answer of an operation, built from its response schema."""
        code = min(int(code) for code in operation["responses"] if str(code)[0] == "2")
        content = resolve(operation["responses"][str(code)]).get("content", {})
        if "application/json" in content:
            return httpx.Response(
                code, json=example(content["application/json"]["schema"])
            )
        if "text/event-stream" in content:
            return httpx.Response(
                code,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )
        if "text/plain" in content:
            return httpx.Response(code, text="OK")
        return httpx.Response(code)

    def problems(self) -> list[str]:
        """Everything the requests so far do against the contract."""
        return [line for request in self.requests for line in check_request(request)]


def sse(*frames: tuple[str | None, Any]) -> bytes:
    """A Server-Sent Events body: ``(event name or None, data)`` per frame.

    ``data`` that is not text is written as JSON, as the backend does.
    """
    out = []
    for event, data in frames:
        if event:
            out.append(f"event: {event}\n")
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        out.append(f"data: {text}\n\n")
    return "".join(out).encode()


def utc(value: str) -> datetime:
    """An RFC 3339 date as a datetime, to the second."""
    return datetime.fromisoformat(value).astimezone(UTC).replace(microsecond=0)
