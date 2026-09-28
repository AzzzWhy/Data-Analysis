# 更快的分析执行

这一轮不改变数值定义，不抽样，不以放宽内存保护换取成功。

## 默认行为

- `SKILL_SHOW_SPEEDUP` 默认 **0**：正常分析不额外执行两次 pandas 基线，会话也不额外读一次 CPU 数据。需要展示对照实验时显式设为 `1`。已有比较缓存不代表本次实际执行了基线。
- `analyze_dataset` 默认复用分析工作进程的解释器、导入模块和 CUDA 上下文。每个单次请求仍读取所需数据并释放自己的帧；这是进程复用，不是结果缓存或数据驻留。首次调用仍承担启动成本。
- `GPU_ANALYSIS_PERSISTENT_WORKER=0` 可恢复独立进程模式。工作进程通信失败时自动降级；正常的列名/操作错误直接返回，不重复计算。
- 显式开启 CPU 对照时，单次分析恢复独立进程计时，避免拿 GPU 热上下文与 CPU 冷启动对比。它不代表最快的交互模式。
- 工作进程超时会停止并在下一次请求重新启动；stderr 持续排空，避免诊断信息塞满管道。若进程重启，旧会话 ID 不再有效，应重新打开数据。

## 一次调用，多项分析

模型新增 `analyze_batch` 工具，适合已知列名、无需根据上一项结果调整参数的 1–8 项分析。示例：

```json
{
  "file_path": "/data/sales.csv",
  "steps": [
    {"op": "summary", "columns": "revenue"},
    {"op": "outliers", "columns": "revenue"},
    {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 5}
  ]
}
```

执行器取所需列的并集，只打开一次数据，按顺序执行并共享精确统计量。profile/auto 或无法证明列足够的步骤仍读取完整模式。按原有路由选择 CPU/GPU；小数据可以使用 CPU 驻留帧。成功或中途失败均关闭自有会话，不接管模型已打开的同文件会话。

默认分组返回前三十组，每一步若截断会给出覆盖警告；不能据此推断全局最小值。相关矩阵送给模型前仍按原规则压缩。检查每一步实际 engine/fallback，而不是只看最初加载引擎。

需要自适应下钻时仍使用 `dataset_session`。batch 减少工具往返，不宣称所有算子融合成一次物理扫描。

## 耗时口径

`phase_timings` 区分初始化、加载与计算；`transport_timings.wall_seconds` 包含工作进程第一次启动和通信。已加载会话的 load 阶段只取帧，不表示读盘。GPU 加载计时后同步，避免把异步加载错误归到计算阶段。

cuDF 不支持操作导致 pandas 重试时，attempts 列表记录已完成加载的尝试；总请求时间才涵盖失败和重试的全部开销。不得将局部阶段耗时当端到端耗时，模型推理仍需另计。

已有 Parquet 缓存仍通过 `GPU_ANALYSIS_PARQUET_CACHE_DIR` 显式启用。第一次转换完整 CSV 的成本归首次请求；后续按需读列。此轮未添加过滤表达式下推、DuckDB 后端或自动磁盘缓存策略。

## 验证

```sh
python agent/fast_execution_test.py
python agent/tool_contract_test.py
python skills/cudf-analytics/scripts/smoke_test.py --require-gpu
python benchmark/fast_execution_benchmark.py --small /data/small.csv --large /data/large.csv
```

基准使用独立进程、热工作进程和单次加载批量执行三种模式，比对真实结果。它不计模型推理，不清空系统页缓存，也不是最优多线程 CPU 引擎的结论。
