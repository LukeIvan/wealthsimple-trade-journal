"""ACB (Adjusted Cost Base) engine.

Canada requires average cost basis per identical property, computed in CAD.
This module replays all trades chronologically per symbol and produces:

  - realized gains per SELL (proceeds - ACB of shares sold), in CAD
  - the current open position with its running ACB
  - total explicit fees and estimated FX conversion drag

Conventions:
  - BUY:  ACB += gross_amount_cad + fee_cad
  - SELL: proceeds = gross_amount_cad - fee_cad
          gain = proceeds - acb_per_share * qty; ACB reduced proportionally
  - USD trades are converted at the trade's fx_rate (CAD per USD). If missing,
    we fall back to the most recent known rate, flagging the trade.

Note: this engine does NOT implement the superficial loss rule (repurchase
within 30 days). Realized losses are flagged as candidates so you can verify
manually or extend `apply_superficial_loss` later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    acb_cad: float = 0.0          # total CAD cost base of open shares
    last_fx_rate: float = 1.0
    warnings: list[str] = field(default_factory=list)

    @property
    def acb_per_share(self) -> float:
        return self.acb_cad / self.quantity if self.quantity else 0.0


def _to_cad(amount: float, currency: str, fx_rate: float | None,
            pos: Position) -> float:
    if currency == "CAD":
        return amount
    if fx_rate:
        pos.last_fx_rate = fx_rate
        return amount * fx_rate
    pos.warnings.append(
        f"missing fx_rate; used last known {pos.last_fx_rate:.4f}"
    )
    return amount * pos.last_fx_rate


def compute(conn: sqlite3.Connection) -> dict:
    """Replay trades; persist realized lots; return positions + summary."""
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY symbol, executed_at, id"
    ).fetchall()

    # Seed the FX fallback from the cached USDCAD=X rate so that USD trades
    # with null fx_rate convert at a real rate rather than the 1.0 default.
    # This handles payloads where WS reports amount in USD (not CAD) and
    # omits fxRate — seen in DIY_BUY/SELL activities from ~2026-07.
    _seed_fx = _usdcad(conn) or 1.0

    positions: dict[str, Position] = {}
    realized: list[dict] = []
    total_fees_cad = 0.0

    for r in rows:
        pos = positions.setdefault(
            r["symbol"], Position(r["symbol"], last_fx_rate=_seed_fx))
        fee_cad = _to_cad(r["fee"], r["currency"], r["fx_rate"], pos)
        gross_cad = _to_cad(r["gross_amount"], r["currency"], r["fx_rate"], pos)
        total_fees_cad += fee_cad

        if r["action"] == "BUY":
            pos.quantity += r["quantity"]
            pos.acb_cad += gross_cad + fee_cad
        else:  # SELL
            qty = min(r["quantity"], pos.quantity) if pos.quantity else r["quantity"]
            if r["quantity"] > pos.quantity + 1e-9:
                pos.warnings.append(
                    f"sell of {r['quantity']} exceeds tracked position "
                    f"{pos.quantity} on {r['executed_at']} (missing history?)"
                )
            acb_sold = pos.acb_per_share * qty
            proceeds = gross_cad - fee_cad
            gain = proceeds - acb_sold
            pos.quantity = max(pos.quantity - qty, 0.0)
            pos.acb_cad = max(pos.acb_cad - acb_sold, 0.0)
            realized.append({
                "trade_id": r["id"], "symbol": r["symbol"],
                "sold_at": r["executed_at"], "quantity": qty,
                "proceeds_cad": round(proceeds, 2),
                "acb_cad": round(acb_sold, 2),
                "gain_cad": round(gain, 2),
            })

    realized = check_superficial_loss(rows, realized)

    conn.execute("DELETE FROM realized_pnl")
    conn.executemany(
        """INSERT INTO realized_pnl (trade_id, symbol, sold_at, quantity,
                                     proceeds_cad, acb_cad, gain_cad)
           VALUES (:trade_id,:symbol,:sold_at,:quantity,:proceeds_cad,
                   :acb_cad,:gain_cad)""",
        realized,
    )

    fx_fee = conn.execute(
        "SELECT COALESCE(SUM(fee_cad),0) AS f FROM fx_events"
    ).fetchone()["f"]

    open_positions = [
        {
            "symbol": p.symbol,
            "quantity": round(p.quantity, 6),
            "acb_cad": round(p.acb_cad, 2),
            "acb_per_share_cad": round(p.acb_per_share, 4),
            "warnings": p.warnings,
        }
        for p in positions.values() if p.quantity > 1e-9
    ]

    loss_lots = [x for x in realized if x["gain_cad"] < 0]
    return {
        "open_positions": open_positions,
        "realized": realized,
        "summary": {
            "realized_gain_cad": round(sum(x["gain_cad"] for x in realized), 2),
            "explicit_fees_cad": round(total_fees_cad, 2),
            "fx_conversion_cost_cad": round(fx_fee, 2),
            "loss_lots_check_superficial_rule": loss_lots,
            "n_superficial_losses": sum(1 for x in loss_lots if x.get("is_superficial")),
        },
    }


def check_superficial_loss(all_trades: list, realized: list[dict]) -> list[dict]:
    """Detect superficial losses (±30-day repurchase rule, ITA s.54).

    For each realized loss lot, checks whether any BUY of the same symbol
    occurred within [sold_at − 30d, sold_at + 30d] AND the position is
    still non-zero 30 days after the sale. If so, marks the lot superficial.

    Does NOT adjust ACB — call apply_superficial_loss() for that (not yet
    implemented). Adds keys to every lot:
      is_superficial (bool), disallowed_cad (float),
      repurchase_date (str|None), repurchase_qty (float)
    """
    by_symbol: dict[str, list] = {}
    for r in all_trades:
        by_symbol.setdefault(r["symbol"], []).append(r)

    out = []
    for lot in realized:
        lot = dict(lot)
        if lot["gain_cad"] >= 0:
            lot.update(is_superficial=False, disallowed_cad=0.0,
                       repurchase_date=None, repurchase_qty=0.0)
            out.append(lot)
            continue

        sold_d = date.fromisoformat(str(lot["sold_at"])[:10])
        window_start = (sold_d - timedelta(days=30)).isoformat()
        window_end = (sold_d + timedelta(days=30)).isoformat()

        sym_trades = by_symbol.get(lot["symbol"], [])
        buys_in_window = [
            r for r in sym_trades
            if r["action"] == "BUY"
            and window_start <= str(r["executed_at"])[:10] <= window_end
        ]

        if not buys_in_window:
            lot.update(is_superficial=False, disallowed_cad=0.0,
                       repurchase_date=None, repurchase_qty=0.0)
            out.append(lot)
            continue

        qty_at_cutoff = 0.0
        for r in sym_trades:
            if str(r["executed_at"])[:10] > window_end:
                break
            if r["action"] == "BUY":
                qty_at_cutoff += r["quantity"]
            else:
                qty_at_cutoff = max(qty_at_cutoff - r["quantity"], 0.0)

        if qty_at_cutoff > 1e-9:
            earliest = min(buys_in_window, key=lambda r: str(r["executed_at"]))
            lot.update(
                is_superficial=True,
                disallowed_cad=abs(lot["gain_cad"]),
                repurchase_date=str(earliest["executed_at"])[:10],
                repurchase_qty=round(sum(r["quantity"] for r in buys_in_window), 6),
            )
        else:
            lot.update(is_superficial=False, disallowed_cad=0.0,
                       repurchase_date=None, repurchase_qty=0.0)
        out.append(lot)
    return out


def apply_superficial_loss(realized: list[dict], all_trades: list) -> list[dict]:
    """Apply ACB adjustment for superficial losses (NOT YET IMPLEMENTED).

    When a loss is superficial, the disallowed amount must be added to the
    ACB of the re-acquired shares, reducing the future gain on their eventual
    sale. Requires a second pass through the ACB engine so adjusted ACBs
    cascade forward. Stub retained for future extension.
    """
    return realized  # no-op until implemented


def trade_ledger(conn: sqlite3.Connection) -> list[dict]:
    """Per-trade mark-to-market ledger. Mirrors compute()'s ACB math exactly
    (fees into ACB on buys, proceeds net of fees on sells, fx fallback) so
    realized figures always agree with realized_pnl.

    One row per fill, answering "how did each trade affect my P&L":
      - book_cost_cad / acb_per_share_cad: total ACB of the open position
        after this fill and its per-share average price (book/qty) — the
        running cost basis, updating with every purchase and sale
      - realized_cad: gain this sell locked in (0 on buys)
      - unrealized_cad: remaining position marked AT THIS FILL'S OWN PRICE —
        every fill is a ground-truth mark of the security at that instant,
        so no price-history lookup is needed
      - total_pnl_cad: cumulative realized + unrealized for the symbol
      - mtm_delta_cad: change in total since the symbol's previous trade.
        A trade itself never moves total P&L at execution (it reclassifies
        unrealized<->realized or adds exposure at zero gain), so this delta
        is pure market movement on what was held between decisions.
      - flat_move_cad: only on a buy that re-opens a flat position — the
        per-share move since the flattening exit x shares exited. Positive
        = the move missed while flat (the exit cost you); negative =
        drawdown avoided. The blind spot of mtm_delta_cad, made explicit.
    """
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY symbol, executed_at, id"
    ).fetchall()

    _seed_fx = _usdcad(conn) or 1.0
    out: list[dict] = []
    positions: dict[str, Position] = {}
    sym_state: dict[str, dict] = {}

    for r in rows:
        pos = positions.setdefault(
            r["symbol"], Position(r["symbol"], last_fx_rate=_seed_fx))
        s = sym_state.setdefault(r["symbol"], {
            "realized_cum": 0.0, "prev_total": None,
            "exit_price": None, "exit_qty": 0.0,
        })
        fee_cad = _to_cad(r["fee"], r["currency"], r["fx_rate"], pos)
        gross_cad = _to_cad(r["gross_amount"], r["currency"], r["fx_rate"], pos)
        fill_ps = gross_cad / r["quantity"] if r["quantity"] else 0.0

        realized, flat_move = 0.0, None
        if r["action"] == "BUY":
            if pos.quantity <= 1e-9 and s["exit_price"] is not None:
                flat_move = (fill_ps - s["exit_price"]) * s["exit_qty"]
            pos.quantity += r["quantity"]
            pos.acb_cad += gross_cad + fee_cad
        else:  # SELL
            qty = min(r["quantity"], pos.quantity) if pos.quantity else r["quantity"]
            acb_sold = pos.acb_per_share * qty
            realized = (gross_cad - fee_cad) - acb_sold
            s["realized_cum"] += realized
            pos.quantity = max(pos.quantity - qty, 0.0)
            pos.acb_cad = max(pos.acb_cad - acb_sold, 0.0)
            if pos.quantity <= 1e-9:
                s["exit_price"], s["exit_qty"] = fill_ps, qty

        unreal = (pos.quantity * (fill_ps - pos.acb_per_share)
                  if pos.quantity > 1e-9 else 0.0)
        total = s["realized_cum"] + unreal
        mtm = total - s["prev_total"] if s["prev_total"] is not None else 0.0
        s["prev_total"] = total

        out.append({
            "trade_id": r["id"], "symbol": r["symbol"],
            "executed_at": r["executed_at"], "action": r["action"],
            "quantity": r["quantity"], "price": r["price"],
            "position_qty": round(pos.quantity, 6),
            "book_cost_cad": round(pos.acb_cad, 2),
            "acb_per_share_cad": round(pos.acb_per_share, 4),
            "realized_cad": round(realized, 2),
            "unrealized_cad": round(unreal, 2),
            "total_pnl_cad": round(total, 2),
            "mtm_delta_cad": round(mtm, 2),
            "flat_move_cad": (round(flat_move, 2)
                              if flat_move is not None else None),
        })
    return out


def _usdcad(conn: sqlite3.Connection) -> float | None:
    """Latest cached USD/CAD rate (cached by prices.refresh as 'USDCAD=X')."""
    row = conn.execute(
        "SELECT close FROM prices WHERE symbol='USDCAD=X' "
        "ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return row["close"] if row else None


def unrealized(conn: sqlite3.Connection, positions: list[dict]) -> list[dict]:
    """Mark open positions to latest cached price (CAD-converted)."""
    out = []
    for p in positions:
        row = conn.execute(
            "SELECT close, currency, date FROM prices WHERE symbol=? "
            "ORDER BY date DESC LIMIT 1", (p["symbol"],)
        ).fetchone()
        entry = dict(p)
        if row:
            if row["currency"] == "CAD":
                rate = 1.0
            else:
                fxr = conn.execute(
                    "SELECT fx_rate FROM trades WHERE symbol=? AND fx_rate IS NOT NULL "
                    "ORDER BY executed_at DESC LIMIT 1", (p["symbol"],)
                ).fetchone()
                rate = fxr["fx_rate"] if fxr else _usdcad(conn)
                if rate is None:
                    rate = 1.0
                    entry.setdefault("warnings", []).append(
                        f"no fx_rate and no cached USDCAD=X; treated "
                        f"{row['currency']} price as CAD — run prices.refresh"
                    )
            mv = row["close"] * p["quantity"] * rate
            entry.update({
                "last_price": row["close"], "price_date": row["date"],
                "market_value_cad": round(mv, 2),
                "unrealized_gain_cad": round(mv - p["acb_cad"], 2),
            })
        else:
            entry["market_value_cad"] = None
            entry["note"] = "no cached price; run prices.refresh or add manually"
        out.append(entry)
    return out
