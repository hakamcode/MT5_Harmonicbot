"""Optional Claude AI review of an alert: chart + pattern data in, short verdict out.

Off unless config.json has "claude": {"anthropic_api_key": "sk-ant-..."}.
If the call fails for any reason the alert is still sent, just without the review.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os

from harmonics import Setup, rr

log = logging.getLogger("ai_review")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["TAKE", "CAUTION", "SKIP"]},
        "confidence": {"type": "integer", "description": "0-100, how sure you are of the verdict"},
        "reasoning": {"type": "string", "description": "2-4 short plain sentences"},
        "watch_out": {"type": "string", "description": "one short sentence: the main risk or what would cancel the idea"},
    },
    "required": ["verdict", "confidence", "reasoning", "watch_out"],
    "additionalProperties": False,
}

SYSTEM = """You are an experienced harmonic-pattern trader reviewing alerts from an automatic MT5 scanner \
for a small trading team before they act on them. Judge each alert against the team's own method below, \
the chart image and the numbers given. Be honest and practical: say SKIP when the pattern is weak or the \
trade is poor, CAUTION when it is tradable but has a clear problem, TAKE only when it fits the method well. \
The verdict is for the alert's stage: for a SETUP FORMING alert, judge whether it is worth waiting for; \
for a BUY/SELL NOW alert, judge whether to enter now. Use simple English (the readers are not native speakers). \
Do not repeat the price levels back; the alert already shows them. This is a second opinion, not financial advice.

<team_method>
{method}
</team_method>"""

ICON = {"TAKE": "✅", "CAUTION": "⚠️", "SKIP": "⛔"}


class Reviewer:
    def __init__(self, cfg: dict, method_file: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "method.md")):
        import anthropic                     # only needed when the review is switched on
        self.anthropic = anthropic
        self.model = cfg.get("model") or "claude-opus-5-5"
        self.effort = cfg.get("effort") or "medium"
        self.events = set(cfg.get("review_events") or ["watch", "entry"])
        self.client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"].strip(),
                                          timeout=cfg.get("timeout_seconds", 180), max_retries=2)
        method = ""
        if os.path.exists(method_file):
            with open(method_file, encoding="utf-8") as f:
                method = f.read()
        self.system = SYSTEM.replace("{method}", method or "(not provided)")

    def wants(self, event: str) -> bool:
        return event in self.events

    def _facts(self, event, s: Setup, group, df, digits) -> str:
        ref = s.entry if s.entry is not None else s.prz_inner
        r1, r2 = rr(s, ref)
        f = lambda v: None if v is None else round(float(v), digits)
        data = {
            "alert": {"watch": "SETUP FORMING (price still heading to the PRZ)",
                      "entry": "BUY/SELL NOW (entry triggered)"}.get(event, event),
            "symbol": s.symbol, "timeframe": s.timeframe, "pattern": s.label,
            "points": {k: {"time": t, "price": f(p)} for k, (t, p) in s.points.items()},
            "ratios": s.ratios,
            "prz": {"rank": s.rank, "inner_edge": f(s.prz_inner), "outer_edge": f(s.prz_outer)},
            "entry": f(s.entry), "entry_time": s.entry_time,
            "stop": f(s.stop), "tp1": f(s.tp1), "tp2": f(s.tp2), "mid_line": f(s.mid),
            "reward_risk": {"tp1": round(r1, 2), "tp2": round(r2, 2)},
            "alternate_zones": [{"pattern": o.label, "rank": o.rank, "prz": [f(o.prz_inner), f(o.prz_outer)],
                                 "stop": f(o.stop)} for o in (group or []) if o is not s],
        }
        bars = df.tail(40)
        rows = "\n".join(f"{t:%Y-%m-%d %H:%M},{r.open:.{digits}f},{r.high:.{digits}f},{r.low:.{digits}f},{r.close:.{digits}f}"
                         for t, r in bars.iterrows())
        return (f"Alert data:\n{json.dumps(data, indent=1, default=str)}\n\n"
                f"Last {len(bars)} closed candles (time,open,high,low,close):\n{rows}")

    def review(self, event, s: Setup, group, df, digits, chart_path: str | None) -> str | None:
        """Return a ready-to-send Telegram (HTML) text, or None if the review failed."""
        content = []
        if chart_path and os.path.exists(chart_path):
            with open(chart_path, "rb") as f:
                img = base64.standard_b64encode(f.read()).decode("utf-8")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}})
        content.append({"type": "text", "text": self._facts(event, s, group, df, digits)
                        + "\n\nReview this alert and give your verdict."})
        req = dict(model=self.model, max_tokens=16000,
                   system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                   messages=[{"role": "user", "content": content}],
                   output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}})
        try:
            try:
                # fallbacks: if Opus declines, Anthropic re-runs it on its recommended backup model
                resp = self.client.beta.messages.create(**req, betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks="default")
            except self.anthropic.BadRequestError as e:
                if "fallback" not in str(e.message).lower():
                    raise
                resp = self.client.messages.create(**req)   # model without fallback support
        except self.anthropic.AuthenticationError:
            log.error("Claude review: the anthropic_api_key in config.json was rejected. Check it on console.anthropic.com.")
            return None
        except self.anthropic.PermissionDeniedError as e:
            log.error("Claude review: key not allowed to use %s (%s)", self.model, e.message)
            return None
        except self.anthropic.RateLimitError:
            log.warning("Claude review: rate limited, alert sent without review")
            return None
        except self.anthropic.BadRequestError as e:
            # most common cause: no credit left on the Anthropic account
            log.error("Claude review: request refused (%s). If it mentions credit, add credit on console.anthropic.com.", e.message)
            return None
        except self.anthropic.APIStatusError as e:
            log.warning("Claude review: Anthropic API error %s, alert sent without review", e.status_code)
            return None
        except self.anthropic.APIConnectionError:
            log.warning("Claude review: no connection to Anthropic, alert sent without review")
            return None

        if resp.stop_reason == "refusal":
            log.warning("Claude review: Claude declined to review this alert")
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            v = json.loads(text)
        except ValueError:
            log.warning("Claude review: unexpected answer (stop=%s): %s", resp.stop_reason, text[:200])
            return None
        u = resp.usage
        log.info("Claude review %s %s %s: %s (%s%%), tokens in %s out %s", s.symbol, s.timeframe, s.pattern,
                 v["verdict"], v["confidence"], u.input_tokens + (u.cache_read_input_tokens or 0)
                 + (u.cache_creation_input_tokens or 0), u.output_tokens)
        e = html.escape
        verdict = str(v["verdict"]).upper()
        return (f"🧠 <b>Claude review — {s.symbol} · {s.timeframe} {e(s.label)}</b>\n"
                f"{ICON.get(verdict, '•')} <b>{e(verdict)}</b>  (confidence {int(v['confidence'])}%)\n\n"
                f"{e(v['reasoning'])}\n\n"
                f"👀 <b>Watch out:</b> {e(v['watch_out'])}\n"
                f"<i>AI second opinion, not financial advice.</i>")


def from_config(cfg: dict):
    """Build a Reviewer if a key is set, else None. Never raises."""
    c = cfg.get("claude") or {}
    key = str(c.get("anthropic_api_key") or "").strip()
    if not key or "PASTE" in key:
        return None
    if not key.startswith("sk-ant-"):
        log.error("Claude review is OFF: anthropic_api_key in config.json should start with sk-ant-")
        return None
    try:
        return Reviewer(c)
    except ImportError:
        log.error("Claude review is OFF: run  pip install -r requirements.txt  (the anthropic package is missing)")
    except Exception:
        log.exception("Claude review is OFF: could not start it")
    return None
