# main / guiv2 整合验收（2026-09-29）

## 整合范围

- 基线 main：`cad6284b35c182dd4fcd41386dbae8f4cf0da960`。
- GUI 输入：guiv2 `703b2007aba6cc90caac53ad64acd7d080fc0828`。
- 使用双父提交合并，保留核心与 GUI 历史，不用 GUI 分支覆盖主分支。
- README 重写为统一产品入口：8877 工作台、本机/SSH 远端后端、兼容模型、CLI/TUI、固定任务及可复现性能证据。
- 本轮只整合仓库，不部署 GB10，不重启现有工作台，不迁移访问密码或模型配置。

## 冲突处理

| 文件 | 处理 |
| --- | --- |
| `README.md` | 按当前真实能力重写，明确历史性能与本次回归不是同一批证据 |
| `agent/skills.py` | 保留 main 的工作进程管理，双向 UTF-8 通信，并保留 GUI 的解码容错 |
| `skills/cudf-analytics/scripts/gpu_session.py` | 保留 main 的活动会话引擎/读取路径切换保护；复用响应继续携带 GUI 分析计划，避免提前 return 丢失计划 |

新增 `agent/main_integration_test.py` 使用真实 pandas 工作进程验证 GUI → Skill → 会话链路。它不调用模型、不读取用户模型配置、不启动浏览器，也不使用正在运行的工作台进程。

## 本机回归结果

环境：Windows、Python 3.12；测试解释器已有 pandas、OpenAI SDK、Paramiko。Parquet 用到的 PyArrow 25.0.1 安装在仓库外的独立测试依赖目录，通过 `PYTHONPATH` 提供，不改变工作台虚拟环境。没有 cuDF/GPU 或 Textual 验收条件。

### JavaScript：322 项通过

| 测试 | 通过数 |
| --- | ---: |
| gui_dataset_drag_test.js | 97 |
| login_page_test.js | 76 |
| gui_job_recovery_test.js | 34 |
| gui_result_contract_test.js | 51 |
| gui_reset_test.js | 23 |
| backend_switch_test.js | 41 |

以上是 DOM/VM 逻辑测试，不是 Chrome 视觉验收。`app.js` 和 `login.js` 的 Node 语法检查通过。

### Python unittest：218 项通过

| 测试 | 通过数 |
| --- | ---: |
| agent/fast_execution_test.py | 14 |
| agent/interactive_reuse_test.py | 5 |
| agent/gui_concurrency_test.py | 8 |
| agent/gui_job_recovery_test.py | 17 |
| agent/gui_outcome_bridge_test.py | 4 |
| agent/agent_run_outcome_test.py | 16 |
| agent/gui_reset_gate_test.py | 16 |
| agent/frontend_gateway_test.py | 21 |
| session_statistics_test.py | 12 |
| hybrid_batch_test.py | 14 |
| hybrid_execution_test.py | 24 |
| shared_batch_test.py | 4 |
| optimization_test.py | 11 |
| gpu_coordination_test.py | 5 |
| agent/batch_job_test.py | 3 |
| agent/batch_queue_test.py | 9 |
| agent/report_service_test.py | 5 |
| agent/managed_ssh_test.py | 15 |
| agent/managed_ssh_integration_test.py | 13 |
| agent/main_integration_test.py | 2 |

未写目录前缀的 Python 文件位于 `skills/cudf-analytics/scripts/`。网关 21 项完整测试连续运行两次均通过，表格不重复计数。SSH 集成测试使用合成服务器，不代表本次已连接 GB10。

### 额外检查

- `agent/api_config_test.py`：配置与端点检查通过；Textual 界面检查因缺少可选依赖跳过。
- `session_worker_test.py --fixture-rows 2000`：真实 CPU 工作进程的打开、全量分析、重复操作、过期保护、列举和关闭检查通过；GPU 引擎、GPU 驻留内存及小文件无法可靠区分的计时断言跳过。没有将该小规模计时作为性能结论。
- 新增合并回归验证：中文目标经进程管道返回后不变；会话复用 `read_count=0`；实际 summary 引擎为 pandas；切换活动 CPU 会话到 GPU 被拒绝，既有会话与计划仍保留。
- README 本地文档链接存在，工作台、独立后端、Agent、批量任务、队列及报表服务的 `--help` 均可正常执行。

## 测试中遇到的环境问题

1. Parquet 测试首次因缺少 PyArrow 失败；提供独立依赖后完整复跑通过。这不是修改计算算法得到的修复。
2. 队列测试首次在默认共享 GPU 锁文件上遇到 `PermissionError`。改用测试专属 `GPU_ANALYSIS_GPU_SLOT_FILE` 后，9 项完整通过；未删除共享锁、修改权限或中断工作台。此结果不表示任意现有锁路径的权限问题已自动解决。
3. 合成 SSH 断线/拒绝场景输出 Windows socket 10038/10053/10054 日志，但 13 项断言均通过。此前 GUI 记录中的网关偶发断连不能仅凭这两次通过就宣称根因已修复。

复现隔离环境时，可将 `GPU_ANALYSIS_GPU_SLOT_FILE` 指向测试专用的可写临时文件，并让测试解释器及其子进程都能导入 PyArrow。不要用修改正在运行的服务、共享锁或用户配置来使测试通过。

## 未覆盖的验收

- 本轮未重新运行 GB10/cuDF 性能实验、云端或本地模型真实对话。
- 未新增 Chrome 登录后全流程或浏览器视觉验收，也未跑需要浏览器 renderer 的完整 `agent/gui_test.py`。
- 未证明全量未知数据始终选择最快路径；README 性能数字明确引用历史、同口径基准。
- Textual TUI、跨机器全新安装、长期运行/断网恢复仍需对应环境验收。

结论：统一源码通过上述本机 CPU、前端逻辑和合成网络回归，可作为 main 的整合版本；不将这些结果等同于生产级全平台认证或重新测得的 GPU 加速成绩。
