"""Chart image in the team's TradingView style: shaded XABCD, PRZ box, STOP, Expect Targets."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle
import pandas as pd

from harmonics import Setup

UP, DOWN = "#2962ff", "#c2185b"
SHADE_BULL, SHADE_BEAR = "#80deea", "#e1bee7"


def _fmt(v: float, digits: int) -> str:
    return f"{v:.{digits}f}"


def render(df: pd.DataFrame, setups: list[Setup], path: str, digits: int = 5, bars: int = 160,
           price: float | None = None) -> str:
    s0 = setups[0]
    first_pt = min(pd.Timestamp(t) for t, _ in s0.points.values())
    start = max(0, min(len(df) - bars, df.index.get_indexer([first_pt], method="nearest")[0] - 10))
    d = df.iloc[start:]
    n = len(d)
    pad = max(25, n // 4)
    x_of = lambda t: d.index.get_indexer([pd.Timestamp(t)], method="nearest")[0]

    fig, ax = plt.subplots(figsize=(12, 6.4), dpi=110)
    fig.patch.set_facecolor("white")
    w = 0.6
    for i, (o, h, l, c) in enumerate(d[["open", "high", "low", "close"]].to_numpy()):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=col, linewidth=0.8)
        ax.add_patch(Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), 1e-12), color=col))

    right = n + pad
    for s in setups:
        pts = {k: (x_of(t), p) for k, (t, p) in s.points.items()}
        # D: real extreme if the trade has started, otherwise projected into the PRZ
        dx = n + pad // 3 if s.state == "WATCH" else x_of(s.entry_time)
        if s.state == "WATCH":
            dy = (s.prz_inner + s.prz_outer) / 2
        else:                                   # actual extreme of the C->D leg
            seg = d.iloc[pts["C"][0]:dx + 1]
            dx = int(seg["low"].to_numpy().argmin() if s.side == "buy" else seg["high"].to_numpy().argmax()) + pts["C"][0]
            dy = float(d["low"].iloc[dx] if s.side == "buy" else d["high"].iloc[dx])
        pts["D"] = (dx, dy)
        shade = SHADE_BULL if s.side == "buy" else SHADE_BEAR
        if s.rank == 1 or s.pattern == "AB=CD":
            if "X" in pts:
                ax.add_patch(Polygon([pts["X"], pts["A"], pts["B"]], color=shade, alpha=0.55, lw=0))
            ax.add_patch(Polygon([pts["B"], pts["C"], pts["D"]], color=shade, alpha=0.55, lw=0))
            for k, (px, py) in pts.items():
                low = (k in "XBD") == (s.side == "buy") if "X" in pts else (k in "BD") == (s.side == "buy")
                ax.annotate(k, (px, py), xytext=(0, -14 if low else 8), textcoords="offset points",
                            ha="center", fontsize=10, fontweight="bold")
            rt = "  ".join(f"{k} {v}" for k, v in s.ratios.items())
            ax.text(0.01, 0.97, f"{s.label}   {rt}", transform=ax.transAxes, fontsize=9, va="top",
                    bbox=dict(fc="white", ec="#bbb"))

        # PRZ box
        x0 = max(pts["C"][0] + 2, n - pad // 2)
        lo, hi = sorted((s.prz_inner, s.prz_outer))
        tag = f"PRZ {'BUY' if s.side == 'buy' else 'SELL'} ({s.rank})" if s.pattern != "AB=CD" else "PRZ AB=CD"
        ax.add_patch(Rectangle((x0, lo), right - x0 - 4, hi - lo,
                               color="#fff59d" if s.side == "buy" else "#e1bee7", alpha=0.75, lw=0))
        ax.text((x0 + right - 4) / 2, (lo + hi) / 2, f"{tag} {s.pattern}", ha="center", va="center",
                fontsize=8, fontweight="bold")
        for v in (lo, hi):
            ax.text(right - 3, v, f"({_fmt(v, digits)})", fontsize=7, va="center")

        def hline(v, color, label, ls="-", lw=1.0):
            ax.hlines(v, x0, right - 4, colors=color, linestyles=ls, linewidth=lw)
            ax.text(right - 3, v, f"{label} {_fmt(v, digits)}", fontsize=7, va="center", color=color,
                    fontweight="bold")

        if s.rank == 1 or s.pattern == "AB=CD" or s.state != "WATCH":
            hline(s.stop, "#e91e63", "STOP", lw=1.3)
            hline(s.tp1, "#424242", "Expect Target (1)")
            hline(s.mid, "#26a69a", "")
            hline(s.tp2, "#424242", "Expect Target (2)")
            if s.entry is not None and not (s.state == "TP1" and s.sl == s.entry):
                hline(s.entry, UP, "ENTRY", ls="--")
            if s.sl is not None and s.state == "TP1":
                hline(s.sl, "#fb8c00", "TS/SL+", ls="--")
        else:
            hline(s.stop, "#e91e63", f"STOP ({s.rank})", ls=":")

    if price is not None:                      # live price line, like MT5's bid line
        ax.axhline(price, color="#757575", linewidth=0.7, linestyle=(0, (4, 3)))
        ax.text(right + pad * 0.88, price, f" {_fmt(price, digits)} ", fontsize=7, va="center", ha="right",
                color="white", bbox=dict(fc="#455a64", ec="none", pad=1.5))
    ax.set_xlim(-1, right + pad * 0.9)
    ys = [d["low"].min(), d["high"].max()] + [v for s in setups for v in (s.stop, s.tp2, s.prz_outer)]
    ys += [price] if price is not None else []
    span = max(ys) - min(ys)
    ax.set_ylim(min(ys) - span * 0.04, max(ys) + span * 0.04)
    ticks = list(range(0, n, max(1, n // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.index[i].strftime("%d %b %H:%M") for i in ticks], fontsize=7)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelsize=7)
    ax.grid(alpha=0.15)
    for sp in ("top", "left"):
        ax.spines[sp].set_visible(False)
    ax.set_title(f"{s0.symbol} · {s0.timeframe} · {s0.label}  [{s0.state}]", loc="left", fontsize=11,
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
