# CLAUDE.md — ws-journal project context

## What this is
A self-hosted trade journal for a Wealthsimple (Canada) self-directed account,
built because the WS UI makes it hard to track P&L, fees, and FX conversion
costs, and paid journals (TradesViz etc.) weren't worth a subscription.

Three components, all reading one SQLite DB (`~/.ws-journal/journal.db`,
overridable via `WSJ_DB_PATH`):
1. **Journal** — Streamlit dashboard (`dashboard.py`), Journal/Performance/Summary tabs
2. **Analytics** — `wsjournal/acb.py` (Canadian ACB share P&L in CAD) and
   `wsjournal/options.py` (per-contract option premium P&L)
3. **MCP server** — `mcp_server.py`, FastMCP over stdio, lets agents query the journal

## Architecture principles (do not break these)
- Ingestion is decoupled: `ingest_api.py` (unofficial wealthsimple-python
  GraphQL lib) and `ingest_csv.py` (official statement CSVs) both write the
  same normalized schema in `wsjournal/db.py`. The API WILL break someday;
  CSV is the permanent fallback. Downstream code must only depend on the schema.
- Raw payloads are preserved immutably in `raw_activities` for re-derivation.
- The MCP server is read-only except `journal_add_note` (agent-attributed
  notes). Never add order placement, raw SQL execution, or credential access
  to the MCP layer.
- Tax convention: ACB (not FIFO), computed in CAD; USD trades convert at
  per-trade `fx_rate` (CAD per USD).
- **Currency convention:** For a CAD-only account (pre-2026-07), WS reported
  `amount` in CAD with FX already baked in (`currency='CAD'`, `fx_rate`
  informational). For a **CAD/USD hybrid account** (switched 2026-07), USD
  trades settle in USD with no WS conversion: `currency='USD'`, `fx_rate=NULL`.
  The ACB engine resolves these using the daily `USDCAD=X` close from the
  `prices` table for the trade date (`acb._daily_fx_map`); `pos.last_fx_rate`
  is the fallback for weekend/holiday gaps or a cold cache. `acb.unrealized()`
  correctly uses the latest cached rate for mark-to-market (no change needed
  there). `prices.currency` is the LISTING's quote currency from Yahoo —
  never use it to infer trade settlement currency or vice versa (that
  conflation caused the `.TO`-suffix bug).
- Options activities (`OPTIONS_BUY`/`OPTIONS_SELL`) go to `option_trades`,
  never to `trades`; `premium` is the total cash amount (×100 multiplier
  already included). `python -m wsjournal.ingest_api --renormalize` replays
  `raw_activities` through current normalizers after a normalization fix.

## Verified working (tested against the real account, 2026-06-10)
- API ingest end-to-end with cursor pagination (server caps pages at ~50
  items; `_fetch_all_activities` follows pageInfo.endCursor — 493 activities)
- **Status filtering**: only `status='FILLED'` activities become trades;
  the feed also carries CANCELLED / REJECTED / EXPIRED (unfilled) orders
  under the same types — ingesting those inflated realized P&L ~6x
- Activity-type whitelist (TRADE_ACTIONS): substring matching misfiled
  OPTIONS_* and CRYPTO_* as share trades — never match on substrings
- Options pipeline incl. OPTIONS_EXPIRY events (action='EXPIRE', premium 0,
  status COMPLETED) → option_trades → options.compute → MCP/dashboard
- Probe-based price resolution: bare US listing first, then `.TO`/`.V`/
  `.CN`/`.NE`; quote currency from Yahoo meta; USDCAD=X cached
- Hourly bars (`prices_intraday`, 60m interval, 14-day window) for the
  dashboard's granularity toggle
- All 6 MCP tools end-to-end (incl. journal_list_options)
- CSV ingest → ACB engine math (checked against hand calculations)
- Streamlit dashboard boots (HTTP 200) + data path exercised headless;
  executed_at/occurred_at converted to datetime dtype for temporal sorting
- Performance tab plots options alongside shares (verified via AppTest on
  every option underlying, daily + hourly): option fills anchored to the
  nearest plotted close, strike lines colored by contract status, expiry
  vlines, holding-period shading, and a cumulative realized P&L chart
  (shares ACB lots vs options net premium cash flow vs total). Option-only
  underlyings (QCOM, POET) appear in the symbol picker. Strike lines
  outside [0.2×, 5×] of the plotted close range are suppressed with a
  caption — guards the MU-style case where the option underlies a
  different listing than the shares resolve to.
- Per-trade mark-to-market ledger (`acb.trade_ledger`, mirrors compute()'s
  ACB math — per-symbol realized sums verified identical): book_cost_cad +
  acb_per_share_cad (running cost basis after each fill), realized_cad /
  unrealized-at-fill / total_pnl_cad / mtm_delta_cad / flat_move_cad
  (missed-or-avoided move between full exit and re-entry). Shown on the
  Journal table (merged by trade id) and as the Performance tab's trade
  table.
- Per-trade modeled FX cost (`fxdrag.per_trade_costs`, same scope/math as
  the aggregate) on the Journal and ledger tables; blank for CAD
  listings/CDRs. The `fee` column is hidden from display (all 498 raw
  payloads report fees=0 — verified 2026-06-11; WS embeds costs in
  amounts) but stays in the schema for CSV ingestion.

## Known gaps / next work items
1. **Options assignment/exercise**: expiry is modeled (OPTIONS_EXPIRY), but
   assignment/exercise activity types have never appeared in the feed;
   `normalize_option` warns on unknown `OPTIONS_*` types. Past-expiry
   contracts without an expiry event are assumed worthless and flagged.
2. **Transfers and income not modeled**: INTERNAL_TRANSFER (22),
   INSTITUTIONAL_TRANSFER_INTENT, CORPORATE_ACTION (3), DIVIDEND (120),
   INTEREST (59), FEE, NON_RESIDENT_TAX, and CRYPTO_* activities are
   raw-only. Positions that arrived via transfer have no BUY in the feed,
   so their ACB is incomplete. Dividend/interest income tracking is the
   natural next feature.
3. **FX drag** is computed by `wsjournal/fxdrag.py` (hybrid: 1.5% modeled on
   USD-listing volume, validated empirically — hourly-matched subset observed
   ~1.55%). The fee is EMBEDDED in the CAD amounts WS reports, so realized
   gains already include it — never subtract the drag from P&L again. The
   `fx_events` path is dead for this account type (implicit per-trade
   conversions emit no FX activities); the table stays for other account
   types. Optional refinement: Bank of Canada Valet API for CRA-grade mid
   rates. Per-trade fx_rate is null — unrealized P&L falls back to latest
   USDCAD=X. Empirical estimates on pre-split trades are outliers (Yahoo
   closes are split-adjusted; trade prices aren't) — filtered at |10%|.
4. **Superficial-loss rule**: NOT implemented; loss lots are only flagged
   (`loss_lots_check_superficial_rule`). Implementing = check repurchase of
   identical property within ±30 days of a loss sale.
5. **CSV header drift**: real WS statement headers may differ from
   `HEADER_ALIASES` in `ingest_csv.py`; tighten against a real export.
   CSV path also has no options handling.
6. **Ticker collisions are auto-resolved by price fit** (`symbol_listings`
   table; `python -m wsjournal.prices resolve` to redo): every candidate
   listing (bare US, .TO/.V/.CN/.NE) is scored against the symbol's actual
   trade prices — fills are ground truth. This caught 13 CAD-hedged
   CDR/TSX holdings (AVGO.TO, INTC.TO, TXN.TO, SHLD.TO, KEEL.TO, ENB.TO,
   ...) that the old first-hit probe mapped to US listings. Caveats:
   symbols with NO share trades (option-only underlyings like QCOM) can't
   be scored → `fallback` to first candidate; split-adjusted Yahoo closes
   can degrade fit (warned at score > 0.25); `SYMBOL_OVERRIDES` always
   wins. Sub-case still open: the SAME feed symbol can be two securities —
   MU *shares* are TSX but the MU *option* underlies NASDAQ Micron
   (strike 970); per-security resolution needs a securityId map.
   CDRs are CAD-hedged → correctly excluded from fxdrag by quote currency.

## Conventions
- Python only; no ORM (plain sqlite3); pip installs may need
  `--break-system-packages` on Debian-based systems.
- Be gentle with the unofficial WS API: read-only, ≤1-2 pulls/day, never
  hardcode or commit credentials/tokens.
- P&L output is for personal tracking; reconcile with T5008 for tax filing.
