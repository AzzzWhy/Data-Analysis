# GUI 收尾验收（2026-09-29，guiv2 本地开发版）

本记录针对 guiv2 工作台，不代表已经合入 main，也不代表跨平台生产发布验收。各阶段记录按时间追加；SSH 表单认证的新验收见文末，前面的系统终端限制是早期阶段的状态。

## 本轮修复

- 普通分析、驻留分析与 auto 返回结构统一展示：概况、完整摘要、异常、相关性、分组明细。
- 结果失败/为空时不残留上一次数值；异常计数明确是各列合计，不冒充去重异常行数。
- API Key 可仅保存在后端进程内存；换服务商清除旧凭据，重启遗忘，不回传浏览器。
- GUI 对话开启已有的单步热工作复用；设置偏好不清对话历史。
- 运行期间模型设置、释放、重置不能抢占任务；启动时固定 Agent 实例，失败也收尾。
- 网关每个远程请求均校验本地登录，不能用伪造模式 Cookie 绕过；SSH 会话 Cookie 按连接代际隔离。
- 登录页不再显示随机性能数据，无法读取后端状态时不误判为首次设置。
- SSH 断线可恢复到本机；连接弹窗具有焦点循环，关闭后不再后台自动切换。

## 自动回归

```text
python -B agent/gui_test.py                  124 checks PASS（含真实 Edge 渲染探针）
python -B agent/gui_concurrency_test.py -v   8 tests PASS
python -B agent/frontend_gateway_test.py    PASS
node agent/login_page_test.js               51 checks PASS
node agent/gui_result_contract_test.js      49 checks PASS（结果、状态及交互契约）
```

JS 契约测试使用无依赖 DOM harness，不能替代真实浏览器视觉验收。
网关 SSH 启停生命周期使用替身测试；本轮真实远程访问使用已建立的系统 SSH 隧道。

## 本机 8877 → GB10 → 本地模型真实对话

浏览器页面来自本机，数据与计算都在 GB10；模型为已部署的 `qwen3.6-35b-a3b-fp8`。
无鉴权模型使用 `local` 占位值且不勾选保存；没有下载新模型或在仓库写入密钥。
输入为 `power_clean.csv`，110.3 MB，2,075,259 行。

| 对话步骤 | 实际调用/引擎 | 可核验结果 |
| --- | --- | --- |
| 统计摘要 | analyze_dataset / pandas | Global_active_power 有效值 2,049,280；缺失 25,979；均值 1.0916150365 |
| GPU 异常检测 | dataset_session open + analyze / cuDF | IQR 异常值 94,907；有效值占比 4.6312% |
| 下一轮重新打开并复用 | dataset_session / cuDF warm cache | 工具返回未重新读盘；打开调用 0.017 s，摘要调用 0.077 s；摘要数值一致 |
| 对话生成报告 | export_deliverables / pandas | 新目录 gui_finish_report_20260929 中生成中文 report.md 和 3 张 SVG；浏览器确认 3 张图片均成功加载 |

上述耗时为工具调用耗时，不含整轮模型思考，也不是重复性能基准；本轮不据此宣称加速比。
Agent 每轮结束会关闭活动会话，但可保留受限热缓存；本次实际保留一帧 161.9 MB。
模型自然语言可能称旧会话仍开启，判断真实生命周期应以工作进程返回的会话/缓存卡片和执行记录为准。

## 尚未完成的发布边界

- 最新 main 的统一调度/校准/批任务内核尚未整体合入 guiv2；不能称为最新内核的最终发行版。
- 本轮未实际完成“GUI 创建全新 SSH 连接 → 用户在系统窗口验证”的整条交互路径；已有隧道访问已实测。
- 需要独立的长时间运行、跨机器安装和多浏览器验收，才能称成熟发布版。
- 本轮修改未提交、未推送；部署测试只更新隔离的 GB10 GUI 后端，并保留旧文件备份。

## b9：真实终态与任务恢复

- `Agent.run()` 保持字符串返回兼容 CLI/TUI，另外提供结构化 `last_run_outcome`。GUI 不通过回答文案猜成功；区分成功、失败、部分完成和未知状态。模型 API 错误、空回答、工具错误和清理失败均有测试。
- `/api/jobs` 只读取已鉴权的本后端任务注册表，不访问数据、不启动计算。最多保留 20 项任务，每项最多 200 条事件；实例 ID 在启动或重置时更换。
- SSE 输出单调事件序号，支持 `Last-Event-ID` / `after` 恢复；网关透传游标。断线不会取消任务或自动再执行。事件超出缓冲时明确提示截断，不伪造缺失结果。
- 刷新后优先接回活动任务；本页书签只保存后端实例 ID 和任务 ID，不在浏览器保存提问、路径、密钥或事件正文。历史按钮仅回放已有结果。
- 本轮仍不提供跨后端重启的磁盘任务历史、任务取消或跨机器恢复；也未合并最新 main 内核。任务成功只代表本次调用按协议结束，不代表模型结论经过独立正确性审查。

新增隔离回归命令（模型/worker 边界使用替身，HTTP、SSE、Agent 循环为产品代码）：

```text
python -B agent/agent_run_outcome_test.py -v  16 tests PASS
python -B agent/gui_job_recovery_test.py -v   17 tests PASS
python -B agent/gui_outcome_bridge_test.py -v 4 tests PASS
node agent/gui_job_recovery_test.js          34 cases PASS
```

隔离测试不使用真实 API Key，也不声称替代真实模型或 GB10 性能基准。

本轮既有 124 项 GUI 检查（含真实 Edge 渲染探针）、8 项并发检查、网关回归、结果契约与 51 项登录检查均通过；前端整合后再次通过 124 项 GUI 检查。

### b9 真实页面验收

- 本机 `8877` 网关、GB10 隔离后端与已有 Qwen 本地模型，均为真实调用。
- 故意使用不存在的分组列，后端确实返回列校验错误；GUI 状态及历史保持“失败”，没有被结束事件改为成功。
- 请求 GPU 打开 `power_clean.csv` 并分析 Global_active_power。在第一轮 cuDF 打开完成、模型仍在运行时刷新页面；刷新后接回同一任务，继续收到第 2 轮摘要、第 3 轮离群点、第 4 轮关闭及最终回答。
- 总行数 2,075,259；有效值 2,049,280；均值 1.0916150365（模型回答四舍五入为 1.092）；离群点 94,907。GPU 工具步骤摘要 0.115 s、离群点 0.051 s，均不含模型时间，不是性能加速比基准。
- 在成功/失败历史之间切换能回放对应原结果，任务列表始终只有两项；没有重新执行计算。浏览器未记录 JavaScript error。

## SSH 表单认证与请求来源修复

- 网关改为管理进程内 SSH 连接，不再依赖弹出系统终端。表单支持密码、SSH agent/默认密钥和指定私钥（可带口令）。可解析 URI 和 `ssh -p PORT USER@HOST`，不执行粘贴的命令。
- SSH 主机校验读取本机 known_hosts；未知指纹先确认再认证，信任只对本次连接有效。冲突拒绝连接，不覆盖已知指纹。不在配置、日志、命令行或浏览器存储保存 SSH 凭据；清理认证后的凭据引用不等于保证物理内存擦除。
- 本机连接变更要求登录及会话 CSRF 值。回环别名/嵌入页面不再仅因 Origin 字符串差异被挡住；任意外站来源即便持有令牌也拒绝，不关闭跨站防护。只有服务端明确报告认证尚未执行的 CSRF 拒绝，前端才可刷新令牌后重试一次。
- 单独显示当前服务来源、候选连接、SSH 认证与候选 API 状态。连接新候选不会自动切换、不会用旧隧道的成功掩盖新失败；只有显式“使用远程”才替换目标。认证令牌按连接身份和代际隔离，即便端口数字重用也不转发旧 Cookie。
- 取消、超时、DNS 晚返回和断线有代际屏障，关闭托管转发不杀已有外部 SSH 进程。远端服务需要预先启动，本轮没有添加远程命令执行能力。

### 自动验收

```text
python -B agent/managed_ssh_test.py -v              15 tests PASS
python -B agent/managed_ssh_integration_test.py -v  13 tests PASS
python -B agent/frontend_gateway_test.py -v        15 tests PASS
node agent/backend_switch_test.js                 37 cases PASS
```

其中 13 项为真实回环 SSH 服务器：临时生成账号/密钥/known_hosts，测试密码、加密私钥、指纹确认前无认证、指纹冲突、TCP 转发、空闲 SSE、断开已有套接字；不使用真实 GB10 密码。网关测试使用真实 HTTP 和合成后端/SSH 状态边界；JS 为无依赖 DOM harness，不替代浏览器视觉验收。

既有 GUI 124 项、并发 8 项、Agent 终态 16 项、任务恢复 Python 17 项和 JS 34 项、桥接 4 项、结果 JS 51 项、登录 JS 51 项重新通过。首次沙箱内 GUI 检查的 Edge 子进程被环境限制，获准在沙箱外重跑后 124 项（含真实 Edge 渲染）全部通过。

### 本机 8877 到 GB10 的真实验收

- 重启的只有本机 8877 网关，旧外部 SSH 隧道与 GB10 服务保留；重新完成本机工作台登录。
- 在新表单粘贴简短 SSH 命令，选择密码认证，用用户既有账号完成一次新托管 SSH 连接。已知主机校验通过，窗口未再出现原来的跨来源报错。
- 先观察到“当前正在使用：外部隧道 / 候选：网关管理 SSH”；明确点击“使用远程”后，当前来源变成“网关管理 SSH”。不是用旧隧道的可达状态冒充新连接成功。提交后密码框为空。
- 通过已激活的新连接提交 `power_clean.csv` 统计摘要，真实扫描 2,075,259 行。此次即时分析的实际引擎是 pandas，后端耗时约 0.730 秒；Global_active_power 有效值 2,049,280、均值 1.0916150365。任务历史记录成功，结果可回放；这是连通性/结果验证，不是 GPU 性能基准。
- 本次还发现既有即时分析表单的“强制 GPU”不传入 `analyze_dataset`（该接口只支持 force_cpu）；其勾选不能作为实际 GPU 证据。强制 GPU 已有能力在 dataset_session 的 open 路径。这是独立 UI 能力提示欠缺，此次 SSH 修复未扩展分析接口。
- 真实 GB10 只验收密码认证；私钥和主机指纹拒绝/冲突等异常路径由上述合成 SSH 服务器覆盖，不宣称已在 GB10 上逐一尝试。

此轮不提交、不推送，不更新 GB10 后端代码；仍需跨机器安装、长时间网络故障等发布验收。

## b10：重置后的访问门、嵌入浏览器来源与本地模型说明

- 修复本机密码重置后回到免登录工作台：8877 网关缺少密码时始终要求首次设置；明确重置会持久保存不含凭据的 `setup_required` 标记，因此重启不会撤销设置要求。旧密码摘要格式仍兼容；启动参数 `--token` 的密码不会被界面重置删除。
- 重置要求本机登录、受信任来源与会话 CSRF 值；远端重置始终拒绝。前端先核对当前计算位置，只有明确成功才跳转；网络超时、失败、未知结果不自动重试，并恢复请求按钮状态。远端/任务忙的独立保护不会被另一请求解除。
- 远端模式旁明确写出重置禁用原因，以及先选择“计算连接 → 使用本机”的操作。普通退出登录不擦除配置。窄屏设置窗口可滚动，重置说明和按钮改为纵向布局。
- 针对曾记录到的回环 Origin 缺少端口，新增限定兼容：仍须正确的本机 Host/端口、有效登录和 CSRF 值；显式不同端口、外站、伪造 Host 不予放行，不启用跨域读取。
- 未登录时旧远端模式 Cookie 不再把本机密码设置页标成远端；仅纠正该浏览器的模式，不切断其他浏览器的远端连接。
- 模型表单改为“模型连接设置 / 模型服务地址 / 模型服务访问密钥”。中英文帮助说明 OpenAI 兼容本地服务、无鉴权时 `local` 占位、选定后端访问地址及远端模式的回环地址含义。旧远端翻译表缺少新字段时也有英文兜底。本轮不改变模型配置格式、不自动保存密钥、不新增直接加载权重或原生 Ollama `/api/chat` 协议。

### b10 自动验收

```text
python -B agent/gui_reset_gate_test.py -v        16 tests PASS
python -B agent/frontend_gateway_test.py -v      21 tests PASS
node agent/gui_reset_test.js                    23 cases PASS
node agent/backend_switch_test.js               41 cases PASS
node agent/gui_result_contract_test.js          51 cases PASS
node agent/gui_job_recovery_test.js             34 cases PASS
node agent/login_page_test.js                   51 checks PASS
python -B agent/gui_concurrency_test.py -v         8 tests PASS
python -B agent/gui_job_recovery_test.py -v       17 tests PASS
python -B agent/gui_outcome_bridge_test.py -v      4 tests PASS
python -B agent/agent_run_outcome_test.py -v      16 tests PASS
python -B agent/managed_ssh_test.py -v            15 tests PASS
python -B agent/managed_ssh_integration_test.py -v 13 tests PASS
python -B agent/gui_test.py                     124 checks PASS, two consecutive runs
```

以上按各套件计数合计 434 项，不把两次完整复跑重复计入。完整 GUI 套件包含真实 Edge 渲染。首轮曾出现登录子进程的 urllib 连接异常（123/124）；未复现，不能声称原因已经查明。另修正测试子进程未显式传入隔离环境的问题，并保留有界异常尾部，随后连续两轮 124/124 通过。隔离重置/SSH 用临时配置与合成服务，不操作真实用户的重置或新密码。

### b10 本机实际检查

- Codex 页面实际显示远端模型服务 `http://127.0.0.1:8000/v1`、新增模型说明和远端重置禁用原因。未按下用户工作台的真实重置按钮；Chrome 的重置回跳通过隔离 HTTP/JS 回归覆盖，本轮不声称已经人工完成 Chrome 全流程验收。
- 仅重启本机 8877 网关；现有外部隧道与 GB10 服务保留，旧网关进程内托管 SSH 连接随重启结束。已在真实 Codex 页面看到 `guiv2 · b10`、“计算连接 · 本机”和“设置访问密码”；新密码由用户自行完成输入和提交。
- 重启后只读检查 GB10 返回 `qwen3.6-35b-a3b-fp8`、模型地址 `http://127.0.0.1:8000/v1`，`config_ready` 和 `agent_available` 均为 true。本轮未重新进行模型推理或性能测试，不能把配置可用性当作本轮推理基准。
- 未提交、未推送 GitHub，未更新 GB10 后端代码，未再次清空用户配置或数据文件。

### guiv2 推送前复测状态（2026-09-29）

用户选择先把当前开发内容推到 `guiv2`，不合并 `main`，后续继续开发。上文各次验收是当时记录，不代表当前所有复跑始终通过。

- 完整 GUI 再次 124/124 通过（包含 Edge 渲染），重置门禁 16/16、前端重置与模型帮助 23/23、SSH 表单交互 41/41 通过。
- 网关套件本次连续两轮均为 20/21，`test_foreign_origin_and_foreign_host_rejected_even_with_nonce` 在客户端读取 HTTP 状态前出现 Windows `ConnectionAbortedError / WinError 10053`，未取得预期的拒绝响应。原因尚未确认；不能把断连当作已验证的 HTTP 403，也没有通过放宽来源检查或忽略异常来让测试变绿。该项保留为开发分支待排查问题。
- 用户提供的 Chrome 截图确认 `b10` 本机密码设置页能够加载；自动化控制 Chrome 连接持续超时，登录后的 Chrome 交互和完整重置流程仍未完成实测。不以 Edge 测试或 Codex 页面代替 Chrome 全流程验收。
- 此次只发布开发分支快照，不代表已完成稳定发行验收，也不部署 GB10 后端。
