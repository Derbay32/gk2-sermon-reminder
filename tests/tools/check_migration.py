#!/usr/bin/env python3
"""GKSA-23 migration traceability oracle (test code).

Freezes independent oracles from the six legacy ticket manifests at the base
commit, then checks a candidate ``tests/e2e/migration-map.json`` and the
migrated v2 manifest content against those frozen oracles.

The migration map is traceability only and is never a runtime protocol input.
This tool imports no production module: it reads the frozen JSON baseline and
the candidate files directly, and it never re-derives expected digests from the
candidate. Real game scenarios remain unexecuted; every report keeps
``e2eVerdict: null``.

Usage:
  python tests/tools/check_migration.py --repo-root . \
      --output artifacts/gksa23-tests/migration-check.json

  # freeze the committed oracle fixture from the base commit (test code only)
  python tests/tools/check_migration.py --freeze-baseline

  # run the disposable subprocess mutation suite for this oracle
  python tests/tools/check_migration.py --self-test --repo-root . \
      --output artifacts/gksa23-tests/migration-selftest.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeGuard

import protocol_support as ps

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE = SCRIPT_DIR / "fixtures" / "migration-baseline.json"
MIGRATION_MAP_REL = "tests/e2e/migration-map.json"
MANIFEST_REL = "tests/e2e/manifest.json"
BASELINE_VERSION = 1

REQUIRED_MAP_KEYS = ("mappingVersion", "baselineCommit", "sourceManifests", "scenarios")
SOURCE_MANIFEST_KEYS = ("ticket", "path", "sha256", "scenarioCount")
MAPPING_KEYS = ("ticket", "oldId", "newId")

RENAMES: dict[str, dict[str, str]] = {
    "GKSA-10": {"plugin-load-proton": "plugin-load-proton-countdown"},
    "GKSA-14": {"plugin-load-proton": "plugin-load-proton-local-fault-notices"},
    "GKSA-15": {
        "plugin-load-proton": "plugin-load-proton-combined-recovery",
        "localization-normal-popup-preserves-gksa13-input-native-pause": (
            "localization-normal-popup-preserves-native-input-pause"
        ),
    },
}


class MigrationInputError(Exception):
    """The candidate or oracle input is unreadable or structurally invalid."""


# --------------------------------------------------------------------------
# baseline freeze
# --------------------------------------------------------------------------


def _read_base_manifest_bytes(repo_root: Path, rel_path: str) -> bytes:
    """Read a legacy manifest from the exact base commit, failing closed.

    There is deliberately no working-tree fallback: a checkout that lacks the
    base commit (shallow clone, exported tree, non-Git directory) can never be
    mistaken for frozen history or written out as a baseline.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ps.BASE_COMMIT}:{rel_path}"],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MigrationInputError(f"git is not available; cannot read {rel_path} at {ps.BASE_COMMIT}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise MigrationInputError(
            f"exact base commit {ps.BASE_COMMIT} is not available for {rel_path} "
            f"(git exit {exc.returncode}; {detail or 'no objects for this commit'}). "
            "A baseline is never derived from the working tree."
        ) from exc
    return completed.stdout


def _normalized_context(manifest: ps.JsonObject) -> ps.JsonObject:
    context: ps.JsonObject = {key: value for key, value in manifest.items() if key not in ps.CONTEXT_REMOVED_KEYS}
    validation = ps.object_field(context, "validation", "legacy manifest")
    validation["supportedCaptureVersions"] = [2]
    return context


def freeze_baseline(repo_root: Path) -> ps.JsonObject:
    source_manifests: list[ps.JsonObject] = []
    contexts: list[ps.JsonObject] = []
    renames: list[ps.JsonObject] = []
    scenarios: list[ps.JsonObject] = []
    seen_new_ids: set[str] = set()

    for ticket in ps.TICKETS:
        profile_id = ps.TICKET_PROFILE[ticket]
        number = ticket.split("-")[1]
        rel_path = f"tests/e2e/gksa{number}.json"
        raw = _read_base_manifest_bytes(repo_root, rel_path)
        manifest = ps.as_object(ps.parse_json_strict(raw.decode("utf-8"), rel_path), rel_path)
        legacy_scenarios = ps.array_field(manifest, "scenarios", rel_path)
        expected_count = ps.TICKET_SCENARIO_COUNTS[ticket]
        if len(legacy_scenarios) != expected_count:
            raise MigrationInputError(f"{rel_path} has {len(legacy_scenarios)} scenarios, expected {expected_count}")
        source_manifests.append(
            {
                "ticket": ticket,
                "path": rel_path,
                "sha256": ps.sha256_bytes(raw),
                "scenarioCount": len(legacy_scenarios),
            }
        )
        context = _normalized_context(manifest)
        contexts.append(
            {
                "ticket": ticket,
                "profileId": profile_id,
                "digest": ps.canonical_digest(context),
            }
        )
        for scenario_value in legacy_scenarios:
            scenario = ps.as_object(scenario_value, f"{rel_path} scenario")
            old_id = scenario.get("id")
            if not isinstance(old_id, str) or not old_id:
                raise MigrationInputError(f"{rel_path} has a scenario without an id")
            new_id = RENAMES.get(ticket, {}).get(old_id, old_id)
            if new_id in seen_new_ids:
                raise MigrationInputError(f"frozen new id is not unique: {new_id!r}")
            seen_new_ids.add(new_id)
            scenarios.append(
                {
                    "ticket": ticket,
                    "oldId": old_id,
                    "newId": new_id,
                    "profileId": profile_id,
                    "requirements": [ticket],
                    "digest": ps.canonical_digest({key: value for key, value in scenario.items() if key != "id"}),
                }
            )
            if new_id != old_id:
                renames.append({"ticket": ticket, "oldId": old_id, "newId": new_id})

    if len(scenarios) != ps.TOTAL_SCENARIOS:
        raise MigrationInputError(f"frozen {len(scenarios)} scenarios, expected {ps.TOTAL_SCENARIOS}")
    return {
        "baselineVersion": BASELINE_VERSION,
        "baselineCommit": ps.BASE_COMMIT,
        "digestAlgorithm": (
            "sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, "
            "separators=(',',':'), allow_nan=False).encode('utf-8'))"
        ),
        "sourceManifests": ps.json_array(source_manifests),
        "contexts": ps.json_array(contexts),
        "renames": ps.json_array(renames),
        "scenarios": ps.json_array(scenarios),
    }


# --------------------------------------------------------------------------
# checker outcome
# --------------------------------------------------------------------------


@dataclass
class CheckOutcome:
    input_errors: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    checks: list[ps.JsonObject] = field(default_factory=list)
    manifest_id: str | None = None
    fingerprint: str | None = None

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def input_error(self, message: str) -> None:
        self.input_errors.append(message)

    def exit_code(self) -> int:
        if self.input_errors:
            return ps.EXIT_INPUT
        if self.failures:
            return ps.EXIT_FAIL
        return ps.EXIT_OK


# --------------------------------------------------------------------------
# baseline integrity
# --------------------------------------------------------------------------


def _baseline_sources(baseline: ps.JsonObject) -> list[ps.JsonObject]:
    return [
        ps.as_object(item, "baseline.sourceManifests[]")
        for item in ps.array_field(baseline, "sourceManifests", "baseline")
    ]


def _baseline_scenarios(baseline: ps.JsonObject) -> list[ps.JsonObject]:
    return [ps.as_object(item, "baseline.scenarios[]") for item in ps.array_field(baseline, "scenarios", "baseline")]


def _baseline_contexts(baseline: ps.JsonObject) -> list[ps.JsonObject]:
    return [ps.as_object(item, "baseline.contexts[]") for item in ps.array_field(baseline, "contexts", "baseline")]


def validate_baseline(baseline: ps.JsonValue, outcome: CheckOutcome) -> bool:
    if not isinstance(baseline, dict):
        outcome.input_error("baseline fixture is not a JSON object")
        return False
    if baseline.get("baselineVersion") != BASELINE_VERSION:
        outcome.input_error("baseline fixture has an unexpected baselineVersion")
        return False
    if baseline.get("baselineCommit") != ps.BASE_COMMIT:
        outcome.input_error("baseline fixture records a different base commit")
        return False
    scenarios = baseline.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != ps.TOTAL_SCENARIOS:
        outcome.input_error("baseline fixture does not hold 324 scenarios")
        return False
    contexts = baseline.get("contexts")
    if not isinstance(contexts, list) or len(contexts) != len(ps.PROFILE_IDS):
        outcome.input_error("baseline fixture does not hold six contexts")
        return False
    sources = baseline.get("sourceManifests")
    if not isinstance(sources, list) or len(sources) != len(ps.TICKETS):
        outcome.input_error("baseline fixture does not hold six source manifests")
        return False
    outcome.check(
        "baseline-integrity",
        True,
        f"{len(scenarios)} scenarios, {len(contexts)} contexts, {len(sources)} sources",
    )
    return True


# --------------------------------------------------------------------------
# migration map checks
# --------------------------------------------------------------------------


def _nonempty_string(value: ps.JsonValue) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _entry_keys_ok(
    entry: ps.JsonValue, required: tuple[str, ...], label: str, outcome: CheckOutcome
) -> TypeGuard[ps.JsonObject]:
    if not isinstance(entry, dict):
        outcome.input_error(f"{label} is not an object")
        return False
    missing = [key for key in required if key not in entry]
    unknown = sorted(set(entry) - set(required))
    if missing:
        outcome.input_error(f"{label} is missing keys {missing}")
        return False
    if unknown:
        outcome.input_error(f"{label} has unknown keys {unknown}")
        return False
    return True


def check_migration_map(migration_map: ps.JsonValue, baseline: ps.JsonObject, outcome: CheckOutcome) -> None:
    if not isinstance(migration_map, dict):
        outcome.input_error("migration map root is not a JSON object")
        return
    missing = [key for key in REQUIRED_MAP_KEYS if key not in migration_map]
    unknown = sorted(set(migration_map) - set(REQUIRED_MAP_KEYS))
    if missing:
        outcome.input_error(f"migration map is missing keys {missing}")
    if unknown:
        outcome.input_error(f"migration map has unknown keys {unknown}")
    if missing or unknown:
        outcome.check("migration-map-shape", False, "required/unknown key mismatch")
        return
    outcome.check("migration-map-shape", True, "exact top-level shape")

    if not ps.is_int(migration_map["mappingVersion"]) or (migration_map["mappingVersion"] != 1):
        outcome.input_error("mappingVersion must be the integer 1")
    if migration_map["baselineCommit"] != baseline["baselineCommit"]:
        outcome.fail("migration map baselineCommit does not match the frozen base commit")
    outcome.check(
        "migration-map-baseline-commit",
        migration_map["baselineCommit"] == baseline["baselineCommit"],
        str(migration_map["baselineCommit"]),
    )

    _check_source_manifests(migration_map["sourceManifests"], baseline, outcome)
    _check_mapping_bijection(migration_map["scenarios"], baseline, outcome)


def _check_source_manifests(sources: ps.JsonValue, baseline: ps.JsonObject, outcome: CheckOutcome) -> None:
    before = len(outcome.failures)
    base_by_ticket: dict[str, ps.JsonObject] = {}
    for baseline_entry in _baseline_sources(baseline):
        base_by_ticket[ps.as_string(baseline_entry["ticket"], "baseline source ticket")] = baseline_entry
    if not isinstance(sources, list):
        outcome.input_error("sourceManifests is not an array")
        return
    if len(sources) != len(base_by_ticket):
        outcome.fail(f"sourceManifests has {len(sources)} entries, expected {len(base_by_ticket)}")
    seen: set[str] = set()
    for index, entry in enumerate(sources):
        label = f"sourceManifests[{index}]"
        if not _entry_keys_ok(entry, SOURCE_MANIFEST_KEYS, label, outcome):
            continue
        ticket = entry["ticket"]
        if not isinstance(ticket, str) or ticket not in base_by_ticket:
            outcome.fail(f"{label} names an unknown ticket {ticket!r}")
            continue
        if ticket in seen:
            outcome.fail(f"{label} duplicates ticket {ticket!r}")
            continue
        seen.add(ticket)
        expected = base_by_ticket[ticket]
        for field_name in ("path", "sha256", "scenarioCount"):
            if entry[field_name] != expected[field_name]:
                outcome.fail(f"{label}.{field_name} = {entry[field_name]!r}, frozen value is {expected[field_name]!r}")
    missing = sorted(set(base_by_ticket) - seen)
    if missing:
        outcome.fail(f"sourceManifests is missing tickets {missing}")
    ok = len(outcome.failures) == before
    outcome.check("source-manifest-hashes", ok, f"checked {len(sources)} sources")


def _check_mapping_bijection(mappings: ps.JsonValue, baseline: ps.JsonObject, outcome: CheckOutcome) -> None:
    before = len(outcome.failures)
    baseline_scenarios = _baseline_scenarios(baseline)
    base_by_old: dict[tuple[str, str], ps.JsonObject] = {}
    base_by_new: dict[str, ps.JsonObject] = {}
    for baseline_entry in baseline_scenarios:
        ticket = ps.as_string(baseline_entry["ticket"], "baseline scenario ticket")
        old_id = ps.as_string(baseline_entry["oldId"], "baseline scenario oldId")
        new_id = ps.as_string(baseline_entry["newId"], "baseline scenario newId")
        base_by_old[(ticket, old_id)] = baseline_entry
        base_by_new[new_id] = baseline_entry
    if not isinstance(mappings, list):
        outcome.input_error("scenarios mapping is not an array")
        return
    if len(mappings) != len(base_by_old):
        outcome.fail(f"mapping has {len(mappings)} scenarios, expected {len(base_by_old)}")
    seen_old: set[tuple[str, str]] = set()
    seen_new: dict[str, tuple[str, str]] = {}
    for index, entry in enumerate(mappings):
        label = f"scenarios[{index}]"
        if not _entry_keys_ok(entry, MAPPING_KEYS, label, outcome):
            continue
        ticket_value = _nonempty_string(entry["ticket"])
        old_value = _nonempty_string(entry["oldId"])
        new_value = _nonempty_string(entry["newId"])
        if ticket_value is None or old_value is None or new_value is None:
            outcome.input_error(f"{label} must use non-empty string identifiers")
            continue
        ticket = ticket_value
        old_id = old_value
        new_id = new_value
        key = (ticket, old_id)
        if key not in base_by_old:
            outcome.fail(f"{label} names an unknown legacy scenario {key}")
            continue
        if key in seen_old:
            outcome.fail(f"{label} repeats legacy scenario {key}")
            continue
        seen_old.add(key)
        if new_id in seen_new:
            outcome.fail(f"many-to-one mapping: {new_id!r} is claimed by {seen_new[new_id]} and {key}")
            continue
        seen_new[new_id] = key
        if base_by_old[key]["newId"] != new_id:
            outcome.fail(f"{label} newId {new_id!r} != frozen rename {base_by_old[key]['newId']!r}")
        if new_id not in base_by_new:
            outcome.fail(f"{label} newId {new_id!r} is not a frozen scenario id")
    missing = sorted(set(base_by_old) - seen_old)
    if missing:
        outcome.fail(f"mapping is missing legacy scenarios: {missing[:5]}...")
    ok = len(outcome.failures) == before and not missing and len(seen_old) == len(base_by_old)
    outcome.check(
        "scenario-bijection",
        ok,
        f"{len(seen_old)} legacy keys, {len(seen_new)} new ids",
    )


# --------------------------------------------------------------------------
# v2 tree loading
# --------------------------------------------------------------------------


@dataclass
class LoadedTree:
    index: ps.JsonObject
    files: dict[str, ps.JsonValue]
    profiles_rel: str
    profile_ids: list[str]
    scenario_files: list[str]


def _reject_symlink(base: Path, rel: str) -> None:
    current = base
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise MigrationInputError(f"path {rel!r} traverses a symlink at {current}")


def _safe_resolve(base: Path, rel: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute():
        raise MigrationInputError(f"absolute path {rel!r} is not allowed")
    resolved = (base / candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise MigrationInputError(f"path {rel!r} escapes {base}")
    _reject_symlink(base, rel)
    return base / candidate


def _iter_scenario_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in dirnames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                raise MigrationInputError(f"symlinked scenario directory {candidate}")
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.suffix != ".json":
                continue
            if candidate.is_symlink():
                raise MigrationInputError(f"symlinked scenario file {candidate}")
            found.append(candidate)
    return found


def load_tree(index_path: Path) -> LoadedTree:
    base = index_path.parent
    index_value = ps.load_json_strict(index_path)
    if not isinstance(index_value, dict):
        raise MigrationInputError("manifest index is not a JSON object")
    index = index_value
    if index.get("kind") != ps.INDEX_KIND:
        raise MigrationInputError(f"unexpected index kind {index.get('kind')!r}")
    version = index.get("manifestVersion")
    if not ps.is_int(version) or version != ps.MANIFEST_VERSION:
        raise MigrationInputError("index manifestVersion must be integer 2")
    manifest_id = index.get("manifestId")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise MigrationInputError("index manifestId must be a non-empty string")
    profiles_rel_value = index.get("profilesFile")
    scenario_root_value = index.get("scenarioRoot")
    if not isinstance(profiles_rel_value, str) or not profiles_rel_value:
        raise MigrationInputError("index profilesFile must be a non-empty string")
    if not isinstance(scenario_root_value, str) or not scenario_root_value:
        raise MigrationInputError("index scenarioRoot must be a non-empty string")
    profiles_rel = profiles_rel_value
    scenario_root_rel = scenario_root_value

    profiles_path = _safe_resolve(base, profiles_rel)
    if not profiles_path.is_file():
        raise MigrationInputError(f"profiles file not found: {profiles_rel}")
    profiles = ps.load_json_strict(profiles_path)
    if not isinstance(profiles, dict) or not profiles:
        raise MigrationInputError("profiles file must be a non-empty object")

    scenario_root = _safe_resolve(base, scenario_root_rel)
    if not scenario_root.is_dir():
        raise MigrationInputError(f"scenario root not found: {scenario_root_rel}")

    files: dict[str, ps.JsonValue] = {profiles_rel: profiles}
    profile_ids = list(profiles)
    scenario_files: list[str] = []
    for found in _iter_scenario_files(scenario_root):
        rel = ps.relative_posix(found, base)
        parsed = ps.load_json_strict(found)
        if not isinstance(parsed, dict):
            raise MigrationInputError(f"scenario file {rel} is not an object")
        if parsed.get("kind") != ps.SCENARIOS_KIND:
            raise MigrationInputError(f"scenario file {rel} has an unexpected kind")
        schema_version = parsed.get("schemaVersion")
        if not ps.is_int(schema_version) or schema_version != ps.SCHEMA_VERSION:
            raise MigrationInputError(f"scenario file {rel} schemaVersion must be 2")
        collection = parsed.get("scenarios")
        if not isinstance(collection, list) or not collection:
            raise MigrationInputError(f"scenario file {rel} has an empty scenarios array")
        files[rel] = parsed
        scenario_files.append(rel)
    if not scenario_files:
        raise MigrationInputError("scenario root contains no scenario files")
    return LoadedTree(
        index=index,
        files=files,
        profiles_rel=profiles_rel,
        profile_ids=profile_ids,
        scenario_files=sorted(scenario_files),
    )


# --------------------------------------------------------------------------
# migrated content checks
# --------------------------------------------------------------------------


def _iter_candidate_scenarios(
    tree: LoadedTree,
) -> list[tuple[str, ps.JsonValue]]:
    items: list[tuple[str, ps.JsonValue]] = []
    for rel in tree.scenario_files:
        parsed = ps.as_object(tree.files[rel], rel)
        for scenario in ps.array_field(parsed, "scenarios", rel):
            items.append((rel, scenario))
    return items


def check_v2_content(tree: LoadedTree, baseline: ps.JsonObject, outcome: CheckOutcome) -> None:
    manifest_id = tree.index.get("manifestId")
    outcome.manifest_id = manifest_id if isinstance(manifest_id, str) else None
    base_contexts: dict[str, ps.JsonObject] = {}
    for item in _baseline_contexts(baseline):
        base_contexts[ps.as_string(item["profileId"], "baseline context profileId")] = item
    profiles = ps.as_object(tree.files[tree.profiles_rel], tree.profiles_rel)
    profile_ids = set(profiles)
    if profile_ids != set(base_contexts):
        outcome.fail(f"profile ids differ from the frozen contexts: {sorted(profile_ids)} vs {sorted(base_contexts)}")
    context_ok = True
    for profile_id, context_value in profiles.items():
        if not isinstance(context_value, dict):
            outcome.input_error(f"context {profile_id!r} is not an object")
            context_ok = False
            continue
        context = context_value
        removed = sorted(set(context) & set(ps.CONTEXT_REMOVED_KEYS))
        if removed:
            outcome.fail(f"context {profile_id!r} retains legacy keys {removed}")
            context_ok = False
        digest = ps.canonical_digest(context)
        expected = base_contexts.get(profile_id, {}).get("digest")
        if digest != expected:
            outcome.fail(f"context {profile_id!r} content differs from the frozen context digest")
            context_ok = False
    outcome.check("contexts-preserved", context_ok, f"{len(profiles)} contexts compared")

    base_by_new: dict[str, ps.JsonObject] = {}
    by_key: dict[tuple[str, str], ps.JsonObject] = {}
    for item in _baseline_scenarios(baseline):
        new_id = ps.as_string(item["newId"], "baseline scenario newId")
        ticket = ps.as_string(item["ticket"], "baseline scenario ticket")
        old_id = ps.as_string(item["oldId"], "baseline scenario oldId")
        base_by_new[new_id] = item
        by_key[(ticket, old_id)] = item
    candidate_by_id: dict[str, ps.JsonObject] = {}
    id_ok = True
    content_ok = True
    for rel, scenario in _iter_candidate_scenarios(tree):
        if not isinstance(scenario, dict):
            outcome.input_error(f"scenario in {rel} is not an object")
            id_ok = False
            continue
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            outcome.input_error(f"scenario in {rel} has no usable id")
            id_ok = False
            continue
        if scenario_id in candidate_by_id:
            outcome.fail(f"duplicate candidate scenario id {scenario_id!r}")
            id_ok = False
            continue
        candidate_by_id[scenario_id] = scenario
        frozen = base_by_new.get(scenario_id)
        if frozen is None:
            outcome.fail(f"candidate scenario {scenario_id!r} is not in the baseline")
            id_ok = False
            continue
        if scenario.get("profileId") != frozen["profileId"]:
            outcome.fail(f"scenario {scenario_id!r} profileId {scenario.get('profileId')!r} != {frozen['profileId']!r}")
            id_ok = False
        requirements = scenario.get("requirements")
        if (
            not isinstance(requirements, list)
            or not requirements
            or not all(isinstance(item, str) and item for item in requirements)
        ):
            outcome.fail(f"scenario {scenario_id!r} requirements must be a non-empty string array")
            id_ok = False
        elif frozen["ticket"] not in requirements:
            outcome.fail(f"scenario {scenario_id!r} requirements {requirements!r} do not record {frozen['ticket']!r}")
            id_ok = False
        content = {key: value for key, value in scenario.items() if key not in ps.SCENARIO_IDENTITY_KEYS}
        if ps.canonical_digest(content) != frozen["digest"]:
            outcome.fail(f"scenario {scenario_id!r} preserved content differs from the frozen digest")
            content_ok = False
    missing = sorted(set(base_by_new) - set(candidate_by_id))
    if missing:
        outcome.fail(f"candidate is missing scenarios: {missing[:5]}...")
        id_ok = False
    outcome.check(
        "scenario-ids-profile-requirements",
        id_ok,
        f"{len(candidate_by_id)} candidate ids",
    )
    outcome.check("scenario-content-preserved", content_ok, f"{len(by_key)} frozen contents")
    outcome.check(
        "coverage-324",
        len(candidate_by_id) == len(base_by_new),
        f"{len(candidate_by_id)}/{len(base_by_new)} scenarios",
    )


# --------------------------------------------------------------------------
# checker run
# --------------------------------------------------------------------------


def _resolve_output(repo_root: Path, raw: str) -> Path:
    output = Path(raw)
    if not output.is_absolute():
        output = (repo_root / output).resolve()
    return output


def run_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    output = _resolve_output(repo_root, args.output)
    baseline_path = Path(args.baseline).resolve() if args.baseline else DEFAULT_BASELINE
    outcome = CheckOutcome()

    baseline: ps.JsonValue | None = None
    try:
        baseline = ps.load_json_strict(baseline_path)
    except ps.JsonLoadError as exc:
        outcome.input_error(f"cannot load frozen baseline: {exc}")

    if isinstance(baseline, dict) and validate_baseline(baseline, outcome):
        migration_map_path = Path(args.migration_map).resolve() if args.migration_map else repo_root / MIGRATION_MAP_REL
        if not migration_map_path.is_file():
            outcome.input_error(f"migration map not found: {migration_map_path}")
        else:
            try:
                migration_map = ps.load_json_strict(migration_map_path)
                check_migration_map(migration_map, baseline, outcome)
            except ps.JsonLoadError as exc:
                outcome.input_error(f"cannot load migration map: {exc}")

        manifest_path = repo_root / MANIFEST_REL
        if not manifest_path.is_file():
            outcome.input_error(f"candidate v2 manifest not found: {manifest_path}")
        else:
            try:
                tree = load_tree(manifest_path)
                check_v2_content(tree, baseline, outcome)
                outcome.fingerprint = ps.manifest_fingerprint(tree.index, tree.files)
            except MigrationInputError as exc:
                outcome.input_error(str(exc))
            except ps.JsonLoadError as exc:
                outcome.input_error(f"cannot load candidate tree: {exc}")

    code = outcome.exit_code()
    input_error_items: ps.JsonArray = []
    for message in outcome.input_errors:
        input_error_items.append(message)
    failure_items: ps.JsonArray = []
    for message in outcome.failures:
        failure_items.append(message)
    check_items: ps.JsonArray = []
    for check_item in outcome.checks:
        check_items.append(check_item)
    report: ps.JsonObject = {
        "artifactVersion": 1,
        "kind": "gksa23-migration-check",
        "assertion": "traceability-only",
        "e2eVerdict": None,
        "baselineCommit": ps.BASE_COMMIT,
        "repoRoot": str(repo_root),
        "baseline": str(baseline_path),
        "migrationMap": str(
            Path(args.migration_map).resolve() if args.migration_map else repo_root / MIGRATION_MAP_REL
        ),
        "manifest": str(repo_root / MANIFEST_REL),
        "manifestId": outcome.manifest_id,
        "fingerprint": outcome.fingerprint,
        "exit": code,
        "ok": code == ps.EXIT_OK,
        "inputErrors": input_error_items,
        "failures": failure_items,
        "checks": check_items,
        "command": " ".join(
            [
                "python",
                "tests/tools/check_migration.py",
                "--repo-root",
                ".",
                "--output",
                str(output),
            ]
        ),
        "python": sys.version,
        "note": (
            "migration-map.json is traceability only and never a runtime protocol "
            "input. The oracle is frozen from the base commit; real game scenarios "
            "remain unexecuted and e2eVerdict is null."
        ),
    }
    ps.write_report(output, report)
    if code != ps.EXIT_OK:
        for message in outcome.input_errors:
            sys.stderr.write(f"FAIL [input] {message}\n")
        for message in outcome.failures[:20]:
            sys.stderr.write(f"FAIL [migration] {message}\n")
        sys.stderr.write(
            f"RESULT: RED (migration traceability check; exit {code}; "
            "expected until the v2 tree and migration map land)\n"
        )
    else:
        sys.stderr.write(
            "RESULT: PASS (migration traceability only; 324 scenarios compared; no game execution asserted)\n"
        )
    return code


# --------------------------------------------------------------------------
# correct candidate builder (self-test + green oracle self-check)
# --------------------------------------------------------------------------


def build_correct_candidate(repo_root: Path, baseline: ps.JsonObject, dest: Path) -> None:
    e2e = dest / "tests" / "e2e"
    scenarios_dir = e2e / "scenarios"
    shutil.rmtree(dest, ignore_errors=True)
    profiles: dict[str, ps.JsonObject] = {}
    scenarios_by_profile: dict[str, list[ps.JsonObject]] = {profile_id: [] for profile_id in ps.PROFILE_IDS}
    for source in _baseline_sources(baseline):
        ticket = ps.as_string(source["ticket"], "baseline source ticket")
        rel_path = ps.as_string(source["path"], "baseline source path")
        manifest = ps.as_object(
            ps.parse_json_strict(
                _read_base_manifest_bytes(repo_root, rel_path).decode("utf-8"),
                rel_path,
            ),
            rel_path,
        )
        context = _normalized_context(manifest)
        profile_id = ps.TICKET_PROFILE[ticket]
        profiles[profile_id] = context
        old_by_id: dict[str, ps.JsonObject] = {}
        for value in ps.array_field(manifest, "scenarios", rel_path):
            scenario = ps.as_object(value, rel_path)
            old_by_id[ps.as_string(scenario["id"], rel_path)] = scenario
        for entry in _baseline_scenarios(baseline):
            if entry.get("ticket") != ticket:
                continue
            old_id = ps.as_string(entry["oldId"], "baseline scenario oldId")
            new_id = ps.as_string(entry["newId"], "baseline scenario newId")
            scenario = dict(old_by_id[old_id])
            scenario["id"] = new_id
            scenario["profileId"] = profile_id
            scenario["requirements"] = [ticket]
            scenarios_by_profile[profile_id].append(scenario)

    profiles_document: ps.JsonObject = {}
    for profile_id, context in profiles.items():
        profiles_document[profile_id] = context

    _write_json(
        e2e / "manifest.json",
        {
            "kind": ps.INDEX_KIND,
            "manifestVersion": ps.MANIFEST_VERSION,
            "manifestId": "gksr-full-mod",
            "profilesFile": "profiles.json",
            "scenarioRoot": "scenarios",
            "title": "frozen-baseline candidate (test fixture)",
        },
    )
    _write_json(e2e / "profiles.json", profiles_document)
    for profile_id, scenarios in scenarios_by_profile.items():
        _write_json(
            scenarios_dir / f"{profile_id}.json",
            {
                "kind": ps.SCENARIOS_KIND,
                "schemaVersion": ps.SCHEMA_VERSION,
                "scenarios": ps.json_array(scenarios),
            },
        )

    source_items: ps.JsonArray = []
    for item in _baseline_sources(baseline):
        source_items.append(
            {
                "ticket": ps.as_string(item["ticket"], "baseline source ticket"),
                "path": ps.as_string(item["path"], "baseline source path"),
                "sha256": ps.as_string(item["sha256"], "baseline source sha256"),
                "scenarioCount": ps.as_int(item["scenarioCount"], "baseline source scenarioCount"),
            }
        )
    mapping_entries: list[tuple[str, str, ps.JsonObject]] = []
    for item in _baseline_scenarios(baseline):
        ticket = ps.as_string(item["ticket"], "baseline scenario ticket")
        old_id = ps.as_string(item["oldId"], "baseline scenario oldId")
        new_id = ps.as_string(item["newId"], "baseline scenario newId")
        mapping_entries.append(
            (
                ticket,
                old_id,
                {"ticket": ticket, "oldId": old_id, "newId": new_id},
            )
        )
    mapping_entries.sort(key=lambda entry: (entry[0], entry[1]))
    mapping_items: ps.JsonArray = []
    for _, _, mapping_entry in mapping_entries:
        mapping_items.append(mapping_entry)
    _write_json(
        e2e / "migration-map.json",
        {
            "mappingVersion": 1,
            "baselineCommit": ps.as_string(baseline["baselineCommit"], "baseline commit"),
            "sourceManifests": source_items,
            "scenarios": mapping_items,
        },
    )


def _write_json(path: Path, obj: ps.JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _load_json(path: Path) -> ps.JsonValue:
    return ps.load_json_strict(path)


def _load_object(path: Path) -> ps.JsonObject:
    return ps.as_object(_load_json(path), str(path))


# --------------------------------------------------------------------------
# mutation suite
# --------------------------------------------------------------------------


def _first_scenario_file(repo: Path) -> Path:
    scenario_dir = repo / "tests" / "e2e" / "scenarios"
    files = sorted(scenario_dir.glob("*.json"))
    if not files:
        raise MigrationInputError("candidate has no scenario files")
    return files[0]


def _mapping_scenarios(data: ps.JsonObject) -> ps.JsonArray:
    return ps.array_field(data, "scenarios", "migration map")


def _scenario_entries(data: ps.JsonObject) -> ps.JsonArray:
    return ps.array_field(data, "scenarios", "scenario file")


def _first_scenario_entry(data: ps.JsonObject) -> ps.JsonObject:
    return ps.as_object(_scenario_entries(data)[0], "scenarios[0]")


def _profiles_context(data: ps.JsonObject, profile_id: str) -> ps.JsonObject:
    return ps.object_field(data, profile_id, "profiles")


def mut_missing_mapping(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    _mapping_scenarios(data).pop()
    _write_json(path, data)


def mut_many_to_one(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    mappings = _mapping_scenarios(data)
    first = ps.as_object(mappings[0], "scenarios[0]")
    second = ps.as_object(mappings[1], "scenarios[1]")
    second["newId"] = first["newId"]
    _write_json(path, data)


def mut_missing_scenario(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    _scenario_entries(data).pop()
    _write_json(path, data)


def mut_extra_scenario(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    _scenario_entries(data).append(
        {
            "id": "synthetic-extra-scenario",
            "profileId": "countdown",
            "requirements": ["GKSA-10"],
            "evidence": {"screenshot": True, "log": False, "measurement": False},
            "expect": [{"observation": "hud.visible", "equals": True}],
        }
    )
    _write_json(path, data)


def mut_wrong_requirements(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    _first_scenario_entry(data)["requirements"] = ["GKSA-99"]
    _write_json(path, data)


def mut_wrong_profile(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    scenario = _first_scenario_entry(data)
    current = scenario.get("profileId")
    other = "sermon-popup" if current != "sermon-popup" else "countdown"
    scenario["profileId"] = other
    _write_json(path, data)


def mut_content_step(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    scenario = _first_scenario_entry(data)
    steps_value = scenario.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        steps: ps.JsonArray = ["synthetic step"]
        scenario["steps"] = steps
    else:
        steps = steps_value
    steps[0] = "drifted synthetic step"
    _write_json(path, data)


def mut_content_expect(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    for scenario_value in _scenario_entries(data):
        scenario = ps.as_object(scenario_value, "scenario")
        for condition_value in ps.array_field(scenario, "expect", "scenario"):
            condition = ps.as_object(condition_value, "expect condition")
            if "equals" in condition:
                condition["equals"] = "drifted-expected-value"
                _write_json(path, data)
                return
    raise MigrationInputError("no equals expectation to drift")


def mut_content_evidence(repo: Path) -> None:
    path = _first_scenario_file(repo)
    data = _load_object(path)
    scenario = _first_scenario_entry(data)
    evidence_value = scenario.get("evidence")
    if not isinstance(evidence_value, dict):
        evidence: ps.JsonObject = {}
        scenario["evidence"] = evidence
    else:
        evidence = evidence_value
    if "measurement" not in evidence:
        evidence["measurement"] = True
    else:
        evidence["measurement"] = not evidence["measurement"]
    _write_json(path, data)


def mut_content_date(repo: Path) -> None:
    scenario_dir = repo / "tests" / "e2e" / "scenarios"
    for path in sorted(scenario_dir.glob("*.json")):
        data = _load_object(path)
        for scenario_value in _scenario_entries(data):
            scenario = ps.as_object(scenario_value, "scenario")
            for condition_value in ps.array_field(scenario, "expect", "scenario"):
                condition = ps.as_object(condition_value, "expect condition")
                spec_value = condition.get("dateCheck")
                if isinstance(spec_value, dict) and spec_value:
                    spec = spec_value
                    first_key = sorted(spec)[0]
                    spec[first_key] = "drifted.date.check.path"
                    _write_json(path, data)
                    return
    raise MigrationInputError("no dateCheck expectation to drift")


def mut_context_vocab(repo: Path) -> None:
    path = repo / "tests" / "e2e" / "profiles.json"
    data = _load_object(path)
    vocab = ps.object_field(_profiles_context(data, "countdown"), "observationVocabulary", "countdown")
    vocab["hud.visible"] = "drifted vocabulary description"
    _write_json(path, data)


def mut_context_language(repo: Path) -> None:
    path = repo / "tests" / "e2e" / "profiles.json"
    data = _load_object(path)
    final_text = ps.object_field(_profiles_context(data, "countdown"), "finalText", "countdown")
    first_key = sorted(final_text)[0]
    entry = ps.as_object(final_text[first_key], "finalText entry")
    entry["en"] = "drifted english sentence"
    _write_json(path, data)


def mut_context_measurement(repo: Path) -> None:
    path = repo / "tests" / "e2e" / "profiles.json"
    data = _load_object(path)
    validation = ps.object_field(_profiles_context(data, "countdown"), "validation", "countdown")
    validation["measurementObservationPaths"] = ["drifted.path"]
    _write_json(path, data)


def mut_context_removed_key(repo: Path) -> None:
    path = repo / "tests" / "e2e" / "profiles.json"
    data = _load_object(path)
    _profiles_context(data, "countdown")["ticket"] = "GKSA-10"
    _write_json(path, data)


def mut_wrong_commit(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    data["baselineCommit"] = "0" * 40
    _write_json(path, data)


def _first_source_entry(data: ps.JsonObject) -> ps.JsonObject:
    sources = ps.array_field(data, "sourceManifests", "migration map")
    return ps.as_object(sources[0], "sourceManifests[0]")


def mut_wrong_old_hash(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    _first_source_entry(data)["sha256"] = "0" * 64
    _write_json(path, data)


def mut_wrong_count(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    _first_source_entry(data)["scenarioCount"] = 0
    _write_json(path, data)


def mut_wrong_mapping_version(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    data["mappingVersion"] = 2
    _write_json(path, data)


def mut_unknown_key(repo: Path) -> None:
    path = repo / MIGRATION_MAP_REL
    data = _load_object(path)
    data["unexpected"] = True
    _write_json(path, data)


def mut_missing_map(repo: Path) -> None:
    (repo / MIGRATION_MAP_REL).unlink()


@dataclass(frozen=True)
class MutationCase:
    case_id: str
    description: str
    mutate: Callable[[Path], None] | None
    expect_ok: bool
    expect_exit: int


MUTATION_CASES: list[MutationCase] = [
    MutationCase(
        "correct-candidate",
        "frozen baseline candidate passes",
        None,
        True,
        ps.EXIT_OK,
    ),
    MutationCase(
        "missing-mapping",
        "a missing scenario mapping is rejected",
        mut_missing_mapping,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "many-to-one-mapping",
        "a many-to-one new id mapping is rejected",
        mut_many_to_one,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "missing-scenario",
        "a missing migrated scenario is rejected",
        mut_missing_scenario,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "extra-scenario",
        "an extra migrated scenario is rejected",
        mut_extra_scenario,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-requirements",
        "a scenario that drops its original ticket is rejected",
        mut_wrong_requirements,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-profile",
        "a scenario assigned to the wrong profile is rejected",
        mut_wrong_profile,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-step",
        "a drifted steps value is rejected",
        mut_content_step,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-expect",
        "a drifted expectation value is rejected",
        mut_content_expect,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-evidence",
        "a drifted evidence flag is rejected",
        mut_content_evidence,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-date",
        "a drifted dateCheck observation is rejected",
        mut_content_date,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-vocabulary",
        "a drifted observation vocabulary is rejected",
        mut_context_vocab,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-language",
        "a drifted finalText language entry is rejected",
        mut_context_language,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "drift-measurement",
        "a drifted measurement rule is rejected",
        mut_context_measurement,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "context-removed-key",
        "a context that retains a legacy key is rejected",
        mut_context_removed_key,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-base-commit",
        "a wrong baselineCommit is rejected",
        mut_wrong_commit,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-old-hash",
        "a wrong legacy manifest hash is rejected",
        mut_wrong_old_hash,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-scenario-count",
        "a wrong legacy scenario count is rejected",
        mut_wrong_count,
        False,
        ps.EXIT_FAIL,
    ),
    MutationCase(
        "wrong-mapping-version",
        "a wrong mappingVersion is rejected as invalid input",
        mut_wrong_mapping_version,
        False,
        ps.EXIT_INPUT,
    ),
    MutationCase(
        "unknown-map-key",
        "an unknown migration map key is rejected as invalid input",
        mut_unknown_key,
        False,
        ps.EXIT_INPUT,
    ),
    MutationCase(
        "missing-map",
        "a missing migration map is rejected as invalid input",
        mut_missing_map,
        False,
        ps.EXIT_INPUT,
    ),
]


def _report_has_reason(report: ps.JsonObject) -> bool:
    """A rejected migration check must record at least one concrete reason."""
    for key in ("inputErrors", "failures"):
        value = report.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return True
    return False


def _write_plausible_legacy_tree(root: Path) -> None:
    """Write legacy-looking manifests that have no Git history behind them."""
    e2e = root / "tests" / "e2e"
    e2e.mkdir(parents=True, exist_ok=True)
    for ticket in ps.TICKETS:
        number = ticket.split("-")[1]
        _write_json(
            e2e / f"gksa{number}.json",
            {
                "kind": f"gksa{number}-e2e-manifest",
                "manifestVersion": 1,
                "ticket": ticket,
                "scenarios": [{"id": "plausible-legacy-scenario", "expect": []}],
            },
        )


def _history_guard_cases(script: Path, work: Path) -> list[ps.JsonObject]:
    """Black-box CLI cases: a tree without base-commit history fails closed.

    ``GIT_CEILING_DIRECTORIES`` stops git from finding the enclosing real
    repository, so the fixture behaves like an exported tree or a shallow clone
    that lacks the base commit objects. The frozen fixture is never regenerated
    from such a tree and an existing baseline is never overwritten.
    """
    root = work / "no-history"
    shutil.rmtree(root, ignore_errors=True)
    _write_plausible_legacy_tree(root)
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(work)

    cases: list[ps.JsonObject] = []

    sentinel = root / "sentinel-baseline.json"
    sentinel.write_text('{"frozen": "sentinel"}\n', encoding="utf-8")
    sentinel_before = sentinel.read_bytes()
    freeze = subprocess.run(
        [
            sys.executable,
            str(script),
            "--freeze-baseline",
            "--repo-root",
            str(root),
            "--baseline",
            str(sentinel),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    freeze_problems: list[str] = []
    if freeze.returncode == ps.EXIT_OK:
        freeze_problems.append("freeze accepted a tree without the base commit")
    if freeze.returncode != ps.EXIT_INPUT:
        freeze_problems.append(f"freeze exit {freeze.returncode} != {ps.EXIT_INPUT}")
    if sentinel.read_bytes() != sentinel_before:
        freeze_problems.append("freeze overwrote the existing baseline sentinel")
    diagnostic = (freeze.stderr + freeze.stdout).lower()
    if ps.BASE_COMMIT not in diagnostic and "commit" not in diagnostic:
        freeze_problems.append("freeze diagnostic does not name the missing history")
    cases.append(
        {
            "id": "freeze-requires-history",
            "description": (
                "freeze-baseline fails closed on a non-Git tree with plausible "
                "legacy files and never overwrites an existing baseline"
            ),
            "expected": "reject",
            "expectedExit": ps.EXIT_INPUT,
            "exit": freeze.returncode,
            "reportOk": None,
            "passed": not freeze_problems,
            "problems": ps.json_array(freeze_problems),
        }
    )

    selftest_report = work / "reports" / "selftest-requires-history.json"
    selftest = subprocess.run(
        [
            sys.executable,
            str(script),
            "--self-test",
            "--repo-root",
            str(root),
            "--output",
            str(selftest_report),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    selftest_problems: list[str] = []
    if selftest.returncode == ps.EXIT_OK:
        selftest_problems.append("self-test accepted a tree without the base commit")
    if selftest.returncode != ps.EXIT_INPUT:
        selftest_problems.append(f"self-test exit {selftest.returncode} != {ps.EXIT_INPUT}")
    cases.append(
        {
            "id": "selftest-requires-history",
            "description": (
                "self-test requires the exact base commit and fails closed "
                "without the Git objects behind the frozen fixture"
            ),
            "expected": "reject",
            "expectedExit": ps.EXIT_INPUT,
            "exit": selftest.returncode,
            "reportOk": None,
            "passed": not selftest_problems,
            "problems": ps.json_array(selftest_problems),
        }
    )
    return cases


def run_self_test(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    output = _resolve_output(repo_root, args.output)
    work = output.parent / (output.stem + "-work")
    shutil.rmtree(work, ignore_errors=True)
    baseline_path = Path(args.baseline).resolve() if args.baseline else DEFAULT_BASELINE
    baseline_value = ps.load_json_strict(baseline_path)
    if not isinstance(baseline_value, dict):
        sys.stderr.write("FAIL [setup] baseline fixture is not an object\n")
        return ps.EXIT_INPUT
    baseline = baseline_value
    correct = work / "correct"
    try:
        build_correct_candidate(repo_root, baseline, correct)
    except (MigrationInputError, ps.JsonLoadError) as exc:
        sys.stderr.write(
            f"FAIL [history] the migration self-test requires the exact base commit {ps.BASE_COMMIT}; {exc}\n"
        )
        return ps.EXIT_INPUT

    script = Path(__file__).resolve()
    cases: list[ps.JsonObject] = []
    all_passed = True
    for mutation in MUTATION_CASES:
        case_repo = work / "cases" / mutation.case_id
        shutil.copytree(correct, case_repo)
        problems: list[str] = []
        try:
            if mutation.mutate is not None:
                mutation.mutate(case_repo)
        except Exception as exc:  # noqa: BLE001 - surface fixture build failures
            problems.append(f"mutation build failed: {exc}")
        report_path = work / "reports" / f"{mutation.case_id}.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--repo-root",
                str(case_repo),
                "--output",
                str(report_path),
            ],
            capture_output=True,
            text=True,
        )
        report_value: ps.JsonValue | None = None
        if report_path.exists():
            report_value = _load_json(report_path)
        report = report_value if isinstance(report_value, dict) else None
        if mutation.expect_ok:
            if completed.returncode != mutation.expect_exit:
                problems.append(
                    f"expected exit {mutation.expect_exit}, got "
                    f"{completed.returncode}; stderr={completed.stderr.strip()[:300]}"
                )
        else:
            if completed.returncode != mutation.expect_exit:
                problems.append(f"expected rejection exit {mutation.expect_exit}, got {completed.returncode}")
            if report is None:
                problems.append("rejected case wrote no report artifact")
            elif report.get("ok") is not False:
                problems.append("rejected case did not record ok=false")
            elif not _report_has_reason(report):
                problems.append("rejected case recorded no reason")
        if problems:
            all_passed = False
        cases.append(
            {
                "id": mutation.case_id,
                "description": mutation.description,
                "expected": "accept" if mutation.expect_ok else "reject",
                "expectedExit": mutation.expect_exit,
                "exit": completed.returncode,
                "reportOk": report.get("ok") if report is not None else None,
                "passed": not problems,
                "problems": ps.json_array(problems),
            }
        )

    for history_case in _history_guard_cases(script, work):
        if not history_case["passed"]:
            all_passed = False
        cases.append(history_case)

    report_obj: ps.JsonObject = {
        "artifactVersion": 1,
        "kind": "gksa23-migration-selftest",
        "assertion": "traceability-only",
        "e2eVerdict": None,
        "baselineCommit": ps.BASE_COMMIT,
        "repoRoot": str(repo_root),
        "candidateRoot": str(correct),
        "command": " ".join(
            [
                "python",
                "tests/tools/check_migration.py",
                "--self-test",
                "--repo-root",
                ".",
                "--output",
                str(output),
            ]
        ),
        "summary": {
            "total": len(cases),
            "passed": sum(1 for case in cases if case["passed"]),
            "failed": sum(1 for case in cases if not case["passed"]),
        },
        "cases": ps.json_array(cases),
        "note": (
            "Disposable migrated fixtures only. The real repository data is never "
            "modified; migration-map.json remains traceability only and no game "
            "scenario is executed (e2eVerdict null). Freeze and self-test require "
            "the exact base commit; an exported tree or shallow clone fails closed "
            "and can never become a baseline."
        ),
    }
    ps.write_report(output, report_obj)
    if all_passed:
        sys.stderr.write(f"RESULT: PASS (migration oracle self-test; {len(cases)} cases; disposable fixtures only)\n")
        return ps.EXIT_OK
    for case in cases:
        if case["passed"] is True:
            continue
        problems_value = case.get("problems")
        details = ""
        if isinstance(problems_value, list):
            details = "; ".join(item for item in problems_value if isinstance(item, str))
        sys.stderr.write(f"FAIL [{case['id']!r}] {details}\n")
    sys.stderr.write("RESULT: FAIL (migration oracle self-test)\n")
    return ps.EXIT_FAIL


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKSA-23 migration traceability oracle (test code)")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", default=None)
    parser.add_argument("--migration-map", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--freeze-baseline", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.freeze_baseline:
        repo_root = Path(args.repo_root).resolve()
        target = Path(args.baseline).resolve() if args.baseline else DEFAULT_BASELINE
        try:
            baseline = freeze_baseline(repo_root)
        except (MigrationInputError, ps.JsonLoadError) as exc:
            sys.stderr.write(
                "FAIL [history] freeze-baseline requires the exact base commit "
                f"{ps.BASE_COMMIT}; refusing to derive a baseline from the "
                f"working tree: {exc}\n"
            )
            return ps.EXIT_INPUT
        scenarios = ps.array_field(baseline, "scenarios", "frozen baseline")
        _write_json(target, baseline)
        sys.stderr.write(f"wrote frozen migration baseline: {target} ({len(scenarios)} scenarios)\n")
        return ps.EXIT_OK
    if args.self_test:
        return run_self_test(args)
    if not args.output:
        sys.stderr.write("FAIL [cli] --output is required\n")
        return ps.EXIT_INPUT
    return run_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
