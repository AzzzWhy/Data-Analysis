# CPU 加载＋GPU 计算：三个开发分支

三个分支均从本轮更新后的 `main` 同一提交创建，不相互继承。当前状态为**开发方案分支**：已有常驻、批量和分项计时基础，但 CPU 读取后转为 GPU 数据帧、自动混合路由尚未实现。各分支的 `docs/HYBRID_EXECUTION_PLAN.md` 明确具体范围，不应将现有 GPU 原生读取的成绩标成混合执行成绩。

| 分支 | 重点 | 进程生命周期 | 数据生命周期 | 分支说明 |
|---|---|---|---|---|
| `codex/hybrid-single-process` | 单进程、单次分析 | 一次 CLI 调用内 CPU 加载、转换、GPU 计算；完成退出，无常驻工作进程 | 每次重新读取、转换、释放 | [单进程方案](https://github.com/AzzzWhy/Data-Analysis/blob/codex/hybrid-single-process/docs/HYBRID_EXECUTION_PLAN.md) |
| `codex/hybrid-warm-worker` | 热工作进程 | 跨请求复用解释器及 CUDA 上下文 | 基础方案每次重新读取、转换，不缓存数据或结果 | [热工作进程方案](https://github.com/AzzzWhy/Data-Analysis/blob/codex/hybrid-warm-worker/docs/HYBRID_EXECUTION_PLAN.md) |
| `codex/hybrid-batch` | 批量执行 | 复用现有工作进程，一次请求执行有序多步骤 | 所需列并集，CPU 读取一次、GPU 转换一次，共享帧及精确统计，结束释放 | [批量方案](https://github.com/AzzzWhy/Data-Analysis/blob/codex/hybrid-batch/docs/HYBRID_EXECUTION_PLAN.md) |

## 共同原则

目标不是“CPU 比 GPU 快就强制混合”，而是选择预计端到端时间最小的路径：全 CPU、GPU 原生读取、CPU 读取＋GPU 计算。候选混合路径必须同时满足 GPU 支持该计算、有足够内存、收益能够覆盖转换及必要初始化成本。

`T_hybrid = CPU_read + conversion + GPU_compute + non-amortized_startup/transport`

比较必须使用同文件、同所需列、同查询及相同的进程/缓存口径。列少是候选特征，不是强制路由条件；压缩率、解码成本、数据类型、操作和复用次数同样影响结果。GB10 统一内存不保证 pandas/Arrow 转 cuDF 零拷贝。

## 实现与验证要求

1. 先支持 Parquet 的 CPU/PyArrow 投影读取，研究 Arrow→cuDF 转换，避免无必要的 pandas 中间帧。CSV 作为后续独立扩展，不能用不同格式偷换基准。
2. CPU 加载与 GPU 转换不能绕过内存保护；预算覆盖可能同时存在的 CPU/GPU 帧、解析中间数据和计算工作区。不停止模型、不自动清缓存、不降低阈值。
3. 分项报告进程启动、CPU 读取、GPU 转换、GPU 初始化、计算、通信及端到端时间；GPU 异步阶段同步后计时。首启、重复请求不能混为同一种“热态”。
4. 提供显式评测方式；正常回答不默认额外执行两套完整对照。路由校准应有边界、缓存失效条件，不为选择引擎反复扫描全文件。
5. 数值结果、扫描行数、类型和空值语义与全 CPU/GPU 原生路径一致。保留各步骤真实引擎与降级原因，转换失败需释放资源并保守回退，不虚构 GPU 成绩。
6. 同一套小、中、大、上亿行材料测试三个分支。已有 500 万、2000 万 CSV 的 CPU 三项同口径对照仍需补齐；已有上亿行 Parquet 可作为共同原料。原始数据与密钥不入库。
7. 开发后每方式至少五次、正逆序交错，分别公布首启、稳态中位数和波动；记录系统缓存、模型负载与内存状态。任何超大倍率必须说明是否仅为省掉启动的架构收益。

## 当前已验证的基线

真实 113,500,327 行两列 Parquet，三项全量分析、每方式两次：全 CPU 批量 4.008 秒，GPU 原生批量 1.528 秒，CPU/GPU 2.62 倍。GPU 独立进程 7.140 秒、常驻 2.881 秒。以上是已实现路径，**不是混合路径成绩**；详细范围与限制见 [实验报告](POST_CACHE_AND_PUBLIC_DATA_RESULTS.md)。

建立分支不代表发布功能。混合功能完成后应在各自分支补充代码、回归测试及同口径实测，审核后再合回 `main`。
