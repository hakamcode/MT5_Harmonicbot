"""Simulated broker so the app and the money management can be tried without MetaTrader 5.

Prices are made-up candles with Butterfly, Bat, Gartley and Crab patterns in them (some win, some hit the stop).
Every call to advance() moves one price tick; each candle has 4 ticks (open, low/high, high/low, close).
Account: 10,000 USD demo, every symbol behaves like EURUSD (1 lot = 100,000, 1 pip = 10 USD).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAGIC = 777002

# a pattern scenario: list of (price as fraction of XA from X, bars); X=0, A=1, B=1-xb, C=B+ac*xb, D=1-d
#   win: bounce to 75% of A->D, loss: run past the stop, tp1: reach TP1 then come back to the entry
def scenario(xb, ac, d, outcome="win", sl=None):
    B = 1 - xb
    C = B + ac * xb
    D = 1 - d
    legs = [(0.0, 30), (1.0, 50), (B, 35), (C, 25), (D, 45)]
    if outcome == "win":
        legs += [(D + 0.75 * d, 60)]
    elif outcome == "loss":
        legs += [(D + 0.08 * d, 6), (1 - (sl + 0.15), 30), (1 - sl, 30)]
    else:
        legs += [(D + 0.45 * d, 30), (D - 0.03 * d, 40), (D + 0.2 * d, 20)]
    return legs


BUTTERFLY = dict(xb=0.786, ac=0.618, d=1.32, sl=1.64)
BAT = dict(xb=0.45, ac=0.7, d=0.9, sl=1.02)
GARTLEY = dict(xb=0.618, ac=0.618, d=0.80, sl=1.02)
CRAB = dict(xb=0.55, ac=0.886, d=1.65, sl=2.02)
WIN, LOSS, TP1_ONLY = (scenario(**{k: v for k, v in BUTTERFLY.items() if k != "sl"}, outcome=o, sl=BUTTERFLY["sl"])
                       for o in ("win", "loss", "tp1"))


def S(p, outcome="win"):
    return scenario(p["xb"], p["ac"], p["d"], outcome, p["sl"])


def _series(scenarios, base, xa, seed, tf_minutes) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    path, level, sign = [], base, 1
    for sc, s in scenarios:
        sign = s
        x0 = level
        pts = [(x0 + sign * xa * f, n) for f, n in sc]
        cur = path[-1] if path else pts[0][0]
        for p, n in pts:
            path += list(np.linspace(cur, p, n, endpoint=False))
            cur = p
        level = cur
    close = np.array(path) + rng.normal(0, xa * 0.006, len(path))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, xa * 0.008, len(path)))
    idx = pd.date_range("2026-09-01", periods=len(path), freq=f"{tf_minutes}min")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick,
                         "low": np.minimum(open_, close) - wick, "close": close}, index=idx)


class SimBroker:
    name = "Simulator"

    def __init__(self, symbols: list[str], timeframes: list[str], balance=10_000.0, seed=1):
        self.tfs = timeframes
        self.tf_min = {"M15": 15, "M30": 30, "H1": 60, "H2": 120, "H4": 240, "H8": 480, "D1": 1440}
        base_tf = timeframes[0]
        self.data: dict[str, pd.DataFrame] = {}
        plans = [[(S(BUTTERFLY), 1), (S(BAT), -1), (S(GARTLEY, "loss"), 1), (S(CRAB, "tp1"), -1), (S(BAT), 1)],
                 [(S(BAT), -1), (S(BUTTERFLY, "loss"), -1), (S(CRAB), 1), (S(GARTLEY), -1), (S(BAT, "tp1"), 1)],
                 [(S(CRAB, "loss"), 1), (S(GARTLEY), -1), (S(BUTTERFLY), 1), (S(BAT, "loss"), -1), (S(BUTTERFLY), 1)]]
        for i, s in enumerate(symbols):
            warm = [([(0.0, 40), (0.5, 40), (0.2, 40)], 1)]
            self.data[s] = _series(warm + plans[i % len(plans)], 1.10 + 0.05 * i, 0.04, seed + i, self.tf_min[base_tf])
        self.base_tf = base_tf
        self.bar = 130            # current forming bar index (history before it is "closed")
        self.sub = 0              # 0..3 tick inside the bar
        self.balance = balance
        self._pos: dict[int, dict] = {}
        self._closed: dict[int, dict] = {}
        self._next = 1000
        self.length = min(len(d) for d in self.data.values())
        self.allow = True

    # ------------------------------------------------------------------ time
    @property
    def finished(self) -> bool:
        return self.bar >= self.length - 1

    def advance(self):
        if self.finished:
            return
        self.sub += 1
        if self.sub > 3:
            self.sub, self.bar = 0, self.bar + 1
        for t, p in list(self._pos.items()):
            self._check_stops(t, p)

    def _price(self, symbol: str) -> float:
        r = self.data[symbol].iloc[self.bar]
        up = r["close"] >= r["open"]
        seq = [r["open"], r["low"] if up else r["high"], r["high"] if up else r["low"], r["close"]]
        return float(seq[self.sub])

    # ------------------------------------------------------------------ broker interface
    def connect(self):
        pass

    def account(self):
        float_pl = sum(self._pl(p) for p in self._pos.values())
        return dict(login=12345678, server="Simulator-Demo", name="Simulator", currency="USD",
                    balance=round(self.balance, 2), equity=round(self.balance + float_pl, 2),
                    profit=round(float_pl, 2), demo=True)

    def today(self):
        return str(next(iter(self.data.values())).index[self.bar].date())

    def trading_check(self, allow_live):
        return None if self.allow else "simulated: trading switched off"

    def resolve(self, symbol):
        return symbol if symbol in self.data else None

    def watch_symbols(self):
        return list(self.data)

    def candles(self, symbol, tf, count, live=False):
        d = self.data[symbol].iloc[:self.bar + (1 if live else 0)]
        return d.iloc[-count:]

    def spec(self, symbol):
        return dict(digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, vol_min=0.01, vol_max=100.0,
                    vol_step=0.01, stops_level=0)

    def quote(self, symbol):
        p = self._price(symbol)
        return p - 0.00005, p + 0.00005

    def _pl(self, p):
        bid, ask = self.quote(p["symbol"])
        now = bid if p["side"] == "buy" else ask
        diff = (now - p["entry"]) if p["side"] == "buy" else (p["entry"] - now)
        return diff / 0.00001 * 1.0 * p["volume"]

    def positions(self):
        out = []
        for t, p in self._pos.items():
            bid, ask = self.quote(p["symbol"])
            out.append(dict(ticket=t, symbol=p["symbol"], side=p["side"], volume=round(p["volume"], 2),
                            entry=p["entry"], sl=p["sl"], tp=p["tp"], price=bid if p["side"] == "buy" else ask,
                            profit=round(self._pl(p), 2), bot=True, comment=p["comment"]))
        return out

    def open(self, symbol, side, volume, sl, tp, comment):
        bid, ask = self.quote(symbol)
        t = self._next
        self._next += 1
        price = ask if side == "buy" else bid
        self._pos[t] = dict(symbol=symbol, side=side, volume=float(volume), entry=price, sl=sl, tp=tp,
                            comment=comment, realized=0.0)
        return t, price, None

    def close(self, ticket, volume=None, reason="manual"):
        p = self._pos.get(ticket)
        if p is None:
            return "position not found"
        vol = min(float(volume or p["volume"]), p["volume"])
        pl = self._pl(p) * vol / p["volume"]
        self.balance += pl
        p["realized"] += pl
        p["volume"] = round(p["volume"] - vol, 2)
        if p["volume"] <= 1e-9:
            bid, ask = self.quote(p["symbol"])
            self._closed[ticket] = dict(profit=p["realized"], reason=reason,
                                        price=bid if p["side"] == "buy" else ask)
            del self._pos[ticket]
        return None

    def modify(self, ticket, sl, tp):
        p = self._pos.get(ticket)
        if p is None:
            return "position not found"
        p["sl"], p["tp"] = sl, tp
        return None

    def _check_stops(self, t, p):
        bid, ask = self.quote(p["symbol"])
        now = bid if p["side"] == "buy" else ask
        buy = p["side"] == "buy"
        if p["sl"] and ((now <= p["sl"]) if buy else (now >= p["sl"])):
            self.close(t, reason="sl")
        elif p["tp"] and ((now >= p["tp"]) if buy else (now <= p["tp"])):
            self.close(t, reason="tp")

    def closed_info(self, ticket):
        return self._closed.get(ticket)
