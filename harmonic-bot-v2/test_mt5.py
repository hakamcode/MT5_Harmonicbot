"""Live test of every MT5 order function the bot uses, on your DEMO account.

    python test_mt5.py                      # tests on EURUSD
    python test_mt5.py --symbol XMRUSD      # another symbol (crypto also works at the weekend)
    python test_mt5.py --telegram           # also send the bot's messages to your Telegram

It opens and closes a few trades of the smallest lot size, so it costs at most the spread.
It refuses to run on a real-money account. Every position it opens is closed again at the end.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from broker_mt5 import MAGIC, MT5Broker, mt5  # noqa: E402
from trader import Bot  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def load_config() -> dict:
    with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            mine = json.load(f)
        for k, v in mine.items():
            cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return cfg


def wait_for(fn, seconds=5.0):
    """Poll fn() until it returns something truthy (MT5 can take a moment to update)."""
    end = time.time() + seconds
    while True:
        v = fn()
        if v or time.time() > end:
            return v
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--telegram", action="store_true", help="send the bot's messages to Telegram too")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(HERE)
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    cfg["money"]["close_all_button"] = "bot"         # the test never touches your manual trades
    broker = MT5Broker(cfg["mt5"])

    print("\n1) Connection and account")
    try:
        broker.connect()
    except Exception as e:
        check("connect to MT5", False, str(e))
        return finish()
    check("connect to MT5", True)
    acc = broker.account()
    if not check("read account", acc is not None, f"#{acc['login']} {acc['server']} balance {acc['balance']} "
                                                  f"{acc['currency']}" if acc else "no account"):
        return finish()
    if not acc["demo"]:
        check("demo account", False, "this is a REAL money account: the test only runs on demo")
        return finish()
    check("demo account", True)
    why = broker.trading_check(False)
    if not check("trading allowed", why is None, why or "Algo Trading is on"):
        return finish()

    already = [x for x in broker.positions() if x["bot"]]
    if not check("no bot trades open", not already,
                 f"{len(already)} open: stop the app and close them first, the test uses Close All" if already else ""):
        return finish()

    print(f"\n2) Symbol {args.symbol}")
    name = broker.resolve(args.symbol)
    if not check("find symbol at broker", name is not None, name or f"{args.symbol} not offered"):
        return finish()
    spec, q = broker.spec(args.symbol), broker.quote(args.symbol)
    check("symbol info", spec["vol_min"] > 0, f"digits {spec['digits']}, min lot {spec['vol_min']}, "
                                              f"step {spec['vol_step']}, stops level {spec['stops_level']}")
    if not check("live price", bool(q and q[0] > 0), f"bid {q[0]} ask {q[1]}" if q else "no price"):
        return finish()
    df = None
    try:
        df = broker.candles(args.symbol, "H4", 100)
    except Exception as e:
        print("   ", e)
    check("read H4 candles", df is not None and len(df) > 0, f"{len(df)} candles, last {df.index[-1]}" if df is not None else "")

    bid, ask = q
    digits, pt = spec["digits"], spec["point"]
    gap = max(ask * 0.004, (spec["stops_level"] + 50) * pt)          # SL/TP ~0.4% away
    r = lambda v: round(v, digits)
    vmin, step = spec["vol_min"], spec["vol_step"]
    two = round(vmin * 2, 8)
    bot_dir = os.path.join(HERE, "test_mt5_out")
    shutil.rmtree(bot_dir, ignore_errors=True)
    tg = None
    if args.telegram:
        from telegram import Telegram
        tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    bot = Bot(cfg, broker, tg, bot_dir)

    try:
        print("\n3) Lot size from risk")
        lot, risk = bot._lot(acc["balance"], gap, spec)
        check("risk based lot size", lot is not None,
              f"{cfg['money']['risk_percent']}% risk with a stop {gap:.{digits}f} away = {lot} lots (risk {risk})")

        print("\n4) Open BUY with stop loss and take profit")
        ticket, fill, err = broker.open(args.symbol, "buy", two, r(ask - gap), r(ask + gap * 2), "v2 test")
        if not check("open BUY", ticket is not None, err or f"ticket {ticket} {two} lots at {fill}"):
            if err and "10018" in err:
                print("    The market for this symbol is closed now. Try a crypto symbol, e.g. --symbol XMRUSD")
            return finish()
        pos = wait_for(lambda: [p for p in broker.positions() if p["ticket"] == ticket])
        p = pos[0] if pos else None
        check("position visible", p is not None)
        if p:
            check("marked as bot trade", p["bot"], f"magic {MAGIC}")
            check("stop loss and TP set", p["sl"] > 0 and p["tp"] > 0, f"SL {p['sl']} TP {p['tp']}")

        print("\n5) Move stop loss to entry")
        err = broker.modify(ticket, r(p["entry"]), p["tp"])
        p2 = wait_for(lambda: [x for x in broker.positions() if x["ticket"] == ticket and abs(x["sl"] - r(p["entry"])) < pt])
        check("modify SL", err is None and bool(p2), err or f"SL now {r(p['entry'])}")

        print("\n6) Close half (TP1 style)")
        err = broker.close(ticket, vmin)
        p3 = wait_for(lambda: [x for x in broker.positions() if x["ticket"] == ticket and x["volume"] < two - step / 2])
        check("partial close", err is None and bool(p3), err or f"{p3[0]['volume']} lots left, same ticket {ticket}")

        print("\n7) Close the rest and read the result from history")
        err = broker.close(ticket)
        gone = wait_for(lambda: not [x for x in broker.positions() if x["ticket"] == ticket])
        check("full close", err is None and gone, err or "position gone")
        info = wait_for(lambda: broker.closed_info(ticket), 8)
        check("closed trade in history", info is not None,
              f"result {info['profit']:+.2f}, reason '{info['reason']}'" if info else "not found")

        print("\n8) Bot TP1 handling (half close + stop to entry), as the app does it")
        q = broker.quote(args.symbol)
        t2, fill2, err = broker.open(args.symbol, "buy", two, r(q[1] - gap), r(q[1] + gap * 2), "v2 test")
        if check("open BUY for TP1 test", t2 is not None, err or f"ticket {t2}"):
            wait_for(lambda: [x for x in broker.positions() if x["ticket"] == t2])
            bot.trades[str(t2)] = dict(ticket=t2, symbol=args.symbol, timeframe="TEST", side="buy", entry=fill2,
                                       sl=r(q[1] - gap), tp1=r(q[0] - gap / 2),     # TP1 already "reached"
                                       tp2=r(q[1] + gap * 2), volume=two, risk="", tp1_done=False, opened="",
                                       setup="", close_reason=None, missing=0)
            bot.tick()
            p4 = wait_for(lambda: [x for x in broker.positions() if x["ticket"] == t2 and x["volume"] < two - step / 2])
            check("bot closed half at TP1", bool(p4), f"{p4[0]['volume']} lots left" if p4 else "")
            check("bot moved SL to entry", bool(p4) and abs(p4[0]["sl"] - r(fill2)) < pt * 2,
                  f"SL {p4[0]['sl']} entry {fill2}" if p4 else "")

        print("\n9) Close All button (bot positions)")
        q = broker.quote(args.symbol)
        t3, _, err = broker.open(args.symbol, "sell", vmin, r(q[0] + gap), r(q[0] - gap * 2), "v2 test")
        check("open SELL", t3 is not None, err or f"ticket {t3}")
        if t3:
            bot.trades[str(t3)] = dict(ticket=t3, symbol=args.symbol, timeframe="TEST", side="sell", entry=q[0],
                                       sl=r(q[0] + gap), tp1=r(q[0] - gap), tp2=r(q[0] - gap * 2), volume=vmin,
                                       risk="", tp1_done=False, opened="", setup="", close_reason=None, missing=0)
        wait_for(lambda: len([x for x in broker.positions() if x["bot"]]) >= 2)
        n_before = len([x for x in broker.positions() if x["bot"]])
        bot.close_all()
        left = wait_for(lambda: not [x for x in broker.positions() if x["bot"]])
        check("Close All", left, f"closed {n_before} bot positions")
        for _ in range(10):                          # let the bot report each closed trade
            bot.tick()
            if not bot.trades:
                break
            time.sleep(0.5)
        check("bot reported the closed trades", not bot.trades,
              "; ".join(t.split("\n")[0] for _, t in list(bot.events)[-3:]))
    except Exception:
        traceback.print_exc()
        check("no crash", False, "see the error above")
    finally:
        leftovers = [x for x in broker.positions() if x["bot"]]
        for x in leftovers:
            broker.close(x["ticket"])
        if leftovers:
            print(f"\n(cleaned up {len(leftovers)} test positions)")
    finish()


def finish():
    ok = sum(1 for _, good, _ in results if good)
    print(f"\nRESULT: {ok} of {len(results)} checks passed")
    for name, good, detail in results:
        if not good:
            print(f"  FAILED: {name}: {detail}")
    if mt5 is not None:
        mt5.shutdown()


if __name__ == "__main__":
    main()
