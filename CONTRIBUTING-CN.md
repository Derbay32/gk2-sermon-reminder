# 贡献指南

感谢你愿意为本模组进行贡献。

如果你使用编码代理，请配合 [AGENTS.md](AGENTS.md) 开发，并详细阅读本文档的 `Coding Agent 相关` 部分。

## 开始之前

- 说明本次贡献要解决的问题。
- 一个 PR 只围绕一个明确的修改目的，不应当包含彼此不相关的改动。
- 在新增功能或调整现有行为前，请详细说明你的预期结果。
- 保留其他贡献者尚未提交的修改。
- 不要提交游戏文件、原生 DLL、存档或包含隐私的日志。

## Coding Agent 相关

项目接受通过 Claude Code、Codex 等编码代理贡献的代码，但提交者应当对其代码负责。

当你通过编码代理提交合并请求前，请确保：

- 你已充分理解了本次的改动内容
- 确保代码符合当前项目的结构
- 在 PR 中**亲自**说明你解决了什么问题、或者新增了什么功能

注意：请不要让 Agent 提交 PR 说明，维护者会拒绝纯 AI 提交的请求。

## 开发

Fork 这个仓库，然后克隆你自己的仓库：

```bash
git clone https://github.com/your_username/gk2-sermon-reminder.git
cd gk2-sermon-reminder
```

在你自己的仓库创建基于 `dev` 的新分支，修改完成后，申请合并到该仓库 `dev` 分支的 Pull Request。

### 安装项目依赖

| 依赖 | 版本 | 配置来源 |
| --- | --- | --- |
| .NET SDK | 8.0.425 | [global.json](global.json) |
| 插件目标框架 | netstandard2.1 | [项目文件](src/GK2.SermonReminder/GK2.SermonReminder.csproj) |
| Python | 3.13.11 | [.python-version](.python-version) |
| uv | 0.12.19 | [pyproject.toml](pyproject.toml) |
| Ruff | 0.16.9 | [pyproject.toml](pyproject.toml) |
| Pyright | 1.1.414 | [pyproject.toml](pyproject.toml) |
| BepInEx (Unity Mono) | 5.4.23.5| - |
| GK2 Mod Framework | 0.1.14 | - |

### 构建与打包

使用 `tools/local_build.py` 构建真实插件。

构建命令示例见 [README-CN.md](README-CN.md)。

显式传入以下参数：

| 参数 | 指向的位置 |
| --- | --- |
| `--dotnet` | 固定版本 SDK 的可执行文件 |
| `--game-dir` | 包含 `GraveyardKeeper2_Data` 的游戏根目录 |
| `--bepinex-dir` | 包含 `core` 的 BepInEx 目录 |
| `--framework-dir` | 直接包含 `GK2.Framework.dll` 的目录 |

## 分支与 PR 要求

请将所有的 Pull Request 合并入 `dev` 分支。

项目的 `main` 分支仅用于最终发版，请不要将任何代码直接合并入 `main`。

使用简洁的英文提交标题，例如 `docs: clarify translation requirements`。

将提交标题控制在 60 个字符以内。

在 PR 说明中包含以下信息：

- 说明修改目的。
- 列出用户可见的变化。
- 列出执行过的检查及结果。
- 列出未执行的验证及原因。
- 对 UI 或翻译改动提供可用的显示证据。

## 代码贡献规范

### C#

- 遵循 [.editorconfig](.editorconfig) 的格式要求。
- 使用四空格缩进。
- 使用 LF 换行。
- 将 `using` 放在命名空间外。
- 保留现有块级命名空间风格。
- 遵循 [Directory.Build.props](Directory.Build.props) 的分析器配置。
- 不通过扩大告警屏蔽范围绕过检查。
- 遵循本项目的 i18n 国际化规范，禁止硬编码任何最终显示文本。

### Python

- 遵循 `pyproject.toml` 中的 Ruff 与 Pyright 配置。
- 使用 Ruff 检查 `E`、`F`、`I` 规则。
- 使用 Ruff 格式化，行宽为 120。
- 使用 Pyright 的 `standard` 模式检查类型。
- 保留工具的失败退出码与机器可读报告语义。

### 行为与验证

- 在实现前明确预期行为与失败场景，并优先补充端到端或黑盒验收。
- 测试应基于预期行为设计，不应根据既有实现反向调整断言以使测试通过。
- 将未执行的检查明确标为未执行，保留可复验的验证证据。
- 不通过删除模组行为断言掩盖回归。
- 将游戏状态读取失败与正常的隐藏状态区分处理。
- 只清理本模组拥有的 UI 资源和 Harmony 补丁。

### 检查

在贡献代码前，请确保你已根据你修改的范围，运行了对应的检查：

| 改动范围 | 检查 | 可观察结果 |
| --- | --- | --- |
| Python 工具 | `uv run --locked ruff check tools tests` | 无 lint 错误 |
| Python 格式 | `uv run --locked ruff format --check tools tests` | 无待格式化文件 |
| Python 类型 | `uv run --locked pyright` | 无类型错误 |
| C# 格式 | `dotnet format whitespace src/GK2.SermonReminder --folder --verify-no-changes --exclude bin --exclude obj` | 无格式差异 |

### 实机验证

在贡献代码前，请确保你的提交已在游戏内测试通过，且实际表现与你的预期一致。

## 翻译贡献

### 当前语言支持

| 语言 | 资源文件 | 游戏语言标识 |
| --- | --- | --- |
| 简体中文 | [gksr.zh_cn.json](src/GK2.SermonReminder/Localization/gksr.zh_cn.json) | `zh_cn` |
| 英文 | [gksr.en.json](src/GK2.SermonReminder/Localization/gksr.en.json) | `en` |

当前语言选择逻辑只将游戏的 `zh_cn` 映射到简体中文。

其余语言标识默认回退至英文。

### 翻译标准

- 以**简体中文**作为翻译基准。
- 修改 JSON 的文本值，不要翻译或重命名 `gksr.*` 键。
- 使用 UTF-8 保存文件。
- 保持现有两空格 JSON 缩进。
- 不加入 JSON 注释、重复键或尾随逗号。
- 所有值都应当为非空字符串。
- 原样保留占位符名称，例如 `{days}`。
- 使用 `\n` 表示 JSON 字符串中的换行。
- 使用游戏内一致的术语，例如将 sermon 对应为「布道」。
- 保持 HUD 短文案简洁，避免遮挡游戏界面。

例如，`gksr.hud.sermonCountdown.other` 中的 `{days}` 必须原样保留，不能译为 `{天数}`。

### 修订已有译文

如果你只想修订已有的翻译错误，仅需提交针对对应语言 JSON 文件的修改。

维护者无法提供对其他语言的校对，只会检查你的提交文件格式。

### 新增翻译

新增翻译涉及到对代码的修改，你可以仅提交新的 JSON 文件，也可以一并修改代码使其生效。

如果你不懂代码，维护者会在 PR 中帮助你完成修改后再合并。

当你创建新的 JSON 文件时，使用本项目约定的语言标识，优先采用 BCP 47 格式：

```
gksr.语言代码.json
```
