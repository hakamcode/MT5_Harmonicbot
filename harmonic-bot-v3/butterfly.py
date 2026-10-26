"""Butterfly harmonic pattern: swings, detection, PRZ, and the trade plan (pure Python, no AI).

Butterfly (bullish = buy at D, bearish = sell at D):
  * B retraces about 0.786 of XA          (xb_range, default 0.74 - 0.83)
  * C retraces 0.382 - 0.886 of AB        (ac_range)
  * D extends 1.27 - 1.414 of XA past A   (prz_xa)  -> this band is the PRZ
  * the BC projection 1.618 - 2.618 must reach the PRZ
Stop loss: just beyond the 1.618 XA extension (sl_xa + sl_buffer_xa).
Targets are measured on the A->D leg from the real D (the extreme reached in the PRZ):
  TP1 = 0.382 of A->D, TP2 = 0.618 of A->D.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class Pivot:
    idx: int
    time: pd.Timestamp
    price: float
    kind: str  # "H" or "L"


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def zigzag(df: pd.DataFrame, atr_mult: float, atr_len: int = 14) -> list[Pivot]:
    """ATR-threshold zigzag. All pivots but the last are confirmed; the last is the
    running extreme of the current leg (it can still move)."""
    a = atr(df, atr_len).to_numpy()
    hi, lo, t = df["high"].to_numpy(), df["low"].to_numpy(), df.index
    pivots: list[Pivot] = []
    trend = 0
    ext_i = hi_i = lo_i = 0
    for i in range(1, len(df)):
        th = a[i] * atr_mult
        if trend == 0:
            if hi[i] > hi[hi_i]:
                hi_i = i
            if lo[i] < lo[lo_i]:
                lo_i = i
            if hi[hi_i] - lo[lo_i] > th:
                if lo_i < hi_i:
                    pivots.append(Pivot(lo_i, t[lo_i], lo[lo_i], "L")); trend, ext_i = 1, hi_i
                else:
                    pivots.append(Pivot(hi_i, t[hi_i], hi[hi_i], "H")); trend, ext_i = -1, lo_i
        elif trend == 1:
            if hi[i] >= hi[ext_i]:
                ext_i = i
            elif hi[ext_i] - lo[i] > th:
                pivots.append(Pivot(ext_i, t[ext_i], hi[ext_i], "H")); trend, ext_i = -1, i
        else:
            if lo[i] <= lo[ext_i]:
                ext_i = i
            elif hi[i] - lo[ext_i] > th:
                pivots.append(Pivot(ext_i, t[ext_i], lo[ext_i], "L")); trend, ext_i = 1, i
    if trend == 1:
        pivots.append(Pivot(ext_i, t[ext_i], hi[ext_i], "H"))
    elif trend == -1:
        pivots.append(Pivot(ext_i, t[ext_i], lo[ext_i], "L"))
    return pivots


@dataclass
class Setup:
    symbol: str
    timeframe: str
    side: str                 # "buy" (bullish) or "sell" (bearish)
    points: dict              # X, A, B, C -> (iso time, price)
    ratios: dict
    prz_inner: float          # 1.27 XA, price reaches it first
    prz_outer: float          # 1.414 XA
    stop: float               # beyond 1.618 XA
    tp1: float                # planned (re-measured from the real D at entry)
    tp2: float
    mid: float
    created: str
    pattern: str = "Butterfly"
    rank: int = 1
    state: str = "WATCH"      # WATCH, OPEN, DONE, INVALID, EXPIRED, SKIPPED
    touched: bool = False
    touch_bars: int = 0
    d_price: Optional[float] = None   # deepest price reached in the PRZ (the real D)
    entry: Optional[float] = None
    entry_time: Optional[str] = None
    sl: Optional[float] = None
    bars_watching: int = 0
    last_bar: Optional[str] = None
    ticket: Optional[int] = None
    note: str = ""
    chart: str = ""           # latest chart picture of this setup (shown in the app)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.points['X'][0]}|{self.points['C'][0]}"

    @property
    def label(self) -> str:
        return ("Bullish" if self.side == "buy" else "Bearish") + " Butterfly"

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def targets(side: str, A: float, D: float, cfg: dict) -> tuple[float, float]:
    """TP1/TP2 as retracements of the A->D leg, measured from D."""
    sgn = 1 if side == "buy" else -1
    ad = abs(A - D)
    return D + sgn * ad * cfg["tp1_ad"], D + sgn * ad * cfg["tp2_ad"]


def find_setups(df: pd.DataFrame, symbol: str, timeframe: str, cfg: dict) -> list[Setup]:
    """Latest X-A-B-C on every swing size that forms a Butterfly whose D is still ahead."""
    out: dict[str, Setup] = {}
    tol = cfg["ratio_tolerance"]
    for mult in cfg["swing_atr_mults"]:
        piv = zigzag(df, mult)
        if len(piv) < 5:
            continue
        X, A, B, C = piv[:-1][-4:]
        side = "buy" if C.kind == "H" else "sell"      # C high -> D low -> buy at D
        xa, ab, bc = abs(A.price - X.price), abs(B.price - A.price), abs(C.price - B.price)
        if min(xa, ab, bc) == 0:
            continue
        xb, ac = ab / xa, bc / ab
        lo_b, hi_b = cfg["xb_range"]
        lo_c, hi_c = cfg["ac_range"]
        if not (lo_b * (1 - tol) <= xb <= hi_b * (1 + tol) and lo_c * (1 - tol) <= ac <= hi_c * (1 + tol)):
            continue
        sgn = -1 if side == "buy" else 1               # direction of D from A
        inner = A.price + sgn * xa * cfg["prz_xa"][0]
        outer = A.price + sgn * xa * cfg["prz_xa"][1]
        stop = A.price + sgn * xa * (cfg["sl_xa"] + cfg["sl_buffer_xa"])
        # BC projection 1.618 - 2.618 must overlap the PRZ
        p1, p2 = C.price + sgn * bc * cfg["bc_range"][0], C.price + sgn * bc * cfg["bc_range"][1]
        z_lo, z_hi = sorted((inner, outer))
        b_lo, b_hi = sorted((p1, p2))
        slack = xa * tol
        if b_hi < z_lo - slack or b_lo > z_hi + slack:
            continue
        # what price has done since C
        after = df[df.index > C.time]
        far = (after["low"].min() if side == "buy" else after["high"].max()) if len(after) else C.price
        if (far <= stop) if side == "buy" else (far >= stop):
            continue                                   # already blown through the stop
        tp1, tp2 = targets(side, A.price, inner, cfg)
        s = Setup(symbol, timeframe, side,
                  {n: (str(p.time), float(p.price)) for n, p in zip("XABC", (X, A, B, C))},
                  dict(XB=round(xb, 3), AC=round(ac, 3), XD=f"{cfg['prz_xa'][0]}-{cfg['prz_xa'][1]}"),
                  float(inner), float(outer), float(stop), float(tp1), float(tp2), float((tp1 + tp2) / 2),
                  str(df.index[-1]))
        if (far <= inner) if side == "buy" else (far >= inner):
            s.touched, s.d_price = True, float(far)    # already inside the PRZ
        out.setdefault(s.key, s)
    return list(out.values())


def step(s: Setup, bar: pd.Series, bar_time, cfg: dict) -> list[str]:
    """Advance a WATCH setup by one closed bar. Events: touch, signal, invalid, expired."""
    ev: list[str] = []
    buy = s.side == "buy"
    hi, lo, op, cl = float(bar["high"]), float(bar["low"]), float(bar["open"]), float(bar["close"])
    s.last_bar = str(bar_time)
    s.bars_watching += 1
    if (lo <= s.stop) if buy else (hi >= s.stop):
        s.state = "INVALID"; s.note = "price went past the 1.618 stop level"
        return ["invalid"]
    if (lo <= s.prz_inner) if buy else (hi >= s.prz_inner):
        far = lo if buy else hi
        s.d_price = far if s.d_price is None else (min(s.d_price, far) if buy else max(s.d_price, far))
        if not s.touched:
            s.touched = True
            ev.append("touch")
    if s.touched:
        s.touch_bars += 1
        # reversal candle: closes in the trade direction and back on the safe side of the PRZ outer edge
        if (cl > op and cl > s.prz_outer) if buy else (cl < op and cl < s.prz_outer):
            ev.append("signal")
        elif s.touch_bars > cfg["entry_window_bars"]:
            s.state = "EXPIRED"; s.note = "no reversal candle in the PRZ"; ev.append("expired")
        return ev
    C = s.points["C"][1]
    if (hi > C) if buy else (lo < C):
        s.state = "INVALID"; s.note = "price broke point C before reaching the PRZ"; ev.append("invalid")
    elif s.bars_watching > cfg["watch_expiry_bars"]:
        s.state = "EXPIRED"; s.note = "price never reached the PRZ"; ev.append("expired")
    return ev
