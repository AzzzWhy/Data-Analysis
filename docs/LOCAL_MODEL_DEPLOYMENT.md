# 本地模型部署与 Agent 优化说明

## 1. 部署边界

Agent 应用、分析引擎和模型服务是三个不同部分。GB10 可以同时运行模型服务与 CPU/GPU 分析，电脑上的浏览器只连接本机网关，再通过 SSH 访问 GB10 后端。模型权重、KV cache、数据帧和其他程序会竞争统一内存，不能把全部内存分别分配给模型和分析引擎。

历史验收连接的本地服务暴露 `http://127.0.0.1:8000/v1`，返回模型 ID `qwen3.6-35b-a3b-fp8`，并完成真实工具调用。见 [验收记录](FINAL_CORE_DEPLOYMENT.md)。仓库没有完整保存该服务的镜像摘要、权重来源/revision、启动参数和量化制作过程，所以**不能声称只凭这个 ID 就能复建完全相同的推理环境**，也不将名称里的 FP8 当作本项目完成量化的证据。

## 2. 在 GB10 上准备分析环境

先按 [NVIDIA RAPIDS 安装指南](https://docs.nvidia.com/datascience/install/) 选择与 ARM64、驱动、CUDA 和 Python 匹配的安装方式。下面从已经可用的 RAPIDS 环境开始；环境名是示例，不会自动安装 cuDF：

```bash
conda activate rapids-cudf
cd /path/to/Data-Analysis
python -m pip install -r requirements.txt -r requirements-gui.txt
python -c "import cudf, cupy, pandas; print(cudf.__version__, cupy.__version__, pandas.__version__)"
python skills/cudf-analytics/scripts/smoke_test.py --require-gpu
```

根据自己的安装设置 `CUDA_PATH`，不要照搬另一台机器的目录。CPU-only 环境可以验证应用逻辑，但不能替代 `--require-gpu` 验收。

## 3. 准备本地模型服务

**已有服务时直接复用，不要再次下载模型或占用同一端口。** 查看服务的 `/v1/models`，确认实际模型 ID；开启鉴权时按服务要求提供凭据，不把密钥放进共享命令、截图或仓库。

```bash
# 以下针对本机无鉴权服务，只读检查
curl --fail http://127.0.0.1:8000/v1/models
```

从空白机器安装时，按 [NVIDIA DGX Spark vLLM 部署指南](https://build.nvidia.com/spark/vllm/instructions) 选择适配硬件和目标模型的 recipe、镜像与工具调用配置。该指南是安装参考，**不是本项目历史服务使用 vLLM 的证明**。下载前核对模型许可、权重来源和容量；记录模型 revision、推理引擎/镜像版本、上下文长度、并发与内存预算。容器端口仅发布到 `127.0.0.1`，不要直接公开无鉴权接口。

不要只运行普通聊天就判定 Agent 适配成功：服务需要支持 `/v1/chat/completions` 的 `tools`、自动工具选择、合法参数 JSON，以及工具结果回传后的最终回答。具体 tool parser / chat template 随模型和引擎而变，按对应 recipe 配置；没有证据时不能给历史模型编造启动参数。

如果使用 Ollama，其 [OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility) 可以作为另一种接入方式；本机常见地址为 `http://127.0.0.1:11434/v1`。模型需要实际支持工具调用。这是可选适配说明，未作为此次已复测的 GB10 部署成果。

## 4. 接入 Agent / 工作台

在 GB10 项目目录、分析 Python 环境中，针对上述已有无鉴权服务设置本次终端的连接：

```bash
export GPU_API_BASE_URL=http://127.0.0.1:8000/v1
export GPU_API_KEY=local
python agent/agent_main.py --plain --model qwen3.6-35b-a3b-fp8 --ask "请调用分析工具，计算 benchmark/portability/fixture.csv 的 revenue 描述统计，并说明实际引擎。"
```

模型 ID 必须换成 `/v1/models` 实际返回的值。`local` 只用于无鉴权服务的客户端占位；启用鉴权的服务必须用真实访问密钥，推荐通过工作台设置入口输入，不提交到仓库。上面没有新建或修改模型权重。

完整远端工作台后端：

```bash
# 同一分析环境与终端，继承上面的地址和占位密钥
python agent/gui.py --host 127.0.0.1 --port 8765 --model qwen3.6-35b-a3b-fp8
```

电脑端按 [README](../README.md) 启动 `python agent/frontend_gateway.py --port 8877`，在计算连接中填写自己的 SSH 地址和远端后端端口 `8765`。后端的 `127.0.0.1` 指 GB10，不是电脑；SSH 登录密码不是模型密钥，也不是工作台访问密码。

`--model` 是进程启动覆盖值。如需在工作台中自由切换模型，不传该参数，改在后端模型设置中选择并保存实际 ID。

## 5. 本项目如何优化大模型的使用

本项目已实施的是 **Agent 调用与上下文层面的优化**，不是训练新的语言模型或改写推理内核。

| 方式 | 实现位置 | 作用与限制 |
| --- | --- | --- |
| 计算外置 | `agent/skills.py`、分析 Skill | 模型组织任务，工具计算全量统计；避免把整份文件塞进上下文。不代表送给模型的结果不含敏感信息 |
| 有界结果压缩 | `_compact`、`_compact_operation` | 限制列表和嵌套输出，标记截断；保留已受限的异常样例。没有统计值近似计算，但模型看到的是有界展示，不是全部原始记录 |
| 工具轮次约束 | `agent/agent_main.py` 的 `MAX_TOOL_ROUNDS`，默认 10 | 限制无休止调用；到达上限明确记录。过低会影响完成率，不等同于模型推理 tokens 上限 |
| 已知任务批次化 | `analyze_batch`、报表入口 | 可用一个工具请求完成多项分析；固定任务可完全绕过模型。未知探索问题仍可能需要多轮 |
| 热进程与会话复用 | `gpu_session.py`、工具层 | 减少分析启动与重读，不是 LLM 的 KV cache 优化，也不应计为模型 tokens/s 提升 |

模型参数侧的后续调优可围绕上下文、生成预算、并发和内存余量做同题对照；这些是**待测方案**，不是本项目已启用的统一参数。NVIDIA 的 [vLLM 部署参考](https://build.nvidia.com/spark/vllm/instructions) 说明了上下文和内存预算设置；项目不能直接把参考值当成混合分析负载的最优值。

没有实施或没有完整证据的内容包括：LoRA/全参数微调、蒸馏、自行 FP8/NVFP4 量化、TensorRT-LLM 推理优化、跨所有模型的最优路由。没有提供这些操作的加速倍数。历史模型对话总耗时与不足一秒的 GPU 分析耗时分开保存在 [核心验收](FINAL_CORE_DEPLOYMENT.md) 中。

## 6. 部署后的验收顺序

1. 核对模型列表；再验证真实工具调用，不能只检查 HTTP 200。
2. 对仓库小 fixture 完成描述统计，核对工具 JSON、行数、引擎和最终回答。
3. 对同一文件连续追问，检查会话复用与文件变更保护；关闭后检查会话释放。
4. 显式 GPU 测试与 CPU 基线核对数值，分别记录启动、读取、计算、模型规划和回答时间。
5. 在模型保持运行时测试分析内存准入；不能通过关闭保护或清理其他应用来伪造可用容量。

保存脱敏的模型 ID、软件版本、请求内容、工具轨迹、数值核对与计时；不保存密钥、SSH 登录信息或私有数据。本文档更新只做源码与文档检查，未在 GB10 安装模型、改动服务或重新完成以上真实推理验收。
