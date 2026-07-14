# ws-journal

A self-hosted trade journal + analytics + MCP server for Wealthsimple
self-directed accounts. Everything flows through one SQLite database, so each
layer is independently replaceable.

```
┌─────────────────────┐     ┌──────────────────────┐
│ ingest_api.py        │     │ ingest_csv.py         │
│ (unofficial WS       │     │ (official statement   │
│  GraphQL API)        │     │  CSV fallback)        │
└──────────┬──────────┘     └──────────┬───────────┘
           └──────────┬────────────────┘
                      ▼
              ~/.ws-journal/journal.db  (SQLite)
                      ▲
        ┌─────────────┼──────────────────┐
        ▼             ▼                  ▼
  acb.py        dashboard.py        mcp_server.py
  (ACB P&L,     (Streamlit:         (FastMCP stdio →
   FX drag)      journal / perf /    Claude Desktop,
                 summary tabs)       Claude Code, etc.)
```

## Setup

Uses [uv](https://docs.astral.sh/uv/) for environment management:

```bash
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate   # or prefix commands with `uv run`
```

`curl_cffi` and `wealthsimple-python` are optional — only needed for the
price cache and API ingestion respectively. CSV ingestion, the dashboard,
and the MCP server work without them.

## Environment variables & credentials

**No API keys or tokens are required.** The full credential picture:

| What | Required? | Details |
|---|---|---|
| `WSJ_DB_PATH` | No (env var) | Path to the SQLite database. Defaults to `~/.ws-journal/journal.db`. Set it only if you want the DB somewhere else — and if you do, set it consistently everywhere (shell, cron, MCP config), or the components will silently read different databases. |
| Wealthsimple login | Only for API ingestion | First run of `ingest_api` prompts for your WS email/password/2FA; the library then caches its OAuth tokens in the **OS keyring** (needs the `keyring` package), so later runs — including cron — don't prompt. There is no API key to create — Wealthsimple has no official public API. Never hardcode or commit credentials. |
| `WS_ACCESS_TOKEN` / `WS_REFRESH_TOKEN` (or `WS_USERNAME` / `WS_PASSWORD`, `WS_OTP`) | No (env vars) | Non-interactive alternative to the prompt, e.g. for headless machines without a keyring. Sourced by `wealthsimple-python` automatically if set. |
| Yahoo Finance | No | `yfinance` scrapes public endpoints; no account or key needed. |
| Bank of Canada Valet API | No | Planned for FX mid-rate enrichment (not yet implemented). It's a free public API — no key needed there either. |
| MCP server | No | Read-only access to the local SQLite file; deliberately has no order placement, raw SQL, or credential access. |

## 1. Get data in

**Fallback that always works:** bulk-download monthly statement CSVs from
Wealthsimple → **Documents**, then:

```bash
python -m wsjournal.ingest_csv ~/Downloads/statement-*.csv
```

A sample dataset is included for trying things out:

```bash
python -m wsjournal.ingest_csv sample_data/sample_trades.csv
```

**Preferred (automatic):** unofficial API pull. First inspect the raw payloads
so you can confirm/fix the field mapping in `wsjournal/ingest_api.py`
(`FIELD_MAP` / `TRADE_TYPES` / `FX_TYPES`):

```bash
python -m wsjournal.ingest_api --inspect   # prints first 5 raw activities
python -m wsjournal.ingest_api             # ingests trades + FX events
```

The first run prompts for your Wealthsimple login + 2FA in the terminal and
caches the session. Schedule it once a day (cron example — note cron won't be
able to answer a 2FA prompt, so run interactively first to establish the
session):

```cron
30 18 * * 1-5  cd /path/to/ws-journal && .venv/bin/python -m wsjournal.ingest_api
```

The pull paginates through your full activity history automatically (the
server caps each page at ~50 items). Only **filled** orders are ingested —
cancelled, rejected, and expired-unfilled orders stay in `raw_activities`.
Share trades land in `trades`; options fills (`OPTIONS_BUY`/`OPTIONS_SELL`)
and expiries (`OPTIONS_EXPIRY`) land in `option_trades`. After a
normalization fix you can rebuild both from the immutably stored raw
payloads without touching the API:

```bash
python -m wsjournal.ingest_api --renormalize
```

## 2. Cache prices (for the performance charts + unrealized P&L)

```bash
python -m wsjournal.prices                                   # daily + hourly refresh
python -m wsjournal.prices intraday 30                       # hourly bars only, 30-day window
python -m wsjournal.prices set SHOP 2026-06-09 105.32 USD    # manual fallback
```

Daily closes land in `prices`; hourly bars (60-minute, last 14 days by
default) land in `prices_intraday` and power the dashboard's
**Daily / Hourly** granularity toggle on the Performance tab — buy/sell
markers sit at the actual execution time on the hourly view.

Listing resolution is **price-fit based**: tickers collide across exchanges
(CAD-hedged CDRs on Canadian exchanges share symbols with NASDAQ/NYSE
stocks — INTC, AVGO, TXN…), so every candidate listing (bare US, `.TO`,
`.V`, `.CN`, `.NE`) is scored by how well its same-day closes explain your
actual fill prices, and the best fit wins. Resolutions persist in the
`symbol_listings` table; re-run from scratch with:

```bash
python -m wsjournal.prices resolve
```

The stored price currency is the listing's quote currency. The USD/CAD rate
(`USDCAD=X`) is cached on every refresh for converting USD quotes to CAD.
If a resolution is ever wrong (e.g. a symbol with no share trades to score,
or split-adjusted history), pin it in `SYMBOL_OVERRIDES` in
`wsjournal/prices.py` — overrides always win.

## 3. Dashboard

```bash
streamlit run dashboard.py
```

Tabs: **Journal** (filterable trade log with per-trade P&L attribution
columns and the modeled FX conversion cost on USD-listed trades — the fee
column isn't shown since WS reports zero commissions; its real cost is the
~1.5% FX fee embedded in the CAD amounts — plus your notes + agent notes),
**Performance** (price series with share buys/sells AND option fills
overlaid — strike lines, expiry markers, holding-period shading — plus a
per-symbol cumulative realized P&L chart contrasting shares vs options;
option-only underlyings appear in the picker too), **Summary** (realized
gains by ACB, fees, FX conversion drag, superficial-loss flags).

Both Journal and Performance show the **trade ledger**
(`wsjournal.acb.trade_ledger`): per fill, the realized gain locked in, the
remaining position marked at that fill's own price, cumulative total P&L,
the mark-to-market delta since the previous trade (pure market movement on
what was held — a fill itself never changes total P&L at execution), and on
re-entry buys the move missed or avoided while flat.

## 4. MCP server (let agents analyze your trading)

The MCP server exposes the journal to AI clients as six tools:
`journal_list_trades`, `journal_get_positions`, `journal_pnl_summary`,
`journal_list_options`, `journal_symbol_history`, `journal_add_note`.
All are read-only except
`journal_add_note`, which lets an agent write insights back into your journal
(attributed `author='agent'`, visible in the dashboard). There is
intentionally no order placement, no raw SQL, and no credential access in the
MCP layer.

### Claude Code

```bash
claude mcp add ws-journal -- \
  /absolute/path/to/ws-journal/.venv/bin/python \
  /absolute/path/to/ws-journal/mcp_server.py
```

Then verify and use:

```bash
claude mcp list          # should show: ws-journal ... ✔ Connected
```

Restart any open session to pick up the new server (`/mcp` inside a session
shows server status and tools). The server is registered in local scope —
available only inside this project directory. Add `--scope user` to make it
available everywhere.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "ws-journal": {
      "command": "/absolute/path/to/ws-journal/.venv/bin/python",
      "args": ["/absolute/path/to/ws-journal/mcp_server.py"],
      "env": { "WSJ_DB_PATH": "/home/you/.ws-journal/journal.db" }
    }
  }
}
```

The `env` block is only needed if you moved the DB with `WSJ_DB_PATH`.

### Things to ask once connected

- "What's my realized P&L for the 2026 tax year, and how much did FX fees cost me?"
- "Review my SHOP trades and write a post-mortem note into the journal."
- "Which symbols have I traded at the worst win rate?"
- "Which of my loss sales might trigger the superficial-loss rule?"

## Caveats (read these)

- The API ingestion uses an **unofficial, reverse-engineered** library. It can
  break anytime; the CSV path is the safety net. Keep pulls infrequent and
  read-only.
- The ACB engine implements average cost basis in CAD but **not** the
  superficial-loss rule — loss lots are flagged for manual review. For filing
  taxes, verify against your official T5008/statements (or
  adjustedcostbase.ca).
- Options P&L is net premium per contract. Expiry, assignment, and exercise
  activities haven't been observed in the WS feed yet: contracts past expiry
  with open interest are assumed to expire worthless (and flagged) — verify
  assignments manually. Dividends are not ingested at all yet.
- **FX drag** is an estimate: Wealthsimple embeds its ~1.5% CAD↔USD
  conversion fee in the reported CAD amounts of every USD-listing trade, so
  realized P&L already includes the cost. The Summary metric models it at a
  configurable 1.5% of USD volume and cross-checks against implied rates
  from cached hourly bars (observed ≈1.55% on this account). It is shown for
  awareness — don't subtract it from P&L a second time.
- Nothing here is investment or tax advice.
