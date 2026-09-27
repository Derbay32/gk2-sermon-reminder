#!/usr/bin/env python3
"""GKSA-23 v2 E2E evidence validator.

Validates a machine-readable capture of a real in-game observation run against
the aggregated v2 manifest index. It is an evidence checker, not a game
simulator: it never invents observations, never grades an unexecuted scenario as
passed, and never treats logs alone as proof of a visual/layout outcome.

The manifest bundle (index + profiles + every scenario document) is loaded and
validated by the shared :mod:`e2e_manifest` loader, so its schema, containment,
identity and fingerprint rules never diverge from the source guard's. Each
scenario is evaluated against its own profile's ``validation`` and
``languageMapping``; policies are never merged into a weaker combined context.

Fail-closed: any missing file, hash mismatch, unsupported protocol, unexecuted
scenario, wrong expected value, or absent required evidence file fails the run.

Usage:
  python3 tests/e2e/verify.py --manifest tests/e2e/manifest.json \
      --check-manifest --output PATH
  python3 tests/e2e/verify.py --manifest tests/e2e/manifest.json \
      --capture PATH --output PATH [--allow-synthetic]

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TypeGuard

# Direct-script execution (`python3 tests/e2e/verify.py`) only puts this file's
# directory on sys.path, so the shared tools directory is added explicitly
# before the production modules are imported.
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import e2e_manifest  # noqa: E402
import json_data  # noqa: E402
from e2e_manifest import (  # noqa: E402
    RATIO_EXPECTED_DENOMINATOR,
    RATIO_EXPECTED_NUMERATOR,
    RATIO_TOLERANCE,
)
from json_data import JsonArray, JsonObject, JsonValue  # noqa: E402

CAPTURE_KIND = "gksr-e2e-capture"
CAPTURE_VERSION = 2
CAPTURE_PURPOSES = ("game-observation", "tool-fixture", "example")
MANIFEST_KIND_REPORT = "gksr-manifest-check-result"
VALIDATION_KIND_REPORT = "gksr-validation-result"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


class Fail(Exception):
    """Raised when validation cannot proceed or a top-level check fails."""


# --------------------------------------------------------------------------
# tiny path helpers (flat dotted keys, e.g. "game.sha256")
# --------------------------------------------------------------------------


def get_path(obj: JsonValue, dotted: str) -> JsonValue:
    """Resolve a dotted key. A literal flat key wins over nested traversal, so
    captures may record either nested objects or flat 'hud.visible' style keys."""
    if isinstance(obj, dict) and dotted in obj:
        return obj[dotted]
    node = obj
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def has_path(obj: JsonValue, dotted: str) -> bool:
    try:
        get_path(obj, dotted)
        return True
    except KeyError:
        return False


def is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value: object) -> TypeGuard[int]:
    """Strict JSON integer: bool is not an integer, float is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_int(value: JsonValue) -> bool:
    return is_int(value) and value > 0


def _finite_number(value: object) -> TypeGuard[int | float]:
    """True for a finite JSON int/float. Bools, strings, NaN, infinity and
    integers too large to convert to a float are rejected, so a numeric check
    can never be satisfied by a non-number and can never crash on a huge
    integer magnitude."""
    return json_data.is_finite_number(value)


def json_type_kind(value: JsonValue) -> str:
    """Strict JSON type identity so 1, 1.0, true and 'true' never alias."""
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
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# --------------------------------------------------------------------------
# type checks
# --------------------------------------------------------------------------


def check_type(value: JsonValue, kind: str) -> None:
    if kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise Fail(f"expected non-empty string, got {value!r}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise Fail(f"expected boolean, got {value!r}")
    elif kind == "positive-int":
        if not is_positive_int(value):
            raise Fail(f"expected positive integer, got {value!r}")
    elif kind == "positive-number":
        if not is_number(value) or value <= 0:
            raise Fail(f"expected positive number, got {value!r}")
    elif kind == "weekday-int":
        # A day-of-week / sermon-weekday from the game's six-day cycle. The
        # runtime definition is ConstDef.Get("day_wrath").IntValue, a single
        # value that every scenario must reuse instead of a per-test literal.
        if not is_int(value) or not (1 <= value <= 6):
            raise Fail(f"expected an integer weekday in 1..6 (EnvironmentData six-day cycle), got {value!r}")
    elif kind == "sha256":
        if not isinstance(value, str) or not SHA256_RE.match(value):
            raise Fail(f"expected lowercase 64-hex sha256, got {value!r}")
    elif kind == "git-commit":
        if not isinstance(value, str) or not GIT_COMMIT_RE.match(value):
            raise Fail(f"expected 40-hex git commit, got {value!r}")
    elif kind == "timestamp":
        if not isinstance(value, str) or not ISO_RE.match(value):
            raise Fail(f"expected ISO-8601 timestamp, got {value!r}")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError as exc:
            raise Fail(f"unparseable timestamp {value!r}: {exc}") from exc
    elif kind == "nonempty-string-array":
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
            raise Fail(f"expected non-empty array of strings, got {value!r}")
    elif kind == "object":
        if not isinstance(value, dict):
            raise Fail(f"expected object, got {value!r}")
    else:
        raise Fail(f"unknown expected type {kind!r}")


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return json_data.sha256_file(path)


# --------------------------------------------------------------------------
# manifest validation
# --------------------------------------------------------------------------


def load_json(path: Path) -> JsonValue:
    """Strict JSON load: duplicate keys and non-finite numbers are rejected."""
    try:
        return json_data.load_json_strict(path)
    except json_data.JsonInputError as exc:
        raise Fail(str(exc)) from exc


# --------------------------------------------------------------------------
# capture validation
# --------------------------------------------------------------------------


def check_language_observations(observations: JsonObject, bundle: e2e_manifest.ManifestBundle) -> None:
    """Reject a capture whose raw game language observation uses a catalog-only
    label (e.g. 'zh-CN' instead of 'zh_cn'), evaluated per scenario profile."""
    for scenario in bundle.scenarios:
        sid = scenario["id"]
        if not isinstance(sid, str):
            continue
        scenario_observations = observations.get(sid)
        if not isinstance(scenario_observations, dict):
            continue
        value = scenario_observations.get("language.active")
        if not isinstance(value, str):
            continue
        profile = bundle.profile_of(scenario)
        label_map = e2e_manifest.profile_label_map(profile, f"profile {scenario.get('profileId')!r}")
        raw_ids: set[str] = {value for value in label_map.values() if isinstance(value, str)}
        catalog_only = {label for label in label_map if label not in raw_ids}
        if value in catalog_only:
            raise Fail(
                f"scenario {sid} recorded language.active = {value!r}, a catalog-only "
                f"label; raw game language ids are required (known: {sorted(raw_ids)})"
            )


def validate_capture_shape(
    capture: JsonValue, bundle: e2e_manifest.ManifestBundle
) -> tuple[str, JsonObject, JsonObject, JsonArray]:
    """Validate the capture protocol envelope and return its validated parts."""
    if not isinstance(capture, dict):
        raise Fail(f"capture root must be a JSON object, got {type(capture).__name__}")
    if capture.get("kind") != CAPTURE_KIND:
        raise Fail(f"unsupported capture kind: {capture.get('kind')!r} (expected {CAPTURE_KIND!r})")
    version = capture.get("captureVersion")
    # Strict integer identity: True == 1 and 1.0 == 1 in Python, so a bare
    # membership test would accept boolean or float aliases of a valid version.
    if not is_int(version) or version != CAPTURE_VERSION:
        raise Fail(
            f"unsupported captureVersion {version!r} ({json_type_kind(version)}); "
            f"supported integer versions: [{CAPTURE_VERSION}]"
        )
    if capture.get("manifestId") != bundle.manifest_id:
        raise Fail(f"capture manifestId {capture.get('manifestId')!r} does not match manifest {bundle.manifest_id!r}")
    if capture.get("manifestSha256") != bundle.sha256:
        raise Fail(
            f"capture manifestSha256 {capture.get('manifestSha256')!r} does not match the "
            f"manifest fingerprint {bundle.sha256}"
        )
    purpose = capture.get("capturePurpose")
    if not isinstance(purpose, str) or purpose not in CAPTURE_PURPOSES:
        raise Fail(f"capturePurpose must be one of {list(CAPTURE_PURPOSES)}; got {purpose!r}")
    if purpose == "example":
        raise Fail("capturePurpose 'example' is never accepted by the verifier")
    environment = capture.get("environment")
    if not isinstance(environment, dict):
        raise Fail("capture.environment must be an object")
    observations = capture.get("observations")
    if not isinstance(observations, dict) or not observations:
        raise Fail("capture.observations must be a non-empty object keyed by scenario id")
    for sid, observation in observations.items():
        if not sid.strip():
            raise Fail(f"capture.observations has an empty scenario key {sid!r}")
        if not isinstance(observation, dict) or not observation:
            raise Fail(f"capture.observations[{sid!r}] must be a non-empty object")
    results = capture.get("results")
    if not isinstance(results, list):
        raise Fail("capture.results must be an array of executed scenario records")
    seen_ids = set()
    for record in results:
        if not isinstance(record, dict):
            raise Fail(f"each capture result must be an object, got {record!r}")
        scenario_id = record.get("scenarioId")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise Fail("each capture result must carry a non-empty scenarioId")
        if scenario_id in seen_ids:
            raise Fail(f"duplicate capture result scenarioId {scenario_id!r}; results must not overwrite each other")
        seen_ids.add(scenario_id)
        status = record.get("status")
        if not isinstance(status, str) or not status.strip():
            raise Fail(f"capture result {scenario_id!r} must carry a status string")
        evidence = record.get("evidence", {})
        if not isinstance(evidence, dict):
            raise Fail(
                f"capture result {scenario_id!r} evidence must be an object "
                "mapping the evidence category (screenshot/log/video) to a path"
            )
        for category, paths in evidence.items():
            if not category.strip():
                raise Fail(f"capture result {scenario_id!r} has a malformed evidence category")
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not paths:
                raise Fail(
                    f"capture result {scenario_id!r} category {category!r} must list at "
                    "least one evidence path; empty lists are not evidence"
                )
            if not all(isinstance(p, str) and p.strip() for p in paths):
                raise Fail(f"capture result {scenario_id!r} category {category!r} must map to non-empty path strings")
        measurement = record.get("measurement", False)
        if measurement is not False and not isinstance(measurement, dict):
            raise Fail(f"capture result {scenario_id!r} measurement must be false or an object")
    return purpose, environment, observations, results


def validate_environments(env: JsonObject, bundle: e2e_manifest.ManifestBundle) -> JsonObject:
    """Validate the shared capture environment against every used profile.

    Each scenario keeps its own profile policy, so the environment is checked
    against every distinct profile referenced by the manifest scenarios. The
    policies are never merged into one weaker combined context.
    """
    used: list[str] = []
    for scenario in bundle.scenarios:
        profile_id = scenario["profileId"]
        if isinstance(profile_id, str) and profile_id not in used:
            used.append(profile_id)
    failures: list[str] = []
    evidence_files: JsonObject | None = None
    for profile_id in used:
        profile = json_data.as_object(bundle.profiles[profile_id], f"profile {profile_id!r}")
        try:
            evidence_files = validate_environment(env, profile)
        except Fail as exc:
            failures.append(str(exc))
    if failures:
        raise Fail("; ".join(failures))
    if evidence_files is None:
        raise Fail("no profile policy applied to the capture environment")
    return evidence_files


def validate_environment(env: JsonObject, profile: JsonObject) -> JsonObject:
    validation = profile.get("validation", {})
    if not isinstance(validation, dict):
        raise Fail("profile.validation must be an object")
    required = validation.get("requiredEnvironmentPaths", [])
    if not isinstance(required, list):
        raise Fail("profile.validation.requiredEnvironmentPaths must be an array")
    for spec in required:
        if not isinstance(spec, dict):
            raise Fail(f"malformed requiredEnvironmentPaths entry: {spec!r}")
        path_value = spec.get("path")
        type_value = spec.get("type")
        if not isinstance(path_value, str) or not isinstance(type_value, str):
            raise Fail(f"malformed requiredEnvironmentPaths entry: {spec!r}")
        if not has_path(env, path_value):
            raise Fail(f"environment is missing required field {path_value!r}")
        value = get_path(env, path_value)
        check_type(value, type_value)
        if "equals" in spec and (json_type_kind(value) != json_type_kind(spec["equals"]) or value != spec["equals"]):
            raise Fail(
                f"environment field {path_value!r} must equal {spec['equals']!r} "
                f"({json_type_kind(spec['equals'])}), got {value!r} ({json_type_kind(value)})"
            )

    groups = validation.get("buildPluginHashConsistency", [])
    if not isinstance(groups, list):
        raise Fail("profile.validation.buildPluginHashConsistency must be an array")
    for group in groups:
        if not isinstance(group, list) or not group:
            raise Fail(f"malformed buildPluginHashConsistency group: {group!r}")
        values: dict[str, JsonValue] = {}
        for path in group:
            if not isinstance(path, str):
                raise Fail(f"malformed buildPluginHashConsistency path: {path!r}")
            if not has_path(env, path):
                raise Fail(f"environment is missing consistency field {path!r}")
            values[path] = get_path(env, path)
            check_type(values[path], "sha256")
        if len(set(values.values())) != 1:
            raise Fail(f"environment hash inconsistency across {group}: {values}")

    evidence_files = env.get("evidenceFiles")
    if not isinstance(evidence_files, dict) or not evidence_files:
        raise Fail("environment.evidenceFiles must be a non-empty object of sha256 hashes")
    for name, digest in evidence_files.items():
        if not isinstance(name, str) or not name.strip():
            raise Fail(f"evidenceFiles has a non-string file key {name!r}")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise Fail(f"evidenceFiles[{name!r}] must be a lowercase 64-hex sha256")
    return evidence_files


def validate_evidence_files(env: JsonObject, evidence_files: JsonObject, capture_dir: Path) -> set[str]:
    """Every referenced evidence file must exist, stay inside the capture dir,
    and hash to the recorded digest. Returns the set of verified relative paths."""
    verified: set[str] = set()
    root = capture_dir.resolve()
    for name, digest in evidence_files.items():
        candidate = Path(name)
        if candidate.is_absolute():
            raise Fail(f"evidence file path {name!r} must be relative to the capture file")
        resolved = (capture_dir / candidate).resolve()
        if root != resolved and root not in resolved.parents:
            raise Fail(f"evidence file path {name!r} escapes the capture directory")
        if not resolved.is_file():
            raise Fail(f"evidence file missing: {name!r} (looked at {resolved})")
        actual = sha256_file(resolved)
        if actual != digest:
            raise Fail(f"evidence file hash mismatch for {name!r}: recorded {digest}, actual {actual}")
        verified.add(name)
    return verified


# --------------------------------------------------------------------------
# expectation evaluation
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# dedicated date-check helper
# --------------------------------------------------------------------------
#
# Calendar scenarios must never invent a sermon weekday literal. day_wrath is a
# single runtime definition (ConstDef.Get("day_wrath").IntValue) captured once
# as environment.game.sermonWeekday and compared with save.sermonWeekday. Every
# other date quantity is expressed relative to that observed target T, so no
# scenario hardcodes T (T may legitimately be any of 1..6, including 1).
#
# Ground truth (decompiled EnvironmentData.cs, build 25509347):
#   EnvironmentData.Day            -> the ABSOLUTE day, private field day = 1
#   EnvironmentData.CurrentDayNumber -> GetDayNumberFromDay(day) = day % 6, with
#                                     remainder 0 mapped to 6, i.e. WEEKDAY ONLY
#   DAYS_IN_WEEK = 6
#   dayOfWeek == ((absoluteDay - 1) % week) + 1   (equivalent to day % 6, 0 -> 6)
#
# This is one dedicated, bounded helper with a fixed set of modes, deliberately
# not a general expression DSL.


def _manifest_string(spec: JsonObject, key: str, default: str) -> str:
    """Read a manifest string field the shared loader already validated.

    The ``isinstance`` guard is a typing narrow only: an invalid manifest never
    reaches the evidence evaluator because ``load_manifest`` rejected it.
    """
    value = spec.get(key, default)
    return value if isinstance(value, str) and value else default


def weekday_from_absolute_day(absolute_day: int, week: int) -> int:
    """EnvironmentData.CurrentDayNumber == ((absoluteDay - 1) % week) + 1."""
    return ((absolute_day - 1) % week) + 1


def _date_read_int(observations: JsonObject, path: str, label: str, failures: list[str]) -> int | None:
    if not has_path(observations, path):
        failures.append(f"{label} observation {path!r} was not captured")
        return None
    value = get_path(observations, path)
    if not is_int(value):
        failures.append(f"{label} {path} = {value!r} must be a strict integer (floats and booleans do not qualify)")
        return None
    return value


def _date_check_absolute_day(absolute: int | None, path: str, failures: list[str]) -> None:
    if absolute is None:
        return
    if absolute <= 0:
        failures.append(
            f"{path} = {absolute!r} must be a strict positive absolute game day; "
            "EnvironmentData.Day starts at 1 (the 1..6 range is CurrentDayNumber, "
            "which is only the weekday)"
        )


def _date_check_weekday(path: str, value: int | None, week: int, failures: list[str]) -> None:
    if value is None:
        return
    if not (1 <= value <= week):
        failures.append(f"{path} = {value!r} must be a weekday within 1..{week} (EnvironmentData.CurrentDayNumber)")


def _date_check_weekday_matches_absolute(
    absolute: int | None,
    absolute_path: str,
    weekday: int | None,
    weekday_path: str,
    week: int,
    failures: list[str],
) -> None:
    if absolute is None or weekday is None or absolute <= 0:
        return
    expected = weekday_from_absolute_day(absolute, week)
    if weekday != expected:
        failures.append(
            f"{weekday_path} = {weekday!r} does not match {absolute_path} = {absolute!r}: "
            f"(({absolute} - 1) % {week}) + 1 = {expected}"
        )


def date_delta(target: int, current: int, week: int) -> int:
    """Countdown days to the sermon weekday, wrapping inside the six-day cycle."""
    return (target - current + week) % week


def _date_check_countdown_equals(
    observations: JsonObject, path: str, label: str, expected: int, failures: list[str]
) -> None:
    shown = _date_read_int(observations, path, label, failures)
    if shown is not None and shown != expected:
        failures.append(
            f"{path} = {shown!r} != the mathematical delta {expected} computed from the observed sermon weekday"
        )


def check_localized_output(
    observations: JsonObject, text_path: str, key: str, language: str, days: int | None = None
) -> list[str]:
    """Check lookup provenance and interpolation, without grading translation copy.

    Lookup observations must come from the same real run as the rendered text.
    They are captured by the operator/debugger, not inferred from expected text.
    Visual scenarios still require their existing screenshot evidence.
    """
    failures: list[str] = []
    lookup = {
        "key": key,
        "language": language,
        "resource": f"gk2.sermonreminder.localization.{language}.json",
    }
    for field, expected in lookup.items():
        path = f"i18n.{text_path}.{field}"
        if not has_path(observations, path):
            failures.append(f"lookup observation {path!r} was not captured")
        elif get_path(observations, path) != expected:
            failures.append(f"{path} = {get_path(observations, path)!r}, expected {expected!r}")
    if not has_path(observations, text_path):
        return failures + [f"text observation {text_path!r} was not captured"]
    text = get_path(observations, text_path)
    if not isinstance(text, str) or not text.strip():
        return failures + [f"{text_path} must be a non-empty rendered string"]
    if "gksr." in text or re.search(r"\{[^{}]*\}", text):
        failures.append(f"{text_path} contains a resource key or unresolved placeholder")
    if days is not None and days > 1:
        if not re.search(rf"(?<![\d.]){days}(?!\d|[.,]\d)", text):
            failures.append(f"{text_path} does not contain the interpolated integer {days}")
    return failures


def _date_check_text(observations: JsonObject, spec: JsonObject, delta: int, failures: list[str]) -> None:
    """Check the countdown key and integer interpolation, not its sentence."""
    text_path = _manifest_string(spec, "text", "hud.text")
    key_path = _manifest_string(spec, "textKey", "hud.textKey")
    language = _manifest_string(spec, "language", "zh_cn")
    expected_key = "gksr.hud.sermonCountdown.one" if delta == 1 else "gksr.hud.sermonCountdown.other"
    if has_path(observations, key_path) and get_path(observations, key_path) != expected_key:
        failures.append(f"{key_path} must be {expected_key!r} for a shown delta of {delta}")
    failures.extend(check_localized_output(observations, text_path, expected_key, language, delta))


def evaluate_date_check(condition: JsonObject, observations: JsonObject) -> list[str]:
    """Evaluate one dedicated dateCheck helper condition.

    Returns a list of failure messages (empty when the check passes). Every
    quantity is derived from the observed target T and the observed six-day week,
    never from a literal baked into the manifest. The manifest has already been
    validated by the shared loader, so the object/string narrows below only
    satisfy static typing and never hide a checked field.
    """
    spec_value = condition["dateCheck"]
    if not isinstance(spec_value, dict) or not spec_value:
        return ["dateCheck must be a non-empty object"]
    spec = spec_value
    mode_value = spec.get("mode")
    if not isinstance(mode_value, str):
        return [f"dateCheck.mode {mode_value!r} must be a string"]
    mode = mode_value
    failures: list[str] = []

    week_path = _manifest_string(spec, "week", "save.daysInWeek")
    week = _date_read_int(observations, week_path, "daysInWeek", failures)
    if week is not None and week < 2:
        failures.append(f"{week_path} = {week!r} must be at least 2")

    target_path = _manifest_string(spec, "target", "save.sermonWeekday")
    target = _date_read_int(observations, target_path, "sermonWeekday", failures)
    if target is not None and week is not None:
        _date_check_weekday(target_path, target, week, failures)

    # Fail closed before any calendar arithmetic: a missing, non-integer, or
    # out-of-range week/target (e.g. week = 0) is already recorded above and must
    # stop here, so no modulo by an invalid week can raise an uncaught error
    # instead of producing the failure artifact.
    if failures:
        return failures

    single = mode in ("single-day", "multi-day")
    sermon_day = mode == "sermon-day"

    if single or sermon_day:
        abs_path = _manifest_string(spec, "absoluteDay", "")
        dow_path = _manifest_string(spec, "dayOfWeek", "")
        absolute = _date_read_int(observations, abs_path, "absoluteDay", failures)
        _date_check_absolute_day(absolute, abs_path, failures)
        dow = _date_read_int(observations, dow_path, "dayOfWeek", failures)
        if week is not None:
            _date_check_weekday(dow_path, dow, week, failures)
            _date_check_weekday_matches_absolute(absolute, abs_path, dow, dow_path, week, failures)
        if week is None or target is None or absolute is None or dow is None:
            return failures
        if sermon_day:
            if dow != target:
                failures.append(
                    f"sermon-day requires {dow_path} = {dow!r} to equal the observed "
                    f"sermon weekday {target_path} = {target!r}"
                )
            flag_path = _manifest_string(spec, "nonSermonCountdownShown", "hud.nonSermonCountdownShown")
            if not has_path(observations, flag_path):
                failures.append(f"sermon-day requires {flag_path!r} to prove the non-sermon countdown is not shown")
            elif get_path(observations, flag_path) is not False:
                failures.append(f"{flag_path} = {get_path(observations, flag_path)!r} must be false on the sermon day")
            return failures

        delta = date_delta(target, dow, week)
        if mode == "single-day":
            if delta != 1:
                failures.append(
                    f"single-day requires (T - current + {week}) % {week} = 1; "
                    f"{target_path} = {target!r} with {dow_path} = {dow!r} gives {delta}"
                )
        elif not (2 <= delta <= week - 1):
            failures.append(
                f"multi-day requires (T - current + {week}) % {week} in 2..{week - 1}; "
                f"{target_path} = {target!r} with {dow_path} = {dow!r} gives {delta}"
            )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdown", ""), "countdownDays", delta, failures
        )
        if "text" in spec:
            _date_check_text(observations, spec, delta, failures)
        return failures

    # same-day / midnight / cycle-wrap all compare a before/after pair.
    before_abs_path = _manifest_string(spec, "absoluteDayBefore", "")
    after_abs_path = _manifest_string(spec, "absoluteDayAfter", "")
    before_dow_path = _manifest_string(spec, "dayOfWeekBefore", "")
    after_dow_path = _manifest_string(spec, "dayOfWeekAfter", "")
    before_abs = _date_read_int(observations, before_abs_path, "absoluteDayBefore", failures)
    after_abs = _date_read_int(observations, after_abs_path, "absoluteDayAfter", failures)
    _date_check_absolute_day(before_abs, before_abs_path, failures)
    _date_check_absolute_day(after_abs, after_abs_path, failures)
    before_dow = _date_read_int(observations, before_dow_path, "dayOfWeekBefore", failures)
    after_dow = _date_read_int(observations, after_dow_path, "dayOfWeekAfter", failures)
    if week is not None:
        _date_check_weekday(before_dow_path, before_dow, week, failures)
        _date_check_weekday(after_dow_path, after_dow, week, failures)
        _date_check_weekday_matches_absolute(before_abs, before_abs_path, before_dow, before_dow_path, week, failures)
        _date_check_weekday_matches_absolute(after_abs, after_abs_path, after_dow, after_dow_path, week, failures)
    if week is None or target is None:
        return failures
    if before_abs is None or after_abs is None or before_dow is None or after_dow is None:
        return failures

    before_delta = date_delta(target, before_dow, week)
    after_delta = date_delta(target, after_dow, week)

    if mode == "same-day":
        if before_abs != after_abs:
            failures.append(
                f"same-day requires the absolute day to be unchanged: {before_abs_path} = "
                f"{before_abs!r} vs {after_abs_path} = {after_abs!r}"
            )
        if before_dow != after_dow:
            failures.append(
                f"same-day requires the weekday to be unchanged: {before_dow_path} = "
                f"{before_dow!r} vs {after_dow_path} = {after_dow!r}"
            )
        if before_delta != after_delta:
            failures.append(
                f"same-day requires the delta to be unchanged: {before_delta} vs {after_delta} "
                f"from the same observed target {target_path} = {target!r}"
            )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdownBefore", ""), "countdown.beforeDays", before_delta, failures
        )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdownAfter", ""), "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, spec, after_delta, failures)
        return failures

    if after_abs != before_abs + 1:
        failures.append(
            f"{mode} requires the absolute day to advance by exactly one: "
            f"{after_abs_path} = {after_abs!r} != {before_abs_path} + 1 = {before_abs + 1}"
        )

    if mode == "after-sermon-day":
        # The day after the observed sermon day: before weekday == observed T,
        # after weekday is one absolute day later, and the delta returns to a
        # full cycle-1 (the next sermon). Works for a T==week wrap without any
        # invented target constant.
        if before_dow != target:
            failures.append(
                f"after-sermon-day requires {before_dow_path} = {before_dow!r} to equal "
                f"the observed sermon weekday {target_path} = {target!r}"
            )
        expected_after_delta = week - 1
        if after_delta != expected_after_delta:
            failures.append(
                f"after-sermon-day requires afterDelta = week - 1 = {expected_after_delta}: "
                f"{target_path} = {target!r} with {after_dow_path} = {after_dow!r} gives "
                f"{after_delta}"
            )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdownAfter", ""), "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, spec, after_delta, failures)
        return failures

    if mode == "midnight":
        if not (1 <= before_dow <= week - 1):
            failures.append(f"midnight requires the prior weekday in 1..{week - 1}; {before_dow_path} = {before_dow!r}")
        if before_delta < 3:
            failures.append(
                f"midnight requires the prior delta >= 3 (a non-boundary date); "
                f"{target_path} = {target!r} with {before_dow_path} = {before_dow!r} gives "
                f"{before_delta}"
            )
        if after_delta != before_delta - 1:
            failures.append(
                f"midnight requires afterDelta = beforeDelta - 1: {after_delta} != "
                f"{before_delta} - 1 = {before_delta - 1} (both computed from the same "
                f"target {target_path} = {target!r})"
            )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdownBefore", ""), "countdown.beforeDays", before_delta, failures
        )
        _date_check_countdown_equals(
            observations, _manifest_string(spec, "countdownAfter", ""), "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, spec, after_delta, failures)
        return failures

    # cycle-wrap
    if before_dow != week or after_dow != 1:
        failures.append(
            f"cycle-wrap requires the week boundary {week} -> 1; got "
            f"{before_dow_path} = {before_dow!r} and {after_dow_path} = {after_dow!r}"
        )
    if after_delta == 0:
        # T == 1: the post-boundary day IS the sermon day, so no non-sermon
        # countdown may be shown. T is allowed to be 1, so this branch is real.
        flag_path = _manifest_string(spec, "nonSermonCountdownShown", "hud.nonSermonCountdownShown")
        if not has_path(observations, flag_path):
            failures.append(
                f"cycle-wrap with afterDelta 0 requires {flag_path!r} to prove no non-sermon countdown is shown"
            )
        elif get_path(observations, flag_path) is not False:
            failures.append(
                f"{flag_path} = {get_path(observations, flag_path)!r} must be false when the "
                "post-boundary day is the sermon day"
            )
        return failures

    if not (1 <= after_delta <= week - 1):
        failures.append(
            f"cycle-wrap computed afterDelta {after_delta} outside 1..{week - 1} from "
            f"{target_path} = {target!r} and {after_dow_path} = {after_dow!r}"
        )
    visible_path = _manifest_string(spec, "visible", "hud.visible")
    if not has_path(observations, visible_path):
        failures.append(f"cycle-wrap requires {visible_path!r} to prove the countdown is visible")
    elif get_path(observations, visible_path) is not True:
        failures.append(
            f"{visible_path} = {get_path(observations, visible_path)!r} must be true when a "
            "non-sermon countdown is shown"
        )
    _date_check_countdown_equals(
        observations,
        _manifest_string(spec, "countdown", "hud.countdownDays"),
        "countdownDays",
        after_delta,
        failures,
    )
    _date_check_text(observations, spec, after_delta, failures)
    return failures


def evaluate_expectation(condition: JsonObject, observations: JsonObject, environment: JsonObject) -> tuple[bool, str]:
    if "dateCheck" in condition:
        failures = evaluate_date_check(condition, observations)
        if failures:
            return False, "; ".join(failures)
        return True, ""

    obs_path_value = condition.get("observation")
    if not isinstance(obs_path_value, str) or not obs_path_value.strip():
        return False, f"expectation is missing a usable observation path: {condition!r}"
    obs_path = obs_path_value
    if not has_path(observations, obs_path):
        return False, f"observation {obs_path!r} was not captured"

    value = get_path(observations, obs_path)

    if "equalsEnvironment" in condition:
        env_path = condition["equalsEnvironment"]
        if not isinstance(env_path, str) or not env_path.strip():
            return False, "equalsEnvironment must name a non-empty environment path"
        if not isinstance(environment, dict) or not has_path(environment, env_path):
            return False, f"environment field {env_path!r} was not captured"
        env_value = get_path(environment, env_path)
        if json_type_kind(value) != json_type_kind(env_value) or value != env_value:
            return False, (
                f"{obs_path} = {value!r} must equal environment {env_path} = {env_value!r} (strict JSON type match)"
            )

    if "equals" in condition:
        # Strict JSON value type semantics: expected bool/int/str must match the
        # observed JSON type exactly. Python treats 0 == False and 1 == True, so
        # an unchecked equality would let numeric 0/1 satisfy an expected boolean.
        expected = condition["equals"]
        if json_type_kind(value) != json_type_kind(expected):
            return False, (
                f"{obs_path} = {value!r} ({json_type_kind(value)}), expected "
                f"{expected!r} ({json_type_kind(expected)}); strict JSON type match required"
            )
        if value != expected:
            return False, f"{obs_path} = {value!r}, expected {expected!r}"

    if "equalsObservation" in condition:
        other_path_value = condition["equalsObservation"]
        if not isinstance(other_path_value, str) or not other_path_value.strip():
            return False, f"equalsObservation must name an observation path: {other_path_value!r}"
        other_path = other_path_value
        if not has_path(observations, other_path):
            return False, f"comparison observation {other_path!r} was not captured"
        comparison_value = get_path(observations, other_path)
        if json_type_kind(value) != json_type_kind(comparison_value) or value != comparison_value:
            return False, f"{obs_path} = {value!r} != {other_path} = {comparison_value!r} (strict JSON type match)"

    if "notEqualsObservation" in condition:
        other_path_value = condition["notEqualsObservation"]
        if not isinstance(other_path_value, str) or not other_path_value.strip():
            return False, f"notEqualsObservation must name an observation path: {other_path_value!r}"
        other_path = other_path_value
        if not has_path(observations, other_path):
            return False, f"comparison observation {other_path!r} was not captured"
        comparison_value = get_path(observations, other_path)
        if json_type_kind(value) == json_type_kind(comparison_value) and value == comparison_value:
            return False, f"{obs_path} = {value!r} unexpectedly equals {other_path}"

    if "greaterThanObservation" in condition:
        other_path_value = condition["greaterThanObservation"]
        if not isinstance(other_path_value, str) or not other_path_value.strip():
            return False, f"greaterThanObservation must name an observation path: {other_path_value!r}"
        other_path = other_path_value
        if not has_path(observations, other_path):
            return False, f"comparison observation {other_path!r} was not captured"
        comparison_value = get_path(observations, other_path)
        if not (is_number(value) and is_number(comparison_value) and value > comparison_value):
            return False, f"{obs_path} = {value!r} is not greater than {other_path} = {comparison_value!r}"

    if "notEquals" in condition:
        expected = condition["notEquals"]
        if json_type_kind(value) == json_type_kind(expected) and value == expected:
            return False, (f"{obs_path} = {value!r} must differ from {expected!r} (strict JSON type match)")

    if "integerGreaterThan" in condition:
        threshold = condition["integerGreaterThan"]
        if not is_int(threshold):
            return False, f"manifest threshold {threshold!r} must be an integer"
        if not is_int(value) or not value > threshold:
            return False, (
                f"{obs_path} = {value!r} is not a strict integer greater than {threshold}; "
                "floats and booleans do not qualify"
            )

    if "integerBetween" in condition:
        bounds = condition["integerBetween"]
        if not isinstance(bounds, dict):
            return False, f"malformed integerBetween bounds: {bounds!r}"
        low_value = bounds.get("min")
        high_value = bounds.get("max")
        if not is_int(low_value) or not is_int(high_value):
            return False, f"malformed integerBetween bounds: {bounds!r}"
        if not is_int(value) or not (low_value <= value <= high_value):
            return False, (
                f"{obs_path} = {value!r} outside required integer range [{low_value}, {high_value}]; "
                "floats and booleans do not qualify"
            )

    if "numberRange" in condition:
        range_spec = condition["numberRange"]
        if not isinstance(range_spec, dict):
            return False, f"numberRange must be an object: {range_spec!r}"
        if not _finite_number(value):
            return False, (
                f"{obs_path} = {value!r} ({json_type_kind(value)}) is not a finite int/float; "
                "a numeric range cannot be satisfied by a non-number"
            )
        if "min" in range_spec:
            minimum = range_spec["min"]
            if not _finite_number(minimum) or not (value >= minimum):
                return False, f"{obs_path} = {value!r} is below min {minimum!r}"
        if "max" in range_spec:
            maximum = range_spec["max"]
            if not _finite_number(maximum) or not (value <= maximum):
                return False, f"{obs_path} = {value!r} exceeds max {maximum!r}"
        if "maxExclusive" in range_spec:
            exclusive = range_spec["maxExclusive"]
            if not _finite_number(exclusive) or not (value < exclusive):
                return False, (f"{obs_path} = {value!r} must be strictly below maxExclusive {exclusive!r}")

    if "measuredRatio" in condition:
        ratio_spec = condition["measuredRatio"]
        if not isinstance(ratio_spec, dict):
            return False, f"measuredRatio must be an object: {ratio_spec!r}"
        num_path = _manifest_string(ratio_spec, "numerator", "")
        den_path = _manifest_string(ratio_spec, "denominator", "")
        if not has_path(observations, num_path):
            return False, f"measuredRatio numerator observation {num_path!r} was not captured"
        if not has_path(observations, den_path):
            return False, f"measuredRatio denominator observation {den_path!r} was not captured"
        numerator = get_path(observations, num_path)
        denominator = get_path(observations, den_path)
        if not _finite_number(numerator):
            return False, (f"{num_path} = {numerator!r} must be a finite int/float measurement")
        if not _finite_number(denominator) or denominator <= 0:
            return False, (f"{den_path} = {denominator!r} must be a finite positive int/float measurement")
        observed = numerator / denominator
        expected = RATIO_EXPECTED_NUMERATOR / RATIO_EXPECTED_DENOMINATOR
        tolerance_value = ratio_spec.get("tolerance", RATIO_TOLERANCE)
        if not _finite_number(tolerance_value):
            return False, f"measuredRatio tolerance {tolerance_value!r} must be a finite int/float"
        tolerance = tolerance_value
        if abs(observed - expected) > tolerance:
            return False, (
                f"{num_path}/{den_path} = {numerator!r}/{denominator!r} = {observed!r} "
                f"differs from {RATIO_EXPECTED_NUMERATOR}:{RATIO_EXPECTED_DENOMINATOR} "
                f"({expected!r}) by more than tolerance {tolerance!r}"
            )

    if "present" in condition:
        want = condition["present"]
        is_present = value is not None and value != ""
        if want and not is_present:
            return False, f"{obs_path} is absent but must be present"

    if "localizedText" in condition:
        spec = condition["localizedText"]
        if not isinstance(spec, dict):
            return False, "localizedText must be an object"
        key_path = spec.get("keyObservation")
        if isinstance(key_path, str):
            if not has_path(observations, key_path):
                return False, f"localization key observation {key_path!r} was not captured"
            key = get_path(observations, key_path)
            allowed = spec.get("keysAnyOf")
            if not isinstance(allowed, list) or key not in allowed:
                return False, f"{key_path} = {key!r}, expected one of {allowed!r}"
        else:
            key = spec.get("key")
        if not isinstance(key, str) or not key:
            return False, "localizedText requires a non-empty resource key"
        days = None
        days_path = spec.get("daysObservation")
        if isinstance(days_path, str):
            if not has_path(observations, days_path):
                return False, f"interpolation observation {days_path!r} was not captured"
            days = get_path(observations, days_path)
            if not is_int(days) or days < 1:
                return False, f"{days_path} = {days!r} must be a positive integer"
            expected_key = "gksr.hud.sermonCountdown.one" if days == 1 else "gksr.hud.sermonCountdown.other"
            if key != expected_key:
                return False, f"{key!r} must be {expected_key!r} for a shown countdown of {days}"
        elif key == "gksr.hud.sermonCountdown.other":
            return False, "multi-day localization requires daysObservation"
        failures = check_localized_output(observations, obs_path, key, _manifest_string(spec, "language", ""), days)
        if failures:
            return False, "; ".join(failures)

    return True, ""


def evaluate_scenario(scenario: JsonObject, observations: JsonObject, environment: JsonObject) -> list[str]:
    scenario_id = scenario.get("id")
    expect = scenario.get("expect")
    if not isinstance(expect, list):
        return [f"scenario {scenario_id!r} has no expectations"]
    failures: list[str] = []
    for condition in expect:
        if not isinstance(condition, dict):
            failures.append(f"scenario {scenario_id!r} has a malformed expectation: {condition!r}")
            continue
        ok, message = evaluate_expectation(condition, observations, environment)
        if not ok:
            failures.append(message)
    return failures


def write_artifact(output_path: Path, artifact: dict[str, object]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def check_manifest_only(manifest_path: Path, output_path: Path) -> int:
    """Structural-only manifest check for CI.

    This proves the bundle is well-formed and usable by the evidence validator.
    It NEVER asserts that any game scenario passed and is not an E2E verdict;
    the artifact records assertion="manifest-structure-only" explicitly.
    """
    artifact: dict[str, object] = {
        "artifactVersion": 2,
        "kind": MANIFEST_KIND_REPORT,
        "assertion": "manifest-structure-only",
        "e2eVerdict": None,
        "ok": False,
        "manifest": str(manifest_path),
        "manifestId": None,
        "manifestSha256": None,
        "scope": None,
        "scenarioCount": 0,
        "scenarioIds": [],
        "error": None,
        "note": (
            "Structural validation only. This artifact does not represent real "
            "game execution or business acceptance; only a complete capture "
            "validated by the evidence mode can do that."
        ),
    }

    try:
        bundle = e2e_manifest.load_manifest(manifest_path)
    except e2e_manifest.ManifestInputError as exc:
        # An explicit missing/invalid input never inherits the identity of an
        # unrelated sibling index: manifestId/manifestSha256/scope stay null.
        artifact["error"] = str(exc)
        write_artifact(output_path, artifact)
        sys.stderr.write(f"FAIL [manifest] {exc}\n")
        sys.stderr.write("RESULT: FAIL (manifest structure)\n")
        return 2
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Defensive net: malformed JSON shapes must fail closed with a
        # deterministic artifact, never escape as an uncaught interpreter error.
        message = f"malformed manifest structure: {type(exc).__name__}: {exc}"
        artifact["error"] = message
        write_artifact(output_path, artifact)
        sys.stderr.write(f"FAIL [manifest] {message}\n")
        sys.stderr.write("RESULT: FAIL (manifest structure)\n")
        return 2

    artifact["ok"] = True
    artifact["manifestId"] = bundle.manifest_id
    artifact["manifestSha256"] = bundle.sha256
    artifact["scope"] = bundle.scope
    artifact["scenarioCount"] = len(bundle.scenarios)
    artifact["scenarioIds"] = list(bundle.scenario_ids())
    write_artifact(output_path, artifact)
    sys.stderr.write(
        f"RESULT: PASS (manifest structure only; {len(bundle.scenarios)} scenarios "
        "declared; no game execution asserted)\n"
    )
    return 0


def _entry_failures(entry: dict[str, object]) -> list[object]:
    existing = entry.get("failures")
    if isinstance(existing, list):
        return existing
    fresh: list[object] = []
    entry["failures"] = fresh
    return fresh


def check_capture(manifest_path: Path, capture_path: Path, output_path: Path, allow_synthetic: bool) -> int:
    """Validate one capture against the v2 manifest bundle.

    Protocol/shape/containment/environment problems are invalid input (exit 2);
    an unexecuted, skipped, extra or wrong-value scenario is a business failure
    (exit 1). Every outcome writes a deterministic artifact with e2eVerdict null.
    """
    report: dict[str, object] = {
        "artifactVersion": 2,
        "kind": VALIDATION_KIND_REPORT,
        "assertion": None,
        "e2eVerdict": None,
        "ok": False,
        "manifest": str(manifest_path),
        "manifestId": None,
        "manifestSha256": None,
        "scope": None,
        "capture": str(capture_path),
        "captureSha256": None,
        "capturePurpose": None,
        "capturedAt": "",
        "scenarios": [],
        "summary": {"total": 0, "passed": 0, "failed": 0, "unexecuted": 0},
        "unexpectedScenarioRecords": [],
        "unexpectedObservationRecords": [],
        "error": None,
    }

    def reject(stage: str, message: str, code: int = 2) -> int:
        report["error"] = {"stage": stage, "message": message}
        write_artifact(output_path, report)
        sys.stderr.write(f"FAIL [{stage}] {message}\n")
        return code

    try:
        bundle = e2e_manifest.load_manifest(manifest_path)
    except e2e_manifest.ManifestInputError as exc:
        return reject("manifest", str(exc))

    report["manifestId"] = bundle.manifest_id
    report["manifestSha256"] = bundle.sha256
    report["scope"] = bundle.scope

    try:
        capture = load_json(capture_path)
    except Fail as exc:
        return reject("capture", str(exc))

    try:
        purpose, env, observations, results = validate_capture_shape(capture, bundle)
        check_language_observations(observations, bundle)
        evidence_files = validate_environments(env, bundle)
        validated_evidence = validate_evidence_files(env, evidence_files, capture_path.parent)
    except Fail as exc:
        return reject("capture", str(exc))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Defensive net: malformed capture shapes must fail closed with a
        # deterministic artifact, never escape as an uncaught interpreter error.
        return reject("capture", f"malformed capture structure: {type(exc).__name__}: {exc}")

    report["capturePurpose"] = purpose
    report["assertion"] = "synthetic-cli-only" if purpose == "tool-fixture" else "captured-observations"
    report["capturedAt"] = env.get("capturedAt", "")

    require_load_log = False
    for scenario in bundle.scenarios:
        validation = bundle.profile_of(scenario).get("validation")
        if isinstance(validation, dict) and validation.get("requireLoadEvidenceLog") is True:
            require_load_log = True
            break
    if require_load_log and not any("log" in key.lower() for key in evidence_files):
        return reject(
            "capture",
            "requireLoadEvidenceLog is set but no log-like evidence file is declared",
        )

    executed: dict[str, JsonObject] = {}
    for record in results:
        if isinstance(record, dict):
            scenario_id = record.get("scenarioId")
            if isinstance(scenario_id, str):
                executed[scenario_id] = record

    scenario_ids = set(bundle.scenario_ids())
    entry_results: list[dict[str, object]] = []
    any_failed = False

    for scenario in bundle.scenarios:
        sid = scenario["id"]
        if not isinstance(sid, str):
            continue
        record = executed.get(sid)
        entry: dict[str, object] = {
            "scenarioId": sid,
            "title": scenario.get("title", ""),
            # executed is True exactly when a result record exists for the id.
            "executed": record is not None,
        }
        if record is None:
            entry["status"] = "failed"
            entry["failures"] = ["scenario not executed"]
            entry["observations"] = {}
            any_failed = True
            entry_results.append(entry)
            continue

        entry["status"] = record.get("status")
        entry["notes"] = record.get("notes", "")

        recorded_value = record.get("evidence", {})
        recorded_evidence = recorded_value if isinstance(recorded_value, dict) else {}
        requirements_value = scenario.get("evidence")
        requirements = requirements_value if isinstance(requirements_value, dict) else {}

        scenario_evidence: list[dict[str, object]] = []
        for required in ("screenshot", "log"):
            if not requirements.get(required):
                continue
            paths = recorded_evidence.get(required)
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not paths:
                _entry_failures(entry).append(f"required {required} evidence not recorded for the scenario")

        for category, paths in recorded_evidence.items():
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list):
                continue
            for name in paths:
                if not isinstance(name, str):
                    continue
                if name not in evidence_files:
                    _entry_failures(entry).append(
                        f"recorded evidence {name!r} is not declared in environment.evidenceFiles"
                    )
                    continue
                if name not in validated_evidence:
                    _entry_failures(entry).append(f"recorded evidence {name!r} was not verified")
                    continue
                scenario_evidence.append({"category": category, "file": name})
        entry["evidence"] = scenario_evidence

        if requirements.get("measurement") or record.get("measurement", False):
            ok_measure, message = evaluate_measurement(scenario, record, validated_evidence)
            if not ok_measure:
                _entry_failures(entry).append(message)

        scenario_observations = observations.get(sid)
        if not isinstance(scenario_observations, dict) or not scenario_observations:
            _entry_failures(entry).append(f"no observations recorded for scenario {sid}")
        else:
            entry["observations"] = scenario_observations
            for failure in evaluate_scenario(scenario, scenario_observations, env):
                _entry_failures(entry).append(failure)

        if entry.get("failures"):
            entry["status"] = "failed"
            any_failed = True
        elif entry.get("status") != "passed":
            entry["status"] = "failed"
            _entry_failures(entry).append(
                f"scenario status is {entry.get('status')!r}, not 'passed'; unexecuted or skipped scenarios cannot pass"
            )
            any_failed = True
        else:
            entry["failures"] = []
        entry_results.append(entry)

    extras = sorted(set(executed) - scenario_ids)
    observation_extras = sorted(set(observations) - scenario_ids)
    if extras or observation_extras:
        any_failed = True

    passed_count = sum(1 for entry in entry_results if entry.get("status") == "passed")
    report["ok"] = not any_failed
    report["scenarios"] = entry_results
    report["summary"] = {
        "total": len(entry_results),
        "passed": passed_count,
        "failed": len(entry_results) - passed_count,
        "unexecuted": sum(1 for entry in entry_results if entry.get("executed") is not True),
    }
    report["captureSha256"] = sha256_file(capture_path)
    report["unexpectedScenarioRecords"] = extras
    report["unexpectedObservationRecords"] = observation_extras

    # A tool-fixture capture may be validated without the opt-in so its business
    # failures are still reported, but a *passing* synthetic positive is only
    # accepted with an explicit --allow-synthetic; it is never game acceptance.
    if not any_failed and purpose == "tool-fixture" and not allow_synthetic:
        report["ok"] = False
        report["error"] = {
            "stage": "capture",
            "message": (
                "capturePurpose 'tool-fixture' requires the --allow-synthetic opt-in; "
                "a synthetic positive is never a game acceptance result"
            ),
        }
        write_artifact(output_path, report)
        sys.stderr.write("FAIL [capture] capturePurpose 'tool-fixture' requires the --allow-synthetic opt-in\n")
        return 2

    write_artifact(output_path, report)

    for entry in entry_results:
        if entry.get("status") == "passed":
            continue
        failures = entry.get("failures")
        if isinstance(failures, list):
            for reason in failures:
                sys.stderr.write(f"FAIL [{entry.get('scenarioId')}] {reason}\n")
    if extras:
        sys.stderr.write(f"FAIL [capture] unexpected execution records: {extras}\n")
    if observation_extras:
        sys.stderr.write(f"FAIL [capture] unexpected observation records: {observation_extras}\n")
    if any_failed:
        sys.stderr.write("RESULT: FAIL (fail-closed)\n")
        return 1

    sys.stderr.write(f"RESULT: PASS ({passed_count}/{len(entry_results)} scenarios)\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a GKSA-23 v2 E2E capture.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--check-manifest",
        action="store_true",
        help=(
            "Structural-only manifest check for CI. Validates the whole v2 bundle "
            "and emits a structure artifact; NEVER asserts game execution or E2E "
            "pass. Requires --output and rejects --capture."
        ),
    )
    parser.add_argument("--capture")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="accept a capturePurpose=tool-fixture capture (synthetic CLI fixture only).",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    output_path = Path(args.output)

    if args.check_manifest and args.capture:
        sys.stderr.write(
            "FAIL [cli] --check-manifest proves manifest structure only and must "
            "not be combined with --capture; use the evidence mode for a capture.\n"
        )
        return 2

    if not args.check_manifest and not args.capture:
        sys.stderr.write(
            "FAIL [cli] evidence mode requires --capture; use --check-manifest for a structural-only check.\n"
        )
        return 2

    if args.check_manifest:
        return check_manifest_only(manifest_path, output_path)

    return check_capture(manifest_path, Path(args.capture), output_path, args.allow_synthetic)


def evaluate_measurement(scenario: JsonObject, record: JsonObject, validated_evidence: set[str]) -> tuple[bool, str]:
    """A measurement-backed scenario must carry a concrete measurement or an
    explicit operator observation, plus at least one verified screenshot path.
    Logs alone never qualify."""
    recorded_value = record.get("evidence", {})
    recorded = recorded_value if isinstance(recorded_value, dict) else {}
    screenshots_value = recorded.get("screenshot")
    if isinstance(screenshots_value, str):
        screenshots: list[str] = [screenshots_value]
    elif isinstance(screenshots_value, list):
        screenshots = [item for item in screenshots_value if isinstance(item, str)]
    else:
        screenshots = []
    if not screenshots:
        return False, "measurement scenario requires a screenshot evidence category"
    if not any(path in validated_evidence for path in screenshots):
        return False, (
            "measurement scenario requires at least one verified screenshot path; "
            "an unverified or empty screenshot category is not proof"
        )

    measurement = record.get("measurement")
    if measurement is False or measurement is None:
        return False, "measurement evidence required but not recorded"
    if not isinstance(measurement, dict) or not measurement:
        return False, "measurement object is missing or empty"

    values = measurement.get("values")
    has_values = isinstance(values, dict) and bool(values) and all(is_number(v) for v in values.values())
    observation = measurement.get("operatorObservation")
    has_observation = isinstance(observation, str) and bool(observation.strip())
    if not has_values and not has_observation:
        return False, (
            "measurement must include a non-empty numeric values object or a non-empty operatorObservation string"
        )
    return True, ""


if __name__ == "__main__":
    raise SystemExit(main())
