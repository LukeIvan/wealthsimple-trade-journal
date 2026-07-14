"""Estimate the hidden FX conversion drag on USD-listing trades.

Wealthsimple CAD accounts convert implicitly on every USD-listing trade: the
activity feed reports the CAD amount with the conversion (mid rate + ~1.5%
fee) already baked in, and emits no separate FX event. Realized P&L therefore
already includes the fee — this module only quantifies what it cost.

Hybrid model:
  - modeled (headline): fee_rate applied to all USD-listing volume —
    gross_amount * fee_rate / (1 + fee_rate), since the stored amount
    already contains the fee. Deterministic, no market-data noise.
  - empirical (diagnostic): per trade, implied applied rate = trade price /
    listing close (hourly bar at execution when cached, else same-day daily
    close), compared to the USDCAD mid. Validates the fee_rate assumption.
    Daily-close estimates carry +-3% intraday noise per trade; the
    hourly-matched subset is the trustworthy one. |premium| > 10% is a
    ticker-collision/split artifact and is excluded from aggregates.

USD-listing detection uses the QUOTE currency cached in prices (never the
trade currency, which is always the account currency CAD).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .prices import FX_SYMBOL

DEFAULT_FEE_RATE = 0.015
OUTLIER_PREMIUM = 0.10
HOURLY_MAX_GAP = timedelta(hours=2)


def _usd_symbols(conn: sqlite3.Connection) -> set:
    return {r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM prices WHERE currency='USD'")}


def _daily_close(conn, symbol: str, day: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM prices WHERE symbol=? AND date=?", (symbol, day)
    ).fetchone()
    return row["close"] if row else None


def _hourly_close(conn, symbol: str, ts: str) -> float | None:
    """Close of the last hourly bar at/before `ts`, if recent enough."""
    row = conn.execute(
        "SELECT ts, close FROM prices_intraday WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (symbol, ts)
    ).fetchone()
    if not row:
        return None
    gap = datetime.fromisoformat(ts) - datetime.fromisoformat(row["ts"])
    return row["close"] if gap <= HOURLY_MAX_GAP else None


def per_trade_costs(conn: sqlite3.Connection,
                    fee_rate: float = DEFAULT_FEE_RATE) -> dict:
    """trade_id -> modeled embedded FX cost (CAD); only USD-listing trades.

    Same model and scope as compute()'s headline figure, surfaced per trade
    for display. The cost is embedded in the reported CAD amount and already
    inside realized P&L — informational, never subtract it again.
    """
    usd = _usd_symbols(conn)
    factor = fee_rate / (1 + fee_rate)
    return {r["id"]: round(r["gross_amount"] * factor, 2)
            for r in conn.execute("SELECT id, symbol, gross_amount FROM trades")
            if r["symbol"] in usd}


def compute(conn: sqlite3.Connection,
            fee_rate: float = DEFAULT_FEE_RATE) -> dict:
    """Modeled FX drag + empirical implied-rate diagnostic."""
    usd = _usd_symbols(conn)
    factor = fee_rate / (1 + fee_rate)

    warnings = []
    unpriced = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM trades WHERE "
        "symbol NOT IN (SELECT DISTINCT symbol FROM prices) ORDER BY symbol")]
    if unpriced:
        warnings.append(
            f"no cached prices for {', '.join(unpriced)} — cannot tell if "
            "they're USD listings; excluded from FX drag")

    modeled = trade_volume = 0.0
    n_trades = 0
    estimates: list[dict] = []
    for r in conn.execute("SELECT * FROM trades ORDER BY executed_at"):
        if r["symbol"] not in usd:
            continue
        trade_volume += r["gross_amount"]
        modeled += r["gross_amount"] * factor
        n_trades += 1

        day = str(r["executed_at"])[:10]
        close = _hourly_close(conn, r["symbol"], r["executed_at"])
        source = "hourly"
        if close is None:
            close, source = _daily_close(conn, r["symbol"], day), "daily"
        mid = (_hourly_close(conn, FX_SYMBOL, r["executed_at"])
               or _daily_close(conn, FX_SYMBOL, day))
        if not close or not mid:
            continue
        premium = (r["price"] / close) / mid - 1
        estimates.append({
            "trade_id": r["id"], "symbol": r["symbol"], "day": day,
            "source": source, "premium": round(premium, 4),
            "outlier": abs(premium) > OUTLIER_PREMIUM,
        })

    option_volume = 0.0
    n_options = 0
    for r in conn.execute(
            "SELECT * FROM option_trades WHERE action IN ('BUY','SELL')"):
        if r["underlying"] in usd:
            option_volume += r["premium"]
            modeled += r["premium"] * factor
            n_options += 1

    included = sorted(e["premium"] for e in estimates if not e["outlier"])
    hourly = sorted(e["premium"] for e in estimates
                    if not e["outlier"] and e["source"] == "hourly")

    def _med(xs): return round(xs[len(xs) // 2], 4) if xs else None
    def _q(xs, f): return round(xs[int(len(xs) * f)], 4) if xs else None

    return {
        "fee_rate": fee_rate,
        "modeled_drag_cad": round(modeled, 2),
        "usd_volume_cad": round(trade_volume + option_volume, 2),
        "n_usd_trades": n_trades,
        "n_usd_option_fills": n_options,
        "empirical": {
            "median_premium": _med(included),
            "p25": _q(included, 0.25),
            "p75": _q(included, 0.75),
            "n_estimated": len(included),
            "hourly_median_premium": _med(hourly),
            "n_hourly": len(hourly),
            "outliers": [e for e in estimates if e["outlier"]],
        },
        "per_trade": estimates,
        "warnings": warnings,
    }
