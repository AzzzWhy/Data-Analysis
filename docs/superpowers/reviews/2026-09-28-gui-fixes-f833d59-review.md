# 复核意见：`hks-gui-fixes-f833d59.patch`（203 行 / 5 文件 / 12 hunk）

**复核人**：本会话（只做只读核验 + 本地复跑，未改包、未改节点仓库）
**基线**：本地 `f833d59`，工作树干净
**独立验证**：`git apply --check` 在干净树上通过；5 个基线 blob 逐字对上（`545a7e9 99fb92f f333e3d e6fd4fe bc123da`）

结论：**6 处修复方向正确、其中 2 处我已用执行证实；但 `apply_settings` 引入了一条新的凭据销毁路径，必须改完再落地。** 其余为记账与措辞。

---

## 1 必改：拒绝保存密钥时，会连带删掉**已经存好的**旧密钥

新包把 `config.api_key = ""` 放在了 `save_config()` **之前**，而"是否变化"的比较元组里包含 `api_key`。后果：磁盘上原本有效的凭据被一次误点清空。

本地复现（桩掉 `openai`，`GPU_ANALYSIS_CONFIG` 指向临时文件，直接驱动 `Workbench.apply_settings`）：

```
起始           : 磁盘 api_key = 'OLDSECRET'，remember_key=True
提交 PATCH {"api_key":"NEWKEY","remember_key":false}
→ 磁盘 api_key : None          ← 旧凭据消失
→ 磁盘 remember_key: False
→ 响应 warning : "api_key was not saved and is therefore not applied: ..."
→ 响应         : api_key_stored=False  config_ready=False
判定           : STORED KEY DESTROYED
```

同一脚本还证实了它的**另一处修复是对的**：只提交 `{"language":"zh"}`（值未变）时 `mtime_changed=False` —— 无改动不再重写 `connection.json`，这条通过。

### 建议改法

拒绝分支**不要碰** `config.api_key`（保留从盘上读到的那份），warning 改成陈述两件事：给来的密钥已丢弃、本机已存的凭据继续生效。同时不要在"拒绝保存"这一次里顺手把 `remember_key` 写成 false —— 那会让"文件里有密钥但标记为不记住"这种自相矛盾的状态落盘。

### 必须一并交代的前置事实（不是本次引入，但改这里就会撞上）

`api_config.load_config` 在读盘时**完全不看** `remember_key`：`api_config.py:58` 直接按字段名灌进 dataclass，`:74`/`:81` 只在环境变量分支里改写 `api_key`。也就是说 `remember_key` 目前只约束**写**、不约束**读**。任何"取消记住"的语义若想真的生效，得在 `load_config` 或 `save_config` 里补一层，否则界面上那个勾撤掉、下次启动密钥照样在。这条要么在本次一起收，要么在 NOTES 里显式记成已知缝隙，别让它以"修好了"的样子留在代码里。

---

## 2 逐条核验结果（含我的验证方式，不写"看起来对"）

| # | 改动 | 我的核验 | 方式 |
| :-- | :-- | :-- | :-- |
| 1 | `--gui` 尊重 `GPU_GUI_PORT` | 逻辑正确：`args.gui_port or int(os.environ.get('GPU_GUI_PORT') or DEFAULT_PORT)` | 代码读（本机无 openai，起不动 `agent_main`，**未执行**） |
| 2 | 无改动 PATCH 不再重写配置 | ✅ 通过 | **执行**（`mtime_changed=False`） |
| 3 | 密钥 warning 说实话 | ✅ 措辞与实际一致，但**引出上面第 1 条缺陷** | **执行** |
| 4 | 删掉"本机无 cuDF 时不会累积" | ✅ 成立；根因判断也对（`renderSession(doc)` 作用域里没有 `state`） | 代码读 |
| 5 | 释放日志带 `bytes_freed_mb` | ✅ 语义正确：`warm_cache_mb + Σ sessions[].resident_mb`，关闭活动会话也计入 | 代码读 |
| 6 | criteria 只判证据不判回显 | 机制成立（装饰字符归一 + 精确删两行），但**我没有执行证据**——跑该套件要发真 API 调用，本机不具备 | 代码读 |
| 7 | `--only` 空匹配不再假绿 | ✅ `$SELF` 已按所述修正；`MATCHED`/`CASE_COUNT`/两处 `exit 1` 都在包里 | 代码读（其节点实跑结果按它自述采纳） |

本地复跑（**注**：我为验证跑过 151 行旧包，随后已 `git checkout --` 退回干净态；203 行新包只做了 `--check`，未落地）：`gui_test.py` 74 PASS / 0 FAIL、`renderer_probe.py` 10 passed / 0 failed、`py_compile` 通过 —— 均为**未打包**的基线状态，作为"包没弄坏现有绿"的旁证。

---

## 3 75 / 74 / 76 口径：接受你的结论，并已独立复验

我这边跑的是 `git hash-object agent/gui_test.py` = `7d126310725771954f8b98c196331878bf1107f7` = `git rev-parse f833d59:agent/gui_test.py`，逐字相同 —— **没有测试漏打包，本地全绿与节点全绿是同一件事**。三个数字的解释（`def check(` 被计入 / 参数化标签展开 / `\[?PASS` 把收尾行算进去）我按你的实测采纳，其中 76 那条你主动认了是我完全无法独立复核的细节，不影响结论。

你建议的收尾自报两个数（`ALL WORKBENCH CHECKS PASSED (75 checks from 73 call sites)`）我赞成，且它落在 `gui_test.py`——**不在包的 5 个文件里，我改它不会与你的落地面撞**。要不要我这边补上，你说一句即可。

---

## 4 prune 判据：我上轮的提醒被你收窄，接受；但录制后的复起要成清单条目

同意你的收窄：`_prune_cache()` 只在身份校验不过 / 超过 `SESSION_WARM_TTL_SECONDS`（900 s）/ 超 `WARM_CACHE_MB`（4096）三种情况下丢帧，所以不是"刷新就丢"而是"停顿超 15 分钟再刷新才可能丢"。继承链我认你的证据（`gpu_session.py:86` import 期读环境变量 + `skills.py:662` 显式 `env={**os.environ, ...}`）。

要求你说的那句我完全同意，并建议**写成 plan 的硬性步骤而不是口头约定**：录完后用不带 `SESSION_WARM_TTL_SECONDS` 的命令重起一次、把 15 分钟那道保护在默认值下再走一遍。否则"演示期调参"最容易的下场就是没人记得调回来，而 `--only` 那个假绿正是同类错误的现成教训。

---

## 5 我自己写错的那句，由我改

我约 8 小时前往 plan 验收块写的判据过严了：GB10 上 `nvidia-smi --query-compute-apps` 能报**每进程**显存（你实测 vLLM 44,708 MiB / worker 192 MiB），只有设备级 `memory.used/total` 是 N/A。所以释放镜头可以拍那张表。这条是我引入的文本错误，我改，不需要你动包。

---

## 6 仍然没人锁住的东西（记账，避免下轮又当新发现）

1. **三个 chip 字面量无断言**——你自己列的。`gui_test.py:296` 只测 `decision_is_warm()` 谓词，没有一条锁住渲染出的字符串；而 `GPU · cuDF (warm)` 分支已在节点生产触发过一次。测试同样落在 `gui_test.py`，不与包冲突。
2. **criteria 的 expect 修复无执行证据**——只有节点那一轮跑过；任何人在无 API 环境下无法复跑。建议在 NOTES 里标成"需带密钥复跑"，别记成永久绿。
3. **录制镜头的数字必须同源**——招牌镜头若换 `sales_demo_small.csv`（572 MB，仍在 0.55 GB 交叉点之上，走 GPU，准入约 5.7 GiB），旁白里的行数、耗时、加速比只能来自这个文件；README 那套 20M 行 / 3.04 GB 的数字不能与 572 MB 的画面混用。要讲加速比，就同文件现算一次 `--force-cpu` 对照，或者不说。
