#!/usr/bin/env python3
"""GKSA-13 source and resource guard.

Structural / source inspection only. This helper never installs or runs the
game, never compiles native code, and never asserts a game E2E verdict. It is
fail-closed: any violation makes the process exit non-zero and the emitted JSON
report carry ``ok: false``.

Checks:
  * versioned source contains no vendored binaries/bundles/proprietary assets;
  * no tracked docs live outside ``docs/adr/``;
  * every native ``<Reference>`` sets ``Private=false``;
  * every ``<EmbeddedResource>`` keeps an explicit ``LogicalName`` and
    ``WithCulture="false"``;
  * localization declares a readable, nonempty catalog for every language that
    any profile's ``languageMapping`` requires, every supported catalog carries
    an identical key set, every required HUD key is present, every value is a
    nonempty string, placeholder parity holds, and the countdown pair keeps its
    required parameter shape ({days} on "other", none on "one").

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

import e2e_manifest
import json_data

# Vendored binary / bundle / proprietary asset suffixes that must never be
# committed as project source.
PROHIBITED_SUFFIXES = (
    ".dll",
    ".exe",
    ".so",
    ".dylib",
    ".bundle",
    ".zip",
    ".nupkg",
    ".snupkg",
    ".pdb",
    ".a",
    ".lib",
    ".pak",
    ".assets",
    ".unity3d",
    ".apk",
    ".jar",
    ".wasm",
)

# Known native / third-party assembly basenames that must never be vendored.
PROHIBITED_BASENAME_PREFIXES = (
    "assembly-csharp",
    "lazybeartechnology",
    "unityengine",
    "bepinex",
    "0harmony",
    "gk2.framework",
    "newtonsoft.json",
    "unity.textmeshpro",
    "monomod",
)

# HUD keys the implemented plugin must declare in every supported catalog: the
# GKSA-10 countdown pair plus the GKSA-11 ready/done pair. Only the key names are
# required here, so no final sentence is copied into Python.
REQUIRED_HUD_KEYS = (
    "gksr.hud.sermonCountdown.one",
    "gksr.hud.sermonCountdown.other",
    "gksr.hud.sermonReminder",
    "gksr.hud.sermonDone",
)

# Narrow parameter-shape contract (never wording): the countdown "other"
# sentence must take exactly the {days} placeholder and the countdown "one"
# sentence must take none, in every supported catalog. Placeholder parity alone
# would still pass if {days} were dropped from both locales at once.
REQUIRED_PLACEHOLDERS = {
    "gksr.hud.sermonCountdown.other": ("days",),
    "gksr.hud.sermonCountdown.one": (),
}

# Default accepted manifest used to derive required supported languages from
# every profile's languageMapping. The settled project is the aggregated v2
# index; ``--manifest`` overrides it explicitly and there is no discovery or
# fallback to another file.
DEFAULT_MANIFEST = "tests/e2e/manifest.json"


class LocalizationEntry(TypedDict):
    """One parsed ``<EmbeddedResource>`` entry from the project file."""

    include: str
    logicalName: str
    language: str
    withCulture: str | None
    exists: bool


class ReferenceEntry(TypedDict):
    """One parsed native ``<Reference>`` entry from the project file."""

    identity: str
    private: str | None


class LocalizationReport(TypedDict):
    """The localization structure block of the source-check report."""

    logicalResourceMap: dict[str, dict[str, str]]
    languages: list[str]
    supportedLanguages: list[str]
    missingSupportedLanguages: list[str]
    keySetsEqual: bool | None
    missingKeys: dict[str, list[str]]
    keyParity: list[dict[str, object]]
    violations: list[str]
    implementedKeys: list[str]


class ReferencePrivacyReport(TypedDict):
    """The native-reference privacy block of the source-check report."""

    references: list[ReferenceEntry]
    violations: list[str]


class SourceCheckReport(TypedDict):
    """The complete ``gksa10-source-check-result`` report."""

    artifactVersion: int
    kind: str
    assertion: str
    e2eVerdict: None
    ok: bool
    repositoryRoot: str
    note: str
    scannedFileCount: int
    prohibitedBinaries: list[str]
    prohibitedDocs: list[str]
    referencePrivacy: ReferencePrivacyReport
    localization: LocalizationReport
    errors: list[str]
    violationCount: int


def _empty_localization() -> LocalizationReport:
    return {
        "logicalResourceMap": {},
        "languages": [],
        "supportedLanguages": [],
        "missingSupportedLanguages": [],
        "keySetsEqual": None,
        "missingKeys": {},
        "keyParity": [],
        "violations": [],
        "implementedKeys": [],
    }


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def git(root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def versioned_files(root: Path) -> list[str]:
    """Tracked/index files plus new non-ignored files, excluding agent worktrees."""
    paths: list[str] = []
    for raw in git(root, []).split("\0"):
        if raw:
            paths.append(raw)
    for raw in git(root, ["--others", "--exclude-standard"]).split("\0"):
        if raw:
            paths.append(raw)

    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized.startswith(".agents/worktrees/") or normalized == ".agents/worktrees":
            continue
        result.append(normalized)
    return sorted(result)


def find_localization_files(root: Path, project: Path) -> tuple[list[LocalizationEntry], list[str]]:
    """Return (resource_entries, violations) parsed from the project file.

    Each resource entry keeps the filename -> logical resource mapping and the
    WithCulture flag so the mapping stays explicit and inspectable.
    """
    violations: list[str] = []
    entries: list[LocalizationEntry] = []

    if not project.is_file():
        violations.append(f"project file not found: {project}")
        return entries, violations

    try:
        tree = ET.parse(project)
    except ET.ParseError as exc:
        violations.append(f"project file is not valid XML: {exc}")
        return entries, violations

    for element in tree.getroot().iter():
        if strip_ns(element.tag) != "EmbeddedResource":
            continue

        include = element.get("Include", "")
        attributes = {strip_ns(k): v for k, v in element.attrib.items()}
        with_culture = attributes.get("WithCulture")
        logical_el = element.find("LogicalName")
        logical_name = (logical_el.text or "").strip() if logical_el is not None else ""

        if with_culture != "false":
            violations.append(f"EmbeddedResource '{include}' must set WithCulture=\"false\" (found {with_culture!r})")
        if not logical_name:
            violations.append(f"EmbeddedResource '{include}' is missing an explicit LogicalName")

        source_path = project.parent / include
        exists = source_path.is_file()
        if not exists:
            violations.append(f"EmbeddedResource source file not found: {include}")

        language = ""
        stem = Path(include).name
        parts = stem.split(".")
        if len(parts) >= 3:
            language = parts[-2]

        entries.append(
            {
                "include": include,
                "logicalName": logical_name,
                "language": language,
                "withCulture": with_culture,
                "exists": exists,
            }
        )

    return entries, violations


def check_references(root: Path, project: Path) -> tuple[list[ReferenceEntry], list[str]]:
    references: list[ReferenceEntry] = []
    violations: list[str] = []
    if not project.is_file():
        return references, violations

    try:
        tree = ET.parse(project)
    except ET.ParseError:
        return references, violations

    for element in tree.getroot().iter():
        if strip_ns(element.tag) != "Reference":
            continue
        identity = element.get("Include", "")
        private_el = element.find("Private")
        private_value = (private_el.text or "").strip() if private_el is not None else None

        references.append({"identity": identity, "private": private_value})
        if private_value != "false":
            violations.append(f"Reference '{identity}' must set <Private>false</Private> (found {private_value!r})")

    references.sort(key=lambda item: item["identity"])
    return references, violations


def _supported_language_ids(bundle: e2e_manifest.ManifestBundle) -> tuple[str, ...]:
    """Raw game language ids the plugin must ship catalogs for.

    The union of every *validated* profile's language mapping is used, so a
    language required by any context must ship a catalog. The shared loader has
    already rejected a profile without a non-empty mapping, so there is no
    fallback language list: a missing locale can never be silently accepted.
    """
    raws: set[str] = set()
    for profile_id in bundle.profile_ids():
        profile = json_data.as_object(bundle.profiles[profile_id], f"profile {profile_id!r}")
        label_map = e2e_manifest.profile_label_map(profile, f"profile {profile_id!r}")
        for value in label_map.values():
            if isinstance(value, str) and value:
                raws.add(value)
    return tuple(sorted(raws))


def check_localization(
    bundle: e2e_manifest.ManifestBundle, entries: list[LocalizationEntry], project_dir: Path
) -> tuple[LocalizationReport, set[str]]:
    """Validate the implemented localization catalogs against the manifest.

    The bundle's profile ``languageMapping`` stays the single source of truth for
    which raw game languages must ship a readable, nonempty catalog; each declared
    catalog is parsed, key parity and the required HUD keys are enforced,
    placeholder parity and the countdown parameter shape are checked. No
    translated sentence is compared.
    """
    localization: LocalizationReport = _empty_localization()

    catalogs: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for entry in entries:
        file_path = project_dir / entry["include"]
        language = entry["language"]
        localization["logicalResourceMap"][entry["logicalName"]] = {
            "file": entry["include"],
            "language": language,
        }
        if not file_path.is_file():
            continue
        data: object
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            localization["violations"].append(f"{entry['include']}: cannot parse JSON: {exc}")
            continue
        if not isinstance(data, dict):
            localization["violations"].append(f"{entry['include']}: top level must be an object")
            continue
        catalogs.setdefault(language, []).append((entry["include"], data))

    localization["languages"] = sorted(catalogs)

    supported = _supported_language_ids(bundle)
    localization["supportedLanguages"] = list(supported)

    # Every supported language must actually ship a readable catalog. Removing a
    # whole locale's resource declaration is a failure, never a silent pass.
    for language in supported:
        if language not in catalogs:
            localization["missingSupportedLanguages"].append(language)
            localization["violations"].append(
                f"supported language catalog missing: {language!r} (no embedded resource declared or file unreadable)"
            )

    # Every value must be a nonempty string before any regex comparison; a
    # non-string value is a reported violation, never a TypeError.
    keys_by_language: dict[str, set[str]] = {}
    for language in sorted(catalogs):
        keys: set[str] = set()
        for include, data in catalogs[language]:
            for key, value in data.items():
                keys.add(key)
                if not isinstance(value, str):
                    localization["violations"].append(
                        f"{include}: key {key!r} must be a nonempty string (found {type(value).__name__})"
                    )
                elif not value.strip():
                    localization["violations"].append(
                        f"{include}: key {key!r} must be a nonempty string (found blank string)"
                    )
        keys_by_language[language] = keys

    # The supported catalogs must carry identical key sets. A key present in one
    # locale and missing from another is a failure. Catalog-only keys (such as the
    # settings-group title) are allowed; only the required HUD keys below are
    # mandatory.
    all_keys: set[str] = set()
    for language in supported:
        all_keys |= keys_by_language.get(language, set())

    for language in supported:
        missing = sorted(all_keys - keys_by_language.get(language, set()))
        if missing:
            localization["missingKeys"][language] = missing
            localization["violations"].append(f"key parity failure: {language!r} is missing {missing}")
    localization["keySetsEqual"] = not localization["missingKeys"]

    implemented_keys = all_keys

    # Every required HUD key (countdown plus ready/done) must be implemented. A
    # key absent from every catalog is a failure, never silently optional.
    for key in REQUIRED_HUD_KEYS:
        if key not in implemented_keys:
            localization["violations"].append(f"required HUD key not implemented: {key}")

    # Placeholder parity across every declared catalog that defines a key,
    # considering only validated non-empty string values.
    for key in sorted(implemented_keys):
        placeholders_by_language: dict[str, list[str]] = {}
        for language in sorted(catalogs):
            for _, data in catalogs[language]:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    placeholders_by_language[language] = sorted(set(re.findall(r"\{(\w+)\}", value)))

        distinct = {tuple(v) for v in placeholders_by_language.values()}
        parity_entry: dict[str, object] = {
            "key": key,
            "placeholders": {lang: placeholders_by_language[lang] for lang in sorted(placeholders_by_language)},
        }
        localization["keyParity"].append(parity_entry)
        if len(distinct) > 1:
            localization["violations"].append(f"placeholder parity failure for {key!r}: {placeholders_by_language}")

        # Matching placeholder sets must also satisfy the countdown parameter contract.
        if key in REQUIRED_PLACEHOLDERS:
            expected_placeholders = list(REQUIRED_PLACEHOLDERS[key])
            for language in supported:
                actual_placeholders = placeholders_by_language.get(language)
                if actual_placeholders is not None and actual_placeholders != expected_placeholders:
                    localization["violations"].append(
                        f"{key!r} in {language!r} must declare placeholders "
                        f"{expected_placeholders} (found {actual_placeholders})"
                    )

    localization["implementedKeys"] = sorted(implemented_keys)
    return localization, implemented_keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-13 source/resource guard.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--project", default="src/GK2.SermonReminder/GK2.SermonReminder.csproj")
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST,
        help=(
            "Accepted v2 manifest index used to derive the required supported "
            "languages from every profile's languageMapping. Defaults to the "
            "settled aggregated index tests/e2e/manifest.json; the whole bundle "
            "(index, profiles and every scenario document) is loaded and "
            "validated, and no other file is discovered or substituted."
        ),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    project = (root / args.project).resolve()
    manifest_path = root / args.manifest
    output_path = Path(args.output)

    report: SourceCheckReport = {
        "artifactVersion": 1,
        "kind": "gksa10-source-check-result",
        "assertion": "structure-and-source-only",
        "e2eVerdict": None,
        "ok": False,
        "repositoryRoot": str(root),
        "note": (
            "Source/resource inspection only. This report is not a native compile, "
            "not a game execution, and not an E2E acceptance result."
        ),
        "scannedFileCount": 0,
        "prohibitedBinaries": [],
        "prohibitedDocs": [],
        "referencePrivacy": {"references": [], "violations": []},
        "localization": _empty_localization(),
        "errors": [],
        "violationCount": 0,
    }

    files: list[str]
    try:
        files = versioned_files(root)
    except subprocess.CalledProcessError as exc:
        report["errors"].append(f"git ls-files failed: {exc}")
        files = []

    report["scannedFileCount"] = len(files)

    # 1. Vendored binaries / bundles / proprietary assets.
    for relative in files:
        name = Path(relative).name
        lowered = name.lower()
        if lowered.endswith(PROHIBITED_SUFFIXES) or lowered.startswith(PROHIBITED_BASENAME_PREFIXES):
            report["prohibitedBinaries"].append(relative)

    # 2. Tracked docs must live under docs/adr/.
    for relative in files:
        normalized = relative.replace("\\", "/")
        if normalized == "docs" or normalized.startswith("docs/"):
            if not normalized.startswith("docs/adr/"):
                report["prohibitedDocs"].append(normalized)

    # 3. Native references stay Private=false; embedded resources keep mapping.
    references, reference_violations = check_references(root, project)
    report["referencePrivacy"]["references"] = references
    report["referencePrivacy"]["violations"] = reference_violations

    entries, resource_violations = find_localization_files(root, project)

    bundle = None
    try:
        bundle = e2e_manifest.load_manifest(manifest_path)
    except e2e_manifest.ManifestInputError as exc:
        report["errors"].append(f"manifest bundle rejected: {exc}")

    if bundle is not None:
        localization, implemented_keys = check_localization(bundle, entries, project.parent)
    else:
        localization = _empty_localization()
        implemented_keys: set[str] = set()
    localization["violations"].extend(resource_violations)
    report["localization"] = localization
    report["localization"]["implementedKeys"] = sorted(implemented_keys)

    violations = (
        report["prohibitedBinaries"]
        + report["prohibitedDocs"]
        + reference_violations
        + localization["violations"]
        + report["errors"]
    )
    report["violationCount"] = len(violations)
    report["ok"] = not violations

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    if violations:
        for violation in violations:
            sys.stderr.write(f"FAIL [source] {violation}\n")
        sys.stderr.write("RESULT: FAIL (source/resource guard, fail-closed)\n")
        return 2

    sys.stderr.write(
        f"RESULT: PASS (source/resource guard; {report['scannedFileCount']} versioned files; "
        "no game execution asserted)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
