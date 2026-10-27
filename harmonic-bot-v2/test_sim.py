"""Offline test: runs the whole bot on the simulator (no MT5, nothing sent to Telegram).

    python test_sim.py

Messages the bot would send go to sim_out/ (001.txt, 001.png, ...). A summary is printed at the end.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from broker_sim import SimBroker
from trader import Bot


class FileTelegram:
    def __init__(self, out):
        self.out, self.n = out, 0

    def _write(self, text, img=None):
        self.n += 1
        with open(os.path.join(self.out, f"{self.n:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        if img:
            shutil.copy(img, os.path.join(self.out, f"{self.n:03d}.png"))
        print(f"--- message {self.n} ---\n{text}\n")

    def text(self, msg):
        self._write(msg)

    def photo(self, path, caption):
        self._write(caption, path)


def run(cfg, symbols, out, charts=True, close_all_at=None):
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    cfg = {**cfg, "symbols": symbols, "timeframes": ["H4"], "scan_seconds": 0}
    broker = SimBroker(symbols, ["H4"])
    bot = Bot(cfg, broker, FileTelegram(out) if charts else None, out)
    bot.commands.put("start")
    max_open = 0
    i, closed = 0, False
    while not broker.finished:
        broker.advance()
        bot.tick()
        i += 1
        max_open = max(max_open, len([p for p in broker.positions() if p["bot"]]))
        if close_all_at and not closed and len(broker.positions()) >= close_all_at:
            bot.commands.put("close_all")
            closed = True
    acc = broker.account()
    texts = [t for _, t in bot.events]
    count = lambda word: sum(word in t for t in texts)
    return dict(balance=acc["balance"], opened=count(" opened"), tp1=count("TP1 HIT"), tp2=count("TP2 HIT"),
                sl=count("STOP LOSS HIT"), breakeven=count("closed at entry"), skipped_max=count("maximum"),
                day_stop=count("TRADING STOPPED FOR TODAY"), close_all=count("Close All button"),
                max_open_at_once=max_open, still_open=len(broker.positions()))


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    cfg = json.load(open("config.example.json", encoding="utf-8"))

    print("=== 1) normal run: 3 symbols, risk 1%, daily limit off ===")
    c = json.loads(json.dumps(cfg)); c["money"]["daily_loss_limit_percent"] = 100
    r1 = run(c, ["EURUSD", "GBPUSD", "AUDUSD"], "sim_out")

    print("=== 2) max 3 open positions with 6 symbols ===")
    c = json.loads(json.dumps(cfg)); c["money"]["daily_loss_limit_percent"] = 100
    r2 = run(c, ["S1", "S2", "S3", "S4", "S5", "S6"], "sim_out/max3", charts=False)

    print("=== 3) daily loss limit 0.3% (to force it) ===")
    c = json.loads(json.dumps(cfg)); c["money"]["daily_loss_limit_percent"] = 0.3
    r3 = run(c, ["EURUSD", "GBPUSD", "AUDUSD"], "sim_out/daily", charts=False)

    print("=== 4) Close All button pressed while trades are open ===")
    c = json.loads(json.dumps(cfg)); c["money"]["daily_loss_limit_percent"] = 100
    r4 = run(c, ["EURUSD", "GBPUSD", "AUDUSD"], "sim_out/closeall", charts=False, close_all_at=2)

    for name, r in (("normal", r1), ("max3", r2), ("daily limit", r3), ("close all", r4)):
        print(f"{name:12s}", r)


if __name__ == "__main__":
    main()
