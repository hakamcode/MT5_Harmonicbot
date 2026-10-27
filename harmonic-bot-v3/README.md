# Butterfly Bot v3 (MetaTrader 5, pure Python, no AI)

A desktop app that trades **only the Butterfly harmonic pattern** (bullish and bearish) on your MT5 account,
with full money management, and sends every action to your Telegram.

What you see in the app (laid out like MetaTrader 5):

- **Toolbar**: Start Bot / Stop Bot, Close All Bot Positions (asks to confirm and shows the total P/L), bot ON/OFF
- **Market Watch**: live bid, ask and spread of your symbols
- **Bot settings**: the money management rules in use
- **Chart**: the Butterfly of each open trade or forming setup (click the tabs under the chart, or a row in the tables)
- **Toolbox** tabs at the bottom, like MT5:
  - **Trade**: open positions with S/L, T/P, profit and the bot stage, plus the balance/equity line.
    Right-click (or double-click) a position to close just that one.
  - **Butterflies**: setups the bot is waiting on, with the PRZ and stop
  - **History**: the bot's closed trades, how they closed (TP2, stop loss, ...) and the total result
  - **Journal**: everything the bot did (the same messages go to Telegram)
- **Status bar**: what the bot is doing, connection, and today's P/L with the daily stop level
- **Pop-up notifications** (bottom right, with a sound) when you press Start or Stop, when Start is refused
  (and why), when a trade opens, hits TP1, closes, and when the daily loss limit stops the bot
- The chart you are looking at is **redrawn every 5 seconds** with the newest candles and a live price line
  (`chart_refresh_seconds` in `config.json`)

v2 (`harmonic-bot`) is untouched and still works on its own.

## The trading rules

All numbers are in `config.json` and can be changed.

| Rule | Default | Setting |
|---|---|---|
| Pattern | Butterfly: B ≈ 0.786 XA, C 0.382–0.886 AB, BC projection 1.618–2.618 | `butterfly.xb_range`, `ac_range`, `bc_range` |
| PRZ (where D should be) | 1.27–1.414 of XA past A | `butterfly.prz_xa` |
| Entry | after price reaches the PRZ, the first candle that closes in the reversal direction (bullish candle for a buy) | `entry_window_bars` (cancel if no reversal within 10 candles) |
| Stop loss | just beyond the 1.618 XA extension | `sl_xa` 1.618 + `sl_buffer_xa` 0.02 |
| TP1 | 0.382 of the A→D leg, measured from the real D | `tp1_ad` |
| TP2 | 0.618 of the A→D leg | `tp2_ad` |
| At TP1 | close 50% and move the stop loss to the entry price (the rest can't lose any more) | `money.tp1_close_percent`, `move_sl_to_entry_at_tp1` |
| Lot size | so that hitting the stop loses **1% of the balance** | `money.risk_percent` |
| Biggest lot ever | 1.0 | `money.max_lot` |
| Scanning | no limit: every symbol and timeframe, every new candle. When 3 positions are already open, a new entry signal is kept and the bot enters on a later reversal candle if a place frees up while the setup is still valid | `symbols` (or `"all"` = every symbol in MT5's Market Watch), `timeframes` |
| Open positions | **max 3 at the same time**; no limit per day | `money.max_open_positions` |
| One trade per symbol | yes | `money.one_position_per_symbol` |
| Daily stop | when today's loss reaches **3% of the balance** the bot stops opening trades until the next day and tells you why | `money.daily_loss_limit_percent` |
| At the daily stop | open trades keep their own stop loss and TP | `close_positions_at_daily_limit` (set `true` to close them all) |
| Skip weak trades | reward to risk to TP1 below 0.5 | `butterfly.min_rr_tp1` |
| Close all button | closes the bot's positions only | `money.close_all_button`: `"bot"` or `"all"` (also your manual trades) |
| Real money | refused: the bot only trades a **demo** account | `allow_live_trading`: set `true` only when you are ready |

The stop loss and TP2 are placed inside MT5 with the order, so they work even if the app or the PC is off.
TP1 (the half close and moving the stop to entry) is done by the app, so keep the app open.

"Today" is the broker's server day. The daily loss is counted from the balance at the start of the day,
including the floating loss of open trades.

## Telegram messages

| Message | When |
|---|---|
| ▶️ STARTED / ⏸ STOPPED | you press Start or Stop |
| ⏳ Butterfly forming (with chart) | a new X-A-B-C is found, price is heading to the PRZ |
| 📍 price is in the PRZ | waiting for the reversal candle |
| 🟢 BUY / 🔴 SELL opened (with chart) | entry, lots, risk, stop loss, TP1, TP2 |
| ✅ TP1 HIT | 50% closed (with profit) and stop loss moved to entry |
| 🏁 TP2 HIT | rest closed at TP2, with the trade's total result |
| ❌ STOP LOSS HIT | with the trade's total result |
| 🔒 closed at entry after TP1 | price came back to the entry after TP1 |
| 🧹 closed by Close All button / ✋ closed manually | with the result |
| ⚠️ not trading | entry signal but max 3 positions open, daily stop, account too small for 1% risk, ... |
| 🛑 TRADING STOPPED FOR TODAY | daily loss limit reached |
| 🚫 cancelled | setup broke or price never reached the PRZ |

`send_forming_alerts` and `send_prz_alerts` (in `butterfly`) can be set to `false` for fewer messages.

## Setup (Windows PC with MetaTrader 5)

1. Install Python 3.10 or newer from python.org (tick **Add python.exe to PATH**). If you already did this for v2, skip it.
2. Unzip this folder, open it, click the address bar, type `cmd`, press Enter, then run:
   `pip install -r requirements.txt`
3. Copy `config.example.json` to `config.json`. Put in your Telegram `bot_token` and `chat_id`
   (the same ones as v2: you can copy the `telegram` part from v2's `config.json`).
   Put your symbols as you know them (`XMRUSD`, `EURUSD`...): the bot finds your broker's name (e.g. `#XMRUSD`) itself.
4. Open MetaTrader 5, log in to your **demo** account, and turn on the **Algo Trading** button (green).
   An account that allows **hedging** works best (the bot closes half a position at TP1).
5. Double-click **`Start Bot.bat`** (or run `python app.py`). Press **Start bot**.

Try it first without MT5: double-click **`Try Simulator.bat`** (`python app.py --sim`). It plays made-up prices
quickly on a fake 10,000 USD account so you can watch trades open, hit TP1, TP2 or the stop, and test the
Close All button. Nothing is sent to Telegram (`python app.py --sim --send` sends them).
`python test_sim.py` runs the same simulator without the window and writes the messages to `sim_out/`.

## Test the MT5 order functions (demo account)

Double-click **`Test MT5.bat`** (or `python test_mt5.py --symbol EURUSD`) with MT5 open on your **demo** account,
Algo Trading on, and the app's trades closed. It checks, one by one, with the smallest lot:
connect and account, symbol and price, lot size from risk, open BUY with SL/TP, move SL to entry, close half,
close the rest and read the result, the bot's TP1 handling, and the Close All button. It ends with
`RESULT: x of 24 checks passed` and closes everything it opened. It refuses to run on a real-money account.
At the weekend use a crypto symbol: `python test_mt5.py --symbol XMRUSD`. Add `--telegram` to also get the messages.

## Files

- `app.py` the window · `trader.py` the bot and money management · `butterfly.py` pattern finder
- `broker_mt5.py` MetaTrader 5 · `broker_sim.py` simulator · `chart.py` chart picture · `telegram.py` sender
- `state_v3.json` (made when running) remembers setups and trades if you restart · `bot.log` full log
