"""
DGX Spark data-analysis agent — StepFun LLM + locally executed Agent Skills.

The point of this file is the part the earlier version was missing: a tool-calling loop.
The model decides *which* skill to call and *with what arguments*; the skill code runs
locally on the GB10 and its real output is fed back to the model, which then answers the
user in natural language.

Loop shape:

    user question
      -> model (with skill schemas attached)
      -> tool_calls?  --no--> final answer
      |                     (done)
      yes
      -> execute each skill locally
      -> append tool results to the transcript
      -> model again          (bounded by MAX_TOOL_ROUNDS)

Every failure mode is handled without crashing: unknown skill, malformed JSON arguments,
missing file, engine error, API error, and runaway tool loops.

Run:
    source ~/.bashrc            # provides STEPFUN_API_KEY
    conda activate rapids-cudf
    python agent_main.py
    python agent_main.py --ask "帮我分析 /data/sales.csv 有没有异常值"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from openai import OpenAI

import skills
from skills import load_skill_definitions, skill_func_map

MODEL_NAME = os.environ.get("STEPFUN_MODEL", "step-3.7-flash")
BASE_URL = os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.com/step_plan/v1")

# A tool loop that never ends is worse than one that stops and explains itself.
#
# Raised to 10 after measurement, not by taste: a real drill-down run took an extra grouping
# dimension (legitimate, and better analysis), which consumed the budget before it could call
# close -- leaving 1.7 GB of device memory resident. The cleanup backstop released it, but
# spending one more round is far cheaper than a leaked session or a truncated answer.
# Measured: a 6-operation drill-down plus close needs 8 rounds; with extra exploration, more.
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "10"))

SYSTEM_PROMPT = """你是一个部署在 NVIDIA DGX Spark 上的数据分析智能体。

你的工作方式：
- 用户会给你自然语言的分析需求。你有本地可执行的 Skill（工具），它们会在本机服务器上真实运行代码。
- 需要分析本地数据文件时，必须调用 analyze_dataset 工具，而不是凭印象或猜测回答。
- 绝对不要用你自己读到的数据片段去估算统计量（比如均值、最大最小值）。数据可能有几百万行，
  只有工具返回的值才是全量精确结果。工具返回里的 rows_scanned 就是实际扫描的全量行数。
- 不清楚用户指哪个文件时，先调用 list_datasets 查一下有哪些数据。
- 每个新的分析请求都要调用工具去算。不要因为前面某一轮算过类似的东西，就直接把旧数字
  当成本次结果复述；只有在"同一文件 + 同一操作"完全相同时才可以复用，并且要说明这是复用。
- 用户给出了具体的文件路径或列名时，即使你可能觉得这个文件不存在，也要调用工具去试，
  让工具返回真实错误，再据此向用户说明问题——不要凭文件名猜测而跳过调用。
- **工具返回错误后要判断它是否可修复，可修复的必须自己修完再回答**：
  如果错误信息给出了可执行的下一步（例如"文件不存在，请用 list_datasets 拿绝对路径重试"
  或"列名不存在，请先调 profile 查真实列名"），就按它说的再做一次，然后用正确参数重试。
  只有当你已经按指引重试过仍然失败，才把问题报告给用户。
  不要因为第一次调用失败就直接放弃、也不要跳过工具改用猜测。
- 如果工具返回的 error 说某个列不存在（例如 "Column(s) ['销售额'] do not exist"），
  先调用 analyze_dataset(operation="profile") 拿到真实列名，再用正确的列名重试一次；
  如果重试仍失败，就把真实列名告诉用户并请他确认想要哪一列，不要重复猜列名。
- 不要凭空假设列名。用户用中文描述业务口径（如“销售额”“销量”）时，先确认文件里
  实际的列名是什么，再构造 by / agg / columns 参数。
- **用户想要"拿走的东西"时，用 export_deliverables，不要只在对话里回答。**
  触发信号：「出个图」「画一下」「给我一份报告」「导出」「保存」「我要汇报/发邮件/写进文档」。
  它会在 GPU 上做全量分析，然后写出 report.md（Markdown 报告，含表格）、若干 .svg 图表、
  以及 CSV/JSON 数据文件。
  调用后**必须把返回的文件路径原样写在回答里**，否则用户拿不到。不要只说"已保存"。
  如果用户只要一个结论、不要文件，就用 analyze_dataset 回答，别导出。
- 一次调用得到的结果足够回答时，就直接给出结论，不要反复调用。
- 当需求本身是**多步**的（例如「找出异常并分析原因」「对比几个维度」「先概览再深入某一组」），
  用 dataset_session 做：先 operation="open" 打开文件拿到 session_id，之后每一步都用
  operation="analyze" + 同一个 session_id 执行，**不要在中间重复 open**；全部做完后必须
  operation="close" 释放显存。
  这样做的价值：数据已常驻显存，之后每一步都是全量计算但只需几十毫秒，所以你可以放心多问几步去下钻。
- **已经 open 了就必须一直用它，直到 close 为止。** 会话中途某一步失败（例如分组列名写错）时，
  修正参数后**仍然用 dataset_session 重试**，不要改回 analyze_dataset：
  analyze_dataset 每一步都要重新读盘（2000 万行约 10~25 秒），而 session 里只要几十毫秒。
  只有 open 本身失败（例如显存不足）才降级到 analyze_dataset。
- **会话里某一步报错时，顺序必须是「读报错 → 改参数 → 在同一个 session 上重试」，不是「关掉会话」。**
  报错信息通常会告诉你合法取值（例如列出了允许的 agg 函数名），照着改一个再试一次即可。
  在改参数之前**不要 close**：一旦 close 就没法在常驻数据上重试了，只能退回很慢的路径。
  也**不要用完全相同的参数重试第二次**——那不叫重试，叫重复同样的错误；
  换一个合法值，或者换一种能表达你意图的操作（例如按组算不了相关性，就用 op='corr' 看整体、
  再用 groupby + mean/std 看组间差异）。
- **用完一定要 close。** 会话持续占用显存（2000 万行约 1.7GB），不关闭会影响后续任务和其他进程。
  即使中途出错，也要把已打开的会话关掉。
- 只有一步的简单问题**不要**用 session（用 analyze_dataset 即可），session 会白占显存。

回答要求：
- 用中文回答，直接给出用户想知道的结论，把关键数字说清楚（带上单位和量级）。
- 如实说明运行情况：如果工具返回里 engine 是 cudf，可以说是 GPU 加速完成的；
  如果是 pandas（或结果里带 warning），必须说明这次是在 CPU 上运行的，不要声称用了 GPU。
- 工具结果里的 gpu_vs_cpu 是**同一文件、同一命令实测出的本次 GPU 与 CPU 耗时对比**。
  只要它存在，**每一次回答都必须在结尾单独写一段「运行情况」**，并同时给出三个数字：
  GPU 耗时、CPU 耗时、倍数。缺一不可，不要只写 GPU 那个数。
  格式参考（照这个写，不要省掉 CPU）：
  「本次分析在 GPU（NVIDIA GB10）上通过 cuDF 完成，全量 <N> 行耗时 <X> 秒；
   同一计算在 CPU pandas 上耗时 <Y> 秒，GPU 快 <Z> 倍。」
  然后按 gpu_vs_cpu 的 honest_note 附上必要的说明（哪些加速不属于 GPU 计算）。
- 如果用的是 dataset_session：**每一步**的 analyze 返回里有 step_seconds（本步耗时）和
  cumulative_seconds（会话累计）；调 close 时返回 workflow_comparison，里面有
  本次会话总耗时、传统做法（CPU 每步重读）耗时和倍数。回答结尾就按 workflow_comparison
  写明「本次 N 步全量分析共 X 秒；若每步都用 CPU 重新读盘计算约需 Y 秒，快 Z 倍」，
  并照它的 note 说明：该倍数包含「数据已常驻显存」的收益，不等于纯 GPU 计算加速比。
  不要把它说成纯 GPU 计算加速。
- 相关性不等于因果，不要过度解读 corr 的结果。
- 不要输出原始 JSON，用自然语言和必要的表格呈现。
"""


def build_client() -> OpenAI:
    api_key = os.environ.get("STEPFUN_API_KEY")
    if not api_key:
        print("[错误] 环境变量 STEPFUN_API_KEY 未设置。")
        print("       在本机执行: source ~/.bashrc   然后再运行本脚本。")
        sys.exit(2)
    return OpenAI(api_key=api_key, base_url=BASE_URL)


# --------------------------------------------------------------------------------------
# Tool execution
# --------------------------------------------------------------------------------------

def parse_arguments(raw: str) -> tuple[dict, str | None]:
    """Arguments arrive as a JSON string and may be malformed or not an object."""
    if raw is None or raw == "":
        return {}, None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"参数不是合法 JSON: {exc}"
    if not isinstance(loaded, dict):
        return {}, f"参数必须是 JSON 对象，收到的是 {type(loaded).__name__}"
    return loaded, None


def execute_tool(name: str, raw_args: str) -> tuple[str, float]:
    """Run one skill call. Returns (result_json, seconds). Never raises."""
    started = time.perf_counter()
    args, problem = parse_arguments(raw_args)
    if problem:
        return json.dumps({"success": False, "error": problem,
                           "hint": "请按工具 schema 重新生成参数"}, ensure_ascii=False), 0.0

    func = skill_func_map.get(name)
    if func is None:
        return json.dumps({
            "success": False,
            "error": f"未知的 Skill: {name}",
            "available": sorted(skill_func_map.keys()),
        }, ensure_ascii=False), 0.0

    try:
        result = func(**args)
    except TypeError as exc:
        # Almost always a wrong/misspelled parameter name from the model.
        return json.dumps({
            "success": False,
            "error": f"参数不匹配: {exc}",
            "hint": f"{name} 接受的参数见工具 schema",
        }, ensure_ascii=False), time.perf_counter() - started
    except Exception as exc:
        return json.dumps({"success": False,
                           "error": f"{type(exc).__name__}: {exc}"},
                          ensure_ascii=False), time.perf_counter() - started

    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    return result, time.perf_counter() - started


def summarize_tool_result(result_json: str) -> str:
    """Short human-readable line for the console, so a demo shows what actually ran."""
    try:
        payload = json.loads(result_json)
    except json.JSONDecodeError:
        return "unparseable result"
    if not payload.get("success"):
        return f"FAILED: {payload.get('error')}"
    rows = payload.get("rows_scanned")
    # bool is a subclass of int, so exclude it explicitly rather than printing True as rows.
    rows_txt = f"rows={rows:,}" if isinstance(rows, int) and not isinstance(rows, bool) else "rows=?"
    secs = payload.get("seconds")
    secs_txt = f"{secs:.2f}s" if isinstance(secs, (int, float)) else ""
    bits = [f"engine={payload.get('engine')}", rows_txt, secs_txt]

    # Session calls have their own shape: a load, cheap per-step timings, and a
    # workflow-level figure on close. Surface those so a demo shows the multi-step
    # drill-down and its cost as it happens.
    if payload.get("session_id") and "step_seconds" in payload:
        bits.append(f"| step {payload['step_seconds']}s "
                    f"(session total {payload.get('cumulative_seconds')}s, "
                    f"step #{payload.get('steps_this_session')})")
    elif "load_seconds" in payload and payload.get("session_id"):
        cpu_load = payload.get("cpu_load_seconds")
        bits.append(f"| loaded {payload['load_seconds']}s"
                    + (f" vs CPU {cpu_load}s" if cpu_load else "")
                    + (f", resident {payload['resident_mb']}MB"
                       if payload.get("resident_mb") else ""))
    wc = payload.get("workflow_comparison")
    if isinstance(wc, dict) and isinstance(wc.get("speedup_x"), (int, float)):
        bits.append(f"| WORKFLOW: session {wc['session_total_seconds']}s vs "
                    f"naive-CPU {wc['naive_cpu_seconds']}s = {wc['speedup_x']:.1f}x")
        return "OK  " + "  ".join(b for b in bits if b)

    # Deliverables: show what was actually written, so the demo audience sees files appear.
    if payload.get("report"):
        bits.append(f"| wrote {payload.get('chart_count', 0)} charts + report -> "
                    f"{payload['report']}")
        return "OK  " + "  ".join(b for b in bits if b)

    # Surface the measured GPU-vs-CPU comparison in the trace: this is the line that shows
    # the audience what the skill bought them on this very call.
    cmp = payload.get("gpu_vs_cpu")
    if isinstance(cmp, dict) and isinstance(cmp.get("speedup_x"), (int, float)):
        bits.append(
            f"| GPU {cmp['gpu_seconds']:.2f}s vs CPU {cmp['cpu_seconds']:.2f}s "
            f"= {cmp['speedup_x']:.2f}x"
        )
    return "OK  " + "  ".join(b for b in bits if b)


# --------------------------------------------------------------------------------------
# The agent loop
# --------------------------------------------------------------------------------------

class Agent:
    def __init__(self, client: OpenAI, verbose: bool = True):
        self.client = client
        self.verbose = verbose
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.tools = load_skill_definitions()
        if self.verbose:
            names = [t["function"]["name"] for t in self.tools]
            print(f"[agent] model={MODEL_NAME}")
            print(f"[agent] skills available to the model: {', '.join(names)}")

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def run(self, user_query: str) -> str:
        """Answer one question. Any session left open is released on the way out."""
        try:
            return self._run_inner(user_query)
        finally:
            # Structural guarantee rather than a prompt request. The model is asked to call
            # close, and usually does, but a leaked resident frame costs gigabytes and would
            # silently degrade every later question. Observed once in practice, so this is
            # enforced in code.
            released = skills.close_all_sessions()
            if released.get("closed"):
                self.log(f"  [session] 自动释放了 {released['closed']} 个未关闭的会话"
                         f"（模型忘记调用 close）")

    def _run_inner(self, user_query: str) -> str:
        self.messages.append({"role": "user", "content": user_query})

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            try:
                response = self.client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=self.messages,
                    tools=self.tools,
                    tool_choice="auto",
                )
            except Exception as exc:
                # A model/transport failure must not kill the session or lose the history.
                self.log(f"  [api error] {type(exc).__name__}: {exc}")
                if "tool" in str(exc).lower():
                    self.log("  [hint] 该模型可能不支持 function calling。"
                             "可用 --no-tools 降级为纯对话模式，或改用支持工具调用的模型。")
                return f"[调用模型失败] {type(exc).__name__}: {exc}"

            if not response.choices:
                return "[调用模型失败] 返回结果为空"
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)

            # ---- no tool call: this is the final answer ----
            if not tool_calls:
                content = message.content or ""
                self.messages.append({"role": "assistant", "content": content})
                return content

            # ---- tool call(s): echo the assistant turn, then execute each one ----
            self.messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                } for tc in tool_calls],
            })

            for tc in tool_calls:
                name = tc.function.name
                raw = tc.function.arguments
                args, _ = parse_arguments(raw)
                self.log(f"  [round {round_index}] -> {name}({json.dumps(args, ensure_ascii=False)[:160]})")
                result, seconds = execute_tool(name, raw)
                self.log(f"      {summarize_tool_result(result)}   ({seconds:.2f}s wall)")
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # Loop budget exhausted: report what happened instead of spinning forever.
        self.log(f"  [warn] 达到最大工具调用轮数 {MAX_TOOL_ROUNDS}")
        try:
            final = self.client.chat.completions.create(
                model=MODEL_NAME, messages=self.messages)
            content = final.choices[0].message.content or ""
            self.messages.append({"role": "assistant", "content": content})
            return content
        except Exception as exc:
            return (f"[已达最大工具轮数 {MAX_TOOL_ROUNDS}，且无法生成总结] "
                    f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DGX Spark data-analysis agent with Agent Skills")
    ap.add_argument("--ask", help="ask one question and exit (non-interactive demo)")
    ap.add_argument("--quiet", action="store_true", help="suppress tool-call tracing")
    args = ap.parse_args()

    client = build_client()
    agent = Agent(client, verbose=not args.quiet)

    if args.ask:
        print(f"\n=== 用户 ===\n{args.ask}")
        answer = agent.run(args.ask)
        print(f"\n=== Agent ===\n{answer}")
        return 0

    print("\n输入 'exit' 或 'quit' 退出。")
    while True:
        try:
            task = input("\nEnter your question: ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if task.strip().lower() in {"exit", "quit"}:
            print("再见！")
            break
        if not task.strip():
            continue
        answer = agent.run(task)
        print(f"\n=== Agent Reply ===\n{answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())