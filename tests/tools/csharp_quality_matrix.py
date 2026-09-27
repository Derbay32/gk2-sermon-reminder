#!/usr/bin/env python3
"""GKSA-25 .NET quality-gate acceptance matrix (black-box CLI acceptance test).

This module is a TEST helper, never production code. It proves from the outside
that the repository .NET quality configuration actually catches violations: it
queries the real plugin's effective MSBuild properties and builds disposable
``netstandard2.1`` canary fixtures that inherit the REAL ancestor
``Directory.Build.props`` and ``.editorconfig``. It never imports production
modules, never references a native/game assembly, and never edits the plugin.

Failure paths enumerated BEFORE writing the assertions
------------------------------------------------------
Invocation/environment:
  e1  --dotnet missing, nonexistent, not executable, or a
      failing ``--version``                          -> c0 FAIL
  e2  dotnet reports an SDK other than 8.0.425       -> c0 FAIL
  e3  OSError/TimeoutExpired from any subprocess     -> that check FAILS
      (a missing tool is never silently a pass)
  e4  missing future configuration (baseline)        -> orderly RED
Plugin property contract (real csproj, no native reference resolution):
  p1..p6  EnableNETAnalyzers/AnalysisLevel/EnforceCodeStyleInBuild/
          TreatWarningsAsErrors/GenerateDocumentationFile/
          CopyDocumentationFileToOutputDirectory not equal to
          true/8.0-recommended/true/true/true/false  -> FAIL
  p7  msbuild -getProperty exits nonzero or emits no JSON -> every p* FAIL
Fixture canaries (inherit the REAL ancestor props/.editorconfig):
  c2   clean source using an instance field does not build  -> FAIL
  c2b  doc XML missing in obj, or copied into bin           -> FAIL
  c3   unused BCL using does not fail the build with IDE0005 -> FAIL
  c4   stateless instance method does not fail with CA1822  -> FAIL
  c5   non-disposable owner of a BCL disposable field does
       not fail the build with CA1001                       -> FAIL
  c6a  clean source is not whitespace-clean                 -> FAIL
  c6b  mis-indented source passes --verify-no-changes       -> FAIL
  c6c  any check mutates its hashed input files             -> FAIL
Missing future configuration must yield orderly RED, never a crash. Positive
controls (c2, c2b, c6a) run before the negative canaries. Each check records its
command array, exit code, expected/actual values, log path, and the before/after
sha256 of the inputs it read. The artifact carries ``e2eVerdict: null`` and
``scope: quality-gate-canaries-only``: analyzer canaries, not game simulations,
with NO production semantic check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EXPECTED_DOTNET_VERSION = "8.0.425"
ARTIFACTS_REL = "artifacts/gksa25-tests"
SCOPE = "quality-gate-canaries-only"
PLUGIN_REL = "src/GK2.SermonReminder/GK2.SermonReminder.csproj"
EXPECTED_PLUGIN_PROPERTIES = (
    ("EnableNETAnalyzers", "true"),
    ("AnalysisLevel", "8.0-recommended"),
    ("EnforceCodeStyleInBuild", "true"),
    ("TreatWarningsAsErrors", "true"),
    ("GenerateDocumentationFile", "true"),
    ("CopyDocumentationFileToOutputDirectory", "false"),
)

FIXTURE_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>netstandard2.1</TargetFramework>
  </PropertyGroup>
</Project>
"""
CLEAN_SOURCE = """internal sealed class Canary
{
    private readonly string _value = "canary";

    public string Read() => _value;
}
"""
UNUSED_USING_SOURCE = """using System.Text;

internal sealed class Canary
{
    private readonly string _value = "canary";

    public string Read() => _value;
}
"""
CA1822_SOURCE = """internal sealed class Canary
{
    public int Read() => 42;
}
"""
CA1001_SOURCE = """using System.IO;

internal sealed class Canary
{
    private readonly MemoryStream _stream = new MemoryStream();
}
"""
BAD_WHITESPACE_SOURCE = """internal sealed class Canary
{
        private readonly string _value = "canary";

            public string Read() => _value;
}
"""

PASSED = "passed"
FAILED = "failed"
Verdict = Callable[[subprocess.CompletedProcess[str] | None], tuple[bool, str]]


@dataclass
class CheckResult:
    """One quality-gate acceptance check."""

    check_id: str
    name: str
    expected: str
    actual: str
    status: str
    command: list[str]
    exit_code: int | None
    log: str
    detail: str


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_files(paths: list[Path]) -> dict[str, str]:
    return {str(path): (_sha256_path(path) if path.is_file() else "<missing>") for path in paths}


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = 900) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _combined(proc: subprocess.CompletedProcess[str] | None) -> str:
    return "" if proc is None else proc.stdout + proc.stderr


def _log_body(cmd: list[str], proc: subprocess.CompletedProcess[str] | None) -> str:
    head = f"$ {' '.join(cmd)}\n\n"
    if proc is None:
        return head + "subprocess could not start (OSError/TimeoutExpired)\n"
    return f"{head}[stdout]\n{proc.stdout}\n[stderr]\n{proc.stderr}\n"


def _exit_code(proc: subprocess.CompletedProcess[str] | None) -> str:
    return "exit <unavailable>" if proc is None else f"exit {proc.returncode}"


def _diagnostic_verdict(proc: subprocess.CompletedProcess[str] | None, code: str) -> tuple[bool, str]:
    """Nonzero exit that carries the exact ``error <code>:`` diagnostic.

    Matching the ``error <code>:`` form (not a bare substring) matters because the
    compiler's ``EnableGenerateDocumentationFile`` message itself mentions IDE0005;
    a substring test would report a false pass when XML generation is missing.
    """
    text = _combined(proc)
    if not code:
        return (proc is not None and proc.returncode == 0, _exit_code(proc))
    marker = f"error {code}:"
    return (
        proc is not None and proc.returncode != 0 and marker in text,
        f"{_exit_code(proc)}; {code}={'present' if marker in text else 'absent'}",
    )


def _xml_copy_verdict(proc: subprocess.CompletedProcess[str] | None, directory: Path) -> tuple[bool, str]:
    """Pass only when a COMPLETED, successful build left XML in obj and none in bin.

    The runner calls this with the just-finished ``proc``, so the XML tree is
    enumerated AFTER that subprocess ran. A failed/absent process is rejected
    even when XML from an earlier build is still on disk.
    """
    obj_xml = sorted((directory / "obj").rglob("Canary.xml"))
    bin_xml = sorted((directory / "bin").rglob("Canary.xml"))
    actual = f"{_exit_code(proc)}; obj={len(obj_xml)} bin={len(bin_xml)}"
    if proc is None or proc.returncode != 0:
        return (False, actual)
    return (bool(obj_xml) and not bin_xml, actual)


def _base_env(run_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "DOTNET_CLI_HOME": str(run_dir / "home"),
            "NUGET_PACKAGES": str(run_dir / "nuget"),
        }
    )
    return env


def _new_run_dir(repo: Path) -> Path:
    root = repo / ARTIFACTS_REL
    root.mkdir(parents=True, exist_ok=True)
    base = f"csharp-quality-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    candidate, suffix = root / base, 0
    while candidate.exists():
        suffix += 1
        candidate = root / f"{base}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _write_fixture(directory: Path, source: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    csproj, source_path = directory / "Canary.csproj", directory / "Canary.cs"
    csproj.write_text(FIXTURE_CSPROJ, encoding="utf-8")
    source_path.write_text(source, encoding="utf-8")
    return csproj, source_path


class Runner:
    """Executes one check, retains its log, and records before/after input hashes."""

    def __init__(self, repo: Path, env: dict[str, str], log_dir: Path) -> None:
        self.repo = repo
        self.env = env
        self.log_dir = log_dir
        self.evidence: list[dict[str, object]] = []

    def run(
        self,
        check_id: str,
        name: str,
        expected: str,
        cmd: list[str],
        inputs: list[Path],
        verdict: Verdict,
        detail: str,
    ) -> CheckResult:
        before = _hash_files(inputs)
        proc = _run(cmd, self.repo, self.env)
        after = _hash_files(inputs)
        log_path = self.log_dir / f"{check_id}.log"
        log_path.write_text(_log_body(cmd, proc), encoding="utf-8")
        ok, actual = verdict(proc)
        unchanged = before == after
        self.evidence.append({"check": check_id, "before": before, "after": after, "unchanged": unchanged})
        if not unchanged:
            detail = f"{detail}; input files changed during the check"
        return CheckResult(
            check_id=check_id,
            name=name,
            expected=expected,
            actual=actual,
            status=PASSED if ok and unchanged else FAILED,
            command=cmd,
            exit_code=None if proc is None else proc.returncode,
            log=str(log_path),
            detail=detail,
        )


def _plugin_property_checks(runner: Runner, dotnet: str, repo: Path, env: dict[str, str]) -> list[CheckResult]:
    plugin = repo / PLUGIN_REL
    names = ",".join(name for name, _expected in EXPECTED_PLUGIN_PROPERTIES)
    cmd = [dotnet, "msbuild", str(plugin), f"-getProperty:{names}"]
    before = _hash_files([plugin])
    proc = _run(cmd, repo, env)
    after = _hash_files([plugin])
    log_path = runner.log_dir / "c1.log"
    log_path.write_text(_log_body(cmd, proc), encoding="utf-8")
    runner.evidence.append({"check": "c1", "before": before, "after": after, "unchanged": before == after})
    props: dict[str, str] = {}
    if proc is not None and proc.returncode == 0:
        try:
            data: object = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("Properties"), dict):
            props = {str(key): str(value) for key, value in data["Properties"].items()}
    return [
        CheckResult(
            check_id=f"c1.{name}",
            name=f"plugin effective property {name}",
            expected=expected,
            actual=props.get(name, "<missing>"),
            status=PASSED if props.get(name) == expected and before == after else FAILED,
            command=cmd,
            exit_code=None if proc is None else proc.returncode,
            log=str(log_path),
            detail="effective value from dotnet msbuild -getProperty on the real plugin",
        )
        for name, expected in EXPECTED_PLUGIN_PROPERTIES
    ]


def _git_head(repo: Path) -> str | None:
    proc = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], repo, dict(os.environ))
    return None if proc is None or proc.returncode != 0 else (proc.stdout.strip() or None)


def _write_report(output: Path, report: dict[str, object]) -> None:
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(written, dict) or written.get("e2eVerdict", "missing") is not None:
        raise ValueError("written quality artifact lacks an explicit e2eVerdict: null")
    if written.get("scope") != SCOPE:
        raise ValueError(f"written quality artifact scope must be {SCOPE!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-25 .NET quality-gate acceptance matrix.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--dotnet", required=True, help="explicit dotnet executable under test")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    dotnet = args.dotnet
    head = _git_head(repo)
    run_dir = _new_run_dir(repo)
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    env = _base_env(run_dir)
    runner = Runner(repo, env, log_dir)
    checks: list[CheckResult] = []

    checks.append(
        runner.run(
            "c0",
            "pinned .NET SDK is observed",
            EXPECTED_DOTNET_VERSION,
            [dotnet, "--version"],
            [],
            lambda p: (
                p is not None and p.returncode == 0 and p.stdout.strip() == EXPECTED_DOTNET_VERSION,
                p.stdout.strip() if p is not None and p.returncode == 0 else "<unavailable>",
            ),
            "dotnet --version resolved from the repository root (global.json honored)",
        )
    )
    checks.extend(_plugin_property_checks(runner, dotnet, repo, env))

    # c2/c2b: clean positive control, then c3 mutates the SAME controlled source.
    clean_dir = run_dir / "canary-clean"
    clean_csproj, clean_source = _write_fixture(clean_dir, CLEAN_SOURCE)
    clean_inputs = [clean_csproj, clean_source]
    build_cmd = [dotnet, "build", str(clean_csproj), "-v:minimal", "--nologo"]
    checks.append(
        runner.run(
            "c2",
            "clean netstandard2.1 canary builds (positive control)",
            "exit 0",
            build_cmd,
            clean_inputs,
            lambda p: _diagnostic_verdict(p, ""),
            "fixture inherits the real ancestor Directory.Build.props/.editorconfig",
        )
    )
    checks.append(
        runner.run(
            "c2b",
            "documentation XML is generated but not copied to output",
            "obj XML present, bin XML absent",
            build_cmd,
            clean_inputs,
            lambda p: _xml_copy_verdict(p, clean_dir),
            "GenerateDocumentationFile=true and CopyDocumentationFileToOutputDirectory=false are effective",
        )
    )

    fixtures = (
        ("c3", "unused using fails with IDE0005", UNUSED_USING_SOURCE, "canary-clean", "build", "IDE0005"),
        ("c4", "stateless method fails with CA1822", CA1822_SOURCE, "canary-ca1822", "build", "CA1822"),
        ("c5", "disposable-field owner fails with CA1001", CA1001_SOURCE, "canary-ca1001", "build", "CA1001"),
        ("c6a", "clean source passes format --verify-no-changes", CLEAN_SOURCE, "format-clean", "format", ""),
        ("c6b", "mis-indented source fails format", BAD_WHITESPACE_SOURCE, "format-bad", "format", "WHITESPACE"),
    )
    for check_id, name, source_text, subdir, kind, code in fixtures:
        directory = run_dir / subdir
        csproj, source = _write_fixture(directory, source_text)
        if kind == "build":
            cmd = [dotnet, "build", str(csproj), "-v:minimal", "--nologo"]
        else:
            cmd = [dotnet, "format", "whitespace", str(directory), "--folder", "--verify-no-changes", "-v:minimal"]
        checks.append(
            runner.run(
                check_id,
                name,
                "exit 0" if not code else f"nonzero exit with {code}",
                cmd,
                [csproj, source],
                lambda p, code=code: _diagnostic_verdict(p, code),
                "disposable netstandard2.1 analyzer canary; format --verify-no-changes never rewrites its inputs",
            )
        )

    failures = [check for check in checks if check.status == FAILED]
    ok = not failures
    report: dict[str, object] = {
        "artifactVersion": 1,
        "kind": "gksa25-csharp-quality-acceptance-matrix",
        "assertion": SCOPE,
        "scope": SCOPE,
        "synthetic": True,
        "toolOnly": True,
        "e2eVerdict": None,
        "productionSemanticCheck": False,
        "ok": ok,
        "commit": head,
        "command": [sys.executable, *sys.argv],
        "exit": 0 if ok else 1,
        "repositoryRoot": str(repo),
        "workDir": str(run_dir),
        "dotnet": {"path": dotnet, "observedSdk": checks[0].actual, "expectedSdk": EXPECTED_DOTNET_VERSION},
        "expectedPluginProperties": {name: value for name, value in EXPECTED_PLUGIN_PROPERTIES},
        "checks": [check.__dict__ for check in checks],
        "noChangeEvidence": runner.evidence,
        "failures": [f"{check.check_id}: {check.actual}" for check in failures],
        "unexecuted": [],
        "note": (
            "Quality-gate canaries only: effective plugin properties via dotnet msbuild -getProperty, "
            "netstandard2.1 fixtures inheriting the REAL ancestor Directory.Build.props/.editorconfig, "
            "proving IDE0005/CA1822/CA1001/whitespace enforcement. No game execution, no native build, "
            "no production semantic check, no E2E verdict; missing configuration is orderly RED."
        ),
    }
    _write_report(output, report)

    for check in failures:
        sys.stderr.write(f"FAIL [csharp-quality] {check.check_id}: {check.actual} ({check.detail})\n")
    if not ok:
        sys.stderr.write(f"RESULT: FAIL (quality matrix; {len(failures)} failed of {len(checks)})\n")
        return 1
    sys.stderr.write(f"RESULT: PASS (quality matrix; {len(checks)} checks)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
