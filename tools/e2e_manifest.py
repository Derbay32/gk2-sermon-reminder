#!/usr/bin/env python3
"""Shared GKSA-23 v2 E2E manifest loader.

The source guard (``tools/source_check.py``) and the E2E verifier
(``tests/e2e/verify.py``) both load the aggregated index through
:func:`load_manifest`, so schema, containment, identity and fingerprint rules can
never diverge between the two tools. The loader owns:

  * strict JSON decoding (duplicate keys, ``NaN``/``Infinity`` and overflow are
    rejected) for the index, the profiles file and every scenario document;
  * contained, non-symlink path loading for the profiles file and the scenario
    root;
  * complete recursive discovery of every ``*.json`` scenario document under the
    scenario root;
  * identity/reference checks (index kind/version/id, profiles map shape,
    scenario document kind/version, non-empty collections, globally unique
    scenario ids, resolvable ``profileId`` references, non-empty
    ``requirements``);
  * the profile/scenario structural grammar (``finalText`` / ``languageMapping``
    shape, the fixed ``dateCheck`` mode/field tables, the bounded
    ``numberRange`` and ``measuredRatio`` declarations, and every scenario's
    ``expect``/``evidence`` objects), so the returned bundle is already fully
    validated for both the source guard and the evidence verifier.

Every structural rejection raises :class:`ManifestInputError`, so callers (the
source guard and the E2E verifier) map one exception type onto their existing
controlled report and never need a second, divergent validator.
  * the canonical manifest fingerprint and the report ``scope``.

The bundle exposes the parsed index, the profile map, the full scenario list,
the referenced file map, the manifest id, the canonical sha256 and the scope.
Only the Python 3 standard library is used.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

import json_data
from json_data import JsonObject, JsonValue

INDEX_KIND = "gksr-e2e-manifest"
SCENARIOS_KIND = "gksr-e2e-scenarios"
MANIFEST_VERSION = 2
SCHEMA_VERSION = 2

# Fixed native icon aspect 20:18 and its small comparison tolerance for the one
# bounded measuredRatio check. No other ratio or general evaluation is offered.
RATIO_EXPECTED_NUMERATOR = 20
RATIO_EXPECTED_DENOMINATOR = 18
RATIO_TOLERANCE = 0.001
RATIO_TOLERANCE_MAX = 0.001

# One dedicated, bounded dateCheck helper with a fixed set of modes, deliberately
# not a general expression DSL. Ground truth (decompiled EnvironmentData.cs,
# build 25509347):
#   EnvironmentData.CurrentDayNumber -> GetDayNumberFromDay(day) = day % 6,
#                                     remainder 0 mapped to 6 (WEEKDAY ONLY)
#   DAYS_IN_WEEK = 6
#   dayOfWeek == ((absoluteDay - 1) % week) + 1
# Calendar scenarios must never invent a sermon weekday literal: day_wrath is a
# single runtime definition captured once as environment.game.sermonWeekday and
# compared with save.sermonWeekday.
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


class ManifestInputError(ValueError):
    """The supplied v2 manifest bundle is unreadable or structurally invalid."""


@dataclass(frozen=True)
class ManifestBundle:
    """A fully validated v2 manifest index and everything it references."""

    index: JsonObject
    index_path: Path
    profiles: JsonObject
    scenarios: tuple[JsonObject, ...]
    files: dict[str, JsonValue]
    manifest_id: str
    sha256: str
    scope: JsonObject

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))

    def profile_of(self, scenario: JsonObject) -> JsonObject:
        profile_id = scenario["profileId"]
        if not isinstance(profile_id, str) or profile_id not in self.profiles:
            raise ManifestInputError(f"scenario references unresolved profileId {profile_id!r}")
        return json_data.as_object(self.profiles[profile_id], f"profile {profile_id!r}")

    def scenario_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for scenario in self.scenarios:
            scenario_id = scenario["id"]
            if isinstance(scenario_id, str):
                ids.append(scenario_id)
        return tuple(ids)


def _require_nonempty_string(obj: JsonObject, key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestInputError(f"{label}.{key} must be a non-empty string")
    return value


def _contained_path(base: Path, rel: str, label: str) -> Path:
    """Resolve ``rel`` under ``base``, rejecting escapes and symlink traversal."""
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ManifestInputError(f"{label} path {rel!r} must be relative to the index")
    resolved = (base / candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise ManifestInputError(f"{label} path {rel!r} escapes the index directory")
    current = base
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ManifestInputError(f"{label} path {rel!r} traverses a symlink at {current}")
    return base / candidate


def _discover_scenario_files(root: Path) -> list[Path]:
    """Every regular ``*.json`` file under ``root``; symlinks are rejected."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in dirnames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                raise ManifestInputError(f"scenario root traverses the symlinked directory {candidate}")
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.suffix != ".json":
                continue
            if candidate.is_symlink():
                raise ManifestInputError(f"scenario file {candidate} is a symlink")
            if candidate.is_file():
                found.append(candidate)
    found.sort()
    return found


def _load_object(path: Path, label: str) -> JsonObject:
    try:
        value = json_data.load_json_strict(path)
    except json_data.JsonInputError as exc:
        raise ManifestInputError(str(exc)) from exc
    return json_data.as_object(value, label)


def _validate_profile_map(profiles: JsonObject) -> None:
    if not profiles:
        raise ManifestInputError("profiles file must declare a non-empty map of contexts")
    for profile_id, context in profiles.items():
        if not profile_id.strip():
            raise ManifestInputError("profiles file has an empty profile id")
        json_data.as_object(context, f"profile {profile_id!r}")


def _validate_scenario(scenario: JsonObject, profiles: JsonObject, rel: str, position: int) -> None:
    profile_id = scenario.get("profileId")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ManifestInputError(f"{rel} scenarios[{position}] must declare a non-empty profileId")
    if profile_id not in profiles:
        raise ManifestInputError(f"{rel} scenarios[{position}] references unknown profileId {profile_id!r}")
    requirements = scenario.get("requirements")
    if (
        not isinstance(requirements, list)
        or not requirements
        or not all(isinstance(item, str) and item.strip() for item in requirements)
    ):
        raise ManifestInputError(f"{rel} scenarios[{position}] must declare a non-empty requirements string array")


# --------------------------------------------------------------------------
# profile / scenario structural grammar
# --------------------------------------------------------------------------


def finite_number(value: object) -> TypeGuard[int | float]:
    """Bounded numeric guard: see :func:`json_data.is_finite_number`.

    A JSON integer too large to convert to a finite float is *non-finite* here,
    so a bounded numeric comparison can never be satisfied by a huge integer
    magnitude and can never crash on one.
    """
    return json_data.is_finite_number(value)


def _bounded_numeric(sid: str, label: str, value: JsonValue) -> None:
    """Validate one finite int/float bound; bools and non-numbers are rejected."""
    if not finite_number(value):
        raise ManifestInputError(
            f"scenario {sid} {label} must be a finite int/float (bool and non-numbers "
            f"are rejected); got {value!r} ({json_data.json_type_kind(value)})"
        )


def validate_date_check(sid: str, condition: JsonObject) -> None:
    """Structurally validate one bounded dateCheck helper condition.

    The helper is deliberately a fixed-mode calendar checker, not a general
    expression DSL: each mode has an exact required field set and no unknown
    fields are accepted.
    """
    spec = condition["dateCheck"]
    if not isinstance(spec, dict) or not spec:
        raise ManifestInputError(f"scenario {sid} dateCheck must be a non-empty object")
    mode = spec.get("mode")
    if mode not in date_check_modes:
        raise ManifestInputError(f"scenario {sid} dateCheck.mode {mode!r} must be one of {list(date_check_modes)}")
    required = (
        set(date_check_required_fields[mode]) | {"mode", "target", "week"} | set(DATE_CHECK_OPTIONAL_STRING_FIELDS)
    )
    unknown = sorted(set(spec) - required)
    if unknown:
        raise ManifestInputError(f"scenario {sid} dateCheck({mode}) has unknown fields: {unknown}")
    for field in date_check_required_fields[mode]:
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ManifestInputError(
                f"scenario {sid} dateCheck({mode}) requires a non-empty observation path for {field!r}"
            )
    for field in DATE_CHECK_OPTIONAL_STRING_FIELDS:
        if field in spec:
            value = spec[field]
            if not isinstance(value, str) or not value.strip():
                raise ManifestInputError(
                    f"scenario {sid} dateCheck({mode}) field {field!r} must be a non-empty observation path"
                )
    if "target" in spec and spec["target"] != "save.sermonWeekday":
        raise ManifestInputError(
            f"scenario {sid} dateCheck({mode}) must read the target from the observed "
            f"save.sermonWeekday (got {spec['target']!r}); day_wrath is a single runtime "
            "definition that scenarios must never redefine"
        )
    if "week" in spec and spec["week"] != "save.daysInWeek":
        raise ManifestInputError(
            f"scenario {sid} dateCheck({mode}) must read the week length from the "
            f"observed save.daysInWeek (got {spec['week']!r})"
        )
    if "language" in spec and spec["language"] not in ("zh-CN", "en"):
        raise ManifestInputError(
            f"scenario {sid} dateCheck({mode}) language {spec['language']!r} must be a catalog label (zh-CN or en)"
        )


def validate_number_range(sid: str, condition: JsonObject) -> None:
    """One bounded numeric check: inclusive min/max or exclusive maxExclusive.

    Deliberately narrow, not a general evaluation DSL. At least one bound is
    required; an unrecognized bound key is rejected rather than ignored.
    """
    spec = condition["numberRange"]
    if not isinstance(spec, dict) or not spec:
        raise ManifestInputError(f"scenario {sid} numberRange must be a non-empty object")
    allowed = {"min", "max", "maxExclusive"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ManifestInputError(f"scenario {sid} numberRange has unknown fields: {unknown}")
    has_min = "min" in spec
    has_max = "max" in spec
    has_max_exclusive = "maxExclusive" in spec
    if not (has_min or has_max or has_max_exclusive):
        raise ManifestInputError(f"scenario {sid} numberRange requires at least one bound (min, max or maxExclusive)")
    if has_max and has_max_exclusive:
        raise ManifestInputError(f"scenario {sid} numberRange must not declare both max and maxExclusive")
    for key in ("min", "max", "maxExclusive"):
        if key in spec:
            _bounded_numeric(sid, "numberRange." + key, spec[key])
    if has_min and (has_max or has_max_exclusive):
        upper_key = "max" if has_max else "maxExclusive"
        lower = spec["min"]
        upper = spec[upper_key]
        if finite_number(lower) and finite_number(upper) and lower > upper:
            raise ManifestInputError(f"scenario {sid} numberRange min {lower!r} exceeds upper bound {upper!r}")


def validate_measured_ratio(sid: str, condition: JsonObject) -> None:
    """One bounded ratio check: observed width/height against the fixed 20:18 icon.

    Requires raw width and height observations and an explicit finite, positive
    denominator; a pre-asserted ratio flag is not accepted.
    """
    spec = condition["measuredRatio"]
    if not isinstance(spec, dict) or not spec:
        raise ManifestInputError(f"scenario {sid} measuredRatio must be a non-empty object")
    allowed = {"numerator", "denominator", "tolerance"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ManifestInputError(f"scenario {sid} measuredRatio has unknown fields: {unknown}")
    for field in ("numerator", "denominator"):
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ManifestInputError(
                f"scenario {sid} measuredRatio requires the raw {field!r} observation path; got {value!r}"
            )
    tolerance = spec.get("tolerance", RATIO_TOLERANCE)
    if not finite_number(tolerance):
        raise ManifestInputError(
            f"scenario {sid} measuredRatio tolerance must be a finite int/float; "
            f"got {tolerance!r} ({json_data.json_type_kind(tolerance)})"
        )
    if tolerance < 0 or tolerance > RATIO_TOLERANCE_MAX:
        raise ManifestInputError(
            f"scenario {sid} measuredRatio tolerance {tolerance!r} must be within 0..{RATIO_TOLERANCE_MAX}"
        )


def profile_label_map(profile: JsonObject, label: str) -> JsonObject:
    """Return the validated catalog-label -> raw-game-id map for one profile."""
    language_mapping = profile.get("languageMapping")
    if not isinstance(language_mapping, dict) or not language_mapping:
        raise ManifestInputError(f"{label} must declare languageMapping between catalog labels and raw game ids")
    label_map = language_mapping.get("catalogLabelToRawGameLanguageId")
    if not isinstance(label_map, dict) or not label_map:
        raise ManifestInputError(f"{label}.languageMapping.catalogLabelToRawGameLanguageId must be a non-empty object")
    for catalog_label, raw_id in label_map.items():
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ManifestInputError(
                f"{label}.languageMapping entry {catalog_label!r} -> {raw_id!r} must map a "
                "string catalog label to a non-empty raw game language id"
            )
    return label_map


def validate_final_text(final_text: JsonValue, label: str) -> None:
    """Every finalText key must map catalog labels to complete sentences."""
    if not isinstance(final_text, dict) or not final_text:
        raise ManifestInputError(f"{label} must declare finalText for every required key")
    for key, translations in final_text.items():
        if not key.strip():
            raise ManifestInputError(f"{label} finalText key {key!r} must be a non-empty string")
        if not isinstance(translations, dict) or not translations:
            raise ManifestInputError(f"{label} finalText[{key!r}] must map languages to complete sentences")
        for language, text in translations.items():
            if not isinstance(text, str) or not text.strip():
                raise ManifestInputError(f"{label} finalText[{key!r}][{language!r}] must be a non-empty sentence")
        en_text = translations.get("en", "")
        if isinstance(en_text, str) and en_text:
            en_params = set(re.findall(r"\{(\w+)\}", en_text))
            zh_text = translations.get("zh-CN", "")
            zh_params = set(re.findall(r"\{(\w+)\}", zh_text)) if isinstance(zh_text, str) else set()
            if en_params and en_params != zh_params:
                raise ManifestInputError(
                    f"{label} finalText[{key!r}] placeholder mismatch between en and zh-CN: "
                    f"{sorted(en_params)} vs {sorted(zh_params)}"
                )


def validate_profiles(bundle: ManifestBundle) -> None:
    """Strictly check every profile context (finalText and language mapping).

    This is structural validation only: it proves the bundle can be used by the
    full evidence validator. It never asserts that any game scenario passed.
    """
    for profile_id in bundle.profile_ids():
        profile = json_data.as_object(bundle.profiles[profile_id], f"profile {profile_id!r}")
        validate_final_text(profile.get("finalText"), f"profile {profile_id!r}")
        profile_label_map(profile, f"profile {profile_id!r}")


def validate_scenarios(bundle: ManifestBundle) -> None:
    """Strictly check each scenario's expectation and evidence declaration."""
    for scenario in bundle.scenarios:
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ManifestInputError("scenario without a usable id")
        label = f"scenario {scenario_id}"
        profile_id = scenario.get("profileId")
        profile = bundle.profile_of(scenario)
        label_map = profile_label_map(profile, f"profile {profile_id!r}")
        expect = scenario.get("expect")
        if not isinstance(expect, list) or not expect:
            raise ManifestInputError(f"{label} has no expectations")
        for condition in expect:
            if not isinstance(condition, dict):
                raise ManifestInputError(f"{label} has a malformed expectation: {condition!r}")
            if "dateCheck" in condition:
                validate_date_check(scenario_id, condition)
                continue
            observation = condition.get("observation")
            if not isinstance(observation, str) or not observation.strip():
                raise ManifestInputError(f"{label} has a malformed expectation: {condition!r}")
            if "equalsEnvironment" in condition:
                env_target = condition["equalsEnvironment"]
                if not isinstance(env_target, str) or not env_target.strip():
                    raise ManifestInputError(
                        f"{label} equalsEnvironment must name a non-empty environment path: {condition!r}"
                    )
                if observation != "save.sermonWeekday":
                    raise ManifestInputError(
                        f"{label} equalsEnvironment is only valid on "
                        "save.sermonWeekday so the observed sermon weekday is compared "
                        "with the single runtime definition; got "
                        f"{observation!r}"
                    )
            if observation == "save.sermonWeekday" and "equalsEnvironment" not in condition:
                raise ManifestInputError(
                    f"{label} must compare save.sermonWeekday against the observed "
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
                raise ManifestInputError(f"{label} expectation without a check: {condition!r}")
            if "numberRange" in condition:
                validate_number_range(scenario_id, condition)
            if "measuredRatio" in condition:
                validate_measured_ratio(scenario_id, condition)
            if "finalTextKey" in condition and "finalTextKeyFromObservation" in condition:
                raise ManifestInputError(
                    f"{label} expectation must use either finalTextKey or finalTextKeyFromObservation, not both"
                )
            if "finalTextKey" in condition or "finalTextKeyFromObservation" in condition:
                language = condition.get("language")
                if language not in ("zh-CN", "en"):
                    raise ManifestInputError(
                        f"{label} references catalog language {language!r}; "
                        "finalText languages are catalog labels (zh-CN, en)"
                    )
            if observation == "language.active":
                raw_ids = {item for item in label_map.values() if isinstance(item, str)}
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
                        raise ManifestInputError(
                            f"{label} compares language.active against catalog-only "
                            f"label {value!r}; raw game language ids are required "
                            f"(known: {sorted(raw_ids)})"
                        )
        evidence = scenario.get("evidence")
        if not isinstance(evidence, dict):
            raise ManifestInputError(f"{label} must declare an evidence object")


def load_manifest(index_path: Path) -> ManifestBundle:
    """Load and fully validate the v2 manifest index at ``index_path``.

    The explicitly requested index path is rejected when it is itself a symlink
    *before* it is resolved or decoded: a link to a valid index is not the
    requested input. Referenced-path containment and symlink traversal are still
    checked during load. Every strict-JSON narrowing failure is reported through
    the single :class:`ManifestInputError` type, so callers never catch a second
    JSON exception class.
    """
    if index_path.is_symlink():
        raise ManifestInputError(f"manifest index {str(index_path)!r} must not be a symlink")
    try:
        return _load_manifest(index_path)
    except json_data.JsonInputError as exc:
        raise ManifestInputError(str(exc)) from exc


def _load_manifest(index_path: Path) -> ManifestBundle:
    base = index_path.resolve().parent
    index = _load_object(index_path, "manifest index")

    kind = index.get("kind")
    if kind != INDEX_KIND:
        raise ManifestInputError(f"unsupported manifest kind: {kind!r} (expected {INDEX_KIND!r})")
    version = index.get("manifestVersion")
    if not json_data.is_int(version) or version != MANIFEST_VERSION:
        raise ManifestInputError(f"manifestVersion must be the integer {MANIFEST_VERSION}; got {version!r}")
    manifest_id = _require_nonempty_string(index, "manifestId", "manifest index")
    profiles_rel = _require_nonempty_string(index, "profilesFile", "manifest index")
    scenario_root_rel = _require_nonempty_string(index, "scenarioRoot", "manifest index")

    profiles_path = _contained_path(base, profiles_rel, "profilesFile")
    if not profiles_path.is_file():
        raise ManifestInputError(f"profiles file not found: {profiles_rel!r}")
    profiles = _load_object(profiles_path, f"profiles file {profiles_rel!r}")
    _validate_profile_map(profiles)

    scenario_root = _contained_path(base, scenario_root_rel, "scenarioRoot")
    if not scenario_root.is_dir():
        raise ManifestInputError(f"scenario root not found: {scenario_root_rel!r}")
    scenario_paths = _discover_scenario_files(scenario_root)
    if not scenario_paths:
        raise ManifestInputError("scenario root contains no scenario JSON documents")

    files: dict[str, JsonValue] = {profiles_rel: profiles}
    scenarios: list[JsonObject] = []
    seen_ids: set[str] = set()
    for path in scenario_paths:
        rel = path.relative_to(base).as_posix()
        document = _load_object(path, f"scenario document {rel!r}")
        if document.get("kind") != SCENARIOS_KIND:
            raise ManifestInputError(
                f"scenario document {rel!r} has kind {document.get('kind')!r}; expected {SCENARIOS_KIND!r}"
            )
        schema_version = document.get("schemaVersion")
        if not json_data.is_int(schema_version) or schema_version != SCHEMA_VERSION:
            raise ManifestInputError(f"scenario document {rel!r} schemaVersion must be the integer {SCHEMA_VERSION}")
        collection = document.get("scenarios")
        if not isinstance(collection, list) or not collection:
            raise ManifestInputError(f"scenario document {rel!r} must declare a non-empty scenarios array")
        for position, entry in enumerate(collection):
            scenario = json_data.as_object(entry, f"{rel} scenarios[{position}]")
            scenario_id = scenario.get("id")
            if not isinstance(scenario_id, str) or not scenario_id.strip():
                raise ManifestInputError(f"{rel} scenarios[{position}] must declare a non-empty id")
            if scenario_id in seen_ids:
                raise ManifestInputError(f"duplicate scenario id {scenario_id!r}")
            seen_ids.add(scenario_id)
            _validate_scenario(scenario, profiles, rel, position)
            scenarios.append(scenario)
        files[rel] = document

    payload: JsonObject = {"index": index, "files": files}
    fingerprint = json_data.canonical_digest(payload)
    scope: JsonObject = {
        "files": json_data.json_string_array(sorted(files)),
        "scenarioIds": json_data.json_string_array(sorted(seen_ids)),
        "scenarioCount": len(seen_ids),
    }
    bundle = ManifestBundle(
        index=index,
        index_path=index_path,
        profiles=profiles,
        scenarios=tuple(scenarios),
        files=files,
        manifest_id=manifest_id,
        sha256=fingerprint,
        scope=scope,
    )
    # Structural grammar is enforced here so the returned bundle really is
    # fully validated for both the source guard and the evidence verifier.
    validate_profiles(bundle)
    validate_scenarios(bundle)
    return bundle
