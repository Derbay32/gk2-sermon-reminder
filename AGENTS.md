# GK2 Sermon Reminder Project Guide

## Purpose

This project provides sermon reminders for Graveyard Keeper 2.

BepInEx loads the plugin.

The plugin depends on GK2 Mod Framework 0.1.14.

The delivered assembly is `GK2.SermonReminder.dll`.

Python is used only for development tools and acceptance checks, not for the plugin runtime.

Main features:

- Display pending or completed sermon status beneath the native clock on sermon days.
- Display an integer countdown to the next sermon day on other days.
- Provide a configurable church direction beacon.
- Provide a configurable sermon reminder popup.
- Provide local fault notifications and recovery handling.
- Provide Simplified Chinese and English text.

## Reading Map

| Path | Responsibility |
| --- | --- |
| `src/GK2.SermonReminder/SermonReminderPlugin.cs` | Plugin registration and mod lifecycle coordination |
| `src/GK2.SermonReminder/MainGameLifecyclePatches.cs` | Harmony hooks for native save loading, return-to-menu, and save deletion events |
| `src/GK2.SermonReminder/State/` | Native state reads and immutable display snapshots |
| `src/GK2.SermonReminder/Hud/` | Display beneath the clock and native completion icon adaptation |
| `src/GK2.SermonReminder/Beacon/` | Church direction beacon |
| `src/GK2.SermonReminder/Popup/` | Borrowing and lifecycle management of the native reminder window |
| `src/GK2.SermonReminder/Notifications/` | Fault notifications |
| `src/GK2.SermonReminder/Localization/` | Localization loading and embedded language resources |
| `src/GK2.SermonReminder/Settings/` | Localized setting descriptors |
| `tools/` | Source checks, manifest loading, real-reference builds, and packaging tools |
| `tests/e2e/` | Game scenario manifests and observed-evidence validation |
| `.github/workflows/ci.yml` | Actual hosted CI check definitions |

Read the domain terminology in `CONTEXT.md` first when that file exists.

Read relevant decisions under `docs/adr/` first when they exist.

Do not create domain documentation merely to complete the directory structure.

## Key Behavioral Boundaries

- Read sermon state from the active save instead of substituting a cached judgment after a read failure.
- Read the sermon weekday from the game balance constant instead of hardcoding a weekday ordinal.
- Distinguish unmet quest gates from native state read failures.
- Use `Hidden`, `Countdown`, `Ready`, and `Done` as explicit display states.
- Preserve the snapshot's `Readable=false` semantics after a read failure.
- Capture the beacon and popup toggle values for the current load when that load becomes ready.
- Defer toggle changes made during a load until the next load.
- Release only mod-owned UI resources during cleanup.
- Remove only this mod's Harmony patches during cleanup.
- Do not treat log output as evidence that UI was actually displayed.

Start reading state rules at the implementation in `State/SermonStateReader.cs`.

Start reading UI resource ownership rules at the corresponding module's lifecycle code.

## Toolchain and Builds

Use the following files as the authority for version constraints:

- Use the .NET SDK pinned by `global.json`; the current version is 8.0.425.
- Use the target framework declared in the `.csproj`; the current target is `netstandard2.1`.
- Use Python pinned by `.python-version`; the current version is 3.13.11.
- Use uv pinned by `pyproject.toml`; the current version is 0.12.19.
- Use the development dependencies locked in `uv.lock`.

Run `uv sync --locked --python 3.13.11` to prepare the development environment; expect exit code 0; verify the interpreter version with `uv run --locked python --version`.

A real plugin build requires real assemblies from the game, BepInEx, and GK2 Mod Framework.

Do not use fake DLLs or stubs as substitutes for a real-reference build.

Do not encode local installation paths as project defaults.

`GameDir` is a compile-time reference root, not a runtime setting.

MSBuild accepts an explicit `GameDir`, `GameDataDir`, or `GameManagedDir`.

`tools/local_build.py` requires explicit `--dotnet`, `--game-dir`, `--bepinex-dir`, and `--framework-dir` arguments.

Use that tool to generate a build provenance report instead of treating a bare `dotnet build` as provenance evidence.

Use `tools/package.py` to package files from the explicit allowlist.

The distribution ZIP may contain only the plugin DLL, the repository LICENSE, and plugin metadata.

Keep native references set to `Private=false`.

Do not commit or distribute game or third-party assemblies.

## Validation

Select checks according to the impact of a change instead of running game acceptance checks for documentation-only changes.

Restrict tests to mod behavior rather than development tools or quality gates.

Run the following commands from the repository root.

Each command is expected to exit with code 0.

| Check | Command | Observable Result |
| --- | --- | --- |
| Python static analysis | `uv run --locked ruff check tools tests` | No lint errors |
| Python formatting | `uv run --locked ruff format --check tools tests` | No files require formatting |
| Python types | `uv run --locked pyright` | No type errors |
| Game manifest structure | `uv run --locked python tests/e2e/verify.py --manifest tests/e2e/manifest.json --check-manifest --output artifacts/checks/manifest.json` | Report contains `ok=true` |
| C# source formatting | `dotnet format whitespace src/GK2.SermonReminder --folder --verify-no-changes --exclude bin --exclude obj` | No formatting differences |

When real references are unavailable, report the real build as not executed.

Hosted CI does not execute game scenarios.

Hosted CI does not build the plugin against real game assemblies.

`tests/e2e/verify.py` validates captured evidence rather than launching or simulating the game.

Game acceptance requires a capture from an actual run and the required evidence files.

Do not describe a successful `--check-manifest` result as a passing game E2E run.

Do not describe example captures or synthetic data as actual game observations.
