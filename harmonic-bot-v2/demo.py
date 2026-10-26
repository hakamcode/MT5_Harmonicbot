"""Offline replay without MetaTrader: feeds bars one by one into the engine and saves
the Telegram messages + chart images to ./demo_out instead of sending them.

    python demo.py                       # synthetic bullish Bat
    python demo.py --csv EURUSD_H4.csv   # replay your own export (time,open,high,low,close)
    python demo.py --csv file.csv --send # really send to Telegram using config.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import numpy as np
import pandas as pd

import ai_review
import claude_scanner
from engine import Engine
from telegram import Telegram


class FileTelegram:
    def __init__(self, out):
        self.out, self.n = out, 0

    def _write(self, text, img=None):
        self.n += 1
        with open(f"{self.out}/{self.n:03d}.txt", "w", encoding="utf-8") as f:
            f.write(text)
        if img:
            shutil.copy(img, f"{self.out}/{self.n:03d}.png")
        print(f"--- message {self.n} ---\n{text}\n")

    def text(self, msg):
        self._write(msg)

    def photo(self, path, caption):
        self._write(caption, path)


def synthetic_bat(seed=3) -> pd.DataFrame:
    """X=1.1000 A=1.1600 B=1.1330 C=1.1480 D~1.1069 (0.886 XA) then bounce to TP1 and back."""
    rng = np.random.default_rng(seed)
    legs = [(1.1150, 1.1000, 25), (1.1000, 1.1600, 60), (1.1600, 1.1330, 35), (1.1330, 1.1480, 25),
            (1.1480, 1.1060, 45), (1.1060, 1.1270, 30), (1.1270, 1.1095, 30), (1.1095, 1.1150, 20)]
    path = []
    for a, b, n in legs:
        path += list(np.linspace(a, b, n, endpoint=False))
    close = np.array(path) + rng.normal(0, 0.0006, len(path))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.0009, len(path)))
    df = pd.DataFrame({"open": open_, "close": close,
                       "high": np.maximum(open_, close) + wick, "low": np.minimum(open_, close) - wick},
                      index=pd.date_range("2026-06-01", periods=len(path), freq="4h"))
    # make sure the swing points are exact
    for i, (_, b, _) in zip(np.cumsum([n for *_, n in legs]), legs):
        if i < len(df):
            if b < df["close"].iloc[i - 1]:
                df.iloc[i, df.columns.get_loc("low")] = b
            else:
                df.iloc[i, df.columns.get_loc("high")] = b
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--symbol", default="DEMO")
    ap.add_argument("--tf", default="H4")
    ap.add_argument("--digits", type=int, default=5)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--ai", action="store_true", help="also ask Claude to review the alerts (uses your API key, costs a little)")
    ap.add_argument("--claude-scan", action="store_true",
                    help="let Claude find the patterns (one scan every 20 bars, about 10 scans, roughly $1-2)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    # emoji-safe console output on Windows (e.g. Chinese GBK locale)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    # work from this script's folder so config/demo_out are found wherever it's launched from
    if args.csv:
        args.csv = os.path.abspath(args.csv)
    if os.path.exists(args.config):            # else look for it next to this script
        args.config = os.path.abspath(args.config)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if args.csv:
        df = pd.read_csv(args.csv)
        df.columns = [c.lower().strip("<>") for c in df.columns]
        tcol = "time" if "time" in df else ("date" if "date" in df else df.columns[0])
        df.index = pd.to_datetime(df[tcol])
        df = df[["open", "high", "low", "close"]].astype(float)
    else:
        df = synthetic_bat()

    cfg_path = args.config if os.path.exists(args.config) else "config.example.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    out = "demo_out"
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"]) if args.send else FileTelegram(out)
    reviewer = ai_review.from_config(cfg) if args.ai else None
    if args.ai and not reviewer:
        print("--ai: no working anthropic_api_key in config.json, running without Claude review")
    scanner = None
    if args.claude_scan:
        scanner = claude_scanner.from_config({**cfg, "detector": "claude"})
        if scanner:
            scanner.every, scanner.timeframes, scanner.spread, scanner.max_per_day = 20, {args.tf}, False, 99
        else:
            print("--claude-scan: no working anthropic_api_key in config.json, using the Python detector")
    eng = Engine(cfg["strategy"], tg, state_file=f"{out}/state.json", chart_dir=f"{out}/charts", reviewer=reviewer,
                 scanner=scanner, detector="claude" if scanner else "python")
    point = 10 ** -args.digits
    for i in range(120, len(df) + 1):
        eng.process(args.symbol, args.tf, df.iloc[:i], args.digits, point)
    done = {}
    for s in eng.setups.values():
        done[s.state] = done.get(s.state, 0) + 1
    print("final states:", done)


if __name__ == "__main__":
    main()
