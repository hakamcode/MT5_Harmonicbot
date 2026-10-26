"""Harmonic pattern engine: swings, pattern matching, PRZ and the trade plan.

Rules follow the team study in ../harmonic-study/README.md:
  * PRZ (1) / PRZ (2) when one X-A-B-C fits more than one pattern (e.g. Bat then Butterfly).
  * Ladder anchored on the PRZ outer edge: TP2 distance = 1.618 x TP1 distance,
    green mid line = TP1/TP2 midpoint (1.309 x).
  * TP1 defaults to 0.382 of the A->D leg (C->D for ABCD), so TP2 lands at 0.618.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- patterns
# b = AB/XA, c = BC/AB, bd = CD/BC, xd = AD/XA (ideal XD level and accepted band)
PATTERNS = {
    "Gartley":     dict(b=(0.55, 0.68),  c=(0.382, 0.886), bd=(1.13, 1.618), xd=0.786, xd_band=(0.75, 0.82)),
    "Bat":         dict(b=(0.382, 0.55), c=(0.382, 0.886), bd=(1.618, 2.618), xd=0.886, xd_band=(0.85, 0.93)),
    "Alt Bat":     dict(b=(0.30, 0.382), c=(0.382, 0.886), bd=(2.0, 3.618),   xd=1.13,  xd_band=(1.10, 1.17)),
    "Butterfly":   dict(b=(0.70, 0.82),  c=(0.382, 0.886), bd=(1.618, 2.618), xd=1.27,  xd_band=(1.24, 1.414)),
    "Crab":        dict(b=(0.382, 0.618), c=(0.382, 0.886), bd=(2.24, 3.618), xd=1.618, xd_band=(1.55, 1.68)),
    "Deep Crab":   dict(b=(0.85, 0.92),  c=(0.382, 0.886), bd=(2.0, 3.618),   xd=1.618, xd_band=(1.55, 1.68)),
}
ABCD = dict(c=(0.382, 0.886), cd_ab=(1.0, 1.272))
BD_LEVELS = (1.13, 1.272, 1.414, 1.618, 2.0, 2.24, 2.618, 3.14, 3.618)


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
        if trend == 0:                        # until the first swing of size th appears
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


# --------------------------------------------------------------------------- setup
@dataclass
class Setup:
    symbol: str
    timeframe: str
    pattern: str
    side: str                 # "buy" or "sell"
    points: dict              # name -> (iso time, price) for X,A,B,C
    ratios: dict              # XB, AC, BD (at PRZ), XD
    prz_inner: float          # edge price reaches first
    prz_outer: float          # far edge (ladder anchor)
    stop: float
    tp1: float
    tp2: float
    mid: float
    created: str
    rank: int = 1             # PRZ (1), (2) ...
    group: str = ""           # same X-A-B-C shares a group
    source: str = "python"    # "python" detector or "claude" scanner
    note: str = ""            # Claude's reasoning when found by Claude
    confidence: int = 0
    # live state
    state: str = "WATCH"      # WATCH, OPEN, TP1, TP2, STOPPED, CLOSED_BE, CLOSED_TIME, INVALID, EXPIRED
    touched: bool = False
    entry: Optional[float] = None
    entry_time: Optional[str] = None
    sl: Optional[float] = None
    bars_watching: int = 0
    bars_open: int = 0
    last_bar: Optional[str] = None
    ticket: Optional[int] = None
    history: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.group}|{self.pattern}"

    @property
    def label(self) -> str:
        bull = "Bullish" if self.side == "buy" else "Bearish"
        return f"{bull} {self.pattern}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Setup":
        return Setup(**d)


def _in(v: float, rng: tuple, tol: float) -> bool:
    return rng[0] * (1 - tol) <= v <= rng[1] * (1 + tol)


def _plan(side: str, outer: float, leg_end: float, cfg: dict) -> tuple[float, float, float, float]:
    """TP1/TP2/mid/stop from the PRZ outer edge (team ladder rule)."""
    sgn = 1 if side == "buy" else -1
    d1 = abs(leg_end - outer) * cfg["tp1_leg_ratio"]
    tp1 = outer + sgn * d1
    tp2 = outer + sgn * d1 * cfg["tp2_multiple"]
    mid = (tp1 + tp2) / 2
    stop = outer - sgn * d1 * cfg["stop_buffer"]
    return tp1, tp2, mid, stop


def _prz(side, X, A, B, C, p, cfg):
    """PRZ = band between the XA level and BC projections that agree with it."""
    xa, bc = abs(A - X), abs(C - B)
    sgn = -1 if side == "buy" else 1           # D direction from A
    xd_level = A + sgn * xa * p["xd"]
    levels = [xd_level]
    for r in BD_LEVELS:
        if p["bd"][0] <= r <= p["bd"][1]:
            lv = C + sgn * bc * r
            if abs(lv - xd_level) <= xa * cfg["prz_max_width_xa"]:
                levels.append(lv)
    lo, hi = min(levels), max(levels)
    min_w = xa * cfg["prz_min_width_xa"]
    if hi - lo < min_w:                       # give the zone some width past the XA level
        if side == "buy":
            lo = hi - min_w
        else:
            hi = lo + min_w
    inner, outer = (hi, lo) if side == "buy" else (lo, hi)
    bd_at = abs(C - xd_level) / bc if bc else 0
    return inner, outer, bd_at


def find_setups(df: pd.DataFrame, symbol: str, timeframe: str, cfg: dict) -> list[Setup]:
    """Look at the latest X-A-B-C (and A-B-C) on every zigzag scale and project PRZs."""
    out: dict[str, Setup] = {}
    tol = cfg["ratio_tolerance"]
    price = float(df["close"].iloc[-1])
    for mult in cfg["swing_atr_mults"]:
        piv = zigzag(df, mult)
        if len(piv) < 5:
            continue
        conf = piv[:-1]                      # confirmed pivots; piv[-1] is the live C->D leg
        X, A, B, C = conf[-4:]
        side = "buy" if C.kind == "H" else "sell"   # C high -> D low -> buy at D
        group = f"{X.time:%Y%m%d%H%M}-{C.time:%Y%m%d%H%M}"
        pts = {n: (str(p.time), p.price) for n, p in zip("XABC", (X, A, B, C))}
        xa, ab, bc = abs(A.price - X.price), abs(B.price - A.price), abs(C.price - B.price)
        if min(xa, ab, bc) == 0:
            continue
        xb, ac = ab / xa, bc / ab
        found = []
        for name, p in PATTERNS.items():
            if not (_in(xb, p["b"], tol) and _in(ac, p["c"], tol)):
                continue
            inner, outer, bd_at = _prz(side, X.price, A.price, B.price, C.price, p, cfg)
            if not _in(bd_at, p["bd"], tol * 2):
                continue
            tp1, tp2, mid, stop = _plan(side, outer, A.price, cfg)
            found.append(Setup(symbol, timeframe, name, side, pts,
                               dict(XB=round(xb, 3), AC=round(ac, 3), BD=round(bd_at, 3), XD=p["xd"]),
                               inner, outer, stop, tp1, tp2, mid, str(df.index[-1]), group=group))
        # AB=CD on the last three confirmed pivots
        if cfg.get("include_abcd", True) and not found and _in(ac, ABCD["c"], tol):
            sgn = -1 if side == "buy" else 1
            d1, d2 = C.price + sgn * ab * ABCD["cd_ab"][0], C.price + sgn * ab * ABCD["cd_ab"][1]
            inner, outer = (max(d1, d2), min(d1, d2)) if side == "buy" else (min(d1, d2), max(d1, d2))
            tp1, tp2, mid, stop = _plan(side, outer, C.price, cfg)
            found.append(Setup(symbol, timeframe, "AB=CD", side,
                               {n: pts[n] for n in "ABC"}, dict(AC=round(ac, 3), CD_AB="1.0-1.272"),
                               inner, outer, stop, tp1, tp2, mid, str(df.index[-1]), group=group + "-abcd"))
        # drop setups whose PRZ is already blown through (price past the stop)
        found = [s for s in found if (price > s.stop if s.side == "buy" else price < s.stop)]
        # PRZ (1) = nearest to C, (2) = next ...
        found.sort(key=lambda s: abs(s.prz_inner - C.price))
        rank = 0
        for s in found:
            if s.pattern != "AB=CD":
                rank += 1
                s.rank = rank
            out.setdefault(s.key, s)
    return list(out.values())


# --------------------------------------------------------------------------- live management
def step(s: Setup, bar: pd.Series, bar_time, cfg: dict) -> list[str]:
    """Advance one closed bar. Returns event names that need an alert."""
    ev = []
    buy = s.side == "buy"
    hi, lo, op, cl = bar["high"], bar["low"], bar["open"], bar["close"]
    reached = (lambda lvl: lo <= lvl) if buy else (lambda lvl: hi >= lvl)    # adverse side
    target = (lambda lvl: hi >= lvl) if buy else (lambda lvl: lo <= lvl)     # favourable side
    s.last_bar = str(bar_time)

    if s.state == "WATCH":
        s.bars_watching += 1
        if reached(s.stop):
            s.state = "INVALID"; ev.append("invalid")
            return ev
        if reached(s.prz_inner):
            if not s.touched:
                s.touched = True
                ev.append("prz_touch")
            if cfg["entry_mode"] == "touch":
                _open(s, s.prz_inner, bar_time); ev.append("entry")
                return ev
        if s.touched and cfg["entry_mode"] == "confirm":
            # reversal candle that closes back on the right side of the inner edge
            ok = (cl > op and cl > s.prz_outer) if buy else (cl < op and cl < s.prz_outer)
            if ok:
                _open(s, float(cl), bar_time); ev.append("entry")
                return ev
        if not s.touched:
            C = s.points["C"][1]
            if (hi > C) if buy else (lo < C):        # leg C->D failed, structure broken
                s.state = "INVALID"; ev.append("invalid")
            elif s.bars_watching > cfg["watch_expiry_bars"]:
                s.state = "EXPIRED"; ev.append("expired")
        return ev

    if s.state in ("OPEN", "TP1"):
        s.bars_open += 1
        if reached(s.sl):                      # conservative: stop checked before targets
            if s.state == "OPEN":
                s.state = "STOPPED"; ev.append("stopped")
            else:
                s.state = "CLOSED_BE"; ev.append("tp2_invalid")
            return ev
        if s.state == "OPEN" and target(s.tp1):
            s.state = "TP1"
            s.sl = s.entry + (1 if buy else -1) * abs(s.tp1 - s.entry) * cfg["ts_lock_ratio"]
            ev.append("tp1")
        if s.state == "TP1":
            if target(s.tp2):
                s.state = "TP2"; ev.append("tp2")
            elif s.bars_open > cfg["tp2_max_bars"]:
                s.state = "CLOSED_TIME"; ev.append("tp2_timeout")
    return ev


def _open(s: Setup, price: float, t):
    s.state, s.entry, s.entry_time, s.sl = "OPEN", float(price), str(t), s.stop


def rr(s: Setup, price: Optional[float] = None) -> tuple[float, float]:
    e = price if price is not None else (s.entry or s.prz_inner)
    risk = abs(e - s.stop) or 1e-12
    return abs(s.tp1 - e) / risk, abs(s.tp2 - e) / risk
