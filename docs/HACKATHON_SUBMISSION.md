# DGX Spark 黑客松：GPU 加速与数据分析

本页集中提供仓库内参赛材料。源码入口为 [main](https://github.com/AzzzWhy/Data-Analysis/tree/main)，许可见 [MIT LICENSE](../LICENSE)。视频、媒体文章和团队资料不属于本页的完成声明。

## 项目说明

“GPU 加速与数据分析”是一个面向表格数据探索和固定分析任务的 Agent。我希望用户不必先学习一套分析脚本的参数，而是选择数据，直接提出问题，再得到有计算依据的答案。项目既提供自然语言对话，也保留可以绕过模型直接执行的工具入口，使探索式分析和确定性报表能够共用同一套计算核心。

项目将语言理解和数值计算分开。模型负责理解意图、选择工具、组织分析步骤并解释结果；Python 工具负责校验路径与参数，实际读取文件，对全量数据执行描述统计、分组聚合、Pearson 相关性及 IQR 异常检测。模型收到的是工具返回的结构化结果，而不是只看文件前几行就猜测整份数据的统计结论。执行结果同时记录实际引擎、扫描行数、耗时和回退原因，避免把“机器装有 GPU”当成“这次分析一定使用了 GPU”。

在计算架构上，我没有要求所有任务都走 GPU。小数据可能更适合 pandas/CPU，大规模计算可以使用 RAPIDS cuDF；符合条件的数值 Parquet 投影还可以采用 CPU 读取、Arrow 转换后由 GPU 计算。选择依据包括操作支持、内存准入和可选的匹配校准。校准与文件、列、任务、运行环境和冷热场景绑定，失效后保守回退，不保证未知数据上的全局最优，也不会在普通请求中隐式执行三份完整基准。

为了减少端到端开销，项目同时保留热工作进程和批量执行。对话中连续追问同一份数据时，可以复用分析进程和有效的驻留数据帧，减少重复初始化与读取。固定报表则将已知的多项操作放进批次，合并所需列、复用精确统计，并在结束时释放会话。多计划队列提供有界 CPU 并发；参与协调的 GPU 分析进程在计算阶段使用跨进程许可，避免彼此无节制争抢。该许可不管理外部模型服务，也不是统一内存的全局预留器。

项目的交互入口包括 Web 工作台、CLI 和可选 TUI。工作台以本机 8877 为入口，支持通过 SSH 连接远端后端，让浏览器所在电脑负责界面、GB10 负责数据与计算。文件拖入分析区只是选中，用户输入指令后才开始任务；移除卡片不删除源文件。模型连接支持兼容的云端服务或本地部署服务，工作台密码、SSH 认证和模型访问密钥各自独立。

优化成果通过同任务、同数据、核对结果的 CPU/GPU 对照来说明。在保留的 GB10 历史实验中，约 1.135 亿行数据的三步热分析，CPU 为 3.61950 秒，原生 GPU 为 0.81942 秒，约 4.42 倍；计入冷工作进程启动与退出时，同任务为 3.94759 秒和 2.45663 秒，约 1.61 倍。这些数字不包含模型规划与回答，不代表任意数据或本次文档更新后的重新测量。项目的核心价值，是把自然语言工具调用、可追踪计算和有边界的 CPU/GPU 执行选择组合成可操作的分析工作台，而不是只展示一个最大的加速倍数。

## 架构与代码位置

| 层次 | 职责 | 源码 |
| --- | --- | --- |
| 交互与连接 | Web、CLI/TUI、SSH 后端切换 | `agent/gui/`、`agent/frontend_gateway.py`、`agent/managed_ssh.py` |
| Agent | Chat Completions 工具循环、任务结果状态 | `agent/agent_main.py`、`agent/execution_outcome.py` |
| 工具与 Skill | 参数契约、工作进程通信、结果压缩 | `agent/skills.py`、`skills/cudf-analytics/SKILL.md` |
| 计算 | CPU/GPU 算子、会话、混合加载、校准 | `skills/cudf-analytics/scripts/` |
| 固定任务 | 报表、计划队列、常驻服务 | `agent/batch_job.py`、`agent/batch_queue.py`、`agent/report_service.py` |

文件仍留在所选计算后端，但发送给模型的提示词、路径和统计结果仍可能包含业务信息；使用云端模型不能宣传为全部数据零出机。

## NVIDIA 技术栈与实际使用模型

版本为仓库历史实验记录，不是所有安装的固定要求，也不是本次重新检测的运行环境。

| 技术 / 模型 | 本项目中的用途和状态 | 证据 |
| --- | --- | --- |
| NVIDIA DGX Spark / GB10 | 历史 GPU 验收硬件，ARM64、统一内存 | [核心验收](FINAL_CORE_DEPLOYMENT.md) |
| NVIDIA CUDA | GPU 执行基础；记录中使用 CUDA 13.0 头文件配置 | [环境修复记录](PUBLIC_DATA_REPAIR.md) |
| NVIDIA RAPIDS cuDF | GPU DataFrame、统计与聚合；记录版本 25.10.0 | [环境修复记录](PUBLIC_DATA_REPAIR.md) |
| CuPy | CUDA 数组/同步等辅助能力；记录版本 14.2.0。它是独立开源库，不标作 NVIDIA 自有 SDK | [环境修复记录](PUBLIC_DATA_REPAIR.md) |
| pandas / NumPy / PyArrow | CPU 基线、数值处理、Parquet 投影及 Arrow 转换 | [混合执行证据](HYBRID_RESULTS.md) |
| StepFun `step-3.7-flash` | 历史云端 Agent 工具调用与界面联调使用，不是本地权重部署 | [历史 GUI 验证](superpowers/specs/2026-09-27-workbench-gui-design.md) |
| 本地服务 ID `qwen3.6-35b-a3b-fp8` | 历史 GB10 本地模型与 Agent 实际调用。服务 ID 不等于已核验的下载仓库 ID | [核心调用证据](FINAL_CORE_DEPLOYMENT.md)、[GUI 验收](GUI_ACCEPTANCE_2026-09-29.md) |
| NVIDIA 大模型 | 当前仓库没有可核验的 Nemotron 等 NVIDIA 模型调用证据，不列为已使用 | 不适用 |
| TensorRT-LLM / NeMo / NVIDIA Model Optimizer | 未作为本项目已实施的推理优化、训练或量化成果申报 | 不适用 |

OpenAI Python SDK 在这里是协议客户端，不表示测试必然使用了 OpenAI 模型。现有模型可被其他兼容且能正确调用工具的模型替换，但兼容性需要实际测试。

## Agent Skills 设计

正式定义见 [SKILL.md](../skills/cudf-analytics/SKILL.md)。定义包含名称与描述、触发场景、非适用场景、命令参数、工作流和 JSON 结果约束。Agent 应在需要计算文件统计事实时调用工具，而不是将采样内容当作全量结论。

工具层支持单次分析、驻留会话及批次执行。会话检查文件身份，文件改变后拒绝继续用旧帧回答；切换活动会话的引擎/加载路径需要先关闭会话。工具失败与模型回答成功分开记录，不能用流畅的回答掩盖执行失败。外部扩展只发现已安装或已配置的 Skill/MCP，并受明确启用和权限约束，不等同于自动联网安装任意工具。见 [外部工具说明](EXTERNAL_TOOLS.md)。

## 部署、优化与验收入口

- [本地模型部署与优化说明](LOCAL_MODEL_DEPLOYMENT.md)：区分已验证的服务接入与新机器安装参考。
- [README](../README.md)：安装与工作台入口；[远程连接](WORKBENCH_BACKENDS.md)：SSH 认证与切换。
- [优化实现](OPTIMIZATION.md)、[统一执行](UNIFIED_EXECUTION.md)：算法、数据复用与队列边界。
- [核心验收](FINAL_CORE_DEPLOYMENT.md)：GPU 历史原始证据与冷热口径。
- [main 整合回归](MAIN_INTEGRATION_2026-09-29.md)：322 项前端逻辑和 218 项 Python unittest；不是新一轮 GB10 性能或全平台认证。

提交前还需处理已知历史隐私问题：旧提交曾包含真实 SSH 连接信息。最新文件移除不等于历史清除；这次材料补充没有重写 Git 历史，也不声称仓库历史已经完成脱敏。
