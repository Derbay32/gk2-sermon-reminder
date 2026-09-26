#!/usr/bin/env python3
"""GKSA-23 v2 protocol black-box acceptance matrix.

Runs the production CLI (``tests/e2e/verify.py``) over disposable, synthetic v2
manifest and capture fixtures and compares observed behaviour with the settled
v2 contract. It never imports the production tool and never executes a game: all
evidence is a synthetic tool fixture and every report keeps ``e2eVerdict: null``.

Usage:
  python tests/tools/protocol_matrix.py --repo-root . \
      --output artifacts/gksa23-tests/protocol-matrix.json

Against the untouched v1 CLI the v2 cases are expected to be RED (the legacy
tool rejects the v2 protocol); the report records exactly which cases fail and
why so the failure is "unsupported v2", never a test-harness crash.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import protocol_support as ps

MANIFEST_KIND_REPORT = "gksr-manifest-check-result"
VALIDATION_KIND_REPORT = "gksr-validation-result"

SYNTHETIC_MANIFEST_ID = "gksr-synthetic-protocol"
ALPHA = "alpha"
BETA = "beta"
ALPHA_SCENARIO_ID = "alpha-flag-on"
BETA_SCENARIO_ID = "beta-final-text"
BASE_SCENARIO_IDS = (ALPHA_SCENARIO_ID, BETA_SCENARIO_ID)
REFERENCED_FILES = ("profiles.json", "scenarios/base.json")

HEX_A = "a" * 64
HEX_B = "b" * 64

SCENARIO_FILE = "scenarios/base.json"


# --------------------------------------------------------------------------
# synthetic v2 fixture content (tool-only; no game execution)
# --------------------------------------------------------------------------


def alpha_profile() -> ps.JsonObject:
    return {
        "baselineCommit": ps.BASE_COMMIT,
        "title": "synthetic alpha context",
        "status": "requirements-not-results",
        "specRefs": ["GKSA-23"],
        "finalTextSource": "synthetic-tool-fixture",
        "notes": ["Synthetic tool-only context; no game execution."],
        "finalText": {
            "gksr.synthetic.alphaFlag": {"en": "Flag on", "zh-CN": "标志开启"},
            "gksr.synthetic.one": {"en": "1 day", "zh-CN": "还剩1天"},
            "gksr.synthetic.other": {"en": "{days} days", "zh-CN": "还剩{days}天"},
        },
        "languageMapping": {"catalogLabelToRawGameLanguageId": {"en": "en", "zh-CN": "zh_cn"}},
        "observationVocabulary": {
            "v.flag": "bool: synthetic flag",
            "v.count": "int: synthetic count",
            "v.countdown": "int: synthetic countdown",
        },
        "validation": {
            "supportedCaptureVersions": [2],
            "requireLoadEvidenceLog": True,
            "measurementObservationPaths": ["m.alphaWidth", "m.alphaHeight"],
            "requiredEnvironmentPaths": [
                {"path": "game.name", "type": "string"},
                {"path": "game.sha256", "type": "sha256"},
                {"path": "capturedAt", "type": "timestamp"},
            ],
            "buildPluginHashConsistency": [["build.artifactSha256", "plugin.sha256"]],
        },
    }


def beta_profile() -> ps.JsonObject:
    profile = alpha_profile()
    profile["title"] = "synthetic beta context"
    profile["finalText"] = {
        "gksr.synthetic.beta": {"en": "Beta sentence", "zh-CN": "测试句子"},
        "gksr.synthetic.one": {"en": "1 day", "zh-CN": "还剩1天"},
        "gksr.synthetic.other": {"en": "{days} days", "zh-CN": "还剩{days}天"},
    }
    profile["validation"] = {
        "supportedCaptureVersions": [2],
        "requireLoadEvidenceLog": True,
        "measurementObservationPaths": ["m.betaWidth", "m.betaHeight"],
        "requiredEnvironmentPaths": [
            {"path": "game.name", "type": "string"},
            {"path": "game.sha256", "type": "sha256"},
            {"path": "capturedAt", "type": "timestamp"},
            {"path": "resolution.width", "type": "positive-int"},
        ],
        "buildPluginHashConsistency": [["build.artifactSha256", "plugin.sha256"]],
    }
    return profile


def base_index() -> ps.JsonObject:
    return {
        "kind": ps.INDEX_KIND,
        "manifestVersion": 2,
        "manifestId": SYNTHETIC_MANIFEST_ID,
        "profilesFile": "profiles.json",
        "scenarioRoot": "scenarios",
        "title": "GKSA-23 synthetic protocol fixture",
    }


def base_profiles() -> ps.JsonObject:
    return {"alpha": alpha_profile(), "beta": beta_profile()}


def alpha_scenario() -> ps.JsonObject:
    return {
        "id": ALPHA_SCENARIO_ID,
        "profileId": ALPHA,
        "requirements": ["GKSA-23"],
        "title": "synthetic alpha flag",
        "category": "synthetic",
        "prerequisites": ["Synthetic fixture only; no game."],
        "steps": ["Read the synthetic fixture."],
        "expect": [{"observation": "v.flag", "equals": True}],
        "evidence": {"screenshot": True, "log": True, "measurement": False},
    }


def beta_scenario() -> ps.JsonObject:
    return {
        "id": BETA_SCENARIO_ID,
        "profileId": BETA,
        "requirements": ["GKSA-23"],
        "title": "synthetic beta final text",
        "category": "synthetic",
        "prerequisites": ["Synthetic fixture only; no game."],
        "steps": ["Read the synthetic fixture."],
        "expect": [
            {
                "observation": "v.text",
                "equals": "Beta sentence",
                "finalTextKey": "gksr.synthetic.beta",
                "language": "en",
            }
        ],
        "evidence": {"screenshot": True, "log": False, "measurement": True},
    }


def base_scenario_file() -> ps.JsonObject:
    return {
        "kind": ps.SCENARIOS_KIND,
        "schemaVersion": 2,
        "scenarios": [alpha_scenario(), beta_scenario()],
    }


def base_env() -> ps.JsonObject:
    return {
        "game.name": "synthetic-fixture",
        "game.sha256": HEX_A,
        "capturedAt": "2026-01-02T03:04:05Z",
        "resolution.width": 1920,
        "resolution.height": 1080,
        "build.artifactSha256": HEX_B,
        "plugin.sha256": HEX_B,
    }


def base_observations() -> ps.JsonObject:
    return {
        ALPHA_SCENARIO_ID: {"v.flag": True},
        BETA_SCENARIO_ID: {"v.text": "Beta sentence"},
    }


def base_results() -> list[ps.JsonObject]:
    return [
        {
            "scenarioId": ALPHA_SCENARIO_ID,
            "status": "passed",
            "evidence": {
                "screenshot": "shots/alpha.png",
                "log": "logs/run.log",
            },
            "measurement": False,
        },
        {
            "scenarioId": BETA_SCENARIO_ID,
            "status": "passed",
            "evidence": {"screenshot": "shots/beta.png"},
            "measurement": {"values": {"m.betaWidth": 1.0}},
        },
    ]


DEFAULT_EVIDENCE = {
    "shots/alpha.png": "synthetic alpha screenshot bytes",
    "shots/beta.png": "synthetic beta screenshot bytes",
    "logs/run.log": "synthetic load log line\n",
}


def build_env(
    fixture: ps.Fixture,
    *,
    evidence: dict[str, str] | None = None,
    overrides: ps.JsonObject | None = None,
) -> ps.JsonObject:
    files = dict(DEFAULT_EVIDENCE) if evidence is None else dict(evidence)
    evidence_files: ps.JsonObject = {}
    for rel, content in files.items():
        evidence_files[rel] = ps.sha256_file(fixture.write_text(rel, content))
    env = base_env()
    env["evidenceFiles"] = evidence_files
    if overrides:
        env.update(overrides)
    return env


def write_capture(
    fixture: ps.Fixture,
    *,
    purpose: str = "tool-fixture",
    env: ps.JsonObject,
    observations: ps.JsonObject | None = None,
    results: list[ps.JsonObject] | None = None,
    fingerprint: str | None = None,
    capture_over: ps.JsonObject | None = None,
) -> None:
    result_items = ps.json_array(results or base_results())
    capture: ps.JsonObject = {
        "kind": ps.CAPTURE_KIND,
        "captureVersion": 2,
        "manifestId": SYNTHETIC_MANIFEST_ID,
        "manifestSha256": fingerprint or fixture.fingerprint(),
        "capturePurpose": purpose,
        "environment": env,
        "observations": observations or base_observations(),
        "results": result_items,
    }
    if capture_over:
        capture.update(capture_over)
        if "capturePurpose" in capture_over and capture_over["capturePurpose"] is None:
            capture.pop("capturePurpose", None)
    fixture.write_json("capture.json", capture)


def write_base(fixture: ps.Fixture) -> None:
    fixture.write_json("manifest.json", base_index())
    fixture.write_json("profiles.json", base_profiles())
    fixture.write_json(SCENARIO_FILE, base_scenario_file())


def setup_base(fixture: ps.Fixture) -> ps.CaseSetup:
    write_base(fixture)
    return ps.CaseSetup(
        expected_fingerprint=fixture.fingerprint(),
        expected_manifest_id=SYNTHETIC_MANIFEST_ID,
        expected_scenario_ids=sorted(BASE_SCENARIO_IDS),
        expected_files=sorted(REFERENCED_FILES),
    )


def setup_capture(
    fixture: ps.Fixture,
    *,
    purpose: str = "tool-fixture",
    env: ps.JsonObject | None = None,
    observations: ps.JsonObject | None = None,
    results: list[ps.JsonObject] | None = None,
    fingerprint: str | None = None,
    capture_over: ps.JsonObject | None = None,
) -> ps.CaseSetup:
    setup = setup_base(fixture)
    write_capture(
        fixture,
        purpose=purpose,
        env=env if env is not None else build_env(fixture),
        observations=observations,
        results=results,
        fingerprint=fingerprint,
        capture_over=capture_over,
    )
    return setup


# --------------------------------------------------------------------------
# case specification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    group: str
    description: str
    mode: str
    expect_exit: int
    mutate: Callable[[ps.Fixture], ps.CaseSetup]
    expect_exit_in: tuple[int, ...] | None = None
    allow_synthetic: bool = False
    expect_report: bool = True
    expect_kind: str | None = None
    expect_assertion: str | None = None
    check_fingerprint: bool = False
    check_scope: bool = False
    # Explicit null identity: an explicit missing input must never inherit the
    # identity of an unrelated sibling index, so manifestId/manifestSha256/scope
    # are asserted null rather than merely left unchecked.
    expect_null_metadata: bool = False
    # A rejection must carry this token in its scenario failure / error text.
    expect_failure_contains: str | None = None
    # An uncaught interpreter traceback is a crash, never a controlled rejection.
    forbid_traceback: bool = False


def case(
    *,
    case_id: str,
    group: str,
    description: str,
    mode: str,
    expect_exit: int,
    mutate: Callable[[ps.Fixture], ps.CaseSetup],
    expect_exit_in: tuple[int, ...] | None = None,
    allow_synthetic: bool = False,
    expect_report: bool = True,
    expect_kind: str | None = None,
    expect_assertion: str | None = None,
    check_fingerprint: bool = False,
    check_scope: bool = False,
    expect_null_metadata: bool = False,
    expect_failure_contains: str | None = None,
    forbid_traceback: bool = False,
) -> CaseSpec:
    return CaseSpec(
        case_id=case_id,
        group=group,
        description=description,
        mode=mode,
        expect_exit=expect_exit,
        mutate=mutate,
        expect_exit_in=expect_exit_in,
        allow_synthetic=allow_synthetic,
        expect_report=expect_report,
        expect_kind=expect_kind,
        expect_assertion=expect_assertion,
        check_fingerprint=check_fingerprint,
        check_scope=check_scope,
        expect_null_metadata=expect_null_metadata,
        expect_failure_contains=expect_failure_contains,
        forbid_traceback=forbid_traceback,
    )


# --------------------------------------------------------------------------
# mutation helpers
# --------------------------------------------------------------------------


def edit_index(fixture: ps.Fixture, **changes: ps.JsonValue | _Delete) -> ps.CaseSetup:
    setup_base(fixture)
    index = ps.as_object(fixture.load_json("manifest.json"), "manifest.json")
    for key, value in changes.items():
        if isinstance(value, _Delete):
            index.pop(key, None)
        else:
            index[key] = value
    fixture.write_json("manifest.json", index)
    return ps.CaseSetup()


def edit_profiles(fixture: ps.Fixture, mutator: Callable[[ps.JsonObject], None]) -> ps.CaseSetup:
    setup_base(fixture)
    profiles = ps.as_object(fixture.load_json("profiles.json"), "profiles.json")
    mutator(profiles)
    fixture.write_json("profiles.json", profiles)
    return ps.CaseSetup()


def edit_scenarios(fixture: ps.Fixture, mutator: Callable[[ps.JsonObject], None]) -> ps.CaseSetup:
    setup_base(fixture)
    scenario_file = ps.as_object(fixture.load_json(SCENARIO_FILE), SCENARIO_FILE)
    mutator(scenario_file)
    fixture.write_json(SCENARIO_FILE, scenario_file)
    return ps.CaseSetup()


class _Delete:
    pass


_DELETE = _Delete()


def _scenario_array(doc: ps.JsonObject, label: str = SCENARIO_FILE) -> ps.JsonArray:
    return ps.array_field(doc, "scenarios", label)


def _scenario_at(doc: ps.JsonObject, index: int, label: str = SCENARIO_FILE) -> ps.JsonObject:
    scenarios = _scenario_array(doc, label)
    return ps.as_object(scenarios[index], f"{label}.scenarios[{index}]")


def _first_expectation(scenario: ps.JsonObject, label: str = "scenario") -> ps.JsonObject:
    expect = ps.array_field(scenario, "expect", label)
    return ps.as_object(expect[0], f"{label}.expect[0]")


def _drop_scenario_key(scenario: ps.JsonObject, key: str) -> None:
    scenario.pop(key, None)


def _observation(observations: ps.JsonObject, scenario_id: str) -> ps.JsonObject:
    return ps.object_field(observations, scenario_id, "observations")


def _evidence_of(scenario: ps.JsonObject, label: str = "capture result") -> ps.JsonObject:
    return ps.object_field(scenario, "evidence", label)


def _evidence_files(env: ps.JsonObject) -> ps.JsonObject:
    return ps.object_field(env, "evidenceFiles", "capture environment")


# --------------------------------------------------------------------------
# mutation implementations
# --------------------------------------------------------------------------


def _mutate_profilesfile_escape(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    fixture.write_json("../profiles-escape.json", base_profiles())
    index = ps.as_object(fixture.load_json("manifest.json"), "manifest.json")
    index["profilesFile"] = "../profiles-escape.json"
    fixture.write_json("manifest.json", index)
    return ps.CaseSetup()


def _mutate_scenarioroot_escape(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    fixture.write_json("../scenarios-escape/base.json", base_scenario_file())
    index = ps.as_object(fixture.load_json("manifest.json"), "manifest.json")
    index["scenarioRoot"] = "../scenarios-escape"
    fixture.write_json("manifest.json", index)
    return ps.CaseSetup()


def _mutate_profiles_raw(fixture: ps.Fixture, text: str) -> ps.CaseSetup:
    setup_base(fixture)
    fixture.write_text("profiles.json", text)
    return ps.CaseSetup()


def _mutate_raw_scenario(fixture: ps.Fixture, text: str) -> ps.CaseSetup:
    setup_base(fixture)
    fixture.write_text(SCENARIO_FILE, text)
    return ps.CaseSetup()


def _mutate_unknown_profile(fixture: ps.Fixture) -> ps.CaseSetup:
    return edit_scenarios(fixture, lambda s: _scenario_at(s, 0).__setitem__("profileId", "ghost"))


def _mutate_scenario_file_kind_wrong(fixture: ps.Fixture) -> ps.CaseSetup:
    return edit_scenarios(fixture, lambda s: s.__setitem__("kind", ps.INDEX_KIND))


def _mutate_scenario_file_kind_missing(fixture: ps.Fixture) -> ps.CaseSetup:
    return edit_scenarios(fixture, lambda s: _drop_scenario_key(s, "kind"))


def _mutate_schema(fixture: ps.Fixture, value: ps.JsonValue) -> ps.CaseSetup:
    return edit_scenarios(fixture, lambda s: s.__setitem__("schemaVersion", value))


def _mutate_duplicate_global_id(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    duplicate = ps.as_object(fixture.load_json(SCENARIO_FILE), SCENARIO_FILE)
    fixture.write_json("scenarios/extra.json", duplicate)
    return ps.CaseSetup()


def _mutate_nonfinite(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    payload = (
        '{"kind":"gksr-e2e-scenarios","schemaVersion":2,"scenarios":['
        '{"id":"nan-scenario","profileId":"alpha","requirements":["GKSA-23"],'
        '"expect":[{"observation":"v.count","equals":NaN}]}]}'
    )
    fixture.write_text(SCENARIO_FILE, payload)
    return ps.CaseSetup()


def _mutate_overflow(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    payload = (
        '{"kind":"gksr-e2e-scenarios","schemaVersion":2,"scenarios":['
        '{"id":"overflow-scenario","profileId":"alpha",'
        '"requirements":["GKSA-23"],'
        '"expect":[{"observation":"v.count","equals":1e999}]}]}'
    )
    fixture.write_text(SCENARIO_FILE, payload)
    return ps.CaseSetup()


def _mutate_symlink_scenario(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    target = fixture.write_json("scenarios/real.json", base_scenario_file())
    fixture.path(SCENARIO_FILE).unlink()
    fixture.symlink(SCENARIO_FILE, target.name)
    return ps.CaseSetup()


def _mutate_symlink_profiles(fixture: ps.Fixture) -> ps.CaseSetup:
    setup_base(fixture)
    target = fixture.write_json("profiles-real.json", base_profiles())
    fixture.path("profiles.json").unlink()
    fixture.symlink("profiles.json", target.name)
    return ps.CaseSetup()


def _mutate_symlink_index(fixture: ps.Fixture) -> ps.CaseSetup:
    """The explicitly requested index file itself is a symlink.

    The link target is a complete, valid v2 index in the same directory, so the
    rejection must come from the requested index path being a symlink and not
    from the target being malformed. Unrelated OS ancestor aliases are out of
    scope: only the explicit index path and referenced paths are policy-checked.
    """
    setup_base(fixture)
    target = fixture.write_json("manifest-real.json", base_index())
    fixture.path("manifest.json").unlink()
    fixture.symlink("manifest.json", target.name)
    return ps.CaseSetup()


def _mutate_first_scenario(fixture: ps.Fixture, mutator: Callable[[ps.JsonObject], None]) -> ps.CaseSetup:
    return edit_scenarios(fixture, lambda s: mutator(_scenario_at(s, 0)))


def _setup_measurement_operator_observation(fixture: ps.Fixture) -> ps.CaseSetup:
    """A non-empty operatorObservation is accepted regardless of profile paths.

    ``validation.measurementObservationPaths`` is preserved context metadata in
    the v1 contract, not an enforced allow-list: the measurement is satisfied by
    a verified screenshot plus either a numeric ``values`` object or a non-empty
    ``operatorObservation`` string. This retains that legacy behaviour.
    """
    setup = setup_base(fixture)
    results = base_results()
    results[1]["measurement"] = {"operatorObservation": "m.alphaWidth"}
    write_capture(fixture, purpose="tool-fixture", env=build_env(fixture), results=results)
    return setup


_MANIFEST_NEGATIVES: list[CaseSpec] = [
    case(
        case_id="manifest-kind-legacy",
        group="manifest-structure",
        description="legacy gksa10-e2e-manifest kind is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, kind="gksa10-e2e-manifest"),
    ),
    case(
        case_id="manifest-kind-unknown",
        group="manifest-structure",
        description="unknown index kind is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, kind="gksr-e2e-index"),
    ),
    case(
        case_id="manifest-version-1",
        group="manifest-structure",
        description="manifestVersion 1 is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestVersion=1),
    ),
    case(
        case_id="manifest-version-bool",
        group="manifest-structure",
        description="boolean manifestVersion is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestVersion=True),
    ),
    case(
        case_id="manifest-version-float",
        group="manifest-structure",
        description="float manifestVersion is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestVersion=2.0),
    ),
    case(
        case_id="manifest-version-3",
        group="manifest-structure",
        description="manifestVersion 3 is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestVersion=3),
    ),
    case(
        case_id="manifest-id-missing",
        group="manifest-structure",
        description="missing manifestId is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestId=_DELETE),
    ),
    case(
        case_id="manifest-id-empty",
        group="manifest-structure",
        description="empty manifestId is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, manifestId=""),
    ),
    case(
        case_id="manifest-profilesfile-missing",
        group="manifest-structure",
        description="missing profilesFile is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, profilesFile=_DELETE),
    ),
    case(
        case_id="manifest-profilesfile-escape",
        group="manifest-path-safety",
        description="profilesFile escaping the index directory is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_profilesfile_escape,
    ),
    case(
        case_id="manifest-scenarioroot-missing",
        group="manifest-structure",
        description="missing scenarioRoot is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, scenarioRoot=_DELETE),
    ),
    case(
        case_id="manifest-scenarioroot-escape",
        group="manifest-path-safety",
        description="scenarioRoot escaping the index directory is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_scenarioroot_escape,
    ),
    case(
        case_id="manifest-profiles-missing-file",
        group="manifest-structure",
        description="profilesFile pointing to a missing file is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_index(fx, profilesFile="absent.json"),
    ),
    case(
        case_id="manifest-profiles-not-object",
        group="manifest-structure",
        description="non-object profiles file is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_profiles_raw(fx, "[]"),
    ),
    case(
        case_id="manifest-profiles-empty",
        group="manifest-structure",
        description="empty profiles map is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_profiles_raw(fx, "{}"),
    ),
    case(
        case_id="manifest-profile-unknown",
        group="manifest-structure",
        description="scenario referencing an unknown profileId is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_unknown_profile,
    ),
    case(
        case_id="manifest-scenario-file-kind-wrong",
        group="manifest-structure",
        description="scenario file with wrong kind is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_scenario_file_kind_wrong,
    ),
    case(
        case_id="manifest-scenario-file-kind-missing",
        group="manifest-structure",
        description="scenario file without kind is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_scenario_file_kind_missing,
    ),
    case(
        case_id="manifest-schema-1",
        group="manifest-structure",
        description="scenario schemaVersion 1 is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_schema(fx, 1),
    ),
    case(
        case_id="manifest-schema-bool",
        group="manifest-structure",
        description="boolean scenario schemaVersion is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_schema(fx, True),
    ),
    case(
        case_id="manifest-schema-float",
        group="manifest-structure",
        description="float scenario schemaVersion is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_schema(fx, 2.0),
    ),
    case(
        case_id="manifest-scenarios-empty",
        group="manifest-structure",
        description="empty scenarios collection is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: edit_scenarios(fx, lambda s: s.__setitem__("scenarios", [])),
    ),
    case(
        case_id="manifest-duplicate-global-id",
        group="manifest-structure",
        description="duplicate global scenario id across files is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_duplicate_global_id,
    ),
    case(
        case_id="manifest-malformed-json",
        group="manifest-json",
        description="malformed scenario JSON is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_raw_scenario(fx, "{not json"),
    ),
    case(
        case_id="manifest-duplicate-keys",
        group="manifest-json",
        description="duplicate JSON keys in a scenario file are rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_raw_scenario(
            fx,
            '{"kind":"gksr-e2e-scenarios","schemaVersion":2,"scenarios":[],"scenarios":[]}',
        ),
    ),
    case(
        case_id="manifest-profiles-duplicate-keys",
        group="manifest-json",
        description="duplicate JSON keys in profiles.json are rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_profiles_raw(fx, '{"alpha":{},"alpha":{}}'),
    ),
    case(
        case_id="manifest-nonfinite-nan",
        group="manifest-json",
        description="NaN in a scenario file is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_nonfinite,
    ),
    case(
        case_id="manifest-nonfinite-infinity",
        group="manifest-json",
        description="Infinity in the profiles file is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_profiles_raw(fx, '{"alpha":{"validation":{"x":Infinity}}}'),
    ),
    case(
        case_id="manifest-nonfinite-overflow",
        group="manifest-json",
        description=(
            "numeric overflow (1e999) in a scenario file is rejected without crashing the strict independent parser"
        ),
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_overflow,
    ),
    case(
        case_id="manifest-symlink-scenario",
        group="manifest-path-safety",
        description="symlinked scenario file is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_symlink_scenario,
    ),
    case(
        case_id="manifest-symlink-profiles",
        group="manifest-path-safety",
        description="symlinked profilesFile is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_symlink_profiles,
    ),
    case(
        case_id="manifest-symlink-index",
        group="manifest-path-safety",
        description="symlinked requested index file is rejected even when its target is valid",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=_mutate_symlink_index,
    ),
    case(
        case_id="manifest-scenario-missing-id",
        group="manifest-structure",
        description="scenario without an id is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_first_scenario(fx, lambda s: _drop_scenario_key(s, "id")),
    ),
    case(
        case_id="manifest-scenario-missing-profile",
        group="manifest-structure",
        description="scenario without profileId is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_first_scenario(fx, lambda s: _drop_scenario_key(s, "profileId")),
    ),
    case(
        case_id="manifest-scenario-missing-requirements",
        group="manifest-structure",
        description="scenario without requirements is rejected",
        mode="manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=MANIFEST_KIND_REPORT,
        mutate=lambda fx: _mutate_first_scenario(fx, lambda s: _drop_scenario_key(s, "requirements")),
    ),
]


# --------------------------------------------------------------------------
# capture cases
# --------------------------------------------------------------------------


def capture_positive(fixture: ps.Fixture) -> ps.CaseSetup:
    return setup_capture(fixture, purpose="tool-fixture")


def capture_game_observation(fixture: ps.Fixture) -> ps.CaseSetup:
    return setup_capture(fixture, purpose="game-observation")


def capture_missing(fixture: ps.Fixture) -> ps.CaseSetup:
    setup = setup_base(fixture)
    setup.capture_rel = "absent.json"
    return setup


def hetero_setup(fixture: ps.Fixture, *, observed_text: str) -> ps.CaseSetup:
    profiles = base_profiles()
    alpha_final = ps.object_field(
        ps.object_field(profiles, ALPHA, "profiles"),
        "finalText",
        f"profiles.{ALPHA}",
    )
    alpha_final["gksr.synthetic.shared"] = {
        "en": "Alpha shared",
        "zh-CN": "共享甲",
    }
    beta_final = ps.object_field(
        ps.object_field(profiles, BETA, "profiles"),
        "finalText",
        f"profiles.{BETA}",
    )
    beta_final["gksr.synthetic.shared"] = {
        "en": "Beta shared",
        "zh-CN": "共享乙",
    }
    scenario: ps.JsonObject = {
        "id": "beta-shared-text",
        "profileId": "beta",
        "requirements": ["GKSA-23"],
        "title": "synthetic per-profile finalText",
        "category": "synthetic",
        "prerequisites": ["Synthetic fixture only; no game."],
        "steps": ["Read the synthetic fixture."],
        "expect": [
            {
                "observation": "v.text",
                "equals": observed_text,
                "finalTextKey": "gksr.synthetic.shared",
                "language": "en",
            }
        ],
        "evidence": {"screenshot": True, "log": False, "measurement": False},
    }
    fixture.write_json("manifest.json", base_index())
    fixture.write_json("profiles.json", profiles)
    fixture.write_json(
        "scenarios/base.json",
        {
            "kind": ps.SCENARIOS_KIND,
            "schemaVersion": 2,
            "scenarios": [scenario],
        },
    )
    observations: ps.JsonObject = {"beta-shared-text": {"v.text": observed_text}}
    results: list[ps.JsonObject] = [
        {
            "scenarioId": "beta-shared-text",
            "status": "passed",
            "evidence": {"screenshot": "shots/beta.png"},
            "measurement": False,
        }
    ]
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        observations=observations,
        results=results,
    )
    return ps.CaseSetup(
        expected_fingerprint=fixture.fingerprint(),
        expected_manifest_id=SYNTHETIC_MANIFEST_ID,
        expected_scenario_ids=["beta-shared-text"],
        expected_files=["profiles.json", "scenarios/base.json"],
    )


def datecheck_setup(fixture: ps.Fixture, *, countdown: int = 1) -> ps.CaseSetup:
    fixture.write_json("manifest.json", base_index())
    fixture.write_json("profiles.json", {"alpha": alpha_profile()})
    scenario: ps.JsonObject = {
        "id": "alpha-countdown",
        "profileId": "alpha",
        "requirements": ["GKSA-23"],
        "title": "synthetic retained dateCheck",
        "category": "synthetic",
        "prerequisites": ["Synthetic fixture only; no game."],
        "steps": ["Read the synthetic fixture."],
        "expect": [
            {
                "dateCheck": {
                    "mode": "single-day",
                    "absoluteDay": "save.absoluteDay",
                    "dayOfWeek": "save.dayOfWeek",
                    "countdown": "v.countdown",
                    "target": "save.sermonWeekday",
                    "week": "save.daysInWeek",
                }
            }
        ],
        "evidence": {"screenshot": True, "log": False, "measurement": False},
    }
    fixture.write_json(
        "scenarios/base.json",
        {
            "kind": ps.SCENARIOS_KIND,
            "schemaVersion": 2,
            "scenarios": [scenario],
        },
    )
    observations: ps.JsonObject = {
        "alpha-countdown": {
            "save.absoluteDay": 2,
            "save.dayOfWeek": 2,
            "save.sermonWeekday": 3,
            "save.daysInWeek": 6,
            "v.countdown": countdown,
        }
    }
    results: list[ps.JsonObject] = [
        {
            "scenarioId": "alpha-countdown",
            "status": "passed",
            "evidence": {"screenshot": "shots/alpha.png"},
            "measurement": False,
        }
    ]
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        observations=observations,
        results=results,
    )
    return ps.CaseSetup(
        expected_fingerprint=fixture.fingerprint(),
        expected_manifest_id=SYNTHETIC_MANIFEST_ID,
        expected_scenario_ids=["alpha-countdown"],
        expected_files=["profiles.json", "scenarios/base.json"],
    )


NUMERIC_SCENARIO_ID = "alpha-numeric"


def numeric_setup(
    fixture: ps.Fixture,
    *,
    condition: ps.JsonObject,
    observations: ps.JsonObject,
) -> ps.CaseSetup:
    """A valid single-scenario synthetic capture for one numeric expectation.

    The fixture mirrors the real ``hud/sermon-icon.json`` shape: a measurement
    scenario whose screenshot is verified and whose execution record carries a
    numeric measurement, so the guard under test is the observed-value check and
    never a missing-evidence rejection. The measurement values mirror the
    observed numbers so the same fixture exercises both a huge integer and a
    plain ratio control.
    """
    fixture.write_json("manifest.json", base_index())
    fixture.write_json("profiles.json", {"alpha": alpha_profile()})
    scenario: ps.JsonObject = {
        "id": NUMERIC_SCENARIO_ID,
        "profileId": ALPHA,
        "requirements": ["GKSA-23"],
        "title": "synthetic numeric guard",
        "category": "synthetic",
        "prerequisites": ["Synthetic fixture only; no game."],
        "steps": ["Read the synthetic fixture."],
        "expect": [condition],
        "evidence": {"screenshot": True, "log": False, "measurement": True},
    }
    fixture.write_json(
        SCENARIO_FILE,
        {"kind": ps.SCENARIOS_KIND, "schemaVersion": 2, "scenarios": [scenario]},
    )
    results: list[ps.JsonObject] = [
        {
            "scenarioId": NUMERIC_SCENARIO_ID,
            "status": "passed",
            "evidence": {"screenshot": "shots/alpha.png"},
            "measurement": {"values": observations},
        }
    ]
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        observations={NUMERIC_SCENARIO_ID: observations},
        results=results,
    )
    return ps.CaseSetup(
        expected_fingerprint=fixture.fingerprint(),
        expected_manifest_id=SYNTHETIC_MANIFEST_ID,
        expected_scenario_ids=[NUMERIC_SCENARIO_ID],
        expected_files=["profiles.json", "scenarios/base.json"],
    )


def _mutate_capture_purpose(fixture: ps.Fixture, purpose: ps.JsonValue) -> ps.CaseSetup:
    setup = setup_base(fixture)
    env = build_env(fixture)
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=env,
        capture_over={"capturePurpose": purpose},
    )
    return setup


def _mutate_capture_field(fixture: ps.Fixture, key: str, value: ps.JsonValue) -> ps.CaseSetup:
    setup = setup_base(fixture)
    env = build_env(fixture)
    write_capture(fixture, purpose="tool-fixture", env=env, capture_over={key: value})
    return setup


def _mutate_capture_results(fixture: ps.Fixture, mutator: Callable[[list[ps.JsonObject]], object]) -> ps.CaseSetup:
    setup = setup_base(fixture)
    results = base_results()
    mutator(results)
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        results=results,
    )
    return setup


def _mutate_capture_observations(fixture: ps.Fixture, mutator: Callable[[ps.JsonObject], object]) -> ps.CaseSetup:
    setup = setup_base(fixture)
    observations = base_observations()
    mutator(observations)
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        observations=observations,
    )
    return setup


def _mutate_capture_env(fixture: ps.Fixture, mutator: Callable[[ps.JsonObject], object]) -> ps.CaseSetup:
    setup = setup_base(fixture)
    env = build_env(fixture)
    mutator(env)
    write_capture(fixture, purpose="tool-fixture", env=env)
    return setup


def _mutate_stale_hash(fixture: ps.Fixture) -> ps.CaseSetup:
    setup = setup_base(fixture)
    stale = setup.expected_fingerprint or ""
    scenario_file = ps.as_object(fixture.load_json(SCENARIO_FILE), SCENARIO_FILE)
    _first_expectation(_scenario_at(scenario_file, 0))["equals"] = False
    fixture.write_json(SCENARIO_FILE, scenario_file)
    write_capture(
        fixture,
        purpose="tool-fixture",
        env=build_env(fixture),
        fingerprint=stale,
    )
    return setup


_CAPTURE_CASES: list[CaseSpec] = [
    case(
        case_id="capture-synthetic-success",
        group="capture-positive",
        description="two-scenario tool-fixture capture passes with --allow-synthetic",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=capture_positive,
    ),
    case(
        case_id="capture-game-observation-success",
        group="capture-positive",
        description="game-observation purpose capture passes without opt-in",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=False,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="captured-observations",
        check_fingerprint=True,
        check_scope=True,
        mutate=capture_game_observation,
    ),
    case(
        case_id="capture-legacy-kind",
        group="capture-protocol",
        description="legacy gksa capture kind is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "kind", "gksa10-e2e-capture"),
    ),
    case(
        case_id="capture-version-1",
        group="capture-protocol",
        description="captureVersion 1 is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "captureVersion", 1),
    ),
    case(
        case_id="capture-version-bool",
        group="capture-protocol",
        description="boolean captureVersion is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "captureVersion", True),
    ),
    case(
        case_id="capture-version-float",
        group="capture-protocol",
        description="float captureVersion is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "captureVersion", 2.0),
    ),
    case(
        case_id="capture-version-3",
        group="capture-protocol",
        description="captureVersion 3 is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "captureVersion", 3),
    ),
    case(
        case_id="capture-manifest-id-mismatch",
        group="capture-identity",
        description="capture manifestId mismatch is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "manifestId", "other-manifest"),
    ),
    case(
        case_id="capture-manifest-hash-mismatch",
        group="capture-identity",
        description="capture manifestSha256 mismatch is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_field(fx, "manifestSha256", "0" * 64),
    ),
    case(
        case_id="capture-stale-hash-unchanged-index",
        group="capture-identity",
        description="changed referenced scenario with unchanged index/hash is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=_mutate_stale_hash,
    ),
    case(
        case_id="capture-purpose-example",
        group="capture-purpose",
        description="example capture purpose is always rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_purpose(fx, "example"),
    ),
    case(
        case_id="capture-purpose-example-with-flag",
        group="capture-purpose",
        description="example purpose is rejected even with --allow-synthetic",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_purpose(fx, "example"),
    ),
    case(
        case_id="capture-purpose-tool-fixture-no-flag",
        group="capture-purpose",
        description="tool-fixture purpose is rejected without --allow-synthetic",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        allow_synthetic=False,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_purpose(fx, "tool-fixture"),
    ),
    case(
        case_id="capture-purpose-missing",
        group="capture-purpose",
        description="missing capturePurpose is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_purpose(fx, None),
    ),
    case(
        case_id="capture-purpose-invalid",
        group="capture-purpose",
        description="unknown capturePurpose is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_purpose(fx, "synthetic"),
    ),
    case(
        case_id="capture-result-unknown-id",
        group="capture-coverage",
        description="execution record for an unknown scenario id is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(
            fx,
            lambda rs: rs.append(
                {
                    "scenarioId": "ghost-scenario",
                    "status": "passed",
                    "evidence": {"screenshot": "shots/alpha.png"},
                    "measurement": False,
                }
            ),
        ),
    ),
    case(
        case_id="capture-observation-unknown-id",
        group="capture-coverage",
        description="observation block for an unknown scenario id is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_observations(
            fx, lambda obs: obs.__setitem__("ghost-scenario", {"v.flag": True})
        ),
    ),
    case(
        case_id="capture-result-duplicate-id",
        group="capture-coverage",
        description="duplicate execution records are rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(fx, lambda rs: rs.append(dict(rs[0]))),
    ),
    case(
        case_id="capture-missing-result",
        group="capture-coverage",
        description="unexecuted scenario is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(fx, lambda rs: rs.pop()),
    ),
    case(
        case_id="capture-status-skipped",
        group="capture-coverage",
        description="skipped scenario status is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(fx, lambda rs: rs[1].__setitem__("status", "skipped")),
    ),
    case(
        case_id="capture-status-unexecuted",
        group="capture-coverage",
        description="UNEXECUTED scenario status is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(fx, lambda rs: rs[1].__setitem__("status", "UNEXECUTED")),
    ),
    case(
        case_id="capture-wrong-observation-value",
        group="capture-assertion",
        description="wrong observation value is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_observations(
            fx,
            lambda obs: _observation(obs, ALPHA_SCENARIO_ID).__setitem__("v.flag", False),
        ),
    ),
    case(
        case_id="capture-bad-observation-type",
        group="capture-assertion",
        description="strict JSON type mismatch (int for boolean) is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_observations(
            fx,
            lambda obs: _observation(obs, ALPHA_SCENARIO_ID).__setitem__("v.flag", 1),
        ),
    ),
    case(
        case_id="capture-final-text-mismatch",
        group="capture-assertion",
        description="finalText mismatch is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_observations(
            fx,
            lambda obs: _observation(obs, BETA_SCENARIO_ID).__setitem__("v.text", "Wrong sentence"),
        ),
    ),
    case(
        case_id="capture-missing-evidence-category",
        group="capture-evidence",
        description="missing required evidence category is rejected",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_results(fx, lambda rs: _evidence_of(rs[0]).pop("log", None)),
    ),
    case(
        case_id="capture-evidence-hash-mismatch",
        group="capture-evidence",
        description="evidence file hash mismatch is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_env(
            fx,
            lambda env: _evidence_files(env).__setitem__("shots/alpha.png", "0" * 64),
        ),
    ),
    case(
        case_id="capture-evidence-missing-file",
        group="capture-evidence",
        description="missing evidence file is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_env(
            fx,
            lambda env: _evidence_files(env).__setitem__("shots/ghost.png", HEX_A),
        ),
    ),
    case(
        case_id="capture-evidence-path-escape",
        group="capture-evidence",
        description="evidence path escaping the capture directory is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_env(
            fx,
            lambda env: _evidence_files(env).__setitem__("../escape.png", HEX_A),
        ),
    ),
    case(
        case_id="capture-load-log-missing",
        group="capture-evidence",
        description="load log required but no log-like evidence file is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_env(
            fx, lambda env: env.__setitem__("evidenceFiles", {"shots/only.png": HEX_A})
        ),
    ),
    case(
        case_id="capture-profile-rule-weakened",
        group="capture-profile-rules",
        description="heterogeneous profile environment rule cannot be weakened",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: _mutate_capture_env(fx, lambda env: env.pop("resolution.width")),
    ),
    case(
        case_id="capture-measurement-cross-profile",
        group="capture-profile-rules",
        description=(
            "retained measurement semantics: a non-empty operatorObservation text "
            "satisfies a measurement scenario even when it names another profile's "
            "path (measurementObservationPaths is preserved metadata, not an "
            "enforced allow-list)"
        ),
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=_setup_measurement_operator_observation,
    ),
    case(
        case_id="capture-missing-file",
        group="capture-protocol",
        description="missing capture input is rejected fail-closed",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=capture_missing,
    ),
    case(
        case_id="capture-date-check-success",
        group="capture-retained",
        description="retained dateCheck condition passes for delta 1",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=lambda fx: datecheck_setup(fx, countdown=1),
    ),
    case(
        case_id="capture-date-check-wrong-countdown",
        group="capture-retained",
        description="retained dateCheck rejects a wrong countdown",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: datecheck_setup(fx, countdown=2),
    ),
    case(
        case_id="capture-profile-context-effective",
        group="capture-profile-rules",
        description="scenario uses its own profile finalText",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=lambda fx: hetero_setup(fx, observed_text="Beta shared"),
    ),
    case(
        case_id="capture-profile-context-not-merged",
        group="capture-profile-rules",
        description="a sibling profile finalText cannot satisfy the scenario",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: hetero_setup(fx, observed_text="Alpha shared"),
    ),
]


# --------------------------------------------------------------------------
# numeric guard regressions (old verify._finite_number semantics)
# --------------------------------------------------------------------------
#
# Failure paths enumerated before these assertions were written:
#   ratio-huge-numerator    measuredRatio numerator 10**400, denominator 1
#                           -> controlled exit 1 with a written report naming a
#                              non-finite measurement; never an uncaught
#                              OverflowError traceback (current behaviour: crash)
#   ratio-huge-denominator  measuredRatio denominator 10**400
#                           -> controlled exit 1 naming a non-finite measurement
#                              (current behaviour: rejected only incidentally by a
#                              ratio-mismatch message, not by the finite guard)
#   number-range-huge       numberRange min 0 with observed 10**400
#                           -> controlled exit 1 naming a non-finite number
#                              (current behaviour: accepted, exit 0)
#   ratio-valid-20-18       measuredRatio 20/18 still passes (positive control)
#   integer-equality-huge   strict equals 10**400 still passes (positive control
#                           proving plain JSON integers are not globally banned)
_NUMERIC_CASES: list[CaseSpec] = [
    case(
        case_id="capture-ratio-huge-numerator",
        group="capture-numeric-guard",
        description=(
            "measuredRatio numerator 10**400 with denominator 1 fails closed with "
            "a written report, not an uncaught overflow traceback"
        ),
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_failure_contains="finite",
        forbid_traceback=True,
        mutate=lambda fx: numeric_setup(
            fx,
            condition={
                "observation": "m.width",
                "measuredRatio": {"numerator": "m.width", "denominator": "m.height"},
            },
            observations={"m.width": 10**400, "m.height": 1},
        ),
    ),
    case(
        case_id="capture-ratio-huge-denominator",
        group="capture-numeric-guard",
        description=(
            "measuredRatio denominator 10**400 is rejected as a non-finite "
            "measurement rather than incidentally by a ratio mismatch"
        ),
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_failure_contains="finite",
        forbid_traceback=True,
        mutate=lambda fx: numeric_setup(
            fx,
            condition={
                "observation": "m.width",
                "measuredRatio": {"numerator": "m.width", "denominator": "m.height"},
            },
            observations={"m.width": 1, "m.height": 10**400},
        ),
    ),
    case(
        case_id="capture-number-range-huge",
        group="capture-numeric-guard",
        description="numberRange min 0 rejects an observed 10**400 as non-finite",
        mode="capture",
        expect_exit=ps.EXIT_FAIL,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_failure_contains="finite",
        forbid_traceback=True,
        mutate=lambda fx: numeric_setup(
            fx,
            condition={"observation": "v.numeric", "numberRange": {"min": 0}},
            observations={"v.numeric": 10**400},
        ),
    ),
    case(
        case_id="capture-ratio-valid-20-18",
        group="capture-numeric-guard",
        description="the retained 20:18 measuredRatio control still passes",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=lambda fx: numeric_setup(
            fx,
            condition={
                "observation": "m.width",
                "measuredRatio": {"numerator": "m.width", "denominator": "m.height"},
            },
            observations={"m.width": 20, "m.height": 18},
        ),
    ),
    case(
        case_id="capture-integer-equality-huge",
        group="capture-numeric-guard",
        description="a strict integer equals 10**400 still passes (integers are not globally banned)",
        mode="capture",
        expect_exit=ps.EXIT_OK,
        allow_synthetic=True,
        expect_kind=VALIDATION_KIND_REPORT,
        expect_assertion="synthetic-cli-only",
        check_fingerprint=True,
        check_scope=True,
        mutate=lambda fx: numeric_setup(
            fx,
            condition={"observation": "v.numeric", "equals": 10**400},
            observations={"v.numeric": 10**400},
        ),
    ),
]


# --------------------------------------------------------------------------
# CLI-wiring cases
# --------------------------------------------------------------------------


def cli_both_modes(fixture: ps.Fixture) -> ps.CaseSetup:
    return setup_capture(fixture)


def cli_plain(fixture: ps.Fixture) -> ps.CaseSetup:
    return setup_base(fixture)


def cli_missing_manifest(fixture: ps.Fixture) -> ps.CaseSetup:
    """An explicitly requested, missing index beside a valid sibling index.

    The sibling ``manifest.json`` is a complete valid v2 index, but it is not the
    requested input and must never lend its identity to the report. No expected
    identity is returned here: the corrected case asserts explicit null metadata
    instead of the sibling's manifestId.
    """
    setup_base(fixture)
    sibling = ps.as_object(fixture.load_json("manifest.json"), "manifest.json")
    if sibling.get("manifestId") != SYNTHETIC_MANIFEST_ID:
        raise ps.JsonLoadError("missing-manifest fixture sibling index is not the valid synthetic index")
    return ps.CaseSetup()


_CLI_CASES: list[CaseSpec] = [
    case(
        case_id="cli-both-modes",
        group="cli-wiring",
        description="structure check combined with capture is rejected",
        mode="both",
        expect_exit=ps.EXIT_INPUT,
        expect_report=False,
        mutate=cli_both_modes,
    ),
    case(
        case_id="cli-no-mode",
        group="cli-wiring",
        description="neither structure check nor capture is rejected",
        mode="none",
        expect_exit=ps.EXIT_INPUT,
        expect_report=False,
        mutate=cli_plain,
    ),
    case(
        case_id="cli-unknown-option",
        group="cli-wiring",
        description="no subset selector option is accepted",
        mode="unknown-option",
        expect_exit=ps.EXIT_INPUT,
        expect_report=False,
        mutate=cli_plain,
    ),
    case(
        case_id="cli-missing-output",
        group="cli-wiring",
        description="missing required --output is rejected",
        mode="no-output",
        expect_exit=ps.EXIT_INPUT,
        expect_report=False,
        mutate=cli_plain,
    ),
    case(
        case_id="cli-missing-manifest",
        group="cli-wiring",
        description=(
            "explicitly missing manifest input is rejected fail-closed with null "
            "identity even beside a valid unrelated sibling index"
        ),
        mode="missing-manifest",
        expect_exit=ps.EXIT_INPUT,
        expect_report=True,
        expect_kind=MANIFEST_KIND_REPORT,
        expect_null_metadata=True,
        mutate=cli_missing_manifest,
    ),
]


ALL_CASES: list[CaseSpec] = _MANIFEST_NEGATIVES + _CAPTURE_CASES + _NUMERIC_CASES + _CLI_CASES


def positive_cases() -> list[CaseSpec]:
    return [
        case(
            case_id="manifest-valid-v2",
            group="manifest-positive",
            description="valid v2 index is accepted as structure-only",
            mode="manifest",
            expect_exit=ps.EXIT_OK,
            expect_kind=MANIFEST_KIND_REPORT,
            expect_assertion="manifest-structure-only",
            check_fingerprint=True,
            check_scope=True,
            mutate=setup_base,
        )
    ]


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def build_argv(spec: CaseSpec, fixture: ps.Fixture, setup: ps.CaseSetup) -> list[str]:
    manifest = str(fixture.path("manifest.json"))
    output = str(fixture.path("report.json"))
    capture = str(fixture.path(setup.capture_rel))
    if spec.mode == "manifest":
        args = ["--manifest", manifest, "--check-manifest", "--output", output]
    elif spec.mode == "capture":
        args = ["--manifest", manifest, "--capture", capture, "--output", output]
    elif spec.mode == "both":
        args = [
            "--manifest",
            manifest,
            "--check-manifest",
            "--capture",
            capture,
            "--output",
            output,
        ]
    elif spec.mode == "none":
        args = ["--manifest", manifest, "--output", output]
    elif spec.mode == "unknown-option":
        args = [
            "--manifest",
            manifest,
            "--check-manifest",
            "--output",
            output,
            "--scenario",
            "alpha-flag-on",
        ]
    elif spec.mode == "no-output":
        args = ["--manifest", manifest, "--check-manifest"]
    elif spec.mode == "missing-manifest":
        args = [
            "--manifest",
            str(fixture.path("absent.json")),
            "--check-manifest",
            "--output",
            output,
        ]
    else:  # pragma: no cover - guarded by construction
        raise ValueError(f"unknown case mode {spec.mode!r}")
    if spec.allow_synthetic:
        args.append(ps.ALLOW_SYNTHETIC_FLAG)
    return args


def evaluate(spec: CaseSpec, setup: ps.CaseSetup, run: ps.CliRun) -> ps.CaseResult:
    problems: list[str] = []
    allowed_exits = spec.expect_exit_in or (spec.expect_exit,)
    if run.exit not in allowed_exits:
        problems.append(f"exit {run.exit} not in {list(allowed_exits)}; stderr={run.stderr.strip()[:300]!r}")

    report_object = run.report if isinstance(run.report, dict) else None
    if spec.expect_report:
        if run.report_error is not None:
            problems.append(f"report is not valid JSON: {run.report_error}")
        elif report_object is None:
            problems.append("no report artifact written")
        else:
            version = report_object.get("artifactVersion")
            if not ps.is_int(version) or version != 2:
                problems.append(f"artifactVersion {version!r} is not the integer 2")
            if "e2eVerdict" not in report_object:
                problems.append("report is missing the e2eVerdict key")
            elif report_object["e2eVerdict"] is not None:
                problems.append("e2eVerdict must be null in tool reports")
            if spec.expect_null_metadata:
                for key in ("manifestId", "manifestSha256", "scope"):
                    if key not in report_object:
                        problems.append(f"explicit missing input must report {key}=null; the key is omitted")
                        continue
                    value = report_object[key]
                    if value is not None:
                        problems.append(f"explicit missing input must report {key}=null; got {value!r}")
            if run.exit == ps.EXIT_OK:
                if report_object.get("ok") is not True:
                    problems.append("expected ok=true on success")
            elif report_object.get("ok") is not False:
                problems.append("expected ok=false on rejection")
            if spec.expect_kind is not None and (report_object.get("kind") != spec.expect_kind):
                problems.append(f"kind {report_object.get('kind')!r} != {spec.expect_kind!r}")
            if spec.expect_assertion is not None and (report_object.get("assertion") != spec.expect_assertion):
                problems.append(f"assertion {report_object.get('assertion')!r} != {spec.expect_assertion!r}")
            if (
                not spec.expect_null_metadata
                and setup.expected_manifest_id is not None
                and (report_object.get("manifestId") != setup.expected_manifest_id)
            ):
                problems.append(
                    f"report manifestId {report_object.get('manifestId')!r} != {setup.expected_manifest_id!r}"
                )
            if spec.check_fingerprint:
                if report_object.get("manifestSha256") != setup.expected_fingerprint:
                    problems.append(
                        f"manifestSha256 does not match the independent fingerprint {setup.expected_fingerprint}"
                    )
            if spec.check_scope:
                problems.extend(_scope_problems(report_object, setup))
            if spec.expect_failure_contains is not None and not any(
                spec.expect_failure_contains in text for text in _failure_texts(report_object)
            ):
                problems.append(f"rejection reason does not mention {spec.expect_failure_contains!r}")
            if run.exit != ps.EXIT_OK and not _has_rejection_reason(run, report_object):
                problems.append("rejection carries no diagnostic reason in stderr or report")
    if spec.forbid_traceback and "Traceback" in run.stderr:
        problems.append("CLI stderr contains an uncaught interpreter traceback")

    passed = not problems
    evidence: ps.JsonObject = {
        "observedExit": run.exit,
        "expectedExits": list(allowed_exits),
        "reportKind": report_object.get("kind") if report_object is not None else None,
        "reportOk": report_object.get("ok") if report_object is not None else None,
        "stderr": run.stderr.strip()[:500],
    }
    return ps.CaseResult(
        case_id=spec.case_id,
        group=spec.group,
        description=spec.description,
        mode=spec.mode,
        expected_exit=spec.expect_exit,
        actual_exit=run.exit,
        command=run.command(),
        passed=passed,
        red=not passed,
        problems=problems,
        evidence=evidence,
    )


def _failure_texts(report: ps.JsonObject) -> list[str]:
    """Every human-readable rejection reason a capture report carries."""
    texts: list[str] = []
    scenarios = report.get("scenarios")
    if isinstance(scenarios, list):
        for entry in scenarios:
            if not isinstance(entry, dict):
                continue
            failures = entry.get("failures")
            if isinstance(failures, list):
                texts.extend(str(item) for item in failures)
    error = report.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            texts.append(message)
    elif isinstance(error, str):
        texts.append(error)
    return texts


def _has_rejection_reason(run: ps.CliRun, report: ps.JsonObject) -> bool:
    """A rejection must expose a diagnostic, but its exact wording is free."""
    if run.stderr.strip():
        return True
    for key in ("message", "reason", "detail", "error"):
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("inputErrors", "failures", "errors", "problems"):
        value = report.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return True
    return False


def _sorted_strings(value: ps.JsonValue) -> list[str] | None:
    if not isinstance(value, list):
        return None
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        strings.append(item)
    return sorted(strings)


def _scope_problems(report: ps.JsonObject, setup: ps.CaseSetup) -> list[str]:
    problems: list[str] = []
    scope = report.get("scope")
    if not isinstance(scope, dict):
        return ["report scope is missing"]
    raw_ids = scope.get("scenarioIds")
    if _sorted_strings(raw_ids) != setup.expected_scenario_ids:
        problems.append(f"scope.scenarioIds {raw_ids!r} != {setup.expected_scenario_ids!r}")
    if scope.get("scenarioCount") != len(setup.expected_scenario_ids):
        problems.append(f"scope.scenarioCount {scope.get('scenarioCount')!r} != {len(setup.expected_scenario_ids)}")
    raw_files = scope.get("files")
    if _sorted_strings(raw_files) != setup.expected_files:
        problems.append(f"scope.files {raw_files!r} != {setup.expected_files!r}")
    return problems


def run_spec(spec: CaseSpec, cli_path: Path, work_root: Path) -> tuple[ps.CaseResult, ps.CliRun]:
    fixture = ps.Fixture(work_root / spec.case_id)
    shutil.rmtree(fixture.root, ignore_errors=True)
    fixture.root.mkdir(parents=True, exist_ok=True)
    setup = spec.mutate(fixture)
    args = build_argv(spec, fixture, setup)
    output = fixture.path("report.json")
    run = ps.run_cli(cli_path, args, fixture.root, output)
    return evaluate(spec, setup, run), run


# --------------------------------------------------------------------------
# independent fingerprint oracle cases
# --------------------------------------------------------------------------


def _manifest_check(fixture: ps.Fixture, cli_path: Path) -> ps.CliRun:
    output = fixture.path("report.json")
    args = [
        "--manifest",
        str(fixture.path("manifest.json")),
        "--check-manifest",
        "--output",
        str(output),
    ]
    return ps.run_cli(cli_path, args, fixture.root, output)


def _oracle_result(
    case_id: str,
    description: str,
    problems: list[str],
    *,
    expected_fingerprint: str | None = None,
    observed_fingerprint: str | None = None,
    command: str = "",
) -> ps.CaseResult:
    evidence: ps.JsonObject = {
        "oracleFingerprint": observed_fingerprint,
        "expectedFingerprint": expected_fingerprint,
        "stderr": "",
    }
    return ps.CaseResult(
        case_id=case_id,
        group="fingerprint-oracle",
        description=description,
        mode="oracle",
        expected_exit=ps.EXIT_OK,
        actual_exit=ps.EXIT_OK if not problems else ps.EXIT_FAIL,
        command=command,
        passed=not problems,
        red=bool(problems),
        problems=problems,
        evidence=evidence,
    )


def run_fingerprint_oracle(cli_path: Path, work_root: Path) -> list[ps.CaseResult]:
    results: list[ps.CaseResult] = []
    root = work_root / "fingerprint-oracle"
    shutil.rmtree(root, ignore_errors=True)

    # Whitespace / key-order invariance.
    compact = ps.Fixture(root / "compact")
    pretty = ps.Fixture(root / "pretty")
    write_base(compact)
    compact.write_json("manifest.json", base_index())
    compact.write_json("profiles.json", base_profiles())
    compact.write_json(SCENARIO_FILE, base_scenario_file())
    pretty.write_json("manifest.json", base_index(), pretty=True)
    pretty.write_json("profiles.json", base_profiles(), pretty=True)
    pretty.write_json(SCENARIO_FILE, base_scenario_file(), pretty=True)
    fp_compact = compact.fingerprint()
    fp_pretty = pretty.fingerprint()
    run_compact = _manifest_check(compact, cli_path)
    run_pretty = _manifest_check(pretty, cli_path)
    problems: list[str] = []
    if fp_compact != fp_pretty:
        problems.append("whitespace/key-order changed the independent fingerprint")
    for label, run in (("compact", run_compact), ("pretty", run_pretty)):
        if run.exit != ps.EXIT_OK:
            problems.append(f"{label} tree manifest check exit {run.exit}")
        if not isinstance(run.report, dict):
            problems.append(f"{label} tree wrote no report")
        elif run.report.get("manifestSha256") != fp_compact:
            problems.append(f"{label} tree fingerprint != independent fingerprint")
    results.append(
        _oracle_result(
            "fp-whitespace-keyorder-invariant",
            "fingerprint is invariant to whitespace and key ordering",
            problems,
            expected_fingerprint=fp_compact,
            observed_fingerprint=fp_pretty,
            command=run_compact.command(),
        )
    )

    # Content change alters the digest and a stale capture hash is rejected.
    mutated = ps.Fixture(root / "content-change")
    write_base(mutated)
    original = mutated.fingerprint()
    scenario_file = ps.as_object(mutated.load_json(SCENARIO_FILE), SCENARIO_FILE)
    _first_expectation(_scenario_at(scenario_file, 0))["equals"] = False
    mutated.write_json(SCENARIO_FILE, scenario_file)
    changed = mutated.fingerprint()
    setup = ps.CaseSetup(
        expected_fingerprint=changed,
        expected_manifest_id=SYNTHETIC_MANIFEST_ID,
        expected_scenario_ids=sorted(BASE_SCENARIO_IDS),
        expected_files=sorted(REFERENCED_FILES),
    )
    write_capture(
        mutated,
        purpose="tool-fixture",
        env=build_env(mutated),
        fingerprint=original,
    )
    capture_spec = case(
        case_id="fp-stale-capture",
        group="fingerprint-oracle",
        description="stale capture hash after a scenario change is rejected",
        mode="capture",
        expect_exit=ps.EXIT_INPUT,
        expect_kind=VALIDATION_KIND_REPORT,
        mutate=lambda fx: setup,
    )
    stale_run = ps.run_cli(
        cli_path,
        build_argv(capture_spec, mutated, setup),
        mutated.root,
        mutated.path("report.json"),
    )
    problems = []
    if original == changed:
        problems.append("scenario content change did not alter the fingerprint")
    if stale_run.exit != ps.EXIT_INPUT:
        problems.append(f"stale capture exit {stale_run.exit} != {ps.EXIT_INPUT}")
    results.append(
        _oracle_result(
            "fp-content-change-alters",
            "a scenario change alters the digest and invalidates a stale capture",
            problems,
            expected_fingerprint=changed,
            observed_fingerprint=original,
            command=stale_run.command(),
        )
    )

    # Context change alters the digest.
    context = ps.Fixture(root / "context-change")
    write_base(context)
    before = context.fingerprint()
    profiles = ps.as_object(context.load_json("profiles.json"), "profiles.json")
    alpha = ps.object_field(profiles, ALPHA, "profiles")
    alpha["notes"] = ["changed synthetic note"]
    context.write_json("profiles.json", profiles)
    after = context.fingerprint()
    problems = []
    if before == after:
        problems.append("context change did not alter the fingerprint")
    results.append(
        _oracle_result(
            "fp-context-change-alters",
            "any parsed context change alters the digest",
            problems,
            expected_fingerprint=after,
            observed_fingerprint=before,
        )
    )

    # Unreferenced files (mapping/examples/reports) are excluded.
    extra = ps.Fixture(root / "unreferenced")
    write_base(extra)
    base_fp = extra.fingerprint()
    extra.write_json("migration-map.json", {"mappingVersion": 1})
    extra.write_json("examples/example.json", {"kind": "example"})
    extra.write_json("capture.json", {"kind": "report"})
    after = extra.fingerprint()
    run = _manifest_check(extra, cli_path)
    problems = []
    if base_fp != after:
        problems.append("unreferenced files changed the fingerprint")
    if run.exit != ps.EXIT_OK:
        problems.append(f"manifest check exit {run.exit}")
    if isinstance(run.report, dict) and run.report.get("manifestSha256") != base_fp:
        problems.append("CLI fingerprint differs from the independent fingerprint")
    results.append(
        _oracle_result(
            "fp-unreferenced-files-excluded",
            "unreferenced mapping/examples/reports are excluded from the fingerprint",
            problems,
            expected_fingerprint=base_fp,
            observed_fingerprint=after,
            command=run.command(),
        )
    )

    # Moving files preserves ids but may change the digest.
    moved = ps.Fixture(root / "moved")
    write_base(moved)
    before = moved.fingerprint()
    moved.path(SCENARIO_FILE).unlink()
    moved.write_json(
        "scenarios/alpha/alpha.json",
        {
            "kind": ps.SCENARIOS_KIND,
            "schemaVersion": 2,
            "scenarios": [alpha_scenario()],
        },
    )
    moved.write_json(
        "scenarios/beta/beta.json",
        {
            "kind": ps.SCENARIOS_KIND,
            "schemaVersion": 2,
            "scenarios": [beta_scenario()],
        },
    )
    after = moved.fingerprint()
    run = _manifest_check(moved, cli_path)
    problems = []
    if run.exit != ps.EXIT_OK:
        problems.append(f"moved tree manifest check exit {run.exit}")
    if isinstance(run.report, dict):
        scope = run.report.get("scope")
        raw_ids = scope.get("scenarioIds") if isinstance(scope, dict) else None
        if _sorted_strings(raw_ids) != sorted(BASE_SCENARIO_IDS):
            problems.append(f"moving files changed scenario ids: {raw_ids!r}")
    results.append(
        _oracle_result(
            "fp-move-preserves-ids",
            "moving scenario files preserves ids while the digest may change",
            problems,
            expected_fingerprint=after,
            observed_fingerprint=before,
            command=run.command(),
        )
    )

    # Array order is preserved.
    reordered = ps.Fixture(root / "array-order")
    write_base(reordered)
    before = reordered.fingerprint()
    scenario_file = ps.as_object(reordered.load_json(SCENARIO_FILE), SCENARIO_FILE)
    _scenario_array(scenario_file).reverse()
    reordered.write_json(SCENARIO_FILE, scenario_file)
    after = reordered.fingerprint()
    problems = []
    if before == after:
        problems.append("array order change did not alter the fingerprint")
    results.append(
        _oracle_result(
            "fp-array-order-preserved",
            "array order is preserved in the canonical fingerprint",
            problems,
            expected_fingerprint=after,
            observed_fingerprint=before,
        )
    )

    return results


def run_parser_oracle() -> list[ps.CaseResult]:
    """Independent checks on the strict JSON boundary itself (no CLI, no game)."""
    results: list[ps.CaseResult] = []

    overflow_problems: list[str] = []
    nonfinite_samples = (
        ("1e999 overflow", '{"values":{"m.width":1e999}}'),
        ("NaN literal", '{"values":{"m.width":NaN}}'),
        ("Infinity literal", '{"values":{"m.width":Infinity}}'),
    )
    for label, text in nonfinite_samples:
        try:
            ps.parse_json_strict(text, "nonfinite-probe")
        except ps.JsonLoadError:
            continue
        overflow_problems.append(f"{label} was not rejected by the strict parser")
    results.append(
        _oracle_result(
            "oracle-nonfinite-overflow-rejected",
            "the independent strict parser rejects overflow/NaN/Infinity without crashing",
            overflow_problems,
        )
    )

    finite_problems: list[str] = []
    try:
        parsed = ps.as_object(
            ps.parse_json_strict('{"finite":1.5,"integer":2}', "finite-probe"),
            "finite-probe",
        )
    except ps.JsonLoadError as exc:
        finite_problems.append(f"finite JSON was rejected: {exc}")
    else:
        if not ps.is_finite_number(parsed["finite"]):
            finite_problems.append("1.5 was not recognised as a finite number")
    if ps.is_finite_number(float("inf")):
        finite_problems.append("float('inf') was wrongly accepted as finite")
    if ps.is_finite_number(True):
        finite_problems.append("bool was wrongly accepted as a finite number")
    results.append(
        _oracle_result(
            "oracle-finite-number-helper",
            "the math.isfinite-based helper accepts finite numbers and rejects non-finite values and bools",
            finite_problems,
        )
    )
    return results


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKSA-23 v2 protocol black-box acceptance matrix")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--cli", default=None, help="override the CLI under test")
    return parser.parse_args(argv)


def _case_to_json(result: ps.CaseResult) -> ps.JsonObject:
    problem_items = ps.json_array(result.problems)
    return {
        "id": result.case_id,
        "group": result.group,
        "description": result.description,
        "mode": result.mode,
        "expectedExit": result.expected_exit,
        "actualExit": result.actual_exit,
        "command": result.command,
        "passed": result.passed,
        "red": result.red,
        "problems": problem_items,
        "evidence": result.evidence,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = (repo_root / output).resolve()
    cli_path = Path(args.cli).resolve() if args.cli else repo_root / "tests" / "e2e" / "verify.py"
    work_root = output.parent / (output.stem + "-fixtures")

    if not cli_path.is_file():
        sys.stderr.write(f"FAIL [setup] CLI not found: {cli_path}\n")
        return ps.EXIT_INPUT

    specs = positive_cases() + ALL_CASES
    results: list[ps.CaseResult] = []
    for spec in specs:
        result, _ = run_spec(spec, cli_path, work_root)
        results.append(result)

    results.extend(run_fingerprint_oracle(cli_path, work_root))
    results.extend(run_parser_oracle())

    summary = ps.summarize_cases(results, expected_red=True)
    exit_code = ps.EXIT_OK if summary["ok"] else ps.EXIT_FAIL
    report: ps.JsonObject = {
        "artifactVersion": 1,
        "kind": "gksa23-protocol-matrix",
        "assertion": "synthetic-cli-only",
        "e2eVerdict": None,
        "baselineCommit": ps.BASE_COMMIT,
        "repoRoot": str(repo_root),
        "cli": str(cli_path),
        "cliSha256": ps.sha256_file(cli_path),
        "python": sys.version,
        "exit": exit_code,
        "ok": summary["ok"],
        "red": summary["red"],
        "command": " ".join(
            [
                "python",
                "tests/tools/protocol_matrix.py",
                "--repo-root",
                ".",
                "--output",
                str(output),
            ]
        ),
        "summary": summary,
        "cases": [_case_to_json(result) for result in results],
        "note": (
            "Synthetic tool-only protocol fixtures. No game, plugin or native "
            "code is executed or mocked, and e2eVerdict is null by design. Cases "
            "that fail against the untouched v1 tool are intended RED evidence "
            "that the v2 protocol is not yet implemented."
        ),
    }
    ps.write_report(output, report)

    for result in results:
        if not result.passed:
            reason = "; ".join(result.problems) or "failed"
            sys.stderr.write(f"FAIL [{result.case_id}] {reason}\n")
    if summary["ok"]:
        sys.stderr.write(
            f"RESULT: PASS ({summary['passed']}/{summary['total']} synthetic "
            "protocol cases; no game execution asserted)\n"
        )
        return ps.EXIT_OK
    sys.stderr.write(
        f"RESULT: RED ({summary['passed']}/{summary['total']} synthetic protocol "
        "cases; expected against the untouched v1 CLI)\n"
    )
    return ps.EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
