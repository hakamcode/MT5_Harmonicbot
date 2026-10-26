"""MetaTrader 5 data and (optional) order execution. Needs Windows + the MT5 terminal running."""
from __future__ import annotations

import logging
import time
import pandas as pd

log = logging.getLogger("mt5")

try:
    import MetaTrader5 as mt5
except ImportError:  # lets the rest of the code (demo, tests) run on any OS
    mt5 = None

TF = {"M15": 15, "M30": 30, "H1": 60, "H2": 120, "H4": 240, "H8": 480, "D1": 1440, "W1": 10080}


def connect(cfg: dict) -> None:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed (pip install MetaTrader5, Windows only)")
    kw = {k: cfg[k] for k in ("login", "password", "server", "path") if cfg.get(k)}
    if "login" in kw:
        kw["login"] = int(kw["login"])
    if not mt5.initialize(**kw):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    log.info("connected to MT5: %s", mt5.account_info().server if mt5.account_info() else "?")


def _tf(name: str):
    return {"M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
            "H2": mt5.TIMEFRAME_H2, "H4": mt5.TIMEFRAME_H4, "H8": mt5.TIMEFRAME_H8,
            "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1}[name]


_names: dict[str, str] = {}


def resolve(symbol: str) -> str | None:
    """Broker's name for a symbol (e.g. XMRUSD -> XMRUSD.r), added to Market Watch; None if not offered."""
    if symbol in _names:
        return _names[symbol]
    name = symbol if mt5.symbol_info(symbol) else None
    if name is None:
        matches = [s.name for s in (mt5.symbols_get(f"*{symbol}*") or [])]
        if matches:
            name = min(matches, key=len)
            log.info("%s is called %s at this broker", symbol, name)
        else:
            log.error("%s is not offered by this broker/account; remove it from symbols in config.json", symbol)
    if name:
        mt5.symbol_select(name, True)
        _names[symbol] = name
    return name


def candles(symbol: str, tf: str, count: int) -> pd.DataFrame:
    """Closed bars only (the forming bar is dropped)."""
    name = resolve(symbol)
    if name is None:
        raise RuntimeError(f"{symbol} not available at this broker")
    rates = mt5.copy_rates_from_pos(name, _tf(tf), 1, count)
    if rates is None or len(rates) == 0:              # history may still be downloading; try once more
        time.sleep(2)
        rates = mt5.copy_rates_from_pos(name, _tf(tf), 1, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"no data for {symbol} {tf}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]


def symbol_digits(symbol: str) -> tuple[int, float]:
    info = mt5.symbol_info(resolve(symbol) or symbol)
    return (info.digits, info.point) if info else (5, 0.00001)


# ---------------------------------------------------------------- optional auto trading
def _send(req: dict):
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("order failed: %s %s", req, res)
        return None
    return res


def open_market(symbol: str, side: str, volume: float, sl: float, tp: float, comment: str):
    symbol = resolve(symbol) or symbol
    tick = mt5.symbol_info_tick(symbol)
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=symbol, volume=volume,
               type=mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
               price=tick.ask if side == "buy" else tick.bid, sl=sl, tp=tp, deviation=20,
               magic=777001, comment=comment[:31], type_filling=mt5.ORDER_FILLING_IOC)
    res = _send(req)
    return res.order if res else None


def close_part(ticket: int, fraction: float):
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return
    p = pos[0]
    info = mt5.symbol_info(p.symbol)
    vol = max(info.volume_min, round(p.volume * fraction / info.volume_step) * info.volume_step)
    vol = min(vol, p.volume)
    tick = mt5.symbol_info_tick(p.symbol)
    buy = p.type == mt5.POSITION_TYPE_BUY
    _send(dict(action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=vol, position=ticket,
               type=mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
               price=tick.bid if buy else tick.ask, deviation=20, magic=777001,
               type_filling=mt5.ORDER_FILLING_IOC))


def modify_sl(ticket: int, sl: float, tp: float):
    pos = mt5.positions_get(ticket=ticket)
    if pos:
        _send(dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=pos[0].symbol, sl=sl, tp=tp))


def trading_check(allow_live: bool) -> str | None:
    """None if the bot may place orders on this account, else the reason it may not."""
    acc = mt5.account_info()
    if acc is None:
        return "could not read the MT5 account"
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not allow_live:
        return (f"account {acc.login} is a REAL money account. Auto trading only runs on a demo account "
                'unless you set "allow_live_trading": true in config.json')
    term = mt5.terminal_info()
    if term is not None and not term.trade_allowed:
        return "the Algo Trading button in MT5 is off"
    return None
