#!/usr/bin/env python3
"""Shared strict-JSON and canonical-fingerprint helpers for the GKSA-23 tools.

Standard library only. This module is the single validated boundary where the
untyped ``json`` decoder output is narrowed to the explicit JSON data model, and
the single implementation of the canonical document fingerprint used by the
source guard, the E2E verifier and the manifest loader.

JSON typing: every parsed JSON tree is represented as :data:`JsonValue` (a
recursive union of the JSON data model), never as ``Any``. Typed code narrows
objects and arrays with :func:`as_object` / :func:`as_array` and the ``*_field``
helpers. Non-finite numbers (``NaN``, ``Infinity`` and overflow such as
``1e999``) and duplicate object keys are rejected here, so downstream code can
never be surprised by them.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, NoReturn, TypeGuard

# Explicit JSON data model. ``JsonValue`` is the recursive union of every value
# a strict JSON document can hold; ``JsonObject``/``JsonArray`` are the object
# and array members of that union.
type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]


class JsonInputError(ValueError):
    """Strict JSON parsing or narrowing rejected the input."""


# --------------------------------------------------------------------------
# strict JSON decoding (the single validated narrowing boundary)
# --------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise JsonInputError(f"duplicate JSON object key {key!r}")
        obj[key] = value
    return obj


def _reject_nonfinite(token: str) -> NoReturn:
    raise JsonInputError(f"non-finite JSON number literal {token!r}")


def _narrow_json_value(value: object, source: str) -> JsonValue:
    """Validate decoded JSON and narrow it to :data:`JsonValue`.

    ``json.loads`` is typed as returning ``Any``; this is the documented narrow
    safe conversion that checks the untyped decoder output against the JSON data
    model before any typed code consumes it. Non-finite floats (including
    overflow such as ``1e999``) and non-string object keys are rejected here.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonInputError(f"non-finite JSON number in {source}")
        return value
    if isinstance(value, list):
        return [_narrow_json_value(item, source) for item in value]
    if isinstance(value, dict):
        narrowed: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JsonInputError(f"non-string JSON object key in {source}")
            narrowed[key] = _narrow_json_value(item, source)
        return narrowed
    raise JsonInputError(f"unsupported JSON value {type(value).__name__} in {source}")


def parse_json_strict(text: str, source: str = "<memory>") -> JsonValue:
    """Parse JSON, rejecting duplicate keys, non-finite numbers and overflow."""
    try:
        raw: object = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except JsonInputError:
        raise
    except json.JSONDecodeError as exc:
        raise JsonInputError(f"invalid JSON in {source}: {exc}") from exc
    except (ValueError, OverflowError) as exc:
        raise JsonInputError(f"invalid JSON number in {source}: {exc}") from exc
    return _narrow_json_value(raw, source)


def load_json_strict(path: Path) -> JsonValue:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JsonInputError(f"file not found: {path}") from exc
    except OSError as exc:
        raise JsonInputError(f"cannot read {path}: {exc}") from exc
    return parse_json_strict(text, str(path))


# --------------------------------------------------------------------------
# narrow runtime helpers for validated JSON
# --------------------------------------------------------------------------


def is_int(value: object) -> TypeGuard[int]:
    """Strict JSON integer: bool is not an integer, float is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """Finite JSON number in the bounded numeric guard's sense.

    Bools and non-numbers are rejected, and every number that cannot be
    represented as a finite ``float`` is rejected too: ``NaN``/``Infinity`` and
    an integer too large to convert (``math.isfinite(10**400)`` raises
    ``OverflowError``). Such a value is a *non-finite* measurement, never an
    uncaught interpreter error, so a bounded numeric comparison can neither be
    satisfied by a huge integer magnitude nor crash on one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError, TypeError):
        return False


def json_type_kind(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return "object"


def as_object(value: JsonValue, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise JsonInputError(f"{label} must be a JSON object, got {json_type_kind(value)}")
    return value


def as_array(value: JsonValue, label: str) -> JsonArray:
    if not isinstance(value, list):
        raise JsonInputError(f"{label} must be a JSON array, got {json_type_kind(value)}")
    return value


def as_string(value: JsonValue, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise JsonInputError(f"{label} must be a JSON string")
    if not allow_empty and not value.strip():
        raise JsonInputError(f"{label} must be a non-empty string")
    return value


def as_int(value: JsonValue, label: str) -> int:
    if not is_int(value):
        raise JsonInputError(f"{label} must be an integer")
    return value


def field_of(obj: JsonObject, key: str, label: str) -> JsonValue:
    if key not in obj:
        raise JsonInputError(f"{label} is missing key {key!r}")
    return obj[key]


def object_field(obj: JsonObject, key: str, label: str) -> JsonObject:
    return as_object(field_of(obj, key, label), f"{label}.{key}")


def array_field(obj: JsonObject, key: str, label: str) -> JsonArray:
    return as_array(field_of(obj, key, label), f"{label}.{key}")


def string_field(obj: JsonObject, key: str, label: str, *, allow_empty: bool = True) -> str:
    return as_string(field_of(obj, key, label), f"{label}.{key}", allow_empty=allow_empty)


def json_string_array(items: Iterable[str]) -> JsonArray:
    """Copy a string iterable into a concrete :data:`JsonArray`."""
    array: JsonArray = []
    for item in items:
        array.append(item)
    return array


# --------------------------------------------------------------------------
# canonical fingerprint and file hashing
# --------------------------------------------------------------------------


def canonical_bytes(obj: JsonValue) -> bytes:
    """Canonical JSON bytes for hashing; rejects non-finite numbers."""
    text = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_digest(obj: JsonValue) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
