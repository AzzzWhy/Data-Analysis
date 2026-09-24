<div align="center">

# Data-Analysis Agent Skill

### 让大模型在 GB10 上用 GPU 做**真实**的全量数据分析

*第三届 NVIDIA DGX Spark 黑客松 · Agent Skills 开发挑战赛*

<br>

<img alt="platform" src="https://img.shields.io/badge/platform-NVIDIA%20GB10-76B900?style=for-the-badge&logo=nvidia&logoColor=white">
<img alt="engine" src="https://img.shields.io/badge/engine-RAPIDS%20cuDF-7400B8?style=for-the-badge">
<img alt="llm" src="https://img.shields.io/badge/LLM-step--3.7--flash-4B8BBE?style=for-the-badge">
<img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=for-the-badge">

<br><br>

**数据永远不进模型上下文。**
模型只决定「算什么」,统计量由 GPU 在**全量数据**上算出来。

<br>

|  |  |
| :--- | :--- |
| 🔍 **自主决策** | 自己判断该调哪个 Skill、参数怎么填,不需要人指定 |
| ⚡ **GPU 加速** | cuDF 全量计算,20M 行分组聚合 **3.93×**、相关性 **3.69×** |
| 📊 **当场证明** | 每次回答都附上**这一问实测**的 GPU/CPU 耗时对比 |
| 🛡️ **诚实降级** | 没有 GPU 就自动回退 pandas,并如实声明 `engine="pandas"` |
| ✅ **可复核** | 59 项自检 + 7 项评审用例 + 每个数字独立复算 |

</div>

---

## 它解决什么问题

大模型直接读 CSV 做分析有三个死穴:**读不进**(大文件超出上下文)、**算不准**(靠抽样猜)、**算不快**(纯 CPU)。

这个 Agent 的做法是把两件事拆开:

```
        用户用中文提问
              │
              ▼
   ┌──────────────────────┐
   │  大模型（只做决策）    │   决定：调哪个 Skill？参数怎么填？
   │  step-3.7-flash      │   看不到数据本身
   └──────────┬───────────┘
              │  function call
              ▼
   ┌──────────────────────┐
   │  Skill（在本机执行）   │   cuDF 全量计算，不采样
   │  analyze_dataset     │   同时实测 CPU 对照
   └──────────┬───────────┘
              │  真实统计量（JSON）
              ▼
   ┌──────────────────────┐
   │  大模型（只做表达）    │   中文结论 + 实测加速比
   └──────────────────────┘
```

**数据不经过模型,所以数据量不受模型限制。** 20,000,000 行、3 GB 的 CSV,模型只需要看到几十行统计结果。

---

## 现场实录

真实运行输出(`python demo_script.py`,2000 万行 / 3.04 GB):

```console
用户: 按 region 统计 sales_demo.csv 的 revenue 总和与均值，各取前5

  [round 1] -> analyze_dataset({"file_path": "sales_demo.csv",
                                "operation": "groupby", "by": "region",
                                "agg": "revenue:sum,mean", "top_k": 5})
      OK  engine=cudf  rows=20,000,000  2.63s  | GPU 2.63s vs CPU 10.22s = 3.88x
--- Agent 回答 ---
## Revenue 总和排名（Top 5）
| 排名 | 地区  | 总收入            |
|------|-------|-------------------|
| 1    | LATAM | 4,300,004,248.14  |
| 2    | AMER  | 4,299,776,626.08  |
| 3    | APAC  | 4,298,033,716.38  |
| 4    | MEA   | 4,294,925,776.77  |
| 5    | EMEA  | 4,286,931,276.58  |

## 运行情况
本次分析在 GPU（NVIDIA GB10）上通过 cuDF 完成，全量 20,000,000 行耗时 2.63 秒；
同一计算在 CPU pandas 上耗时 10.22 秒，GPU 快 3.88 倍。
> 注意：端到端耗时包含 CSV 读取，读取阶段两引擎都在用多核并行；pandas 基线默认
> 单线程，因此该倍数并不完全等同于纯计算阶段的 GPU 优势。
```

注意三件事:**没有给操作名**(模型自己选的 `groupby`)、**扫描了全量 2000 万行**、**加速比是这一问当场测出来的**。

---

## 对照评审标准

| 评审维度 | 本项目的实现 | 可验证的证据 |
| :--- | :--- | :--- |
| **① 技能调用能力**<br><sub>自主判断何时调用 + 参数正确</sub> | 模型从 2 个 Skill 中自主选择;未给文件时先调 `list_datasets` 发现数据;概念性问题**不调**工具;列名报错后自动改调 `profile` 学真实列名再重试 | `run_criteria_tests.sh` **7/7 通过**,断言打在工具调用轨迹上而非回答文本上 |
| **② 任务完成度**<br><sub>自然语言 → 真实结果</sub> | 6 种操作全部返回真实统计量;回答里给中文结论、排名、表格与关键发现 | `smoke_test.py` **59 项断言**;`verify_*.py` 独立复算 |
| **③ 创新性** | 见下方「创新点在哪」 | 加速比在对话内当场测量 |
| **④ 代码可用性**<br><sub>可部署 / 健壮 / 异常处理</sub> | 引擎自动降级;`analyze_dataset` **永不抛异常**;无 GPU、文件不存在、列名错误、中文列名都能优雅处理并提供可执行的下一步 | 无 GPU 路径可跑;**第⑥步**演示文件不存在;`run_criteria_tests.sh` 覆盖 3 类异常输入 |
| **⑤ 演示效果**<br><sub>端到端对话流畅</sub> | 7 步脚本化演示,**76 秒**,每步显示加速比;`--prewarm` 预热保证现场零等待 | `demo_script.py` |

### 创新点在哪

坦白说,"用 cuDF 加速数据分析"本身门槛极低。所以我们把创新放在**可被验证的诚实性**上:

**① 加速比在对话内当场测量,而不是引用一张事先准备好的表。**
评委看到的不是"我曾在别处测到 6.45×",而是"这一问、这份文件、这条命令,GPU 2.63 秒,CPU 10.22 秒"。

**② 主动报告对自己不利的数据。**
归因实验证明端到端加速里**只有约 3× 来自 GPU 计算**,其余主要是并行 CSV 解析(polars 也能做到);pandas 基线只用 1 核而机器有 20 核。**这些限定条件写在 README 和系统提示词里,由模型主动向用户复述。**

**③ 真实数据上发现的正确性 bug。**
见下方「只在真实数据上暴露的 bug」——两个引擎对同一问题给出**不同答案**,根因是 4e-14 的浮点差。

---

## 实测数据

### 环境

| 项 | 值 |
| :--- | :--- |
| GPU | NVIDIA GB10 |
| 驱动 | 580.126.09 |
| 架构 | aarch64 · Linux 6.14.0-1015-nvidia |
| 内存 | 121 GB(可用 117 GB) |
| CPU | 20 逻辑核 |
| cuDF | 25.10.00 |
| pandas | 2.3.3(基线) |
| Python | 3.11.16 |

### GPU vs CPU(2000 万行 / 3.04 GB,同文件同命令)

| 操作 | GPU | CPU | 加速比 |
| :--- | ---: | ---: | ---: |
| `groupby` 分组聚合 | 2.60 s | 10.22 s | **3.93×** |
| `corr` 相关性矩阵 | 3.13 s | 11.56 s | **3.69×** |
| `outliers` IQR 异常值 | 3.37 s | 10.91 s | **3.24×** |
| `summary` 分位数/标准差 | 5.28 s | 16.16 s | **2.62×** |
| `auto` 综合概览 | 9.45 s | 20.80 s | **2.20×** |

### 多规模端到端基准

| 规模 | 文件 | 读取 | groupby | 端到端 |
| :--- | ---: | ---: | ---: | ---: |
| 3M | 564 MB | 8.30× | 8.25× | 6.86× |
| 10M | 1.88 GB | 8.30× | 11.89× | 7.16× |
| 30M | 5.67 GB | 7.22× | 12.44× | 6.45× |

两个引擎的数值差 `rel_diff = 0.00e+00`(完全一致)。

### ⚠️ 但有多少真的是 GPU 的功劳?

这是**最容易被夸大**的地方,所以单独测了归因:

| 场景 | 加速比 | 说明 |
| :--- | ---: | :--- |
| 冷缓存读取 | 6.09× ~ 6.35× | 含并行 I/O(非 GPU 独有) |
| 热缓存读取 | 7.93× ~ 8.05× | 含并行 I/O(非 GPU 独有) |
| **纯内存计算** | **2.86× ~ 3.09×** | **这才是 GPU 计算本身** |

**结论:冷读加速中约 47~49% 来自计算,其余来自并行解析与核数差。**
本项目如实报告这一点,而不是拿 6.45× 当卖点。

### 只在真实数据上暴露的 bug 🐛

在 UCI 家庭用电数据(2,075,259 行)上,`Voltage` 列的 IQR 异常值数量:

```
pandas → 51,067        cuDF → 50,763        差 304
```

用 3 个诊断脚本逐层排查后定位:Q1/Q3 在所有引擎、所有插值方法下**完全相同**,我的插值假设是错的。真因是**浮点累加**——pandas 算出下界 `233.14000000000004`,cuDF 算出 `233.14`,相差 4e-14,而数据里**恰好有 `233.14` 这个值**,裸 `s < low` 对平局的处理因此不同。

修复:引入 `1e-9` 相对容差,并显式报告 `fence_ties_excluded`。修复后 7 列全部一致:

```
Voltage                 50,763   50,763    OK    fence_ties_excluded=304
Sub_metering_1         169,105  169,105    OK    ties=1,880,175 (IQR=0)
```

---

## Agent 应用结构

### 循环骨架

```python
messages = [system, user]
for _ in range(MAX_TOOL_ROUNDS):          # 上限 6 轮，防止死循环
    resp = client.chat.completions.create(model=MODEL, messages=messages,
                                          tools=skill_definitions, temperature=0)
    if not resp.choices[0].message.tool_calls:
        break                             # 模型认为可以回答了
    for call in resp.choices[0].message.tool_calls:
        result = execute_tool(call)       # 永不抛异常，失败也是 JSON
        messages.append(tool_result(result))
```

### 健壮性:演示而非声称

| 异常输入 | Agent 行为 |
| :--- | :--- |
| 文件不存在 | 明确报告 `文件不存在: <path>`,**不猜测、不编造** |
| 列名错误 | 改调 `profile` 拿到真实列名,再重试一次(3 轮内自愈) |
| 中文列名 `销售额` | 同上,自愈链完整 |
| 数据里有 1,440 个唯一日期 | 不会把每个日期当一组,避免输出撑爆 token |
| 没有 GPU | 回退 pandas,并声明 `engine="pandas"` 与原因 |
| API 报错 / 无 tool_calls | 捕获并降级为文字回答,不崩溃 |

### 每个数字都被独立复算

Agent 报出的数值**不是自己说了算**。`verify_*.py` 用 pandas(与 cuDF 独立的代码路径)从零重算并对比:

```
column           agent  independent   match     pct
----------------------------------------------------
revenue        518,394      518,394      OK  10.37%
quantity             0            0      OK   0.00%

  revenue mean      = 1073.83      (agent said 1073.83)  OK
  revenue median    = 403.67       (agent said 403.67)   OK
  quantity min      = 1            (agent said 1)        OK
  quantity max      = 499          (agent said 499)      OK
  quantity median   = 250.0        (agent said 250)      OK

mismatches: 0
PASS: every recorded claim reproduced independently.
```

### 已知局限

- Agent 自主性**偏浅**:单轮选工具 + 偶尔一步纠错,尚无多步规划型任务
- 只有 CSV 路径有充分测试,Parquet/Excel 走的是通用读取路径
- 加速比对比会额外跑一次 CPU,首次查询增加 15~25 秒(用 `--prewarm` 规避)
- 概念性回答质量受模型本身限制,与 Skill 无关

### 输出契约(模型实际解析的内容)

引擎输出**元数据在顶层、操作数据嵌在自己的键下**,Agent 据此判断有没有真的用上 GPU:

```jsonc
{
  "ok": true,
  "op": "groupby",
  "engine": "cudf",              // cuDF 成功
  "engine_version": "25.10.00",
  "gpu": "NVIDIA GB10",
  "accelerated": true,           // 真的走了 GPU
  "fallback_reason": null,       // 降级时这里说明原因
  "total_seconds": 2.63,         // 端到端（含读取）
  "groupby": {
    "by": "region",
    "agg": "revenue:sum,mean",
    "rows_scanned": 20000000,    // 注意：行数在操作块内部
    "compute_seconds": 1.91,     // 纯计算（不含读取）
    "groups": [ /* ... */ ]
  }
}
```

**退出码**(实测,非推测):

| 退出码 | 触发条件 | 实测场景 |
| ---: | :--- | :--- |
| `0` | 分析跑完,结果在 `ok` 字段里 | 成功;也可出现在「文件能读但没有可用列」的情况 |
| `2` | **输入问题**——模型可以自己修正 | 文件不存在 · `by` 列名不存在 · `columns` 列名不存在 |
| `3` | **引擎内部异常**,非预期 | 未被前两类覆盖的异常(保留给真正的 bug) |

`2` 与 `3` 的区别很实用:前者表示「模型把参数搞错了,重试一次就行」,后者表示「引擎出问题了」。**任何失败都不抛异常**,统一返回 `{"ok": false, "error": "..."}`。

---

## 仓库结构

```
├── agent/                          Agent 应用（比赛主体）
│   ├── agent_main.py               tool-calling 循环
│   ├── skills.py                   Skill 注册 + GPU/CPU 实测对比 + 输出压缩
│   ├── demo_script.py              7 步演示（--prewarm 预热）
│   ├── run_criteria_tests.sh       7 项评审用例
│   ├── gpu_vs_cpu_demo.py          两引擎并排对照
│   ├── env_stepfun.sh              非交互 shell 的 key 加载
│   ├── verify_*.py                 独立复算 Agent 报的数字
│   ├── diagnose_iqr*.py            IQR 浮点 bug 的根因诊断
│   └── probe_tool_calling.py       验证模型支持 function calling
│
├── skills/cudf-analytics/          Skill 本体
│   ├── SKILL.md                    触发条件 + 工作流（中英双语触发词）
│   └── scripts/
│       ├── gpu_analytics.py        核心引擎（cuDF / pandas 双路径）
│       ├── smoke_test.py           59 项自检 + GPU/CPU 数值一致性
│       ├── attribution_test.py     归因：I/O vs 计算
│       ├── memory_ceiling_test.py  多规模压力测试
│       └── benchmark_cpu_vs_gpu.py 基准测试
│
├── benchmark/                      GB10 实测证据与原始日志
├── INNOVATION_OPTIONS.md           5 个创新方案评估
└── LICENSE · requirements.txt
```

### Skill 是怎么被触发的

`SKILL.md` 的 `description` 字段就是触发器,写成「用户会怎么问 / 什么情况下必须调用」,并明确列出**不该触发**的情况。

> **Discovery note:** DSH 只在 `.dsh/skills`、`.agents/skills`、`$DSH_HOME/skills` 下自动发现 Skill,且只认**一层深**的 `<name>/SKILL.md`。因此 `skills/cudf-analytics/SKILL.md` **不会**被 DSH 自动加载;需要把它复制或挂载到上述位置。如果像本项目这样用 `skills.py` 把工具注册给模型(function calling),则不需要这一步。

---

## 跑起来

### 没有 GPU 也能验(评委从这里开始)

GPU 路径需要 GB10,但**整条流水线在没有 GPU 时也能跑**——引擎自动回退 pandas,并如实报告 `engine: "pandas"` 和原因。因此分析正确性、Agent 循环、异常处理都可以在普通机器上验证:

```bash
pip install -r requirements.txt
python skills/cudf-analytics/scripts/smoke_test.py        # 59 项断言
python skills/cudf-analytics/scripts/gpu_analytics.py --input <任意.csv> --op auto
```

应当看到 `"accelerated": false` 和解释 cuDF 不可用的 `fallback_reason`。
**这条路径上我们不主张任何加速**——这正是诚实报告的意义。

```bash
export STEPFUN_API_KEY=<你的 key>
cd agent && python agent_main.py --ask "分析 /path/to/data.csv 的异常值"
```

### 在 GB10 上(完整 GPU 路径)

```bash
ssh -p <port> <user>@<host>
source ~/.bashrc                        # 提供 STEPFUN_API_KEY
conda activate rapids-cudf              # cuDF 25.10 / pandas 2.3.3 / py3.11

# 1) Skill 自检：59 项断言，含 GPU/CPU 数值一致性
cd skills/cudf-analytics && python scripts/smoke_test.py

# 2) Agent 单问
cd ../../agent && python agent_main.py --ask "分析 /path/to/data.csv 的异常值"

# 3) 脚本化演示（7 步，约 76 秒，每步显示实测加速比）
export DEMO_DATA=/path/to/sales_demo.csv
python demo_script.py --prewarm && python demo_script.py

# 4) 评审用例套件（7 项）
bash run_criteria_tests.sh
```

> `--prewarm` 很重要:首次查询某个问题时,Agent 会额外在 CPU 上跑一遍用于测量加速比,约需 15~25 秒。预热把这些对照结果写入 `.gpu_vs_cpu_cache.json`,现场每一步都能立即显示加速比——演示总时长从 **128 秒降至 76 秒**。

### 演示数据从哪来

仓库**不含**数据文件(多 GB)。用自带脚本生成:

```bash
python skills/cudf-analytics/scripts/memory_ceiling_test.py --sizes 20m --columns 8
```

⚠️ **演示务必用 2000 万行那份。** 500 万行只有 **1.4×**,2000 万行有 **3.9×**——数据规模不够会让人误以为 GPU 没用。

---

<div align="center">
<br>

**第三方组件** · RAPIDS cuDF (Apache-2.0) · pandas (BSD-3-Clause) · openai-python (Apache-2.0) · StepFun step-3.7-flash

均为依赖而非打包,仓库不含二进制

<br>

`MIT License` · 详见 [LICENSE](LICENSE)

</div>