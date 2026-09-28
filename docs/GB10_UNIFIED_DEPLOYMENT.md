# GB10 整合版部署与协调验收（2026-09-28）

运行代码版本：`0cedf6b`，来自 `codex/hybrid-unified`。直接同步到 GB10 独立目录 `~/Data-Analysis-unified`，机器上的检出分支是 `codex/hybrid-unified-gb10`。旧 `~/Data-Analysis` 的 `workbench-gui` 分支及未跟踪文件原样保留，没有覆盖、清理或修改模型服务。未推送 GitHub。

## 打开整合版 Agent

SSH 登录机器后，用新目录打开；从旧目录启动的仍是旧版本。使用已经运行的本机无鉴权模型服务时：

```bash
cd ~/Data-Analysis-unified
conda activate rapids-cudf
CUDA_PATH=/usr/local/cuda-13.0/targets/sbsa-linux GPU_API_KEY=local python agent/agent_main.py --base-url http://127.0.0.1:8000/v1 --model qwen3.6-35b-a3b-fp8 --language zh
```

`local` 只是本机无鉴权服务的占位符，不是真实外部 API 密钥。上述环境变量仅对本次启动生效，不覆盖保存的连接设置；如果需要更换地址或模型，可使用 TUI 的设置入口。TUI 未安装或不可用时会退回基础终端界面。

## 当前真实能力

| 部分 | 实现与边界 |
| --- | --- |
| CPU/GPU 路由 | 支持纯 CPU、GPU 直读计算及受支持格式的 CPU 读取→转换→GPU 计算；混合路径不是 CPU/GPU 同时对数据块计算。 |
| 对话热工作 | 常驻 worker 和驻留数据会话，连续问题共用数据；内存保护与文件变更检查仍有效。 |
| 单个批次 | 同一文件的 1～8 个已知分析共用一次读取，顺序执行，完成释放独占帧。 |
| 多计划、多进程 | `batch_queue.py` 管理 1～32 份计划；连续纯 CPU 计划默认两个独立任务进程并行，每份任务另外复用自己的分析 worker；可配置 1～4 份任务并行。 |
| GPU 多批次协调 | 可能使用 GPU 的计划（包含 auto）在本队列中独占执行，不与本队列的 CPU 任务重叠。 |
| 跨队列与热会话协调 | 新版 GPU worker 和单次 CLI 共用跨进程 GPU 计算许可，步骤或批次计算期间互斥；CLI 备用路径也遵守该许可。等待超时明确失败。 |

许可不占据整个对话：驻留数据可以继续保持，其他 GPU 任务可在两次提问之间获得计算许可。它不是全局内存预留器：驻留帧仍占统一内存，内存准入检查不能被协调层绕过。外部模型服务、旧 worker 和其他程序不受此许可调度。尚无 CPU 读取与 GPU 计算异步重叠、跨批次帧共享、多 GPU 调度或数据分块进程池。

多计划例子：

```bash
python agent/batch_queue.py --manifest examples/batch-queue.json --output-root reports/queues --max-cpu-workers 2
```

生成每份任务的独立报表和 `queue-summary.json`。同一文件上已经知道的几个操作应优先放入一份计划，只读一次；不要为追求进程数人为拆成多个计划并重复读数。定时触发由系统定时器执行上述命令，不是安装了新的后台调度服务。

## 机器验收结果

本机 Windows 与 GB10 均通过 64 项相关回归：GPU 许可 5 项、快速执行 14 项、多计划队列 5 项、固定报表 3 项、批量混合 14 项、公共混合 18 项、交互复用 5 项。超时终止、锁释放、重入、CPU 并行和失败隔离均有测试；Windows 无法验证子进程树终止时会明确报告，不能假称完整清理。

实际 TLC 数值 Parquet 联测，每档包含两份 CPU 计划、两份混合 GPU 计划（每份都是收入摘要及区域收入汇总两项），然后运行另一个热会话的两项分析。另一个进程短暂持有 GPU 许可，验证热会话确实等待后恢复。下面是单次功能验收，不是五轮性能基准；队列总耗时包含四份计划的启动、读取、计算，不含后续热会话与人为持锁等待，不能与此前三项分析的表格直接算加速比。

| 行数 | 四计划队列总耗时 | CPU 两任务重叠 | GPU 两任务串行 | 热会话等待共享许可 | CPU/GPU/热会话结果一致 |
| ---: | ---: | :---: | :---: | ---: | :---: |
| 20,000 | 2.9107 秒 | 是 | 是 | 1.2134 秒 | 是 |
| 1,000,000 | 2.8950 秒 | 是 | 是 | 1.2108 秒 | 是 |
| 113,500,327 | 9.1989 秒 | 是 | 是 | 1.2132 秒 | 是 |

每档实际加载路径是 `cpu, cpu, cpu_gpu, cpu_gpu`，混合任务每步实际引擎为 cuDF，CPU 任务每步为 pandas。输入文件未变化，结束无活动会话或缓存帧，系统进程检查没有残留 `gpu_session.py`、`batch_job.py` 或 `batch_queue.py`。

原始脱敏证据：[2 万](hybrid-evidence/gb10-coordination-20260928/coordination-20k.json)、[100 万](hybrid-evidence/gb10-coordination-20260928/coordination-1m.json)、[1.135 亿](hybrid-evidence/gb10-coordination-20260928/coordination-113m.json)。

GB10 正在运行的本地 Qwen 模型另通过完整 Agent 调用链：自主调用一次 `analyze_batch`，完成三行 CSV 的摘要、异常检测和分组汇总，实际 CPU 结果正确，测试输出 `LIVE_BATCH=PASS`。这是小数据工具调用验收，不代表模型自主选取了多计划队列或能保证每次选择同样的工具。
