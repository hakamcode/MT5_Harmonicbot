# Hakam's team harmonic method (what Claude judges each alert against)

You can edit this file: Claude reads it fresh every time the bot starts.

## The method

- Draw an XABCD (or ABCD) pattern, project a **PRZ** (Potential Reversal Zone) box where D should complete,
  and put a **STOP** just beyond it, at a structural level (just past X, or past the next fib level such as 1.618 XA).
- Attach a fixed target ladder to the PRZ: **Expect Target (1)** = TP1, **Expect Target (2)** = TP2, and a green
  mid line between them. From the PRZ outer edge: TP2 = 1.618 x the TP1 distance, mid line = 1.309 x.
- When one X-A-B-C fits two patterns (e.g. Bat vs Butterfly), draw **PRZ (2)** deeper with its own stop.
  If price reverses from PRZ (1), PRZ (2) is cancelled. If price closes beyond the PRZ (1) stop, PRZ (2) becomes live.
- Enter inside the PRZ, ideally after a reversal candle (test / retest of the zone).
- At TP1: close part (50%) and move the stop to entry or a little into profit (**TS/SL+**).
  If price comes back to TS/SL+ after TP1, TP2 is invalid: close the rest.
- While price is still travelling C to D the team also trades the leg itself (**SELL/BUY ON PULLBACK**)
  toward the PRZ. The bot does not alert these yet.

## Patterns the team trades

Mainly **Bat, Butterfly, Alternate Bat, Crab and ABCD**, sometimes Gartley.
Ratios are TradingView style: XB = AB/XA, AC = BC/AB, BD = CD/BC, XD = AD/XA.

| Pattern | XB | XD (D level) |
|---|---|---|
| Gartley | 0.618 | 0.786 |
| Bat | 0.382 - 0.50 | 0.886 |
| Alternate Bat | <= 0.382 | 1.13 |
| Butterfly | 0.786 | 1.27 (to 1.414) |
| Crab | 0.382 - 0.618 | 1.618 |
| Deep Crab | 0.886 | 1.618 |
| AB=CD | - | CD = AB (1.27 / 1.618 extensions) |

AC is normally 0.382 - 0.886.

## What a good setup looks like for the team

- Clean, clearly visible swings; the ratios close to the ideal numbers above.
- Several levels agree inside a tight PRZ (XA level plus BC projection, maybe AB=CD).
- The PRZ sits at an obvious support / resistance area or the end of a strong, extended C-D leg.
- Reward to risk to TP1 near 1:1 or better, and TP2 clearly better.
- Higher timeframes (H4, H8, D1) are trusted more than small ones.

## Warning signs

- Price driving into the PRZ with very large, strong candles and no slowing down (the zone may get run over).
- A higher-timeframe trend pushing hard in the direction of the C-D leg.
- Messy, overlapping swings where the X-A-B-C points are not obvious.
- A very wide PRZ or a stop far away so the reward to risk is poor.
- TP1 sitting right at or behind a strong level that price may not get through.
