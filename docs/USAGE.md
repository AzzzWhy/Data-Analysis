# GPU加速与数据分析 · 使用说明 / User Guide

> [English version](#english)

## 中文

这个 Agent 接收自然语言分析问题，在**运行 Agent 的机器上**调用本地分析工具，并根据实际任务与环境使用 cuDF/GPU 或 pandas/CPU。模型负责选择操作和解释工具结果；不要把某次 CPU/GPU 路由当成整台机器的固定模式。

### 1. 安装并启动

在要执行分析的机器上安装 Python 3.10+，然后取得项目代码：

```bash
git clone https://github.com/AzzzWhy/Data-Analysis.git
cd Data-Analysis
python -m pip install -r requirements.txt -r requirements-tui.txt
python agent/agent_main.py
```

如果你从 SSH 登录服务器，就在 SSH 会话里运行以上命令。`requirements.txt` 包含 CPU 路径所需的依赖；要在有 NVIDIA GPU 的机器上使用 cuDF，需要另外安装与该机器 CUDA、Python 和系统匹配的 RAPIDS 环境。没有可用的 cuDF 时，工具会报告实际的 pandas/CPU 执行，不能把它称为 GPU 加速。

首次进入交互式 TUI 会显示连接设置。如果未安装可选的 Textual，程序会退回基本终端界面。请使用交互式终端；`--ask` 和管道输入不会弹出设置窗口。

### 2. 连接 API、选择模型与语言

启动设置中依次填写：

1. **API 基础地址**：OpenAI 兼容接口的 base URL，例如 `https://api.example.com/v1`，不是聊天网页地址。远程服务必须使用 HTTPS；本机服务可使用 `http://localhost`。
2. **API Key**：输入会隐藏。无鉴权的本机兼容服务可填 `local`。
3. 点击**读取此地址的模型列表**，选择模型；若服务不提供 `/models`，可直接手填模型 ID。
4. 在**界面语言 / UI language** 中选择**中文**或 **English**，然后选**保存并进入**。

选中的模型需要支持 *Chat Completions* 和工具/函数调用。模型列表只证明服务返回了 ID，不证明该模型能完成本项目的工具调用。原生 Anthropic Messages、Gemini 原生接口及仅支持 Responses 的接口没有在此实现适配；如果服务提供 OpenAI 兼容入口，可填写其兼容地址。

默认只保存地址、模型、语言和提示偏好，**不会保存 API Key**。下次启动需重新输入密钥，或通过环境变量提供。只有主动勾选“保存密钥到本机明文文件”才会把密钥写入 `~/.config/gpu-data-analysis/connection.json`；该文件并未加密，不要分享。Linux/macOS 创建为仅当前用户可读写（`0600`）；Windows 使用配置目录的 ACL。取消勾选并保存会移除已保存的密钥。

“暂时跳过”只跳过本次设置；“忽略，不再提示”会记住以后不自动弹出，但不会采用尚未保存的表单内容。即使关闭启动提示，仍可输入 `/settings`、按 F5，或下次运行时使用 `--configure` 重新打开。未配置密钥或模型时无法执行分析，但仍可打开设置。

原始数据由本机工具读取；提问中包含的文件路径，以及工具返回的统计摘要会发给你选择的模型服务。不要在问题或路径中放入不希望该服务看到的敏感信息。

设置界面可选择语言；主界面还可输入 `/language en` 或 `/language zh`，或按 F6 随时切换。选择会保存到本机配置文件。**语言选项翻译的是 TUI 控件与提示，不会自动翻译模型生成的分析答案，也不会改写原始执行日志。**提问使用哪种语言，会影响模型回答语言，但并非由 TUI 保证。

### 3. 选择数据与提问

在底部输入区输入：

```text
/file /absolute/path/to/sales.csv
```

也可输入 `/file` 或按 F3 打开文件输入区，填入路径后按 Enter。这里必须是**运行 Agent 的机器**上已经存在且可读的文件；从 SSH 使用时是服务器路径，输入 Windows 桌面路径并不会自动上传文件。路径有空格时，`/file` 后直接输入完整路径即可；文件输入区按 Esc 返回。

随后直接输入分析问题，按 Enter 发送。例如：

```text
按地区统计 revenue 的总额与均值，指出最高和最低的地区。
```

顶部显示当前模型、API 主机和文件名。状态栏显示工具实际使用的 CPU/GPU 引擎与用时。Ctrl+O、F2 或 `/logs` 可查看执行详情；结果、日志中的原始数值不会因语言切换而改写。常见输入格式包括 CSV、TSV、Parquet、JSON/JSONL 和 Excel；某些格式需额外安装 `pyarrow` 或 `openpyxl`，GPU 支持取决于 RAPIDS 能力。

### 4. 快捷操作

| 操作 | 输入 / 按键 |
| --- | --- |
| 选择文件 | `/file 路径`、`/file`、F3 |
| 连接地址、密钥、模型和语言 | `/settings`、F5 |
| 切换 TUI 语言 | `/language en`、`/language zh`、F6 |
| 执行详情 | `/logs`、Ctrl+O、F2 |
| 帮助 | `/help`、F1 |
| 输入框 | Ctrl+L、F4 |
| 安全退出 | `/quit`、Ctrl+Q、F10 |

分析仍在进行时选择退出会等待当前模型/工具操作结束和会话内存清理；这不是立即取消 CUDA 或网络请求。更换 API 地址、密钥或模型会开启新的后端对话，旧答案仍可查看，但不会发给新的服务。正在分析时不能切换连接。

### 5. 脚本与无界面运行

无界面任务不要依赖弹窗。可通过 `GPU_API_BASE_URL`、`GPU_API_KEY`、`GPU_API_MODEL` 设置连接；也接受 `OPENAI_*` 别名，原有 `STEPFUN_*` 环境变量仍兼容。密钥不要放在命令行参数、Git 仓库、截图或共享日志里。

```bash
python agent/agent_main.py --ask "分析 /absolute/path/to/sales.csv 的异常值" --plain
```

`--plain` 使用基础终端，`--language en` / `--language zh` 只指定 TUI 语言，不会翻译基本终端的所有文本。`GPU_ANALYSIS_CONFIG` 可指定配置文件位置；`XDG_CONFIG_HOME` 可改变默认配置目录。

### 6. 常见问题

- **模型列表读取失败**：检查基础地址、密钥和网络；若服务没有 `/models`，手填模型 ID。不要把失败当成密钥必然无效。
- **模型不能调用工具**：换用支持 Chat Completions 工具/函数调用的模型；只出现在列表里不够。
- **文件找不到**：确认文件在执行 Agent 的机器上，而不是仅在你登录所用的电脑上；检查绝对路径及读取权限。
- **状态显示 CPU**：小数据或 GPU/依赖不可用时可能正常走 pandas。以执行详情里的实际引擎与路由理由为准。
- **关闭启动提示后无法分析**：默认没有保存密钥，使用 `/settings` 重新输入，或提供环境变量。F5 设置入口始终保留。

## English

This agent runs analysis tools **on the machine that runs the agent**. Depending on the task and available libraries, the tool may use cuDF/GPU or pandas/CPU. The model chooses operations and explains the returned results; the actual engine is reported by the tool.

### 1. Install and start

Install Python 3.10+ on the analysis machine, then:

```bash
git clone https://github.com/AzzzWhy/Data-Analysis.git
cd Data-Analysis
python -m pip install -r requirements.txt -r requirements-tui.txt
python agent/agent_main.py
```

If you work through SSH, run these commands **inside the SSH session**. The requirements include the CPU path. For GPU acceleration, install a RAPIDS cuDF environment compatible with that machine's CUDA, OS and Python. Without cuDF, the tool reports its actual pandas/CPU execution; it does not claim a GPU run.

An interactive TUI opens connection setup at startup. Without the optional Textual dependency, a basic terminal interface is used. `--ask` and piped input do not open setup dialogs.

### 2. Connect an API, select a model and choose a language

Enter an OpenAI-compatible **API base URL** (for example `https://api.example.com/v1`), a masked API key, and a model ID. Remote endpoints must use HTTPS; local compatible services may use `http://localhost` (enter `local` as the key if they do not authenticate). Press **Fetch models from this address** to read `/models`, then select an ID. You can enter an ID manually if the provider does not expose a model list.

Choose **中文** or **English** in **UI language / 界面语言**, then press **Save and continue**. The model must support *Chat Completions* with tool/function calling; being listed does not establish that capability. Native Anthropic Messages, native Gemini and Responses-only APIs are not implemented adapters. A provider's OpenAI-compatible endpoint may work.

By default, only the URL, model, language and prompt preferences are saved. The key remains in memory for this run. Saving a key requires the explicit **Save key in a local plaintext file** option. It is **not encrypted** and must not be shared. The default settings path is `~/.config/gpu-data-analysis/connection.json` (owner-only `0600` on POSIX; directory ACL on Windows). Uncheck key saving and save again to remove a previously stored key.

**Skip for now** only skips the current dialog. **Ignore; do not ask again** persists the skip preference without applying incomplete form values. Even after disabling the startup dialog, `/settings`, F5 and `--configure` can reopen it. Without a configured key and model, analysis is unavailable until you configure them.

Local tools read the raw dataset. File paths included in your question and statistical summaries returned by the tools are sent to the selected model provider. Avoid sensitive information in paths or prompts if you do not want that provider to see it.

You can change the UI language in setup, or type `/language en` / `/language zh` or press F6 in the main TUI. The preference is saved. **This translates interface controls and hints, not model-generated answers or raw execution logs.** Ask questions in your preferred language; the TUI itself does not guarantee the answer language.

### 3. Select a file and ask

Enter `/file /absolute/path/to/sales.csv`, or open the file field with `/file` or F3, type a path and press Enter. The path must point to a readable file **on the agent machine**. In an SSH session that means a server-side path; entering a path from your local desktop does not upload a file. A path with spaces can be entered directly after `/file`. Esc closes the file field.

Ask a question and press Enter, for example: `Compare total and average revenue by region; identify the highest and lowest.` The header shows the model, API host and file name. The status reports the actual CPU/GPU engine and elapsed time. Use Ctrl+O, F2 or `/logs` for execution details. Typical supported formats include CSV, TSV, Parquet, JSON/JSONL and Excel; some require optional packages such as `pyarrow` or `openpyxl`, and GPU format support depends on RAPIDS.

### 4. Commands and keys

| Action | Input / key |
| --- | --- |
| Choose a file | `/file PATH`, `/file`, F3 |
| API URL, key, model and language | `/settings`, F5 |
| Change UI language | `/language en`, `/language zh`, F6 |
| Execution details | `/logs`, Ctrl+O, F2 |
| Help | `/help`, F1 |
| Focus input | Ctrl+L, F4 |
| Safe exit | `/quit`, Ctrl+Q, F10 |

Exit during analysis waits for the current model/tool operation and session-memory cleanup. It does **not** cancel in-flight CUDA or network work. Changing the API URL, key or model starts a new backend conversation; earlier answers remain visible but are not sent to the new provider. Connection settings are unavailable while analysis is running.

### 5. Non-interactive use and troubleshooting

For scripts, provide `GPU_API_BASE_URL`, `GPU_API_KEY` and `GPU_API_MODEL` in the environment instead of relying on a dialog. `OPENAI_*` aliases and the legacy `STEPFUN_*` variables are supported. Keep keys out of command-line arguments, Git and shared logs.

```bash
python agent/agent_main.py --ask "Find outliers in /absolute/path/to/sales.csv" --plain
```

`--plain` runs the basic terminal. `--language en` / `--language zh` sets the full-screen TUI language; it does not translate all text in the basic terminal. `GPU_ANALYSIS_CONFIG` overrides the config-file path; `XDG_CONFIG_HOME` changes the default config directory.

If model discovery fails, check the URL, key and network, or enter a model ID manually if `/models` is unavailable. If tool calling fails, choose a compatible model. If a file is missing, check its path **on the agent machine** and its read permissions. A CPU status can be correct for small inputs or when GPU dependencies are unavailable. If startup setup was disabled and no key was saved, reopen `/settings` or supply one through the environment.
