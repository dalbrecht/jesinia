# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading an agent's JSON reply against one of ``schemas/*.json`` (spec §7 "Suggestions":
JSON outputs validated against schemas, like the review tool's jobs).

The reply is untrusted: it is bounded before it is parsed (length, nesting, integer digits),
JSON's non-numbers are refused, and a schema violation is described in Jesinia's words (paths
and rule names, never the reply's own values), so the corrective retry can't echo injected text
back as an instruction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, ValidationError, validators

from jesinia.apps.issues.domain.suggestions import InvalidAgentOutputError, UnknownAgentSchemaError

MAX_REPLY_CHARS = 200_000
MAX_REPLY_DEPTH = 32
MAX_INT_DIGITS = 18
_ERRORS_SHOWN = 5
_FENCED_REPLY = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n(.*)\n```$", re.DOTALL)
SCHEMA_NAMES = ("triage", "duplicates", "grouping", "priority", "queue", "brief")


def _is_json_integer(checker: object, instance: object) -> bool:
    """Only a JSON integer is an integer (JSON Schema also accepts ``3.0``)."""
    return isinstance(instance, int) and not isinstance(instance, bool)


# The stubs leave ``extend`` unannotated; this is the shape it has.
_extend: Callable[..., type[Draft202012Validator]] = validators.extend
_Validator = _extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_json_integer),
)


@cache
def _schema_text(name: str) -> str:
    return (files("jesinia.apps.issues.schemas") / f"{name}.json").read_text(encoding="utf-8")


def schema_for(name: str) -> dict[str, object]:
    """The JSON schema ``schemas/<name>.json`` (a fresh copy each call).

    Raises:
        UnknownAgentSchemaError: ``name`` isn't one of ``SCHEMA_NAMES``.
    """
    if name not in SCHEMA_NAMES:
        raise UnknownAgentSchemaError(f"no issues schema named {name!r}")
    schema: dict[str, object] = json.loads(_schema_text(name))
    return schema


def _nesting_depth(text: str) -> int:
    """How deep arrays and objects nest in JSON ``text`` (brackets in strings excluded)."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _bounded_int(digits: str) -> int:
    if len(digits.lstrip("-")) > MAX_INT_DIGITS:
        raise InvalidAgentOutputError(f"the reply has an integer over {MAX_INT_DIGITS} digits")
    return int(digits)


def _no_constant(name: str) -> object:
    raise InvalidAgentOutputError(f"the reply has {name}, which isn't a JSON number")


def _problem(error: ValidationError) -> str:
    detail = ""
    if error.validator == "required" and isinstance(error.instance, Mapping):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [name for name in required if name not in error.instance]
        detail = f" (missing: {', '.join(map(str, missing))})"
    return f"{error.json_path} fails {error.validator}{detail}"


def parse_agent_json(text: str, schema: Mapping[str, object]) -> dict[str, object]:
    """The JSON object in an agent's reply (bare, or alone in one fenced code block), valid
    against ``schema``.

    Raises:
        InvalidAgentOutputError: too long, nested too deep, an over-long integer, not JSON,
            not an object, or not valid against the schema; the message says what is wrong.
    """
    if len(text) > MAX_REPLY_CHARS:
        raise InvalidAgentOutputError(f"the reply is over {MAX_REPLY_CHARS} characters")
    body = text.strip()
    if fenced := _FENCED_REPLY.match(body):
        body = fenced.group(1)
    if _nesting_depth(body) > MAX_REPLY_DEPTH:
        raise InvalidAgentOutputError(f"the reply nests deeper than {MAX_REPLY_DEPTH} levels")
    try:
        data = json.loads(body, parse_int=_bounded_int, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise InvalidAgentOutputError(
            f"the reply is not valid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise InvalidAgentOutputError("the reply is not a JSON object")
    errors = sorted(_Validator(schema).iter_errors(data), key=lambda e: e.json_path)
    if errors:
        # One ``required`` error per missing property: each names them all, so say it once.
        distinct = list(dict.fromkeys(_problem(error) for error in errors))
        problems = "; ".join(distinct[:_ERRORS_SHOWN])
        more = (
            f" (and {len(distinct) - _ERRORS_SHOWN} more)" if len(distinct) > _ERRORS_SHOWN else ""
        )
        raise InvalidAgentOutputError(f"the reply does not match the schema: {problems}{more}")
    return data
