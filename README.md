# GK2 Sermon Reminder

English | [简体中文](./README-CN.md)

This mod provides sermon reminders for *Graveyard Keeper 2*.

[BepInEx](https://github.com/bepinex/bepinex) loads the mod, and the mod depends on [GK2 Mod Framework](https://github.com/SuperMan4eg/GK2-Mod-Framework).

## Features

### Sermon information beneath the clock

After you complete the quests that unlock the church and the sermon tutorial, the mod displays a reminder beneath the clock.

| Game state | Display |
| --- | --- |
| Sermon Day, no sermon held yet | Sermon Day! |
| Sermon Day, sermon already held | This week’s sermon is complete! |
| One day before Sermon Day | Sermon Day is Tomorrow! |
| More than one day before Sermon Day | N days before the Sermon Day. |

Before you unlock sermons, this mod does nothing.

### Church beacon

When Sermon Day arrives and you have not held the sermon yet, the mod displays a beacon that points to the church.

This feature is enabled by default.

### Ultimate Reminder

When Sermon Day arrives and you have not held the sermon yet, the mod pops up a reminder using the game's native window.

This feature is disabled by default.

### i18n support

The project provides Simplified Chinese and English by default.

The mod uses Simplified Chinese when the game language is Simplified Chinese, and English for every other language.

## Installation

If you have not installed BepInEx or any other mod before, install them first:

1. Download [BepInEx 5.x (Unity Mono)](https://github.com/bepinex/bepinex/releases); the mod needs BepInEx to run properly.
2. Drag the downloaded archive into the game root directory to install BepInEx.
3. Launch the game at least once so that BepInEx generates the required directories.

Once you have installed BepInEx:

1. Download [GK2 Mod Framework](https://www.nexusmods.com/graveyardkeeper2/mods/42), the framework this mod depends on.
2. Put GK2 Mod Framework and this mod into the BepInEx plugins directory together.

Example:
```text
Game root directory/
└── BepInEx/
    └── plugins/
        └── GK2.SermonReminder/     <--- This mod
        └── GK2.Framework.dll       <--- GK2 Mod Framework
```

## Settings

Adjust the following options in the "GK2 Sermon Reminder" group of the mod settings:

| Option | Default | Effect |
| --- | --- | --- |
| Enable church beacon | On | Controls the church direction beacon |
| Ultimate Reminder | Off | Controls the sermon reminder pop-up |

Setting changes are saved immediately. After you change a toggle, reload your save for it to take effect.

## AI development notice

This mod was developed with generative AI. If AI-generated mods bother you, please do not use it.

## Compatibility

This mod has only been tested in a Linux Proton environment and has not been tested on Windows.

If you run into a compatibility problem, submit an issue with your logs to help the maintainer locate the problem.

## Building from source

### Development environment

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

### Prepare the Python tool environment

After you install the Python and uv versions listed above, run:

```sh
uv sync --locked --python 3.13.11
```

### Build the plugin

Replace the path placeholders below with the actual paths on your machine, then run:

```sh
uv run --locked python tools/local_build.py \
  --dotnet "/path/to/dotnet" \
  --game-dir "/path/to/Graveyard Keeper 2" \
  --bepinex-dir "/path/to/BepInEx" \
  --framework-dir "/path/to/folder-containing-GK2.Framework.dll"
```

`--game-dir` points to the game root directory that contains `GraveyardKeeper2_Data`.
`--bepinex-dir` points to the BepInEx directory that contains `core`.
`--framework-dir` points to the directory that directly contains `GK2.Framework.dll`.

Inspect `artifacts/local-build/local-build-report.json` and confirm that `ok` is `true`.

Plugin output location:

```text
src/GK2.SermonReminder/bin/Release/netstandard2.1/GK2.SermonReminder.dll
```

### Create the distribution package

After a successful build, run:

```sh
uv run --locked python tools/package.py
```

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](./CONTRIBUTING.md) before you submit.
Target the `dev` branch with your pull requests.

## Project structure

```text
src/GK2.SermonReminder/   Plugin source
├── State/                Game state reads
├── Hud/                  Reminder display beneath the clock
├── Beacon/               Church beacon
├── Popup/                Sermon reminder pop-up
├── Notifications/        Fault notifications
├── Localization/         Localization resources
└── Settings/             Setting descriptors

tools/                    Build, packaging, and source check tools
tests/e2e/                Game scenario manifests and evidence verifier
```

## License

This project is licensed under the [MIT License](LICENSE).
