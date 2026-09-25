#!/usr/bin/env python3
"""GKSA-11 source and resource guard.

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
  * localization declares both supported catalogs (en, zh_cn), every supported
    catalog carries an identical key set, every required HUD key is present,
    every value is a nonempty string, placeholder parity holds, and every
    implemented HUD sentence matches the accepted manifest finalText;
  * no final localized sentence is duplicated inside C# source.

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

# Vendored binary / bundle / proprietary asset suffixes that must never be
# committed as project source.
PROHIBITED_SUFFIXES = (
    ".dll", ".exe", ".so", ".dylib", ".bundle", ".zip", ".nupkg", ".snupkg",
    ".pdb", ".a", ".lib", ".pak", ".assets", ".unity3d", ".apk", ".jar", ".wasm",
)

# Known native / third-party assembly basenames that must never be vendored.
PROHIBITED_BASENAME_PREFIXES = (
    "assembly-csharp", "lazybeartechnology", "unityengine", "bepinex",
    "0harmony", "gk2.framework", "newtonsoft.json", "unity.textmeshpro",
    "monomod",
)

# Localization language id -> manifest catalog label used in finalText.
LANGUAGE_TO_CATALOG_LABEL = {"en": "en", "zh_cn": "zh-CN"}

# Raw game language ids the plugin must ship catalogs for. Both catalogs must
# exist and carry identical key sets; a locale may never be dropped silently.
SUPPORTED_LANGUAGE_IDS = ("en", "zh_cn")

# HUD keys the implemented plugin must declare in every supported catalog: the
# GKSA-10 countdown pair plus the GKSA-11 ready/done pair. The accepted ticket
# manifests stay the single source of truth for the sentences themselves; only
# the key names are required here, so no final sentence is copied into Python.
REQUIRED_HUD_KEYS = (
    "gksr.hud.sermonCountdown.one",
    "gksr.hud.sermonCountdown.other",
    "gksr.hud.sermonReminder",
    "gksr.hud.sermonDone",
)


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def git(root: Path, args: list) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def versioned_files(root: Path) -> list:
    """Tracked/index files plus new non-ignored files, excluding agent worktrees."""
    paths = []
    for raw in git(root, []).split("\0"):
        if raw:
            paths.append(raw)
    for raw in git(root, ["--others", "--exclude-standard"]).split("\0"):
        if raw:
            paths.append(raw)

    seen = set()
    result = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized.startswith(".agents/worktrees/") or normalized == ".agents/worktrees":
            continue
        result.append(normalized)
    return sorted(result)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def find_localization_files(root: Path, project: Path) -> tuple:
    """Return (resource_entries, violations) parsed from the project file.

    Each resource entry keeps the filename -> logical resource mapping and the
    WithCulture flag so the mapping stays explicit and inspectable.
    """
    violations = []
    entries = []

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
            violations.append(
                f"EmbeddedResource '{include}' must set WithCulture=\"false\" "
                f"(found {with_culture!r})"
            )
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

        entries.append({
            "include": include,
            "logicalName": logical_name,
            "language": language,
            "withCulture": with_culture,
            "exists": exists,
        })

    return entries, violations


def check_references(root: Path, project: Path) -> tuple:
    references = []
    violations = []
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
            violations.append(
                f"Reference '{identity}' must set <Private>false</Private> "
                f"(found {private_value!r})"
            )

    references.sort(key=lambda item: item["identity"])
    return references, violations


def supported_language_ids(manifest: dict) -> tuple:
    """Raw game language ids the plugin must ship catalogs for.

    Derived from the accepted manifest's language mapping when present so the
    manifest stays the single source of truth, falling back to the two
    supported ids otherwise.
    """
    mapping = manifest.get("languageMapping") if isinstance(manifest, dict) else None
    if isinstance(mapping, dict):
        label_to_raw = mapping.get("catalogLabelToRawGameLanguageId")
        if isinstance(label_to_raw, dict):
            raws = {value for value in label_to_raw.values() if isinstance(value, str) and value}
            if raws:
                return tuple(sorted(raws))
    return SUPPORTED_LANGUAGE_IDS


def check_manifest_and_localization(root: Path, manifest_path: Path, entries: list, project_dir: Path) -> dict:
    """Compare implemented localization to the accepted manifest.

    The manifest is the single source of truth for the final sentences, so this
    helper never duplicates that copy in code.
    """
    localization = {
        "logicalResourceMap": {},
        "languages": [],
        "supportedLanguages": [],
        "missingSupportedLanguages": [],
        "keySetsEqual": None,
        "missingKeys": {},
        "keyParity": [],
        "manifestComparison": [],
        "violations": [],
    }

    catalogs = {}
    for entry in entries:
        file_path = project_dir / entry["include"]
        language = entry["language"]
        localization["logicalResourceMap"][entry["logicalName"]] = {
            "file": entry["include"],
            "language": language,
        }
        if not file_path.is_file():
            continue
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

    manifest = {}
    manifest_text = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            localization["violations"].append("manifest top level is not an object")
            manifest = {}
        manifest_text = manifest.get("finalText", {})
        if not isinstance(manifest_text, dict):
            localization["violations"].append("manifest finalText is not an object")
            manifest_text = {}
    except (OSError, json.JSONDecodeError) as exc:
        localization["violations"].append(f"cannot load manifest {manifest_path}: {exc}")

    supported = supported_language_ids(manifest)
    localization["supportedLanguages"] = list(supported)

    # Every supported language must actually ship a readable catalog. Removing a
    # whole locale's resource declaration is a failure, never a silent pass.
    for language in supported:
        if language not in catalogs:
            localization["missingSupportedLanguages"].append(language)
            localization["violations"].append(
                f"supported language catalog missing: {language!r} "
                "(no embedded resource declared or file unreadable)"
            )

    # Every value must be a nonempty string before any regex comparison; a
    # non-string value is a reported violation, never a TypeError.
    keys_by_language = {}
    for language in sorted(catalogs):
        keys = set()
        for include, data in catalogs[language]:
            for key, value in data.items():
                keys.add(key)
                if not isinstance(value, str):
                    localization["violations"].append(
                        f"{include}: key {key!r} must be a nonempty string "
                        f"(found {type(value).__name__})"
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
    all_keys = set()
    for language in supported:
        all_keys |= keys_by_language.get(language, set())

    for language in supported:
        missing = sorted(all_keys - keys_by_language.get(language, set()))
        if missing:
            localization["missingKeys"][language] = missing
            localization["violations"].append(
                f"key parity failure: {language!r} is missing {missing}"
            )
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
        placeholders_by_language = {}
        for language in sorted(catalogs):
            for _, data in catalogs[language]:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    placeholders_by_language[language] = sorted(
                        set(re.findall(r"\{(\w+)\}", value))
                    )

        distinct = {tuple(v) for v in placeholders_by_language.values()}
        localization["keyParity"].append({
            "key": key,
            "placeholders": {lang: placeholders_by_language[lang] for lang in sorted(placeholders_by_language)},
        })
        if len(distinct) > 1:
            localization["violations"].append(
                f"placeholder parity failure for {key!r}: {placeholders_by_language}"
            )

    # Exact comparison for keys that also appear in the accepted manifest.
    for key in sorted(implemented_keys):
        if key not in manifest_text:
            continue
        expected = manifest_text[key]
        for language in sorted(catalogs):
            if language not in LANGUAGE_TO_CATALOG_LABEL:
                continue
            catalog_label = LANGUAGE_TO_CATALOG_LABEL[language]
            if catalog_label not in expected:
                continue
            actual = None
            for _, data in catalogs[language]:
                if key in data:
                    actual = data[key]
                    break
            expected_value = expected[catalog_label]
            matches = actual == expected_value
            localization["manifestComparison"].append({
                "key": key,
                "language": language,
                "catalogLabel": catalog_label,
                "matchesManifest": matches,
            })
            if not matches:
                localization["violations"].append(
                    f"{key!r} {language} does not match accepted manifest finalText"
                )

    return localization, implemented_keys, manifest_text


def find_sentences_in_cs(root: Path, files: list, manifest_text: dict) -> list:
    """No accepted final sentence may be duplicated inside C# source."""
    sentences = set()
    for translations in manifest_text.values():
        if isinstance(translations, dict):
            for value in translations.values():
                if isinstance(value, str) and value.strip():
                    sentences.add(value)

    if not sentences:
        return []

    violations = []
    for relative in files:
        if not relative.endswith(".cs"):
            continue
        text = read_text(root / relative)
        for sentence in sorted(sentences):
            if sentence in text:
                violations.append(f"{relative}: contains final sentence {sentence!r}")
    return violations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-11 source/resource guard.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--project", default="src/GK2.SermonReminder/GK2.SermonReminder.csproj")
    parser.add_argument(
        "--manifest",
        default="tests/e2e/gksa11.json",
        help=(
            "Accepted ticket manifest used as the finalText source of truth. "
            "Defaults to the latest implemented HUD specification (GKSA-11); pass "
            "another ticket's manifest explicitly to guard that ticket instead."
        ),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    project = (root / args.project).resolve()
    manifest_path = (root / args.manifest).resolve()
    output_path = Path(args.output)

    report = {
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
        "localization": {},
        "sentencesInCode": [],
        "errors": [],
    }

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

    localization, implemented_keys, manifest_text = check_manifest_and_localization(
        root, manifest_path, entries, project.parent
    )
    localization["violations"].extend(resource_violations)
    report["localization"] = localization
    report["localization"]["implementedKeys"] = sorted(implemented_keys)

    # 4. Final sentences must not be duplicated in C#.
    report["sentencesInCode"] = find_sentences_in_cs(root, files, manifest_text)

    violations = (
        report["prohibitedBinaries"]
        + report["prohibitedDocs"]
        + reference_violations
        + localization["violations"]
        + report["sentencesInCode"]
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
