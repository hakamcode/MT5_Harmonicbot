"""The bot: finds Butterflies, opens trades with money management, manages TP1/TP2/SL, daily loss stop.

All broker work happens in one worker thread that calls Bot.tick() about once a second.
The app (app.py) only reads Bot.snapshot and puts commands (start, stop, close_all) in Bot.commands.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import time
from collections import deque
from datetime import datetime

import pandas as pd

import chart
from butterfly import Setup, find_setups, step, targets

log = logging.getLogger("bot")


class Bot:
    def __init__(self, cfg: dict, broker, telegram, base_dir: str):
        self.cfg, self.bf, self.mm = cfg, cfg["butterfly"], cfg["money"]
        self.broker, self.tg = broker, telegram
        self.state_file = os.path.join(base_dir, "state_v3.json")
        self.chart_dir = os.path.join(base_dir, "charts")
        os.makedirs(self.chart_dir, exist_ok=True)
        self.running = False
        self.status = "Stopped"
        self.block = None                      # why the bot may not trade on this account right now
        self.setups: dict[str, Setup] = {}
        self.trades: dict[str, dict] = {}      # ticket -> trade the bot opened
        self.day = {"date": None, "start_balance": None, "stopped": ""}
        self.last_bar: dict[str, str] = {}
        self.last_scan = 0.0
        self.commands: queue.Queue = queue.Queue()
        self.events: deque = deque(maxlen=300)
        self.snapshot: dict = {}
        self.history: list[dict] = []          # closed bot trades, newest last
        self.symbols = list(cfg["symbols"])
        self._digits: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------------ state file
    def _load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                d = json.load(f)
            self.setups = {k: Setup.from_dict(v) for k, v in d.get("setups", {}).items()}
            self.trades = d.get("trades", {})
            self.day = d.get("day", self.day)
            self.history = d.get("history", [])
        except Exception:
            log.exception("could not read %s, starting fresh", self.state_file)

    def save(self):
        keep = [s for s in self.setups.values() if s.state in ("WATCH", "OPEN")]
        old = sorted((s for s in self.setups.values() if s.state not in ("WATCH", "OPEN")),
                     key=lambda s: s.last_bar or s.created)[-100:]
        self.setups = {s.key: s for s in old + keep}
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"setups": {k: s.to_dict() for k, s in self.setups.items()}, "trades": self.trades,
                       "day": self.day, "history": self.history[-200:]}, f, indent=1, default=str)
        os.replace(tmp, self.state_file)

    # ------------------------------------------------------------------ messages
    def event(self, text: str, telegram: bool = True, photo: str | None = None):
        """Show in the app's log and (optionally) send to Telegram. Never raises."""
        self.events.append((datetime.now().strftime("%d %b %H:%M:%S"), text.replace("<b>", "").replace("</b>", "")))
        log.info(text.replace("\n", " | "))
        if not telegram or self.tg is None:
            return
        try:
            if photo:
                self.tg.photo(photo, text)
            else:
                self.tg.text(text)
        except Exception:
            log.exception("Telegram send failed")

    def digits(self, symbol: str) -> int:
        if symbol not in self._digits:
            try:
                self._digits[symbol] = self.broker.spec(symbol)["digits"]
            except Exception:
                return 5
        return self._digits[symbol]

    def _f(self, symbol: str, v) -> str:
        return "-" if v is None else f"{v:.{self.digits(symbol)}f}"

    def _money(self, v: float) -> str:
        cur = (self.snapshot.get("account") or {}).get("currency", "")
        return f"{v:+,.2f} {cur}".strip()

    # ------------------------------------------------------------------ main loop step
    def tick(self):
        while not self.commands.empty():
            cmd = self.commands.get()
            try:
                if isinstance(cmd, tuple) and cmd[0] == "close":
                    self.close_one(int(cmd[1]))
                else:
                    {"start": self.start, "stop": self.stop, "close_all": self.close_all}[cmd]()
            except Exception:
                log.exception("command %s failed", cmd)
                self.event(f"❗ '{cmd}' failed, see the log window", telegram=False)
        acc = self.broker.account()
        if acc is None:
            self.status = "MT5 is not connected or not logged in"
            self._snap(None, [])
            return
        self._daily(acc)
        self._manage(self.broker.positions())
        if self.running and time.time() - self.last_scan >= self.cfg.get("scan_seconds", 30):
            self.last_scan = time.time()
            self.block = self.broker.trading_check(bool(self.cfg.get("allow_live_trading")))
            self._scan()
            self.save()
        self._status()
        self._snap(self.broker.account() or acc, self.broker.positions())

    # ------------------------------------------------------------------ commands
    def start(self):
        if self.running:
            return
        why = self.broker.trading_check(bool(self.cfg.get("allow_live_trading")))
        if why:
            self.status = f"Not started: {why}"
            self.event(f"⚠️ Bot NOT started: {why}.")
            return
        missing = [s for s in self.symbols if self.broker.resolve(s) is None]
        if missing:
            self.event("⚠️ Skipping symbols your broker doesn't offer: " + ", ".join(missing), telegram=False)
            self.symbols = [s for s in self.symbols if s not in missing]
        self.running, self.last_scan = True, 0.0
        mm = self.mm
        self.event("▶️ <b>Butterfly bot v3 STARTED</b>\n"
                   f"Symbols: {', '.join(self.symbols)} · {', '.join(self.cfg['timeframes'])}\n"
                   f"Risk {mm['risk_percent']}% per trade · max {mm['max_open_positions']} open at once\n"
                   f"TP1 {self.bf['tp1_ad']} of A→D (close {mm['tp1_close_percent']}%, SL to entry) · "
                   f"TP2 {self.bf['tp2_ad']} of A→D\n"
                   f"Daily loss limit {mm['daily_loss_limit_percent']}% of balance")

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.event("⏸ <b>Bot STOPPED</b>: no new trades. Open trades keep their stop loss and TP2 in MT5, "
                   "and TP1 is still managed while the app is open.")

    def close_all(self, reason: str = "Close All button"):
        scope_all = self.mm.get("close_all_button", "bot") == "all"
        targets_ = [p for p in self.broker.positions() if p["bot"] or scope_all]
        if not targets_:
            self.event("Close All: no open positions to close.", telegram=False)
            return
        ok, failed = 0, []
        for p in targets_:
            tr = self.trades.get(str(p["ticket"]))
            if tr:
                tr["close_reason"] = reason
            err = self.broker.close(p["ticket"])
            if err:
                failed.append(f"{p['symbol']} #{p['ticket']}: {err}")
            else:
                ok += 1
        msg = f"🧹 <b>{reason}</b>: closed {ok} of {len(targets_)} positions."
        if failed:
            msg += "\n❗ Could not close:\n" + "\n".join(failed)
        self.event(msg)
        self._manage(self.broker.positions())
        self.save()

    def close_one(self, ticket: int):
        p = next((p for p in self.broker.positions() if p["ticket"] == ticket), None)
        if p is None:
            return
        tr = self.trades.get(str(ticket))
        if tr:
            tr["close_reason"] = "Close button in the app"
        err = self.broker.close(ticket)
        if err:
            self.event(f"❗ Could not close {p['symbol']} #{ticket}: {err}")
        elif not tr:
            self.event(f"✋ {p['symbol']} #{ticket} closed from the app (result {self._money(p['profit'])})")
        self._manage(self.broker.positions())
        self.save()

    # ------------------------------------------------------------------ daily loss limit
    def _daily(self, acc: dict):
        today = self.broker.today()
        if self.day.get("date") != today:
            if self.day.get("stopped"):
                self.event("🌅 New day: the daily loss limit is reset, the bot may open trades again.")
            self.day = {"date": today, "start_balance": acc["balance"], "stopped": ""}
            self.save()
        start = self.day["start_balance"] or acc["balance"]
        loss = start - acc["equity"]
        limit = start * self.mm["daily_loss_limit_percent"] / 100
        if not self.day["stopped"] and limit > 0 and loss >= limit:
            self.day["stopped"] = (f"daily loss limit reached: {loss:,.2f} {acc['currency']} "
                                   f"({loss / start * 100:.1f}% of {start:,.2f})")
            closing = self.mm.get("close_positions_at_daily_limit", False)
            self.event(f"🛑 <b>TRADING STOPPED FOR TODAY</b>\n{self.day['stopped']} ≥ "
                       f"{self.mm['daily_loss_limit_percent']}%.\nNo new trades until tomorrow. "
                       + ("All bot positions are being closed." if closing else
                          "Open trades keep their stop loss and take profit."))
            self.save()
            if closing:
                self.close_all("Daily loss limit")

    # ------------------------------------------------------------------ scanning
    def _scan(self):
        for sym in self.symbols:
            for tf in self.cfg["timeframes"]:
                try:
                    df = self.broker.candles(sym, tf, self.cfg.get("bars", 500))
                    k = f"{sym}|{tf}"
                    if len(df) < 60 or self.last_bar.get(k) == str(df.index[-1]):
                        continue
                    self.last_bar[k] = str(df.index[-1])
                    self._process(sym, tf, df)
                except Exception as e:
                    log.exception("scan failed for %s %s", sym, tf)
                    self.event(f"❗ Scan failed for {sym} {tf}: {e}", telegram=False)

    def _process(self, sym, tf, df):
        for s in [s for s in self.setups.values() if s.symbol == sym and s.timeframe == tf and s.state == "WATCH"]:
            new = df[df.index > pd.Timestamp(s.last_bar)] if s.last_bar else df.iloc[-1:]
            for t, bar in new.iterrows():
                for ev in step(s, bar, t, self.bf):
                    if ev == "touch":
                        self.event(f"📍 {sym} {tf} {s.label}: price is in the PRZ "
                                   f"({self._f(sym, s.prz_inner)} - {self._f(sym, s.prz_outer)}). "
                                   "Waiting for a reversal candle.", telegram=self.bf.get("send_prz_alerts", True))
                    elif ev == "signal":
                        if t == df.index[-1]:
                            self._open(s, df)
                        else:                    # signal on an old bar (bot was off): too late to enter
                            s.state, s.note = "SKIPPED", "entry signal happened while the bot was off"
                    elif ev in ("invalid", "expired"):
                        self.event(f"🚫 {sym} {tf} {s.label} cancelled: {s.note}.",
                                   telegram=self.bf.get("send_forming_alerts", True))
                if s.state != "WATCH":
                    break
        for s in find_setups(df, sym, tf, self.bf):
            if s.key in self.setups or self._overlaps(s):
                continue
            s.last_bar = str(df.index[-1])
            self.setups[s.key] = s
            text = (f"⏳ <b>{sym} {tf} {s.label} forming</b>\n"
                    f"PRZ (D zone 1.27-1.414 XA): {self._f(sym, s.prz_inner)} - {self._f(sym, s.prz_outer)}\n"
                    f"Stop (beyond 1.618 XA): {self._f(sym, s.stop)}\n"
                    f"Ratios: XB {s.ratios['XB']} · AC {s.ratios['AC']}\n"
                    "The bot will enter on a reversal candle in the PRZ.")
            s.chart = self._chart(df, s) or ""
            self.event(text, telegram=self.bf.get("send_forming_alerts", True), photo=s.chart or None)

    def _overlaps(self, new: Setup) -> bool:
        lo, hi = sorted((new.prz_inner, new.prz_outer))
        for s in self.setups.values():
            if (s.symbol, s.timeframe, s.side) == (new.symbol, new.timeframe, new.side) and s.state == "WATCH":
                a, b = sorted((s.prz_inner, s.prz_outer))
                if a <= hi and lo <= b:
                    return True
        return False

    def _chart(self, df, s: Setup) -> str | None:
        fn = os.path.join(self.chart_dir, f"{s.symbol}_{s.timeframe}_{s.state}_{df.index[-1]:%Y%m%d%H%M}.png")
        try:
            return chart.render(df, [s], fn, digits=self.broker.spec(s.symbol)["digits"])
        except Exception:
            log.exception("chart failed")
            return None

    # ------------------------------------------------------------------ opening a trade
    def _why_not(self, s: Setup) -> str | None:
        if self.block:
            return self.block
        if self.day.get("stopped"):
            return self.day["stopped"]
        bot_pos = [p for p in self.broker.positions() if p["bot"]]
        if len(bot_pos) >= int(self.mm["max_open_positions"]):
            return f"already {len(bot_pos)} positions open (maximum {self.mm['max_open_positions']} at the same time)"
        name = self.broker.resolve(s.symbol)
        if self.mm.get("one_position_per_symbol", True) and any(p["symbol"] == name for p in bot_pos):
            return f"the bot already has a position on {s.symbol}"
        return None

    def _lot(self, balance: float, dist: float, spec: dict) -> tuple[float | None, str]:
        risk_money = balance * self.mm["risk_percent"] / 100
        per_lot = dist / spec["tick_size"] * spec["tick_value"]
        if per_lot <= 0:
            return None, "could not work out the lot size for this symbol"
        step_ = spec["vol_step"] or 0.01
        lot = math.floor(risk_money / per_lot / step_ + 1e-9) * step_
        lot = min(lot, spec["vol_max"], float(self.mm.get("max_lot", 1.0)))
        if lot < spec["vol_min"]:
            min_risk = spec["vol_min"] * per_lot
            if min_risk <= risk_money * 1.5:
                lot = spec["vol_min"]
            else:
                return None, (f"the smallest lot ({spec['vol_min']}) would risk {min_risk:,.2f}, more than "
                              f"{self.mm['risk_percent']}% of the balance ({risk_money:,.2f})")
        decimals = max(0, -int(math.floor(math.log10(step_)))) if step_ < 1 else 0
        return round(lot, decimals), f"{lot * per_lot:,.2f}"

    def _open(self, s: Setup, df):
        sym, buy = s.symbol, s.side == "buy"
        why = self._why_not(s)
        if why:
            s.state, s.note = "SKIPPED", why
            self.event(f"⚠️ {sym} {s.timeframe} {s.label}: entry signal but NOT trading, {why}.")
            return
        q = self.broker.quote(sym)
        if not q:
            s.state, s.note = "SKIPPED", "no price"
            return
        price = q[1] if buy else q[0]
        spec = self.broker.spec(sym)
        tp1, tp2 = targets(s.side, s.points["A"][1], s.d_price, self.bf)
        if not ((s.stop < price < tp1) if buy else (tp1 < price < s.stop)):
            s.state, s.note = "SKIPPED", "price already beyond TP1 or the stop"
            self.event(f"⚠️ {sym} {s.timeframe} {s.label}: skipped, {s.note}.", telegram=False)
            return
        rr = abs(tp1 - price) / abs(price - s.stop)
        if rr < self.bf.get("min_rr_tp1", 0.5):
            s.state, s.note = "SKIPPED", f"reward to risk to TP1 only {rr:.2f}"
            self.event(f"⚠️ {sym} {s.timeframe} {s.label}: skipped, {s.note}.", telegram=False)
            return
        acc = self.broker.account()
        lot, risk_txt = self._lot(acc["balance"], abs(price - s.stop), spec)
        if lot is None:
            s.state, s.note = "SKIPPED", risk_txt
            self.event(f"⚠️ {sym} {s.timeframe} {s.label}: not trading, {risk_txt}.")
            return
        r = lambda v: round(v, spec["digits"])
        sl, tp1, tp2 = r(s.stop), r(tp1), r(tp2)
        ticket, fill, err = self.broker.open(sym, s.side, lot, sl, tp2, f"v3 Butterfly {s.timeframe}")
        if err:
            s.state, s.note = "SKIPPED", err
            self.event(f"❗ <b>Order FAILED</b> {s.side.upper()} {lot} {sym}: {err}")
            return
        s.state, s.ticket, s.entry, s.entry_time, s.sl = "OPEN", ticket, fill, str(df.index[-1]), sl
        s.tp1, s.tp2, s.mid = tp1, tp2, (tp1 + tp2) / 2
        self.trades[str(ticket)] = dict(ticket=ticket, symbol=sym, timeframe=s.timeframe, side=s.side, entry=fill,
                                        sl=sl, tp1=tp1, tp2=tp2, volume=lot, risk=risk_txt, tp1_done=False,
                                        opened=datetime.now().isoformat(timespec="seconds"), setup=s.key,
                                        close_reason=None, missing=0)
        f = lambda v: self._f(sym, v)
        text = (f"{'🟢 BUY' if buy else '🔴 SELL'} <b>{sym} {s.timeframe} {s.label}</b> opened\n"
                f"Entry {f(fill)} · {lot} lots (risk ≈ {risk_txt} = {self.mm['risk_percent']}%)\n"
                f"Stop loss {f(sl)}\nTP1 {f(tp1)} (close {self.mm['tp1_close_percent']}%, SL to entry)\n"
                f"TP2 {f(tp2)}\nRR to TP1 {rr:.2f} · ticket {ticket}")
        s.chart = self._chart(df, s) or ""
        self.trades[str(ticket)]["chart"] = s.chart
        self.event(text, photo=s.chart or None)

    # ------------------------------------------------------------------ managing open trades
    def _manage(self, positions: list[dict]):
        by_ticket = {p["ticket"]: p for p in positions}
        for key, tr in list(self.trades.items()):
            p = by_ticket.get(tr["ticket"])
            if p is None:
                info = self.broker.closed_info(tr["ticket"])
                if info is None and tr["missing"] < 5:   # history can take a moment to appear
                    tr["missing"] += 1
                    continue
                self._closed(tr, info)
                del self.trades[key]
                self.save()
                continue
            tr["now_volume"] = p["volume"]
            if not tr["tp1_done"] and ((p["price"] >= tr["tp1"]) if tr["side"] == "buy" else (p["price"] <= tr["tp1"])):
                self._tp1(tr, p)

    def _tp1(self, tr: dict, p: dict):
        sym, spec = tr["symbol"], self.broker.spec(tr["symbol"])
        step_ = spec["vol_step"] or 0.01
        part = math.floor(p["volume"] * self.mm["tp1_close_percent"] / 100 / step_ + 1e-9) * step_
        part = round(part, 8)
        msg = []
        if part >= spec["vol_min"] and p["volume"] - part >= spec["vol_min"] - 1e-9:
            err = self.broker.close(p["ticket"], part)
            msg.append(f"Closed {part:g} of {p['volume']:g} lots (≈ {self._money(p['profit'] * part / p['volume'])})"
                       if not err else f"❗ partial close failed: {err}")
        else:
            msg.append(f"Position is too small to split ({p['volume']:g} lots), keeping it all")
        if self.mm.get("move_sl_to_entry_at_tp1", True):
            err = self.broker.modify(p["ticket"], tr["entry"], tr["tp2"])
            if err:
                msg.append(f"❗ moving SL failed: {err}")
            else:
                tr["sl"] = tr["entry"]
                msg.append(f"Stop loss moved to entry {self._f(sym, tr['entry'])} (no loss possible now)")
        tr["tp1_done"] = True
        self.save()
        self.event(f"✅ <b>TP1 HIT {sym} {tr['timeframe']}</b> {tr['side'].upper()} at {self._f(sym, tr['tp1'])}\n"
                   + "\n".join(msg) + f"\nRest targets TP2 {self._f(sym, tr['tp2'])}")

    def _closed(self, tr: dict, info: dict | None):
        sym = tr["symbol"]
        s = self.setups.get(tr["setup"])
        if s:
            s.state = "DONE"
        if info is None:
            self.event(f"ℹ️ {sym} #{tr['ticket']} is closed (MT5 history has no details).")
            return
        pl = self._money(info["profit"])
        price = self._f(sym, info["price"])
        reason = (tr.get("close_reason") or {"tp": "TP2", "sl": "SL at entry" if tr["tp1_done"] else "Stop loss",
                                              "stopout": "Stop out"}.get(info["reason"], "Manual"))
        self.history.append(dict(time=datetime.now().strftime("%Y.%m.%d %H:%M"), ticket=tr["ticket"], symbol=sym,
                                 side=tr["side"], volume=tr["volume"], entry=tr["entry"], close=info["price"],
                                 profit=info["profit"], reason=reason, tp1=tr["tp1_done"],
                                 digits=self.digits(sym)))
        if tr.get("close_reason"):
            head = f"🧹 <b>{sym} closed by {tr['close_reason']}</b> at {price}"
        elif info["reason"] == "tp":
            head = f"🏁 <b>TP2 HIT {sym} {tr['timeframe']}</b> at {price}"
        elif info["reason"] == "sl" and tr["tp1_done"]:
            head = f"🔒 <b>{sym} {tr['timeframe']} closed at entry</b> after TP1 (TP2 not reached) at {price}"
        elif info["reason"] == "sl":
            head = f"❌ <b>STOP LOSS HIT {sym} {tr['timeframe']}</b> at {price}"
        elif info["reason"] == "stopout":
            head = f"⚠️ <b>{sym} closed by the broker (stop out)</b> at {price}"
        else:
            head = f"✋ <b>{sym} closed manually</b> at {price}"
        self.event(f"{head}\nResult of this trade: {pl}")

    # ------------------------------------------------------------------ for the app
    def _status(self):
        if self.day.get("stopped"):
            self.status = "Stopped for today: " + self.day["stopped"]
        elif not self.running:
            self.status = "Stopped (press Start)"
        elif self.block:
            self.status = "Running but cannot trade: " + self.block
        else:
            n = sum(1 for s in self.setups.values() if s.state == "WATCH")
            self.status = f"Running · scanning {len(self.symbols)} symbols · {n} Butterflies forming"

    def _snap(self, acc, positions):
        rows = []
        for p in positions:
            tr = self.trades.get(str(p["ticket"]))
            stage = ("manual" if not p["bot"] else "bot" if not tr else
                     "TP1 done, SL at entry" if tr["tp1_done"] else f"waiting TP1 {self._f(tr['symbol'], tr['tp1'])}")
            rows.append({**p, "stage": stage, "timeframe": tr["timeframe"] if tr else "",
                         "digits": self.digits(p["symbol"]), "chart": (tr or {}).get("chart", "")})
        start = self.day.get("start_balance")
        self.snapshot = dict(
            running=self.running, status=self.status, account=acc, positions=rows,
            day_pl=(acc["equity"] - start) if acc and start else 0.0,
            day_limit=(start or 0) * self.mm["daily_loss_limit_percent"] / 100,
            day_stopped=self.day.get("stopped", ""),
            watching=[dict(symbol=s.symbol, timeframe=s.timeframe, label=s.label, inner=s.prz_inner, outer=s.prz_outer,
                           stop=s.stop, touched=s.touched, digits=self.digits(s.symbol), chart=s.chart)
                      for s in self.setups.values() if s.state == "WATCH"],
            events=list(self.events), history=list(self.history[-100:]), quotes=self._quotes())

    def _quotes(self) -> list[dict]:
        out = []
        for sym in self.symbols:
            try:
                q = self.broker.quote(sym)
            except Exception:
                q = None
            if q:
                out.append(dict(symbol=sym, bid=q[0], ask=q[1], digits=self.digits(sym)))
        return out
