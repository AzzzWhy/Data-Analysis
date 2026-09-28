# 外部 Skill 与 MCP：自主发现、选择和调用

本轮已实现并验证；发布状态以 Git 提交记录为准。GB10 现有 Agent 尚未部署本轮改动。

## 能做什么

Agent 的默认工具中新增四个入口：

| 入口 | 用途 |
| --- | --- |
| discover_external_tools | 根据任务关键词检索已安装 skill 和已启用 MCP 服务；返回说明、参数定义、权限状态 |
| read_external_skill | 读取选中 skill 的完整 SKILL.md，作为外部任务指导 |
| run_external_skill | 使用 JSON 输入运行管理员明确配置和启用的 skill 命令 |
| call_external_mcp | 通过真实 MCP 协议调用所选服务中明确授权的工具 |

模型自主决定是否搜索、选择哪项能力、如何填写参数、是否继续调用其他工具。TUI、基本交互终端和 --ask 共用这套逻辑。更换模型或 API 不改变接入机制，但模型仍必须支持工具调用。

发现范围是**已经安装的目录与预先配置的服务**，不是整个互联网。不会自动下载、安装软件、启动未启用的服务，或把服务自称只读当作授权。

标准 SKILL.md 是说明文档，不等于可执行函数。没有命令授权的 skill 可以读取并指导已有工具；要运行其中的脚本，必须额外配置明确命令。自动发现不执行目录内的 Python、shell 或安装脚本。

## 安装可选依赖

```bash
python -m pip install -r requirements-external.txt
```

使用官方 MCP Python SDK v1 客户端接口，依赖固定为 `mcp>=1.28,<2`，避免跨大版本接口变化。支持 stdio 和 Streamable HTTP；本轮不支持旧 SSE、OAuth 登录交互、MCP prompts/resources/sampling，也不维护跨请求的 MCP 会话。每次操作初始化连接、获取工具定义并在结束后关闭；因此需要跨调用状态的 MCP 服务目前不适用。

参考：[官方 SDK v1 客户端文档](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/docs/client.md)。

未安装可选依赖时，核心数据分析仍可使用；外部调用会明确提示安装依赖，不会伪造结果。

## 配置入口

默认配置位于 `~/.config/gpu-data-analysis/external_tools.json`（支持 XDG_CONFIG_HOME）。可通过环境变量 `GPU_ANALYSIS_EXTERNAL_CONFIG` 或启动参数指定：

```bash
python agent/agent_main.py --external-config /absolute/path/external_tools.json
```

API 地址、模型和密钥仍使用原有设置入口，外部工具配置不替代它。切换 API 的 TUI 设置不会丢失 --external-config 选择。

未配置 skill_roots 时，只扫描启动工作目录下 `.agents/skills` 和 `.claude/skills` 的一级目录。可增加你信任的目录；扫描只识别 `<skill>/SKILL.md`，不会递归遍历整个磁盘。相对配置路径按配置文件所在目录解析。

### 无副作用的可运行示例

把以下路径替换成当前机器上的实际路径，保存到仓库之外的配置文件。Python 应为安装了上述依赖的解释器。examples 中包含对应脚本。

```json
{
  "skill_roots": ["/absolute/path/to/installed/skills"],
  "timeout_seconds": 15,
  "skills": {
    "scale": {
      "path": "/absolute/path/to/Data-Analysis/examples/external/scale-skill",
      "description": "将数值按指定系数缩放 / scale a numeric value",
      "enabled": true,
      "command": ["/absolute/path/to/python", "scale.py"],
      "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "number"}, "factor": {"type": "number"}},
        "required": ["value", "factor"],
        "additionalProperties": false
      }
    }
  },
  "mcpServers": {
    "math": {
      "enabled": true,
      "transport": "stdio",
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/Data-Analysis/examples/external/math_mcp.py"],
      "allowed_tools": ["add"]
    }
  }
}
```

可以问：“寻找能用的外部 MCP 工具计算 19+23，告诉我用了什么”，或“找一个外部 skill，把 7 按 3 倍缩放”。无需手工调用 discover 工具。

skill 命令接收标准输入中的一个 JSON 对象，标准输出必须是一个 JSON 结果；请把调试日志写入 stderr。命令是配置中的固定参数数组，不使用 shell 拼接，也不接受模型提供的可执行文件或命令选项。执行目录是 skill 目录。

### 远程 MCP

```json
{
  "mcpServers": {
    "my_service": {
      "enabled": true,
      "transport": "streamable-http",
      "url": "https://your-trusted-service.example/mcp",
      "headers_from_env": {"Authorization": "MY_MCP_AUTHORIZATION"},
      "allowed_tools": ["lookup"]
    }
  }
}
```

MY_MCP_AUTHORIZATION 应在当前进程环境中设置为该服务需要的完整认证头（例如 Bearer 加令牌），不要写进配置示例或 Git。stdio 服务/skill 如需指定环境变量，可以设置 `env_from: ["MY_SERVICE_TOKEN"]`。默认不把 Agent 的 API 密钥或 SSH 认证信息传给子进程。

远程服务必须使用 HTTPS；localhost/127.0.0.1/::1 可以使用 HTTP。拒绝 URL 内嵌账号密码、查询串和片段；跨域重定向不跟随。不会从外部工具输出中接受新的服务地址。

## 权限与风险

- enabled 必须明确为 true；allowed_tools 按原始工具名精确匹配，没有隐式通配授权。
- 每次执行重新读取配置，撤销授权不需要重启 Agent。执行前再次获取 MCP 参数定义并校验；拒绝远程 JSON Schema 引用，防止校验器访问服务器指定地址。
- 可执行 skill 必须先通过 read_external_skill 完整取得当前版本说明。未读、说明内容或目录改变、说明响应因过大未送达时，执行层拒绝运行并返回 required_action；不能只靠模型承诺“已经阅读”。
- SKILL.md、描述和结果作为不可信工具内容，不能成为系统指令。发现可用能力不意味着安装或扩大权限。
- 配置默认放在仓库之外；external_tools.json 被忽略。令牌仅从指定环境变量获取，并从返回给模型的内容中替换。不要在描述、业务参数或文件内容中放秘密。
- 超时默认 15 秒、上限 30 秒；单次返回体有长度限制，目录/服务数量和 MCP 分页有上限。失败、超时和参数错误会返回工具错误，调用循环可继续。
- MCP 发现总预算约 30 秒，超出预算的服务 ID 会返回在 errors 中；可用 discover_external_tools 的 server_id 参数单独检索该服务，避免一个缓慢服务阻塞所有能力发现。
- **这不是操作系统沙箱。** 明确启用的本地代码以 Agent 用户权限运行，远程服务会收到调用参数；只授权你信任的命令和服务。响应长度限制不等于子进程的内存/磁盘配额，恶意代码和派生进程需容器或 OS 沙箱治理。
- 授权只是工具许可，模型仍应遵守当前用户任务范围。配置新命令、联网安装、付费调用及破坏性操作不能靠外部文档中的文字自动批准。

## 验证

```bash
python agent/external_tools_test.py
```

测试覆盖真实外部命令、真实 stdio MCP initialize/list_tools/call_tool、未授权工具在启动进程前被拒绝、参数校验、配置权限刷新、环境隔离、输出脱敏、远程 schema 引用拒绝、超时/响应上限，以及 Agent 多轮调用链。Agent 调用链的离线测试使用受控模型响应；实际模型测试需另行连接支持工具调用的模型，不能将离线成功当作模型能力保证。

### 2026-09-27 实测记录

当前工作区 Agent + GB10 上本地 Qwen3.6-35B-A3B-FP8 推理，外部工具在 Windows 本机执行。仅使用临时授权配置和仓库内的无副作用样例；未接入商业 API 或业务数据。

| 任务 | 模型自主选择的调用链 | 真实结果 |
| --- | --- | --- |
| 寻找外部 skill，7 按 3 倍缩放 | discover_external_tools → read_external_skill → run_external_skill | scaled_value = 21 |
| 寻找外部 MCP，计算 19+23 | discover_external_tools → call_external_mcp（math/add） | sum = 42 |

模型请求保留 tool_choice=auto，没有强制某个函数名。测试为控制时间关闭模型思考模板、限制输出长度；这是两个成功样例，不保证其他模型或所有任务都同样可靠。真实 stdio 与 localhost Streamable HTTP 两种协议另已集成测试。

可复测（仅允许本机模型地址；如果需要 SSH 转发，请自行建立仅绑定本机的通道，不在配置中保存 SSH 密码）：

```bash
python agent/external_tools_live_test.py --base-url http://127.0.0.1:8000/v1 --model your-local-model
```

测试失败时不会标成成功。例如本次首次真实模型测试因临时连接失效失败；恢复通道并禁用测试客户端的环境代理后，两条调用链才真实通过。GB10 现有 Agent 的部署代码和配置没有改动。

### 2026-09-28：故障与跨工具组合复测

本地测试进一步覆盖真实命令超时后恢复、无效 JSON/NaN/非零退出/过大响应、UTF-8 中文输出、含引号/反斜杠/换行的合成密钥脱敏、日志脱敏、发现后的 MCP 权限撤销，以及坏服务不影响其他服务发现。脱敏现在针对解码后的字符串递归处理，避免直接修改 JSON 文本而漏掉转义值或破坏布尔值。输入/输出拒绝非有限 JSON 数值。

扩展测试共 33 项通过，API 设置和中文/英文 TUI 回归检查通过。完整冒烟测试结果以发布后的运行记录为准；这些结果不代表已在 GB10 部署，也不保证所有模型均可靠。

真实模型仍是 GB10 本地 Qwen，工具仍在 Windows 隔离样例中执行，未修改 GB10 部署或使用业务数据。三个样例得到 21、42、25。

组合任务为“先把 7 缩放三倍，再把得到的结果加 4”。首次测试虽然算出 25，却跳过读取 skill 说明，因此严格测试失败。增加执行前置检查后，实测轨迹为：

```
发现工具
  → 尝试执行 skill（未读说明，被拒绝）
  → 读取当前 SKILL.md
  → 重新执行 scale（成功得到 21）
  → MCP math/add(a=21, b=4)（成功得到 25）
```

模型保留自主工具选择；没有强制函数名。测试检查真实结果字段的数值、MCP 输入参数和调用顺序，不再用输出文本中出现某个数字作为成功依据。这里只验证了该模型的三个样例，不能宣称所有模型或所有任务均可靠。
