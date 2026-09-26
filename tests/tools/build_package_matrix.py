#!/usr/bin/env python3
"""GKSA-23 build/package acceptance matrix (black-box CLI acceptance test).

This module is a TEST helper, never production code. It drives
``tools/local_build.py`` and ``tools/package.py`` only through their command
line, and it never imports their internals and never mocks a game or native
assembly.

Failure paths enumerated BEFORE writing the assertions
------------------------------------------------------
Always runnable, black-box negative cases (the required negative gate):
  b1   local_build.py with no --dotnet                      -> FAIL (exit 2)
  b2   local_build.py with a nonexistent --dotnet           -> FAIL (exit 2),
       report ok false, verifiedNamedCommit null, HEAD recorded
  b3   package.py over a disposable repo-root fixture with no
       built plugin DLL (runs even when a real DLL exists)  -> FAIL (exit 2),
       "allowlisted distribution source missing"
  b4   local_build.py --dotnet <real> with empty reference
       roots (no native build)                              -> FAIL (exit 2),
       "GameDir is required"; conditional on an explicit --dotnet
  b5   both scripts keep their public CLI option names      -> PASS
Gated native cases (marked UNEXECUTED when the inputs are absent; a skip is
never a pass):
  n1   local_build.py real build against --dotnet/--game-dir/
       --bepinex-dir/--framework-dir                        -> PASS (exit 0),
       ok true, gameE2eVerdict null, source.head equals HEAD,
       clean-source named-commit attribution (dirty => null),
       every reference Private=false with a sha256 recomputed from
       its own hintPath, no native input copied into the output, and
       output DLL/plugin sha256 + informationalVersion verified
       against the report and the real files
  n2   package.py over the real built DLL                   -> PASS (exit 0),
       exact allowlist archive members, no third-party or
       native DLL, metadata filename retained
  n3   package metadata sha256 matches the archived bytes   -> PASS, with
       localOnly true and gameE2eVerdict null

Artifact retention
------------------
Every run creates a unique work directory under the ignored
``artifacts/gksa23-tests/`` tree and never deletes it: the local-build
report/log, the package archive/metadata and the b3 disposable fixture all stay
inspection-ready for the parent audit. The matrix report records each retained
file's absolute path, byte size and sha256.

``--negative-only``
-------------------
The always-runnable negative gate is what a hosted CI runner can enforce. With
``--negative-only`` the matrix asserts that every required negative executed and
passed, reports the native entries as visibly unexecuted with ``complete: false``
and exits 0 when the gate passes. An explicit ``--dotnet`` makes b4 part of the
required set; without one, b4 stays visibly unexecuted and is never relabelled
as a pass. The default/full invocation still fails an incomplete native
acceptance, so a missing native test can never be disguised as a hosted pass.

The artifact records ``nativeVerdict: null`` because these checks model no game
execution and produce no E2E verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

LOCAL_BUILD_REL = "tools/local_build.py"
PACKAGE_REL = "tools/package.py"
DLL_REL = "src/GK2.SermonReminder/bin/Release/netstandard2.1/GK2.SermonReminder.dll"

ARTIFACTS_REL = "artifacts/gksa23-tests"
METADATA_ARCHIVE_PATH = "BepInEx/plugins/GK2.SermonReminder/gksa10-metadata.json"
EXPECTED_ARCHIVE_MEMBERS = {
    "BepInEx/plugins/GK2.SermonReminder/GK2.SermonReminder.dll",
    "BepInEx/plugins/GK2.SermonReminder/LICENSE",
    METADATA_ARCHIVE_PATH,
}
PROHIBITED_BASENAMES = {
    "assembly-csharp.dll",
    "lazybeartechnology.dll",
    "unityengine.dll",
    "bepinex.dll",
    "0harmony.dll",
    "gk2.framework.dll",
    "newtonsoft.json.dll",
}

PASSED = "passed"
FAILED = "failed"
UNEXECUTED = "unexecuted"

ALWAYS_REQUIRED_NEGATIVES = ("b1", "b2", "b3", "b5a", "b5b")
CONDITIONAL_NEGATIVE = "b4"
NATIVE_CHECK_IDS = ("n1", "n2", "n3")


@dataclass
class CheckResult:
    """One build/package acceptance check."""

    check_id: str
    description: str
    status: str
    detail: str


def _run(cmd: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _load_json(path: Path) -> dict[str, object] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(repo: Path) -> str | None:
    completed = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], repo)
    if completed is None:
        return None
    head = completed.stdout.strip()
    return head or None


def _ok(check_id: str, description: str, detail: str) -> CheckResult:
    return CheckResult(check_id, description, PASSED, detail)


def _fail(check_id: str, description: str, detail: str) -> CheckResult:
    return CheckResult(check_id, description, FAILED, detail)


def _skip(check_id: str, description: str, detail: str) -> CheckResult:
    return CheckResult(check_id, description, UNEXECUTED, detail)


def _new_work_dir(repo: Path) -> Path:
    """Create a unique, persistent work directory under the ignored tree."""
    root = repo / ARTIFACTS_REL
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"build-package-{stamp}-{os.getpid()}"
    candidate = root / base
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = root / f"{base}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _artifact_index(work: Path) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for path in sorted(work.rglob("*")):
        if path.is_file():
            relative = path.relative_to(work).as_posix()
            index[relative] = {
                "path": str(path),
                "sha256": _sha256_path(path),
                "sizeBytes": path.stat().st_size,
            }
    return index


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_cli_contract(repo: Path, script_rel: str, options: tuple[str, ...], check_id: str) -> CheckResult:
    description = f"{script_rel} keeps its public CLI options"
    completed = _run([sys.executable, str(repo / script_rel), "--help"], repo)
    if completed is None or completed.returncode != 0:
        return _fail(check_id, description, f"{script_rel} --help did not succeed")
    text = completed.stdout
    missing = [option for option in options if option not in text]
    if missing:
        return _fail(check_id, description, f"missing options {missing}")
    return _ok(check_id, description, f"found {list(options)}")


def _check_local_build_requires_dotnet(repo: Path) -> CheckResult:
    description = "local_build requires --dotnet"
    completed = _run([sys.executable, str(repo / LOCAL_BUILD_REL), "--repo-root", str(repo)], repo)
    if completed is None:
        return _fail("b1", description, "local_build could not run")
    if completed.returncode == 2 and "--dotnet" in (completed.stderr or ""):
        return _ok("b1", description, "argparse exited 2 naming --dotnet")
    return _fail("b1", description, f"exit={completed.returncode}")


def _check_local_build_missing_dotnet(repo: Path, work: Path, head: str | None) -> CheckResult:
    description = "local_build fails closed without a dotnet executable"
    artifacts = work / "b2-local-build"
    completed = _run(
        [
            sys.executable,
            str(repo / LOCAL_BUILD_REL),
            "--repo-root",
            str(repo),
            "--dotnet",
            "/nonexistent/gksa23-dotnet",
            "--game-dir",
            "/nonexistent/game",
            "--bepinex-dir",
            "/nonexistent/bepinex",
            "--framework-dir",
            "/nonexistent/framework",
            "--artifacts-dir",
            str(artifacts),
        ],
        repo,
    )
    if completed is None or completed.returncode != 2:
        return _fail("b2", description, f"exit={None if completed is None else completed.returncode}")
    report = _load_json(artifacts / "local-build-report.json")
    if report is None:
        return _fail("b2", description, "local-build report not written")
    problems: list[str] = []
    if report.get("ok") is not False:
        problems.append(f"ok={report.get('ok')!r}")
    if report.get("verifiedNamedCommit") is not None:
        problems.append(f"verifiedNamedCommit={report.get('verifiedNamedCommit')!r}")
    if report.get("gameE2eVerdict") is not None:
        problems.append(f"gameE2eVerdict={report.get('gameE2eVerdict')!r}")
    errors = report.get("errors")
    if not isinstance(errors, list) or not any("dotnet executable not found" in str(item) for item in errors):
        problems.append("no dotnet-not-found error")
    source = report.get("source")
    if not isinstance(source, dict) or source.get("head") != head:
        problems.append(f"source.head={source!r}")
    if problems:
        return _fail("b2", description, "; ".join(problems))
    return _ok("b2", description, "ok false, gameE2eVerdict null, attribution null, HEAD recorded")


def _check_local_build_sdk_and_roots(repo: Path, work: Path, dotnet: str | None) -> CheckResult:
    description = "local_build pins the SDK and requires every reference root"
    if not dotnet:
        return _skip(CONDITIONAL_NEGATIVE, description, "no --dotnet provided to this matrix")
    artifacts = work / "b4-local-build"
    completed = _run(
        [
            sys.executable,
            str(repo / LOCAL_BUILD_REL),
            "--repo-root",
            str(repo),
            "--dotnet",
            dotnet,
            "--game-dir",
            "",
            "--bepinex-dir",
            "",
            "--framework-dir",
            "",
            "--artifacts-dir",
            str(artifacts),
        ],
        repo,
    )
    if completed is None or completed.returncode != 2:
        return _fail(CONDITIONAL_NEGATIVE, description, f"exit={None if completed is None else completed.returncode}")
    report = _load_json(artifacts / "local-build-report.json")
    if report is None:
        return _fail(CONDITIONAL_NEGATIVE, description, "local-build report not written")
    errors = report.get("errors")
    joined = " ".join(str(item) for item in errors) if isinstance(errors, list) else ""
    if "GameDir is required" not in joined:
        return _fail(CONDITIONAL_NEGATIVE, description, f"errors={errors!r}")
    return _ok(CONDITIONAL_NEGATIVE, description, "SDK pinned and empty reference roots rejected")


def _check_package_missing_build(repo: Path, work: Path) -> CheckResult:
    """Missing-plugin negative over a disposable repo root.

    The fixture is a fresh directory with only the repository LICENSE, so no
    real build output is present or touched; the case therefore runs even when a
    real plugin DLL exists in the checkout.
    """
    description = "package fails closed without a built plugin DLL"
    fixture = work / "b3-fixture"
    (fixture / "artifacts").mkdir(parents=True, exist_ok=True)
    license_source = repo / "LICENSE"
    if license_source.is_file():
        shutil.copy2(license_source, fixture / "LICENSE")
    completed = _run(
        [
            sys.executable,
            str(repo / PACKAGE_REL),
            "--repo-root",
            str(fixture),
            "--artifacts-dir",
            "artifacts/dist",
        ],
        repo,
    )
    if completed is None:
        return _fail("b3", description, "package could not run")
    if completed.returncode != 2:
        return _fail("b3", description, f"exit={completed.returncode}")
    if "allowlisted distribution source missing" not in (completed.stderr or ""):
        return _fail("b3", description, "missing allowed-source diagnostic")
    return _ok("b3", description, "exit 2, allowlisted source missing over a DLL-free fixture")


def _verify_reference_inputs(report: dict[str, object], problems: list[str]) -> int:
    references = report.get("references")
    inputs = references.get("inputs") if isinstance(references, dict) else None
    if not isinstance(inputs, list) or not inputs:
        problems.append("no resolved reference inputs")
        return 0
    for entry in inputs:
        if not isinstance(entry, dict):
            problems.append("reference entry is not an object")
            continue
        identity = entry.get("identity")
        if str(entry.get("private")).lower() != "false":
            problems.append(f"reference {identity!r} is not Private=false")
        hint = entry.get("hintPath")
        digest = entry.get("sha256")
        if not isinstance(hint, str) or not hint:
            problems.append(f"reference {identity!r} has no hintPath")
            continue
        hint_path = Path(hint)
        if not hint_path.is_file():
            problems.append(f"reference {identity!r} hintPath missing on disk: {hint}")
            continue
        if not isinstance(digest, str) or not digest:
            problems.append(f"reference {identity!r} has no sha256")
        elif _sha256_path(hint_path) != digest:
            problems.append(f"reference {identity!r} sha256 does not match the real file")
    return len(inputs)


def _verify_native_output(repo: Path, report: dict[str, object], head: str | None, problems: list[str]) -> None:
    output = report.get("output")
    if not isinstance(output, dict):
        problems.append("output block missing")
        return
    plugin_dll = output.get("pluginDll")
    dll_path: Path | None = None
    if not isinstance(plugin_dll, str) or not plugin_dll:
        problems.append("output.pluginDll missing")
    else:
        dll_path = repo / plugin_dll
        if not dll_path.is_file():
            problems.append(f"output.pluginDll not on disk: {plugin_dll}")
            dll_path = None
        else:
            if output.get("sha256") != _sha256_path(dll_path):
                problems.append("output.sha256 does not match the produced DLL bytes")
            informational = output.get("informationalVersion")
            if not isinstance(informational, str) or not informational:
                problems.append("output.informationalVersion missing")
            elif head and not informational.endswith("+" + head):
                problems.append(f"informationalVersion {informational!r} does not end with +{head}")
            if output.get("informationalVersionMatchesHead") is not True:
                problems.append("informationalVersionMatchesHead is not true")

    references = report.get("references")
    inputs = references.get("inputs") if isinstance(references, dict) else None
    reference_hashes = {
        str(item.get("sha256"))
        for item in (inputs if isinstance(inputs, list) else [])
        if isinstance(item, dict) and item.get("sha256")
    }

    output_files = report.get("outputFileList")
    if not isinstance(output_files, list) or not output_files:
        problems.append("outputFileList empty")
        return
    for entry in output_files:
        if not isinstance(entry, dict):
            problems.append("output file entry is not an object")
            continue
        relative = str(entry.get("relativePath", ""))
        if Path(relative).name.lower() in PROHIBITED_BASENAMES:
            problems.append(f"prohibited native output {relative}")
        if entry.get("sha256") in reference_hashes:
            problems.append(f"output duplicates a native input {relative}")
        if dll_path is None:
            continue
        path = dll_path.parent / relative
        if not path.is_file():
            problems.append(f"output file missing on disk: {relative}")
            continue
        if entry.get("sha256") != _sha256_path(path):
            problems.append(f"output file sha256 mismatch: {relative}")


def _check_local_build_native(
    repo: Path,
    work: Path,
    head: str | None,
    dotnet: str | None,
    game_dir: str | None,
    bepinex_dir: str | None,
    framework_dir: str | None,
) -> CheckResult:
    description = "local_build real provenance against real native references"
    if not (dotnet and game_dir and bepinex_dir and framework_dir):
        return _skip(
            "n1",
            description,
            "native inputs absent (--dotnet/--game-dir/--bepinex-dir/--framework-dir)",
        )
    artifacts = work / "n1-local-build"
    completed = _run(
        [
            sys.executable,
            str(repo / LOCAL_BUILD_REL),
            "--repo-root",
            str(repo),
            "--dotnet",
            dotnet,
            "--game-dir",
            game_dir,
            "--bepinex-dir",
            bepinex_dir,
            "--framework-dir",
            framework_dir,
            "--artifacts-dir",
            str(artifacts),
        ],
        repo,
    )
    if completed is None or completed.returncode != 0:
        tail = "" if completed is None else (completed.stdout + completed.stderr)[-400:]
        return _fail("n1", description, f"exit={None if completed is None else completed.returncode} {tail}")
    report = _load_json(artifacts / "local-build-report.json")
    if report is None:
        return _fail("n1", description, "local-build report not written")
    problems: list[str] = []

    if report.get("ok") is not True:
        problems.append(f"ok={report.get('ok')!r}")
    if report.get("gameE2eVerdict") is not None:
        problems.append(f"gameE2eVerdict={report.get('gameE2eVerdict')!r}")

    source = report.get("source")
    source_head = source.get("head") if isinstance(source, dict) else None
    if source_head != head:
        problems.append(f"source.head={source_head!r} != HEAD {head!r}")
    dirty = source.get("dirty") if isinstance(source, dict) else None
    verified = report.get("verifiedNamedCommit")
    if dirty is False:
        if verified != head:
            problems.append(f"clean source but verifiedNamedCommit={verified!r} != HEAD")
    elif verified is not None:
        problems.append(f"dirty source but verifiedNamedCommit={verified!r}")

    reference_count = _verify_reference_inputs(report, problems)
    _verify_native_output(repo, report, head, problems)

    if problems:
        return _fail("n1", description, "; ".join(problems))
    return _ok("n1", description, f"build verified; {reference_count} references recomputed")


def _check_package_archive(repo: Path, work: Path) -> CheckResult:
    description = "package archive is exactly the allowlist and no third-party DLL"
    artifacts = work / "n2-dist"
    completed = _run(
        [
            sys.executable,
            str(repo / PACKAGE_REL),
            "--repo-root",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ],
        repo,
    )
    if completed is None or completed.returncode != 0:
        tail = "" if completed is None else (completed.stdout + completed.stderr)[-400:]
        return _fail("n2", description, f"exit={None if completed is None else completed.returncode} {tail}")
    archive = artifacts / "GK2.SermonReminder.zip"
    if not archive.is_file():
        return _fail("n2", description, "archive not created")
    with zipfile.ZipFile(archive) as handle:
        names = sorted(handle.namelist())
    problems: list[str] = []
    if set(names) != EXPECTED_ARCHIVE_MEMBERS:
        problems.append(f"members={names}")
    for name in names:
        if Path(name).name.lower() in PROHIBITED_BASENAMES:
            problems.append(f"prohibited assembly {name}")
    if problems:
        return _fail("n2", description, "; ".join(problems))
    return _ok("n2", description, f"{len(names)} exact allowlist members")


def _check_package_metadata(repo: Path, work: Path) -> CheckResult:
    description = "package metadata hashes match the archived bytes"
    archive = work / "n2-dist" / "GK2.SermonReminder.zip"
    if not archive.is_file():
        return _skip("n3", description, "package archive absent (native build not run)")
    with zipfile.ZipFile(archive) as handle:
        names = set(handle.namelist())
        if METADATA_ARCHIVE_PATH not in names:
            return _fail("n3", description, "metadata member missing")
        raw = handle.read(METADATA_ARCHIVE_PATH)
        payloads = {name: handle.read(name) for name in names}
    try:
        metadata: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _fail("n3", description, f"metadata not valid JSON: {exc}")
    if not isinstance(metadata, dict):
        return _fail("n3", description, "metadata top level is not an object")
    problems: list[str] = []
    if metadata.get("kind") != "gksa10-distribution-metadata":
        problems.append(f"kind={metadata.get('kind')!r}")
    if metadata.get("localOnly") is not True:
        problems.append(f"localOnly={metadata.get('localOnly')!r}")
    if metadata.get("gameE2eVerdict") is not None:
        problems.append(f"gameE2eVerdict={metadata.get('gameE2eVerdict')!r}")
    files = metadata.get("files")
    if not isinstance(files, list) or not files:
        problems.append("files list missing")
    else:
        for entry in files:
            if not isinstance(entry, dict):
                problems.append("file entry is not an object")
                continue
            archive_path = entry.get("archivePath")
            expected = entry.get("sha256")
            if not isinstance(archive_path, str) or archive_path not in payloads:
                problems.append(f"archivePath {archive_path!r} not in archive")
                continue
            if expected != _sha256(payloads[archive_path]):
                problems.append(f"sha256 mismatch for {archive_path}")
    if problems:
        return _fail("n3", description, "; ".join(problems))
    return _ok("n3", description, "metadata retained and archived hashes match")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _write_report(output: Path, report: dict[str, object]) -> None:
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written = _load_json(output)
    if written is None or "e2eVerdict" not in written or written.get("e2eVerdict") is not None:
        raise ValueError("written build/package artifact lacks an explicit e2eVerdict: null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-23 build/package acceptance matrix.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--dotnet", help="explicit dotnet executable for the gated native build")
    parser.add_argument("--game-dir", help="native game directory (gated native build)")
    parser.add_argument("--bepinex-dir", help="BEPInEx directory (gated native build)")
    parser.add_argument("--framework-dir", help="framework directory (gated native build)")
    parser.add_argument(
        "--negative-only",
        action="store_true",
        help="enforce only the always-runnable negative gate; report native entries unexecuted and exit 0 on gate pass",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    head = _git_head(repo)
    work = _new_work_dir(repo)

    checks: list[CheckResult] = []
    checks.append(
        _check_cli_contract(
            repo,
            LOCAL_BUILD_REL,
            ("--dotnet", "--game-dir", "--bepinex-dir", "--framework-dir", "--artifacts-dir"),
            "b5a",
        )
    )
    checks.append(_check_cli_contract(repo, PACKAGE_REL, ("--artifacts-dir", "--output-name", "--repo-root"), "b5b"))
    checks.append(_check_local_build_requires_dotnet(repo))
    checks.append(_check_local_build_missing_dotnet(repo, work, head))
    checks.append(_check_package_missing_build(repo, work))
    checks.append(_check_local_build_sdk_and_roots(repo, work, args.dotnet))

    if args.negative_only:
        checks.append(_skip("n1", "local_build real provenance against real native references", "negative-only mode"))
        checks.append(
            _skip("n2", "package archive is exactly the allowlist and no third-party DLL", "negative-only mode")
        )
        checks.append(_skip("n3", "package metadata hashes match the archived bytes", "negative-only mode"))
    else:
        checks.append(
            _check_local_build_native(
                repo,
                work,
                head,
                args.dotnet,
                args.game_dir,
                args.bepinex_dir,
                args.framework_dir,
            )
        )
        if (repo / DLL_REL).is_file():
            checks.append(_check_package_archive(repo, work))
            checks.append(_check_package_metadata(repo, work))
        else:
            checks.append(
                _skip("n2", "package archive is exactly the allowlist and no third-party DLL", "plugin DLL not built")
            )
            checks.append(_skip("n3", "package metadata hashes match the archived bytes", "plugin DLL not built"))

    by_id = {check.check_id: check for check in checks}
    required_negative_ids: list[str] = list(ALWAYS_REQUIRED_NEGATIVES)
    if args.dotnet:
        required_negative_ids.append(CONDITIONAL_NEGATIVE)
    required_negatives = [
        {
            "id": check_id,
            "status": by_id[check_id].status if check_id in by_id else "missing",
            "detail": by_id[check_id].detail if check_id in by_id else "check not present",
        }
        for check_id in required_negative_ids
    ]
    required_negatives_passed = all(entry["status"] == PASSED for entry in required_negatives)

    failures = [check for check in checks if check.status == FAILED]
    unexecuted = [check for check in checks if check.status == UNEXECUTED]
    complete = not unexecuted
    if args.negative_only:
        # The negative gate is the hosted-CI-enforceable slice; native entries
        # stay visibly unexecuted and complete stays false.
        ok = required_negatives_passed
        exit_code = 0 if ok else 1
    else:
        ok = not failures and not unexecuted
        exit_code = 0 if ok else 1

    report: dict[str, object] = {
        "artifactVersion": 2,
        "kind": "gksa23-build-package-acceptance-matrix",
        "assertion": "build-package-only",
        "synthetic": True,
        "toolOnly": True,
        "hostedCiResult": False,
        "e2eVerdict": None,
        "nativeVerdict": None,
        "ok": ok,
        "executedOk": not failures,
        "complete": complete,
        "negativeOnly": bool(args.negative_only),
        "requiredNegatives": required_negatives,
        "requiredNegativesPassed": required_negatives_passed,
        "nativeChecksExecuted": [
            check.check_id for check in checks if check.check_id in NATIVE_CHECK_IDS and check.status == PASSED
        ],
        "commit": head,
        "command": [sys.executable, *sys.argv],
        "exit": exit_code,
        "repositoryRoot": str(repo),
        "pluginDllPresent": (repo / DLL_REL).is_file(),
        "nativeInputsProvided": bool(args.game_dir or args.bepinex_dir or args.framework_dir),
        "workDir": str(work),
        "artifacts": _artifact_index(work),
        "checks": [check.__dict__ for check in checks],
        "failures": [f"{check.check_id}: {check.detail}" for check in failures],
        "unexecuted": [f"{check.check_id}: {check.detail}" for check in unexecuted],
        "note": (
            "Build/package CLI acceptance only. Negative fail-closed cases run now; real "
            "native build and package archive checks are gated on --dotnet/--game-dir/"
            "--bepinex-dir/--framework-dir and a built DLL, and are marked unexecuted "
            "otherwise. Retained reports/logs/archive live under workDir and every retained "
            "file carries a path and sha256. No game DLL stub, no package publication and no "
            "E2E verdict; an unexecuted check is never counted as a pass."
        ),
    }
    _write_report(output, report)

    for check in failures:
        sys.stderr.write(f"FAIL [build-package] {check.check_id}: {check.detail}\n")
    for check in unexecuted:
        sys.stderr.write(f"UNEXECUTED [build-package] {check.check_id}: {check.detail}\n")
    if not ok:
        if args.negative_only:
            sys.stderr.write(
                "RESULT: FAIL (build/package negative gate; "
                f"requiredNegativesPassed={required_negatives_passed}; native entries unexecuted)\n"
            )
        else:
            sys.stderr.write(
                f"RESULT: FAIL (build/package matrix; {len(failures)} failed, "
                f"{len(unexecuted)} unexecuted of {len(checks)})\n"
            )
        return exit_code
    if args.negative_only:
        sys.stderr.write(
            "RESULT: PASS (build/package negative gate; all required negatives passed; "
            "native entries unexecuted; complete=false)\n"
        )
        return 0
    sys.stderr.write(f"RESULT: PASS (build/package matrix; {len(checks)} checks)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
