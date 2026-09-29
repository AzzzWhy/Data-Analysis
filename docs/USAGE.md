# GPU加速与数据分析 · 使用说明 / User Guide

> [English version](#english)

## 中文

这个 Agent 接收自然语言分析问题，在**运行 Agent 的机器上**调用本地分析工具，并根据实际任务与环境使用 cuDF/GPU 或 pandas/CPU。模型负责选择操作和解释工具结果；不要把某次 CPU/GPU 路由当成整台机器的固定模式。

固定报表、定时任务及已规划多项分析的批量入口见[统一执行说明](UNIFIED_EXECUTION.md)。

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

#### 浏览器工作台

如果要在同一个大文件上连续追问，可以用浏览器工作台代替终端：

```bash
python agent/gui.py --port 8765
# 在自己的电脑上通过 SSH 隧道访问节点：
ssh -p <节点SSH端口> -L 8765:127.0.0.1:8765 Developer@<跳板机地址>
```

`python agent/agent_main.py --gui` 启动同一个服务，走 Agent 自己的入口。它在"请先配置"那道检查之前就被选中，所以没有密钥时界面照样起来，并明确告出哪一半不可用；`--gui` 拒绝与 `--ask` 或管道输入同时使用，因为那是脚本化运行，一个没人要求停的服务器只会变成挂住的进程。

顶部的 **设置** 按钮通过终端同一个 `api_config` 加载器修改 API 地址、模型与密钥。密钥只接受一次、永不回显：响应只告诉你有没有存过；换地址会清掉上一个服务商的密钥与模型；密钥出现在查询串里会被直接拒绝。

服务只绑 `127.0.0.1`。不设 `GPU_GUI_ALLOW_REMOTE=1` 时它拒绝绑定其他网卡，因为这个界面没有鉴权，而节点上 8888/9000 是公网可达端口——请用上面的隧道方式。

界面右下角的引擎标记只报告事实：**有意选择 CPU** 显示为 `CPU · pandas (by choice)` 并附上引擎自己的交叉点判据；**GPU 尝试后失败** 才显示为橙色的 `(fallback)`。两者不会互相冒充。引擎没有给出的数值一律留白，不填估算值。

页面里的「直接工具调用」不经过模型，按下去就是一次真实的 `skills` 调用。它存在的原因是：没有它，在没有 API 密钥的机器上就无法在浏览器里验证图表、会话卡和引擎标记；它不是提问框的替代品，也不会显示任何预置答案。没有安装 `openai` 或未配置密钥时，提问框会禁用并显示真实原因（原始 `ImportError` 或"未配置密钥"），其余面板照常可用。

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
- **没给路径时找不到数据文件**：`list_datasets` 只列举受控范围——当前目录及其下两层、上一级目录，以及设置了 `DEMO_DATA_DIR` 时的该目录。主目录只列**直接放在里面的文件**，不会进入它的子目录，所以下载目录里的无关 CSV 不会当成候选数据端给你。放在主目录顶层的数据集仍然能被发现；不在上述范围时请直接给绝对路径。
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

#### Browser workbench

If you intend to ask several questions about one large file, the workbench replaces the terminal for that job:

```bash
python agent/gui.py --port 8765
# from your own machine, through an SSH tunnel to the node:
ssh -p <node-ssh-port> -L 8765:127.0.0.1:8765 Developer@<jump-host>
```

`python agent/agent_main.py --gui` starts the same server from the agent's own entry point. It is chosen before the "configure me first" check, so the workbench comes up with no key and states which half is unavailable; it refuses to combine with `--ask` or piped input, because those are scripted runs and a server nobody asked to stop is a hung process.

The **设置 / Settings** button edits the API address, model and key through the same `api_config` loader the terminal uses. A key is accepted once and never returned: the response reports only whether one is stored, changing the endpoint drops the previous provider's credential, and a key in the query string is refused outright.

The server binds `127.0.0.1` and refuses any other interface unless `GPU_GUI_ALLOW_REMOTE=1` is set, because it has no authentication and the node's 8888/9000 ports are reachable from the public internet. Use the tunnel above.

The engine chip reports facts only. A deliberate CPU route reads `CPU · pandas (by choice)` and carries the engine's own crossover reason; a GPU that was asked for and failed reads `(fallback)` in a warning colour. The two never impersonate each other, and where the engine produced no value the row stays empty rather than showing an estimate.

The **direct tool call** panel invokes the real `skills` functions without a model, and says so in the log drawer. It exists because without it the charts, the session card and the engine chip could not be checked in a browser on a machine that has no API key. It is not a substitute for asking a question, and it never renders a prewritten answer. With `openai` not installed, or no key configured, the question box is disabled and shows the real reason (the original `ImportError`, or "no key configured"); every other panel keeps working.


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

When a question names no file, the agent looks for candidates with `list_datasets`, which lists the working directory down two levels, its parent one level, and `DEMO_DATA_DIR` when set. The home directory is listed at its top level only -- a dataset sitting directly in it is still found, but `list_datasets` never descends into it, so an unrelated download is not offered as your data. If the file you mean is outside that range, give its absolute path.
