#!/usr/bin/env python3
"""GKSA-10..GKSA-15 E2E evidence validator.

Validates a machine-readable capture of a real in-game observation run against
the matching ticket scenario manifest. It is an evidence checker, not a game
simulator: it never invents observations, never grades an unexecuted scenario
as passed, and never treats logs alone as proof of a visual/layout outcome.

Fail-closed: any missing file, hash mismatch, unsupported schema, unexecuted
scenario, wrong expected value, or absent required evidence file fails the run.

Usage:
  python3 tests/e2e/verify.py --manifest tests/e2e/gksa10.json \
      --capture PATH --output PATH

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

SUPPORTED_CAPTURE_VERSIONS = (1,)
SUPPORTED_MANIFEST_VERSIONS = (1,)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)

# Ticket kinds this validator accepts: exactly 'gksa10'..'gksa15' (lowercase
# ticket id without a dash) followed by '-e2e-manifest' or '-e2e-capture'. The
# kind is the only trusted source of the ticket identity; a capture whose kind
# does not match the manifest ticket fails closed.
TICKET_NUMBER_MIN = 10
TICKET_NUMBER_MAX = 15
TICKET_STEM_RE = re.compile(r"^gksa(\d{1,4})$")

# Fixed native icon aspect 20:18 and its small comparison tolerance for the one
# bounded measuredRatio check. No other ratio or general evaluation is offered.
RATIO_EXPECTED_NUMERATOR = 20
RATIO_EXPECTED_DENOMINATOR = 18
RATIO_TOLERANCE = 0.001
RATIO_TOLERANCE_MAX = 0.01


class Fail(Exception):
    """Raised when validation cannot proceed or a top-level check fails."""


# --------------------------------------------------------------------------
# tiny path helpers (flat dotted keys, e.g. "game.sha256")
# --------------------------------------------------------------------------

def get_path(obj, dotted):
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


def has_path(obj, dotted):
    try:
        get_path(obj, dotted)
        return True
    except KeyError:
        return False


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value):
    """Strict JSON integer: bool is not an integer, float is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_int(value):
    return is_int(value) and value > 0


def _finite_number(value):
    """True for a finite JSON int/float. Bools, strings, NaN and infinity are
    rejected so a numeric check can never be satisfied by a non-number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def ticket_from_kind(kind, suffix):
    """Return (stem, ticket_id) for a supported versioned kind, else None.

    ``kind`` must be exactly ``<stem>-<suffix>`` where ``stem`` is a supported
    lowercase ticket id without a dash (e.g. 'gksa11'), and ``ticket_id`` is the
    display form (e.g. 'GKSA-11'). Nothing else is trusted.
    """
    if not isinstance(kind, str):
        return None
    tail = "-" + suffix
    if not kind.endswith(tail):
        return None
    match = TICKET_STEM_RE.match(kind[: -len(tail)])
    if not match:
        return None
    digits = match.group(1)
    # Canonical form only: no leading zeros, so 'gksa011' is not 'gksa11'.
    if str(int(digits)) != digits:
        return None
    number = int(digits)
    if not (TICKET_NUMBER_MIN <= number <= TICKET_NUMBER_MAX):
        return None
    return match.group(0), "GKSA-%d" % number


def result_kind(ticket_id, suffix):
    """Artifact kind derives from the validated ticket, never from a literal."""
    return ticket_id.lower().replace("-", "") + "-" + suffix


def trusted_ticket_from_manifest(manifest):
    """Best-effort ticket id from a manifest kind, or '' when untrusted."""
    if not isinstance(manifest, dict):
        return ""
    parsed = ticket_from_kind(manifest.get("kind"), "e2e-manifest")
    return parsed[1] if parsed else ""


def json_type_kind(value):
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

def check_type(value, kind):
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
            raise Fail(
                f"expected an integer weekday in 1..6 (EnvironmentData six-day "
                f"cycle), got {value!r}"
            )
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
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and v.strip() for v in value)
        ):
            raise Fail(f"expected non-empty array of strings, got {value!r}")
    elif kind == "object":
        if not isinstance(value, dict):
            raise Fail(f"expected object, got {value!r}")
    else:
        raise Fail(f"unknown expected type {kind!r}")


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# manifest validation
# --------------------------------------------------------------------------

def load_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise Fail(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise Fail(f"invalid JSON in {path}: {exc}") from exc


def validate_date_check(sid, condition):
    """Structurally validate one bounded dateCheck helper condition.

    The helper is deliberately a fixed-mode calendar checker, not a general
    expression DSL: each mode has an exact required field set and no unknown
    fields are accepted.
    """
    spec = condition["dateCheck"]
    if not isinstance(spec, dict) or not spec:
        raise Fail(f"scenario {sid} dateCheck must be a non-empty object")
    mode = spec.get("mode")
    if mode not in date_check_modes:
        raise Fail(
            f"scenario {sid} dateCheck.mode {mode!r} must be one of "
            f"{list(date_check_modes)}"
        )
    required = set(date_check_required_fields[mode]) \
        | {"mode", "target", "week"} \
        | set(DATE_CHECK_OPTIONAL_STRING_FIELDS)
    unknown = sorted(set(spec) - required)
    if unknown:
        raise Fail(f"scenario {sid} dateCheck({mode}) has unknown fields: {unknown}")
    for field in date_check_required_fields[mode]:
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise Fail(
                f"scenario {sid} dateCheck({mode}) requires a non-empty "
                f"observation path for {field!r}"
            )
    for field in ("target", "week", "countdown", "text", "textKey", "language",
                  "visible", "nonSermonCountdownShown"):
        if field in spec and (not isinstance(spec[field], str) or not spec[field].strip()):
            raise Fail(
                f"scenario {sid} dateCheck({mode}) field {field!r} must be a "
                "non-empty observation path"
            )
    if "target" in spec and spec["target"] != "save.sermonWeekday":
        raise Fail(
            f"scenario {sid} dateCheck({mode}) must read the target from the observed "
            f"save.sermonWeekday (got {spec['target']!r}); day_wrath is a single runtime "
            "definition that scenarios must never redefine"
        )
    if "week" in spec and spec["week"] != "save.daysInWeek":
        raise Fail(
            f"scenario {sid} dateCheck({mode}) must read the week length from the "
            f"observed save.daysInWeek (got {spec['week']!r})"
        )
    if "language" in spec and spec["language"] not in ("zh-CN", "en"):
        raise Fail(
            f"scenario {sid} dateCheck({mode}) language {spec['language']!r} must be a "
            "catalog label (zh-CN or en)"
        )


def _bounded_numeric(sid, label, value):
    """Validate one finite int/float bound; bools and non-numbers are rejected."""
    if not _finite_number(value):
        raise Fail(
            f"scenario {sid} {label} must be a finite int/float (bool and non-numbers "
            f"are rejected); got {value!r} ({json_type_kind(value)})"
        )


def validate_number_range(sid, condition):
    """One bounded numeric check: inclusive min/max or exclusive maxExclusive.

    Deliberately narrow, not a general evaluation DSL. At least one bound is
    required; an unrecognized bound key is rejected rather than ignored.
    """
    spec = condition["numberRange"]
    if not isinstance(spec, dict) or not spec:
        raise Fail(f"scenario {sid} numberRange must be a non-empty object")
    allowed = {"min", "max", "maxExclusive"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise Fail(f"scenario {sid} numberRange has unknown fields: {unknown}")
    has_min = "min" in spec
    has_max = "max" in spec
    has_max_exclusive = "maxExclusive" in spec
    if not (has_min or has_max or has_max_exclusive):
        raise Fail(
            f"scenario {sid} numberRange requires at least one bound "
            "(min, max or maxExclusive)"
        )
    if has_max and has_max_exclusive:
        raise Fail(
            f"scenario {sid} numberRange must not declare both max and maxExclusive"
        )
    for key in ("min", "max", "maxExclusive"):
        if key in spec:
            _bounded_numeric(sid, "numberRange." + key, spec[key])
    if has_min and (has_max or has_max_exclusive):
        upper = spec["max"] if has_max else spec["maxExclusive"]
        if spec["min"] > upper:
            raise Fail(
                f"scenario {sid} numberRange min {spec['min']!r} exceeds upper bound "
                f"{upper!r}"
            )


def validate_measured_ratio(sid, condition):
    """One bounded ratio check: observed width/height against the fixed 20:18 icon.

    Requires raw width and height observations and an explicit finite, positive
    denominator; a pre-asserted ratio flag is not accepted.
    """
    spec = condition["measuredRatio"]
    if not isinstance(spec, dict) or not spec:
        raise Fail(f"scenario {sid} measuredRatio must be a non-empty object")
    allowed = {"numerator", "denominator", "tolerance"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise Fail(f"scenario {sid} measuredRatio has unknown fields: {unknown}")
    for field in ("numerator", "denominator"):
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise Fail(
                f"scenario {sid} measuredRatio requires the raw {field!r} observation "
                f"path; got {value!r}"
            )
    tolerance = spec.get("tolerance", RATIO_TOLERANCE)
    if not _finite_number(tolerance):
        raise Fail(
            f"scenario {sid} measuredRatio tolerance must be a finite int/float; "
            f"got {tolerance!r} ({json_type_kind(tolerance)})"
        )
    if tolerance < 0 or tolerance > RATIO_TOLERANCE_MAX:
        raise Fail(
            f"scenario {sid} measuredRatio tolerance {tolerance!r} must be within "
            f"0..{RATIO_TOLERANCE_MAX}"
        )


def validate_manifest(manifest):
    """Strictly check the manifest shape and return the scenario list.

    This is structural validation only: it proves the manifest can be used by
    the full evidence validator. It never asserts that any game scenario passed.
    Every malformed shape raises Fail (never an uncaught AttributeError,
    KeyError or TypeError), so the caller can emit a deterministic artifact.
    """
    if not isinstance(manifest, dict):
        raise Fail(f"manifest root must be a JSON object, got {type(manifest).__name__}")
    parsed_kind = ticket_from_kind(manifest.get("kind"), "e2e-manifest")
    if parsed_kind is None:
        raise Fail(
            "unsupported manifest kind: %r (accepted: gksa10..gksa15 followed by "
            "'-e2e-manifest', lowercase ticket id without a dash)" % (manifest.get("kind"),)
        )
    manifest_ticket = parsed_kind[1]
    declared_ticket = manifest.get("ticket")
    if "ticket" in manifest and declared_ticket != manifest_ticket:
        raise Fail(
            f"manifest ticket {declared_ticket!r} does not match kind {manifest.get('kind')!r} "
            f"(expected {manifest_ticket!r})"
        )
    manifest_version = manifest.get("manifestVersion")
    if not is_int(manifest_version) or manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise Fail(
            f"manifestVersion must be exactly integer {list(SUPPORTED_MANIFEST_VERSIONS)}; "
            f"got {manifest_version!r} ({json_type_kind(manifest_version)})"
        )
    final_text = manifest.get("finalText")
    if not isinstance(final_text, dict) or not final_text:
        raise Fail("manifest must declare finalText for every required key")
    for key, translations in final_text.items():
        if not isinstance(key, str) or not key.strip():
            raise Fail(f"finalText key {key!r} must be a non-empty string")
        if not isinstance(translations, dict) or not translations:
            raise Fail(f"finalText[{key!r}] must map languages to complete sentences")
        for language, text in translations.items():
            if not isinstance(text, str) or not text.strip():
                raise Fail(f"finalText[{key!r}][{language!r}] must be a non-empty sentence")
        en_placeholders = re.findall(r"\{(\w+)\}", translations.get("en", ""))
        if en_placeholders:
            en_params = set(en_placeholders)
            zh_params = set(re.findall(r"\{(\w+)\}", translations.get("zh-CN", "")))
            if en_params != zh_params:
                raise Fail(
                    f"finalText[{key!r}] placeholder mismatch between en and zh-CN: "
                    f"{sorted(en_params)} vs {sorted(zh_params)}"
                )

    language_mapping = manifest.get("languageMapping")
    if not isinstance(language_mapping, dict) or not language_mapping:
        raise Fail("manifest must declare languageMapping between catalog labels and raw game ids")
    label_map = language_mapping.get("catalogLabelToRawGameLanguageId")
    if not isinstance(label_map, dict) or not label_map:
        raise Fail(
            "languageMapping.catalogLabelToRawGameLanguageId must be a non-empty object"
        )
    for label, raw_id in label_map.items():
        if not isinstance(label, str) or not isinstance(raw_id, str) or not raw_id.strip():
            raise Fail(
                f"languageMapping entry {label!r} -> {raw_id!r} must map a string "
                "catalog label to a non-empty raw game language id"
            )

    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise Fail("manifest must declare a non-empty scenarios array")
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise Fail(f"each scenario must be an object, got {type(scenario).__name__}")
        sid = scenario.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise Fail(f"scenario without a usable id: {scenario!r}")
        if sid in seen:
            raise Fail(f"duplicate scenario id {sid!r}")
        seen.add(sid)
        expect = scenario.get("expect")
        if not isinstance(expect, list) or not expect:
            raise Fail(f"scenario {sid} has no expectations")
        for condition in expect:
            if not isinstance(condition, dict):
                raise Fail(f"scenario {sid} has a malformed expectation: {condition!r}")
            if "dateCheck" in condition:
                validate_date_check(sid, condition)
                continue
            if not isinstance(condition.get("observation"), str) \
                    or not condition["observation"].strip():
                raise Fail(f"scenario {sid} has a malformed expectation: {condition!r}")
            if "equalsEnvironment" in condition:
                env_target = condition["equalsEnvironment"]
                if not isinstance(env_target, str) or not env_target.strip():
                    raise Fail(
                        f"scenario {sid} equalsEnvironment must name a non-empty "
                        f"environment path: {condition!r}"
                    )
                if condition["observation"] != "save.sermonWeekday":
                    raise Fail(
                        f"scenario {sid} equalsEnvironment is only valid on "
                        "save.sermonWeekday so the observed sermon weekday is compared "
                        "with the single runtime definition; got "
                        f"{condition['observation']!r}"
                    )
            if condition["observation"] == "save.sermonWeekday" \
                    and "equalsEnvironment" not in condition:
                raise Fail(
                    f"scenario {sid} must compare save.sermonWeekday against the observed "
                    "environment field game.sermonWeekday via equalsEnvironment; a "
                    "hardcoded sermon-weekday literal is forbidden because day_wrath is a "
                    f"single runtime definition. Got: {condition!r}"
                )
            if "equals" not in condition and not (
                "integerGreaterThan" in condition
                or "integerBetween" in condition
                or "greaterThanObservation" in condition
                or "notEquals" in condition
                or "notEqualsObservation" in condition
                or "equalsObservation" in condition
                or "equalsEnvironment" in condition
                or "present" in condition
                or "finalTextKey" in condition
                or "finalTextKeyFromObservation" in condition
                or "numberRange" in condition
                or "measuredRatio" in condition
            ):
                raise Fail(f"scenario {sid} expectation without a check: {condition!r}")
            if "numberRange" in condition:
                validate_number_range(sid, condition)
            if "measuredRatio" in condition:
                validate_measured_ratio(sid, condition)
            if "finalTextKey" in condition and "finalTextKeyFromObservation" in condition:
                raise Fail(
                    f"scenario {sid} expectation must use either finalTextKey or "
                    "finalTextKeyFromObservation, not both"
                )
            if ("finalTextKey" in condition or "finalTextKeyFromObservation" in condition):
                language = condition.get("language")
                if language not in ("zh-CN", "en"):
                    raise Fail(
                        f"scenario {sid} references catalog language {language!r}; "
                        "finalText languages are catalog labels (zh-CN, en)"
                    )
            if condition["observation"] == "language.active":
                raw_ids = set(label_map.values())
                for key in ("equals", "notEquals"):
                    value = condition.get(key)
                    if not isinstance(value, str):
                        continue
                    # A catalog label is only valid here when it is also a raw
                    # game id (e.g. 'en'). Labels like 'zh-CN' are catalog-only
                    # and must never appear in a language.active comparison.
                    # Other raw ids (e.g. an unsupported 'fr') stay allowed so the
                    # unsupported-language fallback scenario remains expressible.
                    if value in label_map and value not in raw_ids:
                        raise Fail(
                            f"scenario {sid} compares language.active against catalog-only "
                            f"label {value!r}; raw game language ids are required "
                            f"(known: {sorted(raw_ids)})"
                        )
        evidence = scenario.get("evidence")
        if not isinstance(evidence, dict):
            raise Fail(f"scenario {sid} must declare an evidence object")
    return scenarios


# --------------------------------------------------------------------------
# capture validation
# --------------------------------------------------------------------------

def check_language_observations(capture, manifest):
    """Reject a capture whose raw game language observation uses a
    catalog-only label (e.g. 'zh-CN' instead of 'zh_cn')."""
    mapping = manifest.get("languageMapping", {})
    label_map = mapping.get("catalogLabelToRawGameLanguageId", {})
    raw_ids = set(label_map.values())
    catalog_only = {label for label in label_map if label not in raw_ids}
    for sid, observations in (capture.get("observations") or {}).items():
        if not isinstance(observations, dict):
            continue
        value = observations.get("language.active")
        if not isinstance(value, str):
            continue
        if value in catalog_only:
            raise Fail(
                f"scenario {sid} recorded language.active = {value!r}, a catalog-only "
                f"label; raw game language ids are required (known: {sorted(raw_ids)})"
            )


def validate_capture_shape(capture, manifest):
    if not isinstance(capture, dict):
        raise Fail(f"capture root must be a JSON object, got {type(capture).__name__}")
    manifest_kind = ticket_from_kind(manifest.get("kind"), "e2e-manifest")
    if manifest_kind is None:
        # Unreachable via main(), which validates the manifest first; kept so this
        # function can never trust a capture against an untrusted manifest.
        raise Fail(
            f"capture cannot be validated: untrusted manifest kind "
            f"{manifest.get('kind')!r}"
        )
    capture_kind = ticket_from_kind(capture.get("kind"), "e2e-capture")
    if capture_kind is None:
        raise Fail(
            "unsupported capture kind: %r (accepted: gksa10..gksa15 followed by "
            "'-e2e-capture', lowercase ticket id without a dash)" % (capture.get("kind"),)
        )
    if capture_kind[0] != manifest_kind[0]:
        raise Fail(
            f"capture kind {capture.get('kind')!r} does not match manifest ticket "
            f"{manifest_kind[1]}; a capture may only be validated against its own "
            "ticket manifest"
        )
    version = capture.get("captureVersion")
    supported = tuple(
        manifest.get("validation", {}).get("supportedCaptureVersions", SUPPORTED_CAPTURE_VERSIONS)
    )
    # Strict integer identity: True == 1 and 1.0 == 1 in Python, so a bare
    # membership test would accept boolean or float aliases of a valid version.
    if not is_int(version) or version not in supported:
        raise Fail(
            f"unsupported captureVersion {version!r} ({json_type_kind(version)}); "
            f"supported integer versions: {list(supported)}"
        )
    environment = capture.get("environment")
    if not isinstance(environment, dict):
        raise Fail("capture.environment must be an object")
    observations = capture.get("observations")
    if not isinstance(observations, dict) or not observations:
        raise Fail("capture.observations must be a non-empty object keyed by scenario id")
    for sid, observation in observations.items():
        if not isinstance(sid, str) or not sid.strip():
            raise Fail(f"capture.observations has a non-string scenario key {sid!r}")
        if not isinstance(observation, dict) or not observation:
            raise Fail(f"capture.observations[{sid!r}] must be a non-empty object")
    results = capture.get("results")
    if not isinstance(results, list):
        raise Fail("capture.results must be an array of executed scenario records")
    seen_ids = set()
    for record in results:
        if not isinstance(record, dict):
            raise Fail(f"each capture result must be an object, got {record!r}")
        if not isinstance(record.get("scenarioId"), str) or not record["scenarioId"].strip():
            raise Fail("each capture result must carry a non-empty scenarioId")
        scenario_id = record["scenarioId"]
        if scenario_id in seen_ids:
            raise Fail(
                f"duplicate capture result scenarioId {scenario_id!r}; "
                "results must not overwrite each other"
            )
        seen_ids.add(scenario_id)
        if not isinstance(record.get("status"), str) or not record["status"].strip():
            raise Fail(f"capture result {scenario_id!r} must carry a status string")
        evidence = record.get("evidence", {})
        if not isinstance(evidence, dict):
            raise Fail(
                f"capture result {scenario_id!r} evidence must be an object "
                "mapping the evidence category (screenshot/log/video) to a path"
            )
        for category, paths in evidence.items():
            if not isinstance(category, str) or not category.strip():
                raise Fail(f"capture result {scenario_id!r} has a malformed evidence category")
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not paths:
                raise Fail(
                    f"capture result {scenario_id!r} category {category!r} must list at "
                    "least one evidence path; empty lists are not evidence"
                )
            if not all(isinstance(p, str) and p.strip() for p in paths):
                raise Fail(
                    f"capture result {scenario_id!r} category {category!r} must map to "
                    "non-empty path strings"
                )
        measurement = record.get("measurement", False)
        if measurement is not False and not isinstance(measurement, dict):
            raise Fail(
                f"capture result {scenario_id!r} measurement must be false or an object"
            )


def validate_environment(env, manifest, capture_dir):
    validation = manifest.get("validation", {})
    if not isinstance(validation, dict):
        raise Fail("manifest.validation must be an object")
    required = validation.get("requiredEnvironmentPaths", [])
    if not isinstance(required, list):
        raise Fail("manifest.validation.requiredEnvironmentPaths must be an array")
    for spec in required:
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise Fail(f"malformed requiredEnvironmentPaths entry: {spec!r}")
        path = spec["path"]
        if not has_path(env, path):
            raise Fail(f"environment is missing required field {path!r}")
        value = get_path(env, path)
        check_type(value, spec["type"])
        if "equals" in spec and (
            json_type_kind(value) != json_type_kind(spec["equals"])
            or value != spec["equals"]
        ):
            raise Fail(
                f"environment field {path!r} must equal {spec['equals']!r} "
                f"({json_type_kind(spec['equals'])}), got {value!r} ({json_type_kind(value)})"
            )

    groups = validation.get("buildPluginHashConsistency", [])
    if not isinstance(groups, list):
        raise Fail("manifest.validation.buildPluginHashConsistency must be an array")
    for group in groups:
        if not isinstance(group, list) or not group:
            raise Fail(f"malformed buildPluginHashConsistency group: {group!r}")
        values = {}
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


def validate_evidence_files(env, evidence_files, capture_dir):
    """Every referenced evidence file must exist, stay inside the capture dir,
    and hash to the recorded digest. Returns the set of verified relative paths."""
    verified = set()
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
            raise Fail(
                f"evidence file hash mismatch for {name!r}: recorded {digest}, actual {actual}"
            )
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

date_check_modes = (
    "single-day",
    "multi-day",
    "same-day",
    "midnight",
    "cycle-wrap",
    "sermon-day",
    "after-sermon-day",
)

date_check_required_fields = {
    "single-day": ("absoluteDay", "dayOfWeek", "countdown"),
    "multi-day": ("absoluteDay", "dayOfWeek", "countdown"),
    "same-day": (
        "absoluteDayBefore",
        "absoluteDayAfter",
        "dayOfWeekBefore",
        "dayOfWeekAfter",
        "countdownBefore",
        "countdownAfter",
    ),
    "midnight": (
        "absoluteDayBefore",
        "absoluteDayAfter",
        "dayOfWeekBefore",
        "dayOfWeekAfter",
        "countdownBefore",
        "countdownAfter",
    ),
    "cycle-wrap": (
        "absoluteDayBefore",
        "absoluteDayAfter",
        "dayOfWeekBefore",
        "dayOfWeekAfter",
    ),
    "sermon-day": ("absoluteDay", "dayOfWeek"),
    "after-sermon-day": (
        "absoluteDayBefore",
        "absoluteDayAfter",
        "dayOfWeekBefore",
        "dayOfWeekAfter",
        "countdownAfter",
    ),
}

DATE_CHECK_OPTIONAL_STRING_FIELDS = (
    "target",
    "week",
    "countdown",
    "text",
    "textKey",
    "language",
    "visible",
    "nonSermonCountdownShown",
)


def weekday_from_absolute_day(absolute_day, week):
    """EnvironmentData.CurrentDayNumber == ((absoluteDay - 1) % week) + 1."""
    return ((absolute_day - 1) % week) + 1


def _date_read_int(observations, path, label, failures):
    if not has_path(observations, path):
        failures.append(f"{label} observation {path!r} was not captured")
        return None
    value = get_path(observations, path)
    if not is_int(value):
        failures.append(
            f"{label} {path} = {value!r} must be a strict integer "
            "(floats and booleans do not qualify)"
        )
        return None
    return value


def _date_check_absolute_day(absolute, path, failures):
    if absolute is None:
        return
    if absolute <= 0:
        failures.append(
            f"{path} = {absolute!r} must be a strict positive absolute game day; "
            "EnvironmentData.Day starts at 1 (the 1..6 range is CurrentDayNumber, "
            "which is only the weekday)"
        )


def _date_check_weekday(path, value, week, failures):
    if value is None:
        return
    if not (1 <= value <= week):
        failures.append(
            f"{path} = {value!r} must be a weekday within 1..{week} "
            "(EnvironmentData.CurrentDayNumber)"
        )


def _date_check_weekday_matches_absolute(
    absolute, absolute_path, weekday, weekday_path, week, failures
):
    if absolute is None or weekday is None or absolute <= 0:
        return
    expected = weekday_from_absolute_day(absolute, week)
    if weekday != expected:
        failures.append(
            f"{weekday_path} = {weekday!r} does not match {absolute_path} = {absolute!r}: "
            f"(({absolute} - 1) % {week}) + 1 = {expected}"
        )


def date_delta(target, current, week):
    """Countdown days to the sermon weekday, wrapping inside the six-day cycle."""
    return (target - current + week) % week


def _date_check_countdown_equals(observations, path, label, expected, failures):
    shown = _date_read_int(observations, path, label, failures)
    if shown is not None and shown != expected:
        failures.append(
            f"{path} = {shown!r} != the mathematical delta {expected} computed from the "
            "observed sermon weekday"
        )


def _date_check_text(observations, final_text, spec, delta, failures):
    """The displayed sentence must correspond to the delta derived from observed T.

    delta == 1 uses the parameterless one-day sentence; any other shown delta uses
    the {days} sentence rendered with that delta. Only zh-CN is asserted here.
    """
    text_path = spec.get("text", "hud.text")
    key_path = spec.get("textKey", "hud.textKey")
    language = spec.get("language", "zh-CN")
    if not has_path(observations, text_path):
        failures.append(f"countdown text observation {text_path!r} was not captured")
        return
    text = get_path(observations, text_path)
    if delta == 1:
        expected_key = "gksr.hud.sermonCountdown.one"
        expected = final_text.get(expected_key, {}).get(language)
    else:
        expected_key = "gksr.hud.sermonCountdown.other"
        template = final_text.get(expected_key, {}).get(language)
        expected = None if template is None else template.replace("{days}", str(delta))
    if expected is None:
        failures.append(
            f"manifest finalText has no {language} sentence for {expected_key!r}"
        )
        return
    if has_path(observations, key_path):
        key = get_path(observations, key_path)
        if key != expected_key:
            failures.append(
                f"{key_path} = {key!r} must be {expected_key!r} for a shown delta of {delta}"
            )
    if text != expected:
        failures.append(
            f"{text_path} = {text!r}, expected the complete {language} sentence {expected!r} "
            f"for delta {delta}"
        )


def evaluate_date_check(condition, observations, final_text):
    """Evaluate one dedicated dateCheck helper condition.

    Returns a list of failure messages (empty when the check passes). Every
    quantity is derived from the observed target T and the observed six-day week,
    never from a literal baked into the manifest.
    """
    spec = condition["dateCheck"]
    mode = spec["mode"]
    failures = []

    week_path = spec.get("week", "save.daysInWeek")
    week = _date_read_int(observations, week_path, "daysInWeek", failures)
    if week is not None and week < 2:
        failures.append(f"{week_path} = {week!r} must be at least 2")

    target_path = spec.get("target", "save.sermonWeekday")
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
        abs_path = spec["absoluteDay"]
        dow_path = spec["dayOfWeek"]
        absolute = _date_read_int(observations, abs_path, "absoluteDay", failures)
        _date_check_absolute_day(absolute, abs_path, failures)
        dow = _date_read_int(observations, dow_path, "dayOfWeek", failures)
        if week is not None:
            _date_check_weekday(dow_path, dow, week, failures)
            _date_check_weekday_matches_absolute(
                absolute, abs_path, dow, dow_path, week, failures
            )
        if week is None or target is None or absolute is None or dow is None:
            return failures
        if sermon_day:
            if dow != target:
                failures.append(
                    f"sermon-day requires {dow_path} = {dow!r} to equal the observed "
                    f"sermon weekday {target_path} = {target!r}"
                )
            flag_path = spec.get("nonSermonCountdownShown", "hud.nonSermonCountdownShown")
            if not has_path(observations, flag_path):
                failures.append(
                    f"sermon-day requires {flag_path!r} to prove the non-sermon "
                    "countdown is not shown"
                )
            elif get_path(observations, flag_path) is not False:
                failures.append(
                    f"{flag_path} = {get_path(observations, flag_path)!r} must be false on the "
                    "sermon day"
                )
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
            observations, spec["countdown"], "countdownDays", delta, failures
        )
        return failures

    # same-day / midnight / cycle-wrap all compare a before/after pair.
    before_abs_path = spec["absoluteDayBefore"]
    after_abs_path = spec["absoluteDayAfter"]
    before_dow_path = spec["dayOfWeekBefore"]
    after_dow_path = spec["dayOfWeekAfter"]
    before_abs = _date_read_int(observations, before_abs_path, "absoluteDayBefore", failures)
    after_abs = _date_read_int(observations, after_abs_path, "absoluteDayAfter", failures)
    _date_check_absolute_day(before_abs, before_abs_path, failures)
    _date_check_absolute_day(after_abs, after_abs_path, failures)
    before_dow = _date_read_int(observations, before_dow_path, "dayOfWeekBefore", failures)
    after_dow = _date_read_int(observations, after_dow_path, "dayOfWeekAfter", failures)
    if week is not None:
        _date_check_weekday(before_dow_path, before_dow, week, failures)
        _date_check_weekday(after_dow_path, after_dow, week, failures)
        _date_check_weekday_matches_absolute(
            before_abs, before_abs_path, before_dow, before_dow_path, week, failures
        )
        _date_check_weekday_matches_absolute(
            after_abs, after_abs_path, after_dow, after_dow_path, week, failures
        )
    if week is None or target is None or None in (before_abs, after_abs, before_dow, after_dow):
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
            observations, spec["countdownBefore"], "countdown.beforeDays", before_delta, failures
        )
        _date_check_countdown_equals(
            observations, spec["countdownAfter"], "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, final_text, spec, after_delta, failures)
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
            observations, spec["countdownAfter"], "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, final_text, spec, after_delta, failures)
        return failures

    if mode == "midnight":
        if not (1 <= before_dow <= week - 1):
            failures.append(
                f"midnight requires the prior weekday in 1..{week - 1}; "
                f"{before_dow_path} = {before_dow!r}"
            )
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
            observations, spec["countdownBefore"], "countdown.beforeDays", before_delta, failures
        )
        _date_check_countdown_equals(
            observations, spec["countdownAfter"], "countdown.afterDays", after_delta, failures
        )
        _date_check_text(observations, final_text, spec, after_delta, failures)
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
        flag_path = spec.get("nonSermonCountdownShown", "hud.nonSermonCountdownShown")
        if not has_path(observations, flag_path):
            failures.append(
                "cycle-wrap with afterDelta 0 requires "
                f"{flag_path!r} to prove no non-sermon countdown is shown"
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
    visible_path = spec.get("visible", "hud.visible")
    if not has_path(observations, visible_path):
        failures.append(f"cycle-wrap requires {visible_path!r} to prove the countdown is visible")
    elif get_path(observations, visible_path) is not True:
        failures.append(
            f"{visible_path} = {get_path(observations, visible_path)!r} must be true when a "
            "non-sermon countdown is shown"
        )
    _date_check_countdown_equals(
        observations,
        spec.get("countdown", "hud.countdownDays"),
        "countdownDays",
        after_delta,
        failures,
    )
    _date_check_text(observations, final_text, spec, after_delta, failures)
    return failures


def evaluate_expectation(condition, observations, final_text, environment):
    if "dateCheck" in condition:
        failures = evaluate_date_check(condition, observations, final_text)
        if failures:
            return False, "; ".join(failures)
        return True, ""

    obs_path = condition["observation"]
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
                f"{obs_path} = {value!r} must equal environment {env_path} = "
                f"{env_value!r} (strict JSON type match)"
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
        other = condition["equalsObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if json_type_kind(value) != json_type_kind(other_value) or value != other_value:
            return False, f"{obs_path} = {value!r} != {other} = {other_value!r} (strict JSON type match)"

    if "notEqualsObservation" in condition:
        other = condition["notEqualsObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if json_type_kind(value) == json_type_kind(other_value) and value == other_value:
            return False, f"{obs_path} = {value!r} unexpectedly equals {other}"

    if "greaterThanObservation" in condition:
        other = condition["greaterThanObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if not (is_number(value) and is_number(other_value) and value > other_value):
            return False, f"{obs_path} = {value!r} is not greater than {other} = {other_value!r}"

    if "notEquals" in condition:
        expected = condition["notEquals"]
        if json_type_kind(value) == json_type_kind(expected) and value == expected:
            return False, (
                f"{obs_path} = {value!r} must differ from {expected!r} "
                f"(strict JSON type match)"
            )

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
        if not isinstance(bounds, dict) or not is_int(bounds.get("min")) or not is_int(bounds.get("max")):
            return False, f"malformed integerBetween bounds: {bounds!r}"
        low, high = bounds["min"], bounds["max"]
        if not is_int(value) or not (low <= value <= high):
            return False, (
                f"{obs_path} = {value!r} outside required integer range [{low}, {high}]; "
                "floats and booleans do not qualify"
            )

    if "numberRange" in condition:
        spec = condition["numberRange"]
        if not _finite_number(value):
            return False, (
                f"{obs_path} = {value!r} ({json_type_kind(value)}) is not a finite int/float; "
                "a numeric range cannot be satisfied by a non-number"
            )
        if "min" in spec and not (value >= spec["min"]):
            return False, f"{obs_path} = {value!r} is below min {spec['min']!r}"
        if "max" in spec and not (value <= spec["max"]):
            return False, f"{obs_path} = {value!r} exceeds max {spec['max']!r}"
        if "maxExclusive" in spec and not (value < spec["maxExclusive"]):
            return False, (
                f"{obs_path} = {value!r} must be strictly below maxExclusive "
                f"{spec['maxExclusive']!r}"
            )

    if "measuredRatio" in condition:
        spec = condition["measuredRatio"]
        num_path = spec["numerator"]
        den_path = spec["denominator"]
        if not has_path(observations, num_path):
            return False, f"measuredRatio numerator observation {num_path!r} was not captured"
        if not has_path(observations, den_path):
            return False, f"measuredRatio denominator observation {den_path!r} was not captured"
        numerator = get_path(observations, num_path)
        denominator = get_path(observations, den_path)
        if not _finite_number(numerator):
            return False, (
                f"{num_path} = {numerator!r} must be a finite int/float measurement"
            )
        if not _finite_number(denominator) or denominator <= 0:
            return False, (
                f"{den_path} = {denominator!r} must be a finite positive int/float measurement"
            )
        observed = numerator / denominator
        expected = RATIO_EXPECTED_NUMERATOR / RATIO_EXPECTED_DENOMINATOR
        tolerance = spec.get("tolerance", RATIO_TOLERANCE)
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

    if "finalTextKey" in condition or "finalTextKeyFromObservation" in condition:
        key_observation = condition.get("finalTextKeyFromObservation")
        if key_observation:
            if not has_path(observations, key_observation):
                return False, f"final text key observation {key_observation!r} was not captured"
            key = get_path(observations, key_observation)
        else:
            key = condition.get("finalTextKey")
        allowed = condition.get("finalTextKeysAnyOf")
        if allowed:
            if key not in allowed:
                return False, f"{obs_path} used key {key!r}, not in {allowed}"
        elif key != condition.get("finalTextKey"):
            return False, f"{obs_path} used key {key!r}, expected {condition.get('finalTextKey')!r}"
        if key not in final_text:
            return False, f"unknown final text key {key!r}"

        language = condition.get("language")
        if language and language not in final_text[key]:
            return False, f"language {language!r} has no final text for {key!r}"
        expected_text = final_text[key].get(language)
        if expected_text is None:
            return False, f"no expected text for {key!r} in language {language!r}"

        if "{days}" not in expected_text:
            if value != expected_text:
                return False, f"{obs_path} = {value!r}, expected exact {expected_text!r} (no interpolation permitted)"
        else:
            days_path = condition.get("daysObservation")
            if not days_path or not has_path(observations, days_path):
                return False, f"multi-day text requires {days_path!r} to interpolate {{days}}"
            days = get_path(observations, days_path)
            if not is_int(days) or days <= 1:
                return False, (
                    f"{days_path} = {days!r} must be a strict integer greater than 1; "
                    "floats and booleans do not qualify"
                )
            rendered = expected_text.replace("{days}", str(days))
            if value != rendered:
                return False, f"{obs_path} = {value!r}, expected rendered {rendered!r}"

    return True, ""


def evaluate_scenario(scenario, observations, final_text, record, environment):
    failures = []
    for condition in scenario["expect"]:
        ok, message = evaluate_expectation(condition, observations, final_text, environment)
        if not ok:
            failures.append(message)

    recorded = record.get("evidence", {})
    return failures


def check_manifest_only(manifest_path, output_path):
    """Structural-only manifest check for CI.

    This proves the manifest is well-formed and usable by the evidence
    validator. It NEVER asserts that any game scenario passed and is not an
    E2E verdict; the artifact records assertion="structure" explicitly.
    """
    ticket = ""
    try:
        manifest_probe = load_json(manifest_path)
        ticket = trusted_ticket_from_manifest(manifest_probe)
    except (Fail, OSError):
        ticket = ""
    artifact = {
        "artifactVersion": 1,
        "kind": result_kind(ticket, "manifest-check-result") if ticket
        else "gksa-manifest-check-error-result",
        "assertion": "structure-only",
        "e2eVerdict": None,
        "ok": False,
        "manifest": str(manifest_path),
        "manifestSha256": None,
        "error": None,
        "scenarioCount": 0,
        "scenarioIds": [],
        "note": (
            "Structural validation only. This artifact does not represent real "
            "game execution or business acceptance; only a complete capture "
            "validated by the evidence mode can do that."
        ),
    }

    def emit():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")

    try:
        manifest = load_json(manifest_path)
        scenarios = validate_manifest(manifest)
    except Fail as exc:
        artifact["error"] = str(exc)
        emit()
        sys.stderr.write(f"FAIL [manifest] {exc}\n")
        sys.stderr.write("RESULT: FAIL (manifest structure)\n")
        return 2
    except OSError as exc:
        artifact["error"] = str(exc)
        emit()
        sys.stderr.write(f"FAIL [manifest] {exc}\n")
        sys.stderr.write("RESULT: FAIL (manifest structure)\n")
        return 2
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Defensive net: malformed JSON shapes must fail closed with a
        # deterministic artifact, never escape as an uncaught interpreter error.
        message = f"malformed manifest structure: {type(exc).__name__}: {exc}"
        artifact["error"] = message
        emit()
        sys.stderr.write(f"FAIL [manifest] {message}\n")
        sys.stderr.write("RESULT: FAIL (manifest structure)\n")
        return 2

    artifact["ok"] = True
    artifact["manifestSha256"] = sha256_file(manifest_path)
    artifact["scenarioCount"] = len(scenarios)
    artifact["scenarioIds"] = [s["id"] for s in scenarios]
    artifact["ticket"] = manifest.get("ticket", "")
    emit()
    sys.stderr.write(
        f"RESULT: PASS (manifest structure only; {len(scenarios)} scenarios declared; "
        "no game execution asserted)\n"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a GKSA-10..GKSA-15 E2E capture.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--check-manifest",
        action="store_true",
        help=(
            "Structural-only manifest check for CI. Asserts schema validity and "
            "emits a structure artifact; NEVER asserts game execution or E2E pass. "
            "Requires --output and rejects --capture."
        ),
    )
    parser.add_argument("--capture")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    output_path = Path(args.output)

    if args.check_manifest:
        if args.capture:
            sys.stderr.write(
                "FAIL [cli] --check-manifest proves manifest structure only and must "
                "not be combined with --capture; use the evidence mode for a capture.\n"
            )
            return 2
        return check_manifest_only(manifest_path, output_path)

    if not args.capture:
        sys.stderr.write(
            "FAIL [cli] evidence mode requires --capture; use --check-manifest for a "
            "structural-only check.\n"
        )
        return 2

    capture_path = Path(args.capture)

    def fail_early(stage, message, code=2, ticket=""):
        """Always emit a deterministic artifact, even when the run fails closed
        before any scenario can be evaluated. The artifact kind derives from the
        validated ticket; a generic error kind is used when the manifest ticket
        cannot be trusted."""
        artifact_kind = (
            result_kind(ticket, "validation-result") if ticket
            else "gksa-validation-error-result"
        )
        artifact = {
            "artifactVersion": 1,
            "kind": artifact_kind,
            "ok": False,
            "manifest": str(manifest_path),
            "capture": str(capture_path),
            "captureSha256": None,
            "ticket": ticket,
            "capturedAt": "",
            "scenarios": [],
            "summary": {"total": 0, "passed": 0, "failed": 0, "unexecuted": 0},
            "unexpectedScenarioRecords": [],
            "error": {"stage": stage, "message": message},
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        sys.stderr.write(f"FAIL [{stage}] {message}\n")
        return code

    trusted_ticket = ""
    try:
        manifest = load_json(manifest_path)
    except Fail as exc:
        return fail_early("manifest", str(exc))
    except OSError as exc:
        return fail_early("manifest", str(exc))
    trusted_ticket = trusted_ticket_from_manifest(manifest)
    try:
        scenarios = validate_manifest(manifest)
    except Fail as exc:
        return fail_early("manifest", str(exc), ticket=trusted_ticket)
    except OSError as exc:
        return fail_early("manifest", str(exc), ticket=trusted_ticket)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return fail_early(
            "manifest",
            f"malformed manifest structure: {type(exc).__name__}: {exc}",
            ticket=trusted_ticket,
        )

    validation = manifest.get("validation", {})
    require_load_log = bool(validation.get("requireLoadEvidenceLog", False))
    final_text = manifest.get("finalText", {})

    try:
        capture = load_json(capture_path)
        validate_capture_shape(capture, manifest)
        check_language_observations(capture, manifest)
        env = capture["environment"]
        evidence_files = validate_environment(env, manifest, capture_path.parent)
        validated_evidence = validate_evidence_files(env, evidence_files, capture_path.parent)
    except Fail as exc:
        return fail_early("capture", str(exc), ticket=trusted_ticket)
    except OSError as exc:
        return fail_early("capture", str(exc), ticket=trusted_ticket)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Defensive net: malformed capture shapes must fail closed with a
        # deterministic artifact, never escape as an uncaught interpreter error.
        return fail_early(
            "capture",
            f"malformed capture structure: {type(exc).__name__}: {exc}",
            ticket=trusted_ticket,
        )

    results = []
    executed = {}
    for record in capture["results"]:
        # Shape validation already rejected duplicates and non-string ids, so this
        # mapping is unambiguous; keep the guard defensive anyway.
        if isinstance(record, dict) and isinstance(record.get("scenarioId"), str):
            executed[record["scenarioId"]] = record

    if require_load_log and not any("log" in key.lower() for key in evidence_files):
        return fail_early(
            "capture",
            "requireLoadEvidenceLog is set but no log-like evidence file is declared",
            ticket=trusted_ticket,
        )

    any_failed = False
    for scenario in scenarios:
        sid = scenario["id"]
        record = executed.get(sid)
        entry = {
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
            results.append(entry)
            continue

        entry["executed"] = True
        entry["status"] = record.get("status")
        entry["notes"] = record.get("notes", "")

        scenario_evidence = []
        recorded_evidence = record.get("evidence", {})
        for required in ("screenshot", "log"):
            if not scenario["evidence"].get(required):
                continue
            paths = recorded_evidence.get(required)
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not paths:
                entry.setdefault("failures", []).append(
                    f"required {required} evidence not recorded for the scenario"
                )

        for category, paths in recorded_evidence.items():
            if isinstance(paths, str):
                paths = [paths]
            for name in paths:
                if name not in evidence_files:
                    entry.setdefault("failures", []).append(
                        f"recorded evidence {name!r} is not declared in environment.evidenceFiles"
                    )
                    continue
                if name not in validated_evidence:
                    entry.setdefault("failures", []).append(
                        f"recorded evidence {name!r} was not verified"
                    )
                    continue
                scenario_evidence.append((category, name))
        entry["evidence"] = [
            {"category": category, "file": name} for category, name in scenario_evidence
        ]

        if scenario["evidence"].get("measurement") or record.get("measurement", False):
            ok_measure, message = evaluate_measurement(
                scenario, capture["observations"].get(sid, {}), record, validated_evidence
            )
            if not ok_measure:
                entry.setdefault("failures", []).append(message)

        observations = capture["observations"].get(sid)
        if not isinstance(observations, dict) or not observations:
            entry.setdefault("failures", []).append(
                f"no observations recorded for scenario {sid}"
            )
        else:
            entry["observations"] = observations
            failures = evaluate_scenario(
                scenario, observations, final_text, record, env
            )
            if failures:
                entry.setdefault("failures", []).extend(failures)

        if entry.get("failures"):
            entry["status"] = "failed"
            any_failed = True
        elif entry.get("status") != "passed":
            entry["status"] = "failed"
            entry.setdefault("failures", []).append(
                f"scenario status is {entry.get('status')!r}, not 'passed'; unexecuted or skipped scenarios cannot pass"
            )
            any_failed = True
        else:
            entry["failures"] = []

        results.append(entry)

    extras = sorted(set(executed) - {s["id"] for s in scenarios})
    if extras:
        any_failed = True

    ok = not any_failed
    artifact = {
        "artifactVersion": 1,
        "kind": result_kind(trusted_ticket, "validation-result"),
        "ok": ok,
        "manifest": str(manifest_path),
        "capture": str(capture_path),
        "captureSha256": sha256_file(capture_path),
        "ticket": trusted_ticket,
        "capturedAt": env.get("capturedAt", ""),
        "scenarios": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "passed"),
            "failed": sum(1 for r in results if r["status"] != "passed"),
            "unexecuted": sum(1 for r in results if not r["executed"]),
        },
        "unexpectedScenarioRecords": extras,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    for entry in results:
        if entry["status"] != "passed":
            for reason in entry.get("failures", []):
                sys.stderr.write(f"FAIL [{entry['scenarioId']}] {reason}\n")
    if extras:
        sys.stderr.write(f"FAIL [capture] unexpected scenario records: {extras}\n")
    if not ok:
        sys.stderr.write("RESULT: FAIL (fail-closed)\n")
        return 1

    sys.stderr.write(
        f"RESULT: PASS ({artifact['summary']['passed']}/{artifact['summary']['total']} scenarios)\n"
    )
    return 0


def evaluate_measurement(scenario, observations, record, validated_evidence):
    """A measurement-backed scenario must carry a concrete measurement or an
    explicit operator observation, plus at least one verified screenshot path.
    Logs alone never qualify."""
    recorded = record.get("evidence", {})
    screenshots = recorded.get("screenshot")
    if isinstance(screenshots, str):
        screenshots = [screenshots]
    if not isinstance(screenshots, list) or not screenshots:
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
    has_values = (
        isinstance(values, dict)
        and bool(values)
        and all(is_number(v) for v in values.values())
    )
    observation = measurement.get("operatorObservation")
    has_observation = isinstance(observation, str) and bool(observation.strip())
    if not has_values and not has_observation:
        return False, (
            "measurement must include a non-empty numeric values object or a "
            "non-empty operatorObservation string"
        )
    return True, ""


if __name__ == "__main__":
    raise SystemExit(main())
