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
Missing future configuration must yield orderly RED, never a crash.
Build controls c2/c2b run before negative build canaries c3-c5.
The clean formatting control c6a runs before the malformed-source check c6b.
Each check records its command array, exit code, expected/actual values and log path.
Input-mutation evidence hashes only explicitly listed files: the real plugin
project for property queries and fixture project/source files for canaries.
Inherited configuration files are not part of that hash evidence. The artifact carries ``e2eVerdict: null`` and
``scope: quality-gate-canaries-only``: analyzer canaries, not game simulations,
with NO production semantic check.

GKSA-27 explicit game reference root (property/validation probes only)
----------------------------------------------------------------------
Five bounded real-CLI probes run against the REAL plugin project under a
reference-clean environment: every ``GameDir``/``GameDataDir``/``GameManagedDir``/
``BepInExDir``/``FrameworkDir``/``FrameworkDll`` variable is removed
case-insensitively so a leaked Windows-style variable cannot impersonate explicit
input. No directory is created, no native assembly is resolved and no build runs:
  g1  with no reference input, ``-getProperty:GameDir,GameDataDir,
      GameManagedDir`` does not exit 0 with JSON, or any of the three values is
      non-empty (the hardcoded Windows default leaks)          -> FAIL. An empty
      ``-p:GameDir=`` global property is deliberately NOT passed: it would
      override and hide the very default under test.
  g2  ``-target:GK2ValidateNativeReferences`` does not exit nonzero, does not
      carry the exact ``game reference root is not configured`` message, or the
      message omits any of ``-p:GameDir`` / ``-p:GameDataDir`` /
      ``-p:GameManagedDir`` (e.g. it is the older missing-DLL error) -> FAIL.
  g3  an explicit ``-p:GameDir=<absolute path with a space>`` is not preserved,
      or the derived GameDataDir/GameManagedDir is not
      ``<GameDir>/GraveyardKeeper2_Data[/Managed]`` (separators normalised only
      for comparison, never to hide an empty value)              -> FAIL.
  g4  an explicit ``-p:GameDataDir=<path>`` is not preserved, or the derived
      GameManagedDir is not ``<GameDataDir>/Managed``            -> FAIL.
  g5  an explicit ``-p:GameManagedDir=<path>`` is not preserved -> FAIL.
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

# GKSA-27: explicit game reference root contract, probed under a reference-clean environment.
G27_ARTIFACTS_REL = "artifacts/gksa27-tests"
GAME_REFERENCE_PROPERTIES = ("GameDir", "GameDataDir", "GameManagedDir")
ROOT_MESSAGE = "game reference root is not configured"
ROOT_OPTIONS = ("-p:GameDir", "-p:GameDataDir", "-p:GameManagedDir")
REFERENCE_ENV_KEYS = frozenset(
    {"gamedir", "gamedatadir", "gamemanageddir", "bepinexdir", "frameworkdir", "frameworkdll"}
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


def _get_properties(proc: subprocess.CompletedProcess[str] | None) -> dict[str, str] | None:
    """Effective properties from a successful ``msbuild -getProperty`` JSON probe.

    ``None`` means the process did not run, exited nonzero, or did not emit the
    expected JSON shape; an undefined or empty requested property comes back as an
    empty string and is therefore distinguishable from a missing JSON key.
    """
    if proc is None or proc.returncode != 0:
        return None
    try:
        data: object = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("Properties"), dict):
        return None
    return {str(key): str(value) for key, value in data["Properties"].items()}


def _norm_path(value: str) -> str:
    """Normalise separators for comparison only; never used to hide an empty value."""
    return value.replace("\\", "/")


def _expected_game_chain(game_dir: str) -> tuple[str, str, str]:
    data_dir = f"{game_dir}/GraveyardKeeper2_Data"
    return game_dir, data_dir, f"{data_dir}/Managed"


def _g1_verdict(proc: subprocess.CompletedProcess[str] | None) -> tuple[bool, str]:
    props = _get_properties(proc)
    if props is None:
        return False, f"{_exit_code(proc)}; no JSON properties"
    shown = ", ".join(f"{name}={props.get(name) or '<empty>'}" for name in GAME_REFERENCE_PROPERTIES)
    ok = all(name in props and props[name] == "" for name in GAME_REFERENCE_PROPERTIES)
    return ok, f"{_exit_code(proc)}; {shown}"


def _g2_verdict(proc: subprocess.CompletedProcess[str] | None) -> tuple[bool, str]:
    if proc is None:
        return False, "subprocess could not start (OSError/TimeoutExpired)"
    lowered = _combined(proc).lower()
    marker = ROOT_MESSAGE in lowered
    options = [option for option in ROOT_OPTIONS if option.lower() in lowered]
    ok = proc.returncode != 0 and marker and len(options) == len(ROOT_OPTIONS)
    counted = f"{len(options)}/{len(ROOT_OPTIONS)}"
    return ok, f"{_exit_code(proc)}; root-message={'present' if marker else 'absent'}; options={counted}"


def _derived_property_verdict(expected: dict[str, str]) -> Verdict:
    """Pass only when each expected property is non-empty and equal after separator normalisation."""

    def verdict(proc: subprocess.CompletedProcess[str] | None) -> tuple[bool, str]:
        props = _get_properties(proc)
        if props is None:
            return False, f"{_exit_code(proc)}; no JSON properties"
        shown = ", ".join(f"{name}={props.get(name) or '<empty>'}" for name in expected)
        ok = all(
            name in props and props[name] != "" and _norm_path(props[name]) == _norm_path(value)
            for name, value in expected.items()
        )
        return ok, f"{_exit_code(proc)}; {shown}"

    return verdict


def _without_reference_env(env: dict[str, str]) -> dict[str, str]:
    """Drop every reference-related variable case-insensitively (Windows env names are case-insensitive)."""
    return {key: value for key, value in env.items() if key.lower() not in REFERENCE_ENV_KEYS}


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
        env: dict[str, str] | None = None,
    ) -> CheckResult:
        before = _hash_files(inputs)
        proc = _run(cmd, self.repo, self.env if env is None else env)
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


def _game_reference_checks(runner: Runner, dotnet: str, repo: Path, clean_env: dict[str, str]) -> list[CheckResult]:
    """Five bounded real-CLI probes of the explicit game reference root contract.

    Property queries and the validation target only: no build, no native assembly
    resolution, no game execution and no filesystem creation. The explicit paths
    live under the ignored ``G27_ARTIFACTS_REL`` tree but are never created; only
    the real plugin project is hashed as input, exactly like the c1 property probe.
    """
    plugin = repo / PLUGIN_REL
    probe = [dotnet, "msbuild", str(plugin), f"-getProperty:{','.join(GAME_REFERENCE_PROPERTIES)}"]
    game_root = str(repo / G27_ARTIFACTS_REL / "Game Root With Space")
    data_root = str(repo / G27_ARTIFACTS_REL / "Explicit Data Root")
    managed_root = str(repo / G27_ARTIFACTS_REL / "Explicit Managed Root")
    _, derived_data, derived_managed = _expected_game_chain(game_root)
    return [
        runner.run(
            "g1",
            "game reference properties stay empty without explicit input",
            "exit 0; GameDir, GameDataDir and GameManagedDir all empty",
            probe,
            [plugin],
            _g1_verdict,
            "reference-clean environment; no empty -p:GameDir global property, which would hide the default",
            env=clean_env,
        ),
        runner.run(
            "g2",
            "missing game reference root fails with configuration guidance",
            f"nonzero exit; error contains {ROOT_MESSAGE!r} and all three -p: option names",
            [dotnet, "msbuild", str(plugin), "-target:GK2ValidateNativeReferences"],
            [plugin],
            _g2_verdict,
            "validation target only; no build, native resolution or game execution",
            env=clean_env,
        ),
        runner.run(
            "g3",
            "explicit GameDir is preserved and derives data/managed roots",
            "GameDir preserved; GameDataDir=<GameDir>/GraveyardKeeper2_Data; GameManagedDir=.../Managed",
            [*probe, f"-p:GameDir={game_root}"],
            [plugin],
            _derived_property_verdict(
                {"GameDir": game_root, "GameDataDir": derived_data, "GameManagedDir": derived_managed}
            ),
            "absolute path with a space under the ignored gksa27-tests artifacts; never created on disk",
            env=clean_env,
        ),
        runner.run(
            "g4",
            "explicit GameDataDir is preserved and derives managed root",
            "GameDataDir preserved; GameManagedDir=<GameDataDir>/Managed",
            [*probe, f"-p:GameDataDir={data_root}"],
            [plugin],
            _derived_property_verdict({"GameDataDir": data_root, "GameManagedDir": f"{data_root}/Managed"}),
            "explicit data path; does not assert GameDir (g1 covers default removal)",
            env=clean_env,
        ),
        runner.run(
            "g5",
            "explicit GameManagedDir is preserved",
            "GameManagedDir preserved exactly",
            [*probe, f"-p:GameManagedDir={managed_root}"],
            [plugin],
            _derived_property_verdict({"GameManagedDir": managed_root}),
            "explicit managed path; no new GameDir requirement and no filesystem creation",
            env=clean_env,
        ),
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

    checks.extend(_game_reference_checks(runner, dotnet, repo, _without_reference_env(env)))

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
            "no production semantic check, no E2E verdict; missing configuration is orderly RED. "
            "Five GKSA-27 probes (g1-g5) query the real plugin under a reference-clean environment: "
            "property emptiness without explicit input, the GK2ValidateNativeReferences configuration "
            "error, and preservation/derivation of -p:GameDir/-p:GameDataDir/-p:GameManagedDir. "
            "Property/validation only: never a native semantic build or game acceptance."
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
