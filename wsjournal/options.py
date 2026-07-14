"""Options P&L engine.

Groups option fills by contract identity (underlying, type, strike, expiry)
and nets premium cash flow per contract:

  - SELL adds premium received; BUY subtracts premium paid
  - EXPIRE (from OPTIONS_EXPIRY events) zeroes remaining contracts at no
    cash — the feed confirmed a worthless expiry
  - net contracts: + long / - short
  - closed  : net contracts == 0 -> net premium is realized
  - open    : net contracts != 0 and expiry >= today -> net premium so far
  - expired : net contracts != 0 and expiry < today WITHOUT an expiry event
              -> remaining premium treated as realized, flagged for review

Premiums in the WS activity feed are TOTAL cash amounts in the account
currency (CAD), already including the x100 contract multiplier, so no
per-share math happens here.

Known limitation: assignment and exercise activities haven't been observed
in the feed yet and are not modeled — an assigned short option will look
like a worthless expiry until the resulting share trade is reconciled
manually. ingest_api warns when an unknown OPTIONS_* activity appears.
"""

from __future__ import annotations

import sqlite3
from datetime import date


def compute(conn: sqlite3.Connection) -> dict:
    """Net premium per contract; return contract rows + summary totals."""
    rows = conn.execute(
        "SELECT * FROM option_trades "
        "ORDER BY underlying, expiry, occurred_at, id"
    ).fetchall()

    contracts: dict[tuple, dict] = {}
    for r in rows:
        key = (r["underlying"], r["contract_type"], r["strike"], r["expiry"])
        c = contracts.setdefault(key, {
            "underlying": r["underlying"],
            "contract_type": r["contract_type"],
            "strike": r["strike"],
            "expiry": r["expiry"],
            "net_contracts": 0.0,
            "net_premium_cad": 0.0,
            "fills": 0,
            "warnings": [],
        })
        if r["currency"] != "CAD" and "non-CAD premium" not in str(c["warnings"]):
            c["warnings"].append(
                f"non-CAD premium ({r['currency']}) treated as CAD — "
                "fx conversion not available for options yet"
            )
        if r["action"] == "EXPIRE":
            # confirmed worthless expiry: remaining contracts cease to exist
            if abs(c["net_contracts"]) > 1e-9:
                step = min(r["contracts"], abs(c["net_contracts"]))
                c["net_contracts"] -= step * (1 if c["net_contracts"] > 0 else -1)
            c["fills"] += 1
            continue
        sign = 1 if r["action"] == "BUY" else -1
        c["net_contracts"] += sign * r["contracts"]
        c["net_premium_cad"] += -sign * r["premium"]  # BUY pays, SELL receives
        c["fills"] += 1

    today = date.today().isoformat()
    out, realized_cad, open_premium_cad = [], 0.0, 0.0
    for c in contracts.values():
        if abs(c["net_contracts"]) < 1e-9:
            c["status"] = "closed"
            realized_cad += c["net_premium_cad"]
        elif str(c["expiry"]) < today:
            c["status"] = "expired"
            c["warnings"].append(
                "expired with open contracts; premium realized assuming "
                "worthless expiry — verify no assignment/exercise occurred"
            )
            realized_cad += c["net_premium_cad"]
        else:
            c["status"] = "open"
            open_premium_cad += c["net_premium_cad"]
        c["net_contracts"] = round(c["net_contracts"], 6)
        c["net_premium_cad"] = round(c["net_premium_cad"], 2)
        out.append(c)

    return {
        "contracts": out,
        "summary": {
            "realized_premium_cad": round(realized_cad, 2),
            "open_contracts": [c for c in out if c["status"] == "open"],
            "open_net_premium_cad": round(open_premium_cad, 2),
        },
    }
