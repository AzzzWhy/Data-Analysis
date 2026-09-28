# GUI 对接分析核心：验收用接口约定

本文只说明已有分析核心的调用方式，不新增或修改 GUI。核心部署基线为 `6e287a2`；真实机器验收见 [核心收尾验收](FINAL_CORE_DEPLOYMENT.md)。

## 1. 对话与固定任务使用不同入口

| 场景 | 已有入口 | GUI 负责什么 |
| --- | --- | --- |
| 自然语言对话 | `agent_main.Agent(client, model=..., reuse_one_shot=True, event_sink=...).run(question)` | 在后台执行；保持同一 Agent 实例和对话历史；展示最终文字与真实工具结果。 |
| 已规划好的同文件分析 | `skills.analyze_batch(file_path, steps, load_backend="auto")` | 传递已确认的文件和列；解析返回的 JSON 字符串。不要为了展示加速比自动再跑 CPU。 |
| 单份固定报表 | `agent/batch_job.py --plan ... --output-root ...` | 管理任务状态、退出码以及 `report.md`、`result.json`。 |
| 多份报表、定时任务 | `agent/batch_queue.py --manifest ... --output-root ...` | 计划由清单表达，定时由调用者或操作系统调度器触发；不是让模型临时重写计划。 |
| 高频重复报表 | `agent/report_service.py --output-root ...` | 保持服务的标准输入/输出连接；每行一个请求；结束时发送 `close` 并等待确认。 |

`client` 沿用项目 `api_config` 连接配置创建；不要在源码中写密钥。Agent 的 `event_sink` 接收的是日志文字，不是结构化的 GUI 事件协议。`run()` 返回最终文字，现有入口不是 token 流式接口。GUI 若要展示工具详情，应读取 `Agent.messages` 中实际 `role="tool"` 消息的 JSON 内容，而不是从模型回复里推断引擎。

同一进程的 `skills` 共享一个带锁的对话 worker。不要让多个 GUI 线程同时改变全局环境变量或并发写入同一个 Agent 的历史。报表服务与对话 worker 使用不同的生命周期；关闭 GUI 时只停止自己拥有的进程，不停止机器上的模型服务或其他项目进程。

## 2. `analyze_batch` 请求

```python
steps = [
    {"op": "summary", "columns": "revenue"},
    {"op": "outliers", "columns": "revenue", "top_k": 3},
    {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3},
]
raw = skills.analyze_batch(file_path, steps, load_backend="auto")
```

每次 1～8 步；仅允许 `op/by/agg/columns/top_k` 步骤字段。默认自动选择；高级调试可设置 `force_cpu`、`force_gpu` 或显式 `load_backend="cpu_gpu"`，冲突参数会报错。选择混合加载不代表一定更快，也不代表任意格式都受支持。

`hybrid_profile` 可以显式传给函数；也可在启动分析进程前配置 `GPU_ANALYSIS_HYBRID_PROFILE`。校准必须匹配机器、实现指纹、文件身份、列、完整操作参数及冷/热场景，有效期为 24 小时。任一条件变化就退回已有保守策略，不在正常请求中自动做三路性能实验。Agent 常驻会话的未来步骤未知，不能套用已规划批量任务的速度承诺。

## 3. 显示字段：以工具结果为准

| 界面信息 | 实际字段 | 注意 |
| --- | --- | --- |
| 成功/失败 | `success`；失败时 `error`，可能有 `hint` | 失败结果可能没有 `loading` 和 `results`，先检查成功标记。 |
| 加载路径 | `loading.actual` | `cpu`、`native_gpu`、`cpu_gpu` 分别对应 CPU、GPU 原生读取、CPU 读取后转 GPU。 |
| 冷/热状态 | `loading.execution_context` | `cold` / `warm` 指 GPU 引擎是否已初始化，不等于“文件已缓存”。小数据始终 CPU 时，后续请求仍可能标记 cold。 |
| 选择原因 | `loading.decision.policy/reason/selected` | `matched_hybrid_calibration` 表示命中校准；其他策略不能展示成“已训练的最优路线”。 |
| 每一步真正使用的引擎 | `results[i].engine` | `pandas` / `cudf`；实际回退看 `fallback_reason`。不要只看顶层 GPU 标识。 |
| 扫描行数 | `results[i].rows_scanned` | 事务内精确结果复用时可为 0；同时看 `source_rows`、`result_reused`，不要解释成空文件或抽样。 |
| 步骤耗时 | `results[i].step_seconds`、`execution_decision.observed.compute_seconds` | 前者包括步骤开销，后者是计算口径，不与总壁钟时间混用。 |
| 引擎初始化/读取耗时 | `loading.engine_init_seconds/load_seconds/attempts` | 混合读取的 CPU 读取、转换时间在 attempts 中；并非每种路径都有所有子字段。 |
| 批次总耗时 | `total_seconds` | 工具内部口径；GUI 请求完整壁钟时间另外计时，包含首次 worker 启动与传输。 |
| 数值结果 | `results[i][op]` | 摘要、分组、异常值具有不同结构，不从自然语言答案反向提取。 |
| 是否实测加速比 | `comparison_measured` | 默认 batch 为 false，不能展示虚构的 CPU 基线或“加速 N 倍”。 |

建议界面显示“自动 · CPU / GPU / CPU→GPU”“实际引擎”“用时”“选择原因”。当没有可比基线时，加速比显示“未测量”，不要填 1× 或预计倍数。

## 4. 常驻报表服务协议

启动后服务输出：

```json
{"event":"ready","protocol_version":1,"process_reuse":true,"cross_request_frame_cache":false}
```

客户端每行发送一份 JSON，日志不要写入该输入流：

```json
{"cmd":"run","manifest":"/absolute/path/to/queue.json","id":"report-001"}
{"cmd":"status","id":"status-001"}
{"cmd":"close","id":"close-001"}
```

`run` 响应包含 `id/ok/run_dir/wall_seconds/summary`；成功也应检查 `summary.jobs[i].ok`，从 `report_dir/result.json` 读取该报表的 `result`。`status` 返回 `completed_requests/worker_pids/cross_request_frame_cache`。`close` 在关闭拥有的 worker 后返回 `ok=true,event="closed"`；输入 EOF 也会清理。

服务不是网络 HTTP API，没有额外监听端口。请求行最多 16 KiB；超限返回错误后关闭输入流。普通失败返回 `ok=false,error`。GUI 应区分服务断开、任务失败与模型失败，保留重试入口，不将模型错误显示成 GPU 故障。

## 5. 演示前检查

1. 小文件与大文件都能返回完整数据结果；选择 CPU 不是故障。
2. 明确区分首次启动、热工作请求和模型回答耗时。
3. 同文件多报表事务能复用结果，但报表服务每次请求都会重读文件。
4. 缺失文件有错误提示；关闭对话活动会话与退出服务后不遗留自己拥有的计算进程。
5. API 密钥、机器连接信息与本地校准文件不写入提交内容或界面诊断截图。

全量分析在本机进行，但用户问题、路径、工具返回的统计摘要及有界预览/异常样例会进入所选模型上下文。使用外部 API 时，这些内容会送往该服务；“不上传完整数据文件”不等于“模型看不到任何数据值”。
