#!/usr/bin/env python3
"""Shared black-box support for the GKSA-23 protocol and migration matrices.

Standard library only. These helpers exist to *test* the v2 manifest protocol
through its command-line interface. They never import a production module:
they build disposable fixture trees, compute the canonical manifest fingerprint
independently of any loader, and launch the production CLI through subprocess.

The fingerprints and digests computed here are independent oracles. They are
derived from the documented canonical form:

    SHA256(json.dumps(
        {"index": parsed_index,
         "files": {relative_posix_path: parsed_json}},
        ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8"))

JSON typing: every parsed JSON tree is represented as :data:`JsonValue` (a
recursive union of the JSON data model), never as ``Any``. The single place
where the untyped ``json`` decoder output is trusted is the validated narrowing
conversion in :func:`_narrow_json_value`; typed code then narrows objects and
arrays with :func:`as_object` / :func:`as_array` and the ``*_field`` helpers.

Every fixture described as synthetic is tool-only input: no game, plugin or
native code is executed or modelled by these tools.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NoReturn, TypeGuard

BASE_COMMIT = "5473abf5fcd9b340169c0ddd3230d3718fe8ce84"

INDEX_KIND = "gksr-e2e-manifest"
SCENARIOS_KIND = "gksr-e2e-scenarios"
CAPTURE_KIND = "gksr-e2e-capture"
MANIFEST_VERSION = 2
SCHEMA_VERSION = 2
CAPTURE_VERSION = 2
CAPTURE_PURPOSES = ("game-observation", "tool-fixture", "example")
ALLOW_SYNTHETIC_FLAG = "--allow-synthetic"

# Exit-code contract preserved from the v1 verifier:
#   0 success, 1 business/evidence assertion failure, 2 invalid shape/input.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INPUT = 2

PROFILE_IDS = (
    "countdown",
    "sermon-state",
    "church-beacon",
    "sermon-popup",
    "local-fault-notices",
    "combined-recovery",
)

TICKETS = ("GKSA-10", "GKSA-11", "GKSA-12", "GKSA-13", "GKSA-14", "GKSA-15")
TICKET_SCENARIO_COUNTS = {
    "GKSA-10": 24,
    "GKSA-11": 32,
    "GKSA-12": 47,
    "GKSA-13": 69,
    "GKSA-14": 44,
    "GKSA-15": 108,
}
TICKET_PROFILE = {
    "GKSA-10": "countdown",
    "GKSA-11": "sermon-state",
    "GKSA-12": "church-beacon",
    "GKSA-13": "sermon-popup",
    "GKSA-14": "local-fault-notices",
    "GKSA-15": "combined-recovery",
}
TOTAL_SCENARIOS = 324

# The canonical identity keys the v2 migration adds to each legacy scenario.
# They are excluded from the preserved-content digest.
SCENARIO_IDENTITY_KEYS = ("id", "profileId", "requirements")
# The legacy top-level keys that must not survive in a v2 shared context.
CONTEXT_REMOVED_KEYS = ("kind", "manifestVersion", "ticket", "scenarios")

# Explicit JSON data model. ``JsonValue`` is the recursive union of every value
# a strict JSON document can hold; ``JsonObject``/``JsonArray`` are the object
# and array members of that union. None of these is ``Any``.
type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]


class JsonLoadError(ValueError):
    """Strict JSON parsing rejected the input."""


# --------------------------------------------------------------------------
# strict JSON decoding (the single validated narrowing boundary)
# --------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise JsonLoadError(f"duplicate JSON object key {key!r}")
        obj[key] = value
    return obj


def _reject_nonfinite(token: str) -> NoReturn:
    raise JsonLoadError(f"non-finite JSON number literal {token!r}")


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
            raise JsonLoadError(f"non-finite JSON number in {source}")
        return value
    if isinstance(value, list):
        return [_narrow_json_value(item, source) for item in value]
    if isinstance(value, dict):
        narrowed: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JsonLoadError(f"non-string JSON object key in {source}")
            narrowed[key] = _narrow_json_value(item, source)
        return narrowed
    raise JsonLoadError(f"unsupported JSON value {type(value).__name__} in {source}")


def parse_json_strict(text: str, source: str = "<memory>") -> JsonValue:
    """Parse JSON, rejecting duplicate keys, non-finite numbers and overflow."""
    try:
        raw: object = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except JsonLoadError:
        raise
    except json.JSONDecodeError as exc:
        raise JsonLoadError(f"invalid JSON in {source}: {exc}") from exc
    except (ValueError, OverflowError) as exc:
        raise JsonLoadError(f"invalid JSON number in {source}: {exc}") from exc
    return _narrow_json_value(raw, source)


def load_json_strict(path: Path) -> JsonValue:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JsonLoadError(f"file not found: {path}") from exc
    except OSError as exc:
        raise JsonLoadError(f"cannot read {path}: {exc}") from exc
    return parse_json_strict(text, str(path))


# --------------------------------------------------------------------------
# narrow runtime helpers for validated JSON
# --------------------------------------------------------------------------


def is_int(value: object) -> TypeGuard[int]:
    """Strict JSON integer: bool is not an integer, float is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """Finite JSON number: bools excluded, float non-finite values rejected."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
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
        raise JsonLoadError(f"{label} must be a JSON object, got {json_type_kind(value)}")
    return value


def as_array(value: JsonValue, label: str) -> JsonArray:
    if not isinstance(value, list):
        raise JsonLoadError(f"{label} must be a JSON array, got {json_type_kind(value)}")
    return value


def as_string(value: JsonValue, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise JsonLoadError(f"{label} must be a JSON string")
    if not allow_empty and not value.strip():
        raise JsonLoadError(f"{label} must be a non-empty string")
    return value


def as_int(value: JsonValue, label: str) -> int:
    if not is_int(value):
        raise JsonLoadError(f"{label} must be an integer")
    return value


def as_number(value: JsonValue, label: str) -> int | float:
    if not is_finite_number(value):
        raise JsonLoadError(f"{label} must be a finite number")
    return value


def as_float(value: JsonValue, label: str) -> float:
    return float(as_number(value, label))


def field_of(obj: JsonObject, key: str, label: str) -> JsonValue:
    if key not in obj:
        raise JsonLoadError(f"{label} is missing key {key!r}")
    return obj[key]


def object_field(obj: JsonObject, key: str, label: str) -> JsonObject:
    return as_object(field_of(obj, key, label), f"{label}.{key}")


def array_field(obj: JsonObject, key: str, label: str) -> JsonArray:
    return as_array(field_of(obj, key, label), f"{label}.{key}")


def json_array(items: Iterable[JsonValue]) -> JsonArray:
    """Copy any JSON-value iterable into a concrete :data:`JsonArray`."""
    array: JsonArray = []
    for item in items:
        array.append(item)
    return array


def string_field(obj: JsonObject, key: str, label: str, *, allow_empty: bool = True) -> str:
    return as_string(field_of(obj, key, label), f"{label}.{key}", allow_empty=allow_empty)


# --------------------------------------------------------------------------
# canonical fingerprint oracle
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


def manifest_fingerprint(index: JsonValue, files: dict[str, JsonValue]) -> str:
    """The documented full-fingerprint oracle; preserves array order."""
    payload: JsonObject = {"index": index, "files": dict(files)}
    return canonical_digest(payload)


def relative_posix(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _iter_json_under(root: Path) -> list[Path]:
    """Recursively list regular .json files under root, path-escape safe."""
    found: list[Path] = []
    if not root.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.suffix == ".json" and candidate.is_file() and not candidate.is_symlink():
                found.append(candidate)
    return found


def collect_referenced_files(index: JsonObject, base: Path) -> dict[str, JsonValue]:
    """Load profilesFile plus every recursive scenario JSON under scenarioRoot.

    Returns a map of index-relative POSIX path to parsed JSON. Symlinked files
    are skipped here; an enforcing loader must reject them (the CLI cases cover
    that). This function is deliberately independent of the production loader.
    """
    files: dict[str, JsonValue] = {}
    profiles_rel = index.get("profilesFile")
    if isinstance(profiles_rel, str) and profiles_rel:
        files[profiles_rel] = load_json_strict(base / profiles_rel)
    scenario_root_rel = index.get("scenarioRoot")
    if isinstance(scenario_root_rel, str) and scenario_root_rel:
        scenario_root = base / scenario_root_rel
        for found in _iter_json_under(scenario_root):
            rel = relative_posix(found, base)
            files[rel] = load_json_strict(found)
    return files


def fingerprint_of_index(index_path: Path) -> str:
    index = as_object(load_json_strict(index_path), str(index_path))
    files = collect_referenced_files(index, index_path.parent)
    return manifest_fingerprint(index, files)


# --------------------------------------------------------------------------
# disposable fixtures and CLI execution
# --------------------------------------------------------------------------


class Fixture:
    """A disposable fixture directory with strict-write helpers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write_json(self, rel: str, obj: JsonValue, *, pretty: bool = False) -> Path:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if pretty:
            text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
        else:
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        target.write_text(text + "\n", encoding="utf-8")
        return target

    def write_text(self, rel: str, text: str) -> Path:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def symlink(self, rel: str, target_rel: str) -> Path:
        link = self.path(rel)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(target_rel, link)
        return link

    def load_json(self, rel: str) -> JsonValue:
        return load_json_strict(self.path(rel))

    def fingerprint(self, index_rel: str = "manifest.json") -> str:
        return fingerprint_of_index(self.path(index_rel))


@dataclass
class CliRun:
    """Result of one production-CLI subprocess invocation."""

    argv: list[str]
    exit: int
    stdout: str
    stderr: str
    report: JsonValue | None
    report_error: str | None

    def command(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


def run_cli(
    cli_path: Path,
    args: list[str],
    cwd: Path,
    output_path: Path,
) -> CliRun:
    argv = [sys.executable, str(cli_path), *args]
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    report: JsonValue | None = None
    report_error: str | None = None
    if output_path.exists():
        try:
            report = load_json_strict(output_path)
        except JsonLoadError as exc:
            report_error = str(exc)
    return CliRun(
        argv=argv,
        exit=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        report=report,
        report_error=report_error,
    )


def write_report(path: Path, report: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def scenario_ids_of(files: dict[str, JsonValue]) -> list[str]:
    ids: list[str] = []
    for rel in sorted(files):
        parsed = files[rel]
        if not isinstance(parsed, dict):
            continue
        scenarios = parsed.get("scenarios")
        if not isinstance(scenarios, list):
            continue
        for scenario in scenarios:
            if isinstance(scenario, dict):
                scenario_id = scenario.get("id")
                if isinstance(scenario_id, str):
                    ids.append(scenario_id)
    return ids


# --------------------------------------------------------------------------
# case reporting
# --------------------------------------------------------------------------


@dataclass
class CaseSetup:
    """Per-case facts computed by the fixture builder."""

    expected_fingerprint: str | None = None
    expected_manifest_id: str | None = None
    expected_scenario_ids: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    capture_rel: str = "capture.json"


@dataclass
class CaseResult:
    case_id: str
    group: str
    description: str
    mode: str
    expected_exit: int
    actual_exit: int
    command: str
    passed: bool
    red: bool
    problems: list[str] = field(default_factory=list)
    evidence: JsonObject = field(default_factory=dict)


def summarize_cases(results: Iterable[CaseResult], *, expected_red: bool) -> JsonObject:
    items = list(results)
    passed = [item for item in items if item.passed]
    all_pass = len(passed) == len(items)
    return {
        "total": len(items),
        "passed": len(passed),
        "failed": len(items) - len(passed),
        "red": not all_pass if expected_red else False,
        "ok": all_pass,
    }
