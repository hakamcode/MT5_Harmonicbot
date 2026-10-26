# Harmonic trading bots (MetaTrader 5 + Telegram)

| Folder | What it is |
|---|---|
| `harmonic-bot-v3/` | **Butterfly Bot v3**: pure Python desktop app (Start/Stop, live positions, Close All) that trades the Butterfly pattern with full money management. Start here: `harmonic-bot-v3/README.md` |
| `harmonic-bot-v2/` | Harmonic scanner (all patterns) with Telegram alerts and optional Claude AI pattern finding/review |

Both run on Windows with MetaTrader 5. Copy `config.example.json` to `config.json` inside the folder and fill it in.
`config.json` is never uploaded to GitHub (see `.gitignore`), so your Telegram token and passwords stay on your PC.
