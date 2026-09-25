#!/usr/bin/env python3
"""GKSA-10 local build helper.

Builds the real plugin project against real native references and records an
ignored, local-only provenance report. This helper:

  * requires an explicit dotnet executable and explicit GameDir / BepInExDir /
    FrameworkDir reference roots;
  * uses the repository-pinned SDK through ``global.json`` and verifies the
    reported SDK version matches;
  * never substitutes fake reference stubs and never compiles SDK-only;
  * records the source HEAD, a dirty flag, the exact command and exit code,
    hashes of the actual resolved native reference inputs, the produced plugin
    DLL hash, and the output file list;
  * attributes the named commit only after every check succeeds on clean source
    and the produced DLL's informational version ends in ``+HEAD``; failed
    builds, dirty source, invalid HEAD and version mismatches all leave the
    attribution null;
  * writes no secret or environment dump.

The emitted report is explicitly local-only: it is not a hosted CI result and
not a game execution/E2E verdict. Only the Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PINNED_SDK_VERSION = "8.0.425"

# Native/third-party assembly basenames that must never end up in the build
# output. Copying any of these would ship a proprietary dependency.
PROHIBITED_OUTPUT_BASENAMES = (
    "assembly-csharp.dll",
    "lazybeartechnology.dll",
    "unityengine.dll",
    "unityengine.coremodule.dll",
    "unityengine.uimodule.dll",
    "unityengine.ui.dll",
    "unityengine.textrenderingmodule.dll",
    "unity.textmeshpro.dll",
    "newtonsoft.json.dll",
    "bepinex.dll",
    "0harmony.dll",
    "gk2.framework.dll",
)

INFORMATIONAL_VERSION_RE = re.compile(rb"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?\+[0-9a-f]{40})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_capture(command, cwd, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=str(cwd), env=env, capture_output=True, text=False, check=False
    )


def git(root: Path, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def resolve_dotnet(dotnet_arg: str) -> tuple:
    """Return (executable_path, dotnet_root, errors)."""
    errors = []
    candidate = Path(dotnet_arg)
    if candidate.is_file():
        exe = candidate
    else:
        found = shutil.which(dotnet_arg)
        if not found:
            return None, None, [f"dotnet executable not found: {dotnet_arg!r}"]
        exe = Path(found)

    if not os.access(str(exe), os.X_OK):
        errors.append(f"dotnet executable is not executable: {exe}")

    # The SDK root is the executable's directory (dotnet layout keeps
    # `dotnet` next to `sdk/`, `shared/`, `packs/`).
    return exe, exe.parent, errors


def parse_getitem(output: str) -> tuple:
    """Parse the JSON emitted by `dotnet build -getItem:Reference`."""
    text = output.strip()
    if not text:
        return [], ["-getItem:Reference produced no output"]
    # The JSON document is the last balanced object in the stream.
    start = text.find("{")
    if start < 0:
        return [], ["-getItem:Reference output had no JSON object"]
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        return [], [f"cannot parse -getItem:Reference JSON: {exc}"]

    items = data.get("Items", {}).get("Reference", [])
    if not isinstance(items, list):
        return [], ["-getItem:Reference Items.Reference was not a list"]
    return items, []


def collect_native_inputs(root: Path, project: Path, env, dotnet: Path, properties: list, configuration: str) -> tuple:
    """Resolve the actual reference inputs and hash the native ones.

    Uses the same configuration as the real build so the resolved reference set
    is the one the build will actually use.
    """
    command = [
        str(dotnet), "build", str(project), "-c", configuration,
        *properties,
        "-getItem:Reference",
    ]
    result = subprocess.run(
        command, cwd=str(root), env=env, capture_output=True, text=True, check=False
    )

    errors = []
    if result.returncode != 0:
        errors.append(
            f"reference resolution failed with exit code {result.returncode} "
            f"(command: {' '.join(command)})"
        )

    items, parse_errors = parse_getitem(result.stdout)
    errors.extend(parse_errors)
    if not items:
        errors.append(
            "reference resolution produced an empty reference list; refusing to treat "
            "an unresolved project as buildable"
        )

    references = []
    seen = set()
    for item in items:
        identity = str(item.get("Identity", ""))
        hint = str(item.get("HintPath", "") or "")
        private = str(item.get("Private", "") or "").lower()
        path = Path(hint) if hint else None
        exists = bool(path and path.is_file())
        entry = {
            "identity": identity,
            "hintPath": hint,
            "private": private or None,
            "exists": exists,
            "sha256": None,
        }
        if exists and str(path) not in seen:
            seen.add(str(path))
            entry["sha256"] = sha256_file(path)
        references.append(entry)

        if not exists:
            errors.append(f"resolved reference missing on disk: {identity} -> {hint!r}")
        if private != "false":
            errors.append(f"reference {identity!r} is not Private=false (found {private!r})")

    references.sort(key=lambda item: item["identity"])
    return references, errors


def informational_version(dll: Path) -> str:
    data = dll.read_bytes()
    match = INFORMATIONAL_VERSION_RE.search(data)
    return match.group(1).decode("ascii") if match else ""


def output_files(output_dir: Path) -> list:
    files = []
    if not output_dir.is_dir():
        return files
    for path in sorted(output_dir.rglob("*")):
        if path.is_file():
            files.append({
                "relativePath": path.relative_to(output_dir).as_posix(),
                "sizeBytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-10 local build helper.")
    parser.add_argument("--dotnet", required=True, help="explicit dotnet executable")
    parser.add_argument("--game-dir", required=True)
    parser.add_argument("--bepinex-dir", required=True)
    parser.add_argument("--framework-dir", required=True)
    parser.add_argument("--configuration", default="Release")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--project", default="src/GK2.SermonReminder/GK2.SermonReminder.csproj")
    parser.add_argument("--artifacts-dir", default="artifacts/local-build")
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    project = (root / args.project).resolve()
    artifacts = (root / args.artifacts_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    report_path = artifacts / "local-build-report.json"
    log_path = artifacts / "build.log"

    report = {
        "artifactVersion": 1,
        "kind": "gksa10-local-build-result",
        "assertion": "local-build-only",
        "hostedCiResult": False,
        "gameE2eVerdict": None,
        "note": (
            "Local build provenance only. Not a hosted CI result and not a game "
            "execution/E2E acceptance result."
        ),
        "ok": False,
        "verifiedNamedCommit": None,
        "source": {},
        "sdk": {},
        "references": {"inputs": [], "errors": []},
        "command": [],
        "commandText": "",
        "exitCode": None,
        "output": {
            "pluginDll": None,
            "sha256": None,
            "informationalVersion": None,
            "informationalVersionMatchesHead": False,
        },
        "outputFileList": [],
        "logPath": str(log_path.relative_to(root)),
        "warnings": [],
        "errors": [],
    }

    def write_report() -> None:
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")

    # 1. Source identity + dirty state.
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = git(root, "status", "--porcelain")
    dirty_paths = [line[3:] for line in porcelain.splitlines() if len(line) > 3]
    dirty = bool(dirty_paths)
    report["source"] = {
        "head": head,
        "branch": branch,
        "detached": branch == "HEAD",
        "dirty": dirty,
        "dirtyPathCount": len(dirty_paths),
        "dirtyPaths": dirty_paths[:50],
    }

    # A named commit can only be attributed once ALL checks succeed on clean
    # source. Until then the field stays null so a failed report can never carry
    # a supposedly verified commit.
    head_is_valid = bool(re.match(r"^[0-9a-f]{40}$", head))
    report["source"]["headValid"] = head_is_valid
    if not head_is_valid:
        report["errors"].append(
            f"cannot read a valid HEAD commit id (got {head!r}); no commit will be attributed"
        )
    if dirty:
        # A dirty tree may still build, but the result must never be presented
        # as verifying the named commit.
        report["warnings"].append(
            f"source tree is dirty ({len(dirty_paths)} changed path(s)); this build does "
            f"NOT verify commit {head}"
        )

    # 2. Explicit dotnet + pinned SDK.
    dotnet, dotnet_root, dotnet_errors = resolve_dotnet(args.dotnet)
    report["errors"].extend(dotnet_errors)
    if dotnet is None:
        write_report()
        sys.stderr.write("FAIL [local-build] " + "; ".join(report["errors"]) + "\n")
        return 2

    report["sdk"]["dotnetExecutable"] = str(dotnet)
    report["sdk"]["requested"] = PINNED_SDK_VERSION

    env = os.environ.copy()
    env["DOTNET_ROOT"] = str(dotnet_root)
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"

    sdk_result = subprocess.run(
        [str(dotnet), "--version"], cwd=str(root), env=env,
        capture_output=True, text=True, check=False,
    )
    reported_sdk = sdk_result.stdout.strip()
    report["sdk"]["reported"] = reported_sdk
    if reported_sdk != PINNED_SDK_VERSION:
        report["errors"].append(
            f"SDK version mismatch: global.json pins {PINNED_SDK_VERSION}, dotnet reported "
            f"{reported_sdk!r}"
        )

    # 3. Explicit reference roots must exist before an actionable build failure.
    for label, value in (
        ("GameDir", args.game_dir),
        ("BepInExDir", args.bepinex_dir),
        ("FrameworkDir", args.framework_dir),
    ):
        if not value:
            report["errors"].append(f"{label} is required")
    if report["errors"]:
        write_report()
        sys.stderr.write("FAIL [local-build] " + "; ".join(report["errors"]) + "\n")
        return 2

    # 4. Resolve + hash the actual reference inputs.
    reference_properties = [
        f"-p:GameDir={args.game_dir}",
        f"-p:BepInExDir={args.bepinex_dir}",
        f"-p:FrameworkDir={args.framework_dir}",
    ]
    references, reference_errors = collect_native_inputs(
        root, project, env, dotnet, reference_properties, args.configuration
    )
    report["references"]["inputs"] = references
    report["references"]["errors"] = reference_errors
    report["errors"].extend(reference_errors)
    if reference_errors:
        write_report()
        sys.stderr.write("FAIL [local-build] reference resolution problems\n")
        return 2

    # 5. Real build against the real references.
    command = [
        str(dotnet), "build", str(project), "-c", args.configuration,
        *reference_properties,
    ]
    report["command"] = command
    report["commandText"] = " ".join(command)

    build_result = run_capture(command, root, env)
    exit_code = build_result.returncode
    report["exitCode"] = exit_code

    stdout = build_result.stdout.decode("utf-8", errors="replace")
    stderr = build_result.stderr.decode("utf-8", errors="replace")
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("$ " + report["commandText"] + "\n")
        handle.write(f"# exit code: {exit_code}\n\n")
        handle.write("## stdout\n" + stdout)
        handle.write("\n## stderr\n" + stderr)

    if exit_code != 0:
        report["errors"].append(f"dotnet build failed with exit code {exit_code} (see {log_path})")
        write_report()
        sys.stderr.write(f"FAIL [local-build] build exit code {exit_code}; log {log_path}\n")
        sys.stderr.write((stdout + stderr)[-2000:] + "\n")
        return 2

    # 6. Output artifact + file list; assert no native reference was copied.
    output_dir = project.parent / "bin" / args.configuration / "netstandard2.1"
    dll = output_dir / "GK2.SermonReminder.dll"
    if not dll.is_file():
        report["errors"].append(f"expected plugin DLL not found: {dll}")
        write_report()
        sys.stderr.write("FAIL [local-build] plugin DLL missing after successful build\n")
        return 2

    report["output"] = {
        "pluginDll": str(dll.relative_to(root)),
        "sha256": sha256_file(dll),
        "informationalVersion": informational_version(dll),
    }
    report["outputFileList"] = output_files(output_dir)

    # The produced assembly must actually be built from this HEAD; otherwise the
    # build cannot be attributed to the named commit.
    informational = report["output"]["informationalVersion"] or ""
    expected_suffix = "+" + head
    report["output"]["informationalVersionMatchesHead"] = (
        head_is_valid and informational.endswith(expected_suffix)
    )
    if head_is_valid and not informational.endswith(expected_suffix):
        report["errors"].append(
            f"produced DLL informational version {informational!r} does not end with "
            f"'+{head}'; refusing to attribute the build to that commit"
        )

    native_hashes = {entry["sha256"] for entry in references if entry["sha256"]}
    for entry in report["outputFileList"]:
        lowered = Path(entry["relativePath"]).name.lower()
        if lowered in PROHIBITED_OUTPUT_BASENAMES:
            report["errors"].append(f"native reference copied into output: {entry['relativePath']}")
        if entry["sha256"] in native_hashes:
            report["errors"].append(
                f"output file duplicates a native reference input: {entry['relativePath']}"
            )

    report["ok"] = not report["errors"]

    # Attribute the named commit only now: clean source, valid HEAD, and a DLL
    # whose informational version identifies exactly that commit.
    if report["ok"] and not dirty and head_is_valid and report["output"]["informationalVersionMatchesHead"]:
        report["verifiedNamedCommit"] = head

    write_report()

    if not report["ok"]:
        for error in report["errors"]:
            sys.stderr.write(f"FAIL [local-build] {error}\n")
        return 2

    verified = report["verifiedNamedCommit"] or "dirty-source (unverified commit)"
    sys.stderr.write(
        f"RESULT: PASS (local build; SDK {reported_sdk}; verified {verified}; "
        f"dll sha256 {report['output']['sha256']})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
