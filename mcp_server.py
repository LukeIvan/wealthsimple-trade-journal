"""ws_journal_mcp — MCP server exposing the trade journal to AI agents.

Run locally over stdio. Add to Claude Desktop / Claude Code config:

    {
      "mcpServers": {
        "ws-journal": {
          "command": "python",
          "args": ["/path/to/ws-journal/mcp_server.py"],
          "env": {"WSJ_DB_PATH": "/home/you/.ws-journal/journal.db"}
        }
      }
    }

Design notes:
  - All tools are read-only except journal_add_note (agents can write insights
    back into the journal, clearly attributed with author='agent').
  - There is deliberately NO raw-SQL tool and NO tool that can place orders or
    touch credentials. The server only ever opens the SQLite file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP

from wsjournal import acb, db, fxdrag, options

mcp = FastMCP("ws_journal_mcp")


def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


class ListTradesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: Optional[str] = Field(None, description="Filter by ticker, e.g. 'SHOP'")
    start: Optional[str] = Field(None, description="ISO date lower bound, e.g. '2026-01-01'")
    end: Optional[str] = Field(None, description="ISO date upper bound")
    action: Optional[str] = Field(None, description="'BUY' or 'SELL'")
    limit: int = Field(100, ge=1, le=1000, description="Max rows returned")


@mcp.tool(
    name="journal_list_trades",
    annotations={"title": "List trades", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def journal_list_trades(params: ListTradesInput) -> str:
    """List executed trades from the journal, newest first.

    Returns JSON rows: id, executed_at, symbol, action, quantity, price,
    currency, fx_rate, fee, gross_amount. Use filters to narrow before
    analyzing; prefer journal_pnl_summary for aggregate questions.
    """
    q = "SELECT * FROM trades WHERE 1=1"
    args: list = []
    if params.symbol:
        q += " AND symbol=?"; args.append(params.symbol.upper())
    if params.start:
        q += " AND executed_at>=?"; args.append(params.start)
    if params.end:
        q += " AND executed_at<=?"; args.append(params.end)
    if params.action:
        q += " AND action=?"; args.append(params.action.upper())
    q += " ORDER BY executed_at DESC LIMIT ?"; args.append(params.limit)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    return _j({"count": len(rows), "trades": rows})


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _listings(conn) -> dict:
    return {r["symbol"]: {"yahoo_symbol": r["yahoo_symbol"],
                          "quote_currency": r["quote_currency"],
                          "method": r["method"]}
            for r in conn.execute("SELECT * FROM symbol_listings")}


@mcp.tool(
    name="journal_get_positions",
    annotations={"title": "Open positions with ACB and unrealized P&L",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
def journal_get_positions(params: EmptyInput) -> str:
    """Current open positions with Canadian ACB cost basis, latest cached
    price, market value, and unrealized gain/loss in CAD. Positions with a
    'warnings' list have data-quality issues (e.g. missing FX rates) — call
    these out when giving advice.

    Each position carries its resolved 'listing': journal symbols are bare
    tickers that collide across exchanges, and several holdings are CAD-
    hedged CDRs on Canadian exchanges (quote_currency CAD) — NOT the US
    stock with the same ticker. Don't quote US market prices for those.
    """
    with db.connect() as conn:
        result = acb.compute(conn)
        positions = acb.unrealized(conn, result["open_positions"])
        listings = _listings(conn)
        for p in positions:
            p["listing"] = listings.get(p["symbol"])
    return _j({"positions": positions})


class PnlSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Optional[int] = Field(None, ge=2000, le=2100,
                                description="Restrict realized gains to a calendar year (tax year)")


@mcp.tool(
    name="journal_pnl_summary",
    annotations={"title": "P&L and fee summary", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def journal_pnl_summary(params: PnlSummaryInput) -> str:
    """Aggregate performance: total realized gains (CAD, ACB method),
    per-symbol realized breakdown, explicit fees, estimated FX conversion
    drag, win rate on closed lots, options premium P&L, and loss lots that
    should be checked against the superficial-loss rule. Optionally filtered
    to one tax year (note: the options and fx_drag blocks are all-time).

    fx_drag quantifies Wealthsimple's ~1.5% conversion fee embedded in every
    USD-listing trade. Realized gains ALREADY include this cost implicitly —
    do not subtract it again; it shows what implicit conversions cost.
    """
    with db.connect() as conn:
        result = acb.compute(conn)
        opt = options.compute(conn)["summary"]
        fx = fxdrag.compute(conn)
        realized = result["realized"]
        if params.year:
            realized = [r for r in realized
                        if str(r["sold_at"])[:4] == str(params.year)]
        by_symbol: dict[str, float] = {}
        for r in realized:
            by_symbol[r["symbol"]] = round(
                by_symbol.get(r["symbol"], 0) + r["gain_cad"], 2)
        wins = sum(1 for r in realized if r["gain_cad"] > 0)

        inc_q = ("SELECT income_type, ROUND(SUM(amount_cad),2) AS total_cad, "
                 "COUNT(*) AS cnt FROM income")
        if params.year:
            inc_q += f" WHERE occurred_at LIKE '{params.year}%'"
        inc_q += " GROUP BY income_type"
        inc_by_type = {r["income_type"]: {"total_cad": r["total_cad"],
                                           "count": r["cnt"]}
                       for r in conn.execute(inc_q).fetchall()}
        inc_divs = (inc_by_type.get("DIVIDEND") or {}).get("total_cad") or 0.0
        inc_int = (inc_by_type.get("INTEREST") or {}).get("total_cad") or 0.0
        inc_tax = (inc_by_type.get("NON_RESIDENT_TAX") or {}).get("total_cad") or 0.0

        loss_lots = [r for r in realized if r["gain_cad"] < 0]
        return _j({
            "year": params.year or "all",
            "realized_gain_cad": round(sum(r["gain_cad"] for r in realized), 2),
            "closed_lots": len(realized),
            "win_rate": round(wins / len(realized), 3) if realized else None,
            "by_symbol": by_symbol,
            "explicit_fees_cad": result["summary"]["explicit_fees_cad"],
            "fx_conversion_cost_cad": fx["modeled_drag_cad"],
            "fx_drag": {
                "modeled_drag_cad": fx["modeled_drag_cad"],
                "fee_rate": fx["fee_rate"],
                "usd_volume_cad": fx["usd_volume_cad"],
                "n_usd_trades": fx["n_usd_trades"],
                "n_usd_option_fills": fx["n_usd_option_fills"],
                "empirical_median_premium": fx["empirical"]["median_premium"],
                "empirical_hourly_median_premium":
                    fx["empirical"]["hourly_median_premium"],
                "n_estimated": fx["empirical"]["n_estimated"],
                "n_outliers_excluded": len(fx["empirical"]["outliers"]),
                "warnings": fx["warnings"],
            },
            "options": {
                "realized_premium_cad": opt["realized_premium_cad"],
                "open_contracts": opt["open_contracts"],
                "open_net_premium_cad": opt["open_net_premium_cad"],
            },
            "income": {
                "dividends_cad": inc_divs,
                "interest_cad": inc_int,
                "withholding_tax_cad": inc_tax,
                "net_cad": round(inc_divs + inc_int + inc_tax, 2),
                "by_type": inc_by_type,
            },
            "loss_lots_check_superficial_rule": loss_lots,
            "n_superficial_losses": sum(1 for r in loss_lots
                                        if r.get("is_superficial")),
        })


class ListOptionsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    underlying: Optional[str] = Field(
        None, description="Filter by underlying ticker, e.g. 'NOK'")
    status: Optional[str] = Field(
        None, description="'open', 'closed' or 'expired'")


@mcp.tool(
    name="journal_list_options",
    annotations={"title": "List option contracts with premium P&L",
                 "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
def journal_list_options(params: ListOptionsInput) -> str:
    """Option contracts grouped by (underlying, type, strike, expiry) with
    net contracts, net premium in CAD, and status (open / closed / expired).
    Contracts with a 'warnings' list have caveats (e.g. expiry assumed
    worthless because assignment isn't tracked) — call these out when
    giving advice.
    """
    with db.connect() as conn:
        result = options.compute(conn)
    contracts = result["contracts"]
    if params.underlying:
        contracts = [c for c in contracts
                     if c["underlying"] == params.underlying.upper()]
    if params.status:
        contracts = [c for c in contracts
                     if c["status"] == params.status.lower()]
    return _j({"count": len(contracts), "contracts": contracts,
               "summary": result["summary"]})


class SymbolHistoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: str = Field(..., min_length=1, max_length=12,
                        description="Ticker, e.g. 'SHOP'")


@mcp.tool(
    name="journal_symbol_history",
    annotations={"title": "Full history for one symbol", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def journal_symbol_history(params: SymbolHistoryInput) -> str:
    """Everything the journal knows about one symbol: every share trade,
    option trades on it as underlying, realized lots, notes, the resolved
    Yahoo listing, and the last 30 cached daily closes. Ideal for 'review my
    trading in X' style analysis. Check 'listing' before reasoning about
    prices: a CAD quote_currency means a Canadian listing/CDR, not the US
    stock with the same ticker.
    """
    sym = params.symbol.upper()
    with db.connect() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE symbol=? ORDER BY executed_at", (sym,))]
        opts = [dict(r) for r in conn.execute(
            "SELECT * FROM option_trades WHERE underlying=? "
            "ORDER BY occurred_at", (sym,))]
        lots = [dict(r) for r in conn.execute(
            "SELECT * FROM realized_pnl WHERE symbol=? ORDER BY sold_at", (sym,))]
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE symbol=? ORDER BY created_at", (sym,))]
        px = [dict(r) for r in conn.execute(
            "SELECT date, close, currency FROM prices WHERE symbol=? "
            "ORDER BY date DESC LIMIT 30", (sym,))]
        listing = _listings(conn).get(sym)
    if not trades and not opts:
        return _j({"error": f"No trades found for {sym}. "
                            "Call journal_list_trades to see available symbols."})
    return _j({"symbol": sym, "listing": listing, "trades": trades,
               "option_trades": opts, "realized_lots": lots, "notes": notes,
               "recent_prices": px})


class ListIncomeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: Optional[str] = Field(None, description="Filter by ticker, e.g. 'XOM'")
    income_type: Optional[str] = Field(
        None, description="DIVIDEND | INTEREST | NON_RESIDENT_TAX | FEE | PROMOTION")
    start: Optional[str] = Field(None, description="ISO date lower bound, e.g. '2026-01-01'")
    end: Optional[str] = Field(None, description="ISO date upper bound")
    limit: int = Field(500, ge=1, le=2000, description="Max rows returned")


@mcp.tool(
    name="journal_list_income",
    annotations={"title": "List income events", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def journal_list_income(params: ListIncomeInput) -> str:
    """Income events from the journal: dividends, interest (cash and stock-
    lending), withholding tax (NON_RESIDENT_TAX), and management fees.
    amount_cad is signed — positive for inflows, negative for tax/fees.
    Returns rows plus a per-type summary of totals.
    """
    q = "SELECT * FROM income WHERE 1=1"
    args: list = []
    if params.symbol:
        q += " AND symbol=?"; args.append(params.symbol.upper())
    if params.income_type:
        q += " AND income_type=?"; args.append(params.income_type.upper())
    if params.start:
        q += " AND occurred_at>=?"; args.append(params.start)
    if params.end:
        q += " AND occurred_at<=?"; args.append(params.end)
    q += " ORDER BY occurred_at DESC LIMIT ?"; args.append(params.limit)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        summary_rows = conn.execute(
            "SELECT income_type, ROUND(SUM(amount_cad),2) AS total_cad, "
            "COUNT(*) AS count FROM income GROUP BY income_type"
        ).fetchall()
    by_type = {r["income_type"]: {"total_cad": r["total_cad"],
                                   "count": r["count"]}
               for r in summary_rows}
    return _j({"count": len(rows), "income": rows, "by_type": by_type})


class AddNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    body: str = Field(..., min_length=1, max_length=5000,
                      description="The note text")
    symbol: Optional[str] = Field(None, description="Ticker this note relates to")
    trade_id: Optional[str] = Field(None, description="Specific trade id to attach to")
    tag: Optional[str] = Field(None, description="e.g. 'thesis', 'mistake', 'review', 'agent-insight'")


@mcp.tool(
    name="journal_add_note",
    annotations={"title": "Add a journal note", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
def journal_add_note(params: AddNoteInput) -> str:
    """Append a note to the journal (attributed to author='agent'). Use this
    to persist insights, post-trade reviews, or flagged patterns so the human
    sees them in the dashboard's journal section. Never overwrites anything.
    """
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes (created_at, trade_id, symbol, tag, body, author) "
            "VALUES (?,?,?,?,?,'agent')",
            (datetime.now(timezone.utc).isoformat(), params.trade_id,
             params.symbol.upper() if params.symbol else None,
             params.tag, params.body),
        )
        return _j({"ok": True, "note_id": cur.lastrowid})


if __name__ == "__main__":
    db.init_db()
    mcp.run(transport="stdio")
