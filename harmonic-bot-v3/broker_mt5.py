"""MetaTrader 5 broker: prices, account, positions and orders. Needs Windows + the MT5 terminal running."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import pandas as pd

log = logging.getLogger("mt5")

try:
    import MetaTrader5 as mt5
except ImportError:  # lets the simulator and tests run on any computer
    mt5 = None

MAGIC = 777003          # marks the orders placed by this bot (v3)


class MT5Broker:
    name = "MetaTrader 5"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._names: dict[str, str] = {}

    # ------------------------------------------------------------------ connection
    def connect(self):
        if mt5 is None:
            raise RuntimeError("The MetaTrader5 package is not installed. Run: pip install MetaTrader5 (Windows only)")
        kw = {k: self.cfg[k] for k in ("login", "password", "server", "path") if self.cfg.get(k)}
        if "login" in kw:
            kw["login"] = int(kw["login"])
        if not mt5.initialize(**kw):
            raise RuntimeError(f"Could not connect to MetaTrader 5 ({mt5.last_error()}). "
                               "Open MT5, log in to your account, then try again.")

    def account(self) -> dict | None:
        a = mt5.account_info()
        if a is None:
            return None
        return dict(login=a.login, server=a.server, name=a.name, currency=a.currency, balance=a.balance,
                    equity=a.equity, profit=a.profit, demo=a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)

    def today(self) -> str:
        """Broker server date (the trading day the daily loss limit counts)."""
        for name in list(self._names.values()) + ["EURUSD"]:
            t = mt5.symbol_info_tick(name)
            if t and t.time:
                return datetime.fromtimestamp(t.time, timezone.utc).date().isoformat()
        return date.today().isoformat()

    def trading_check(self, allow_live: bool) -> str | None:
        """None if the bot may place orders on this account, else the reason it may not."""
        acc = mt5.account_info()
        if acc is None:
            return "could not read the MT5 account"
        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not allow_live:
            return (f"account {acc.login} is a REAL money account. The bot only trades on a demo account "
                    'unless you set "allow_live_trading": true in config.json')
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            return "the Algo Trading button in MT5 is off (click it so it turns green)"
        return None

    # ------------------------------------------------------------------ market data
    def resolve(self, symbol: str) -> str | None:
        """Broker's name for a symbol (e.g. XMRUSD -> #XMRUSD), added to Market Watch; None if not offered."""
        if symbol in self._names:
            return self._names[symbol]
        name = symbol if mt5.symbol_info(symbol) else None
        if name is None:
            matches = [s.name for s in (mt5.symbols_get(f"*{symbol}*") or [])]
            if matches:
                name = min(matches, key=len)
                log.info("%s is called %s at this broker", symbol, name)
        if name:
            mt5.symbol_select(name, True)
            self._names[symbol] = name
        return name

    def candles(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Closed bars only (the forming bar is dropped)."""
        name = self.resolve(symbol)
        if name is None:
            raise RuntimeError(f"{symbol} is not offered by this broker")
        tfc = getattr(mt5, f"TIMEFRAME_{tf}")
        rates = mt5.copy_rates_from_pos(name, tfc, 1, count)
        if rates is None or len(rates) == 0:              # history may still be downloading
            time.sleep(2)
            rates = mt5.copy_rates_from_pos(name, tfc, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"no data for {symbol} {tf}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time")[["open", "high", "low", "close"]]

    def spec(self, symbol: str) -> dict:
        i = mt5.symbol_info(self.resolve(symbol) or symbol)
        return dict(digits=i.digits, point=i.point, tick_size=i.trade_tick_size or i.point,
                    tick_value=i.trade_tick_value, vol_min=i.volume_min, vol_max=i.volume_max,
                    vol_step=i.volume_step, stops_level=i.trade_stops_level)

    def quote(self, symbol: str) -> tuple[float, float] | None:
        t = mt5.symbol_info_tick(self.resolve(symbol) or symbol)
        return (t.bid, t.ask) if t else None

    # ------------------------------------------------------------------ positions and orders
    def positions(self) -> list[dict]:
        out = []
        for p in mt5.positions_get() or []:
            buy = p.type == mt5.POSITION_TYPE_BUY
            out.append(dict(ticket=p.ticket, symbol=p.symbol, side="buy" if buy else "sell", volume=p.volume,
                            entry=p.price_open, sl=p.sl, tp=p.tp, price=p.price_current,
                            profit=p.profit + p.swap, bot=p.magic == MAGIC, comment=p.comment))
        return out

    def _filling(self, name: str):
        fm = mt5.symbol_info(name).filling_mode
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, req: dict) -> tuple[object, str | None]:
        res = mt5.order_send(req)
        if res is None:
            return None, f"MT5 gave no answer ({mt5.last_error()})"
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            return None, f"MT5 refused the order: {res.comment} (code {res.retcode})"
        return res, None

    def open(self, symbol: str, side: str, volume: float, sl: float, tp: float, comment: str):
        name = self.resolve(symbol) or symbol
        tick = mt5.symbol_info_tick(name)
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=name, volume=float(volume),
                   type=mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                   price=tick.ask if side == "buy" else tick.bid, sl=float(sl), tp=float(tp), deviation=30,
                   magic=MAGIC, comment=comment[:31], type_time=mt5.ORDER_TIME_GTC, type_filling=self._filling(name))
        res, err = self._send(req)
        if err:
            return None, None, err
        ticket = res.order
        pos = mt5.positions_get(ticket=ticket)
        return ticket, (pos[0].price_open if pos else res.price), None

    def close(self, ticket: int, volume: float | None = None) -> str | None:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return "position not found"
        p = pos[0]
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=float(volume or p.volume), position=ticket,
                   type=mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                   price=tick.bid if buy else tick.ask, deviation=30, magic=MAGIC,
                   type_time=mt5.ORDER_TIME_GTC, type_filling=self._filling(p.symbol))
        return self._send(req)[1]

    def modify(self, ticket: int, sl: float, tp: float) -> str | None:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return "position not found"
        return self._send(dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=pos[0].symbol,
                               sl=float(sl), tp=float(tp)))[1]

    def closed_info(self, ticket: int) -> dict | None:
        """How a closed position ended: total profit, reason (sl, tp, manual, other), close price."""
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
        if not outs:
            return None
        last = outs[-1]
        reason = {mt5.DEAL_REASON_SL: "sl", mt5.DEAL_REASON_TP: "tp", mt5.DEAL_REASON_SO: "stopout",
                  mt5.DEAL_REASON_CLIENT: "manual", mt5.DEAL_REASON_MOBILE: "manual",
                  mt5.DEAL_REASON_WEB: "manual", mt5.DEAL_REASON_EXPERT: "bot"}.get(last.reason, "other")
        profit = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        return dict(profit=profit, reason=reason, price=last.price)
