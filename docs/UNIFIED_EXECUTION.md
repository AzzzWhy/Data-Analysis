# 对话热工作进程与计划批量执行

本地整合分支：`codex/hybrid-unified`。两个原实验分支的提交历史已合并。当前文档描述整合后的入口；[修复与实测记录](PUBLIC_DATA_REPAIR.md)仍保留原实验口径。这里的代码和文档只在本地，未推送 GitHub、未替换 GB10 上的正式部署。

## 选择入口

| 需求 | 入口 | 数据生命周期 |
| --- | --- | --- |
| Agent 对话、逐条提问 | `python agent/agent_main.py` | 常驻工作进程。单次分析可用有上限的 GPU 帧缓存；自适应追问使用 `dataset_session`，在会话内复用同一帧。 |
| 已知的多项分析 | Agent 的 `analyze_batch`，或下述批量命令 | 所需列取并集，读取和转换一次，多步骤共享精确统计；结束即释放，不占用对话会话。 |
| 固定报表、系统定时任务 | `python agent/batch_job.py --plan ... --output-root ...` | 每次生成独立的 `report.md` 与完整 `result.json`，有失败退出码。定时频率由操作系统调度器决定。 |
| 多个已规划的报表 | `python agent/batch_queue.py --manifest ... --output-root ...` | 每份计划在独立子进程执行；纯 CPU 任务有界并行，可能使用 GPU 的任务独占队列，结果分别保存。 |

两种路径都可按实际任务选 pandas/CPU、cuDF 原生读取＋GPU 计算，或受支持的 CPU 读取＋GPU 计算。`auto` 批量模式只使用**同文件、同列、同任务、同运行环境**且仍有效的校准结果；没有相符校准时保守采用现有路由。对话会话在打开时尚不知道后续操作，因此 `load_backend=auto` 使用原生读取；已实测需要混合读取时，可在 `dataset_session(operation="open", load_backend="cpu_gpu", usecols="数值列1,数值列2")` 显式选择。`usecols` 限定常驻帧的列，后续分析无法访问未加载列。返回值中的 `loading.actual` 和每步 `engine` 才是实际执行路径。混合读取目前适用于受支持的数值 Parquet 与 XLSX；不适用时显式请求会报错。

## 固定报表和定时任务

复制 [示例计划](../examples/batch-plan.json)，将 `file_path` 改为本机数据路径。相对路径以计划文件所在目录为准。一个计划含 1～8 个步骤；`groupby` 需要 `by`，可用 `top_k` 控制展示组数。支持 `profile`、`summary`、`groupby`、`corr`、`outliers`、`auto`。不需要模型 API 密钥。

若已用 `benchmark/hybrid_calibrate.py --mode batch` 为**同一文件、列和步骤**生成校准文件，可在计划中填 `"hybrid_profile": "/absolute/path/to/profile.json"` 并保留 `"load_backend": "auto"`。校准过期、文件变化或环境变化时，路由不会套用旧数据；不要把含机器及文件身份的校准文件上传到仓库。也可显式选 `native` 或 `cpu_gpu` 做受支持场景的验证。

```bash
python agent/batch_job.py --plan examples/batch-plan.json --output-root reports
```

命令成功时打印这次运行的目录并返回 0；分析或校验失败则返回非零且不会生成成功报表。每次运行都新建时间戳目录，不覆盖旧报告。`report.md` 汇总行数、实际加载方式、每步实际引擎及主要表格；`result.json` 保留完整数值、路由和时间证据。报表的“分析调用耗时”包含本次工作进程启动，工作进程内部的 `total_seconds` 不包含启动；两者不可混称。报表不会虚构 CPU/GPU 加速倍数。

系统定时器只需按所需频率执行同一条命令。例如 Linux crontab 的工作日 08:00：

```cron
0 8 * * 1-5 cd /absolute/path/to/Data-Analysis && /absolute/path/to/python agent/batch_job.py --plan /absolute/path/to/plan.json --output-root /absolute/path/to/reports
```

定时任务应使用绝对解释器、计划和输出路径；所需 pandas/pyarrow/cuDF 环境由该解释器提供。运行目录要有写权限。数据文件改变后，下次运行读取新文件；不会跨批次借用旧帧。

## 多进程、多批次队列

一个清单可含 1～32 份固定计划，每份计划仍限 1～8 个分析步骤。清单里的计划路径相对于清单文件；每份计划读取自己的数据、在独立 Python 子进程及分析 worker 中执行，产出独立报告。示例：

```bash
python agent/batch_queue.py --manifest examples/batch-queue.json --output-root reports/queues --max-cpu-workers 2
```

每次队列执行新建 `queue-*` 目录，内有 `queue-summary.json` 和 `reports/`。摘要保留每个任务的进程 ID、耗时、成功/失败与报告路径；有任何失败时命令返回非零，但其他任务的结果不会被丢弃。默认单任务上限一小时，可用 `--timeout-seconds` 在 1～86400 秒间调整；超时会停止该任务的子进程组。可将上面命令交给操作系统定时器定期执行。

队列按清单顺序调度：连续的 `force_cpu=true` 任务最多并行 2 份（可调 1～4）；其余任务包括 `auto` 都按“可能使用 GPU”对待，**一次只运行一份，且与该队列的 CPU 任务不重叠**。这是统一内存机器上的保守上限，不能把多个进程同时启动误称为数据分块并行、CPU/GPU 流水线重叠或多 GPU 加速。每份任务内部的 CPU 读取＋GPU 计算仍是读取、转换、计算的顺序流水，不并发叠加算力。

队列的任务顺序只协调**同一次清单运行内**的任务。采用本版本 worker 的 Agent 热会话、单次分析和不同队列进程，还会共用当前用户临时目录下的跨进程 GPU **计算许可**：打开 GPU 帧、执行 GPU 步骤和批量 GPU 分析时持有许可，步骤完成即释放；热会话的帧可继续驻留，不会阻塞其他任务整段对话。CPU 强制任务不占用许可，因此队列中的两个 CPU 子进程仍可并行。默认等待上限 300 秒，可用 `GPU_ANALYSIS_GPU_SLOT_TIMEOUT` 调整到不超过 1800 秒；超时明确报错。需要跨用户共享许可时，所有进程须把 `GPU_ANALYSIS_GPU_SLOT_FILE` 设置为同一个可写的绝对路径。

这个许可只串行化 GPU 计算，**不是全局显存/统一内存预留器**：热会话驻留帧仍占内存，每个进程依靠已有的内存准入检查。旧版 worker 或未使用本版本的外部程序也不会遵守该许可。Agent 对话继续使用常驻工作进程，不会为每次提问新建队列；已经规划好的跨报告任务才使用队列。尚无跨批次帧共享、多 GPU 分布式调度或 CPU 读取与 GPU 计算的异步重叠。

## 性能与限制

既有 GB10 实验在同一组三项分析中，批量比逐项重新读取的热工作进程快约 1.4～1.8 倍，主要因为批量共用一次读取和转换。具体数值见[修复与验收记录](PUBLIC_DATA_REPAIR.md)及[原始规模样本](hybrid-evidence/public-repair-scale.jsonl)。这描述的是旧热工作口径。

整合后新增的同口径测试让**对话常驻会话与批量都只读一次**。真实家庭用电数据 2,075,259 行，摘要、异常值、相关性三项，显式 CPU 读取＋GPU 计算，五轮交错测试：热会话中位数 **0.09018 秒**，批量 **0.08794 秒**；批量约快 **1.025 倍**。两路结果一致、实际加载路径均为 `cpu_gpu`、源文件未变、每轮后无活动会话或帧缓存。首次热会话为 0.389 秒，明显高于后续轮次；进程启动另测为 0.647 秒，均未计入上述稳态中位数。这个结果只覆盖一份数据、一组任务，不能推广为所有数据规模的加速比。[五轮原始记录](hybrid-evidence/unified-power-2m.json)；复测脚本为 [`benchmark/unified_execution_benchmark.py`](../benchmark/unified_execution_benchmark.py)。

计划步骤必须针对同一个文件且操作参数事先已知。需要依据上一步结果才能决定下一步的对话，使用 `dataset_session`；该会话保持帧常驻，但受内存预算、文件身份校验及到期淘汰约束。计算引擎可因格式或操作限制选择 CPU，不能从“启用了混合模式”推断每一步都由 GPU 完成。

## 本地验收

```bash
python agent/batch_job_test.py
python agent/batch_queue_test.py
python agent/fast_execution_test.py
python agent/interactive_reuse_test.py
python skills/cudf-analytics/scripts/hybrid_batch_test.py
python skills/cudf-analytics/scripts/hybrid_execution_test.py
python skills/cudf-analytics/scripts/gpu_coordination_test.py
```

真实设备的多进程、混合路径和热会话联测：`python agent/batch_queue_live_test.py --input /absolute/path/to/numeric.parquet --output-root /tmp/gda-queue-live`。输入须包含数值 `region`、`revenue` 列；测试产出在指定临时目录，原始数据只读。该测试还让另一个进程短暂持有 GPU 计算许可，验证 Agent 热会话确实等待共享许可后继续。

2026-09-28 的 GB10 隔离目录冒烟测试：真实家庭用电 Parquet 的 2,075,259 行上，显式 `cpu_gpu` 批量执行摘要、异常值和相关性，`loading.actual=cpu_gpu`，三步的实际引擎均为 `cudf`，结束后活动会话数为 0。对话会话用 `usecols=Global_active_power,Voltage` 显式混合加载，连续两步均为 `cudf`；关闭后再次打开命中受限帧缓存，实际加载方式仍报告为 `cpu_gpu`，活动会话数仍为 0。固定报表命令在示例 CSV 上生成 `report.md`、`result.json`，实际路径为 CPU。以上是功能冒烟，不是五轮性能对照，不据此声明新版本的加速倍数。

同一隔离目录连接 GB10 正在运行的本地 Qwen 模型做了两条完整 Agent 调用链。逐步追问触发 `dataset_session` 的打开、两次分析和关闭；2.08 百万行文件按路由阈值选 CPU，模型没有将其称作 GPU 加速。事先规划好的多项分析触发一次 `analyze_batch`，返回每步实际 pandas 引擎和记录的 CPU 选择原因；中文问题得到中文回答。该单次模型测试只能证明调用链可工作，不保证所有模型或每次输出都一致。
