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
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from telegram import Telegram  # noqa: E402
from trader import Bot  # noqa: E402

from PIL import Image, ImageTk  # noqa: E402  (comes with matplotlib)


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


# MetaTrader 5 look: light grey frame, white tables, blue buy / red sell, toolbox tabs at the bottom
BG, PANEL, LINE, HEAD = "#f0f0f0", "#ffffff", "#b9b9b9", "#f3f3f3"
TEXT, MUTED, SEL = "#000000", "#6b6b6b", "#cce4f7"
BUY, SELL, UP, DOWN = "#1f5fbf", "#d8261d", "#1f5fbf", "#d8261d"
ON, OFF, WARN = "#1e9e3a", "#d8261d", "#e08600"
FONT, BOLD, SMALL = ("Segoe UI", 9), ("Segoe UI", 9, "bold"), ("Segoe UI", 8)


def money(v: float) -> str:
    """MT5 number style: 10 000.00"""
    return f"{v:,.2f}".replace(",", " ")


class App:
    def __init__(self, root: tk.Tk, bot: Bot, broker, sim: bool, cfg: dict):
        self.root, self.bot, self.broker, self.sim, self.cfg = root, bot, broker, sim, cfg
        self.quit = threading.Event()
        self.error = ""
        self.shown_events = -1
        self.shown_history = -1
        self.last_bid: dict[str, float] = {}
        self.chart_path = ""                  # chart picked by the user ("" = newest)
        self.chart_cache = (None, None, None)  # (path, size, PhotoImage)
        root.title("Butterfly Bot v3" + (" - Simulator" if sim else " - MetaTrader 5"))
        root.geometry("1280x800")
        root.minsize(980, 620)
        root.configure(bg=BG)
        self._style()
        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        self._refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ look
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=FONT)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=20,
                     borderwidth=0, font=FONT)
        st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", TEXT)])
        st.configure("Treeview.Heading", background=HEAD, foreground=TEXT, relief="flat", font=FONT,
                     bordercolor=LINE, lightcolor=HEAD, darkcolor=LINE, padding=(4, 2))
        st.map("Treeview.Heading", background=[("active", "#e5e5e5")])
        st.configure("Toolbox.TNotebook", background=BG, tabposition="sw", borderwidth=0, tabmargins=(2, 0, 2, 2))
        st.configure("Toolbox.TNotebook.Tab", background=BG, foreground=TEXT, padding=(12, 3), font=FONT,
                     bordercolor=LINE, lightcolor=BG)
        st.map("Toolbox.TNotebook.Tab", background=[("selected", PANEL)], expand=[("selected", (0, 2, 0, 0))])
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=4, background=BG)

    def _panel(self, parent, title):
        """White panel with a grey title strip, like MT5's Market Watch / Navigator windows."""
        outer = tk.Frame(parent, bg=LINE, padx=1, pady=1)
        head = tk.Frame(outer, bg=HEAD, height=22)
        head.pack(fill="x")
        lbl = tk.Label(head, text=title, bg=HEAD, fg=TEXT, font=FONT, anchor="w", padx=6)
        lbl.pack(side="left", fill="x", expand=True)
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="both", expand=True)
        return outer, body, lbl

    def _tree(self, parent, cols):
        """cols: (id, heading, width, anchor)"""
        tv = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for cid, head, w, anchor in cols:
            tv.heading(cid, text=head, anchor=anchor)
            tv.column(cid, width=w, minwidth=30, anchor=anchor, stretch=cid in ("stage", "msg", "state"))
        sb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)
        tv.tag_configure("buy", foreground=BUY)
        tv.tag_configure("sell", foreground=SELL)
        tv.tag_configure("up", foreground=UP)
        tv.tag_configure("down", foreground=DOWN)
        tv.tag_configure("muted", foreground=MUTED)
        tv.tag_configure("summary", font=BOLD, background=HEAD)
        tv.tag_configure("alert", foreground=SELL)
        return tv

    def _tool_button(self, parent, text, command, fg=TEXT):
        b = tk.Label(parent, text=text, bg=BG, fg=fg, font=FONT, padx=10, pady=4, cursor="hand2",
                     relief="flat", bd=1)
        b.bind("<Enter>", lambda e: b.config(bg="#dcebfb", relief="solid"))
        b.bind("<Leave>", lambda e: b.config(bg=BG, relief="flat"))
        b.bind("<Button-1>", lambda e: command())
        return b

    # ------------------------------------------------------------------ layout
    def _build(self):
        # toolbar
        bar = tk.Frame(self.root, bg=BG, bd=0)
        bar.pack(fill="x", padx=2, pady=(2, 0))
        self.algo_btn = self._tool_button(bar, "▶  Start Bot", self._toggle)
        self.algo_btn.config(font=BOLD)
        self.algo_btn.pack(side="left")
        tk.Frame(bar, bg=LINE, width=1, height=22).pack(side="left", padx=4, fill="y", pady=3)
        scope = "All Positions" if self.cfg["money"].get("close_all_button", "bot") == "all" else "All Bot Positions"
        self._tool_button(bar, f"✖  Close {scope}", self._close_all, fg=SELL).pack(side="left")
        tk.Frame(bar, bg=LINE, width=1, height=22).pack(side="left", padx=4, fill="y", pady=3)
        self.algo_chip = tk.Label(bar, text="", bg=BG, font=BOLD, padx=8)
        self.algo_chip.pack(side="left")
        self.acc_lbl = tk.Label(bar, text="connecting…", bg=BG, fg=MUTED, font=FONT, padx=8)
        self.acc_lbl.pack(side="right")
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x", pady=(2, 0))

        # status bar
        sb = tk.Frame(self.root, bg=BG)
        sb.pack(side="bottom", fill="x")
        tk.Frame(self.root, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.status_lbl = tk.Label(sb, text="", bg=BG, font=FONT, anchor="w", padx=6)
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.day_lbl = tk.Label(sb, text="", bg=BG, font=FONT, padx=10)
        self.day_lbl.pack(side="right")
        tk.Frame(sb, bg=LINE, width=1).pack(side="right", fill="y", pady=2)
        self.conn_lbl = tk.Label(sb, text="", bg=BG, font=FONT, padx=10)
        self.conn_lbl.pack(side="right")

        main = ttk.PanedWindow(self.root, orient="vertical")
        main.pack(fill="both", expand=True, padx=2, pady=2)
        top = ttk.PanedWindow(main, orient="horizontal")
        main.add(top, weight=3)

        # left column: Market Watch + Bot settings (like Navigator)
        left = ttk.PanedWindow(top, orient="vertical")
        top.add(left, weight=0)
        mw, body, self.mw_title = self._panel(left, "Market Watch")
        self.mw = self._tree(body, [("sym", "Symbol", 90, "w"), ("bid", "Bid", 70, "e"),
                                    ("ask", "Ask", 70, "e"), ("spr", "Spread", 50, "e")])
        left.add(mw, weight=3)
        nav, body, _ = self._panel(left, "Bot settings")
        self.nav = self._tree(body, [("k", "Rule", 120, "w"), ("v", "Value", 160, "w")])
        mm, bf = self.cfg["money"], self.cfg["butterfly"]
        for k, v in (("Pattern", "Butterfly (bull + bear)"),
                     ("Timeframes", ", ".join(self.cfg["timeframes"])),
                     ("Risk per trade", f"{mm['risk_percent']}% of balance"),
                     ("Max positions", f"{mm['max_open_positions']} at the same time"),
                     ("Stop loss", f"beyond {bf['sl_xa']} XA"),
                     ("TP1", f"{bf['tp1_ad']} of A→D, close {mm['tp1_close_percent']}%"),
                     ("After TP1", "stop loss moved to entry"),
                     ("TP2", f"{bf['tp2_ad']} of A→D"),
                     ("Daily stop", f"-{mm['daily_loss_limit_percent']}% of balance"),
                     ("Account", "live allowed" if self.cfg.get("allow_live_trading") else "demo only")):
            self.nav.insert("", "end", values=(k, v))
        left.add(nav, weight=2)

        # chart window
        ch, body, self.chart_title = self._panel(top, "Chart")
        self.canvas = tk.Canvas(body, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_chart(force=True))
        self.chart_tabs = tk.Frame(body, bg=HEAD)
        self.chart_tabs.pack(fill="x")
        top.add(ch, weight=1)

        # toolbox
        tb_outer, tb_body, self.tb_title = self._panel(main, "Toolbox")
        main.add(tb_outer, weight=2)
        nb = ttk.Notebook(tb_body, style="Toolbox.TNotebook")
        nb.pack(fill="both", expand=True)

        f = tk.Frame(nb, bg=PANEL)
        self.trade = self._tree(f, [("sym", "Symbol", 85, "w"), ("ticket", "Ticket", 80, "w"), ("tf", "TF", 40, "w"),
                                    ("type", "Type", 45, "w"), ("vol", "Volume", 60, "e"), ("open", "Price", 80, "e"),
                                    ("sl", "S / L", 80, "e"), ("tp", "T / P", 80, "e"), ("now", "Price", 80, "e"),
                                    ("profit", "Profit", 85, "e"), ("stage", "Bot stage", 200, "w")])
        self.trade.bind("<Button-3>", self._trade_menu)
        self.trade.bind("<Double-1>", self._trade_menu)
        self.trade.bind("<<TreeviewSelect>>", lambda e: self._pick_chart(self.trade))
        nb.add(f, text="Trade")

        f = tk.Frame(nb, bg=PANEL)
        self.watch = self._tree(f, [("sym", "Symbol", 85, "w"), ("tf", "TF", 40, "w"), ("pat", "Pattern", 150, "w"),
                                    ("prz", "PRZ (D zone)", 170, "e"), ("stop", "Stop", 85, "e"),
                                    ("state", "Status", 260, "w")])
        self.watch.bind("<<TreeviewSelect>>", lambda e: self._pick_chart(self.watch))
        self.watch_tab = f
        nb.add(f, text="Butterflies")

        f = tk.Frame(nb, bg=PANEL)
        self.hist = self._tree(f, [("time", "Time", 120, "w"), ("ticket", "Ticket", 80, "w"), ("sym", "Symbol", 85, "w"),
                                   ("type", "Type", 45, "w"), ("vol", "Volume", 60, "e"), ("open", "Price", 80, "e"),
                                   ("close", "Close", 80, "e"), ("why", "Closed by", 150, "w"),
                                   ("profit", "Profit", 85, "e")])
        nb.add(f, text="History")

        f = tk.Frame(nb, bg=PANEL)
        self.journal = self._tree(f, [("time", "Time", 130, "w"), ("msg", "Message", 900, "w")])
        nb.add(f, text="Journal")
        self.nb = nb

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
        try:
            self._update()
        except Exception:
            logging.exception("screen refresh failed")
        self.root.after(500, self._refresh)

    def _fill(self, tv, rows):
        """Replace a table's rows but keep the selected row selected."""
        sel = tv.selection()
        keep = tv.item(sel[0], "values")[:2] if sel else None
        tv.delete(*tv.get_children())
        for values, tags in rows:
            iid = tv.insert("", "end", values=values, tags=tags)
            if keep and tuple(str(v) for v in values[:2]) == tuple(keep):
                tv.selection_set(iid)

    def _update(self):
        snap = self.bot.snapshot
        acc = snap.get("account") if snap else None
        cur = acc["currency"] if acc else ""
        running = bool(snap and snap["running"])

        # toolbar + status bar
        self.algo_btn.config(text="■  Stop Bot" if running else "▶  Start Bot", fg=OFF if running else ON)
        if snap and snap["day_stopped"]:
            self.algo_chip.config(text="● Stopped for today (daily loss limit)", fg=WARN)
        else:
            self.algo_chip.config(text="● Bot ON" if running else "● Bot OFF", fg=ON if running else MUTED)
        if self.error:
            self.status_lbl.config(text="⚠ " + self.error[:160], fg=SELL)
        elif snap:
            self.status_lbl.config(text=snap["status"], fg=TEXT)
        if acc:
            kind = "Demo" if acc["demo"] else "REAL MONEY"
            self.acc_lbl.config(text=f"{acc['login']}: {acc['server']} · {kind}", fg=MUTED if acc["demo"] else SELL)
            self.conn_lbl.config(text=f"● {self.broker.name} connected", fg=ON)
            day = snap["day_pl"]
            self.day_lbl.config(text=f"Today: {money(day)} {cur}   (stops at -{money(snap['day_limit'])})",
                                fg=SELL if day < 0 else TEXT)
        else:
            self.conn_lbl.config(text="● No connection", fg=SELL)
        if not snap:
            return

        # Market Watch
        rows = []
        for q in snap.get("quotes", []):
            d = q["digits"]
            prev = self.last_bid.get(q["symbol"])
            tag = "up" if prev is not None and q["bid"] > prev else "down" if prev is not None and q["bid"] < prev else ""
            self.last_bid[q["symbol"]] = q["bid"]
            spread = round((q["ask"] - q["bid"]) * 10 ** d)
            rows.append(((q["symbol"], f"{q['bid']:.{d}f}", f"{q['ask']:.{d}f}", spread), (tag,)))
        self._fill(self.mw, rows)
        self.mw_title.config(text=f"Market Watch: {time.strftime('%H:%M:%S')}")

        # Trade tab + MT5 balance line
        rows = []
        for p in snap["positions"]:
            d = p["digits"]
            fmt = lambda v: f"{v:.{d}f}" if v else ""
            rows.append(((p["symbol"], p["ticket"], p["timeframe"], p["side"], f"{p['volume']:.2f}", fmt(p["entry"]),
                          fmt(p["sl"]), fmt(p["tp"]), fmt(p["price"]), money(p["profit"]), p["stage"]),
                         (p["side"],)))
        if acc:
            n = sum(1 for p in snap["positions"] if p["bot"])
            rows.append((("", "", "", "", "", "", "", "", "", money(acc["profit"]),
                          f"Balance: {money(acc['balance'])} {cur}   Equity: {money(acc['equity'])}   "
                          f"Bot positions: {n} / {self.cfg['money']['max_open_positions']}"), ("summary",)))
        self._fill(self.trade, rows)

        # Butterflies tab
        rows = []
        for s in snap["watching"]:
            d = s["digits"]
            rows.append(((s["symbol"], s["timeframe"], s["label"], f"{s['inner']:.{d}f} – {s['outer']:.{d}f}",
                          f"{s['stop']:.{d}f}",
                          "IN THE PRZ, entry signal: waiting for a free place (max positions)"
                          if s.get("waiting") else
                          "IN THE PRZ, waiting for reversal candle" if s["touched"] else "waiting for price"),
                         ("buy" if "Bullish" in s["label"] else "sell",)))
        self._fill(self.watch, rows)
        self.nb.tab(self.watch_tab, text=f"Butterflies ({len(rows)})")

        # History tab
        hist = snap.get("history", [])
        if len(hist) != self.shown_history:
            self.shown_history = len(hist)
            self.hist.delete(*self.hist.get_children())
            for h in reversed(hist):
                d = h["digits"]
                self.hist.insert("", "end", tags=(h["side"],), values=(
                    h["time"], h["ticket"], h["symbol"], h["side"], f"{h['volume']:.2f}", f"{h['entry']:.{d}f}",
                    f"{h['close']:.{d}f}", h["reason"], money(h["profit"])))
            if hist:
                wins = sum(1 for h in hist if h["profit"] > 0)
                self.hist.insert("", "end", tags=("summary",), values=(
                    f"{len(hist)} trades", "", "", "", "", "", "", f"{wins} won / {len(hist) - wins} lost",
                    money(sum(h["profit"] for h in hist))))

        # Journal tab (newest first)
        events = snap.get("events", [])
        if len(events) != self.shown_events:
            self.shown_events = len(events)
            self.journal.delete(*self.journal.get_children())
            for t, text in reversed(events[-200:]):
                one = " · ".join(x.strip() for x in text.splitlines() if x.strip())
                bad = any(w in text for w in ("STOP LOSS", "FAILED", "NOT", "STOPPED FOR TODAY", "❗"))
                self.journal.insert("", "end", values=(t, one), tags=("alert",) if bad else ())

        self._chart_tabs(snap)
        self._draw_chart()
        while self.bot.notices:
            self._toast(*self.bot.notices.popleft())

    # ------------------------------------------------------------------ pop-up notifications (bottom right)
    def _toast(self, level, title, body):
        color = {"ok": ON, "info": BUY, "warn": WARN, "error": SELL}.get(level, BUY)
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=LINE)
        frame = tk.Frame(t, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(frame, bg=color, width=6).pack(side="left", fill="y")
        box = tk.Frame(frame, bg=PANEL, padx=12, pady=8)
        box.pack(side="left", fill="both", expand=True)
        tk.Label(box, text=title.strip(), bg=PANEL, fg=color, font=BOLD, anchor="w", justify="left",
                 wraplength=340).pack(fill="x")
        if body.strip():
            tk.Label(box, text=body.strip()[:400], bg=PANEL, fg=TEXT, font=FONT, anchor="w", justify="left",
                     wraplength=340).pack(fill="x", pady=(3, 0))
        self.toasts = [x for x in getattr(self, "toasts", []) if x.winfo_exists()] + [t]
        self._place_toasts()
        close = lambda e=None: (t.destroy(), self._place_toasts())
        for w in (t, frame, box) + tuple(box.winfo_children()):
            w.bind("<Button-1>", close)
        t.after(8000, lambda: t.winfo_exists() and close())
        self.root.bell()

    def _place_toasts(self):
        self.toasts = [x for x in getattr(self, "toasts", []) if x.winfo_exists()]
        self.root.update_idletasks()
        right = self.root.winfo_rootx() + self.root.winfo_width() - 12
        bottom = self.root.winfo_rooty() + self.root.winfo_height() - 40
        for t in reversed(self.toasts[-4:]):
            t.update_idletasks()
            w, h = max(t.winfo_reqwidth(), 300), t.winfo_reqheight()
            t.geometry(f"{w}x{h}+{right - w}+{bottom - h}")
            bottom -= h + 8
        for t in self.toasts[:-4]:
            t.destroy()

    # ------------------------------------------------------------------ chart window
    def _charts(self, snap):
        """(title, path) of every chart worth showing: open trades first, then forming Butterflies."""
        out = []
        for p in snap["positions"]:
            if p.get("chart") and os.path.exists(p["chart"]):
                out.append((f"{p['symbol']},{p['timeframe']} #{p['ticket']}", p["chart"], p.get("key")))
        for s in snap["watching"]:
            if s.get("chart") and os.path.exists(s["chart"]):
                out.append((f"{s['symbol']},{s['timeframe']} {s['label'].split()[0]}", s["chart"], s.get("key")))
        return out

    def _chart_tabs(self, snap):
        charts = self._charts(snap)
        paths = [p for _, p, _ in charts]
        if self.chart_path not in paths and (paths or not os.path.exists(self.chart_path)):
            self.chart_path = paths[0] if paths else ""   # else keep showing the last chart
        self.chart_keys = {p: k for _, p, k in charts}
        self.bot.live_key = self.chart_keys.get(self.chart_path)    # the bot redraws this chart every 5 s
        key = tuple(charts), self.chart_path
        if getattr(self, "_tabs_key", None) == key:
            return
        self._tabs_key = key
        for w in self.chart_tabs.winfo_children():
            w.destroy()
        for title, path, _ in charts[:10]:
            active = path == self.chart_path
            b = tk.Label(self.chart_tabs, text=title, font=FONT, padx=8, pady=2, cursor="hand2",
                         bg=PANEL if active else HEAD, fg=TEXT if active else MUTED,
                         relief="solid" if active else "flat", bd=1 if active else 0)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, p=path: self._show(p))
        title = next((t for t, p, _ in charts if p == self.chart_path), None)
        if title:
            self.chart_title.config(text=f"Chart: {title}")
        elif self.chart_path:
            self.chart_title.config(text="Chart: last trade (closed)")
        else:
            self.chart_title.config(text="Chart")

    def _show(self, path):
        self.chart_path = path
        self.bot.live_key = getattr(self, "chart_keys", {}).get(path, self.bot.live_key)
        self._tabs_key = None
        self._draw_chart(force=True)

    def _pick_chart(self, tv):
        sel = tv.selection()
        if not sel or not self.bot.snapshot:
            return
        vals = tv.item(sel[0], "values")
        snap = self.bot.snapshot
        if tv is self.trade:
            path = next((p.get("chart") for p in snap["positions"] if str(p["ticket"]) == str(vals[1])), "")
        else:
            path = next((s.get("chart") for s in snap["watching"]
                         if s["symbol"] == vals[0] and s["timeframe"] == vals[1]), "")
        if path:
            self._show(path)

    def _draw_chart(self, force=False):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 50 or h < 50:
            return
        path = self.chart_path
        try:
            mtime = os.path.getmtime(path) if path else 0
        except OSError:
            mtime = 0
        if not force and self.chart_cache[0] == path and self.chart_cache[1] == (w, h, mtime):
            return
        self.canvas.delete("all")
        if not path or not os.path.exists(path):
            self.chart_cache = (path, (w, h, mtime), None)
            self.canvas.create_text(w // 2, h // 2, fill=MUTED, font=FONT, justify="center",
                                    text="Charts of forming Butterflies and open trades appear here.\n"
                                         "Press Start Bot to begin scanning.")
            return
        try:
            img = Image.open(path)
            scale = min(w / img.width, h / img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            logging.exception("could not show chart %s", path)
            return
        self.chart_cache = (path, (w, h, mtime), photo)
        self.canvas.create_image(w // 2, h // 2, image=photo)

    # ------------------------------------------------------------------ buttons
    def _toggle(self):
        starting = not self.bot.snapshot.get("running")
        self.algo_btn.config(text="…  Starting" if starting else "…  Stopping", fg=MUTED)
        self.bot.commands.put("start" if starting else "stop")

    def _trade_menu(self, event):
        iid = self.trade.identify_row(event.y)
        if not iid:
            return
        self.trade.selection_set(iid)
        vals = self.trade.item(iid, "values")
        if not vals[1]:
            return                               # the balance line
        m = tk.Menu(self.root, tearoff=0, font=FONT)
        m.add_command(label=f"Close position #{vals[1]} ({vals[0]} {vals[3]} {vals[4]})",
                      command=lambda: self._close_one(vals))
        m.add_separator()
        m.add_command(label="Close all bot positions", command=self._close_all)
        m.tk_popup(event.x_root, event.y_root)

    def _close_one(self, vals):
        if messagebox.askyesno("Close position", f"Close {vals[0]} {vals[3]} {vals[4]} lots (#{vals[1]}) now at "
                                                 f"market price?\n\nProfit now: {vals[9]}", icon="warning"):
            self.bot.commands.put(("close", int(vals[1])))

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
        shutil.rmtree(os.path.join(data_dir, "charts"), ignore_errors=True)
    else:
        from broker_mt5 import MT5Broker
        broker = MT5Broker(cfg["mt5"])
        data_dir = HERE
    bot = Bot(cfg, broker, tg, data_dir)
    App(root, bot, broker, args.sim, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
