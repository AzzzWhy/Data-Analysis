# 对话热工作进程与计划批量执行

本地整合分支：`codex/hybrid-unified`。两个原实验分支的提交历史已合并。当前文档描述整合后的入口；[修复与实测记录](PUBLIC_DATA_REPAIR.md)仍保留原实验口径。这里的代码和文档只在本地，未推送 GitHub、未替换 GB10 上的正式部署。

## 选择入口

| 需求 | 入口 | 数据生命周期 |
| --- | --- | --- |
| Agent 对话、逐条提问 | `python agent/agent_main.py` | 常驻工作进程。单次分析可用有上限的 GPU 帧缓存；自适应追问使用 `dataset_session`，在会话内复用同一帧。 |
| 已知的多项分析 | Agent 的 `analyze_batch`，或下述批量命令 | 所需列取并集，读取和转换一次，多步骤共享精确统计；结束即释放，不占用对话会话。 |
| 固定报表、系统定时任务 | `python agent/batch_job.py --plan ... --output-root ...` | 每次生成独立的 `report.md` 与完整 `result.json`，有失败退出码。定时频率由操作系统调度器决定。 |

两种路径都可按实际任务选 pandas/CPU、cuDF 原生读取＋GPU 计算，或受支持的 CPU 读取＋GPU 计算。`auto` 批量模式只使用**同文件、同列、同任务、同运行环境**且仍有效的校准结果；没有相符校准时保守采用现有路由。对话会话在打开时尚不知道后续操作，因此 `load_backend=auto` 使用原生读取；已实测需要混合读取时，可在 `dataset_session(operation="open", load_backend="cpu_gpu")` 显式选择。返回值中的 `loading.actual` 和每步 `engine` 才是实际执行路径。混合读取目前适用于受支持的数值 Parquet 与 XLSX；不适用时显式请求会报错。

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

## 性能与限制

既有 GB10 实验在同一组三项分析中，批量比热工作进程快约 1.4～1.8 倍，主要因为批量共用一次读取和转换。具体数值见[修复与验收记录](PUBLIC_DATA_REPAIR.md)及[原始规模样本](hybrid-evidence/public-repair-scale.jsonl)。那些是整合前两分支的测试；本地整合后仍需在 GB10 上复测，不能将该倍率写成新版本的实测成绩。

计划步骤必须针对同一个文件且操作参数事先已知。需要依据上一步结果才能决定下一步的对话，使用 `dataset_session`；该会话保持帧常驻，但受内存预算、文件身份校验及到期淘汰约束。计算引擎可因格式或操作限制选择 CPU，不能从“启用了混合模式”推断每一步都由 GPU 完成。

## 本地验收

```bash
python agent/batch_job_test.py
python agent/fast_execution_test.py
python agent/interactive_reuse_test.py
python skills/cudf-analytics/scripts/hybrid_batch_test.py
python skills/cudf-analytics/scripts/hybrid_execution_test.py
```
