"""Live bot: scans MetaTrader 5 for harmonic patterns and sends alerts to Telegram.

    python bot.py                # uses config.json
    python bot.py --once         # one scan then exit (good for testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import ai_review
import claude_scanner
import mt5_feed
from engine import Engine
from telegram import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    # emoji-safe console output on Windows (e.g. Chinese GBK locale)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    # work from this script's folder so config/state/charts are found wherever it's launched from
    if os.path.exists(args.config):            # else look for it next to this script
        args.config = os.path.abspath(args.config)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(args.config):
        sys.exit(f"Settings file not found: {os.path.abspath(args.config)}\n"
                 "Copy config.example.json to config.json in this folder and fill in your MT5 and Telegram details.")
    cfg = json.load(open(args.config, encoding="utf-8"))

    mt5_feed.connect(cfg["mt5"])
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"], cfg["telegram"].get("dry_run", False))
    problem = tg.check()
    if problem:
        sys.exit(f"{problem}\n(settings file used: {args.config})")
    missing = [s for s in cfg["symbols"] if mt5_feed.resolve(s) is None]
    if missing:
        log.warning("skipping symbols your broker doesn't offer: %s", ", ".join(missing))
        cfg["symbols"] = [s for s in cfg["symbols"] if s not in missing]
    reviewer = ai_review.from_config(cfg)
    log.info("Claude review: %s", f"ON ({reviewer.model})" if reviewer else "off (no anthropic_api_key in config.json)")
    scanner = claude_scanner.from_config(cfg)
    detector = cfg.get("detector", "python") if scanner else "python"
    log.info("pattern finder: %s", detector + (f" (Claude on {', '.join(sorted(scanner.timeframes))})" if scanner else ""))
    st = cfg["strategy"]
    if st.get("auto_trade"):
        why = mt5_feed.trading_check(bool(cfg.get("allow_live_trading")))
        if why:
            log.warning("auto trading OFF: %s", why)
            tg.text(f"⚠️ Auto trading is OFF: {why}. Alerts still run.")
            st["auto_trade"] = False
    eng = Engine(st, tg, trader=mt5_feed, reviewer=reviewer, scanner=scanner, detector=detector)
    tg.text("🤖 Harmonic scanner started: " + ", ".join(cfg["symbols"]) + " on " + ", ".join(cfg["timeframes"])
            + {"python": "", "claude": "\n🧠 Patterns found by: Claude AI", "both": "\n🧠 Patterns found by: Python + Claude AI"}[detector]
            + (f" ({', '.join(sorted(scanner.timeframes))})" if scanner else "")
            + ("\n🧠 Claude AI review: ON" if reviewer else "")
            + (f"\n💹 Auto trading: ON (lot {min(st['lot_size'], st.get('max_lot', 0.05))}, max {st.get('max_open_trades', 3)} trades)"
               if st.get("auto_trade") else "\n💹 Auto trading: off (alerts only)"))

    last_bar: dict[str, object] = {}
    while True:
        for sym in cfg["symbols"]:
            for tf in cfg["timeframes"]:
                try:
                    df = mt5_feed.candles(sym, tf, cfg["bars"])
                    k = f"{sym}|{tf}"
                    if last_bar.get(k) == df.index[-1]:
                        continue                         # no new closed bar yet
                    last_bar[k] = df.index[-1]
                    digits, point = mt5_feed.symbol_digits(sym)
                    eng.process(sym, tf, df, digits, point)
                except Exception:
                    log.exception("scan failed for %s %s", sym, tf)
        if args.once:
            break
        time.sleep(cfg.get("poll_seconds", 30))


if __name__ == "__main__":
    main()
