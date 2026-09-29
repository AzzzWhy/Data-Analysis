# GPU 加速与数据分析

![项目图标](assets/app-icon.svg)

一个可以对话、调用分析工具并解释真实结果的数据分析 Agent。模型负责理解问题；Python 分析核心负责全量计算，根据任务与环境使用 CPU、原生 GPU，或 CPU 读取后转 GPU 计算。

本项目面向 NVIDIA DGX Spark 黑客松 Agent Skills 赛道。主分支整合 **Web 工作台、CLI/TUI、对话热工作进程及批量报表核心**，提供同一套可追踪的分析能力。

[快速开始](#快速开始) · [远程计算](#本机前端--远程后端) · [性能证据](#性能证据与测量边界) · [使用说明](docs/USAGE.md) · [English](#english)

## 黑客松项目材料

- [项目说明与技术栈](docs/HACKATHON_SUBMISSION.md)：500 字以上项目说明、架构、优化方案、NVIDIA 技术及实际模型使用清单。
- [本地模型部署与优化说明](docs/LOCAL_MODEL_DEPLOYMENT.md)：本地算力部署、模型服务接入、工具调用验证、已实现优化与未验证事项。
- [Skill 入口](skill.md) 与 [Agent Skill 定义](skills/cudf-analytics/SKILL.md)：触发条件、工作流、参数及结果约束。
- [GB10 测试数据目录清单](docs/gb10-dataset-inventory-20260930/README.md)：实机只读采集的文件规模、Parquet 行数、SHA-256 及历史实验对应；不分发原始大数据。

这些链接是仓库内材料索引，不表示已替参赛者向组委会提交表单。已实现能力、历史实验和部署参考分别标注，不将 NVIDIA 硬件使用等同于使用了 NVIDIA 大模型。

## 能做什么

| 场景 | 实现 |
| --- | --- |
| 对话分析 | 模型选择工具和参数，执行分析后解释工具结果；数据文件不需要整体放进模型上下文 |
| Web 工作台 | 本机 `8877` 入口、访问密码、中英文界面、模型设置、数据文件选择、任务进度、结果与报告展示 |
| 文件交互 | 从数据列表拖入分析区，输入指令后执行；卡片上的 × 只取消选择，不删除源文件 |
| 远程计算 | 在连接窗口配置 SSH 主机、端口及密码/密钥认证，切换到远端分析后端 |
| 分析操作 | 数据画像、描述统计、分组聚合、Pearson 相关性、IQR 异常检测及组合分析 |
| 多轮探索 | 驻留会话复用已读取的数据帧，记录计划进度和计划外步骤 |
| 固定任务 | 同文件多项分析、固定报表、多份计划队列和常驻报表服务；定时触发交给外部调度器 |
| 终端与扩展 | CLI、可选 Textual TUI；发现已安装 Skill 和已配置 MCP，调用明确启用、授权的能力 |

**不承诺所有数据都由 GPU 加速，也不承诺自动路由永远选到最快路径。** 没有可用 GPU，或 CPU 更合适时，可以使用 CPU，并报告实际执行引擎。

## 快速开始

需要 Python 3.10+。以下步骤先提供 CPU 可运行环境；真正的 GPU 分析还需要兼容的 NVIDIA GPU、CUDA 和 RAPIDS cuDF 环境。

### 1. 安装

```bash
git clone https://github.com/AzzzWhy/Data-Analysis.git
cd Data-Analysis
python -m venv .venv
```

激活虚拟环境，按系统选择一条：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

安装 Agent 与工作台依赖：

```bash
python -m pip install -r requirements.txt -r requirements-gui.txt
```

可选依赖：

```bash
# 全屏终端界面
python -m pip install -r requirements-tui.txt

# Parquet / Excel 支持
python -m pip install pyarrow openpyxl
```

`requirements-gui.txt` 为工作台托管 SSH 提供 Paramiko。基础依赖不会安装 cuDF；CPU 安装并不等于已经具备 GPU 环境。已有 RAPIDS 环境时，用对应环境启动计算后端即可，本机前端不需要 GPU。

### 2. 打开工作台

在项目根目录执行：

```bash
python agent/frontend_gateway.py --port 8877
```

打开 **http://127.0.0.1:8877/**。该程序同时启动本机前端网关和内部本机分析后端，不需要再启动第二个本机后端。

1. 首次访问设置工作台访问密码。
2. 在模型连接设置中填写服务地址，读取模型列表并选择模型。
3. 点击左侧文件，或把文件拖入中间分析区。
4. 在下方输入指令并发送，例如“汇总各地区的收入，找出异常值”。
5. 查看实际引擎、进度、分析结果及生成的报告。

文件列表来自**当前计算后端**。拖动只选择文件，不上传电脑文件、不复制远端数据，也不自动开始计算。可用仓库里的 `benchmark/portability/fixture.csv` 进行小规模试用。

如果 8877 已被占用，先确认并正常关闭旧工作台；不要同时在同一端口启动网关与独立后端。工作台仍以 8877 为默认入口，不需要另开旧的 8878 页面。

## 本机前端 + 远程后端

适用于“电脑操作界面、GB10 或其他服务器执行分析”。两个位置使用兼容的项目版本。

**在远端机器上**，激活具备分析依赖的 Python 环境，在项目根目录启动后端：

```bash
python agent/gui.py --host 127.0.0.1 --port 8765
```

**在本机电脑上**，启动前述 8877 网关，打开“计算连接”：

- SSH 地址填 `ssh://用户名@主机:SSH端口`，也可粘贴 `ssh -p 22 user@your-host`。
- 远端后端端口填 `8765`，不要与 SSH 登录端口混淆。
- 选择密码、自动密钥/SSH agent，或指定本机私钥文件。
- 首次连接核对主机指纹；连接就绪后点击“使用远程”。远端要求工作台密码时，另行登录。

SSH 连接不会替你安装依赖或启动远端后端。断线也不会偷偷改成本机计算；工作台会提示故障，由你决定重连或切回本机。

工作台密码、SSH 密码与模型服务密钥是三种不同凭据。密码/私钥口令仅用于本次 SSH 认证，不保存为下次默认值。不要公开后端端口、提交私钥或把真实凭据写进仓库。

完整认证、指纹、已有隧道和故障恢复说明见 [工作台连接指南](docs/WORKBENCH_BACKENDS.md)。

## 模型：云服务或本地部署都可以

Agent 使用 **OpenAI 兼容的 Chat Completions 与工具调用接口**，不绑定单一模型厂商。

| 模型服务 | 设置方式 |
| --- | --- |
| 云端兼容 API | 服务商的基础地址、访问密钥与实际可用模型 |
| 后端机器上的本地服务 | 例如 `http://127.0.0.1:8000/v1`，模型填已部署的 ID |
| 无鉴权的兼容服务 | 密钥栏可用 `local` 占位；如服务开启鉴权，应填写真实密钥 |

远程模式下，模型请求由远端后端发出，因此设置中的 `127.0.0.1` 指**远端机器**，不是浏览器所在电脑。调用本地模型也可以通过 HTTP API，不等于必须购买云端服务。

读取到模型列表不代表该模型能可靠调用工具。当前未直接适配原生 Anthropic Messages、Gemini 原生接口或仅支持 Responses 的接口，也不在浏览器内加载模型权重。

默认可以仅在后端进程内存中使用密钥。选择保存时，密钥会写入当前计算后端的明文配置文件；更换服务地址会清除继承的密钥与模型，避免凭据发往其他服务。

## CLI、TUI 与固定报表

交互式 Agent：

```bash
python agent/agent_main.py
```

安装 Textual 后使用 TUI；没有该依赖时使用基本终端，也可显式添加 `--plain`。常用命令：`/file 路径`、`/settings`、`/logs`、`/language zh`、`/language en`、`/help`。

已配置模型时，可直接提问：

```bash
python agent/agent_main.py --plain --ask "分析 benchmark/portability/fixture.csv 的收入分布"
```

已确定操作时，可绕过模型直接调用分析工具，无需模型密钥：

```bash
python skills/cudf-analytics/scripts/gpu_analytics.py --input benchmark/portability/fixture.csv --op summary --force-cpu
```

固定报表与队列：

```bash
python agent/batch_job.py --plan examples/batch-plan.json --output-root reports
python agent/batch_queue.py --manifest examples/batch-queue.json --output-root reports
python agent/report_service.py --output-root reports/service
```

示例计划引用仓库中的小型 fixture；分析自己的数据时修改计划的文件路径、列名和操作。常驻报表服务使用标准输入/输出 JSON Lines，**不是 HTTP 服务，也不是内置定时任务界面**。

格式与协议见 [统一执行说明](docs/UNIFIED_EXECUTION.md)。Skill/MCP 的配置和权限见 [外部工具说明](docs/EXTERNAL_TOOLS.md)；发现范围不包含任意联网搜索和自动安装。

## CPU/GPU 如何配合

模型选择分析工具；工具层负责校验参数、协调进程并整理结果；分析引擎执行全量统计，再将实际结果交给模型解释。

| 执行方式 | 适用情况与限制 |
| --- | --- |
| CPU / pandas | 小数据、GPU 不可用或算子不支持时的路径 |
| GPU 原生读取与计算 | 支持的格式和足够的计算收益，减少不必要的主机侧转换 |
| CPU 读取 → GPU 计算 | 读取成本与计算收益满足条件的混合路径；当前限于符合约束的数值型 Parquet 投影 |
| 热工作进程 / 驻留会话 | 对话探索，减少重复启动，按条件复用数据帧 |
| 批量 / 报表事务 | 已规划任务合并列读取、复用精确统计，并协调 CPU 并发与 GPU 独占计算 |

支持 CSV/TSV、Parquet、JSON/JSONL、Excel，但各格式读取方式不同。JSON/JSONL 与 Excel 使用 CPU 格式桥接；不能将“支持这个文件格式”理解成该格式一定原生 GPU 加载。

自动选择结合保守规则与可选校准。校准要匹配文件、列、操作、机器、实现和冷/热场景，过期或不匹配时回退；正常请求不会隐藏地把三条路线都跑一遍。这不是训练大模型后获得的全局最优调度器。

热工作进程、数据缓存、批量执行和多进程协调是不同层次的优化，不代表 CPU/GPU 对同一数据自动进行无条件并行流水线。实际规模仍受内存准入、格式与算子支持限制。

## 性能证据与测量边界

以下为仓库保留的 **GB10 历史实验**，不是此次 GUI 合并后重新测出的性能，也不是任意数据上的速度保证。

| 相同任务的对照 | 行数 | CPU | GPU | CPU / GPU |
| --- | ---: | ---: | ---: | ---: |
| 三步分析，热工作进程 | 113,500,327 | 3.61950 s | 0.81942 s | 4.42× |
| 三步分析，冷工作进程（含启动/退出） | 113,500,327 | 3.94759 s | 2.45663 s | 1.61× |
| SUSY 四项分析，热工作进程 | 5,000,000 | 3.9507 s | 1.3071 s | 3.02× |

前两行见 [核心验收记录](docs/FINAL_CORE_DEPLOYMENT.md)，第三行见 [公开数据测试](docs/new-public-data-20260929/README.md)。各记录包含测量配置、口径及数值检查；不可跨行混用冷态 CPU 与热态 GPU 来计算加速比。

- 上表不包含模型规划、网络和最终回答的耗时。
- 小数据可能 CPU 更快，自动模式选 CPU 是合理结果。
- 引擎显示 `cudf` 不代表已经测过加速倍数；没有匹配 CPU 基线时显示未测量。
- 消除重复读取的收益不能全部归为 GPU 算子提速。

更多证据：[七档规模对照](docs/ALL_SCALES_RESULTS.md) · [公平驻留基准](benchmark/resident/README.md) · [独立客户端验证](benchmark/portability/README.md)。

## 验证、状态与边界

合并验收及当前已知限制见 [主分支整合记录](docs/MAIN_INTEGRATION_2026-09-29.md)；界面历次测试见 [GUI 验收记录](docs/GUI_ACCEPTANCE_2026-09-29.md)。

本地可运行的示例检查（不调用真实模型）：

```bash
node agent/gui_dataset_drag_test.js
node agent/login_page_test.js
node agent/gui_job_recovery_test.js
python agent/fast_execution_test.py
python agent/gui_concurrency_test.py
python agent/frontend_gateway_test.py
python skills/cudf-analytics/scripts/session_worker_test.py --fixture-rows 2000
```

Node 用于前端测试，不是运行工作台的必需依赖。CPU 回归、合成 SSH/HTTP 测试、真实 GPU 实验和浏览器视觉验收是不同证据，不能互相替代。

当前仍有明确边界：

- 不保证未知数据上最优路由、任意模型可靠调用工具或所有 GPU/操作系统组合可运行。
- GUI 的即时分析、驻留会话、Agent 对话和固定报表入口能力不同，不能只凭控件名称推断核心已强制使用 GPU。
- 登录后的 Chrome 全流程、跨机器安装和长期断网恢复不能仅凭 Node 测试认定完成。
- 勾选保存模型密钥会写入明文配置；请保护配置文件权限，不公开服务端口。
- 本轮收尾不是继续添加功能，也不代表性能已经达到硬件极限。

## 代码与文档导航

| 路径 | 内容 |
| --- | --- |
| `agent/agent_main.py`、`agent/skills.py` | Agent 工具循环与执行入口 |
| `agent/gui.py`、`agent/frontend_gateway.py`、`agent/gui/` | 后端、单一前端网关及工作台页面 |
| `agent/batch_job.py`、`agent/batch_queue.py`、`agent/report_service.py` | 固定报表、队列和常驻服务 |
| `skills/cudf-analytics/` | 分析 Skill、CPU/GPU 引擎、会话与校准 |
| `benchmark/`、`docs/` | 复现实验、使用说明和验收证据 |

[完整使用说明](docs/USAGE.md) · [工作台连接指南](docs/WORKBENCH_BACKENDS.md) · [GUI 核心接口](docs/GUI_CORE_CONTRACT.md) · [优化说明](docs/OPTIMIZATION.md)

## English

GPU-accelerated data analysis with an agent, a web workbench, CLI/TUI, reusable workers and batch reports. The model selects tools; local Python code computes the actual results using CPU, native GPU or an eligible CPU-to-GPU loading path.

Install `requirements.txt` and `requirements-gui.txt`, then run `python agent/frontend_gateway.py --port 8877`. Open http://127.0.0.1:8877/, set a workbench password and configure an OpenAI-compatible tool-calling model. A local model server is supported; a paid cloud API is not required.

For remote compute, run `python agent/gui.py --host 127.0.0.1 --port 8765` in the remote analysis environment, then configure SSH in the local workbench. Data stays on the selected backend. Dragging a dataset stages a selection; removing its card never deletes the source file.

GPU dependencies are separate from the CPU installation. Published speedups are workload-specific historical measurements, not universal guarantees. See the [English user guide](docs/USAGE.md#english) and [integration status](docs/MAIN_INTEGRATION_2026-09-29.md).
