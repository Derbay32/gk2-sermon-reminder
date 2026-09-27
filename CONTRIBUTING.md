# Contributing Guide

Thank you for contributing to this mod.

If you use a coding agent, develop with [AGENTS.md](AGENTS.md) and read the "Coding agents" section of this document carefully.

## Before you start

- State the problem this contribution solves.
- Keep one pull request focused on one clear purpose, and exclude unrelated changes.
- Describe your expected result in detail before you add a feature or change existing behavior.
- Preserve the changes other contributors have not committed yet.
- Do not commit game files, native DLLs, save files, or logs that contain private data.

## Coding agents

The project accepts code contributed through coding agents such as Claude Code and Codex, but the submitter is responsible for that code.

Before you submit a merge request produced by a coding agent, make sure:

- You fully understand the change.
- The code matches the current project structure.
- You explain **yourself** in the pull request which problem you solved or which feature you added.

Note: do not let an agent write the pull request description. The maintainer rejects requests that are purely AI submissions.

## Development

Fork this repository，then clone yours:

```bash
git clone https://github.com/your_username/gk2-sermon-reminder.git
cd gk2-sermon-reminder
```

Create a new branch based on `dev` in your own repository. After you finish your changes, open a pull request that merges into the `dev` branch of this repository.

### Install project dependencies

| Dependency | Version | Configuration source |
| --- | --- | --- |
| .NET SDK | 8.0.425 | [global.json](global.json) |
| Plugin target framework | netstandard2.1 | [project file](src/GK2.SermonReminder/GK2.SermonReminder.csproj) |
| Python | 3.13.11 | [.python-version](.python-version) |
| uv | 0.12.19 | [pyproject.toml](pyproject.toml) |
| Ruff | 0.16.9 | [pyproject.toml](pyproject.toml) |
| Pyright | 1.1.414 | [pyproject.toml](pyproject.toml) |
| BepInEx (Unity Mono) | 5.4.23.5 | - |
| GK2 Mod Framework | 0.1.9 | - |

### Build and package

Build the real plugin with `tools/local_build.py`.

See [README.md](README.md) for an example build command.

Pass the following arguments explicitly:

| Argument | Target |
| --- | --- |
| `--dotnet` | Executable of the pinned SDK version |
| `--game-dir` | Game root directory that contains `GraveyardKeeper2_Data` |
| `--bepinex-dir` | BepInEx directory that contains `core` |
| `--framework-dir` | Directory that directly contains `GK2.Framework.dll` |

## Branch and pull request requirements

Merge every pull request into the `dev` branch.

The `main` branch of the project is only for final releases; do not merge any code directly into `main`.

Use a concise English commit title, for example `docs: clarify translation requirements`.

Keep the commit title within 60 characters.

Include the following information in the pull request description:

- State the purpose of the change.
- List user-visible changes.
- List the checks you ran and their results.
- List the verification you did not run and the reason.
- Provide usable display evidence for UI or translation changes.

## Code contribution rules

### C#

- Follow the formatting requirements in [.editorconfig](.editorconfig).
- Use four-space indentation.
- Use LF line endings.
- Place `using` directives outside the namespace.
- Keep the existing block-scoped namespace style.
- Follow the analyzer configuration in [Directory.Build.props](Directory.Build.props).
- Do not bypass checks by widening the warning suppression scope.
- Follow the i18n rules of this project, and never hardcode any final display text.

### Python

- Follow the Ruff and Pyright configuration in `pyproject.toml`.
- Check the `E`, `F`, and `I` rules with Ruff.
- Format with Ruff at a line width of 120.
- Check types with Pyright in `standard` mode.
- Preserve the failure exit codes and the machine-readable report semantics of the tools.

### Behavior and verification

- Define the expected behavior and the failure cases before implementation, and add end-to-end or black-box acceptance first.
- Design tests from the expected behavior; do not adjust assertions backwards to make tests pass against the existing implementation.
- Mark the checks you did not run as not run, and keep reproducible verification evidence.
- Do not hide regressions by deleting mod behavior assertions.
- Treat a failed game state read and a normal hidden state as separate cases.
- Clean up only the UI resources and the Harmony patches owned by this mod.

### Checks

Before you contribute code, run the checks that match the scope of your change:

| Change scope | Check | Observable result |
| --- | --- | --- |
| Python tools | `uv run --locked ruff check tools tests` | No lint errors |
| Python formatting | `uv run --locked ruff format --check tools tests` | No files need formatting |
| Python types | `uv run --locked pyright` | No type errors |
| C# formatting | `dotnet format whitespace src/GK2.SermonReminder --folder --verify-no-changes --exclude bin --exclude obj` | No formatting differences |

### In-game verification

Before you contribute code, make sure your commit has passed in-game testing and that the actual behavior matches your expectation.

## Translation contributions

### Current language support

| Language | Resource file | Game language identifier |
| --- | --- | --- |
| Simplified Chinese | [gksr.zh_cn.json](src/GK2.SermonReminder/Localization/gksr.zh_cn.json) | `zh_cn` |
| English | [gksr.en.json](src/GK2.SermonReminder/Localization/gksr.en.json) | `en` |

The current language selection logic maps only the game's `zh_cn` to Simplified Chinese.

Every other language identifier falls back to English.

### Translation standards

- Use **Simplified Chinese** as the translation baseline.
- Change the text values in JSON; do not translate or rename the `gksr.*` keys.
- Save files as UTF-8.
- Keep the existing two-space JSON indentation.
- Do not add JSON comments, duplicate keys, or trailing commas.
- Make every value a non-empty string.
- Keep placeholder names as they are, for example `{days}`.
- Use `\n` for a line break inside a JSON string.
- Use terminology consistent with the game; for example, sermon corresponds to 布道.
- Keep HUD text short so that it does not obscure the game interface.

For example, the `{days}` placeholder in `gksr.hud.sermonCountdown.other` must stay as it is and must not be translated into `{天数}`.

### Revise an existing translation

If you only want to fix an existing translation error, submit changes to the JSON file of that language only.

The maintainer cannot proofread other languages and only checks the format of the files you submit.

### Add a translation

Adding a translation involves code changes. You may submit only the new JSON file, or change the code as well so that the translation takes effect.

If you do not understand the code, the maintainer helps you complete the change in the pull request before merging.

When you create a new JSON file, use the language identifier conventions of this project and prefer the BCP 47 format:

```
gksr.language-code.json
```
