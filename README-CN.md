# GK2 布道提醒

[English](./README.md) | 简体中文

这个模组为游戏《守墓人2》提供了提醒布道的功能。

模组通过 [BepInEx](https://github.com/bepinex/bepinex) 加载，依赖 [GK2 Mod Framework](https://github.com/SuperMan4eg/GK2-Mod-Framework)。

## 模组功能

### 时钟下方的布道信息

在完成教堂解锁与布道教学相关任务后，模组会在时钟的下方显示一个提醒。

| 游戏状态 | 显示 |
| --- | --- |
| 布道日尚未布道 | 布道日！ |
| 布道日已完成布道 | 已完成本周布道！ |
| 距离布道日还有一天 | 明天是布道日！ |
| 距离布道日还有多天 | 距离下一次布道日还有 N 天。 |

当你没有解锁布道前，这个模组不会工作。

### 教堂信标

在布道日尚未布道时，显示指向教堂的信标。

该功能默认开启。

### 究极提醒

在布道日尚未布道时，使用游戏原生窗口弹出提醒。

该功能默认关闭。

### i18n 支持

项目默认提供简体中文与英文支持。

游戏语言为简体中文时使用简体中文，其余语言使用英文。

## 安装方式

如果你之前没有安装过 BepInEx 或者其他 Mod，你需要先安装它们：

1. 下载 [BepInEx 5.x (Unity Mono)](https://github.com/bepinex/bepinex/releases)，模组需要使用 BepInEx 才能正常运行。
2. 将你下载的压缩包拖进游戏根目录，安装 BepInEx。
3. 至少启动一次游戏，让 BepInEx 生成必要的目录。

当你完成了 BepInEx 的安装：

1. 下载 [GK2 Mod Framework](https://www.nexusmods.com/graveyardkeeper2/mods/42)，这是模组的前置框架。
2. 将 GK2 Mod Framework 与本模组同时放入 BepInEx 的 plugins 目录。

示例：
```text
游戏根目录/
└── BepInEx/
    └── plugins/
        └── GK2.SermonReminder/     <--- 本模组
        └── GK2.Framework.dll       <--- GK2 Mod Framework
```

## 设置

在模组设置的「GK2布道提醒」分组中调整以下选项：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| 开启教堂信标提醒 | 开启 | 控制教堂方向信标 |
| 究极提醒 | 关闭 | 控制布道提醒弹窗 |

设置修改会立即保存。修改开关后，需要重新加载存档以生效。

## AI 开发注意

本模组使用了生成式 AI 开发，如果你介意 AI Mod，请不要使用。

## 兼容性

本模组仅在 Linux Proton 环境中测试，并未测试过 Windows 平台。

如果你遇到了兼容性问题，请提交 Issue 与日志，帮助维护者定位问题。

## 从源码构建

### 开发环境

| 依赖 | 版本 | 配置来源 |
| --- | --- | --- |
| .NET SDK | 8.0.425 | [global.json](global.json) |
| 插件目标框架 | netstandard2.1 | [项目文件](src/GK2.SermonReminder/GK2.SermonReminder.csproj) |
| Python | 3.13.11 | [.python-version](.python-version) |
| uv | 0.12.19 | [pyproject.toml](pyproject.toml) |
| Ruff | 0.16.9 | [pyproject.toml](pyproject.toml) |
| Pyright | 1.1.414 | [pyproject.toml](pyproject.toml) |
| BepInEx (Unity Mono) | 5.4.23.5| - |
| GK2 Mod Framework | 0.1.9 | - |

### 准备 Python 工具环境

安装上述版本的 Python 与 uv 后，执行：

```sh
uv sync --locked --python 3.13.11
```

### 构建插件

将以下路径占位符替换为本机的实际路径后执行：

```sh
uv run --locked python tools/local_build.py \
  --dotnet "/path/to/dotnet" \
  --game-dir "/path/to/Graveyard Keeper 2" \
  --bepinex-dir "/path/to/BepInEx" \
  --framework-dir "/path/to/folder-containing-GK2.Framework.dll"
```

`--game-dir` 指向包含 `GraveyardKeeper2_Data` 的游戏根目录。
`--bepinex-dir` 指向包含 `core` 的 BepInEx 目录。
`--framework-dir` 指向直接包含 `GK2.Framework.dll` 的目录。

检查 `artifacts/local-build/local-build-report.json`，确认 `ok` 为 `true`。

插件输出位置：

```text
src/GK2.SermonReminder/bin/Release/netstandard2.1/GK2.SermonReminder.dll
```

### 生成分发包

构建成功后执行：

```sh
uv run --locked python tools/package.py
```

## 贡献

欢迎 Issue 与 PR。提交前请阅读 [CONTRIBUTING-CN.md](./CONTRIBUTING-CN.md)。
PR 请以 `dev` 为目标分支。

## 项目结构

```text
src/GK2.SermonReminder/   插件源码
├── State/               游戏状态读取
├── Hud/                 时钟下方的提醒显示
├── Beacon/              教堂信标
├── Popup/               布道提醒弹窗
├── Notifications/       故障通知
├── Localization/        本地化资源
└── Settings/            设置描述

tools/                   构建、打包与源码检查工具
tests/e2e/               游戏场景清单与证据校验器
```

## 许可证

本项目采用 [MIT License](LICENSE)。
