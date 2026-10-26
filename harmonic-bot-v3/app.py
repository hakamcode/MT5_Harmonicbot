"""Butterfly Bot v3: desktop app.

    python app.py          # trade with MetaTrader 5 (config.json)
    python app.py --sim    # try the app on simulated prices (no MT5, nothing sent to Telegram)
    python app.py --sim --send   # simulator, but really send the messages to your Telegram
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from telegram import Telegram  # noqa: E402
from trader import Bot  # noqa: E402

GREEN, RED, GREY, BLUE, AMBER = "#1b8a3a", "#c62828", "#616161", "#1565c0", "#e65100"


def _merge(defaults: dict, mine: dict) -> dict:
    """Your settings on top of the defaults, so a missing section or setting never stops the app."""
    out = dict(defaults)
    for k, v in mine.items():
        out[k] = _merge(defaults[k], v) if isinstance(v, dict) and isinstance(defaults.get(k), dict) else v
    return out


def load_config(sim: bool) -> tuple[dict, str]:
    with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
        defaults = json.load(f)
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        if not sim:
            raise FileNotFoundError(
                f"Settings file not found:\n{path}\n\nCopy config.example.json to config.json in this folder "
                "and fill in your Telegram details (see README.md).")
        return defaults, os.path.join(HERE, "config.example.json")
    with open(path, encoding="utf-8") as f:
        mine = json.load(f)
    missing = [k for k in ("money", "butterfly") if k not in mine]
    if missing:
        logging.warning("config.json has no %s section (old v2 file?): using the v3 defaults from "
                        "config.example.json for it", " / ".join(missing))
    return _merge(defaults, mine), path


class App:
    def __init__(self, root: tk.Tk, bot: Bot, broker, sim: bool, cfg: dict):
        self.root, self.bot, self.broker, self.sim, self.cfg = root, bot, broker, sim, cfg
        self.quit = threading.Event()
        self.error = ""
        self.shown_events = 0
        root.title("Butterfly Bot v3" + ("  (SIMULATOR)" if sim else ""))
        root.geometry("1180x760")
        root.minsize(900, 600)
        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        self._refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        big = ("Segoe UI", 11, "bold")

        top = tk.Frame(self.root, padx=12, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="🦋 Butterfly Bot v3", font=("Segoe UI", 16, "bold")).pack(side="left")
        self.acc_lbl = tk.Label(top, text="connecting…", font=("Segoe UI", 10), fg=GREY)
        self.acc_lbl.pack(side="right")

        bar = tk.Frame(self.root, padx=12)
        bar.pack(fill="x")
        self.start_btn = tk.Button(bar, text="▶  Start bot", font=big, bg=GREEN, fg="white", width=14,
                                   command=lambda: self.bot.commands.put("start"))
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = tk.Button(bar, text="⏸  Stop bot", font=big, width=14,
                                  command=lambda: self.bot.commands.put("stop"))
        self.stop_btn.pack(side="left", padx=(0, 8))
        scope = "ALL" if self.cfg["money"].get("close_all_button", "bot") == "all" else "bot"
        tk.Button(bar, text=f"✖  Close all {scope} positions", font=big, bg=RED, fg="white",
                  command=self._close_all).pack(side="left")
        self.status_lbl = tk.Label(bar, text="", font=big, anchor="w", padx=14)
        self.status_lbl.pack(side="left", fill="x", expand=True)

        cards = tk.Frame(self.root, padx=12, pady=8)
        cards.pack(fill="x")
        self.card = {}
        for key, title in (("balance", "Balance"), ("equity", "Equity"), ("floating", "Open P/L"),
                           ("today", "Today P/L"), ("open", "Bot positions")):
            f = tk.Frame(cards, bd=1, relief="solid", padx=12, pady=6)
            f.pack(side="left", padx=(0, 8), fill="x", expand=True)
            tk.Label(f, text=title, font=("Segoe UI", 9), fg=GREY).pack(anchor="w")
            self.card[key] = tk.Label(f, text="-", font=("Segoe UI", 14, "bold"))
            self.card[key].pack(anchor="w")
            if key in ("today", "open"):
                self.card[key + "_sub"] = tk.Label(f, text="", font=("Segoe UI", 8), fg=GREY)
                self.card[key + "_sub"].pack(anchor="w")

        body = ttk.PanedWindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        pos = ttk.LabelFrame(body, text=" Open positions (live) ")
        cols = ("ticket", "symbol", "tf", "side", "lots", "entry", "sl", "tp", "price", "pl", "stage")
        heads = ("Ticket", "Symbol", "TF", "Side", "Lots", "Entry", "Stop loss", "TP2", "Price", "P/L", "Stage")
        widths = (80, 90, 45, 55, 55, 90, 90, 90, 90, 90, 210)
        self.pos = ttk.Treeview(pos, columns=cols, show="headings", height=6)
        for c, h, w in zip(cols, heads, widths):
            self.pos.heading(c, text=h)
            self.pos.column(c, width=w, anchor="center" if c not in ("stage",) else "w")
        self.pos.tag_configure("win", foreground=GREEN)
        self.pos.tag_configure("loss", foreground=RED)
        self.pos.pack(fill="both", expand=True, padx=4, pady=4)
        body.add(pos, weight=3)

        watch = ttk.LabelFrame(body, text=" Butterflies forming (waiting for price to reach the PRZ) ")
        cols = ("symbol", "tf", "pattern", "prz", "stop", "state")
        heads = ("Symbol", "TF", "Pattern", "PRZ (D zone)", "Stop", "Status")
        self.watch = ttk.Treeview(watch, columns=cols, show="headings", height=4)
        for c, h, w in zip(cols, heads, (90, 45, 150, 200, 100, 300)):
            self.watch.heading(c, text=h)
            self.watch.column(c, width=w, anchor="center" if c != "state" else "w")
        self.watch.pack(fill="both", expand=True, padx=4, pady=4)
        body.add(watch, weight=2)

        logf = ttk.LabelFrame(body, text=" Activity (also sent to Telegram) ")
        self.log = tk.Text(logf, height=8, font=("Consolas", 9), wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        body.add(logf, weight=2)

        mm, bf = self.cfg["money"], self.cfg["butterfly"]
        rules = (f"Rules: risk {mm['risk_percent']}% per trade · max {mm['max_open_positions']} open at once · "
                 f"stop beyond {bf['sl_xa']} XA · TP1 {bf['tp1_ad']} A→D (close {mm['tp1_close_percent']}%, SL to entry) · "
                 f"TP2 {bf['tp2_ad']} A→D · stop for the day at -{mm['daily_loss_limit_percent']}% · "
                 f"{'live trading ALLOWED' if self.cfg.get('allow_live_trading') else 'demo accounts only'}")
        tk.Label(self.root, text=rules, font=("Segoe UI", 9), fg=GREY, anchor="w", padx=12).pack(fill="x", pady=(0, 6))

    # ------------------------------------------------------------------ worker thread (all broker work)
    def _worker(self):
        connected = False
        while not self.quit.is_set():
            try:
                if not connected:
                    self.broker.connect()
                    connected, self.error = True, ""
                if self.sim:
                    self.broker.advance()
                self.bot.tick()
                time.sleep(0.04 if self.sim else 1.0)
            except Exception as e:  # keep the app alive; retry the connection
                logging.exception("worker error")
                self.error = str(e)
                connected = False if not self.sim else connected
                time.sleep(5)

    # ------------------------------------------------------------------ screen refresh
    def _refresh(self):
        snap = self.bot.snapshot
        acc = snap.get("account")
        cur = acc["currency"] if acc else ""
        money = lambda v: f"{v:,.2f} {cur}"
        if self.error:
            self.status_lbl.config(text="⚠ " + self.error[:140], fg=RED)
        elif snap:
            color = GREEN if snap["running"] and not snap["day_stopped"] else (AMBER if snap["day_stopped"] else GREY)
            self.status_lbl.config(text=("● " if snap["running"] else "■ ") + snap["status"], fg=color)
        if snap:
            self.start_btn.config(state="disabled" if snap["running"] else "normal",
                                  bg="#bdbdbd" if snap["running"] else GREEN)
            self.stop_btn.config(state="normal" if snap["running"] else "disabled")
        if acc:
            kind = "DEMO" if acc["demo"] else "REAL MONEY"
            self.acc_lbl.config(text=f"{self.broker.name} · {acc['server']} · #{acc['login']} · {kind}",
                                fg=BLUE if acc["demo"] else RED)
            self.card["balance"].config(text=money(acc["balance"]))
            self.card["equity"].config(text=money(acc["equity"]))
            self.card["floating"].config(text=money(acc["profit"]), fg=GREEN if acc["profit"] >= 0 else RED)
            self.card["today"].config(text=money(snap["day_pl"]), fg=GREEN if snap["day_pl"] >= 0 else RED)
            self.card["today_sub"].config(text=("STOPPED for today" if snap["day_stopped"] else
                                                f"stops at -{snap['day_limit']:,.2f}"))
            n = sum(1 for p in snap["positions"] if p["bot"])
            self.card["open"].config(text=f"{n} / {self.cfg['money']['max_open_positions']}")
            manual = len(snap["positions"]) - n
            self.card["open_sub"].config(text=f"+ {manual} manual" if manual else "max at the same time")

            self.pos.delete(*self.pos.get_children())
            for p in snap["positions"]:
                d = p["digits"]
                fmt = lambda v: f"{v:.{d}f}" if v else "-"
                self.pos.insert("", "end", tags=("win" if p["profit"] >= 0 else "loss",), values=(
                    p["ticket"], p["symbol"], p["timeframe"], p["side"].upper(), f"{p['volume']:g}", fmt(p["entry"]),
                    fmt(p["sl"]), fmt(p["tp"]), fmt(p["price"]), f"{p['profit']:+,.2f}", p["stage"]))
            self.watch.delete(*self.watch.get_children())
            for s in snap["watching"]:
                d = s["digits"]
                self.watch.insert("", "end", values=(
                    s["symbol"], s["timeframe"], s["label"], f"{s['inner']:.{d}f} – {s['outer']:.{d}f}",
                    f"{s['stop']:.{d}f}", "IN THE PRZ, waiting for reversal candle" if s["touched"] else "waiting for price"))
        events = snap.get("events", []) if snap else []
        if len(events) != self.shown_events:
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            for t, text in reversed(events[-150:]):
                self.log.insert("end", f"{t}  {text}\n\n")
            self.log.config(state="disabled")
            self.shown_events = len(events)
        self.root.after(500, self._refresh)

    # ------------------------------------------------------------------ buttons
    def _close_all(self):
        snap = self.bot.snapshot
        scope_all = self.cfg["money"].get("close_all_button", "bot") == "all"
        ps = [p for p in snap.get("positions", []) if p["bot"] or scope_all]
        if not ps:
            messagebox.showinfo("Close all", "There are no open positions to close.")
            return
        total = sum(p["profit"] for p in ps)
        cur = (snap.get("account") or {}).get("currency", "")
        lines = "\n".join(f"  {p['symbol']} {p['side'].upper()} {p['volume']:g} lots: {p['profit']:+,.2f}" for p in ps)
        if messagebox.askyesno("Close all positions?",
                               f"Close these {len(ps)} positions now at market price?\n\n{lines}\n\n"
                               f"Total P/L: {total:+,.2f} {cur}\n\nThis cannot be undone.", icon="warning"):
            self.bot.commands.put("close_all")

    def _on_close(self):
        if self.bot.running and not messagebox.askyesno(
                "Quit", "The bot is running. Stop it and close the app?\n\n"
                        "Open trades keep their stop loss and TP2 in MT5, but TP1 (half close + stop to entry) "
                        "is only handled while the app is open."):
            return
        self.bot.commands.put("stop")
        time.sleep(1.5)
        self.quit.set()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="simulated prices, no MT5 needed")
    ap.add_argument("--send", action="store_true", help="with --sim: really send messages to Telegram")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if s:
            s.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(HERE)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(os.path.join(HERE, "bot.log"), encoding="utf-8"),
                                  logging.StreamHandler()])
    root = tk.Tk()
    try:
        cfg, cfg_path = load_config(args.sim)
    except Exception as e:
        root.withdraw()
        messagebox.showerror("Butterfly Bot", str(e))
        return
    tg = None
    if not args.sim or args.send:
        t = cfg["telegram"]
        tg = Telegram(t["bot_token"], t["chat_id"], t.get("dry_run", False))
        try:
            problem = tg.check()
        except Exception as e:
            problem = f"could not reach Telegram ({e})"
        if problem:
            messagebox.showwarning("Telegram", f"{problem}\n\nThe bot will run WITHOUT Telegram messages.\n"
                                               f"(settings file: {cfg_path})")
            tg = None
    if args.sim:
        from broker_sim import SimBroker
        cfg = {**cfg, "symbols": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"], "timeframes": ["H4"], "scan_seconds": 0}
        broker = SimBroker(cfg["symbols"], cfg["timeframes"])
        data_dir = os.path.join(HERE, "sim_app")
        os.makedirs(data_dir, exist_ok=True)
        state = os.path.join(data_dir, "state_v3.json")
        if os.path.exists(state):
            os.remove(state)                      # every simulator run starts fresh
    else:
        from broker_mt5 import MT5Broker
        broker = MT5Broker(cfg["mt5"])
        data_dir = HERE
    bot = Bot(cfg, broker, tg, data_dir)
    App(root, bot, broker, args.sim, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
