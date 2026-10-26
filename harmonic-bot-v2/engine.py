"""Scan -> detect -> manage -> notify. Shared by the live bot and the offline demo."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import pandas as pd

import chart
import messages
from harmonics import Setup, find_setups, step

log = logging.getLogger("engine")
ACTIVE = {"WATCH", "OPEN", "TP1"}


class Engine:
    def __init__(self, cfg: dict, telegram, trader=None, state_file="state.json", chart_dir="charts", reviewer=None,
                 scanner=None, detector="python"):
        self.cfg, self.tg, self.trader, self.reviewer = cfg, telegram, trader, reviewer
        self.scanner, self.detector = scanner, detector if scanner else "python"
        self.state_file, self.chart_dir = state_file, chart_dir
        os.makedirs(chart_dir, exist_ok=True)
        self.setups: dict[str, Setup] = {}
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                self.setups = {k: Setup.from_dict(v) for k, v in json.load(f).items()}

    def save(self):
        cutoff = datetime.utcnow() - timedelta(days=self.cfg.get("keep_history_days", 30))
        self.setups = {k: s for k, s in self.setups.items()
                       if s.state in ACTIVE or pd.Timestamp(s.last_bar or s.created) > cutoff}
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: s.to_dict() for k, s in self.setups.items()}, f, indent=1, default=str)
        os.replace(tmp, self.state_file)

    # ------------------------------------------------------------------ main entry
    def process(self, symbol: str, tf: str, df: pd.DataFrame, digits: int, point: float):
        # 1) manage what we already track
        for s in [s for s in self.setups.values() if s.symbol == symbol and s.timeframe == tf and s.state in ACTIVE]:
            new = df[df.index > pd.Timestamp(s.last_bar)] if s.last_bar else df.iloc[-1:]
            for t, bar in new.iterrows():
                for ev in step(s, bar, t, self.cfg):
                    s.history.append([str(t), ev])
                    self._trade(ev, s)
                    self._notify(ev, s, df, digits, point)
                if s.state not in ACTIVE:
                    break
        # 2) look for new setups
        found = self._detect(df, symbol, tf, digits)
        groups: dict[str, list[Setup]] = {}
        for s in found:
            if s.key in self.setups or (s.source == "claude" and self._overlaps(s)):
                continue
            s.last_bar = str(df.index[-1])
            self.setups[s.key] = s
            groups.setdefault(s.group, []).append(s)
        for g in groups.values():
            g.sort(key=lambda s: s.rank)
            if self.cfg.get("send_watch_alerts", True):
                self._notify("watch", g[0], df, digits, point, group=g)
        self.save()

    # ------------------------------------------------------------------ helpers
    def _detect(self, df, symbol, tf, digits) -> list[Setup]:
        """Python detector, Claude scanner, or both (Claude only on its scan_timeframes)."""
        found = []
        if self.detector in ("python", "both") or not self.scanner.wants(tf):
            found += find_setups(df, symbol, tf, self.cfg)
        if self.detector in ("claude", "both") and self.scanner.wants(tf):
            tracking = [s for s in self.setups.values()
                        if s.symbol == symbol and s.timeframe == tf and s.state in ACTIVE]
            try:
                found += self.scanner.scan(df, symbol, tf, digits, self.cfg, tracking)
            except Exception:  # never stop the bot because of the AI scanner
                log.exception("Claude scan failed for %s %s", symbol, tf)
        return found

    def _overlaps(self, new: Setup) -> bool:
        """Claude may describe the same setup slightly differently each scan: skip it if an active
        setup on the same chart and side already has an overlapping PRZ."""
        lo, hi = sorted((new.prz_inner, new.prz_outer))
        for s in self.setups.values():
            if (s.symbol, s.timeframe, s.side) == (new.symbol, new.timeframe, new.side) and s.state in ACTIVE:
                a, b = sorted((s.prz_inner, s.prz_outer))
                if a <= hi and lo <= b:
                    return True
        return False

    def _notify(self, ev, s, df, digits, point, group=None):
        if ev == "prz_touch" and not self.cfg.get("send_touch_alerts", True):
            return
        group = group or [s]
        text = messages.build(ev, s, digits, point, self.cfg, others=group)
        fn = None
        if ev in messages.CHART_EVENTS:
            fn = f"{self.chart_dir}/{s.symbol}_{s.timeframe}_{s.pattern.replace(' ', '')}_{ev}_{df.index[-1]:%Y%m%d%H%M}.png"
            try:
                chart.render(df, group, fn, digits=digits)
            except Exception:  # never lose the alert because of a drawing problem
                log.exception("chart failed")
                fn = None
        review = None
        if self.reviewer and self.reviewer.wants(ev):
            try:
                review = self.reviewer.review(ev, s, group, df, digits, fn)
            except Exception:  # never lose the alert because of the AI review
                log.exception("Claude review failed")
        if fn:
            try:
                self.tg.photo(fn, text)
            except Exception:
                log.exception("sending chart failed")
                self.tg.text(text)
        else:
            self.tg.text(text)
        if review:
            self.tg.text(review)

    def _trade(self, ev, s: Setup):
        if not self.trader or not self.cfg.get("auto_trade"):
            return
        try:
            if ev == "entry":
                open_now = sum(1 for o in self.setups.values() if o.ticket and o.state in ("OPEN", "TP1"))
                if open_now >= int(self.cfg.get("max_open_trades", 3)):
                    log.warning("not opening %s: already %d bot trades open (max_open_trades)", s.key, open_now)
                    self.tg.text(f"⚠️ Not opening {s.symbol} {s.timeframe} {s.label}: max_open_trades "
                                 f"({self.cfg.get('max_open_trades', 3)}) reached.")
                    return
                lot = min(float(self.cfg["lot_size"]), float(self.cfg.get("max_lot", 0.05)))
                s.ticket = self.trader.open_market(s.symbol, s.side, lot, s.stop, s.tp2, f"harm {s.pattern}")
                if s.ticket:
                    self.tg.text(f"🤖 Order placed: {s.side.upper()} {lot} {s.symbol}, SL {s.stop:g}, TP {s.tp2:g} "
                                 f"(ticket {s.ticket})")
                else:
                    self.tg.text(f"❗ Order FAILED for {s.symbol} {s.label}. Check MT5 (Algo Trading on?) and the log.")
            elif s.ticket and ev == "tp1":
                self.trader.close_part(s.ticket, self.cfg["tp1_close_percent"] / 100)
                self.trader.modify_sl(s.ticket, s.sl, s.tp2)
            elif s.ticket and ev in ("tp2_invalid", "tp2_timeout"):
                self.trader.close_part(s.ticket, 1.0)
        except Exception:
            log.exception("trade action %s failed for %s", ev, s.key)
