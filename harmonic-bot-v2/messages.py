"""Telegram message text (HTML) for each event."""
from __future__ import annotations

import html

from harmonics import Setup, rr


def _f(v, d):
    return f"{v:.{d}f}"


def _pts(a, b, point):
    return abs(a - b) / point if point else 0


def _ratios(s: Setup) -> str:
    return " | ".join(f"{k} {v}" for k, v in s.ratios.items())


def _levels(s: Setup, d: int, point: float, ref: float) -> str:
    r1, r2 = rr(s, ref)
    return (f"🛑 <b>Stop loss:</b> {_f(s.sl if s.sl is not None else s.stop, d)}  ({_pts(ref, s.stop, point):.0f} pts)\n"
            f"🎯 <b>TP1:</b> {_f(s.tp1, d)}  ({_pts(s.tp1, ref, point):.0f} pts, RR 1:{r1:.1f})\n"
            f"🎯 <b>TP2:</b> {_f(s.tp2, d)}  ({_pts(s.tp2, ref, point):.0f} pts, RR 1:{r2:.1f})\n"
            f"〰️ Mid line: {_f(s.mid, d)}")


def build(event: str, s: Setup, d: int, point: float, cfg: dict, others: list[Setup] = ()) -> str:
    act = "BUY" if s.side == "buy" else "SELL"
    dot = "🟢" if s.side == "buy" else "🔴"
    head = f"<b>{s.symbol} · {s.timeframe}</b> — {s.label}"
    lo, hi = sorted((s.prz_inner, s.prz_outer))
    partial = int(cfg["tp1_close_percent"])
    rules = (f"\n\n<b>Plan</b>\n"
             f"• At TP1: close {partial}% and move SL to entry (TS/SL+).\n"
             f"• At TP2: close the rest.\n"
             f"• If price comes back to the TS/SL+ level after TP1, TP2 is invalid: close the position.")

    if event == "watch":
        alt = ""
        for o in others:
            if o is not s and o.pattern != "AB=CD":
                alo, ahi = sorted((o.prz_inner, o.prz_outer))
                alt += f"\n• PRZ ({o.rank}) {o.pattern}: {_f(alo, d)} – {_f(ahi, d)}, stop {_f(o.stop, d)}"
        how = ("a reversal candle closes inside the PRZ" if cfg["entry_mode"] == "confirm"
               else f"price touches {_f(s.prz_inner, d)}")
        found = ""
        if s.source == "claude":
            found = (f"\n\n🧠 <b>Found by Claude</b> (confidence {s.confidence}%): {html.escape(s.note)}"
                     if s.note else "\n\n🧠 <b>Found by Claude</b>")
        return (f"⏳ <b>SETUP FORMING</b> — {head}\n\n"
                f"<b>Action:</b> wait. {act} when {how}.\n"
                f"📦 <b>PRZ ({s.rank}) {act}:</b> {_f(lo, d)} – {_f(hi, d)}\n"
                f"{_levels(s, d, point, s.prz_inner)}\n"
                f"📐 {_ratios(s)}"
                + (f"\n\n<b>Alternate zones</b>{alt}" if alt else "") + found + rules)

    if event == "prz_touch":
        return (f"📍 <b>PRICE IN PRZ</b> — {head}\n"
                f"Price reached the PRZ {_f(lo, d)} – {_f(hi, d)}. "
                + ("Waiting for a reversal candle to enter." if cfg["entry_mode"] == "confirm" else ""))

    if event == "entry":
        return (f"{dot} <b>{act} NOW</b> — {head}\n\n"
                f"💵 <b>Entry:</b> {_f(s.entry, d)}  ({s.entry_time})\n"
                f"{_levels(s, d, point, s.entry)}\n"
                f"📦 PRZ ({s.rank}): {_f(lo, d)} – {_f(hi, d)}\n"
                f"📐 {_ratios(s)}" + rules)

    if event == "tp1":
        return (f"✅ <b>TP1 HIT</b> — {head}\n"
                f"TP1 {_f(s.tp1, d)} reached.\n"
                f"<b>Action:</b> close {partial}% now and move SL to {_f(s.sl, d)} (TS/SL+). "
                f"Hold the rest for TP2 {_f(s.tp2, d)}.")
    if event == "tp2":
        return f"🏁 <b>TP2 HIT</b> — {head}\nTP2 {_f(s.tp2, d)} reached. <b>Action:</b> close the rest. Trade done."
    if event == "tp2_invalid":
        return (f"⚠️ <b>TP2 INVALID — CLOSE POSITION</b> — {head}\n"
                f"Price came back to the TS/SL+ level {_f(s.sl, d)} after TP1. "
                f"<b>Action:</b> close the remaining position now. TP1 profit is kept.")
    if event == "tp2_timeout":
        return (f"⚠️ <b>TP2 INVALID — CLOSE POSITION</b> — {head}\n"
                f"TP2 not reached after {cfg['tp2_max_bars']} bars. <b>Action:</b> close the remaining position now.")
    if event == "stopped":
        return f"❌ <b>STOP LOSS HIT</b> — {head}\nStop {_f(s.stop, d)} hit before TP1. Trade closed."
    if event == "invalid":
        return f"🚫 <b>SETUP CANCELLED</b> — {head}\nPrice broke the pattern before entry. No trade."
    if event == "expired":
        return f"⌛ <b>SETUP EXPIRED</b> — {head}\nPrice never reached the PRZ. No trade."
    return f"{event}: {head}"


CHART_EVENTS = {"watch", "entry", "tp1", "tp2", "tp2_invalid", "stopped"}
