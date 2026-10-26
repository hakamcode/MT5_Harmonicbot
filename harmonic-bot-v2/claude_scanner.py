"""Claude finds the harmonic setups: chart image + candles in, setups as JSON out.

Python then checks every number against the real candles (scan_checks below) before a setup is
tracked, alerted or traded. Anything that fails a check is dropped and logged.
Switched on with "detector": "claude" (or "both") and an anthropic_api_key in config.json.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from harmonics import PATTERNS, Setup, _in, rr

log = logging.getLogger("claude_scan")

PATTERN_NAMES = ["Gartley", "Bat", "Alt Bat", "Butterfly", "Crab", "Deep Crab", "AB=CD"]

POINT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "enum": ["X", "A", "B", "C"]},
        "bar": {"type": "integer", "description": "bar number from the candle table"},
        "price": {"type": "number", "description": "that bar's high (for a swing high) or low (for a swing low)"},
    },
    "required": ["name", "bar", "price"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "setups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "enum": PATTERN_NAMES},
                    "side": {"type": "string", "enum": ["buy", "sell"], "description": "trade direction at D"},
                    "points": {"type": "array", "items": POINT, "description": "X,A,B,C in time order (A,B,C for AB=CD)"},
                    "prz_inner": {"type": "number", "description": "PRZ edge price reaches first"},
                    "prz_outer": {"type": "number", "description": "far PRZ edge"},
                    "stop": {"type": "number"},
                    "tp1": {"type": "number"},
                    "tp2": {"type": "number"},
                    "confidence": {"type": "integer", "description": "0-100"},
                    "reasoning": {"type": "string", "description": "1-3 short plain sentences"},
                },
                "required": ["pattern", "side", "points", "prz_inner", "prz_outer", "stop", "tp1", "tp2",
                             "confidence", "reasoning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["setups"],
    "additionalProperties": False,
}

SYSTEM = """You are an expert harmonic-pattern trader scanning MetaTrader 5 charts for a small trading team. \
You get one chart image and the matching candle table. Find harmonic setups that are worth trading with the \
team's method below: the X, A, B, C swings are already complete and price is now travelling the C to D leg \
toward the PRZ (or has just reached it). Do not report patterns whose D completed long ago, or whose PRZ \
price has already broken through.

Rules for your answer:
- Points are swing highs and lows. Give each point's bar number from the candle table and that bar's exact \
high (swing high) or low (swing low). Points alternate high/low and are in time order.
- side "buy" means D is a low (C is a swing high); "sell" means D is a high (C is a swing low).
- PRZ: prz_inner is the edge price reaches first, prz_outer the far edge. Stop goes beyond prz_outer.
- Targets follow the team ladder: TP1 is the nearer target; TP2 distance from prz_outer = 1.618 x the TP1 distance.
- Only report setups you would really show the team (confidence 60 or more). Most scans should return an \
empty list; that is normal. Return at most 2 setups.
- Use simple English in reasoning.

<team_method>
{method}
</team_method>"""


def plain_chart(d: pd.DataFrame, digits: int, title: str) -> bytes:
    """Candles with bar numbers on the x axis, so the image matches the table."""
    fig, ax = plt.subplots(figsize=(12, 6.4), dpi=110)
    for i, (_, r) in enumerate(d.iterrows()):
        up = r.close >= r.open
        col = "#26a69a" if up else "#ef5350"
        ax.vlines(i, r.low, r.high, color=col, linewidth=0.8)
        ax.add_patch(plt.Rectangle((i - 0.35, min(r.open, r.close)), 0.7, abs(r.close - r.open) or 1e-12,
                                   color=col))
    ax.set_xlim(-2, len(d) + 2)
    ax.set_xticks(range(0, len(d), 10))
    ax.grid(alpha=0.25)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter(f"%.{digits}f"))
    ax.set_title(title + "   (x axis = bar number)")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


class Scanner:
    def __init__(self, cfg: dict, method_file: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "method.md")):
        import anthropic                     # only needed when the Claude scanner is switched on
        self.anthropic = anthropic
        self.model = cfg.get("model") or "claude-opus-5-5"
        self.effort = cfg.get("scan_effort") or cfg.get("effort") or "medium"
        self.timeframes = set(cfg.get("scan_timeframes") or ["H4", "D1"])
        self.bars = int(cfg.get("scan_bars", 200))
        self.max_per_day = int(cfg.get("max_scans_per_day", 60))
        self.min_conf = int(cfg.get("min_confidence", 60))
        self.every = 1                       # scan every Nth new bar (demo uses more)
        self.client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"].strip(),
                                          timeout=cfg.get("timeout_seconds", 300), max_retries=2)
        method = ""
        if os.path.exists(method_file):
            with open(method_file, encoding="utf-8") as f:
                method = f.read()
        self.system = SYSTEM.replace("{method}", method or "(not provided)")
        self.today, self.count = None, 0
        self.seen: dict[str, int] = {}
        self.last_scan: dict[str, pd.Timestamp] = {}     # chart -> bar time of its last Claude scan
        self.last_ask: dict[str, pd.Timestamp] = {}      # chart -> bar time it last had a new candle
        self.spread = True                               # share the daily scans fairly over the day and charts

    def wants(self, tf: str) -> bool:
        return tf in self.timeframes

    def _budget_ok(self, day) -> bool:
        if day != self.today:
            self.today, self.count, self.told = day, 0, False
        if self.count >= self.max_per_day:
            if not self.told:
                log.info("Claude scan: daily limit of %s scans used (max_scans_per_day), next scans tomorrow",
                         self.max_per_day)
                self.told = True
            return False
        return True

    def _my_turn(self, k: str, now: pd.Timestamp) -> bool:
        """With few scans a day, space them out (24h / max_scans_per_day apart) and give the turn
        to the chart that has waited longest, so every symbol gets scanned in rotation."""
        self.last_ask[k] = now
        if not self.spread:
            return True
        gap = pd.Timedelta(hours=0.8 * 24 / max(1, self.max_per_day))   # 0.8: candles close on fixed hours
        latest = max(self.last_scan.values(), default=None)
        if latest is not None and now - latest < gap:
            return False
        never = pd.Timestamp.min
        mine = self.last_scan.get(k, never)
        for j, asked in self.last_ask.items():           # charts with fresh candles only (skips closed markets)
            if j != k and now - asked <= pd.Timedelta(hours=12) and self.last_scan.get(j, never) < mine:
                return False
        return True

    def scan(self, df: pd.DataFrame, symbol: str, tf: str, digits: int, scfg: dict,
             tracking: list[Setup] = ()) -> list[Setup]:
        k = f"{symbol}|{tf}"
        now = pd.Timestamp(df.index[-1])
        self.seen[k] = self.seen.get(k, -1) + 1
        if self.seen[k] % self.every or not self._budget_ok(now.date()) or not self._my_turn(k, now):
            return []
        self.count += 1
        self.last_scan[k] = now
        d = df.tail(self.bars)
        rows = "\n".join(f"{i},{t:%Y-%m-%d %H:%M},{r.open:.{digits}f},{r.high:.{digits}f},{r.low:.{digits}f},{r.close:.{digits}f}"
                         for i, (t, r) in enumerate(d.iterrows()))
        img = base64.standard_b64encode(plain_chart(d, digits, f"{symbol} {tf}")).decode("utf-8")
        already = "".join(f"\n- {s.label}, PRZ {s.prz_inner:.{digits}f}-{s.prz_outer:.{digits}f} ({s.state})"
                          for s in tracking)
        text = (f"Symbol {symbol}, timeframe {tf}. Last closed bar is bar {len(d) - 1}, "
                f"close {d['close'].iloc[-1]:.{digits}f}.\n"
                + (f"Setups the bot is already tracking (do not report these again):{already}\n" if already else "")
                + f"\nCandles (bar,time,open,high,low,close):\n{rows}\n\nFind the setups.")
        req = dict(model=self.model, max_tokens=32000,
                   system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                   messages=[{"role": "user", "content": [
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                       {"type": "text", "text": text}]}],
                   output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}})
        try:
            try:
                with self.client.beta.messages.stream(**req, betas=["server-side-fallback-2026-07-01"],
                                                      fallbacks="default") as st:
                    resp = st.get_final_message()
            except self.anthropic.BadRequestError as e:
                if "fallback" not in str(e.message).lower():
                    raise
                with self.client.messages.stream(**req) as st:
                    resp = st.get_final_message()
        except self.anthropic.AuthenticationError:
            log.error("Claude scan: the anthropic_api_key in config.json was rejected")
            return []
        except self.anthropic.BadRequestError as e:
            log.error("Claude scan refused (%s). If it mentions credit, add credit on console.anthropic.com.", e.message)
            return []
        except (self.anthropic.APIStatusError, self.anthropic.APIConnectionError) as e:
            log.warning("Claude scan %s %s failed: %s", symbol, tf, getattr(e, "message", e))
            return []
        if resp.stop_reason == "refusal":
            log.warning("Claude scan %s %s: Claude declined", symbol, tf)
            return []
        raw = "".join(b.text for b in resp.content if b.type == "text")
        try:
            items = json.loads(raw)["setups"]
        except (ValueError, KeyError, TypeError):
            log.warning("Claude scan %s %s: unexpected answer (stop=%s) %s", symbol, tf, resp.stop_reason, raw[:200])
            return []
        u = resp.usage
        log.info("Claude scan %s %s: %d setup(s), tokens in %s out %s (scan %d today)", symbol, tf, len(items),
                 u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0),
                 u.output_tokens, self.count)
        out = []
        for it in items:
            s, why = check(it, d, symbol, tf, scfg, self.min_conf)
            if s:
                out.append(s)
            else:
                log.info("Claude scan %s %s: dropped %s %s (%s)", symbol, tf, it.get("side"), it.get("pattern"), why)
        return out


# --------------------------------------------------------------------------- python checks
def check(it: dict, d: pd.DataFrame, symbol: str, tf: str, cfg: dict, min_conf: int):
    """Turn one Claude setup into a Setup, or (None, reason) if any number doesn't hold up."""
    try:
        pattern, side = it["pattern"], it["side"]
        pts = sorted(it["points"], key=lambda p: "XABC".index(p["name"]))
        lv = {k: float(it[k]) for k in ("prz_inner", "prz_outer", "stop", "tp1", "tp2")}
        conf = int(it["confidence"])
    except (KeyError, TypeError, ValueError) as e:
        return None, f"missing field {e}"
    if pattern not in PATTERN_NAMES or side not in ("buy", "sell"):
        return None, "unknown pattern/side"
    if conf < min_conf:
        return None, f"confidence {conf} < {min_conf}"
    need = "ABC" if pattern == "AB=CD" else "XABC"
    if "".join(p["name"] for p in pts) != need:
        return None, f"points should be {need}"
    bars = [int(p["bar"]) for p in pts]
    if bars != sorted(bars) or len(set(bars)) != len(bars) or bars[0] < 0 or bars[-1] >= len(d) - 1:
        return None, "point bars out of order or out of range"
    # each point must be a real high/low on its bar, alternating; snap to the exact candle value
    # C is the last point: high for buy, low for sell; earlier points alternate backwards
    kinds = []
    k = "H" if side == "buy" else "L"
    for _ in pts:
        kinds.insert(0, k)
        k = "L" if k == "H" else "H"
    tol = max(float(d["high"].max() - d["low"].min()) * 0.02, float((d["high"] - d["low"]).mean()))
    prices = []
    for j, (p, b, kind) in enumerate(zip(pts, bars, kinds)):
        # snap to the real swing: the highest high / lowest low within 3 bars of Claude's bar
        win = d.iloc[max(0, b - 3): b + 4]
        col = win["high"] if kind == "H" else win["low"]
        sb = d.index.get_loc(col.idxmax() if kind == "H" else col.idxmin())
        real = float(d["high"].iloc[sb] if kind == "H" else d["low"].iloc[sb])
        if abs(real - float(p["price"])) > tol:
            return None, (f"{p['name']} price {p['price']} is not near the swing "
                          f"{'high' if kind == 'H' else 'low'} {real} around bar {b}")
        bars[j] = sb
        prices.append(real)
    if bars != sorted(bars) or len(set(bars)) != len(bars) or bars[-1] >= len(d) - 1:
        return None, "points overlap after snapping to the candles"
    sgn = 1 if side == "buy" else -1                 # direction of profit
    inner, outer, stop, tp1, tp2 = lv["prz_inner"], lv["prz_outer"], lv["stop"], lv["tp1"], lv["tp2"]
    if not (sgn * stop < sgn * outer <= sgn * inner < sgn * tp1 < sgn * tp2):
        return None, "levels in the wrong order (stop / PRZ / TP1 / TP2)"
    C = prices[-1]
    if sgn * inner >= sgn * C:
        return None, "PRZ is not beyond C"
    price = float(d["close"].iloc[-1])
    after_c = d.iloc[bars[-1] + 1:]
    if sgn * price <= sgn * stop or (len(after_c) and (sgn * (after_c["low"] if side == "buy" else after_c["high"]) <= sgn * stop).any()):
        return None, "price already went past the stop"
    if (sgn * (after_c["high"] if side == "buy" else after_c["low"]) > sgn * C).any():
        return None, "price went past C after C (structure broken)"
    a_to_c = abs(prices[1 if pattern != "AB=CD" else 0] - C) or 1e-12
    if abs(inner - outer) > a_to_c * 1.0 or abs(outer - stop) > a_to_c * 1.0:
        return None, "PRZ or stop far too wide"
    times = {n: (str(d.index[b]), pr) for n, b, pr in zip(need, bars, prices)}
    ratios = {}
    if pattern == "AB=CD":
        A, B, Cp = prices
        ratios["AC"] = round(abs(Cp - B) / (abs(B - A) or 1e-12), 3)
    else:
        X, A, B, Cp = prices
        xa, ab, bc = abs(A - X), abs(B - A), abs(Cp - B)
        if not (xa and ab and bc):
            return None, "flat swings"
        ratios = {"XB": round(ab / xa, 3), "AC": round(bc / ab, 3),
                  "BD": round(abs(Cp - (inner + outer) / 2) / bc, 3),
                  "XD": round(abs(A - (inner + outer) / 2) / xa, 3)}
        p = PATTERNS[pattern]
        if not (_in(ratios["XB"], p["b"], 0.12) and _in(ratios["AC"], p["c"], 0.12)):
            return None, f"ratios XB {ratios['XB']} / AC {ratios['AC']} don't fit a {pattern}"
    group = f"ai-{d.index[bars[0]]:%Y%m%d%H%M}-{d.index[bars[-1]]:%Y%m%d%H%M}"
    s = Setup(symbol, tf, pattern, side, times, ratios, inner, outer, stop, tp1, tp2, (tp1 + tp2) / 2,
              str(d.index[-1]), group=group, source="claude", note=str(it.get("reasoning", ""))[:400],
              confidence=conf)
    r1, _ = rr(s, inner)
    if r1 < float(cfg.get("min_rr_tp1", 0.3)):
        return None, f"reward to risk to TP1 only {r1:.2f}"
    return s, ""


def from_config(cfg: dict):
    """Build a Scanner if detector is claude/both and a key is set, else None. Never raises."""
    if cfg.get("detector", "python") not in ("claude", "both"):
        return None
    c = cfg.get("claude") or {}
    key = str(c.get("anthropic_api_key") or "").strip()
    if not key.startswith("sk-ant-"):
        log.error('detector is "%s" but there is no anthropic_api_key (sk-ant-...) in config.json: '
                  "using the Python detector", cfg.get("detector"))
        return None
    try:
        return Scanner(c)
    except ImportError:
        log.error("Claude scanner is OFF: run  pip install -r requirements.txt  (anthropic package missing)")
    except Exception:
        log.exception("Claude scanner is OFF: could not start it")
    return None
