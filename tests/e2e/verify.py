#!/usr/bin/env python3
"""GKSA-10 E2E evidence validator.

Validates a machine-readable capture of a real in-game observation run against
the GKSA-10 scenario manifest. It is an evidence checker, not a game simulator:
it never invents observations, never grades an unexecuted scenario as passed,
and never treats logs alone as proof of a visual/layout outcome.

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
import re
import sys
from datetime import datetime
from pathlib import Path

SUPPORTED_CAPTURE_VERSIONS = (1,)
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


def is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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


def validate_manifest(manifest):
    """Strictly check the manifest shape and return the scenario list.

    This is structural validation only: it proves the manifest can be used by
    the full evidence validator. It never asserts that any game scenario passed.
    """
    if manifest.get("kind") != "gksa10-e2e-manifest":
        raise Fail(f"unsupported manifest kind: {manifest.get('kind')!r}")
    if not is_positive_int(manifest.get("manifestVersion")):
        raise Fail("manifestVersion must be a positive integer")
    final_text = manifest.get("finalText")
    if not isinstance(final_text, dict) or not final_text:
        raise Fail("manifest must declare finalText for every required key")
    for key, translations in final_text.items():
        if not isinstance(translations, dict) or not translations:
            raise Fail(f"finalText[{key!r}] must map languages to complete sentences")
        for language, text in translations.items():
            if not isinstance(text, str) or not text.strip():
                raise Fail(f"finalText[{key!r}][{language!r}] must be a non-empty sentence")
        if "{" in translations.get("en", ""):
            en_params = set(re.findall(r"\{(\w+)\}", translations["en"]))
            zh_params = set(re.findall(r"\{(\w+)\}", translations.get("zh-CN", "")))
            if en_params != zh_params:
                raise Fail(
                    f"finalText[{key!r}] placeholder mismatch between en and zh-CN: "
                    f"{sorted(en_params)} vs {sorted(zh_params)}"
                )

    language_mapping = manifest.get("languageMapping")
    if not isinstance(language_mapping, dict) or not language_mapping:
        raise Fail("manifest must declare languageMapping between catalog labels and raw game ids")

    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise Fail("manifest must declare a non-empty scenarios array")
    seen = set()
    for scenario in scenarios:
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
            if not isinstance(condition, dict) or not condition.get("observation"):
                raise Fail(f"scenario {sid} has a malformed expectation")
            if "equals" not in condition and not (
                "integerGreaterThan" in condition
                or "greaterThanObservation" in condition
                or "notEquals" in condition
                or "notEqualsObservation" in condition
                or "equalsObservation" in condition
                or "present" in condition
                or "finalTextKey" in condition
                or "finalTextKeyFromObservation" in condition
            ):
                raise Fail(f"scenario {sid} expectation without a check: {condition!r}")
            if ("finalTextKey" in condition or "finalTextKeyFromObservation" in condition):
                language = condition.get("language")
                if language not in ("zh-CN", "en"):
                    raise Fail(
                        f"scenario {sid} references catalog language {language!r}; "
                        "finalText languages are catalog labels (zh-CN, en)"
                    )
            if condition["observation"] == "language.active":
                label_map = language_mapping["catalogLabelToRawGameLanguageId"]
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
        raise Fail("capture root must be a JSON object")
    if capture.get("kind") != "gksa10-e2e-capture":
        raise Fail(f"unsupported capture kind: {capture.get('kind')!r}")
    version = capture.get("captureVersion")
    supported = tuple(manifest.get("validation", {}).get("supportedCaptureVersions", SUPPORTED_CAPTURE_VERSIONS))
    if version not in supported:
        raise Fail(f"unsupported captureVersion {version!r}; supported: {list(supported)}")
    if not isinstance(capture.get("environment"), dict):
        raise Fail("capture.environment must be an object")
    if not isinstance(capture.get("observations"), dict) or not capture["observations"]:
        raise Fail("capture.observations must be a non-empty object keyed by scenario id")
    if not isinstance(capture.get("results"), list):
        raise Fail("capture.results must be an array of executed scenario records")
    for record in capture["results"]:
        if not isinstance(record, dict):
            raise Fail(f"each capture result must be an object, got {record!r}")
        if not isinstance(record.get("scenarioId"), str) or not record["scenarioId"].strip():
            raise Fail("each capture result must carry a non-empty scenarioId")
        if not isinstance(record.get("status"), str):
            raise Fail(f"capture result {record.get('scenarioId')!r} must carry a status string")
        evidence = record.get("evidence", {})
        if not isinstance(evidence, dict):
            raise Fail(
                f"capture result {record['scenarioId']!r} evidence must be an object "
                "mapping the evidence category (screenshot/log/video) to a path"
            )
        for category, paths in evidence.items():
            if not isinstance(category, str) or not category.strip():
                raise Fail(f"capture result {record['scenarioId']!r} has a malformed evidence category")
            if isinstance(paths, str):
                paths = [paths]
            if not isinstance(paths, list) or not all(
                isinstance(p, str) and p.strip() for p in paths
            ):
                raise Fail(
                    f"capture result {record['scenarioId']!r} category {category!r} "
                    "must map to a path or a list of paths"
                )


def validate_environment(env, manifest, capture_dir):
    validation = manifest.get("validation", {})
    for spec in validation.get("requiredEnvironmentPaths", []):
        path = spec["path"]
        if not has_path(env, path):
            raise Fail(f"environment is missing required field {path!r}")
        value = get_path(env, path)
        check_type(value, spec["type"])
        if "equals" in spec and value != spec["equals"]:
            raise Fail(
                f"environment field {path!r} must equal {spec['equals']!r}, got {value!r}"
            )

    for group in validation.get("buildPluginHashConsistency", []):
        values = {}
        for path in group:
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

def evaluate_expectation(condition, observations, final_text):
    obs_path = condition["observation"]
    if not has_path(observations, obs_path):
        return False, f"observation {obs_path!r} was not captured"

    value = get_path(observations, obs_path)

    if "equals" in condition:
        if isinstance(condition["equals"], bool) and isinstance(value, bool):
            if value is not condition["equals"]:
                return False, f"{obs_path} = {value!r}, expected {condition['equals']!r}"
        elif value != condition["equals"]:
            return False, f"{obs_path} = {value!r}, expected {condition['equals']!r}"

    if "equalsObservation" in condition:
        other = condition["equalsObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if value != other_value:
            return False, f"{obs_path} = {value!r} != {other} = {other_value!r}"

    if "notEqualsObservation" in condition:
        other = condition["notEqualsObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if value == other_value:
            return False, f"{obs_path} = {value!r} unexpectedly equals {other}"

    if "greaterThanObservation" in condition:
        other = condition["greaterThanObservation"]
        if not has_path(observations, other):
            return False, f"comparison observation {other!r} was not captured"
        other_value = get_path(observations, other)
        if not (is_number(value) and is_number(other_value) and value > other_value):
            return False, f"{obs_path} = {value!r} is not greater than {other} = {other_value!r}"

    if "notEquals" in condition:
        if value == condition["notEquals"]:
            return False, f"{obs_path} = {value!r} must differ from {condition['notEquals']!r}"

    if "integerGreaterThan" in condition:
        if not is_number(value) or not value > condition["integerGreaterThan"]:
            return False, f"{obs_path} = {value!r} is not an integer greater than {condition['integerGreaterThan']}"

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
            if not is_number(days) or not float(days).is_integer() or days <= 1:
                return False, f"{days_path} = {days!r} must be an integer greater than 1"
            rendered = expected_text.replace("{days}", str(int(days)))
            if value != rendered:
                return False, f"{obs_path} = {value!r}, expected rendered {rendered!r}"

    return True, ""


def evaluate_scenario(scenario, observations, final_text, record):
    failures = []
    for condition in scenario["expect"]:
        ok, message = evaluate_expectation(condition, observations, final_text)
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
    artifact = {
        "artifactVersion": 1,
        "kind": "gksa10-manifest-check-result",
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
    parser = argparse.ArgumentParser(description="Validate a GKSA-10 E2E capture.")
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

    def fail_early(stage, message, code=2):
        """Always emit a deterministic artifact, even when the run fails closed
        before any scenario can be evaluated."""
        artifact = {
            "artifactVersion": 1,
            "kind": "gksa10-validation-result",
            "ok": False,
            "manifest": str(manifest_path),
            "capture": str(capture_path),
            "captureSha256": None,
            "ticket": "",
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

    try:
        manifest = load_json(manifest_path)
        scenarios = validate_manifest(manifest)
    except Fail as exc:
        return fail_early("manifest", str(exc))
    except OSError as exc:
        return fail_early("manifest", str(exc))

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
        return fail_early("capture", str(exc))
    except OSError as exc:
        return fail_early("capture", str(exc))

    results = []
    executed = {}
    for record in capture["results"]:
        if isinstance(record, dict) and isinstance(record.get("scenarioId"), str):
            executed[record["scenarioId"]] = record

    if require_load_log and not any("log" in key.lower() for key in evidence_files):
        return fail_early(
            "capture",
            "requireLoadEvidenceLog is set but no log-like evidence file is declared",
        )

    any_failed = False
    for scenario in scenarios:
        sid = scenario["id"]
        record = executed.get(sid)
        entry = {
            "scenarioId": sid,
            "title": scenario.get("title", ""),
            "executed": record is None,
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
            if scenario["evidence"].get(required) and required not in recorded_evidence:
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
                scenario, capture["observations"].get(sid, {}), record
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
                scenario, observations, final_text, record
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
        "kind": "gksa10-validation-result",
        "ok": ok,
        "manifest": str(manifest_path),
        "capture": str(capture_path),
        "captureSha256": sha256_file(capture_path),
        "ticket": manifest.get("ticket", ""),
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


def evaluate_measurement(scenario, observations, record):
    """A measurement-backed scenario must carry a concrete measurement or an
    explicit operator observation, plus a screenshot. Logs alone never qualify."""
    recorded = record.get("evidence", {})
    if "screenshot" not in recorded:
        return False, "measurement scenario requires a screenshot evidence category"
    measurement = record.get("measurement")
    if measurement is False or measurement is None:
        return False, "measurement evidence required but not recorded"
    if not isinstance(measurement, dict) or not measurement:
        return False, "measurement object is missing or empty"
    if not measurement.get("values") and not measurement.get("operatorObservation"):
        return False, "measurement must include measured values or an operator observation"
    return True, ""


if __name__ == "__main__":
    raise SystemExit(main())
