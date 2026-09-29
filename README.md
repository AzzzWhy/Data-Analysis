# GPU 加速与数据分析

![GPU 加速与数据分析图标](assets/app-icon.svg)

一个面向本地表格数据的分析 Agent。模型理解问题、选择工具并解释结果；本机 Python 工具执行全量统计，根据任务和环境使用 pandas/CPU、RAPIDS cuDF/GPU，或 CPU 读取后转 GPU 计算。

支持自然语言对话、已规划的批量分析和固定报表。项目用于第三届 NVIDIA DGX Spark 黑客松 Agent Skills 赛道，当前 `main` 包含统一后的对话与批量执行核心。

A local data-analysis agent with CPU/GPU routing, reusable analysis workers and batch reports. The model chooses tools; local Python code computes statistics over all relevant rows. See the [English user guide](docs/USAGE.md#english).

[使用说明](docs/USAGE.md) · [统一执行入口](docs/UNIFIED_EXECUTION.md) · [最新核心验收](docs/FINAL_CORE_DEPLOYMENT.md) · [公开数据测试](docs/new-public-data-20260929/README.md) · [GUI 对接约定](docs/GUI_CORE_CONTRACT.md)

## 能做什么

| 能力 | 当前实现 |
| --- | --- |
| 基础统计 | 数据画像、描述统计、单列分组聚合、Pearson 相关性、IQR 异常值检测，以及组合分析 `auto` |
| 自然语言分析 | 支持工具调用的模型选择操作与参数，读取工具结果后生成回答 |
| 多步探索 | `dataset_session` 在会话内复用数据帧，记录计划进度与计划外步骤 |
| 已知任务批量执行 | `analyze_batch` 接收同一文件的 1～8 项分析，读取所需列的并集，并共享可复用的精确统计 |
| 固定报表 | 单份计划、多份计划队列和常驻报表服务；定时触发由外部调度器负责 |
| 本地导出 | 按所选入口生成 Markdown 报告、JSON 结果、CSV 表格或 SVG 图表；另有 HTML 报告构建脚本 |
| 终端交互 | CLI 和可选 Textual TUI，支持连接设置、文件选择、日志及中英文界面 |
| 外部扩展 | 发现已安装的 Skill 和已配置的 MCP 服务，调用明确启用和授权的能力 |

支持 CSV/TSV、Parquet、JSON/JSONL 和 Excel 输入，但各格式的加载路径不同。CSV/Parquet 可使用原生 GPU 读取；JSON/JSONL、Excel 通过 pandas 读取。Parquet 通常需要 `pyarrow`，`.xlsx` 需要 `openpyxl`；旧 `.xls` 文件需要 pandas 对应的读取依赖。当前混合加载路径仅支持满足约束的数值型 Parquet 投影，不支持任意文本或时间列。

“全量”指统计操作覆盖相关列的全部行，不表示把所有行、分组或异常样例返回给模型。Top-K 和预览会限制展示范围；从部分排名不能推断未展示分组的数值或全局最小值。

## 快速开始

### 安装与交互式运行

在执行分析的机器上准备 Python 3.10+：

```bash
git clone https://github.com/AzzzWhy/Data-Analysis.git
cd Data-Analysis
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-tui.txt
python agent/agent_main.py
```

以上虚拟环境激活命令适用于 Linux/macOS；Windows PowerShell 使用 `.venv\Scripts\Activate.ps1`。已有 RAPIDS 环境时应使用该环境的 Python，无需另外创建上述 CPU 虚拟环境。

`requirements.txt` 提供基础依赖和 CPU 路径，**不会安装 cuDF**。GPU 执行需要另行配置与机器系统、CUDA 和 Python 匹配的 RAPIDS 环境。本仓库的 GPU 实验主要在 NVIDIA GB10 上完成；其他兼容硬件需要自行验证。没有可用 GPU/cuDF，或任务更适合 CPU 时，工具会报告实际的 pandas 执行。

首次交互启动会打开连接设置。填写 API 基础地址、密钥和模型；模型需要支持 **OpenAI 兼容的 Chat Completions 工具调用**。模型列表里有该 ID，不代表它一定能正确调用工具。当前未实现原生 Anthropic Messages、Gemini 原生接口或仅支持 Responses 的接口适配。

进入 TUI 后选择本机文件并提问，例如：

```text
/file /absolute/path/to/sales.csv
按 region 统计 revenue 的总额和均值，展示前 5 组。
分析 revenue 的分布和异常值，并说明实际使用的计算引擎。
```

这里的文件必须位于**运行 Agent 的机器**上。通过 SSH 使用时填写服务器路径；选择路径不会上传本地电脑上的文件。

| 操作 | TUI 命令 / 按键 |
| --- | --- |
| 选择文件 | `/file 路径`、`/file`、F3 |
| 配置模型连接 | `/settings`、F5 |
| 查看执行日志 | `/logs`、Ctrl+O、F2 |
| 切换界面语言 | `/language zh`、`/language en`、F6 |
| 查看帮助 | `/help`、F1 |
| 退出 | `/quit`、Ctrl+Q、F10 |

未安装 Textual 时使用基本终端；也可加 `--plain`。语言选项翻译界面控件，不自动翻译模型答案或原始日志。分析中退出会等待当前操作结束和资源清理，不会立即取消 CUDA 或模型请求。

### 脚本运行与直接分析

无界面运行前，通过环境变量提供 `GPU_API_BASE_URL`、`GPU_API_KEY`、`GPU_API_MODEL`，或使用已保存的完整配置。也兼容 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` 和原有 `STEPFUN_*` 设置。

```bash
python agent/agent_main.py --ask "分析 /absolute/path/to/sales.csv 的异常值" --plain
```

`--ask` 和管道输入不会弹出连接设置。`--configure` 可在交互式终端重新设置连接，`--base-url` 和 `--model` 可覆盖本次运行的地址和模型。

如果分析操作已经确定，可以直接调用引擎，**无需模型或 API 密钥**：

```bash
python skills/cudf-analytics/scripts/gpu_analytics.py \
  --input /absolute/path/to/sales.csv --op auto

python skills/cudf-analytics/scripts/gpu_analytics.py \
  --input /absolute/path/to/sales.csv --op groupby \
  --by region --agg 'revenue:sum,mean' --force-cpu
```

### 固定报表与常驻服务

以下命令从仓库根目录运行；示例计划使用仓库中的小型测试数据。分析自己的文件时，复制计划并修改 `file_path`、列名与操作。

```bash
python agent/batch_job.py --plan examples/batch-plan.json --output-root reports
python agent/batch_queue.py --manifest examples/batch-queue.json --output-root reports
python agent/report_service.py --output-root reports/service
```

常驻服务使用标准输入/输出的 JSON Lines 协议，可接收 `run`、`status`、`close` 请求；它不是 HTTP 服务。服务复用工作进程，但每次请求重新读取数据，不跨请求保留输入数据帧。队列允许有界 CPU 并发，可能使用 GPU 的任务走保守的独占调度。定时任务需要由操作系统或调用方触发。

具体计划格式、服务协议和资源生命周期见[统一执行说明](docs/UNIFIED_EXECUTION.md)；GUI 集成见[核心接口约定](docs/GUI_CORE_CONTRACT.md)。

## 执行方式

```text
用户问题
   ↓
模型选择工具与参数
   ↓
agent/skills.py：参数处理、工作进程通信、结果整理
   ↓
本地分析核心：CPU / 原生 GPU / CPU 读取后转 GPU
   ↓
统计结果 + 实际引擎 + 耗时 + 路由或回退原因
   ↓
模型解释结果；需要时调用导出工具
```

模型不需要把完整文件放进上下文。可处理的数据规模仍受本机内存、GPU 可用内存、格式及算子支持限制。

当前注册六个内置工具：

| 工具 | 用途 |
| --- | --- |
| `list_datasets` | 查找可用数据文件 |
| `analyze_dataset` | 单次分析；交互模式可尝试常驻数据复用 |
| `analyze_batch` | 执行同一文件上已确定的多项分析 |
| `dataset_session` | 打开、分析、查看和关闭驻留会话 |
| `export_deliverables` | 导出本地报告、图表和表格 |
| `load_csv_dataset` | 保留的 CSV 加载兼容入口 |

Agent 另注册四个外部工具入口：发现工具、读取 Skill、执行授权 Skill 命令及调用授权 MCP 工具。发现范围是已安装目录和已配置服务，不包含自动联网搜索或安装。[配置与权限说明](docs/EXTERNAL_TOOLS.md)

对话工具循环默认最多 10 轮，随后尝试生成最终回答。常见参数错误、文件错误和执行失败以结构化结果返回，供模型解释或重试；这不保证任意模型都能自动纠正错误。`Agent.run()` 在 `finally` 中关闭遗留活动会话；允许保留的有界缓存与活动会话分别管理。

### 计划、复用与内存

- 自适应探索使用四类固定计划：`drill_down`、`compare_groups`、`data_quality`、`relationships`。分类器包含**中文和英文关键词**；未匹配的目标使用默认计划，不是任意自然语言规划器。
- 相同目标的计划分类可重复，但模型的实际执行路径仍可能不同。系统记录已完成步骤及 `off_plan` 步骤，不强制模型完成全部计划，也不能由相关性证明因果关系。
- 已知列与操作时，批量分析可以减少重复读取，并复用同一事务内的精确统计。交互会话则适合后续操作依赖前一步结果的探索。
- 活动会话默认上限为 4，并有内存准入检查。可保留的 GPU 帧缓存默认上限为 4096 MB，空闲有效期 900 秒；`SESSION_WARM_CACHE_MB=0` 或 `SESSION_WARM_TTL_SECONDS=0` 可关闭它。
- 文件身份检查用于拒绝已变化文件的旧会话或缓存。工作进程退出会释放其数据；报表服务的进程复用不等于跨请求的数据缓存。

### CPU/GPU 自动选择

小数据不一定受益于 GPU：初始化、读取和转换开销可能超过计算收益。引擎使用保守的规模规则和可选校准记录，并返回实际选择及回退原因。

批量与混合路径的校准需要匹配文件身份、列投影、完整操作参数、实现与机器指纹以及执行上下文，有效期为 24 小时。冷启动、热工作进程和报表事务使用各自的测量，不能借用另一场景的时间。无匹配校准时回到默认策略，**不保证未知数据上总能选到最快路径**，正常请求也不会自动把三条路径全部重跑。

性能收益需要分开理解：GPU 算子计算、读取方式、工作进程复用、减少重复读盘及统计结果复用都会影响总时间。额外 CPU 对照默认关闭；显式开启 `SKILL_SHOW_SPEEDUP=1` 会增加测量开销。没有可比的实测基线时，应显示“未测量”，不能由引擎名称推算加速倍数。

详细实现见[本地优化](docs/OPTIMIZATION.md)、[快速执行](docs/FAST_EXECUTION.md)和[统一执行说明](docs/UNIFIED_EXECUTION.md)。

## 性能与验证证据

以下是仓库保存的实验记录，不是任意机器或数据上的性能承诺。GPU 测试主要使用 NVIDIA GB10、cuDF 25.10、pandas 2.3.3、Python 3.11。CPU 基线为相应实验的 pandas 实现，未证明优于所有经过优化的 CPU 分析引擎。

### 公开数据：同模式 CPU/GPU 对照

2026-09-29 的测试在热工作进程中执行四项单批次分析，共享一次读取；每条路径预热后交错运行三次，取中位数。时间包含请求、读取、转换、计算与返回结果，不含模型调用或工作进程首次启动。

| 工作负载 | 行数 | CPU | 原生 GPU | CPU / GPU | 匹配校准后的自动选择 |
| --- | ---: | ---: | ---: | ---: | --- |
| Gas Sensor，128 个特征 | 13,910 | 0.0714 s | 0.4007 s | 0.18× | CPU |
| Online Retail II，国家分组 | 1,067,371 | 0.1477 s | 0.0924 s | 1.60× | 原生 GPU |
| SUSY，18 个特征 | 5,000,000 | 3.9507 s | 1.3071 s | 3.02× | 原生 GPU |

比值小于 1 表示 GPU 更慢。自动选择列使用本轮匹配校准；未校准的零售分组任务曾选择 CPU。SUSY 来源本身是蒙特卡洛模拟数据，不是实测粒子事件。完整八组实验、数值一致性检查、数据来源及限制见[公开数据测试记录](docs/new-public-data-20260929/README.md)。

### 其他可复核记录

| 记录 | 测量内容与边界 |
| --- | --- |
| [公平驻留基准](benchmark/resident/README.md) | 两个引擎均只加载一次，五次重复。20M 行 / 3.04 GB CSV 的工作流比值为 2.92×，含启动为 2.46×；1M 行工作流 CPU 更快。计算阶段波动另行标注。 |
| [最新核心验收](docs/FINAL_CORE_DEPLOYMENT.md) | 覆盖 20,000 行和 113,500,327 行、三步分析与四份报表，并有真实本地模型调用。大数据热三步分析 CPU/GPU 为 4.42×，冷工作进程含启动为 1.61×；不包含模型耗时。性能计时早于最后的输出压缩保真修复，文档明确区分。 |
| [真实用电数据演示](benchmark/real_power_demo/README.md) | UCI 家庭用电数据 2,075,259 行，包含来源、缺失值处理和独立数值核验；该工作负载选择 CPU。 |
| [独立客户端验证](benchmark/portability/README.md) | 独立 Codex CLI 发现已安装 Skill、执行工具并导出核验过的结果，不依赖本仓库 Agent 循环；不代表已验证 Claude 执行。 |
| [七档规模实验](docs/ALL_SCALES_RESULTS.md) | 不同规模、冷／热状态及执行方式的历史对照，应按各自测量口径阅读。 |

历史演示中将“逐步重读文件的 CPU”与“驻留 GPU”比较得到的较大倍数，包含消除重复读取的收益，不能作为纯 GPU 加速比。完整模型对话还包含规划、网络和生成回答的时间，不能等同于工具计算时间。

### 本地验证

安装基础依赖后，可从仓库根目录运行以下检查，无需模型 API：

```bash
python agent/tool_contract_test.py
python agent/api_config_test.py
python skills/cudf-analytics/scripts/plan_test.py
python skills/cudf-analytics/scripts/smoke_test.py
python agent/batch_result_compaction_test.py
```

在已配置的 RAPIDS 环境中验证 GPU 路径：

```bash
python skills/cudf-analytics/scripts/smoke_test.py --require-gpu
```

可选 TUI 测试为 `python agent/tui_test.py`，需要 `requirements-tui.txt`。真实模型演示、验收及较大数据基准需要相应模型配置、数据和硬件，不包含在上述离线检查中。更多验收范围见[核心验收记录](docs/FINAL_CORE_DEPLOYMENT.md)与[早期验证记录](benchmark/VERIFICATION.md)，以各次输出为准，不将选定测试通过描述成全仓库测试通过。

## 数据与配置边界

内置分析工具在本机读取数据，不上传完整数据文件。**用户问题、文件路径、统计摘要，以及工具返回的有限预览和异常样例会进入所选模型上下文。** 使用远程 API 时，这些内容会发送给该服务；授权的外部工具也会收到相应调用参数。

连接配置默认位于 `~/.config/gpu-data-analysis/connection.json`，支持 `XDG_CONFIG_HOME` 和 `GPU_ANALYSIS_CONFIG`。默认只保存地址、模型和偏好，密钥保留在内存；只有主动选择保存密钥才写入本机明文配置。POSIX 文件使用 `0600` 权限，Windows 继承配置目录 ACL。具体设置与切换行为见[使用说明](docs/USAGE.md)。

外部 Skill/MCP 配置独立于模型配置。明确启用的本地命令以 Agent 用户权限执行，外部工具机制本身不是操作系统沙箱。[外部工具说明](docs/EXTERNAL_TOOLS.md)

## 独立使用 Skill

可将分析 Skill 安装到另一个项目：

```bash
python tools/install_skill.py --project /absolute/path/to/project
```

默认复制到目标项目的 `.agents/skills/cudf-analytics`，已有目录会保留并报错，不覆盖。`--client claude` 改用 `.claude/skills` 目录布局；它不等于已经验证该客户端的执行行为。安装器不复制 Agent 代码或 API 凭据，也不安装 Python/GPU 依赖。

Skill 包包含 [SKILL.md](skills/cudf-analytics/SKILL.md)、[能力与风险说明](skills/cudf-analytics/skill-card.md)、[本地评估任务](skills/cudf-analytics/evals/evals.json)和[评估报告](skills/cudf-analytics/BENCHMARK.md)。这是第三方 Skill，没有 NVIDIA 官方签名；本地评估器也不代表官方认证。

## 仓库结构

```text
agent/
  agent_main.py             模型对话与工具调用循环
  skills.py                 工具定义、工作进程通信与结果整理
  api_config.py             模型连接配置
  api_setup.py              连接设置界面
  tui_app.py                Textual 终端界面
  batch_job.py              单份固定分析计划
  batch_queue.py            多份计划队列
  report_service.py         JSON Lines 常驻报表服务
  external_tools.py         外部 Skill / MCP 发现与授权调用
skills/cudf-analytics/
  SKILL.md                  独立 Skill 使用说明
  scripts/
    gpu_analytics.py        CPU/GPU 统计引擎
    gpu_session.py          常驻会话、批量执行和数据复用
    hybrid_execution.py     混合加载与匹配校准选路
    analysis_plan.py        中英文关键词计划与进度记录
    make_deliverables.py    SVG、Markdown 和表格导出
    build_html_report.py    HTML 报告构建
  evals/                    本地评估任务与执行器
  references/               引擎与会话接口约定
docs/                       使用、部署、接口和实验记录
benchmark/                  基准脚本、结果与演示材料
examples/                   批量计划和外部工具示例
tools/                      Skill 安装与可移植性验证
scripts/run_gb10_core.sh    已配置 GB10 环境的启动入口
```

## 当前限制

- 分析能力以既有算子和计划为边界，不提供任意 SQL、多表连接或任意 Python 分析代码生成执行。当前分组接口使用单个分组列。
- 模型可能选错工具、遗漏步骤或误读统计结果；计划记录与工具错误处理不保证回答正确或完整。
- 自动路由依赖规则和匹配校准，尚不保证未知数据的最优执行路径。数值型 Parquet 混合加载不适用于所有数据类型。
- 当前主要界面是 CLI/TUI；本仓库提供 GUI 对接约定，没有内置交互式分析仪表盘。`Agent.run()` 返回最终文字，现有接口不是逐 token 流式输出。
- 导出文件保存在本机；内置报表不提供托管分享或多数据集连接分析。图表类型有限，图表渲染不属于 GPU 统计加速。
- 驻留与批量执行仍受可用内存限制，不是任意大数据的分块流式引擎，也不是 CPU/GPU 异步流水线。

## 许可

项目采用 [MIT License](LICENSE)。RAPIDS cuDF、pandas、NumPy、openai-python、Textual 等依赖遵循各自许可，不随仓库捆绑。模型服务可配置，历史 StepFun 或本地模型实验不构成对某个模型的固定依赖。公开数据的来源与许可见对应实验记录。
