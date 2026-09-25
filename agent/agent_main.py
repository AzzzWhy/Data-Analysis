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
    python agent_main.py --ask "analyse /data/sales.csv for outliers"
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

SYSTEM_PROMPT = """You are a data-analysis agent running on an NVIDIA DGX Spark.

How you work:
- The user gives you an analytical request in natural language. You have locally executable
  skills (tools), and they run real code on this machine.
- To analyse a local data file you must call the analyze_dataset tool. Do not answer from
  memory or from a guess.
- Never estimate statistics (a mean, a min or max) from a data excerpt you happened to read.
  A file can hold millions of rows, and only the values a tool returns are exact over the full
  data. The rows_scanned field in a tool result is the number of rows actually scanned.
- When it is unclear which file the user means, call list_datasets first to see what data is
  available.
- Every new analysis request needs a tool call. Do not repeat numbers from an earlier round as
  this round's result; you may reuse them only when the file and the operation are both
  identical, and you must say that they are reused.
- When the user names a specific file path or column, call the tool even if you suspect the
  file does not exist, so the tool returns the real error and you can explain the problem from
  that. Do not skip the call because of a guess about the filename.
- When a tool returns an error, judge whether it is fixable, and fix what is fixable before you
  answer: if the message gives an actionable next step (for example "file does not exist, use
  list_datasets to get an absolute path and retry", or "column does not exist, call profile
  first to get the real column names"), do that and then retry with corrected arguments. Report
  the problem to the user only after you followed the guidance and still failed. Do not give up
  after one failed call, and do not skip the tool and guess instead.
- If a tool error says a column does not exist (for example "Column(s) ['sales'] do not exist"),
  call analyze_dataset(operation="profile") to get the real column names, then retry once with
  the correct name. If the retry fails too, tell the user the real column names and ask which
  one they want; do not keep guessing names.
- Do not invent column names. When the user describes a business term in their own words
  ("revenue", "units sold"), confirm what the file actually calls it before you build the
  by / agg / columns arguments.
- When the user wants something they can take away, use export_deliverables instead of
  answering in chat alone. Trigger phrases: "make a chart", "plot it", "give me a report",
  "export this", "save it", "I need this for a briefing / an email / a document".
  It runs the full analysis on the GPU and writes report.md (a Markdown report with tables),
  several .svg charts, and CSV/JSON data files.
  After the call you must put the returned file paths in your answer verbatim, or the user
  cannot get the files. Do not just say "saved".
  If the user wants one conclusion and no files, answer with analyze_dataset and skip the export.
- When one call gives you enough to answer, give the conclusion instead of calling again.
- When the request itself is multi-step (find the outliers and explain why they occur, compare
  several dimensions, take an overview then drill into one group), use dataset_session: call
  operation="open" first to load the file and get a session_id, then run every further step with
  operation="analyze" and the same session_id, and do not open the file again in between. When
  the work is done, operation="close" is required to release device memory.
  What this buys you: the data stays resident in device memory, so every later step is still a
  full-data computation that takes only tens of milliseconds, which makes extra drill-down steps
  cheap.
- Once a session is open, keep using it until you close it. When a step fails midway (a
  misspelled grouping column, say), correct the arguments and retry on the same dataset_session;
  do not switch back to analyze_dataset. analyze_dataset re-reads the file on every step (about
  10 to 25 seconds for 20M rows), while a session step takes tens of milliseconds. Fall back to
  analyze_dataset only if the open itself fails, for example for lack of device memory.
- When a step inside a session fails, the order is: read the error, change the arguments, retry
  on the same session. Not "close the session". The error message usually names the legal values
  (it lists the permitted agg functions, for example), so change one and try again. Do not close
  before you have changed anything: after a close you cannot retry against the resident data and
  you are back on the slow path. Do not retry a second time with exactly the same arguments
  either; that is not a retry, it is the same mistake twice. Pick a legal value, or pick another
  operation that expresses what you mean (if a correlation cannot be computed per group, use
  op='corr' over the whole dataset, then groupby with mean/std to compare the groups).
- Always close when you are finished. A session holds device memory (about 1.7GB for 20M rows),
  and leaving it open affects later tasks and other processes. Close what is open even if
  something failed partway.
- Do not use a session for a single-step question; analyze_dataset is enough there, and a
  session would only occupy device memory for nothing.

Answer requirements:
- Reply in English. State the conclusion the user asked for, with the key numbers made clear
  (with units and orders of magnitude).
- Report how it actually ran. If engine is cudf in the tool result, you can say the computation
  finished on the GPU. If it is pandas, or the result carries a warning, you must say this run
  happened on the CPU; do not claim GPU acceleration.
- gpu_vs_cpu in a tool result is the measured GPU and CPU time for this run, on the same file
  and the same command. Whenever it is present, every answer must end with a separate "How it
  ran" paragraph giving three numbers: GPU time, CPU time, and the factor. All three, or the
  figure is incomplete; the GPU number alone is not enough.
  Reference format (follow it, do not drop the CPU):
  "This analysis ran on the GPU (NVIDIA GB10) through cuDF over all <N> rows in <X> seconds;
  the same computation took <Y> seconds on CPU pandas, so the GPU was <Z>x faster."
  Then add the caveats from honest_note in gpu_vs_cpu (which parts of the speedup are not GPU
  compute).
- If you used dataset_session: every analyze response carries step_seconds (time for that step)
  and cumulative_seconds (running session total). The close response carries
  workflow_comparison with the total session time, the time the conventional approach takes
  (CPU re-reading the file at each step) and the factor between them. End the answer with a
  sentence built from workflow_comparison: "this run did N full-data steps in X seconds; doing
  each step on the CPU with a fresh read would take about Y seconds, so it was Z times faster."
  Then repeat its note: that factor includes the benefit of the data already being resident in
  device memory, and it is not a pure GPU compute speedup. Do not present it as one.
- Correlation is not causation. Do not over-read a corr result.
- Do not print raw JSON. Use plain language and tables where they help.
"""


def build_client() -> OpenAI:
    api_key = os.environ.get("STEPFUN_API_KEY")
    if not api_key:
        print("[error] the STEPFUN_API_KEY environment variable is not set.")
        print("        On this machine run: source ~/.bashrc   then start the script again.")
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
        return {}, f"arguments are not valid JSON: {exc}"
    if not isinstance(loaded, dict):
        return {}, f"arguments must be a JSON object, got {type(loaded).__name__}"
    return loaded, None


def execute_tool(name: str, raw_args: str) -> tuple[str, float]:
    """Run one skill call. Returns (result_json, seconds). Never raises."""
    started = time.perf_counter()
    args, problem = parse_arguments(raw_args)
    if problem:
        return json.dumps({"success": False, "error": problem,
                           "hint": "regenerate the arguments to match the tool schema"},
                          ensure_ascii=False), 0.0

    func = skill_func_map.get(name)
    if func is None:
        return json.dumps({
            "success": False,
            "error": f"unknown skill: {name}",
            "available": sorted(skill_func_map.keys()),
        }, ensure_ascii=False), 0.0

    try:
        result = func(**args)
    except TypeError as exc:
        # Almost always a wrong/misspelled parameter name from the model.
        return json.dumps({
            "success": False,
            "error": f"argument mismatch: {exc}",
            "hint": f"see the tool schema for the arguments {name} accepts",
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

def _system_prompt() -> str:
    """The system prompt, adjusted for the control run.

    Without this the control arm is not a control: the model's training makes it emit a
    tool call as plain text when no tools are supplied, so it never actually answers and the
    comparison measures nothing. Telling it plainly that it has no tools turns the question into
    the one worth asking -- what does the same model say about a dataset it cannot open?
    """
    if not os.environ.get("NO_TOOLS"):
        return SYSTEM_PROMPT
    return (
        "You have no tools, no file access and no ability to read data. Answer from your own "
        "knowledge only.\n"
        "- Never write a tool call, a function call or an XML tag resembling one. Any call you "
        "write will not be executed, so it is not an answer.\n"
        "- If the question requires data you do not have, say so directly and explain what would "
        "be needed.\n"
        "- Do not invent numbers. State plainly that you cannot know them."
    )


class Agent:
    def __init__(self, client: OpenAI, verbose: bool = True):
        self.client = client
        self.verbose = verbose
        self.messages: list[dict] = [{"role": "system", "content": _system_prompt()}]
        # Control condition for the comparison experiment: the same model, same prompt, no skills.
        # Nothing else changes, so any difference in the answer is attributable to the tools rather
        # than to a different question or a different system prompt. Without this the project has
        # no evidence that the Skill changes an answer -- only that behaviour matches expectation.
        self.no_tools = bool(os.environ.get("NO_TOOLS"))
        self.tools = [] if self.no_tools else load_skill_definitions()
        if self.verbose:
            names = [t["function"]["name"] for t in self.tools]
            print(f"[agent] model={MODEL_NAME}")
            if self.no_tools:
                print("[agent] CONTROL RUN: no skills are available to the model")
            else:
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
                self.log(f"  [session] auto-released {released['closed']} session(s) left open"
                         f" (the model did not call close)")

    def _run_inner(self, user_query: str) -> str:
        self.messages.append({"role": "user", "content": user_query})
        # Let the skill layer know what language the user wrote in, so a generated report
        # comes back in that language rather than always in the default one.
        skills.remember_request_text(user_query)

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            try:
                # Omit the tools parameter entirely in the control run: an empty list is rejected
                # by some OpenAI-compatible providers, and passing nothing is the honest form of
                # "this model has no skills here".
                kwargs = {} if self.no_tools else {"tools": self.tools, "tool_choice": "auto"}
                response = self.client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=self.messages,
                    **kwargs,
                )
            except Exception as exc:
                # A model/transport failure must not kill the session or lose the history.
                self.log(f"  [api error] {type(exc).__name__}: {exc}")
                if "tool" in str(exc).lower():
                    self.log("  [hint] this model may not support function calling. "
                             "Use --no-tools to fall back to plain chat, or switch to a model "
                             "that supports tool calls.")
                return f"[model call failed] {type(exc).__name__}: {exc}"

            if not response.choices:
                return "[model call failed] empty response"
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
        self.log(f"  [warn] hit the tool-round limit ({MAX_TOOL_ROUNDS})")
        try:
            final = self.client.chat.completions.create(
                model=MODEL_NAME, messages=self.messages)
            content = final.choices[0].message.content or ""
            self.messages.append({"role": "assistant", "content": content})
            return content
        except Exception as exc:
            return (f"[tool round limit {MAX_TOOL_ROUNDS} reached, no summary could be generated] "
                    f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DGX Spark data-analysis agent with Agent Skills")
    ap.add_argument("--ask", help="ask one question and exit (non-interactive demo)")
    ap.add_argument("--quiet", action="store_true", help="suppress tool-call tracing")
    args = ap.parse_args()

    client = build_client()
    agent = Agent(client, verbose=not args.quiet)

    if args.ask:
        print(f"\n=== User ===\n{args.ask}")
        answer = agent.run(args.ask)
        print(f"\n=== Agent ===\n{answer}")
        return 0

    print("\nType 'exit' or 'quit' to leave.")
    while True:
        try:
            task = input("\nEnter your question: ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if task.strip().lower() in {"exit", "quit"}:
            print("Goodbye.")
            break
        if not task.strip():
            continue
        answer = agent.run(task)
        print(f"\n=== Agent Reply ===\n{answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())