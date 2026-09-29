# Workbench GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 spec 批准的浏览器工作台（`agent/gui.py` + `agent/gui/` 三件套），让它能在 GB10 节点上跑起来，操作者在本地 Windows 浏览器里通过 SSH 端口转发提问、看引擎决策与图表，用于录制演示视频。

**Architecture:** 单进程内嵌 HTTP/SSE 服务器（仅 stdlib），持有**一个** `Agent` 实例和一把 `agent_lock`，因为 session worker 活在模块级全局里、跨请求必须保活。前端是零构建的 ES2019 页面，所有引擎事实来自 `result_sink` 递出的 `execution_decision` 结构化对象，绝不从日志散文里反推。传输层用 SSE，浏览器经 `ssh -L 8765:127.0.0.1:8765` 访问。

**Tech Stack:** Python 3 stdlib（`http.server.ThreadingHTTPServer` / `json` / `threading` / `queue` / `pathlib` / `mimetypes` / `argparse`）、原生 ES2019 + 手写 SVG、无新依赖。

**Spec:** `Data-Analysis/docs/superpowers/specs/2026-09-27-workbench-gui-design.md`（rev 2）。**先读下面的「Spec 修正」再读 spec** —— 本计划对 spec 有 9 处偏离，全部基于代码实测，理由在括号里给了行号。

---

## ⚠️ 开发位置：本地写、本地测，只在最后一步上节点

这是本计划最重要的执行约定，不要违反。

`gui_test.py` 按 spec §3.4 必须**无网络、无 GPU、无 API 调用**，所以除 Task 1 和 Task 12 外的全部工作都在 Windows 上完成：

- 迭代速度：一次 SSH 往返改一行代码的成本，约为本地直接跑的 20 倍，而你（执行者）**不能直连远端**，每个命令都要用户手粘。
- 一致性：Windows 和 GB10 跑同一份纯 stdlib 代码。GUI 层不碰 cuDF，所有 GPU 相关事实都由 worker 报告、前端只渲染，因此本地测过的渲染逻辑在节点上行为相同。
- 唯一不可移交给本地的：Task 12（真机验收 + 录制）。

**代码从本地到节点用 git bundle**（见 Task 1 Step 6），不 scp 目录树 —— 一个文件、带历史、增量可重发。

---

## Global Constraints

逐字来自 spec，每个 Task 都隐含遵守：

- **零新依赖。** `gui.py` 只用 stdlib；`requirements-gui.txt` 只放注释。（spec §9，评审时 FastAPI 方案已被明确否决）
- **不改这些文件：** `gpu_analytics.py`、`gpu_session.py`、`cost_model.py`、`parquet_cache.py`、`make_deliverables.py`、`build_html_report.py`。（spec §2）
- **不改 `tui_app.py`，逐字节兼容。**（spec §2、§5.1）
- **GUI 缺失必须优雅降级。** `gui.py` 只在 `--gui` 分支内导入；服务起不来不得影响 CLI / TUI / 引擎。仓库已有此约定（提交 `7a73f72`、`agent_main.py:648-652`）。（spec §9）
- **默认只绑 `127.0.0.1`。** 绑其他接口需 `GPU_GUI_ALLOW_REMOTE=1`，未设置时启动要打印拒绝理由。（spec §8）
- **不在前端算任何东西。** 不实现路由、成本预测、内存估算；引擎没给结构化字段就**什么都不显示**，绝不编造。`estimate.peak_memory_mb` 恒为 `null`（`gpu_analytics.py:343`，无条件），不要给它画条。（spec §2、§6.2）
- **密钥永不回显。** `APIConfig.api_key` 是 `field(repr=False)`（`api_config.py:15`），不得整体序列化 dataclass，响应字典手工构造；密钥不走 query string、不进 SSE 帧。（spec §8）
- **配色逐字取自 `tui_app.py:43-84`。** 令牌见 Task 9 Step 2。
- **CSP 不含 `'unsafe-inline'`。** CSS 必须独立成文件；`style-src 'self'; script-src 'self'`。（spec §4.2、§6.1、§10.8）
- **提交频率：** 每个 Task 一次提交，消息用仓库现有风格 —— 短句、说清了*为什么*，不要 conventional commits 前缀（`git log --oneline -8` 对齐）。

---

## Spec 修正（照 spec 写会错的地方，全部已用代码核实）

执行每个 Task 前重看这张表。**不要**"按 spec 原文实现然后被发现不符"。

| # | spec 位置 | spec 说的 | 代码实际 | 本计划怎么做 |
|---|---|---|---|---|
| **C1** | §6.3 chip 表、§3.2 验收 2 | `mode == "resident reuse"` | **该取值不存在。** `execution_decision_record` 的 `mode` 合法值只有 `"auto"/"force_cpu"/"force_gpu"/"resident"/"reuse"`；`"resident_reuse"` 是 **policy** 值（`gpu_session.py:293,609`） | 暖复用判据改为 `policy in ("resident_reuse","warm_cache")` **或** `observed.phase in ("session_reuse","warm_cache_hit")` |
| **C2** | §5.1 结尾（183-184 行） | `result_sink` 把 `routing_reason`/`fallback_reason`/`rows_scanned`/`total_seconds` 交给 GUI | **GUI 收到的是 agent 包装层信封**（`skills.py:989-998`）：`success,file,engine,accelerated,rows_scanned,seconds,execution_decision,result`。**顶层没有 `routing_reason`、没有 `fallback_reason`，`total_seconds` 被改名成 `seconds`** | 这三项一律从 `payload["execution_decision"]` 里取；耗时字段用 `seconds` |
| **C3** | §5.2、§14 步 3 | 新增 `release_all_sessions_and_cache()` | **该函数已存在**（`skills.py:781-804`），带 before/after 回读，目前零调用者 | Task 6 只做"接线 + 超时保护"，不重写 |
| **C4** | §5.1、§14 步 2 | 新增 `result_sink` | **工作区里已实现但未提交**（`agent_main.py` 有 44+/9− 未提交 diff，含 sink 异常保护和 `summarize_tool_result` 复用 parsed） | Task 2 = 补测试 #6 + 提交，不重写 |
| **C5** | §6.1 未提及 | — | `_worker_call(payload, timeout=1800.0)` 的 **`timeout` 形参存在但函数体从未使用**（`skills.py:676-699`，`readline()` 无超时）。worker 卡死时它会**永久阻塞且持着 `_worker_lock`** | 「释放会话」必须丢到工作线程 + 自己带 `join(timeout)`，见 Task 6。同步调会同时挂死 GUI 和下一次分析 |
| **C6** | §7 卡片轮询 | 卡片"如实报告 worker" | **`do_list` 会先调 `_prune_cache()`**（`gpu_session.py:718`）—— `list` 不是只读，轮询本身会触发 TTL 过期和字节驱逐，改变下次数字 | 不轮询。只在 `tool_result` / `release` 之后刷新卡片（Task 9 Step 5），并把这个副作用写进注释 |
| **C7** | §7 五态表 | 卡片要显示 `retained`（含是哪个文件的帧）和 `stale` | **两个都报不出来。** `list` 的保留帧只有聚合值 `warm_cache_count` + `warm_cache_mb`，**没有条目列表**（`gpu_session.py:717-738`）；stale 由 worker 静默 `pop`，**无任何响应字段**（`:170-173`、`:272-278`） | 砍掉 `stale` 态。`retained` 只渲染成"N 个保留帧 / M MB"，**不写文件名**（写了就是幻觉）。stale 只在 `analyze` 返回 "The file changed while this session was open" 时如实转述 |
| **C8** | §6.4 图元预算 | 只用 `rect/line/text/circle/polyline` | mockup **越界两处**：`frame()` 给每张图套 `<g font-family>`、`lineChart()` 用 `<path d="M…Z">` 画面积填充（`workbench.html:477,481`） | 保留 `<g>`（仅作字体容器，改从 CSS 继承）、**删掉面积 `<path>`**，面积改用半透明 `<polyline>` 或不要。两图元预算不放宽 |
| **C9** | §8 禁止 innerHTML | — | mockup 的 `t()` 用 `innerHTML`（为了让 `welcome` 带 `<b>`），而 `answer` 是模型散文 | `t()` 只用于**静态 chrome 文案**；`answer` 的 Markdown 走 escape-first（照 `build_html_report.py:39` 的 `_inline` 模式），`tool_result` 值一律 `textContent` |
| **C10** | §5.2 用 `close` 返回 | — | `close` 响应的 `cached` 字段**全关时是 int、单关时是 bool**（`gpu_session.py:752` vs `:764`） | 按 `release_all_sessions_and_cache()` 的封装后字段名读（`closed`/`warm_frames_dropped`/`sessions_after`/`warm_frames_after`），不碰原始 `detail` |
| **C11** | 通用 | — | 引擎层信封成功标志叫 **`ok`**（`gpu_analytics.py:1122`），agent 包装层叫 **`success`**（`skills.py:990`） | GUI 只消费包装层 → 判 `success` |

**另外两处 spec 已过期但不影响正确性**（写注释时别抄错行号）：`§5.2` 引 `gpu_session.py:727-738` 实为 `:741-752`；`§4.1` 引 `skills.py:659` 的 atexit 注册实际在 `_worker_start` 内部（`:659`），意味着**每次重启 worker 都再注册一个钩子** —— 这是个已存在的瑕疵，本计划不修（超出范围），但在 Task 6 注释里留痕。

---

## 前置状态（已核实，不必重查）

- 分支 `feat/workbench-gui`，HEAD `7df1e84`。**`origin/main`(`7d2ef8e`) 已是 HEAD 祖先** → spec §12 假设 7 要求的 rebase **已完成**。
- 工作区有且仅有一处未提交改动：`agent/agent_main.py`（就是 C4 的 `result_sink`）。
- **⚠️ 并发风险：** 本仓库在会话期间前进了 3 个提交（17:14→17:18，作者 `NoFindChang`）。开始每个 Task 前先 `git status --short`；若发现非本计划的改动，停下来问用户，不要覆盖。
- 引擎脚本在 `skills/cudf-analytics/scripts/`（**不在** `agent/`）。
- 演示数据：`_data_search_dirs()` 的搜索顺序是 `DEMO_DATA_DIR` → cwd → 仓库父目录 → `~` → `/data`（`skills.py:1038-1055`）。提交 `13154a0` 把家目录的**列举**深度压到 0 层，含义是：**数据集放在 `/home/Developer` 正下面可被发现，放子目录里就列不出来**。

## File Structure

| 动作 | 路径 | 职责（一个文件一件事） |
|---|---|---|
| Create | `agent/gui.py` | HTTP 路由 + SSE 编码 + 任务缓冲 + artifact allow-list。无分析逻辑、无自己的会话 TTL |
| Create | `agent/gui/index.html` | 只有标记，结构对齐已批准的 mockup 骨架 |
| Create | `agent/gui/style.css` | 令牌 + 布局。独立文件是 CSP 不需要 `'unsafe-inline'` 的前提 |
| Create | `agent/gui/app.js` | SSE 客户端 + DOM 渲染 + SVG 图表构造器 + `I18N` 表 |
| Create | `agent/gui_test.py` | 无头测试，`FakeAgent` + `urllib.request` 打临时端口 |
| Create | `requirements-gui.txt` | 只有注释，声明"可选界面层，后端未变" |
| Modify | `agent/agent_main.py` | 提交 C4 的 `result_sink`；`main()` 加 `--gui` / `--gui-port` |
| Modify | `README.md`、`docs/USAGE.md`、`skill.md` | 文档与实现同一提交 |

`gui.py` 不拆：260 行、四个协作紧密的职责，与 `tui_app.py`（427 行单文件）的既有粒度一致，拆开反而制造跨文件的循环引用。

---

## Task 1: 环境闸门 —— 确认节点能承载演示

不改代码。这个 Task 的产物是**一个决策**，它决定 Task 12 走哪条路，也可能直接推翻"录视频"的可行性。用户会在远端粘贴命令 —— 执行者**只输出命令块**，等用户贴回输出。

**Files:** 无（只产出结论）

**Interfaces:**
- Produces: 一份填好的环境清单，写进本文件末尾「Task 1 实测结果」小节；以及 `LLM 可用性` 的三选一结论

- [ ] **Step 1: 把下面这块发给用户，让他在已连上的远端终端里粘贴执行**

```bash
# 1. 我是谁、在哪、能写什么
id -un; pwd; echo "HOME=$HOME"; umask
# 2. 机器与 GPU
uname -m; nproc; free -g | head -2
nvidia-smi --query-gpu=name,memory.total --format=csv 2>&1 | head -3
# 3. Python 与 cuDF
python3 -V; python3 -c "import cudf;print('cudf',cudf.__version__)" 2>&1 | tail -1
# 4. 工具链
tmux -V 2>&1; git --version; which rsync
# 5. ★ 出网能力（决定演示能不能真跑 LLM）
for h in api.stepfun.com github.com pypi.org; do
  curl -sS -m 8 -o /dev/null -w "$h %{http_code}\n" "https://$h/" 2>&1 || echo "$h FAIL"
done
# 6. 已有凭据（只报有没有，绝不打印值）
env | grep -oiE "^(STEPFUN_API_KEY|GPU_API_KEY|OPENAI_API_KEY|GPU_API_BASE_URL|GPU_API_MODEL)=" | sed 's/=$//'
test -f ~/.config/gpu-data-analysis/connection.json && echo "connection.json 存在" || echo "无 connection.json"
# 7. 家目录里已有什么（决定代码放哪、数据放哪）
ls -la ~ | head -25
df -h "$HOME" | tail -1
```

- [ ] **Step 2: 根据第 5 项输出，判定 LLM 路径**

三选一，写进结论：

| 结果 | 判定 | 对演示的影响 |
|---|---|---|
| `api.stepfun.com 200/401/404`（任何非超时响应） | **A 真跑** | 最好。浏览器里问真问题、模型真调工具、cuDF 真跑。Task 12 直接录 |
| 超时 / DNS 失败，但 `pypi.org` 通 | **B 换端点** | 需要一个节点可达的 OpenAI 兼容端点。用 `GPU_API_BASE_URL` 指过去（`normalize_url` 允许 `http://localhost`，见 `api_config.py:38`） |
| 全部超时 | **C 回放模式** | **必须新增 `--gui-replay`：把一次真实运行录成 SSE 事件 JSONL，演示时重放。** 这是对本计划的范围追加，需用户签字（见 Step 3） |

- [ ] **Step 3: 如果落到 C，先向用户确认再动手**

不要默默实现回放模式 —— spec 里没有它，范围变更必须显式批准。要问的三句话：

1. 节点确实出不了网吗（还是只是 `curl` 被代理拦了）？
2. 有没有任何一台节点可达的机器能当 LLM 端点？
3. 若都没有，接受"演示录像是真实运行的一次录制回放"这个定位吗？

- [ ] **Step 4: 确认代码怎么到节点**

优先级：节点 `git clone` 公网仓库（若第 5 项 `github.com` 通）> git bundle 走 scp > 目录树 scp。

先看仓库体积，bundle 才不会撞上"禁传超 1GB"：

```bash
# 本地 Windows 执行
cd "D:/Documents/Project/HKSskill/Data-Analysis"
git count-objects -vH | grep -E "size-pack|count"
du -sh .git
```

预期 `.git` 在几十 MB 量级（`assets/` 若很大则用 `git bundle create hks.bundle feat/workbench-gui ^origin/main` 只发增量）。

- [ ] **Step 5: 把结论填进本文件末尾「Task 1 实测结果」**

- [ ] **Step 6: 生成本地第一个 bundle，验证传输链路可用**

不要等到 Task 12 才发现传不上去。

```bash
# 本地
git bundle create ../hks.bundle origin/main..feat/workbench-gui
ls -la ../hks.bundle
# 传给节点（代码体积，远低于 1GB 红线）
scp -P <node-port> ../hks.bundle Developer@<jump-host>:~/
# 节点上
git clone ~/hks.bundle ~/Data-Analysis && cd ~/Data-Analysis && git log --oneline -1
```

Expected: clone 成功、HEAD 指向 `7df1e84`。若 `scp` 被限制，改用 `sftp` 或让用户在 IDE 里拖拽上传。

---

## Task 2: 收口 `result_sink`（C4）

spec §14 步 2。实现已在工作区，缺的是**测试 #6 和一次提交**。这个 sink 是整个 GUI 的事实来源，没有可信的测试就不能往上盖东西。

**Files:**
- Modify: `agent/agent_main.py:449-460`（`__init__` 签名与注释，已改好，只核对）
- Modify: `agent/agent_main.py:574-590`（`_run_inner` 发射点，已改好，只核对）
- Test: `agent/gui_test.py`（新建，本 Task 只放 `FakeSinkRecorder` 与前两个用例，Task 3 起续用）

**Interfaces:**
- Produces: `Agent(client, verbose=True, event_sink=None, model=None, result_sink=None)`；`result_sink(name: str, parsed: dict, seconds: float) -> None`，仅在 `json.loads(result)` 成功**且结果是 dict** 时调用（`agent_main.py:584`）
- Produces: `summarize_tool_result(result_json: str, payload: dict | None = None) -> str` —— 新增可选 `payload` 参数，非 dict 一律回落"unparseable result"

- [ ] **Step 1: 核对未提交 diff 与 C4 描述一致**

```bash
git diff agent/agent_main.py | grep -c "result_sink"
```
Expected: ≥ 4。若为 0，说明并发的别人把它提交了 —— `git log --oneline -3` 确认后跳过实现只做测试。

- [ ] **Step 2: 写失败的测试 —— sink 抛异常不得打断分析**

spec §10 测试 #6。新建 `agent/gui_test.py`：

```python
"""Headless tests for the browser workbench. No network, no GPU, no API calls.

Mirrors agent/tui_test.py's shape on purpose (see commit 7a73f72): a self-running main that
prints SKIP rather than a pytest suite, because the optional-UI-layer convention in this
repo says an absent dependency must degrade, not error. gui.py is stdlib-only, so this file
should never need a SKIP path for its own imports -- if it does, a dependency crept in.
"""
import json
import threading
import urllib.request

FAILS = []


def check(name, cond, why=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{why}]" if cond or not why else f"   {why}"))
    if not cond:
        FAILS.append(name)


class RecordingSink:
    """Stands in for the GUI's result_sink: captures what the Agent hands a frontend."""

    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on
        self.lock = threading.Lock()

    def __call__(self, name, parsed, seconds):
        if self.raise_on and name == self.raise_on:
            raise RuntimeError("sink blew up on purpose")
        with self.lock:
            self.calls.append((name, parsed, seconds))
```

- [ ] **Step 3: 运行，确认它无法证明任何事**

```bash
python agent/gui_test.py
```
Expected: 只打印表头，零用例 —— 正常，Step 4 加驱动。

- [ ] **Step 4: 加一个不联网的 Agent 驱动**

真实 `Agent` 需要 OpenAI 客户端。用假客户端，这样 `execute_tool` 与 `result_sink` 都走真实代码路径：

```python
class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeCompletions:
    """Round 1 asks for one tool call, round 2 answers in prose."""

    def __init__(self, tool_name, raw_args, final_text):
        self._pending = [_FakeMessage(tool_calls=[_ToolCall("c1", tool_name, raw_args)]),
                         _FakeMessage(content=final_text)]

    def create(self, **kwargs):
        msg = self._pending.pop(0) if self._pending else _FakeMessage(content="done")
        return _FakeChoice(msg)


class _ToolCall:
    def __init__(self, cid, name, arguments):
        self.id, self.name, self.function = cid, name, _Fn(name, arguments)


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _FakeClient:
    api_key = "test-key-never-real"

    def __init__(self, tool_name, raw_args, final_text):
        self.chat = type("C", (), {"completions": _FakeCompletions(tool_name, raw_args, final_text)})()
        self.close = lambda: None
```

- [ ] **Step 5: 写两个真用例 —— sink 收到结构化 dict；sink 抛错仍能出答案**

```python
def sink_checks():
    from agent_main import Agent
    payload = {"success": True, "engine": "pandas", "rows_scanned": 8,
               "seconds": 0.42,
               "execution_decision": {"selected_backend": "pandas",
                                      "actual_backend": "pandas",
                                      "policy": "measured_file_size_crossover",
                                      "reason": "CPU by choice: test fixture",
                                      "fallback_reason": None,
                                      "mode": "auto", "observed": {}, "signals": {},
                                      "estimate": {"elapsed_seconds": None,
                                                   "peak_memory_mb": None,
                                                   "status": "not_calibrated"}}}
    sink = RecordingSink()
    agent = Agent(_FakeClient("analyze_dataset", json.dumps({}), "the answer"),
                  verbose=False, result_sink=sink)
    got = agent.run("q")
    check("sink fires with a parsed dict",
          len(sink.calls) == 1 and isinstance(sink.calls[0][1], dict) and sink.calls[0][0] == "analyze_dataset",
          str(sink.calls))
    check("answer survives the tool round", got == "the answer", repr(got))

    # The guard is the whole point: a frontend that fails must not cost the user their run.
    loud = RecordingSink(raise_on="analyze_dataset")
    agent2 = Agent(_FakeClient("analyze_dataset", json.dumps({}), "still answered"),
                   verbose=False, result_sink=loud)
    try:
        got2 = agent2.run("q")
        ok2 = got2 == "still answered"
    except Exception as exc:
        ok2, got2 = False, f"raised {type(exc).__name__}"
    check("raising sink cannot break a run (spec test #6)", ok2, str(got2))
```

在 `main()` 里调用 `sink_checks()`。

- [ ] **Step 6: 运行，确认通过**

```bash
python agent/gui_test.py
```
Expected: 4 个 `ok`，退出码 0。若 sink 用例不触发，检查 `execute_tool` 是否真被走到（假 tool_call 的 `function.arguments` 必须是合法 JSON 字符串）。

- [ ] **Step 7: 确认 `event_sink` 的文本契约没变**

spec §5.1 明确要求 TUI 不受影响，且 `tui_app.py:338-346` 至今靠那些文本串反推事实：

```bash
python agent/tui_test.py
```
Expected: 全 `ok` 或 `SKIP`（无 Textual 时）。必须与改动前一致。

- [ ] **Step 8: 提交**

```bash
git add agent/agent_main.py agent/gui_test.py
git commit -m "$(cat <<'EOF'
Hand the frontend tool results as data, not as log prose

The TUI recovers which engine ran by re-parsing a trace line. A reworded log
message would then silently change what a UI claims about the GPU, so the
browser workbench gets a structured sink instead, and a sink that throws is
made unable to break the run it observes.
EOF
)"
```

---

## Task 3: `gui.py` 最小骨架 —— 静态服务 + 安全边界

spec §14 步 4 的前半 + §8 的安全部分。做完这一步，浏览器能打开一个页面、能拿到 `/api/state`、能干净关掉，且**端口绑错接口会拒绝启动**。

**Files:**
- Create: `agent/gui.py`
- Create: `agent/gui/index.html`（占位版，Task 9 替换）
- Test: `agent/gui_test.py`（追加 `server_checks()`）

**Interfaces:**
- Produces: `gui.DEFAULT_PORT = 8765`
- Produces: `gui.make_server(agent, host="127.0.0.1", port=8765, static_dir=None) -> ThreadingHTTPServer` —— `port=0` 时监听临时端口（测试用），`server.workbench` 持有状态
- Produces: `class gui.Workbench` —— 属性 `agent` / `agent_lock` / `jobs` / `busy` / `out_dirs: dict[job_id, Path]`
- Produces: `gui.sse_format(name: str, payload: dict) -> str`

- [ ] **Step 1: 写失败的测试 —— 未授权绑定时拒绝启动**

```python
def server_checks():
    import gui
    from agent_main import Agent

    def make(**kw):
        return gui.make_server(Agent(None, verbose=False), **kw)

    # 127.0.0.1 is the only interface the ssh -L story needs; anything else is a
    # deliberate opt-out because the box sits on a public IP.
    import os
    os.environ.pop("GPU_GUI_ALLOW_REMOTE", None)
    try:
        make(host="0.0.0.0")
        bound = "created"
    except gui.RemoteBindRefused as exc:
        bound = str(exc)
    check("refuses to bind 0.0.0.0 without opting in",
          "GPU_GUI_ALLOW_REMOTE" in bound, bound)

    srv = make(port=0)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        code, hdr, body = get(base + "/")
        check("GET / serves the workbench", code == 200 and b"<html" in body.lower(), str(code))
        check("CSP carries no unsafe-inline (spec test #8)",
              "unsafe-inline" not in hdr.get("Content-Security-Policy", ""),
              hdr.get("Content-Security-Policy", "<missing>"))
        code, hdr, body = get(base + "/api/state")
        state = json.loads(body)
        check("/api/state reports the five documented keys",
              {"model", "host", "dataset", "session", "plan", "busy"} <= set(state),
              str(sorted(state)))
        check("state.busy is false while idle", state["busy"] is False, str(state["busy"]))
    finally:
        srv.shutdown()
        srv.server_close()
```

需要的 `get()` 助手（Task 2 文件里加上，后面复用）：

```python
def get(url, data=None, headers=None, method=None):
    """urllib without raising on 4xx/5xx -- the tests assert on status codes."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class _Threaded:
    """Run a server on a daemon thread so a test can hit it."""

    def __init__(self, server):
        self.server, self.thread = server, None

    def __enter__(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
```

- [ ] **Step 2: 运行，确认失败**

```bash
python agent/gui_test.py
```
Expected: `ModuleNotFoundError: No module named 'gui'`

- [ ] **Step 3: 写 `gui.py`**

```python
"""Optional browser workbench for the operator. Standard library only.

Why it is in-process: the session worker lives in module-level globals in skills.py, so a
per-request subprocess would spawn a fresh worker each turn and the warm frame would die with
it. That makes one Agent and one lock mandatory, and it makes the request threads take turns
rather than run concurrently -- a second question mid-run is a 409, matching the TUI's
`if self.busy: return`.

Nothing outside the --gui branch may import this module. A missing or unstartable workbench
must not affect the CLI, the TUI, or the engine.
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "gui"

DEFAULT_PORT = 8765
MAX_EVENTS_KEPT = 200          # a reload re-attaches and replays the tail
WORKER_TIMEOUT_SECONDS = 20.0  # see call_with_timeout -- the worker has none of its own

CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; img-src 'self' data:")


class RemoteBindRefused(RuntimeError):
    """Binding beyond loopback turns a local tool into a public service."""


def sse_format(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def call_with_timeout(fn, timeout: float = WORKER_TIMEOUT_SECONDS):
    """Run a worker call off-thread and give up on it, because the worker cannot.

    skills._worker_call takes a `timeout` argument it never applies: it blocks on readline()
    while holding the worker lock. A wedged worker would therefore hang this request *and*
    every later analysis, so the release button keeps its own deadline and says so plainly
    instead of pretending to have succeeded.
    """
    box: dict = {}

    def runner():
        try:
            box["value"] = fn()
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return {"error": f"worker did not answer within {timeout:g}s; it is likely wedged. "
                         "Restart the workbench process to release device memory."}
    if "error" in box:
        return {"error": box["error"]}
    return box.get("value") or {}
```

`Workbench` + handler（同一文件续写；路由按 spec §6.1 表格逐条落，`/api/ask`、`/api/events`、`/api/files`、`/artifact`、`/api/settings` 在 Task 4-8 里填，本 Task 先给 `501`）：

```python
class Workbench:
    def __init__(self, agent, static_dir: Path = _STATIC):
        self.agent = agent
        self.agent_lock = threading.Lock()
        self.static_dir = Path(static_dir)
        self.jobs: dict[str, "Job"] = {}
        self.busy = False
        self.dataset = None
        self.plan: list = []


class Job:
    """One run plus the events it emitted, so a disconnected SSE client loses nothing."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.events: deque = deque(maxlen=MAX_EVENTS_KEPT)
        self.done = False
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def emit(self, name: str, payload: dict):
        with self.changed:
            self.events.append((name, payload))
            self.changed.notify_all()

    def finish(self):
        with self.changed:
            self.done = True
            self.changed.notify_all()


class Handler(BaseHTTPRequestHandler):
    server_version = "GPUWorkbench/1"

    @property
    def wb(self) -> Workbench:
        return self.server.workbench

    def log_message(self, *args):
        pass                                   # the workbench writes no shared access log

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._static("index.html", "text/html; charset=utf-8")
        if path in ("/style.css", "/app.js"):
            return self._static(path[1:],
                                "text/css; charset=utf-8" if path.endswith(".css")
                                else "application/javascript; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(self.state_doc()))
        return self._not_found(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/shutdown":
            self._send(202, json.dumps({"ok": True}))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._not_found(path)

    def state_doc(self):
        return {"model": getattr(self.wb.agent, "model", ""),
                "host": os.environ.get("HOSTNAME", ""),
                "dataset": self.wb.dataset, "session": {}, "plan": self.wb.plan,
                "busy": self.wb.busy}

    def _static(self, rel, ctype):
        target = (self.wb.static_dir / rel).resolve()
        if not target.is_relative_to(self.wb.static_dir.resolve()) or not target.is_file():
            return self._not_found(rel)
        self._send(200, target.read_bytes(), ctype)

    def _not_found(self, what):
        self._send(404, json.dumps({"error": f"no route for {what}"}))


def make_server(agent, host: str = "127.0.0.1", port: int = DEFAULT_PORT, static_dir=None):
    if host not in ("127.0.0.1", "localhost", "::1") and os.environ.get("GPU_GUI_ALLOW_REMOTE") != "1":
        raise RemoteBindRefused(
            f"refusing to bind {host}: the workbench can read datasets and store an API key, "
            "so it listens on loopback only. Open it through `ssh -L`, or set "
            "GPU_GUI_ALLOW_REMOTE=1 if this box is genuinely private.")
    server = ThreadingHTTPServer((host, port), Handler)
    server.workbench = Workbench(agent, static_dir or _STATIC)
    return server
```

- [ ] **Step 4: 写 `agent/gui/index.html` 占位版**

只够证明静态服务与 CSP 通，Task 9 整体替换。注意 `<link>` 引外部 CSS —— 这是"无 unsafe-inline"成立的原因：

```html
<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'">
<title>GPU 工作台</title>
<link rel="stylesheet" href="/style.css">
</head><body>
<h1 id="brand">工作台骨架已就位</h1>
<pre id="state">loading…</pre>
<script src="/app.js"></script>
</body></html>
```

配套 `agent/gui/style.css` 先只放令牌（Task 9 补全），`app.js` 先只 fetch `/api/state`。

- [ ] **Step 5: 运行测试，确认通过**

```bash
python agent/gui_test.py
```
Expected: Step 1 的 4 个断言全 `ok`。

- [ ] **Step 6: 手工验证一次（本地，不用节点）**

```bash
python -c "
import sys; sys.path[:0]=['agent']
import gui
from agent_main import Agent
s = gui.make_server(Agent(None, verbose=False), port=8765)
print('listening on', s.server_address)
"
```
浏览器开 `http://127.0.0.1:8765/`，能看到标题。Ctrl+C 结束。

- [ ] **Step 7: 确认 TUI 与 CLI 未被牵连**

```bash
python agent/tui_test.py && python -c "import agent_main" && echo OK
```
Expected: `OK`。`gui` 不可导入时也必须 OK（spec §3.3）。

- [ ] **Step 8: 提交**

```bash
git add agent/gui.py agent/gui/index.html agent/gui/style.css agent/gui/app.js agent/gui_test.py
git commit -m "Stand up the workbench server on loopback with a CSP and a refusal to go public"
```

---

## Task 4: 任务缓冲与 SSE 流

spec §14 步 4 的后半。做完这步，`POST /api/ask` 能排队、`GET /api/events` 能按顺序推事件。

**Files:**
- Modify: `agent/gui.py`
- Test: `agent/gui_test.py`（追加 `ask_checks()`）

**Interfaces:**
- Consumes: `Job`、`sse_format`（Task 3）
- Produces: `Workbench.start_job(text: str, file: str | None) -> str | None` —— `None` 表示当前忙（映射 `409`）
- Produces: 事件名与 payload **严格**为 spec §6.1 表的七个：`phase` / `tool_call` / `tool_result` / `answer` / `session` / `error` / `done`
- Produces: `phase` 取值 `"thinking" | "tool" | "summarizing" | "done" | "failed"`

- [ ] **Step 1: 写失败的测试 —— 事件顺序与 409**

```python
def ask_checks():
    import gui
    from agent_main import Agent

    class SlowAgent(Agent):
        """Blocks in run() until told, so a second POST must be refused."""
        gate = threading.Event()
        seen = []

        def run(self, prompt):
            self.seen.append(prompt)
            self.event_sink(f"  [round 1] -> {prompt}")
            self.result_sink("analyze_dataset", {"success": True}, 0.42)
            self.gate.wait(5)
            return "final prose"

    agent = SlowAgent(None, verbose=False)
    server = gui.make_server(agent, port=0)
    job_b = gui.Job.__new__(gui.Job); job_b.job_id = ""   # noqa - just to satisfy typing
    with _Threaded(server) as w:
        base = f"http://127.0.0.1:{server.server_address[1]}"

        code, _, body = get(base + "/api/ask", json.dumps({"text": ""}).encode(), method="POST")
        check("empty text is 400", code == 400, str(code))

        code, _, body = get(base + "/api/ask", json.dumps({"text": "跑一下"}).encode(), method="POST")
        job = json.loads(body)["job_id"]
        check("ask returns 202 with a job id", code == 202 and job, f"{code} {body[:120]}")

        code, _, body = get(base + "/api/ask", json.dumps({"text": "再来一次"}).encode(), method="POST")
        check("second ask mid-run is 409, not queued (spec test #2)", code == 409, str(code))

        events = read_sse(base + f"/api/events?job={job}", want=6)
        names = [e[0] for e in events]
        check("SSE ordering ends with done (spec test #1)",
              names[-1] == "done" and "tool_result" in names and "answer" in names, str(names))
        SlowAgent.gate.set()

        code, _, _ = get(base + "/api/events?job=nope")
        check("unknown job is 404, not an infinite stream", code == 404, str(code))
```

`read_sse` 助手 —— 按帧解析，带超时，绝不因为服务端挂住而让测试卡死：

```python
def read_sse(url, want=1, timeout=10):
    """Collect `want` SSE frames. Returns [(event, payload)]. Raises on timeout."""
    out, deadline = [], time.monotonic() + timeout
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        buf = ""
        while time.monotonic() < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                event = data = None
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                if event:
                    out.append((event, data))
                    if len(out) >= want:
                        return out
    raise AssertionError(f"only saw {out} before the deadline")
```

- [ ] **Step 2: 运行，确认 404**

```bash
python agent/gui_test.py
```
Expected: `/api/ask` 返回 404（Task 3 的占位路由）。

- [ ] **Step 3: 实现 `start_job` 与两个路由**

```python
    def start_job(self, text, file=None):
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id)
        with self.agent_lock:
            if self.busy:
                return None
            self.busy = True
            self.jobs[job_id] = job
        if file:
            self.dataset = file
        job.emit("phase", {"phase": "thinking"})
        threading.Thread(target=self._drive, args=(job, text), daemon=True).start()
        return job_id

    def _drive(self, job, text):
        """Run the agent with both sinks wired into this job, then release the lock."""
        agent = self.agent
        started = time.monotonic()
        agent.event_sink = lambda msg: self._event_from_trace(job, msg)
        agent.result_sink = lambda name, parsed, seconds: job.emit(
            "tool_result", {"round": self._round(job), "name": name,
                            "result": parsed, "seconds": seconds})
        try:
            answer = agent.run(text)
            job.emit("answer", {"text": answer})
            job.emit("done", {"seconds": round(time.monotonic() - started, 2)})
        except Exception as exc:
            job.emit("error", {"message": agent.safe_error(exc)})
            job.emit("phase", {"phase": "failed"})
        finally:
            agent.event_sink = agent.result_sink = None
            job.finish()
            with self.agent_lock:
                self.busy = False
```

**这里有个必须处理的坑**：`Agent.run()` 的 `finally` 会调 `skills.close_all_sessions()`（`agent_main.py:517`），它在 `skills` 模块里、不在 `agent_lock` 保护范围内 —— 也就是说工具执行期间的 worker 锁与这里的 GUI 锁是**两把不同的锁**。本 Task 的 `busy` 标志负责 GUI 侧串行，worker 侧由 `_worker_lock` 串行，两层都要，注释里写明白，别去合并它们。

`/api/ask` 与 `/api/events`：

```python
        if path == "/api/ask":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"]) or 0) or b"{}")
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "text is required"}))
            job_id = self.wb.start_job(text, body.get("file"))
            if job_id is None:
                return self._send(409, json.dumps({"error": "an analysis is already running"}))
            return self._send(202, json.dumps({"job_id": job_id}))

        if path == "/api/events":
            job = self.wb.jobs.get(parse_qs(urlparse(self.path).query).get("job", [""])[0])
            if job is None:
                return self._not_found("events for that job")
            return self._sse(job)
```

```python
    def _sse(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = 0
        while True:
            with job.changed:
                while cursor >= len(job.events) and not job.done:
                    job.changed.wait(1.0)          # 1s heartbeat window, spec §13
                    if cursor >= len(job.events) and job.done:
                        break
                batch = list(job.events[cursor:])
                cursor = len(job.events)
                finished = job.done and cursor >= len(job.events)
            try:
                for name, payload in batch:
                    self.wfile.write(sse_format(name, payload).encode("utf-8"))
                self.wfile.flush()
                if finished:
                    return None
            except (BrokenPipeError, ConnectionResetError):
                return None      # a closed tab must not cancel the run
```

- [ ] **Step 4: 加 `phase` / `tool_call` 事件（从 trace 文本推）**

注意这是**唯一**允许从 trace 文本取信息的地方，且只取"当前在第几轮、在调哪个工具"这种展示性信息，**不取引擎事实**（引擎事实一律走 `result_sink`，见 C2）：

```python
    _CALL = re.compile(r"-> ([a-z_]+)\(")

    def _event_from_trace(self, job, msg):
        match = self._CALL.search(msg)
        if match:
            job._round = getattr(job, "_round", 0) + 1
            return job.emit("tool_call", {"round": job._round, "name": match.group(1),
                                          "args": msg.split("(", 1)[-1][:160]})
        job.emit("phase", {"phase": "tool"})
```

- [ ] **Step 5: 运行测试**

```bash
python agent/gui_test.py
```
Expected: `ask_checks` 全 `ok`。若顺序断言失败，检查 `done` 是否在 `answer` 之后、`finish()` 之前。

- [ ] **Step 6: 验证断开客户端不影响运行（spec §6.1）**

```bash
python -c "
import sys, threading, time; sys.path[:0]=['agent']
import gui
from agent_main import Agent
class Gated(Agent):
    def run(self, p):
        self.result_sink('analyze_dataset', {'success': True}, 0.1)
        time.sleep(1.5); return 'ok'
s = gui.make_server(Gated(None, verbose=False), port=0)
w = gui.Workbench(s.server.workbench.agent)
job = w.jobs  # noqa
wid = w.start_job('x'); time.sleep(0.2)
del wid
print('busy during run:', w.busy)
time.sleep(2.0)
print('busy after run :', w.busy)
print('job events     :', [e[0] for e in list(next(iter(w.jobs.values())).events)])
"
```
Expected: `busy during run: True` → `busy after run : False`，事件里含 `answer` 与 `done`。

- [ ] **Step 7: 提交**

`git commit -m "Serialize workbench runs behind one busy flag and stream their events over SSE"`

---

## Task 5: 会话状态上报（受 C6 约束）

**Files:**
- Modify: `agent/gui.py`（`state_doc` 的 `session` 字段 + `session` 事件）
- Test: `agent/gui_test.py`

**Interfaces:**
- Produces: `Workbench.session_doc() -> dict`，形状 `{"sessions": int, "warm_frames": int, "bytes": int, "reused": bool}`
- Consumes: `skills.list_sessions()`（若无此公开函数，见 Step 1 —— 必须**先加一个薄的只读公开函数**，而不是让 GUI 直接伸手进 `_worker_call`）

- [ ] **Step 1: 查清 GUI 能公开拿到 `list` 的哪条路**

```bash
grep -n "def list_sessions\|cmd.: .list.\|_worker_call" agent/skills.py | head
```

若没有公开包装器，则在 `agent/skills.py` 里加一个（这是本计划对"只改 §4.3 两个文件"的一处**有意偏离**，理由：让 GUI 直接调 `_worker_call` 会绕过模块边界，且 C5 的无超时问题会在每个新调用点重现一次）：

```python
def session_status() -> dict:
    """Report what the worker holds. Read-only from the caller's view, but do_list prunes."""
    try:
        resp = _worker_call({"cmd": "list"})
        return {"sessions": resp.get("count") or 0,
                "warm_frames": resp.get("warm_cache_count") or 0,
                "mb": resp.get("warm_cache_mb") or 0,
                "detail": resp}
    except Exception as exc:
        return {"sessions": 0, "warm_frames": 0, "mb": 0, "error": f"{type(exc).__name__}: {exc}"}
```

- [ ] **Step 2: 写测试 —— 卡片读 payload、不自己记账（spec 测试 #5）**

```python
def session_checks():
    import gui
    wb = gui.Workbench(None)
    wb._last_decision = {"policy": "resident_reuse", "observed": {"phase": "session_reuse"}}
    doc = wb.session_doc({"count": 1, "warm_cache_count": 2, "warm_cache_mb": 37.5})
    check("session doc comes from the worker payload",
          doc["sessions"] == 1 and doc["warm_frames"] == 2, str(doc))
    check("warm bytes are reported, never summed in the frontend", doc["mb"] == 37.5, str(doc))
    check("reuse is read from policy, not from a mode string (C1)",
          wb.decision_is_warm(wb._last_decision) is True, str(wb._last_decision))
    check("a one-off run is not warm",
          wb.decision_is_warm({"policy": "measured_file_size_crossover",
                                "observed": {"phase": "request_total"}}) is False)
    check("no worker answer means no numbers, not zeros (C6)",
          gui.Workbench(None).session_doc({"error": "wedged"})["sessions"] is None)
```

最后一条是刻意的：**未知不能渲染成 0** —— `resident_mb` 测量失败时 worker 自己就返回 `None` 而非 `0`（`gpu_session.py:704-714`），GUI 要同一口径。

- [ ] **Step 3: 实现 `decision_is_warm` 与 `session_doc`**

```python
    WARM_POLICIES = ("resident_reuse", "warm_cache")
    WARM_PHASES = ("session_reuse", "warm_cache_hit")

    @staticmethod
    def decision_is_warm(decision: dict) -> bool:
        """C1: there is no mode == "resident reuse". Warmth lives in policy and observed.phase."""
        if not isinstance(decision, dict):
            return False
        return (decision.get("policy") in Workbench.WARM_POLICIES
                or (decision.get("observed") or {}).get("phase") in Workbench.WARM_PHASES)

    def session_doc(self, status: dict) -> dict:
        return {"sessions": status.get("count"), "warm_frames": status.get("warm_cache_count"),
                "mb": status.get("warm_cache_mb"), "reused": self.decision_is_warm(
                    getattr(self, "_last_decision", None) or {}),
                "error": status.get("error")}
```

- [ ] **Step 4: `session` 事件只在 run 结束和 release 之后发，不轮询**

在 `_drive` 的 `finally` 里补：

```python
                status = call_with_timeout(skills_session_status)
                job.emit("session", {**self.session_doc(status), "reused": reused})
```

注释里必须写明 C6：**不能定时轮询 `/api/state`**，因为 `do_list` 会 `_prune_cache()`，轮询会自己改变它想观测的东西。

- [ ] **Step 5: 运行测试**

```bash
python agent/gui_test.py
```
Expected: `session_checks` 全 `ok`。

- [ ] **Step 6: 提交**

`git commit -m "Let the session card repeat the worker instead of keeping its own books"`

---

## Task 6: 「释放会话」接线（C3 + C5 + C10）

**Files:**
- Modify: `agent/gui.py`（`POST /api/session/release`）
- Test: `agent/gui_test.py`（spec 测试 #4）

**Interfaces:**
- Consumes: `skills.release_all_sessions_and_cache()`（**已存在**，`skills.py:781-804`，零调用者）、`call_with_timeout`（Task 3）
- Produces: `POST /api/session/release` → `{"closed": int, "warm_frames_dropped": int, "sessions_after": int, "warm_frames_after": int, "error"?: str}`

- [ ] **Step 1: 写失败的测试 —— 释放后必须"会话 0 且热帧 0"**

```python
def release_checks():
    import gui

    seen = []

    def fake_release():
        seen.append(1)
        return {"closed": 2, "warm_frames_dropped": 1,
                "sessions_after": 0, "warm_frames_after": 0}

    gui.skills_release = fake_release          # seam; the real one retains frames
    wb = gui.Workbench(None)
    out = wb.release()
    check("release calls the non-retaining verb, not close_all_sessions",
          len(seen) == 1, str(seen))
    check("release reports zero frames afterwards (spec test #4)",
          out["sessions_after"] == 0 and out["warm_frames_after"] == 0, str(out))

    def wedged():
        time.sleep(3)
        return {"sessions_after": 0}

    gui.skills_release = wedged
    slow = wb.release(timeout=0.2)
    check("a wedged worker is reported, not hidden (C5)",
          "error" in slow and "wedged" in slow["error"], str(slow))
```

这个测试**必须**能区分 `close_all_sessions()`（带 `retain:True`，会"成功"却继续占着最多 4096 MB）和 `release_all_sessions_and_cache()`。spec §10 测试 #4 特别点名：弱断言会放过这个 wiring 错误。

- [ ] **Step 2: 运行，确认失败** → `AttributeError: no 'release'`

- [ ] **Step 3: 实现**

```python
    def release(self, timeout: float = WORKER_TIMEOUT_SECONDS) -> dict:
        """Close sessions AND drop warm frames, off-thread, with our own deadline.

        skills.release_all_sessions_and_cache() already exists and already re-reads the
        after-counts, so this method only adds the timeout the underlying worker call
        never honours. A worker that answers nothing is a worker that still holds the
        memory; saying so beats showing "released".
        """
        out = call_with_timeout(skills_release, timeout)
        if "error" not in out:
            self.busy = False
        return out
```

路由：

```python
        if path == "/api/session/release":
            with self.wb.agent_lock:
                result = self.wb.release()
            self._send(200, json.dumps(result, ensure_ascii=False))
            return None
```

- [ ] **Step 4: 运行，确认通过**

- [ ] **Step 5: 确认没有把 `close_all_sessions` 用错地方**

```bash
grep -rn "close_all_sessions\|release_all_sessions_and_cache" agent/
```
Expected: `close_all_sessions` 只出现在 `skills.py` 定义处与 `agent_main.py:517`（每轮结束保留帧的**正确**行为）；`release_all_sessions_and_cache` 出现在定义处 + `gui.py` 的 seam。

- [ ] **Step 6: 提交**

`git commit -m "Give the release button a verb that really frees frames"`

---

## Task 7: `/api/files` 与 `/artifact` allow-list

**Files:**
- Modify: `agent/gui.py`
- Test: `agent/gui_test.py`（spec 测试 #3）

**Interfaces:**
- Consumes: `skills.list_datasets(directory)`（`skills.py:1057` 附近）、`_data_search_roots()`（`skills.py:1040`）
- Produces: `GET /api/files?dir=` → `list_datasets` 解析结果；`GET /artifact?path=` → 文件字节 + 推断 MIME

- [ ] **Step 1: 写失败的测试 —— 三条穿越攻击**

```python
def artifact_checks():
    import gui
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "out"
        root.mkdir()
        (root / "chart.svg").write_text("<svg/>", encoding="utf-8")
        escape = Path(tmp) / "secret.txt"
        escape.write_text("do not read", encoding="utf-8")
        link = root / "link.svg"
        try:
            link.symlink_to(escape)
        except OSError:
            link.write_text("<svg/>", encoding="utf-8")   # Windows without dev mode
        server = gui.make_server(gui.StubAgent(), port=0,
                                 static_dir=Path(tmp), artifact_roots=[root])
        with _Threaded(server) as w:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, hdr, body = get(base + "/artifact?path=chart.svg")
            check("an allowed artifact is served with MIME",
                  code == 200 and "image/svg" in hdr.get("Content-Type", ""), f"{code} {hdr}")
            for bad in ("../../etc/passwd", "/etc/passwd", "link.svg", ".."):
                code, _, body = get(base + f"/artifact?path={urllib.parse.quote(bad)}")
                check(f"traversal rejected: {bad} (spec test #3)",
                      code in (403, 404) and b"do not read" not in body and b"root:" not in body,
                      str(code))
```

- [ ] **Step 2: 运行，确认 404**

- [ ] **Step 3: 实现 —— `resolve()` + `is_relative_to()`，符号链接不豁免**

```python
    def _artifact(self, raw: str):
        """Serve only files under a root this job was allowed to produce."""
        candidate = Path(raw)
        if candidate.is_absolute():
            return self._send(403, json.dumps({"error": "absolute paths are not artifacts"}))
        for root in self.wb.artifact_roots:
            resolved_root = root.resolve()
            target = (resolved_root / candidate).resolve()
            if not str(target).startswith(str(resolved_root) + os.sep) and target != resolved_root:
                continue                        # a symlink that climbs out of the root
            if target.is_file():
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), mime)
        return self._send(404, json.dumps({"error": "not an allowed artifact"}))
```

**为什么不能只写 `is_relative_to`**：Python 3.9+ 有这个方法，仓库要跑的 Python 版本未知，`str().startswith(root + sep)` 是等价且不挑版本的写法。若 Step 4 显示节点是 3.9+，可换回 `target.is_relative_to(resolved_root)` 提高可读性。

- [ ] **Step 4: `/api/files` 委托给 `list_datasets`，不做通用文件浏览器**

```python
        if path == "/api/files":
            wanted = parse_qs(urlparse(self.path).query).get("dir", [""])[0]
            return self._send(200, json.dumps(
                skills_list_datasets(wanted or None), ensure_ascii=False))
```

注释里写明 C7 前置约束与 `13154a0` 的语义：**列举根的家目录只到 0 层**，所以节点上数据集必须直接放在 `/home/Developer` 下面，放子目录里列不出来。这句话演示者一定会踩，值得写进注释而不是文档。

- [ ] **Step 5: 运行测试** → 全 `ok`

- [ ] **Step 6: 提交**

`git commit -m "Keep workbench artifacts inside the roots a run was allowed to write"`

---

## Task 8: `/api/settings`

**Files:**
- Modify: `agent/gui.py`
- Test: `agent/gui_test.py`（spec 测试 #7）

**Interfaces:**
- Consumes: `api_config.load_config` / `save_config` / `normalize_url` / `discover_models` / `APIConfig`
- Produces: `PATCH /api/settings`，请求 `{base_url?, model?, api_key?, remember_key?, skip_setup?, language?}`；响应**不含 key**

- [ ] **Step 1: 写失败的测试 —— 密钥不回显、文件 0600**

```python
def settings_checks():
    import gui
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["GPU_ANALYSIS_CONFIG"] = str(Path(tmp) / "connection.json")
        try:
            cfg = gui.api_load_config()
            server = gui.make_server(gui.StubAgent(cfg), port=0)
            with _Threaded(server) as w:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                code, _, body = get(base + "/api/settings",
                                    json.dumps({"base_url": "https://api.example.com/v1",
                                                "api_key": "sk-SUPER-SECRET-VALUE",
                                                "model": "step-3.7-flash",
                                                "remember_key": True}).encode(),
                                    method="PATCH")
                check("settings accepted", code == 200, f"{code} {body[:160]}")
                check("the response never echoes the key (spec test #7)",
                      b"sk-SUPER-SECRET-VALUE" not in body, body[:200].decode("utf-8", "replace"))
                check("a key-present flag is reported instead",
                      json.loads(body).get("has_key") is True, body[:160].decode())
                saved = Path(tmp) / "connection.json"
                check("remember_key persists at 0600",
                      saved.exists() and (saved.stat().st_mode & 0o777) in (0o600, 0o644) or True)
                mode = saved.stat().st_mode & 0o777 if saved.exists() else None
                check("0600 on POSIX (Windows inherits the dir ACL, documented)",
                      mode in (0o600, 0o644), oct(mode or 0))
                check("the saved file really holds the key when asked to remember",
                      "sk-SUPER-SECRET-VALUE" in saved.read_text(encoding="utf-8"))
        finally:
            os.environ.pop("GPU_ANALYSIS_CONFIG", None)
```

POSIX 上必须 `0o600`（`api_config.py:113` 的 `os.chmod`），Windows 上 `0o644` 是既有行为、spec §8 已声明 —— 所以测试断言写成"接受两者之一"，并在真机（Task 12）上单独确认 `0600`。

- [ ] **Step 2: 实现 —— 手工构造响应字典，永不序列化 dataclass**

```python
    def settings_doc(self, config) -> dict:
        """Never api.asdict(config): api_key is field(repr=False) precisely because it
        must not ride along by accident."""
        return {"base_url": config.base_url, "model": config.model,
                "has_key": bool(config.api_key), "ready": config.ready,
                "language": config.language, "skip_setup": config.skip_setup,
                "remember_key": config.remember_key}
```

`PATCH` 处理：`base_url` 走 `normalize_url()`（它会拒绝带 userinfo/query 的地址、拒绝非 https 的远程地址，`api_config.py:34-45`），`ValueError` → `400` 并把它的中文错误信息原样回给前端。`language` 只接受 `zh|en`（`save_config` 自己会抛 `ValueError`，`api_config.py:102-103`）。

密钥换了 `base_url` 时必须清空 —— 复用 `agent_main.py:635-638` 已有的语义，别在 GUI 里发明第二套。

- [ ] **Step 3: 运行测试** → 全 `ok`

- [ ] **Step 4: 确认没有任何路径接受 query string 里的密钥（spec §8）**

```bash
grep -n "api_key" agent/gui.py
```
Expected: 只出现在 `settings_doc` 的 `has_key` 判定与 PATCH body 解析处；`grep "api_key.*parse_qs"` 必须无结果。

- [ ] **Step 5: 提交**

`git commit -m "Share one connection file between the workbench and the TUI"`

---

## Task 9: 前端三件套

spec §14 步 6。这一步最大，拆成三个可独立验收的子块：标记+样式、事件渲染（chip/卡片/表格）、图表。

**Files:**
- Create/Replace: `agent/gui/index.html`
- Create/Replace: `agent/gui/style.css`
- Create/Replace: `agent/gui/app.js`
- Test: `agent/gui_test.py`（chip 映射 #9、estimate #10 + DOM 探针）

**Interfaces:**
- Consumes: SSE 事件（Task 4）、`/api/state`（Task 5）
- Produces: `window.Gui.chipFromDecision(decision) -> {label, cls, note}`
- Produces: `window.Gui.buildChart(kind, data) -> svgString`

- [ ] **Step 1: 先写 chip 映射的测试 —— 这是最容易被写反的地方**

spec §6.3 点名两种反向错误，且明确"只检查 GPU 显示 GPU 的测试抓不住反向"。三种输入必须三种输出：

```python
def chip_checks():
    """The mapping is pure JS, so exercise it with the same table the browser uses."""
    import subprocess
    cases = [
        ({"actual_backend": "cudf", "selected_backend": "cudf", "fallback_reason": None,
          "policy": "measured_file_size_crossover", "observed": {}}, "gpu", "GPU · cuDF"),
        ({"actual_backend": "cudf", "selected_backend": "cudf", "fallback_reason": None,
          "policy": "resident_reuse", "observed": {"phase": "session_reuse"}},
         "gpu", "GPU · cuDF (reuse)"),
        ({"actual_backend": "pandas", "selected_backend": "cudf",
          "fallback_reason": "cuDF is not installed", "policy": "x", "observed": {}},
         "warn", "CPU · pandas (fallback)"),
        ({"actual_backend": "pandas", "selected_backend": "pandas", "fallback_reason": None,
          "reason": "CPU chosen by byte size", "policy": "measured_file_size_crossover",
          "observed": {}}, "neutral", "CPU · pandas (by choice)"),
        ({}, "muted", "no result yet"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "chip.mjs"
        script.write_text(
            "import('./app.js').then(async () => { "
            "const cases = JSON.parse(process.argv[1]); "
            "console.log(JSON.stringify(cases.map(([d]) => window.Gui.chipFromDecision(d)))); });",
            encoding="utf-8")
        # Node is optional. Without it the table is asserted by reading the source.
        ...
```

**若本机没有 node**，退化为在 Python 里复刻同一张表做**结构**断言，并把真正的行为断言留到 Task 12 真机 + 浏览器手工核对（明确写进测试注释，不要假装测过了）。

- [ ] **Step 2: 写 `style.css` 的令牌层 —— 逐字取自 `tui_app.py:43-84`**

```css
:root {
  /* Verbatim from agent/tui_app.py CSS -- the two frontends must not drift apart. */
  --surface:   #191919;  /* $surface */
  --surface-2: #22211f;  /* #details background */
  --surface-3: #292725;  /* question bubble background */
  --ink:       #e4ded6;  /* $ink */
  --muted:     #a49c93;  /* $muted */
  --accent:    #d99a76;  /* $accent -- also means "working" in the TUI */
  --line:      #57504a;  /* every TUI border */
  /* New: the mockup's engine colours have no TUI counterpart. TUI has no green/blue. */
  --gpu:  #7fb069;  --cpu:  #6d9dc5;
  --warn: #d9a94f;  --bad:  #d97b6a;
  --grid: #332f2c;                /* C8: mockup used this with no token */
  --mono: "Cascadia Mono", Consolas, "Sarasa Mono SC", Menlo, monospace;
  --ui:   system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
}
```

**必须同时把 mockup 里 14 处游离十六进制收成令牌**（`.card.live` 的 `#43563a`/`#1c2119`、`.chip.gpu` 的 `#3f5238`/`#1b2118`、`.chip.cpu` 的 `#33475b`/`#181d22`、`#2f3a2b`、`#2d2a27`、`#211d16`、`#221d19`、`#1b1a19`、`#3a3532`、`#6f6862`、`#000a`、`#4a4540`、`#2a2118`），否则 §6.3 要的六个 chip 变体没地方放。补 `.chip.warn` / `.chip.muted` / `.chip.outline` / `.card.retained`。

- [ ] **Step 3: `index.html` —— 按 mockup 骨架，修掉它的四个缺陷**

结构照 mockup 的 6 段纵向带（`header / context / main(3 列 grid) / drawer / status / composer / keys`），但必须修：

1. **给 `.v` 加 id**（`#sess-file-value` 等）。mockup 的 release 处理器用 `card.querySelectorAll(".row .v")[1]` 按**下标**定位（`workbench.html:620-629`），任何行序变动都会静默写错格子 —— 而这正是 §7 批评的"GUI 自己记账"。
2. **`<select>` 里用 `<option>`**。mockup 第 319/324 行写了 `<li>`，非法、不渲染。
3. **删掉 `.watermark` 带**（"DEMO 静态稿"）。
4. **别删掉的快捷键**：mockup 的 `keys` 文案宣传 `Tab 切换区域 / F5 / F6`，但只绑了 `Ctrl+Enter / Ctrl+L / Ctrl+K / Ctrl+, / Escape`。要么实现，要么改文案 —— 录视频时按下去没反应很难看。

- [ ] **Step 4: `app.js` 的 `I18N` —— 把 KPI 标签补进表里（mockup 漏了）**

mockup 的 `applyLang()` 里躺着一个空实现（`workbench.html:568`：`document.querySelectorAll(".kpi .l").forEach(el => {});`），且 `TURNS[0].a.en` **整块丢了 KPI**，切英文会删卡片而不是翻译。补三个键：

```js
  "kpi-peak":   ["峰值时均功率", "Peak hourly mean"],
  "kpi-trough": ["谷值时均功率", "Trough hourly mean"],
  "kpi-ratio":  ["峰谷比",       "Peak-to-trough ratio"],
```

这是 spec §6.2 亲自举的例子，必须实现。KPI 卡由 JS 按数据构造，标签查表、数字取 `tool_result`。

- [ ] **Step 5: 事件渲染 —— 遵守 C9**

```js
function renderNumber(node, value) {
  node.textContent = (value === null || value === undefined) ? "" : String(value);
  node.classList.toggle("absent", value === null || value === undefined);
}
```

`answer` 的 Markdown 走 escape-first 再贴白名单标签；`tool_result` 的任何值一律 `textContent`。mockup 的 `t()` 用 `innerHTML`，**只能用于静态 chrome**。

**卡片不轮询**（C6）：`session` 只在收到 SSE `session` 事件时更新。

- [ ] **Step 6: 图表 —— 修 C8，且优先内嵌 artifact**

`axes()` 复用 mockup 的固定 padding（`x0=44, y0=14, x1=w-10, y1=h-26`，5 条网格线，9px 标签），但：

- 删掉 `<path>` 面积填充；折线只留 `<polyline>`
- 删掉 `frame()` 里的 `<g font-family="Consolas,monospace">`，字体改由 `style.css` 的 `.chartbox text{font-family:var(--mono)}` 承担
- 图元严格 `rect / line / text / circle / polyline`
- 颜色从 `getComputedStyle(document.documentElement).getPropertyValue('--accent')` 读，**不要**像 mockup 那样在 JS 里另写 `ACCENT = "#d99a76"`（会和 CSS 漂移）
- 若 `make_deliverables.py` 已为该数据集产出 SVG，用 `/artifact` 内嵌而不是重画（§6.4：一个图表一个渲染器，且工作台不能和导出报告打架）
- `scatterChart` 的 LCG 伪随机（`workbench.html:534`）删掉，点必须来自 `tool_result`

- [ ] **Step 7: 验证"没有预测就不显示"（spec 测试 #10）**

`estimate.elapsed_seconds === null` 时，DOM 里不得出现任何预测数字或进度条；`estimate.peak_memory_mb` 恒 `null`（C2 已证），永远不渲染。

- [ ] **Step 8: 浏览器手工过一遍**

```bash
python -c "
import sys; sys.path[:0]=['agent']
import gui
from agent_main import Agent
s = gui.make_server(Agent(None, verbose=False), port=8765)
s.serve_forever()"
```
本地开 `http://127.0.0.1:8765/`。逐项确认：五个图表标签页都能渲染、中英文切换后模型散文不变而标签变、chip 四种状态颜色不同、按 `Ctrl+K` 有反应。

- [ ] **Step 9: 提交**

`git commit -m "Render the workbench from the decision record instead of from log prose"`

---

## Task 10: `--gui` / `--gui-port` 接入 `main()`

**Files:**
- Modify: `agent/agent_main.py:615-675`
- Create: `requirements-gui.txt`

**Interfaces:**
- Consumes: `gui.make_server`
- Produces: `--gui`（起服务并进入交互）与 `--gui-port`

- [ ] **Step 1: 加参数**

```python
    ap.add_argument("--gui", action="store_true",
                    help="serve the browser workbench on 127.0.0.1 and stream this run into it")
    ap.add_argument("--gui-port", type=int, default=None,
                    help=f"workbench port (default {8765})")
```

- [ ] **Step 2: 放在 `config.ready` 判定之后、TUI 分支之前，且互斥**

`--gui` 与全屏 TUI 抢同一个终端，二者只能选一。规则：显式 `--gui` 优先于 TUI；`--ask` 与管道输入**拒绝**起服务（spec §12 假设 5）：

```python
    if args.gui and args.ask:
        print("[error] --gui serves an interactive run; drop --ask to script one.", file=sys.stderr)
        return 2
    if args.gui:
        try:
            import gui
        except ImportError as exc:
            print(f"[error] Workbench unavailable ({exc}). The TUI and CLI are unaffected.",
                  file=sys.stderr)
            return 2
        server = gui.make_server(agent, port=args.gui_port or gui.DEFAULT_PORT)
        print(f"Workbench on http://127.0.0.1:{server.server_address[1]} "
              f"-- open it from Windows with: ssh -L "
              f"{server.server_address[1]}:127.0.0.1:{server.server_address[1]} <host>",
              file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()   # atexit in skills.py frees the frames on the way out
        return 0
```

- [ ] **Step 3: `requirements-gui.txt`**

```
# Optional browser workbench (agent/gui.py). The analytical backend is unchanged.
# no dependencies -- gui.py is standard library only.
# Run it with:  python agent/agent_main.py --gui
```

- [ ] **Step 4: 验证降级（spec §3.3、§9）**

```bash
python agent/agent_main.py --help | grep -- --gui
python -c "import agent_main; print('imports fine without gui')"
python -c "
import sys, builtins; sys.path[:0]=['agent']
real=builtins.__import__
def block(name,*a,**k):
    if name=='gui': raise ImportError('simulated: gui.py removed')
    return real(name,*a,**k)
builtins.__import__=block
import agent_main; print('agent_main fine with gui unimportable')
"
python agent/tui_test.py
```
Expected: 四条都正常，`--help` 里出现 `--gui`。

- [ ] **Step 5: 提交**

`git commit -m "Offer the workbench as an opt-in front end that cannot break the other two"`

---

## Task 11: 文档与回归闸

**Files:**
- Modify: `README.md`、`docs/USAGE.md`、`skill.md`（文件表）、`references/engine-contract.md`（脚本表）

- [ ] **Step 1: 跑全套回归（spec §10 测试 #11）**

```bash
python agent/gui_test.py
python agent/renderer_probe.py            # the browser-side Markdown probe
python agent/tui_test.py
python agent/api_config_test.py
bash agent/run_criteria_tests.sh
python skills/cudf-analytics/scripts/execution_decision_test.py
python skills/cudf-analytics/scripts/optimization_test.py
```
Expected: `run_criteria_tests.sh` 报 **11/11**；其余全 `ok` 或 `SKIP`。

> 已核实（2026-09-28）：上面两个测试在 `skills/cudf-analytics/scripts/` 下，不在 `agent/` 下 ——
> 之前那条"没找到"的提醒是**路径写错**造成的假警报，两个文件一直都在。
> `renderer_probe.py` 用无头 Edge/Chrome 真跑 `gui/app.js` 的渲染器；找不到浏览器时它退出码 2
> 并明说 SKIPPED，**不会伪装成通过**。`gui_test.py` 是 Python，测不到那段 JavaScript。

- [ ] **Step 2: 文档四处，与实现同一提交**

`README.md` 加一节"浏览器工作台（可选）"：一句定位（操作者前端，不是交付物，`report.html` 依旧是那个"不可能打不开"的产物 —— spec §1 特意论证过这两者不冲突）、`--gui` 用法、`ssh -L` 那一条、以及"绑定 0.0.0.0 需要 GPU_GUI_ALLOW_REMOTE=1，不建议"。

`docs/USAGE.md` 双语补同上；并把 C6（不轮询）、C7（`retained` 只有聚合数、无 `stale` 态）、`13154a0`（数据集要放在家目录正下面）三条限制如实写下 —— 这三条都会被当成 bug 报上来。

`skill.md` 文件表 + `references/engine-contract.md` 脚本表各加一行。

- [ ] **Step 3: 提交**

`git commit -m "Document the workbench and what its panels cannot honestly say"`

---

## Task 12: 上节点、打通隧道、验收录制

只有这个 Task 在节点上执行。所有命令由用户粘贴。

**Files:** 无

- [ ] **Step 1: 同步代码**

```bash
# 本地
git bundle create ../hks.bundle origin/main..feat/workbench-gui
scp -P <node-port> ../hks.bundle Developer@<jump-host>:~/
# 节点
cd ~/Data-Analysis && git fetch ~/hks.bundle feat/workbench-gui && git reset --hard FETCH_HEAD
```

- [ ] **Step 2: 装依赖（若 Task 1 判定 A/B 需要）**

```bash
cd ~/Data-Analysis && python3 -m pip install -r requirements.txt
python3 -c "import cudf; print(cudf.__version__)"
```
`requirements-gui.txt` **不需要安装任何东西**（零依赖），列出来只是声明它是可选层。

- [ ] **Step 3: 配置连接（不要在命令行传密钥）**

```bash
cd ~/Data-Analysis && python agent/agent_main.py --configure
```
按提示填 API 地址 / 密钥 / 模型。或者 `source ~/.bashrc` 用现成的 `STEPFUN_API_KEY`。
配完 `chmod 600 ~/.config/gpu-data-analysis/connection.json` 并核对。

- [ ] **Step 4: 数据集就位**

```bash
ls -la ~/ | grep -iE "\.csv|power"
python3 -c "
import sys; sys.path[:0]=['agent']
import skills; print(skills.list_datasets(None))"
```
若列表为空：数据集在**家目录的子目录里**了（C7/`13154a0` 把家目录列举压到 0 层）。把它移到 `~/` 正下面，或 `export DEMO_DATA_DIR=~/data`。

- [ ] **Step 5: tmux 里起服务（红线）**

```bash
tmux new -As gui
cd ~/Data-Analysis && python agent/agent_main.py --gui
# Ctrl+b 松手按 d 脱离，服务继续活着
```

> **起服务前先看这两个变量（`995bda6` 之后新增的前置）：**
> - `SKILL_SHOW_SPEEDUP` 被 `origin/main` 改成**默认关**（"两次额外 CPU 遍历不该拖慢正常回答"）。
>   这个默认是对的，但意味着**录制里任何"对比/加速比"数字都必须显式开**：
>   `SKILL_SHOW_SPEEDUP=1 ~/miniforge3/envs/rapids-cudf/bin/python agent/agent_main.py --gui`。
>   不带它就是空面板——**空不是 bug，别当 bug 修**，也别在空面板上口头补一个数字。
> - 若中途停顿可能超过 15 分钟，`SESSION_WARM_TTL_SECONDS=7200` 可以保住要拍的保留帧（默认
>   900 s；见复核意见 `hks-node-review-995bda6.md` A 项——失败兜底的 close 也会清池，那是更
>   短的坑）。**录完必须用不带这个变量的命令重起一次**，把 15 分钟保护在默认值下再走一遍，否
>   则这道保护在真实使用里形同不存在。

- [ ] **Step 6: 本地开隧道**

```bash
ssh -N -o ClearAllForwardings=yes -o ServerAliveInterval=30 -L 8899:127.0.0.1:8765 Developer@<jump-host> -p <node-port>
```

> 本机 `8765` **不要直接用**：Qoder 的 Remote-SSH 已经绑了 `127.0.0.1:8765` 和 `[::1]:8765`，而
> Windows 允许两个监听共存，于是请求会随机落到其中一个——表现是"有时是旧界面"。`8805` 实测也被
> 占了，用 `8899`（或先 `netstat -ano | findstr :8765` 确认）。`ClearAllForwardings` 是为了压掉
> `~/.ssh/config` 里那条 `LocalForward 8765 …`，否则它会和上面这条抢同一个本地端口。
> 浏览器开 `http://127.0.0.1:8899/`。

- [ ] **Step 7: 浏览器验收清单（逐条对着 spec §3）**

> **显存怎么量：** GB10 是统一内存，`nvidia-smi` 的**设备级** `memory.used` / `memory.total` 返回
> **N/A**（2026-09-28 节点实测），`free -h` 又被大量 buff/cache 糊住 —— 所以"释放了多少"不能看设备
> 总量。两个可用来源：① worker 自己的 `cupy.cuda.runtime.memGetInfo()`，即 `gpu_session.py` 的 `ping`
> 报 `free_gpu_gb`、`list` 报 `resident_mb` / `warm_cache_mb`（工作台卡片读的就是这两个字段）；
> ② `nvidia-smi --query-compute-apps`，它报**每进程**显存，节点实测能拍出 vLLM 44,708 MiB /
> worker 192 MiB 这样的对照，**释放镜头拍这张表**最直观。`nvidia-smi -L` 只列设备名，留作 GPU 身份证据。
> （更正记录：本块 01:40 那版写的"任何显存回落判据都不能用 nvidia-smi"过严 —— 错在把设备级字段的
> N/A 推广到了整张表。这句是我本会话写进 plan 的，按节点实测改回。）

- [ ] `http://127.0.0.1:8899/` 打开（本地转发端口，见 Step 6 关于 `8765` 被占的说明），工作台渲染完整，无控制台报错
- [ ] 选数据集 → 问一句 → 看到 `phase / tool_call / tool_result / answer / done` 依次出现
- [ ] **验收 #2**：对同一数据集连问 5 轮，第 2 轮起 chip 显示 `GPU · cuDF (reuse)`，单步耗时接近 `references/engine-contract.md` 的 ~0.06 s（这是"文件没被重读"的证据，spec §3.2 的原始意图）。**判据用 `execution_decision.observed.phase == "warm_cache_hit"`（或 `policy == "warm_cache"`）**：`resident_reuse` 只说明复用了打开中的会话，pandas 路径也会出现，不能当作 GPU 证据
- [ ] **验收 #5**：按「释放会话」→ 卡片显示会话 0 **且保留帧 0**，显存确实下降（另开 tmux 窗口跑 `gpu_session.py` 的 `ping`，对拍 `free_gpu_gb` 前后差；`/api/session/release` 的响应里 `warm_frames_dropped` / `sessions_after` / `warm_frames_after` 是同一事实的服务端说法）
- [ ] **验收 #6**：`Ctrl+C` 杀掉服务进程 → `ping` 的 `free_gpu_gb` 回到释放前水平（`skills.py:659` 的 atexit 生效）。不要用 `nvidia-smi` 的内存字段判断，GB10 上它是 N/A
- [ ] `nvidia-smi -L` 输出在日志抽屉里可见（录视频要用的 GPU 证据）
- [ ] 中英文切换：控件文案翻译、模型散文原样
- [ ] 没有预测值时页面上**不出现**任何预测数字（测试 #10）

- [ ] **Step 8: 录制**

建议顺序（一次过，避免中途等 LLM）：
1. `nvidia-smi -L` + 数据集 `wc -l` —— 交代环境
2. 问一个明确的聚合问题，展示 `GPU · cuDF` chip 与 `rows_scanned`
3. 立刻问第二个 —— 展示 `(reuse)` 与单步 ~0.06 s，**这是全片最重要的一帧**（暖复用）
4. 切一个 CPU 被**主动**选中的例子（小文件）—— 展示 chip 是 `by choice` 而不是"故障"。这是项目的核心诚实主张，spec §6.3 专门警告过不能渲染反
5. 按「释放会话」，卡片上 `占用` 与 `保留帧` 同时归零，`ping` 的 `free_gpu_gb` 回落
6. 收尾提一句 `report.html` 才是交付物

- [ ] **Step 9: 回填 Task 1 实测结果**

---

## Task 1 实测结果（执行时填写）

```
节点:            <node> / <jump-host>:<node-port>
架构 / GPU:
Python / cuDF:
tmux:
出网:            api.stepfun.com=___  github.com=___  pypi.org=___
LLM 路径判定:    A 真跑 / B 换端点 / C 回放
凭据来源:
数据集位置:
代码到节点方式:
```

---

## Self-Review

**1. Spec 覆盖。** spec §14 十步 → 本计划：步 1（rebase）已在前置状态确认完成；步 2 → Task 2；步 3 → Task 6（且发现函数已存在，C3）；步 4 → Task 3+4；步 5 → Task 7；步 6 → Task 9；步 7 → Task 8；步 8 → Task 10；步 9 → Task 11；步 10 → Task 11（回归闸）+ Task 12（真机）。§10 十一个测试全部落到具体 Task：#1→T4、#2→T4、#3→T7、#4→T6、#5→T5、#6→T2、#7→T8、#8→T3、#9/#10→T9、#11→T11。**§3 验收 7 条 → Step 7 清单逐条对应。** 无遗漏。

**2. 占位符扫描。** 无 TBD/TODO。Task 9 Step 1 的 node 缺失分支故意留了省略号 —— 那里必须写的是"无 node 时如何退化"，但退化方式取决于本机是否装了 node，属于执行时探测；已明确要求"不得假装测过了"，并给了替代方案。这是唯一一处不完全展开，已标注原因。

**3. 类型一致性。** `chipFromDecision` 返回 `{label, cls, note}`（T9 定义，T9 Step 3 的 `.chip.warn/.muted/.outline` 消费 `cls`）；`session_doc` 返回 `{sessions, warm_frames, mb, reused, error}`（T5 定义，T9 Step 5 消费同名键，`sessions` 允许 `None`）；`call_with_timeout` 返回含可选 `error` 键的 dict（T3 定义，T6 消费）；`release()` 的四个键名逐字来自 `skills.py:796-802` 已存在的实现（T6 断言）。`Workbench.start_job` / `_drive` / `decision_is_warm` / `settings_doc` 命名在各 Task 间一致。

**4. 有意偏离 spec 的地方（共 4 处，都需执行者知情）：**
- T5 Step 1 往 `skills.py` 加 `session_status()` —— 突破 §4.3"只改两个文件"。理由：否则 GUI 直接调 `_worker_call`，把 C5 的无超时问题在每个调用点复制一遍。**若用户不接受，改为在 `gui.py` 内部包一层，不改 `skills.py`。**
- T3/T7/T8 把 spec §14 步 4、5、7 拆成三个 Task —— 每个都该独立可测，合成一个 Task 会让"释放按钮"和"SSE 编码"共用一次 review 关口。
- 全程在 Windows 开发、只在 T12 上节点 —— spec §3.2/§14 步 10 隐含"在 GB10 上做"。已论证无头测试不需要 GPU。
- C7 砍掉 `stale` 态 —— 直接违反 §7 的五态表。理由是 worker 根本没有这个信号，硬做就是幻觉渲染，与 §2/§6.2 的"没有字段就什么都不显示"冲突。**这是必须让用户签字的降级。**
