#!/usr/bin/env python3
"""GKSA-23 toolchain acceptance matrix (black-box CLI acceptance test).

This module is a TEST helper, never production code. It inspects the repository
toolchain contract from the outside: it parses ``pyproject.toml``/``uv.lock``
with the standard library, inspects the CI workflow text, checks the uv-managed
virtual environment and the git ignore rules, and (only when the exact pinned
tools are actually available) invokes ``uv``, ``ruff`` and ``pyright`` to prove
that the configured file selection is real rather than a substring claim.

Failure paths enumerated BEFORE writing the assertions
------------------------------------------------------
Configuration and files:
  t1   .python-version missing or not exactly 3.13.11            -> FAIL
  t2   pyproject.toml missing or not valid TOML                  -> FAIL
  t3   project.requires-python != "==3.13.11"                    -> FAIL
  t4   tool.uv.package is not false                              -> FAIL
  t5   tool.uv.required-version != "==0.12.19"                   -> FAIL
  t6   tool.uv.python-downloads != "never"                       -> FAIL
  t7   [dependency-groups].dev pins absent, inexact or wrong    -> FAIL
       versions (optional-dependencies is not accepted)
  t8   uv.lock missing, not TOML, or missing the exact pins      -> FAIL
  t9   [tool.ruff] does not select E, F and I                    -> FAIL
  t9b  [tool.ruff] line-length is not exactly 120                -> FAIL
  t10  [tool.pyright] not standard/3.13, wrong include/exclude,
       an exclude that swallows an actual target, or a broad
       ignore list                                               -> FAIL
  t10b real generated directories are not effectively excluded
       from tool file discovery                                  -> FAIL
  t11  .gitignore does not ignore .venv and the tool caches      -> FAIL
  t12  .venv missing, or present without an explicit uv marker in
       pyvenv.cfg (uv management is never inferred)              -> FAIL
  t13  .venv interpreter is not 3.13.11 or does not point at the
       preserved pyenv install when one is present               -> FAIL
  t14  ci.yml does not set up 3.13.11, pin uv 0.12.19 and use
       --locked                                                  -> FAIL
  t15  ci.yml embeds a large Python matrix or a ticket loop in a
       quality step                                              -> FAIL
  t16  a target Python file carries a broad type-ignore     -> FAIL
Tool invocation (executed only when the exact pinned tool exists):
  q1   uv --version is not exactly 0.12.19                       -> FAIL
  q2   ruff --version is not exactly 0.16.9                      -> FAIL
  q3   pyright --version is not exactly 1.1.414                  -> FAIL
  q4   ruff file selection is not every tools+tests python file  -> FAIL
  q5   ruff check reports E/F/I diagnostics at 120               -> FAIL
  q6   ruff format --check reports unformatted files across the
       whole tools+tests tree at 120                             -> FAIL
  q7   pyright reports errors or analyzes the wrong file count   -> FAIL
  q8   uv lock --check reports a stale lock                      -> FAIL

Missing setup is reported as a failed check with a written artifact, never a
crash. When a quality tool is absent its invocation is marked ``unexecuted`` and
``unexecuted`` is never counted as a pass, so the matrix cannot be green by
skipping. ``pyvenv.cfg`` without a uv marker is a failure: an environment is
never reported uv-managed merely because a pip-less layout looks plausible. The
configured line length is frozen at 120 and the selection/format checks cover
the whole ``tools`` and ``tests`` trees with no path narrowing. Real generated
directories that actually exist are checked for effective exclusion instead of
requiring a literal placeholder pattern. The emitted artifact carries an
explicit ``e2eVerdict: null``; no game is run and no native code is built.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXPECTED_PYTHON_VERSION = "3.13.11"
EXPECTED_UV_VERSION = "0.12.19"
EXPECTED_RUFF_VERSION = "0.16.9"
EXPECTED_PYRIGHT_VERSION = "1.1.414"

PYTHON_TARGET_ROOTS = ("tools", "tests")
CACHE_DIR_NAMES = ("__pycache__", ".venv", ".ruff_cache", ".pytest_cache")
EXPECTED_LINE_LENGTH = 120
REAL_GENERATED_CANDIDATES = (
    ".agents/worktrees",
    ".venv",
    "artifacts",
    "dist",
    "bin",
    "obj",
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
)

PASSED = "passed"
FAILED = "failed"
UNEXECUTED = "unexecuted"


@dataclass
class CheckResult:
    """One toolchain acceptance check."""

    check_id: str
    description: str
    status: str
    detail: str


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _get(obj: object, *keys: str) -> object:
    current = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _load_toml(path: Path) -> tuple[dict[str, object] | None, str]:
    # Imported here so import sorting is stable whether or not a pyproject pins
    # the 3.13 target; tomllib is standard on the preserved 3.13.11 interpreter.
    import tomllib

    if not path.is_file():
        return None, f"{path.name} not found"
    try:
        data: object = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"{path.name} is not valid TOML: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} did not parse to a table"
    return {str(key): value for key, value in data.items()}, ""


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str] | None:
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


def _tool_executable(repo: Path, name: str) -> str | None:
    candidate = repo / ".venv" / "bin" / name
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name)


def _expected_python_targets(repo: Path) -> list[str]:
    targets: list[str] = []
    for root_name in PYTHON_TARGET_ROOTS:
        root = repo / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(repo).as_posix()
            if any(part in CACHE_DIR_NAMES for part in relative.split("/")):
                continue
            targets.append(relative)
    return targets


def _real_generated_dirs(repo: Path) -> list[str]:
    """Generated directories that actually exist in this checkout.

    Only real, present paths are considered; the matrix never invents an unused
    placeholder directory just to satisfy a config substring.
    """
    found: set[str] = set()
    for rel in REAL_GENERATED_CANDIDATES:
        if (repo / rel).exists():
            found.add(rel)
    for base in ("src", "tools", "tests"):
        base_path = repo / base
        if not base_path.is_dir():
            continue
        for pattern in ("**/bin", "**/obj", "**/__pycache__", "**/.ruff_cache", "**/.pytest_cache"):
            for path in base_path.glob(pattern):
                if path.is_dir():
                    found.add(path.relative_to(repo).as_posix())
    return sorted(found)


def _relative_to_repo(repo: Path, raw: str) -> str:
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _pattern_covers(relative: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        stripped = pattern.rstrip("/")
        if stripped in ("", "."):
            return True
        if relative == stripped or relative.startswith(stripped + "/"):
            return True
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, pattern + "/*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Configuration checks
# ---------------------------------------------------------------------------


def _check_python_version_file(repo: Path) -> CheckResult:
    path = repo / ".python-version"
    if not path.is_file():
        return CheckResult("t1", ".python-version pins 3.13.11", FAILED, ".python-version missing")
    content = path.read_text(encoding="utf-8").strip()
    if content != EXPECTED_PYTHON_VERSION:
        return CheckResult(
            "t1",
            ".python-version pins 3.13.11",
            FAILED,
            f".python-version is {content!r}, expected {EXPECTED_PYTHON_VERSION!r}",
        )
    return CheckResult("t1", ".python-version pins 3.13.11", PASSED, f".python-version={content!r}")


def _check_pyproject_present(pyproject_path: Path, pyproject: dict[str, object] | None, error: str) -> CheckResult:
    if pyproject is None:
        return CheckResult("t2", "pyproject.toml parses as a table", FAILED, error)
    return CheckResult(
        "t2",
        "pyproject.toml parses as a table",
        PASSED,
        f"parsed {pyproject_path.name}",
    )


def _check_requires_python(pyproject: dict[str, object] | None) -> CheckResult:
    value = _get(pyproject, "project", "requires-python")
    if value == "==3.13.11":
        return CheckResult("t3", "project.requires-python == '==3.13.11'", PASSED, f"requires-python={value!r}")
    return CheckResult(
        "t3",
        "project.requires-python == '==3.13.11'",
        FAILED,
        f"requires-python={value!r}",
    )


def _check_uv_flag(pyproject: dict[str, object] | None, key: str, expected: object) -> CheckResult:
    value = _get(pyproject, "tool", "uv", key)
    check_id = {"package": "t4", "required-version": "t5", "python-downloads": "t6"}[key]
    description = f"tool.uv.{key} == {expected!r}"
    if value == expected:
        return CheckResult(check_id, description, PASSED, f"{key}={value!r}")
    return CheckResult(check_id, description, FAILED, f"{key}={value!r}, expected {expected!r}")


def _dev_dependency_strings(pyproject: dict[str, object] | None) -> list[str]:
    groups = _get(pyproject, "dependency-groups")
    if isinstance(groups, dict):
        return _as_string_list(groups.get("dev"))
    return []


def _exact_pins(dependencies: list[str]) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    inexact: list[str] = []
    for dependency in dependencies:
        if "==" in dependency:
            name, _, version = dependency.partition("==")
            pins[name.strip().lower()] = version.strip()
        else:
            inexact.append(dependency)
    return pins, inexact


def _check_dev_pins(pyproject: dict[str, object] | None) -> CheckResult:
    dependencies = _dev_dependency_strings(pyproject)
    if not dependencies:
        return CheckResult(
            "t7",
            "[dependency-groups].dev pins ruff==0.16.9 and pyright==1.1.414",
            FAILED,
            "no [dependency-groups].dev group found",
        )
    pins, inexact = _exact_pins(dependencies)
    missing = [
        f"{name}=={version}"
        for name, version in (("ruff", EXPECTED_RUFF_VERSION), ("pyright", EXPECTED_PYRIGHT_VERSION))
        if pins.get(name) != version
    ]
    if missing or inexact:
        detail = f"missing/inexact={missing} inexactSpecifiers={inexact} found={dependencies}"
        return CheckResult("t7", "[dependency-groups].dev pins ruff==0.16.9 and pyright==1.1.414", FAILED, detail)
    return CheckResult(
        "t7",
        "[dependency-groups].dev pins ruff==0.16.9 and pyright==1.1.414",
        PASSED,
        f"exact pins present: {dependencies}",
    )


def _lock_versions(lock: dict[str, object] | None) -> dict[str, str]:
    versions: dict[str, str] = {}
    packages = lock.get("package") if isinstance(lock, dict) else None
    if isinstance(packages, list):
        for entry in packages:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            version = entry.get("version")
            if isinstance(name, str) and isinstance(version, str):
                versions[name.lower()] = version
    return versions


def _check_lock(repo: Path) -> CheckResult:
    lock_path = repo / "uv.lock"
    lock, error = _load_toml(lock_path)
    if lock is None:
        return CheckResult("t8", "uv.lock pins the exact tool versions", FAILED, error)
    versions = _lock_versions(lock)
    problems = [
        f"{name}=={version} (lock has {versions.get(name)!r})"
        for name, version in (("ruff", EXPECTED_RUFF_VERSION), ("pyright", EXPECTED_PYRIGHT_VERSION))
        if versions.get(name) != version
    ]
    if problems:
        return CheckResult("t8", "uv.lock pins the exact tool versions", FAILED, f"lock problems: {problems}")
    return CheckResult(
        "t8",
        "uv.lock pins the exact tool versions",
        PASSED,
        f"uv.lock pins ruff=={versions.get('ruff')} pyright=={versions.get('pyright')}",
    )


def _check_ruff_config(pyproject: dict[str, object] | None) -> CheckResult:
    selected = _get(pyproject, "tool", "ruff", "lint", "select")
    if selected is None:
        selected = _get(pyproject, "tool", "ruff", "select")
    codes = set(_as_string_list(selected))
    required = {"E", "F", "I"}
    if not required.issubset(codes):
        return CheckResult(
            "t9",
            "[tool.ruff] selects E, F and I",
            FAILED,
            f"select={sorted(codes)} is missing {sorted(required - codes)}",
        )
    return CheckResult("t9", "[tool.ruff] selects E, F and I", PASSED, f"select={sorted(codes)}")


def _check_ruff_line_length(pyproject: dict[str, object] | None) -> CheckResult:
    description = f"[tool.ruff] line-length == {EXPECTED_LINE_LENGTH}"
    value = _get(pyproject, "tool", "ruff", "line-length")
    if value == EXPECTED_LINE_LENGTH:
        return CheckResult("t9b", description, PASSED, f"line-length={value}")
    return CheckResult("t9b", description, FAILED, f"line-length={value!r}")


def _check_pyright_config(
    pyproject: dict[str, object] | None, targets: list[str], generated_dirs: list[str]
) -> CheckResult:
    config = _get(pyproject, "tool", "pyright")
    if not isinstance(config, dict):
        return CheckResult("t10", "[tool.pyright] configures standard type checking", FAILED, "[tool.pyright] missing")
    problems: list[str] = []
    mode = config.get("typeCheckingMode")
    if mode != "standard":
        problems.append(f"typeCheckingMode={mode!r}")
    version = config.get("pythonVersion")
    if version != "3.13":
        problems.append(f"pythonVersion={version!r}")

    include = _as_string_list(config.get("include"))
    if not include:
        problems.append("include missing")
    else:
        uncovered = [target for target in targets if not _pattern_covers(target, include)]
        if uncovered:
            problems.append(f"include does not cover {uncovered[:5]}")

    exclude = _as_string_list(config.get("exclude"))
    if not exclude:
        problems.append("exclude missing")
    else:
        swallowed = [target for target in targets if _pattern_covers(target, exclude)]
        if swallowed:
            problems.append(f"exclude swallows actual targets {swallowed[:5]}")

    # Real generated dirs that are inside the include surface must be excluded;
    # an unused literal placeholder is never required.
    not_excluded = [
        generated
        for generated in generated_dirs
        if _pattern_covers(generated, include) and not _pattern_covers(generated, exclude)
    ]
    if not_excluded:
        problems.append(f"real generated dirs not excluded: {not_excluded}")

    ignore = _as_string_list(config.get("ignore"))
    if ignore:
        swallowed = [target for target in targets if _pattern_covers(target, ignore)]
        if swallowed:
            problems.append(f"ignore swallows actual targets {swallowed[:5]}")

    if problems:
        detail = "; ".join(problems)
        return CheckResult("t10", "[tool.pyright] configures standard type checking", FAILED, detail)
    return CheckResult(
        "t10",
        "[tool.pyright] configures standard type checking",
        PASSED,
        f"standard/3.13 include={include} exclude={exclude} generatedExcluded={generated_dirs}",
    )


def _check_gitignore(repo: Path) -> CheckResult:
    outcomes: list[str] = []
    problems: list[str] = []
    for name in (".venv", "__pycache__", ".ruff_cache", ".pytest_cache"):
        result = _run(["git", "-C", str(repo), "check-ignore", name], repo)
        if result is None:
            problems.append(f"{name}: git check-ignore unavailable")
            continue
        ignored = result.returncode == 0
        outcomes.append(f"{name}={'ignored' if ignored else 'NOT ignored'}")
        if not ignored:
            problems.append(f"{name} is not ignored")
    if problems:
        return CheckResult("t11", ".gitignore ignores .venv and caches", FAILED, "; ".join(problems))
    return CheckResult("t11", ".gitignore ignores .venv and caches", PASSED, "; ".join(outcomes))


def _check_venv_present(repo: Path) -> CheckResult:
    venv_python = repo / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return CheckResult("t12", "uv-managed .venv exists", FAILED, f"missing {venv_python}")
    cfg = repo / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        return CheckResult(
            "t12",
            "uv-managed .venv exists",
            FAILED,
            ".venv/pyvenv.cfg missing; uv management cannot be confirmed",
        )
    text = cfg.read_text(encoding="utf-8", errors="replace").lower()
    if re.search(r"(?m)^\s*uv\s*=", text):
        return CheckResult("t12", "uv-managed .venv exists", PASSED, "pyvenv.cfg carries the explicit uv marker")
    return CheckResult(
        "t12",
        "uv-managed .venv exists",
        FAILED,
        "pyvenv.cfg has no uv marker; environment is not confirmed uv-managed",
    )


def _check_venv_interpreter(repo: Path) -> CheckResult:
    venv_python = repo / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return CheckResult(
            "t13", ".venv interpreter is the preserved pyenv 3.13.11", FAILED, ".venv/bin/python missing"
        )
    script = (
        "import json,sys;print(json.dumps({'version': list(sys.version_info[:3]), 'base_prefix': sys.base_prefix}))"
    )
    result = _run([str(venv_python), "-c", script], repo)
    if result is None or result.returncode != 0:
        return CheckResult(
            "t13",
            ".venv interpreter is the preserved pyenv 3.13.11",
            FAILED,
            "could not run .venv/bin/python",
        )
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(
            "t13", ".venv interpreter is the preserved pyenv 3.13.11", FAILED, "unparseable interpreter probe"
        )
    version = payload.get("version") if isinstance(payload, dict) else None
    base_prefix = payload.get("base_prefix") if isinstance(payload, dict) else None
    expected = [3, 13, 11]
    if version != expected:
        return CheckResult(
            "t13",
            ".venv interpreter is the preserved pyenv 3.13.11",
            FAILED,
            f"interpreter version={version!r}",
        )
    local_pyenv = Path.home() / ".pyenv" / "versions" / EXPECTED_PYTHON_VERSION
    if local_pyenv.is_dir():
        resolved_base = Path(str(base_prefix)).resolve() if base_prefix else None
        if resolved_base != local_pyenv.resolve():
            return CheckResult(
                "t13",
                ".venv interpreter is the preserved pyenv 3.13.11",
                FAILED,
                f"base_prefix={base_prefix!r} != {str(local_pyenv)}",
            )
        return CheckResult(
            "t13",
            ".venv interpreter is the preserved pyenv 3.13.11",
            PASSED,
            f"python {version} base_prefix={base_prefix}",
        )
    if base_prefix and Path(str(base_prefix)).name != EXPECTED_PYTHON_VERSION:
        return CheckResult(
            "t13",
            ".venv interpreter is the preserved pyenv 3.13.11",
            FAILED,
            f"base_prefix={base_prefix!r}",
        )
    return CheckResult(
        "t13",
        ".venv interpreter is the preserved pyenv 3.13.11",
        PASSED,
        f"python {version} base_prefix={base_prefix} (no local pyenv to compare)",
    )


def _check_ci(repo: Path) -> list[CheckResult]:
    path = repo / ".github" / "workflows" / "ci.yml"
    if not path.is_file():
        missing = "ci.yml not found"
        return [
            CheckResult("t14", "CI sets up 3.13.11 and pins uv", FAILED, missing),
            CheckResult("t14b", "CI uses the lock with --locked", FAILED, missing),
            CheckResult("t15", "CI has no large python matrix or ticket loop", FAILED, missing),
        ]
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    setup_problems: list[str] = []
    if EXPECTED_PYTHON_VERSION not in text or "python-version" not in lowered:
        setup_problems.append("no 3.13.11 python-version setup")
    if EXPECTED_UV_VERSION not in text:
        setup_problems.append(f"no uv {EXPECTED_UV_VERSION} pin")
    if setup_problems:
        t14 = CheckResult("t14", "CI sets up 3.13.11 and pins uv", FAILED, "; ".join(setup_problems))
    else:
        t14 = CheckResult("t14", "CI sets up 3.13.11 and pins uv", PASSED, "3.13.11 and uv 0.12.19 present")

    if "--locked" in text:
        t14b = CheckResult("t14b", "CI uses the lock with --locked", PASSED, "--locked present")
    else:
        t14b = CheckResult("t14b", "CI uses the lock with --locked", FAILED, "--locked missing")

    matrix_problem = ""
    list_match = re.search(r"python-version\s*:\s*\[", text)
    if list_match:
        matrix_problem = "python-version is an embedded list"
    for block in re.split(r"\n(?=\s*-\s+(?:name|uses):)", text):
        lowered_block = block.lower()
        if "uv " in lowered_block or "ruff" in lowered_block or "pyright" in lowered_block:
            if "for ticket" in lowered_block:
                matrix_problem = "a quality step loops over tickets"
                break
    if matrix_problem:
        t15 = CheckResult("t15", "CI has no large python matrix or ticket loop", FAILED, matrix_problem)
    else:
        t15 = CheckResult(
            "t15",
            "CI has no large python matrix or ticket loop",
            PASSED,
            "single python-version and no ticket loop in quality steps",
        )
    return [t14, t14b, t15]


def _check_no_type_ignores(repo: Path, targets: list[str]) -> CheckResult:
    pattern = re.compile(r"#\s*type\s*:\s*ignore")
    offenders: list[str] = []
    for target in targets:
        text = (repo / target).read_text(encoding="utf-8", errors="replace")
        if pattern.search(text):
            offenders.append(target)
    if offenders:
        return CheckResult("t16", "no target carries a broad type-ignore", FAILED, f"offenders={offenders}")
    return CheckResult("t16", "no target carries a broad type-ignore", PASSED, "none found")


# ---------------------------------------------------------------------------
# Tool invocation checks
# ---------------------------------------------------------------------------


def _check_version(repo: Path, tool: str, check_id: str, expected: str) -> CheckResult:
    description = f"{tool} --version is exactly {expected}"
    executable = _tool_executable(repo, tool)
    if executable is None:
        return CheckResult(check_id, description, UNEXECUTED, f"{tool} executable not found")
    result = _run([executable, "--version"], repo)
    if result is None:
        return CheckResult(check_id, description, FAILED, f"{tool} --version could not run")
    text = (result.stdout or result.stderr).strip().splitlines()
    observed = text[0].strip() if text else ""
    prefix = f"{tool} {expected}"
    if result.returncode == 0 and observed.startswith(prefix):
        return CheckResult(check_id, description, PASSED, observed)
    return CheckResult(check_id, description, FAILED, f"observed {observed!r}")


def _check_ruff_selection(repo: Path, targets: list[str]) -> CheckResult:
    description = "ruff checks every tools+tests python file"
    executable = _tool_executable(repo, "ruff")
    if executable is None:
        return CheckResult("q4", description, UNEXECUTED, "ruff executable not found")
    result = _run([executable, "check", "--no-cache", "--show-files", *PYTHON_TARGET_ROOTS], repo)
    if result is None or result.returncode != 0:
        return CheckResult("q4", description, FAILED, "ruff --show-files failed")
    selected = {_relative_to_repo(repo, line.strip()) for line in result.stdout.splitlines() if line.strip()}
    expected = set(targets)
    missing = sorted(expected - selected)
    extra = sorted(selected - expected)
    if missing or extra:
        return CheckResult("q4", description, FAILED, f"missing={missing} extra={extra}")
    return CheckResult("q4", description, PASSED, f"{len(selected)} files selected")


def _check_generated_effective(repo: Path, generated_dirs: list[str]) -> CheckResult:
    description = "real generated directories are absent from effective tool file discovery"
    if not generated_dirs:
        return CheckResult("t10b", description, PASSED, "no real generated directories exist in this checkout")
    executable = _tool_executable(repo, "ruff")
    if executable is None:
        return CheckResult("t10b", description, UNEXECUTED, "ruff executable not found")
    result = _run([executable, "check", "--no-cache", "--show-files"], repo)
    if result is None or result.returncode != 0:
        return CheckResult("t10b", description, FAILED, "ruff --show-files failed")
    selected = [_relative_to_repo(repo, line.strip()) for line in result.stdout.splitlines() if line.strip()]
    leaked = [
        path
        for path in selected
        if any(path == generated or path.startswith(generated + "/") for generated in generated_dirs)
    ]
    if leaked:
        return CheckResult("t10b", description, FAILED, f"generated files selected: {leaked[:5]}")
    return CheckResult("t10b", description, PASSED, f"0 of {len(selected)} selected files under generated dirs")


def _ruff_check_command(repo: Path, args: list[str], check_id: str, description: str) -> CheckResult:
    executable = _tool_executable(repo, "ruff")
    if executable is None:
        return CheckResult(check_id, description, UNEXECUTED, "ruff executable not found")
    result = _run([executable, *args], repo)
    if result is None:
        return CheckResult(check_id, description, FAILED, "ruff command could not run")
    if result.returncode == 0:
        return CheckResult(check_id, description, PASSED, "clean")
    tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
    return CheckResult(check_id, description, FAILED, " | ".join(tail))


def _check_pyright(repo: Path, targets: list[str]) -> CheckResult:
    description = "pyright standard checks every tools+tests python file"
    executable = _tool_executable(repo, "pyright")
    if executable is None:
        return CheckResult("q7", description, UNEXECUTED, "pyright executable not found")
    result = _run([executable, "-p", ".", "--outputjson"], repo)
    if result is None:
        return CheckResult("q7", description, FAILED, "pyright could not run")
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        return CheckResult("q7", description, FAILED, "unparseable pyright output: " + " | ".join(tail))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    error_count = summary.get("errorCount") if isinstance(summary, dict) else None
    files_analyzed = summary.get("filesAnalyzed") if isinstance(summary, dict) else None
    if error_count != 0:
        return CheckResult("q7", description, FAILED, f"errorCount={error_count!r}")
    if files_analyzed != len(targets):
        return CheckResult(
            "q7",
            description,
            FAILED,
            f"filesAnalyzed={files_analyzed!r}, expected {len(targets)}",
        )
    return CheckResult("q7", description, PASSED, f"{files_analyzed} files, 0 errors")


def _check_uv_lock(repo: Path) -> CheckResult:
    description = "uv lock --check accepts the lock"
    executable = _tool_executable(repo, "uv")
    if executable is None:
        return CheckResult("q8", description, UNEXECUTED, "uv executable not found")
    result = _run([executable, "lock", "--check"], repo)
    if result is None:
        return CheckResult("q8", description, FAILED, "uv lock --check could not run")
    if result.returncode == 0:
        return CheckResult("q8", description, PASSED, "lock is current")
    tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
    return CheckResult("q8", description, FAILED, " | ".join(tail))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _git_head(repo: Path) -> str | None:
    completed = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], repo)
    if completed is None:
        return None
    head = completed.stdout.strip()
    return head or None


def _write_report(output: Path, report: dict[str, object]) -> None:
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(written, dict) or "e2eVerdict" not in written or written.get("e2eVerdict") is not None:
        raise ValueError("written toolchain artifact lacks an explicit e2eVerdict: null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-23 toolchain acceptance matrix.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    pyproject_path = repo / "pyproject.toml"
    pyproject, pyproject_error = _load_toml(pyproject_path)
    targets = _expected_python_targets(repo)
    generated_dirs = _real_generated_dirs(repo)

    checks: list[CheckResult] = [
        _check_python_version_file(repo),
        _check_pyproject_present(pyproject_path, pyproject, pyproject_error),
        _check_requires_python(pyproject),
        _check_uv_flag(pyproject, "package", False),
        _check_uv_flag(pyproject, "required-version", "==0.12.19"),
        _check_uv_flag(pyproject, "python-downloads", "never"),
        _check_dev_pins(pyproject),
        _check_lock(repo),
        _check_ruff_config(pyproject),
        _check_ruff_line_length(pyproject),
        _check_pyright_config(pyproject, targets, generated_dirs),
        _check_generated_effective(repo, generated_dirs),
        _check_gitignore(repo),
        _check_venv_present(repo),
        _check_venv_interpreter(repo),
        *_check_ci(repo),
        _check_no_type_ignores(repo, targets),
        _check_version(repo, "uv", "q1", EXPECTED_UV_VERSION),
        _check_version(repo, "ruff", "q2", EXPECTED_RUFF_VERSION),
        _check_version(repo, "pyright", "q3", EXPECTED_PYRIGHT_VERSION),
        _check_ruff_selection(repo, targets),
        _ruff_check_command(
            repo,
            ["check", "--no-cache", "--line-length", str(EXPECTED_LINE_LENGTH), *PYTHON_TARGET_ROOTS],
            "q5",
            f"ruff E/F/I check is clean at line-length {EXPECTED_LINE_LENGTH}",
        ),
        _ruff_check_command(
            repo,
            [
                "format",
                "--check",
                "--no-cache",
                "--line-length",
                str(EXPECTED_LINE_LENGTH),
                *PYTHON_TARGET_ROOTS,
            ],
            "q6",
            f"ruff format --check is clean across tools+tests at line-length {EXPECTED_LINE_LENGTH}",
        ),
        _check_pyright(repo, targets),
        _check_uv_lock(repo),
    ]

    failures = [check for check in checks if check.status == FAILED]
    unexecuted = [check for check in checks if check.status == UNEXECUTED]
    ok = not failures and not unexecuted

    report: dict[str, object] = {
        "artifactVersion": 2,
        "kind": "gksa23-toolchain-acceptance-matrix",
        "assertion": "toolchain-config-only",
        "synthetic": True,
        "toolOnly": True,
        "e2eVerdict": None,
        "ok": ok,
        "commit": _git_head(repo),
        "command": [sys.executable, *sys.argv],
        "exit": 0 if ok else 1,
        "repositoryRoot": str(repo),
        "expected": {
            "python": EXPECTED_PYTHON_VERSION,
            "uv": EXPECTED_UV_VERSION,
            "ruff": EXPECTED_RUFF_VERSION,
            "pyright": EXPECTED_PYRIGHT_VERSION,
            "lineLength": EXPECTED_LINE_LENGTH,
        },
        "pythonTargets": targets,
        "generatedDirs": generated_dirs,
        "checks": [check.__dict__ for check in checks],
        "failures": [f"{check.check_id}: {check.detail}" for check in failures],
        "unexecuted": [f"{check.check_id}: {check.detail}" for check in unexecuted],
        "note": (
            "Toolchain contract inspection only: parsed pyproject.toml/uv.lock with the "
            "standard library, inspected CI text and the uv-managed .venv, and invoked the "
            "pinned quality tools when they exist. The dev group is [dependency-groups].dev "
            "only; line-length is frozen at 120 and selection/format cover all of tools+tests; "
            "real generated directories are checked for effective exclusion. No native build, "
            "no game execution, no E2E verdict. unexecuted tool checks are never counted as passes."
        ),
    }
    _write_report(output, report)

    if failures:
        for check in failures:
            sys.stderr.write(f"FAIL [toolchain] {check.check_id}: {check.detail}\n")
    for check in unexecuted:
        sys.stderr.write(f"UNEXECUTED [toolchain] {check.check_id}: {check.detail}\n")
    if not ok:
        sys.stderr.write(
            f"RESULT: FAIL (toolchain matrix; {len(failures)} failed, {len(unexecuted)} unexecuted of {len(checks)})\n"
        )
        return 1
    sys.stderr.write(f"RESULT: PASS (toolchain matrix; {len(checks)} checks)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
