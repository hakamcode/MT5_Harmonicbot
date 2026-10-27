# Harmonic Pattern Telegram Bot (MetaTrader 5)

Scans your MT5 symbols for harmonic patterns (Gartley, Bat, Alt Bat, Butterfly, Crab, Deep Crab, AB=CD),
draws the chart in your team's style and sends it to Telegram with the full trade plan, then follows
each trade and tells you what to do: entry, TP1 (close part + move SL), TP2, or close when TP2 is invalid.


## NEW: the v2 app (all harmonic patterns, auto trading, MT5-style window)

`app.py` is the same app as Butterfly Bot v3, but it trades **every harmonic pattern**: Gartley, Bat, Alt Bat,
Butterfly, Crab, Deep Crab and AB=CD. Pure Python (no Claude AI needed). Double-click **`Start Bot.bat`**.
Try it first without MT5 with **`Try Simulator.bat`**, and check the order functions on demo with **`Test MT5.bat`**.

Everything from v3 is here: MT5-style window (Market Watch, chart refreshed every 5 s, Trade / Patterns / History /
Journal tabs, balance line, status bar), pop-up notifications, Start/Stop, Close All and close one position
(right-click), Telegram messages for every action, and the same money management:

| Rule | Default | Setting in `config.json` |
|---|---|---|
| Lot size | risk **1% of the balance** per trade | `money.risk_percent` |
| Open positions | **max 3 at the same time**, no daily count limit; scanning is unlimited and a signal that comes while 3 are open waits for a free place | `money.max_open_positions` |
| Entry | first reversal candle after price reaches the pattern's PRZ | `harmonic.entry_window_bars` |
| Stop loss | just beyond the level where the pattern is wrong (see the table below) | `harmonic.patterns.<name>.sl_xa`, `sl_buffer_xa` |
| TP1 / TP2 | 0.382 / 0.618 of the A→D leg (C→D for AB=CD); at TP1 close 50% and move the stop to entry | `harmonic.tp1_ad`, `tp2_ad`, `money.tp1_close_percent` |
| Daily stop | no new trades for the rest of the day after a 3% loss | `money.daily_loss_limit_percent` |
| Real money | refused unless you set `allow_live_trading` to true | `allow_live_trading` |

| Pattern | B (of XA) | D zone / PRZ | Stop just beyond |
|---|---|---|---|
| Gartley | 0.58–0.66 | 0.786–0.83 XA | X (1.0 XA) |
| Bat | 0.382–0.50 | 0.886–0.93 XA | X (1.0 XA) |
| Alt Bat | 0.30–0.382 | 1.13–1.18 XA | 1.27 XA |
| Butterfly | 0.74–0.83 | 1.27–1.414 XA | 1.618 XA |
| Crab | 0.382–0.618 | 1.618–1.70 XA | 2.0 XA |
| Deep Crab | 0.85–0.92 | 1.618–1.70 XA | 2.0 XA |
| AB=CD | – | CD = 1.0–1.272 AB | 1.618 AB |

Switch a pattern off with `"enabled": false` in `harmonic.patterns`. Your existing `config.json` keeps working:
missing `money` / `harmonic` settings are taken from `config.example.json`. The app's orders are marked 777002
(v3 uses 777003), so the two apps never touch each other's trades.

The older alert bot (`bot.py`, with optional Claude AI) below still works on its own.

## What you get in Telegram

| Alert | When | Chart |
|---|---|---|
| ⏳ SETUP FORMING | X-A-B-C found, price heading to the PRZ. Shows PRZ (1), alternate PRZ (2), stop, TP1, TP2 | yes |
| 📍 PRICE IN PRZ | Price touched the PRZ | no |
| 🟢 BUY NOW / 🔴 SELL NOW | Entry triggered: entry price, stop loss, TP1, TP2, RR, ratios | yes |
| ✅ TP1 HIT | Close 50% and move SL to entry (TS/SL+) | yes |
| 🏁 TP2 HIT | Close the rest | yes |
| ⚠️ TP2 INVALID — CLOSE POSITION | After TP1, price came back to the TS/SL+ level, or TP2 not reached within `tp2_max_bars` | yes |
| ❌ STOP LOSS HIT / 🚫 CANCELLED / ⌛ EXPIRED | Trade lost, or setup broke / never reached the PRZ | stop only |

Example output from the offline test is in `demo_out/` (messages `*.txt`, charts `*.png`).

## Trading rules (from the team study, all adjustable in `config.json`)

- **PRZ**: band between the pattern's XA level (e.g. 0.886 for Bat) and the BC projections that agree with it.
  When one X-A-B-C fits two patterns, the nearer is PRZ (1), the next PRZ (2), each with its own stop.
- **Entry** (`entry_mode`): `confirm` = after price touches the PRZ, enter on the first candle that closes in the
  reversal direction (default). `touch` = enter at the PRZ inner edge.
- **TP1** = 0.382 of the A→D leg measured from the PRZ outer edge (`tp1_leg_ratio`).
- **TP2** = 1.618 × the TP1 distance from the PRZ outer edge (`tp2_multiple`), the rule found on your charts.
- **Stop** = PRZ outer edge + 0.4 × the TP1 distance (`stop_buffer`, the average of your charts).
- **At TP1**: close `tp1_close_percent` (50%) and move SL to entry (`ts_lock_ratio` 0 = breakeven,
  0.3 = lock 30% of the TP1 move).
- **TP2 invalid → close**: price returns to the TS/SL+ level, or `tp2_max_bars` passes.

## Setup (Windows PC with MetaTrader 5)

1. Install Python 3.10+ from python.org (tick "Add to PATH").
2. Open a terminal in this folder and run `pip install -r requirements.txt`.
3. Telegram:
   - In Telegram open **@BotFather**, send `/newbot`, copy the **token**.
   - Send any message to your new bot, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `"chat":{"id": ...}`.
     (For a group: add the bot to the group, send a message there; group ids start with `-`.)
4. Copy `config.example.json` to `config.json`, paste the token and chat id, and set your symbols
   exactly as your broker names them (e.g. `XAGUSD`, `XMRUSD`, `EURUSD.m`) and the timeframes
   (`M15 M30 H1 H2 H4 H8 D1 W1`).
5. Open MetaTrader 5 and log in. Leave `mt5.login/password/server` empty to use the open terminal.
6. Test offline: `python demo.py` (writes to `demo_out/`, sends nothing).
7. Test live once: `python bot.py --once`.
8. Run: `python bot.py`. It checks every `poll_seconds` and acts only when a new candle closes.
   Keep the PC (or a Windows VPS) and MT5 running.

Replay your own history: export bars from MT5 (or any CSV with time,open,high,low,close) and run
`python demo.py --csv EURUSD_H4.csv --symbol EURUSD --tf H4` to see which alerts the bot would have sent.

## Claude finds the patterns (detector)

`"detector"` in `config.json` chooses who finds the setups:

| value | who finds the patterns |
|---|---|
| `"claude"` | Claude AI looks at the chart (image + last `scan_bars` candles + `method.md`) on the timeframes in `claude.scan_timeframes`. Other timeframes still use the Python detector. |
| `"both"` | Python and Claude both look; you get alerts from either. |
| `"python"` | the original Python rules only (free). |

Without an `anthropic_api_key` the bot always uses Python. Python always does the rest: it checks Claude's
numbers against the real candles (swing points must be real highs/lows, levels in the right order, price not
already past the stop or C, ratios close to the named pattern, reward to risk at least `min_rr_tp1`) and drops
anything that fails, then follows the trade, places orders (if auto trading is on) and sends everything to Telegram.
Setups found by Claude show "🧠 Found by Claude" with its confidence and reason.

**Cost control (current setting): Claude scans H4 only, at most 5 scans a day in total** (`scan_timeframes`,
`max_scans_per_day`). The 5 scans are spread over the day (one about every 4 hours) and rotate through your symbols,
so each symbol gets a Claude scan about every 1.5 days with 8 symbols. Cost: about $0.10–0.20 per scan, so roughly
**$0.50–1 a day**. H1 and D1 (not in `scan_timeframes`) keep using the free Python detector.
More scans = more chances to catch a setup: e.g. `"max_scans_per_day": 40` scans every symbol on every H4 candle
(about $4–8 a day). `"model": "claude-sonnet-5-5"` costs about half; `"scan_effort"` can be `low`, `medium` or `high`.

Test offline first: `python demo.py --claude-scan` (about 8 scans on the test chart, roughly $1–2; the daily limit is not applied in the demo).

## Claude AI review (optional, paid per use)

When switched on, every **SETUP FORMING** and **BUY/SELL NOW** alert is first sent to Claude (model
`claude-opus-5-5`) together with the chart image, the pattern numbers, the last 40 candles and your team's method
(`method.md`). Right after the chart, Telegram gets a second message like:

> 🧠 Claude review — EURUSD · H4 Bullish Bat
> ⚠️ CAUTION (confidence 62%)
> Clean Bat, ratios close to ideal, but reward to risk to TP1 is only 0.6 ...
> 👀 Watch out: a close below 1.0934 cancels the idea.

Switch it on:
1. Go to **console.anthropic.com**, sign up, open **Billing** and add credit (e.g. $5).
2. Open **API Keys** → **Create Key**, copy it (starts with `sk-ant-`). Never paste it in a chat or share it.
3. In `config.json` put it in `"claude": {"anthropic_api_key": "sk-ant-..."}`, then `pip install -r requirements.txt`.
4. Test: `python demo.py --ai` (about 5 reviews, a few cents) and look at `demo_out/`.

Cost: roughly **$0.03–0.08 per reviewed alert**. Choose which alerts get reviewed with `review_events`
(`watch`, `entry`, also possible: `tp1`, `tp2_invalid`). `effort` can be `low` (cheaper, faster), `medium`, or `high`.
Leave the key empty to switch it off. If Claude is slow or fails, the alert is still sent, just without the review.
Edit `method.md` to teach Claude more of your team's rules. Claude's verdict is a second opinion; auto trading
does not use it.

## Auto trading (off by default)

`"auto_trade": true` (in `strategy`) makes the bot open the trade itself at entry, close `tp1_close_percent` at TP1
and move the SL, and close the rest when TP2 becomes invalid. Enable **Algo Trading** in MT5.

Safety limits:
- It only trades on a **demo account**. On a real-money account it refuses and tells you in Telegram, unless you
  set `"allow_live_trading": true` yourself (top level of `config.json`). Run it on demo for a few weeks first.
- Lot per trade = `lot_size`, never more than `max_lot` (default 0.05).
- At most `max_open_trades` (default 3) bot trades open at the same time.
- Every order (or failed order) is reported in Telegram.

## Files

- `bot.py` live loop · `engine.py` scan/manage/notify · `harmonics.py` swings, patterns, PRZ, trade rules
- `chart.py` chart image · `messages.py` Telegram text · `telegram.py` sender · `mt5_feed.py` MT5 data/orders
- `claude_scanner.py` Claude pattern finder + Python checks · `ai_review.py` optional Claude review · `method.md` your team's method, read by Claude
- `demo.py` offline replay · `state.json` (created at run time) remembers open setups across restarts
