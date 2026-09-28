# `jsdom/` — 无浏览器机器上的界面验证器

这些脚本在**没有 Chromium/Edge 的机器**（GB10 节点就是）上驱动工作台前端，用 node 的
`jsdom` 做 DOM。它们证明的东西比看起来少，所以这里把"能证明什么"和"怎么跑"一起写清。

来源：`995bda6` 之后节点侧交付（`hks-gui-code-995bda6p5`），随其代码一起进仓库。

## 前置

```bash
npm install jsdom@24        # v24 是能加载在 Node 18 上的最后一条线；更新的需要 Node 20+/22
export NODE_PATH=~/hks-run/jsdom-probe/node_modules
```

验证器需要一个**已经在跑的工作台**，用 `BASE` 指过去：

```bash
GPU_ANALYSIS_CONFIG=<一个临时 connection.json> ~/miniforge3/envs/rapids-cudf/bin/python \
    agent/gui.py --port 8790 &
```

`GPU_ANALYSIS_CONFIG` 不是可选项：不设它，这些脚本会读写你真正的
`~/.config/gpu-data-analysis/connection.json`。

## 跑什么

| 命令 | 验的东西 |
| :--- | :--- |
| `BASE=http://127.0.0.1:8790 node jsdom/verify-demo.js` | 键位、右栏抽屉、图表标签页、状态脉冲 |
| `BASE=http://127.0.0.1:8790 node jsdom/verify-readiness.js` | 就绪状态：缺哪项点名、中英切换、被拒时的话术 |
| `BASE=http://127.0.0.1:8790 node jsdom/verify-n1n2.js` | 设置面板回填、断流后解锁 |
| `PAGE=agent/gui/index.html node jsdom/outline.js` | 当前 DOM 结构与控件计数（改了 `index.html` 先看这个） |
| `python3 agent/renderer_probe.py` | 渲染器断言。有浏览器走 Chromium，没有则降级到 `gui/renderer_probe_jsdom.js` |

## 一个会咬两次的坑：必须显式写 `Content-Length`

node 的 `socket.write()` 走 chunked 编码，而 `gui.py` 是 HTTP/1.0、按 `Content-Length`
读请求体（`agent/gui.py:705`）。所以任何 PATCH/POST 若不带这个头，服务端读到的是**空 body**。

空 body 之所以危险，是因为**一个空的 PATCH 在语义上是合法 no-op**——早先的 `_body()` 用一句
`except: return {}` 把四种完全不同的情况压成同一种：读不了的 chunked body、垃圾
`Content-Length`、非对象 JSON、根本解析不了的 JSON。四种都以"空补丁"的身份被回答
`ok: true`，而服务端其实**从没看见那个 body**。实测过一次：chunked 的
`PATCH {"language":"en"}` 返回成功、语言原封不动，界面上没有任何异样
（见 `agent/gui.py:686-703`）。现在这几种各自返回 `400` 并说明原因。

因此这些验证器都自己做两件事：把 `fetch` 桥到 node 的 `http`，并**显式写
`Content-Length`**。写新的验证器时必须照做，否则你测的是"空补丁成功"这件假事。

## 这些脚本证明不了什么

jsdom 只覆盖 DOM 结构与事件语义。以下五项**只有真浏览器能证**，节点上没有，必须人眼看：

1. CSP 是否真被浏览器执行（`gui_test.py` 只能证"CSP 头按预期发出去了"）
2. 布局与配色
3. Chromium 的 HTML 解析器行为
4. `EventSource` 的真实重连
5. SVG 的实际绘制

`renderer_probe.py` 在 jsdom 缺席时打印 `SKIPPED: jsdom is not installed, so the renderer
assertions never ran.` 并以退出码 **2** 结束——**2 不是通过**。`gui_test.py` 会据此额外打一行：
`gui/app.js was NEVER executed on this machine`。看到那行时，不要把整轮当成"渲染器已验证"。
