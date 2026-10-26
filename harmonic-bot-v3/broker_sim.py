"""Simulated broker so the app and the money management can be tried without MetaTrader 5.

Prices are made-up candles with Butterfly patterns in them (some win, some hit the stop).
Every call to advance() moves one price tick; each candle has 4 ticks (open, low/high, high/low, close).
Account: 10,000 USD demo, every symbol behaves like EURUSD (1 lot = 100,000, 1 pip = 10 USD).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAGIC = 777003

# one Butterfly scenario: list of (price as fraction of XA from X, bars); X=0, A=1
#   B = 1 - 0.786, C = B + 0.618*AB, D = 1 - 1.30 (inside the 1.27-1.414 PRZ)
WIN = [(0.0, 30), (1.0, 50), (1 - 0.786, 35), (1 - 0.786 + 0.618 * 0.786, 25), (1 - 1.32, 45),
       (1 - 1.32 + 0.75 * 1.32, 60)]
LOSS = [(0.0, 30), (1.0, 50), (1 - 0.786, 35), (1 - 0.786 + 0.618 * 0.786, 25), (1 - 1.32, 45),
        (1 - 1.24, 6), (1 - 1.80, 30), (1 - 1.5, 30)]
TP1_ONLY = [(0.0, 30), (1.0, 50), (1 - 0.786, 35), (1 - 0.786 + 0.618 * 0.786, 25), (1 - 1.32, 45),
            (1 - 1.32 + 0.45 * 1.32, 30), (1 - 1.35, 40), (1 - 1.10, 20)]


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
        plans = [[(WIN, 1), (WIN, -1), (LOSS, 1), (TP1_ONLY, -1), (WIN, 1)],
                 [(WIN, -1), (LOSS, -1), (WIN, 1), (WIN, -1), (TP1_ONLY, 1)],
                 [(LOSS, 1), (WIN, -1), (WIN, 1), (LOSS, -1), (WIN, 1)]]
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

    def candles(self, symbol, tf, count):
        d = self.data[symbol].iloc[:self.bar]
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
