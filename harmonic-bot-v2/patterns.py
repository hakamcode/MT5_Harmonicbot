"""Harmonic patterns for the v2 app: Gartley, Bat, Alt Bat, Butterfly, Crab, Deep Crab and AB=CD (pure Python, no AI).

For every X-A-B-C (or A-B-C for AB=CD) the bot checks each enabled pattern in config.json ("harmonic" -> "patterns"):
  * xb  : B retracement of XA            * ac : C retracement of AB
  * bc  : BC projection that must reach the PRZ
  * prz_xa : where D should be, as a level of XA measured from A (the PRZ band)
  * sl_xa  : the level where the pattern is wrong; the stop loss goes just beyond it (+ sl_buffer_xa)
AB=CD: D = C + 1.0 - 1.272 x AB (cd_ab), stop beyond sl_ab x AB.
Targets are measured from the real D (the extreme reached in the PRZ):
  TP1 = tp1_ad of the A->D leg (C->D for AB=CD), TP2 = tp2_ad.
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
    prz_inner: float          # PRZ edge price reaches first
    prz_outer: float          # far PRZ edge
    stop: float               # just beyond the level where the pattern is wrong
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
    anchor: float = 0.0       # A (C for AB=CD): targets are measured on the anchor->D leg

    @property
    def key(self) -> str:
        first = self.points.get("X", self.points["A"])[0]
        return f"{self.symbol}|{self.timeframe}|{self.pattern}|{first}|{self.points['C'][0]}"

    @property
    def label(self) -> str:
        return ("Bullish " if self.side == "buy" else "Bearish ") + self.pattern

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def targets(side: str, A: float, D: float, cfg: dict) -> tuple[float, float]:
    """TP1/TP2 as retracements of the A->D leg (C->D for AB=CD), measured from D."""
    sgn = 1 if side == "buy" else -1
    ad = abs(A - D)
    return D + sgn * ad * cfg["tp1_ad"], D + sgn * ad * cfg["tp2_ad"]


def _in(v: float, rng, tol: float) -> bool:
    return rng[0] * (1 - tol) <= v <= rng[1] * (1 + tol)


def _price_since(df, t, side):
    after = df[df.index > t]
    if not len(after):
        return None
    return float(after["low"].min() if side == "buy" else after["high"].max())


def find_setups(df: pd.DataFrame, symbol: str, timeframe: str, cfg: dict) -> list[Setup]:
    """Latest X-A-B-C on every swing size that forms an enabled pattern whose D is still ahead."""
    out: dict[str, Setup] = {}
    tol = cfg["ratio_tolerance"]
    pats = {n: p for n, p in cfg["patterns"].items() if p.get("enabled", True)}
    for mult in cfg["swing_atr_mults"]:
        piv = zigzag(df, mult)
        if len(piv) < 5:
            continue
        X, A, B, C = piv[:-1][-4:]
        side = "buy" if C.kind == "H" else "sell"      # C high -> D low -> buy at D
        sgn = -1 if side == "buy" else 1               # direction of D from A
        xa, ab, bc = abs(A.price - X.price), abs(B.price - A.price), abs(C.price - B.price)
        if min(xa, ab, bc) == 0:
            continue
        xb, ac = ab / xa, bc / ab
        far = _price_since(df, C.time, side)
        far = C.price if far is None else far
        found = []
        for name, p in pats.items():
            if name == "AB=CD":
                continue
            if not (_in(xb, p["xb"], tol) and _in(ac, p["ac"], tol)):
                continue
            inner = A.price + sgn * xa * p["prz_xa"][0]
            outer = A.price + sgn * xa * p["prz_xa"][1]
            stop = A.price + sgn * xa * (p["sl_xa"] + cfg["sl_buffer_xa"])
            # the BC projection must reach the PRZ
            b_lo, b_hi = sorted((C.price + sgn * bc * p["bc"][0], C.price + sgn * bc * p["bc"][1]))
            z_lo, z_hi = sorted((inner, outer))
            if b_hi < z_lo - xa * tol or b_lo > z_hi + xa * tol:
                continue
            found.append((name, {n: (str(q.time), float(q.price)) for n, q in zip("XABC", (X, A, B, C))},
                          dict(XB=round(xb, 3), AC=round(ac, 3), XD=f"{p['prz_xa'][0]}-{p['prz_xa'][1]}"),
                          inner, outer, stop, A.price))
        p = pats.get("AB=CD")
        if p and not found and _in(ac, p["ac"], tol):
            inner = C.price + sgn * ab * p["cd_ab"][0]
            outer = C.price + sgn * ab * p["cd_ab"][1]
            stop = C.price + sgn * ab * (p["sl_ab"] + cfg["sl_buffer_xa"])
            found.append(("AB=CD", {n: (str(q.time), float(q.price)) for n, q in zip("ABC", (A, B, C))},
                          dict(AC=round(ac, 3), CD_AB=f"{p['cd_ab'][0]}-{p['cd_ab'][1]}"), inner, outer, stop, C.price))
        for name, pts, ratios, inner, outer, stop, anchor in found:
            if (far <= stop) if side == "buy" else (far >= stop):
                continue                               # already blown through the stop
            tp1, tp2 = targets(side, anchor, inner, cfg)
            s = Setup(symbol, timeframe, side, pts, ratios, float(inner), float(outer), float(stop), float(tp1),
                      float(tp2), float((tp1 + tp2) / 2), str(df.index[-1]), pattern=name, anchor=float(anchor))
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
        s.state = "INVALID"; s.note = "price went past the stop level"
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
