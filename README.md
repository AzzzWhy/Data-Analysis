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
| 🔁 **多步会话** | 数据常驻显存,后续每步 **0.03 秒**,整条工作流快 **7.9×** |
| 🧭 **规划托管** | 目标分解成计划由**系统记账**,进度不靠模型记性 |
| 📦 **交付物** | 出 `.md` 报告 + `.svg` 图表 + CSV,不只是聊天里的文字 |
| 📊 **当场证明** | 每次回答都附上**这一问实测**的 GPU/CPU 耗时对比 |
| 🛡️ **诚实降级** | 没有 GPU 就自动回退 pandas,并如实声明 `engine="pandas"` |

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
| **① 技能调用能力**<br><sub>自主判断何时调用 + 参数正确</sub> | 模型从 4 个 Skill 中自主选择;未给文件时先调 `list_datasets` 发现数据;概念性问题**不调**工具;列名报错后自动改调 `profile` 学真实列名再重试;**多步需求自主切换到会话式 Skill 并传 `goal` 领取计划** | `run_criteria_tests.sh` **11 项通过**;断言打在工具调用轨迹上而非回答文本上 |
| **② 任务完成度**<br><sub>自然语言 → 真实结果</sub> | 6 种操作全部返回真实统计量;回答里给中文结论、排名、表格与关键发现;**并写出可带走的报告与图表文件** | `smoke_test.py` **59 项断言**;`verify_*.py` 独立复算 |
| **③ 创新性** | ①显存常驻会话让多步下钻成本降一个数量级 ②**规划托管**:目标→计划由系统记账,不让模型每轮重新分类 ③**零依赖 SVG 交付物** ④加速比在对话内当场测量 | 实测 6 步分析 1.86 秒 vs CPU 37.9 秒 |
| **④ 代码可用性**<br><sub>可部署 / 健壮 / 异常处理</sub> | 引擎自动降级;三个工具**永不抛异常**;显存不足**载入前拒绝**并给出可执行建议;文件改动后拒绝用旧数据作答;双列分组报错时明确告知"只能一列";无 GPU、文件不存在、列名错误都能优雅处理;**收尾强制释放显存** | 无 GPU 路径可跑;`plan_test.py` 56 项 + `session_*_test.py` 41 项断言 |
| **⑤ 演示效果**<br><sub>端到端对话流畅</sub> | 9 环节脚本化演示,**176.8 秒**跑完,每步显示加速比;含**交付物**与**多步下钻**两个高潮环节;`--prewarm` 预热保证现场零等待 | `demo_script.py` |

### 创新点在哪

坦白说,"用 cuDF 加速数据分析"本身门槛极低。所以我们把创新放在**可被验证的诚实性**上:

**① 加速比在对话内当场测量,而不是引用一张事先准备好的表。**
评委看到的不是"我曾在别处测到 6.45×",而是"这一问、这份文件、这条命令,GPU 2.63 秒,CPU 10.22 秒"。

**② 主动报告对自己不利的数据。**
归因实验证明端到端加速里**只有约 3× 来自 GPU 计算**,其余主要是并行 CSV 解析(polars 也能做到);pandas 基线只用 1 核而机器有 20 核。**这些限定条件写在 README 和系统提示词里,由模型主动向用户复述。**

**③ 真实数据上发现的正确性 bug。**
见下方「只在真实数据上暴露的 bug」——两个引擎对同一问题给出**不同答案**,根因是 4e-14 的浮点差。

**④ 把"规划"做成系统的状态,而不是模型每轮的即兴发挥。**
同一句多步问题连跑三次曾给出三条不同路径(一次中途放弃会话、一次耗尽轮数、一次半途换工具)。
现在目标→计划由**纯函数**从固定目录选出,进度由**执行器**记账,并允许记录计划外步骤。
这让多步分析从"看运气"变成"可复现",而不是靠再往提示词里加一条规则。
详见下方「规划托管」。

**⑤ 交付物零依赖。**
图表是手写 SVG,只 import 标准库。评委不装任何东西就能打开;
而且报告里明写"图表渲染没有 GPU 加速"——不拿画柱子冒充算力。

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

### 多步分析为什么需要常驻显存

单次查询快 3 倍不算什么。真正的问题是:**数据分析从来不是一步**——"找出异常"之后必然要"定位来源"、"量化影响"、"对比分组"。

而无状态路径每问一次就要重新读盘一次:

| 做法(2000 万行 / 3.04 GB) | 5 步全量分析 |
| :--- | ---: |
| CPU,每步重读 | **49.7 s** |
| GPU,每步重读 | 10.8 s |
| **GPU 常驻显存(`dataset_session`)** | **1.5 s** |

这就是 `dataset_session` 存在的理由:它把"单次查询快一点"变成"**多步工作流快一个数量级**",从而让"下钻"从奢侈变成默认动作。

实测会话内部(同一份数据):

```
open  载入 2000 万行        1.85 s    (常驻 1,692 MB 显存)
      profile               0.05 s
      outliers              0.42 s
      groupby by region     0.04 s
      groupby by category   0.03 s
      corr                  0.53 s
close 会话总计 9 步           8.46 s
      vs 传统「CPU 每步重读」   约 77 s     → 19.2×
```

演示脚本里那条会话下钻(带预热、可复现)的实测范围是 **7.9× ~ 20.4×**,取决于那一步里模型实际跑了几个操作。即便取最保守的 7.9×,也比单次查询的 3~4× 高一个量级——因为它省的是"每一步都要重读 3 GB"这件事。

**为什么这必须是 GPU skill 而不是纯调度技巧:**

- 121 GB 显存装得下 2000 万行,CPU 内存方案在多数机器上会 OOM
- 它**也真的会不够用**。所以 `open` 会在载入**之前**检查显存并拒绝:
  ```
  显存不足，已拒绝载入：该文件约 2.83GB，预计需要 12.5GB 空闲显存，当前只有 4.1GB。
  建议改用 analyze_dataset 单次分析，或先用 columns 参数只读需要的列。
  ```
- 同时最多 4 个会话,超出即拒绝,而不是让显存静默耗尽

**三条守卫,都是踩过坑才加的:**

| 守卫 | 防的问题 |
| :--- | :--- |
| **文件身份校验**(路径+大小+mtime) | 会话期间文件被改,后续步骤会用**旧快照**算出错误答案 → 拒绝作答并提示重新 open |
| **"没有重读"的可证伪断言** | 用「同一操作连跑两次耗时不变」+「累计耗时 = 载入 + 各步之和」证明,而不是靠阈值猜 |
| **收尾强制释放** | 实测模型**会忘记 close**(留下 1.7 GB);`run()` 用 `finally` 兜底释放,不依赖提示词 |

### 交付物:不只回答,还交东西

赛题要的是"从**会回答**升级为**能完成任务**"。一个只会在聊天框里输出文字的 Agent,严格说只完成了前一半。

`export_deliverables` 在 GPU 上做完全量分析后,写出三样可以直接拿走的文件:

| 文件 | 内容 |
| :--- | :--- |
| `report.md` | Markdown 报告:数据来源、行数、引擎、统计表、异常表、相关表、内嵌图表 |
| `*.svg` | 柱状/折线/热力图,从聚合结果生成 |
| `*.csv` `result.json` | 底层数据,便于复用 |

**为什么手写 SVG 而不用 matplotlib:**

1. **零依赖** —— GB10 上没装 matplotlib,而 aarch64 装它要拖一整个 wheel 栈。这个文件只 import 标准库。
2. **渲染确定** —— 没有字体发现、没有 backend、没有 DPI 差异。同一个文件在评审判电脑上、浏览器里、转成 PDF 后长得一样。
3. **可 diff** —— SVG 是文本,图表改动能在 commit 里看见。

**一句必须说清楚的话(也写进了报告本身):**

> 图表**渲染**没有 GPU 加速。GPU 加速的是喂给图表的那份聚合计算。为"画 5 根柱子"报一个加速比是没有意义的。

报告里对"纯粹标识列"会自动剔除(例如自增 `row_id`,均值 1,000 万,会把其他列在坐标轴上压成一条线——这是实测踩到的)。判断是透明的:列名像 ID,或者取值恰好是从 0 开始的等差数列。

### 规划托管:让规划成为状态,而不是提示

多步分析原本靠模型一步步"自己决定下一步看什么"。能用,但**不可复现**:同一个问题连跑三次出现三条不同路径,其中一次耗尽了轮数、中途放弃会话。演示时这是风险,评审时看起来像运气。

所以计划被做成**状态**:

```
dataset_session(operation="open", file_path=..., goal="找出 revenue 异常值的原因")
    ↓ 系统立刻返回一份针对该目标排好序的计划,并填好真实列名
  plan: {kind: "drill_down", total_steps: 5, next_step: 0,
         steps: [profile, outliers, groupby(region), groupby(category), corr],
         do_next: "operation=analyze, op=profile  # 先确认列名与类型"}
    ↓ 每执行一步,系统自动记账
  plan: {completed: 1, next_step: 1, ...}
```

三点设计取舍:

- **分类是纯函数。** 目标文本 → 策略,由关键词打分决定,**不让模型分类**。会让演示漂移的分类就是坏分类。实测同一目标连跑 50 次结果完全一致。
- **计划是可执行的,不是模板。** 分组步骤直接带上从数据里读出来的真实列名。一个只说"按分组看"而不说按哪一列的计划,是模板。
- **允许偏离。** 计划外的步骤会被记成 `off_plan` 而不是被拒绝——隐藏它会让进度报告说谎。计划引导,但不是牢笼。

实测效果:同一句"找出 revenue 的异常值并分析原因",托管前后差别很明显:

| | 轮数 | 结局 |
| :--- | :--- | :--- |
| 托管前 · 第 1 次 | 6(用满) | **中途放弃会话**,改用无状态工具 |
| 托管前 · 第 2 次 | 8(用满) | 走完 9 步,但**忘记 close**(靠兜底释放) |
| 托管前 · 第 3 次 | 3 | **耗尽轮数**,没得出答案 |
| **托管后** | **6** | **按计划走完 4 步 + close,零重试** |

演示脚本第⑨环节就是这个场景,实测 **session 1.861 s vs CPU 37.933 s = 20.4×**。

### 可复现性:这是脚本验证的,不是嘴上说的

"现在很稳定"这种话不该由我来断言。仓库里有 `agent/run_stability_check.sh`,把同一句多步问题连跑 N 次,
打印每次的**工具调用序列**,并检查三件事:

1. 全程用会话 skill,没有中途退回无状态路径
2. 会话被正确 close(没有泄漏显存)
3. 每次的操作顺序**符合计划的有序子序列**

```bash
bash run_stability_check.sh 3 /path/to/data.csv
```

**实测结果(GB10,2000 万行):**

| 问题 | 计划类型 | 卫生性(全程用会话 + 收尾) | 计划覆盖 |
| :--- | :--- | :---: | :--- |
| 相关性 + 分组一致性 | `relationships` | **3/3** | **3/3**,三次调用序列逐次完全相同 |
| revenue 异常值来源 | `drill_down` | **3/3** | **2/3**(一次只走了 3 步就收尾) |

"卫生性"指:**没有中途退回无状态路径、没有重复 open、会话被正确 close**。
这一项现在 6/6;修之前有一次跑满轮数、忘了 close,遗留了 1.7GB 显存。

**残留的真实变异性,我不打算靠继续堆提示词消除:**`drill_down` 有一次只覆盖了 3 步
(profile→outliers→groupby)就收尾了。答案是合法的,但不如计划完整。这是模型自身的
判断波动,不是机制缺陷——机制保证了"不迷路、不泄漏、不卡死",**不保证"每次都把 5 步走满"**。

**这轮验证挖出四个真 bug,全都是脚本逼出来的:**

| # | 问题 | 根因 | 修法 |
| :--- | :--- | :--- | :--- |
| 1 | `relationships` 只有 1/3 成功 | 模型请求 `agg="revenue:corr"`,而**按组算相关性 groupby 表达不了**;报错只列合法函数名,它以为自己拼错了,连试三次后耗尽轮数给出**失败回答** | 引擎按意图给替代方案(`corr`→`op='corr'`、`var`→`std`、分位数→`op='summary'`);提示词补上"会话内报错的处理顺序" |
| 2 | 一次运行**忘了 close**,遗留 1.7GB | 多跑了一个下钻维度,轮数预算用光 | `MAX_TOOL_ROUNDS` 8→10(实测需要,不是拍脑袋) |
| 3 | 一次运行**开了两个会话** | 模型丢了 session_id 就重新 open,同一文件被载入两遍(约 3.4GB) | 同一文件**复用已有会话**并明确告知 session_id |
| 4 | 我的检查器把正确行为判成失败 | 先要求"三次序列字节级相同"(多探索一个维度被误判),再要求"顺序严格符合计划"(合法调序被误判);还把**早期的交叉验证**当成"中途退回无状态路径" | 判据改为:**卫生性门禁 + 覆盖度门禁**,顺序和额外步骤只作信息输出 |

第 4 条特别值得说:**手写断言比代码更容易想错。** 一个判据写歪了会让整套验证失去意义
(要么永远失败、要么永远通过),而这两种情况都不会自己报错。

### 交给评委自己跑

```bash
# 测试套件(计划层不需要 GPU)
python skills/cudf-analytics/scripts/plan_test.py            # 56 项
python skills/cudf-analytics/scripts/smoke_test.py           # 59 项
bash   agent/run_criteria_tests.sh                           # 11 项评审用例
bash   agent/run_stability_check.sh 3 <你的数据.csv>          # 可复现性
```

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

- **规划来自固定目录,不是即时推理。** 四种策略(drill_down / compare_groups /
  data_quality / relationships)覆盖常见问法;目录之外的复杂目标会回落到默认形状,
  而不是现场推导出一个新策略。这是**为了可复现性**付的代价,不是能力上限。
- **计划不强制,步数会浮动。** 模型可以走计划外的步骤(会被标成 `off_plan`),也可能像实测那样
  只覆盖部分步骤就收尾。机制保证"进度不丢、不泄漏、不卡死",**不保证每次都把计划走满**。
- 图表种类限于柱状/折线/散点/直方图/热力图,没有交互式仪表盘。
- 只有 CSV 路径有充分测试,Parquet/Excel 走的是通用读取路径。
- 交付物写的是本机目录,**没有上传/分享能力**,也不能把多个数据集汇总进同一份报告。
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
    "groups": 5,                 // 分组数量（整数，不是数据）
    "sorted_by": "revenue__sum",
    "top_k": [ /* 实际的分组结果行在这里，最多 top_k 条 */ ]
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
│   ├── demo_script.py              9 环节演示（--prewarm 预热，含交付物与多步下钻）
│   ├── run_criteria_tests.sh       11 项评审用例
│   ├── run_stability_check.sh      可复现性：同一问题连跑 N 次，校验会话卫生与计划覆盖
│   ├── gpu_vs_cpu_demo.py          两引擎并排对照
│   ├── session_skill_test.py       会话工具层断言
│   ├── env_stepfun.sh              非交互 shell 的 key 加载
│   ├── verify_*.py                 独立复算 Agent 报的数字
│   ├── diagnose_iqr*.py            IQR 浮点 bug 的根因诊断
│   └── probe_tool_calling.py       验证模型支持 function calling
│
├── skills/cudf-analytics/          Skill 本体
│   ├── SKILL.md                    触发条件 + 工作流（中英双语触发词）
│   └── scripts/
│       ├── gpu_analytics.py        核心引擎（cuDF / pandas 双路径，无状态）
│       ├── gpu_session.py          常驻会话 worker（数据驻留显存，多步复用）
│       ├── analysis_plan.py        计划目录 + 进度记账（纯函数分类，可测试）
│       ├── make_deliverables.py    零依赖 SVG 图表 + Markdown 报告生成
│       ├── smoke_test.py           59 项自检 + GPU/CPU 数值一致性
│       ├── plan_test.py            56 项计划层断言
│       ├── session_worker_test.py  worker 协议与守卫测试
│       ├── attribution_test.py     归因：I/O vs 计算
│       ├── memory_ceiling_test.py  多规模压力测试
│       └── benchmark_cpu_vs_gpu.py 基准测试
│
├── benchmark/                      GB10 实测证据与原始日志
│   ├── benchmark_results.json      机器可读的实测数字
│   ├── benchmark_results.md        实测表格
│   ├── gb10_run.log                原始运行日志
│   ├── smoke/gb10_smoke_test.log   GB10 上的自检日志
│   └── charts/                     工具生成的图表样本（直接可看，无需装依赖）
│       ├── groupby_bar.svg         分组柱状图
│       ├── corr_heatmap.svg        相关性热力图
│       ├── outliers_bar.svg        异常值分布
│       └── summary_means.svg       列均值对比（已剔除标识列）
│
├── skill.md                        入口页（指向 SKILL.md 与 README）
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

# 4) 评审用例套件（11 项）
bash run_criteria_tests.sh

# 5) 计划层单测（不需要 GPU，56 项）
python skills/cudf-analytics/scripts/plan_test.py
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