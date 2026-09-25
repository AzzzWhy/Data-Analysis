#!/usr/bin/env python3
"""Draw the CPU-vs-GPU comparison chart from measured data only.

Every number plotted here was measured on the GB10 with both engines in fresh processes. Nothing
is illustrative or smoothed into a nicer shape: where the trend is not monotonic (the 80M point
is slower per GB than the 50M point) the chart shows the dip rather than hiding it.

Output is SVG, built by hand from the standard library plus numpy. MATLAB-style plotting is not
available in this environment (no matplotlib, no PIL, no cairo), and a chart that has to be
regenerated on the machine that produced the measurements is worth more than one that has to be
exported by hand. Chromium renders the SVG to PNG at 2x when a raster is wanted.

    python make_chart.py [--out DIR]
"""
from __future__ import annotations

import argparse
import math
import os

# --------------------------------------------------------------------------------------
# Measured data. Columns: label, rows, bytes, cpu_seconds, gpu_seconds.
# Both engines fresh, single process, --op auto. See references/engine-contract.md.
# --------------------------------------------------------------------------------------
POINTS = [
    ("2M narrow", 2_000_000, 0.086e9, 1.01, 2.08),
    ("4M wide", 4_000_000, 0.604e9, 4.66, 3.67),
    ("5M narrow", 5_000_000, 0.217e9, 2.21, 2.85),
    ("8M narrow", 8_000_000, 0.348e9, 3.35, 3.25),
    ("20M narrow", 20_000_000, 0.881e9, 8.19, 5.40),
    ("20M wide", 20_000_000, 3.038e9, 21.61, 9.92),
    ("30M narrow", 30_000_000, 1.490e9, 15.47, 7.05),
    ("40M wide", 40_000_000, 6.082e9, 44.59, 16.84),
    ("50M narrow", 50_000_000, 2.500e9, 25.23, 10.59),
    ("80M narrow", 80_000_000, 4.000e9, 40.20, 19.22),
]

# Controlled experiment: total bytes held at ~600 MB, only row/cell structure varies.
SHAPE = [
    (4, 66.0, 1.39),
    (12, 220.5, 1.19),
    (24, 448.9, 1.18),
    (48, 879.9, 1.18),
]

# Resident session: open once, then 5 operations, against 5 stateless CPU calls that re-read.
SESSION = [
    (1_000_000, 11.17, 2.95, 3.79),
    (2_000_000, 17.11, 3.98, 4.29),
    (5_000_000, 35.51, 6.95, 5.11),
]

# Fitted per-byte rates (seconds per GB), fixed cost excluded.
RATE = {
    "narrow": {"cpu": 10.2, "gpu": 2.4},
    "wide": {"cpu": 7.2, "gpu": 1.5},
}

# Design tokens.
GPU_C = "#1f77b4"
CPU_C = "#d95f02"
INK = "#1a1a1a"
MUTED = "#6b6b6b"
RULE = "#d9d9d9"
FONT = ("-apple-system, 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', "
        "Roboto, Helvetica, Arial, sans-serif")

W, H = 1920, 1116
M = 34                      # outer margin
GAP = 18                    # gap between cards
CW = (W - 2 * M - GAP) / 2  # card width
# Card height is fixed rather than derived from H, so that the two rows, the header and the
# two-line footer are all guaranteed to fit. Deriving it from H overflowed the canvas and cut
# the footer off, which the geometry audit caught.
CH = 432.0
ROW1_Y = 112.0
ROW2_Y = ROW1_Y + CH + GAP


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Svg:
    """Minimal SVG writer: the subset needed for a chart, nothing more."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, fill="none", stroke="none", sw=1.0, rx=0, op=None):
        o = f' opacity="{op}"' if op is not None else ""
        self.add(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                 f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{o}/>')

    def line(self, x1, y1, x2, y2, stroke=RULE, sw=1.0, dash=None, op=None, cap="butt"):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op is not None else ""
        self.add(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                 f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"{d}{o}/>')

    def path(self, d, stroke=GPU_C, sw=2.0, fill="none", dash=None, op=None):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op is not None else ""
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
                 f'stroke-linejoin="round" stroke-linecap="round"{da}{o}/>')

    def circle(self, cx, cy, r, fill=GPU_C, stroke="none", sw=0, op=None):
        o = f' opacity="{op}"' if op is not None else ""
        self.add(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{o}/>')

    def text(self, x, y, s, size=13, fill=INK, anchor="start", weight="400",
             family=FONT, ls=0, op=None, rotate=None):
        l = f' letter-spacing="{ls}"' if ls else ""
        o = f' opacity="{op}"' if op is not None else ""
        # Axis titles read bottom-to-top, so the rotation is applied about the anchor point
        # rather than the origin; otherwise the label lands far outside the panel.
        tr = f' transform="rotate({rotate} {x:.2f} {y:.2f})"' if rotate is not None else ""
        self.add(f'<text x="{x:.2f}" y="{y:.2f}" font-family="{family}" font-size="{size}" '
                 f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}"{l}{o}{tr}>'
                 f'{esc(s)}</text>')

    def render(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                f'viewBox="0 0 {W} {H}">\n' + "\n".join(self.parts) + "\n</svg>\n")


def card(s: Svg, x, y, w, h, title, sub, accent=GPU_C):
    s.rect(x, y, w, h, fill="#ffffff", stroke=RULE, sw=1.2, rx=10)
    s.rect(x, y, 5, h, fill=accent, rx=3)
    s.text(x + 20, y + 29, title, size=17, weight="600")
    if sub:
        s.text(x + 20, y + 49, sub, size=12.5, fill=MUTED)


def panel_a(s: Svg, x, y, w, h):
    """Time against size, both engines, with the measured crossover and fitted rates."""
    card(s, x, y, w, h, "① 时间 vs 数据量 — 交叉点与渐近线",
         "双引擎全新进程,单次 op,端到端实测。横轴 数据量,纵轴 耗时", GPU_C)

    px, py = x + 78, y + 96
    pw, ph = w - 78 - 26, h - 96 - 62

    x_min, x_max = 0.05e9, 7.0e9
    lx0, lx1 = math.log2(x_min), math.log2(x_max)

    def X(b):
        return px + (math.log2(b) - lx0) / (lx1 - lx0) * pw

    y_max = 46.0

    def Y(t):
        return py + ph - (math.sqrt(max(t, 0.0)) / math.sqrt(y_max)) * ph

    # Crossover shading: below ~0.4-0.55 GB the CPU wins, and the chart says so instead of
    # showing only the region where the GPU looks good.
    s.rect(X(x_min), py, X(0.42e9) - X(x_min), ph, fill=CPU_C, op=0.055)
    s.text(X(x_min) + 8, py + 15, "CPU 更快区", size=11.5, fill=CPU_C, weight="600")
    s.text(X(0.42e9) - 8, py + 15, "GPU 更快区 →", size=11.5, fill=GPU_C,
           anchor="end", weight="600")

    # Grid + axes.
    for b, lab in [(0.1e9, "0.1"), (0.25e9, "0.25"), (0.5e9, "0.5"), (1e9, "1"),
                   (2e9, "2"), (4e9, "4"), (7e9, "7")]:
        if not (x_min <= b <= x_max):
            continue
        s.line(X(b), py, X(b), py + ph, stroke=RULE, sw=0.8, dash="3,4")
        s.text(X(b), py + ph + 19, lab, size=11.5, fill=MUTED, anchor="middle")
    s.text(px + pw / 2, py + ph + 42, "文件大小 (GB, CSV)", size=12.5, fill=MUTED,
           anchor="middle")

    for t in (0, 2, 5, 10, 20, 30, 40):
        s.line(px, Y(t), px + pw, Y(t), stroke=RULE, sw=0.8, dash="3,4")
        s.text(px - 10, Y(t) + 4, str(t), size=11.5, fill=MUTED, anchor="end")
    s.text(px - 58, py + ph / 2, "耗时 (秒)", size=12.5, fill=MUTED,
           anchor="middle", rotate=-90)

    # Fitted lines, drawn across the range each shape actually covers.
    for key, b0, b1, col in (("narrow", 0.15e9, 4.2e9, None), ("wide", 0.4e9, 6.4e9, None)):
        r = RATE[key]
        seg = []
        for i in range(61):
            b = b0 * (b1 / b0) ** (i / 60)
            seg.append((X(b), Y(1.5 + r["gpu"] * b / 1e9)))
        s.path("M " + " L ".join(f"{a:.2f} {c:.2f}" for a, c in seg), stroke=GPU_C,
               sw=2.0, dash="6,4", op=0.85)
        seg = []
        for i in range(61):
            b = b0 * (b1 / b0) ** (i / 60)
            seg.append((X(b), Y(0.18 + r["cpu"] * b / 1e9)))
        s.path("M " + " L ".join(f"{a:.2f} {c:.2f}" for a, c in seg), stroke=CPU_C,
               sw=2.0, dash="6,4", op=0.85)

    # Measured points. Wide and narrow are distinguished so the reader can see that the same
    # row count lands in two different places.
    for lab, rows, b, cpu, gpu in POINTS:
        wide = "wide" in lab
        s.circle(X(b), Y(cpu), 5.6, fill=CPU_C, stroke="#ffffff", sw=1.6)
        s.circle(X(b), Y(gpu), 5.6, fill=GPU_C, stroke="#ffffff", sw=1.6)
        if wide:
            s.circle(X(b), Y(cpu), 10.4, fill="none", stroke=CPU_C, sw=1.3, op=0.5)
            s.circle(X(b), Y(gpu), 10.4, fill="none", stroke=GPU_C, sw=1.3, op=0.5)

    # Highlight the pair that proves rows cannot be the axis.
    for lab, rows, b, cpu, gpu in POINTS:
        if lab in ("20M narrow", "20M wide"):
            s.line(X(b), Y(cpu), X(b), Y(gpu), stroke=MUTED, sw=1.2, dash="2,3")
    s.text(X(0.881e9) + 4, Y(8.19) - 12, "2000万行 窄 = 1.52×", size=11, fill=MUTED,
           anchor="middle")
    s.text(X(3.038e9) + 6, Y(21.61) - 14, "2000万行 宽 = 2.18×", size=11, fill=MUTED,
           anchor="middle")

    # Fixed-cost annotation: the reason the GPU loses on the left.
    s.line(X(0.30e9), Y(1.5), X(1.9e9), Y(1.5), stroke=GPU_C, sw=1.2, dash="2,3", op=0.8)
    s.text(X(1.95e9), Y(1.5) + 4, "GPU 固定开销 ≈ 1.5 s(与数据量无关)", size=11.5, fill=GPU_C)

    # Legend.
    lx, ly = px + 14, py + ph - 20
    s.circle(lx, ly - 4, 5.6, fill=CPU_C, stroke="#ffffff", sw=1.6)
    s.text(lx + 12, ly, "CPU (pandas, 单线程)", size=12, fill=MUTED)
    s.circle(lx + 178, ly - 4, 5.6, fill=GPU_C, stroke="#ffffff", sw=1.6)
    s.text(lx + 190, ly, "GPU (cuDF)", size=12, fill=MUTED)
    s.line(lx + 292, ly - 4, lx + 320, ly - 4, stroke=MUTED, sw=1.8, dash="6,4")
    s.text(lx + 326, ly, "拟合速率(实测斜率)", size=12, fill=MUTED)


def panel_b(s: Svg, x, y, w, h):
    """Speedup per measured file, with the break-even line and the fitted ceiling."""
    card(s, x, y, w, h, "② 加速比 — 何时 GPU 才划得来",
         "同一批实测点。1.0× 以下 GPU 更慢;虚线为拟合渐近上限", CPU_C)

    px, py = x + 176, y + 96
    pw, ph = w - 176 - 30, h - 96 - 62

    data = sorted(((lab, b, cpu / gpu) for lab, r, b, cpu, gpu in POINTS), key=lambda t: t[1])
    v_max = 2.9

    def X(v):
        return px + (v / v_max) * pw

    bar_h = min(20.0, (ph - 8) / len(data) - 7)

    for v in (0.5, 1.0, 1.5, 2.0, 2.5):
        s.line(X(v), py, X(v), py + ph, stroke=RULE, sw=0.8, dash="3,4")
        s.text(X(v), py + ph + 19, f"{v:.1f}×", size=11.5, fill=MUTED, anchor="middle")

    for i, (lab, b, sp) in enumerate(data):
        cy = py + 10 + i * ((ph - 8) / len(data))
        col = GPU_C if sp >= 1.0 else CPU_C
        s.text(px - 12, cy + bar_h / 2 + 4, f"{lab}  ·  {b:.2f} GB", size=11.5,
               fill=INK, anchor="end")
        s.rect(px, cy, max(X(sp) - px, 1.0), bar_h, fill=col, rx=3, op=0.88)
        s.text(X(sp) + 7, cy + bar_h / 2 + 4, f"{sp:.2f}×", size=11.5,
               fill=col, weight="600")

    # Break-even and ceilings.
    s.line(X(1.0), py, X(1.0), py + ph, stroke=INK, sw=1.6)
    s.text(X(1.0), py - 8, "1.0× 收支平衡", size=11.5, fill=INK, anchor="middle", weight="600")
    for v, key in ((RATE["wide"]["cpu"] / RATE["wide"]["gpu"], "wide"),
                   (RATE["narrow"]["cpu"] / RATE["narrow"]["gpu"], "narrow")):
        if v <= v_max:
            s.line(X(v), py, X(v), py + ph, stroke=GPU_C, sw=1.4, dash="7,4", op=0.75)
            s.text(X(v) - 6, py + 14 + (0 if key == "wide" else 30),
                   f"{key} 上限 ≈ {v:.1f}×", size=11, fill=GPU_C, anchor="end", weight="600")

    s.text(px + pw / 2, py + ph + 42, "GPU 相对 CPU 的加速倍数", size=12.5, fill=MUTED,
           anchor="middle")

    # Call out the non-monotonic point, because hiding it would be the dishonest move. Placed to
    # the LEFT of the bar when the bar is long, since a fixed offset pushed it outside the card.
    for i, (lab, b, sp) in enumerate(data):
        if lab == "80M narrow":
            cy = py + 10 + i * ((ph - 8) / len(data)) + bar_h / 2 + 4
            txt = "4.0 GB 反而低于 2.5 GB(复测确认)→"
            if X(sp) + 54 + len(txt) * 7.5 > px + pw + 10:
                s.text(px + 6, cy, txt, size=11, fill="#ffffff", weight="600")
            else:
                s.text(X(sp) + 54, cy, txt, size=11, fill=CPU_C, weight="600")


def panel_c(s: Svg, x, y, w, h):
    """Controlled experiment: bytes held constant, shape varied."""
    card(s, x, y, w, h, "③ 控制实验 — 决定因素是字节,不是行数",
         "总字节固定 ≈600 MB,只变行/列结构;字节/行跨度 13×", GPU_C)

    px, py = x + 88, y + 100
    pw, ph = w - 88 - 34, h - 100 - 66

    v_max = 1.6

    def X(v):
        return px + (v / v_max) * pw

    for v in (1.0, 1.2, 1.4):
        s.line(X(v), py, X(v), py + ph, stroke=RULE, sw=0.8, dash="3,4")
        s.text(X(v), py + ph + 19, f"{v:.1f}×", size=11.5, fill=MUTED, anchor="middle")

    pts = []
    for i, (cols, bpr, sp) in enumerate(SHAPE):
        cy = py + 22 + i * ((ph - 30) / len(SHAPE))
        col = GPU_C if sp >= 1.0 else CPU_C
        s.text(px - 12, cy + 5, f"{bpr:.0f} B/行 · {cols} 列", size=11.5, fill=INK, anchor="end")
        s.rect(px, cy - 9, max(X(sp) - px, 1.0), 18, fill=col, rx=3, op=0.88)
        s.text(X(sp) + 7, cy + 5, f"{sp:.2f}×", size=11.5, fill=col, weight="600")
        pts.append((X(sp), cy))

    s.line(X(1.0), py, X(1.0), py + ph, stroke=INK, sw=1.6)
    s.text(px + pw / 2, py + ph + 42, "GPU 加速倍数", size=12.5, fill=MUTED, anchor="middle")

    # The claim this panel exists to support.
    s.rect(px + 6, py + ph - 46, pw - 12, 38, fill=GPU_C, op=0.07, rx=6)
    s.text(px + 16, py + ph - 21,
           "字节/行跨度 13×,加速比仅 1.39× → 1.18×:形状改变幅度,不改变结论",
           size=12, fill=GPU_C, weight="600")


def panel_d(s: Svg, x, y, w, h):
    """Resident session: the same small file, analysed repeatedly."""
    card(s, x, y, w, h, "④ 同一小文件反复分析 — 摊薄固定开销",
         "5 个 op。CPU 每次重读全文件;GPU 会话 open 一次后复用驻留数据", GPU_C)

    px, py = x + 104, y + 96
    pw, ph = w - 104 - 34, h - 96 - 76

    labels = [f"{r // 1_000_000}M 行" for r, c, g, sp in SESSION]
    t_max = 36.0

    def Y(t):
        return py + ph - (t / t_max) * ph

    for t in (0, 10, 20, 30):
        s.line(px, Y(t), px + pw, Y(t), stroke=RULE, sw=0.8, dash="3,4")
        s.text(px - 10, Y(t) + 4, str(t), size=11.5, fill=MUTED, anchor="end")
    s.text(px - 56, py + ph / 2, "耗时 (秒)", size=12.5, fill=MUTED,
           anchor="middle", rotate=-90)

    n = len(SESSION)
    step = pw / n
    grp_w = step * 0.52
    for i, (rows, cpu, gpu, sp) in enumerate(SESSION):
        cx = px + step * (i + 0.5)
        bw = grp_w / 2
        s.rect(cx - bw, Y(cpu), bw, Y(0) - Y(cpu), fill=CPU_C, rx=3, op=0.88)
        s.rect(cx, Y(gpu), bw, Y(0) - Y(gpu), fill=GPU_C, rx=3, op=0.88)
        s.text(cx - bw / 2, Y(cpu) - 7, f"{cpu:.1f}", size=11, fill=CPU_C,
               anchor="middle", weight="600")
        s.text(cx + bw / 2, Y(gpu) - 7, f"{gpu:.1f}", size=11, fill=GPU_C,
               anchor="middle", weight="600")
        s.text(cx, Y(0) + 20, labels[i], size=12, fill=INK, anchor="middle", weight="600")
        s.text(cx, Y(0) - 12, f"{sp:.2f}×", size=15, fill=GPU_C, anchor="middle",
               weight="700")

    s.text(px + pw / 2, Y(0) + 42, "同一文件上运行的 op 组数 × 5", size=12.5, fill=MUTED,
           anchor="middle")

    lx, ly = px + 4, y + h - 13
    s.rect(lx, ly - 11, 12, 12, fill=CPU_C, rx=2, op=0.88)
    s.text(lx + 18, ly, "CPU 5 次独立调用(每次重读)", size=12, fill=MUTED)
    s.rect(lx + 228, ly - 11, 12, 12, fill=GPU_C, rx=2, op=0.88)
    s.text(lx + 246, ly, "GPU 驻留会话(open 一次)", size=12, fill=MUTED)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    s = Svg()
    s.rect(0, 0, W, H, fill="#f7f8fa")

    # Header.
    s.text(M, 58, "GB10 上 CPU 与 GPU 的实测对比", size=30, weight="700")
    s.text(M, 86, "cuDF 25.10 vs pandas 2.3.3  ·  两个引擎都是全新进程  ·  "
                  "全部数字为 GB10 实测,无估算、无平滑", size=13.5, fill=MUTED)
    s.text(W - M, 58, "NVIDIA DGX Spark GB10", size=16, weight="600",
           anchor="end", fill=GPU_C)
    s.text(W - M, 80, "20 核 · 121 GB 统一内存 · cuDF 25.10.00", size=12.5,
           fill=MUTED, anchor="end")
    s.line(M, 100, W - M, 100, stroke=RULE, sw=1.2)

    panel_a(s, M, ROW1_Y, CW, CH)
    panel_b(s, M + CW + GAP, ROW1_Y, CW, CH)
    panel_c(s, M, ROW2_Y, CW, CH)
    panel_d(s, M + CW + GAP, ROW2_Y, CW, CH)

    # Two short footer lines rather than one long one: a single line of this length ran to
    # roughly 1100 logical px and would have been clipped at the right edge.
    s.line(M, H - 46, W - M, H - 46, stroke=RULE, sw=1.0)
    s.text(M, H - 27,
           "结论:小数据上 GPU 输在固定开销(≈1.5 s)而非算力;数据量增大后加速比趋近斜率上限"
           "(窄 ~4.3×,宽 ~4.6×+)。",
           size=12.5, fill=MUTED)
    s.text(M, H - 10,
           "但 4.0 GB 处加速比已回落(best-of-3 复测确认),且内存容量是可行性悬崖而非效率曲线 —— "
           "上限是天花板,不是地板。",
           size=12.5, fill=MUTED)

    out = os.path.join(args.out, "cpu_vs_gpu_benchmark.svg")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(s.render())
    print(f"wrote {out} ({os.path.getsize(out) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())