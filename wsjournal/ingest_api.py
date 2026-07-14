"""Ingestion from the unofficial Wealthsimple GraphQL API.

Uses `wealthsimple-python` (https://github.com/henryhuangh/wealthsimple-python),
which wraps the same GraphQL backend the official app uses. Install with:

    pip install wealthsimple-python

IMPORTANT CAVEATS
  - Unofficial: can break without notice when WS changes their backend.
    Everything downstream only depends on the SQLite schema, so if this
    module dies you can fall back to `ingest_csv.py` with official statements.
  - Credentials: on first run this prompts for email/password/2FA and the
    library caches the OAuth tokens in the OS keyring (install `keyring`).
    Non-interactive alternative: set WS_ACCESS_TOKEN + WS_REFRESH_TOKEN (or
    WS_USERNAME + WS_PASSWORD) in the environment. Never hardcode
    credentials; never commit tokens.
  - Be gentle: read-only pulls once or twice a day. Do not poll aggressively.

The activity payload field names below (`securitySymbol`, `filledQuantity`,
etc.) match the library's normalized activity feed at time of writing, but
verify against your own data on first run with --inspect, which dumps the raw
JSON of the first few activities so you can correct the mapping in one place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

from . import db

# Map raw activity -> normalized trade. Single place to fix if WS renames fields.
# Key names verified against the FetchActivityFeedItems fragment in
# wealthsimple_python (activity feed items expose canonicalId, assetSymbol,
# assetQuantity, amount, fees, fxRate — there is no per-share price field;
# normalize_trade derives it from amount / quantity).
FIELD_MAP = {
    "id": ("id", "canonicalId"),
    "account_id": ("accountId",),
    "executed_at": ("occurredAt", "processDate", "createdAt"),
    "symbol": ("securitySymbol", "symbol", "assetSymbol"),
    "description": ("description", "securityName"),
    "quantity": ("filledQuantity", "quantity", "assetQuantity"),
    "price": ("fillPrice", "price", "averagePrice"),
    "currency": ("currency", "marketCurrency"),
    "fee": ("fee", "totalFees", "commission", "fees"),
    "fx_rate": ("fxRate", "exchangeRate"),
}

# Explicit whitelist — substring matching ("BUY" in type) misfiled both
# OPTIONS_BUY and CRYPTO_BUY activities as share trades. Anything not listed
# here stays raw-only.
TRADE_ACTIONS = {"DIY_BUY": "BUY", "DIY_SELL": "SELL",
                 "BUY": "BUY", "SELL": "SELL"}
TRADE_TYPES = {"ORDER_FILLED"}  # action comes from the 'side' field
FX_TYPES = {"FX", "CURRENCY_CONVERSION", "FUNDS_CONVERTED"}
INCOME_TYPES = {"DIVIDEND", "INTEREST", "NON_RESIDENT_TAX", "FEE", "PROMOTION"}


def _pick(payload: dict, keys: tuple) -> object:
    for k in keys:
        if payload.get(k) not in (None, ""):
            return payload[k]
    return None


def _synthetic_id(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:24]


def _is_option(payload: dict) -> bool:
    raw_type = str(payload.get("type") or "").upper()
    return raw_type.startswith("OPTIONS") or payload.get("contractType") not in (None, "")


def normalize_option(payload: dict) -> dict | None:
    """Normalize an OPTIONS_* activity into an option_trades row.

    Only fills (OPTIONS_BUY / OPTIONS_SELL) are handled; expiry, assignment,
    and exercise activity types haven't been observed in the feed yet and are
    kept raw-only with a warning so they surface when they first appear.
    """
    raw_type = str(payload.get("type") or "").upper()
    action = {"OPTIONS_BUY": "BUY", "OPTIONS_SELL": "SELL",
              "OPTIONS_EXPIRY": "EXPIRE"}.get(raw_type)
    # fills must be FILLED; expiry events arrive as COMPLETED
    if action in ("BUY", "SELL") and not _is_filled(payload):
        return None
    contract_type = str(payload.get("contractType") or "").lower()
    if action is None or contract_type not in ("call", "put"):
        print(f"  ! unhandled options activity type={raw_type!r} "
              f"contractType={payload.get('contractType')!r} "
              "(assignment/exercise?) — kept in raw_activities only")
        return None
    try:
        contracts = float(payload.get("assetQuantity"))
        strike = float(payload.get("strikePrice"))
        # expiry events carry no amount — the contract just ceases to exist
        premium = abs(float(payload.get("amount"))) if action != "EXPIRE" else 0.0
    except (TypeError, ValueError):
        print("  ! options activity missing quantity/strike/amount — skipped")
        return None
    return {
        "id": str(payload.get("canonicalId") or _synthetic_id(payload)),
        "account_id": str(payload.get("accountId") or "unknown"),
        "occurred_at": str(payload.get("occurredAt") or ""),
        "underlying": str(payload.get("assetSymbol") or "UNKNOWN").upper(),
        "contract_type": contract_type,
        "strike": strike,
        "expiry": str(payload.get("expiryDate") or ""),
        "action": action,
        "contracts": contracts,
        "premium": premium,
        "currency": str(payload.get("currency") or "CAD").upper(),
        "source": "ws_api",
        "raw_id": None,
    }


def normalize_income(payload: dict) -> dict | None:
    """Normalize DIVIDEND / INTEREST / NON_RESIDENT_TAX / FEE / PROMOTION
    into an income row.  All WS amounts arrive in CAD already; amountSign
    tells us the cash direction (negative for tax and fees)."""
    raw_type = str(payload.get("type") or "").upper()
    if raw_type not in INCOME_TYPES:
        return None
    try:
        amount = abs(float(payload.get("amount") or 0))
    except (TypeError, ValueError):
        return None
    if not amount:
        return None
    sign = -1 if str(payload.get("amountSign") or "positive").lower() == "negative" else 1
    # assetSymbol is a real ticker only for DIVIDEND and NON_RESIDENT_TAX;
    # for INTEREST and FEE it's null or the literal string "CAD".
    raw_sym = payload.get("assetSymbol")
    symbol = str(raw_sym).upper() if raw_type in ("DIVIDEND", "NON_RESIDENT_TAX") and raw_sym else None
    return {
        "id": str(payload.get("canonicalId") or _synthetic_id(payload)),
        "account_id": str(payload.get("accountId") or "unknown"),
        "occurred_at": str(payload.get("occurredAt") or ""),
        "income_type": raw_type,
        "sub_type": payload.get("subType"),
        "symbol": symbol,
        "amount_cad": round(sign * amount, 6),
        "source": "ws_api",
        "raw_id": None,
    }


def _is_filled(payload: dict) -> bool:
    """Only actual fills count — the feed also carries CANCELLED, REJECTED,
    and EXPIRED (unfilled limit) orders under the same activity types."""
    status = str(payload.get("status") or "FILLED").upper()
    return status == "FILLED"


def normalize_trade(payload: dict) -> dict | None:
    if _is_option(payload):
        return None
    if not _is_filled(payload):
        return None
    raw_type = str(payload.get("type") or "").upper()
    action = TRADE_ACTIONS.get(raw_type)
    if not action and raw_type in TRADE_TYPES:
        side = str(payload.get("side") or "").upper()
        action = side if side in ("BUY", "SELL") else None
    if not action:
        return None

    qty = _pick(payload, FIELD_MAP["quantity"])
    if qty is None:
        return None
    qty = float(qty)

    # Prefer `amount` (the total settlement figure) over per-share price
    # fields — price fields (fillPrice, averagePrice) may carry the security's
    # trading currency (e.g. USD) while `amount` is the true settlement total.
    # For older payloads where amount is absent, fall back to price fields.
    amount_raw = payload.get("amount")
    if amount_raw not in (None, ""):
        try:
            gross_amount = abs(float(amount_raw))
            price = gross_amount / qty if qty else None
        except (TypeError, ValueError, ZeroDivisionError):
            gross_amount = price = None
    else:
        gross_amount = price = None

    if price is None:
        price_raw = _pick(payload, FIELD_MAP["price"])
        if price_raw is None:
            return None
        price = float(price_raw)
        gross_amount = qty * price

    currency = str(_pick(payload, FIELD_MAP["currency"]) or "CAD").upper()
    fee = float(_pick(payload, FIELD_MAP["fee"]) or 0)
    fx = _pick(payload, FIELD_MAP["fx_rate"])
    return {
        "id": str(_pick(payload, FIELD_MAP["id"]) or _synthetic_id(payload)),
        "account_id": str(_pick(payload, FIELD_MAP["account_id"]) or "unknown"),
        "executed_at": str(_pick(payload, FIELD_MAP["executed_at"]) or ""),
        "symbol": str(_pick(payload, FIELD_MAP["symbol"]) or "UNKNOWN"),
        "description": _pick(payload, FIELD_MAP["description"]),
        "action": action,
        "quantity": qty,
        "price": price,
        "currency": currency,
        "fx_rate": float(fx) if fx is not None else None,
        "fee": fee,
        "gross_amount": round(gross_amount, 6),
        "source": "ws_api",
        "raw_id": None,
    }


def normalize_fx(payload: dict) -> dict | None:
    raw_type = str(payload.get("type") or payload.get("subType") or "").upper()
    if raw_type not in FX_TYPES and "CONVER" not in raw_type:
        return None
    try:
        from_amt = abs(float(payload.get("fromAmount") or payload.get("amount")))
        to_amt = abs(float(payload.get("toAmount")
                          or payload.get("counterPartyCurrencyAmount") or 0))
    except (TypeError, ValueError):
        return None
    if not (from_amt and to_amt):
        return None
    return {
        "id": str(payload.get("id") or payload.get("canonicalId")
                  or _synthetic_id(payload)),
        "account_id": payload.get("accountId"),
        "occurred_at": str(payload.get("occurredAt") or ""),
        "from_currency": str(payload.get("fromCurrency")
                             or payload.get("currency") or "CAD").upper(),
        "to_currency": str(payload.get("toCurrency")
                           or payload.get("counterPartyCurrency") or "USD").upper(),
        "from_amount": from_amt,
        "to_amount": to_amt,
        "applied_rate": to_amt / from_amt,
        "mid_rate": None,   # enrich later from Bank of Canada valet API
        "fee_cad": None,    # computed once mid_rate is known
        "source": "ws_api",
    }


def _authenticate_interactive(ws) -> None:
    """Prompt for Wealthsimple credentials and 2FA code.

    On success the library stores the OAuth tokens in the OS keyring, so
    subsequent runs (including cron) won't prompt until the refresh token
    expires.
    """
    import getpass

    print("Wealthsimple login (tokens cached in the OS keyring)")
    username = input("  email: ").strip()
    password = getpass.getpass("  password: ")
    try:
        ws.authenticate(username, password)
    except Exception:
        otp = input("  2FA code: ").strip()
        ws.authenticate(username, password, otp=otp)


def _fetch_all_activities(ws, page_size: int = 100, max_pages: int = 50) -> list:
    """Page through the full activity feed.

    The server caps each page (~50 items regardless of the requested limit),
    so a single get_activities call silently truncates history. Follow
    pageInfo.endCursor until exhausted.
    """
    items: list = []
    cursor = None
    for _ in range(max_pages):
        result = ws.get_activities(limit=page_size, cursor=cursor)
        if isinstance(result, dict):
            page = result.get("items", [])
            info = result.get("pageInfo", {})
        else:  # older lib versions returned a bare list
            page, info = result, {}
        items.extend(page)
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    else:
        print(f"  ! stopped after {max_pages} pages "
              f"({len(items)} activities); oldest history may be missing")
    return items


def pull(inspect: bool = False) -> None:
    try:
        from wealthsimple_python import WealthsimpleV2
    except ImportError:
        sys.exit("pip install wealthsimple-python  (see module docstring)")

    ws = WealthsimpleV2()  # picks up cached keyring tokens or WS_* env vars
    try:
        activities = _fetch_all_activities(ws)
    except Exception as e:
        # "Not authenticated" (no cached tokens) or "Please re-authenticate"
        # (refresh token expired) — anything else is a real error.
        if "authenticat" not in str(e).lower():
            raise
        _authenticate_interactive(ws)
        activities = _fetch_all_activities(ws)
    print(f"Fetched {len(activities)} activities.")

    if inspect:
        print(json.dumps(activities[:5], indent=2, default=str))
        return

    now = datetime.now(timezone.utc).isoformat()
    db.init_db()
    n_trades = n_options = n_fx = n_income = 0
    with db.connect() as conn:
        for a in activities:
            payload = a if isinstance(a, dict) else a.__dict__
            rid = str(payload.get("id") or _synthetic_id(payload))
            db.insert_raw(conn, rid, "ws_api", now, payload)
            if _is_option(payload):
                o = normalize_option(payload)
                if o:
                    o["raw_id"] = rid
                    db.upsert_option_trade(conn, o)
                    n_options += 1
                continue
            t = normalize_trade(payload)
            if t:
                t["raw_id"] = rid
                db.upsert_trade(conn, t)
                n_trades += 1
                continue
            fx = normalize_fx(payload)
            if fx:
                conn.execute(
                    """INSERT OR IGNORE INTO fx_events
                       VALUES (:id,:account_id,:occurred_at,:from_currency,
                               :to_currency,:from_amount,:to_amount,
                               :applied_rate,:mid_rate,:fee_cad,:source)""",
                    fx,
                )
                n_fx += 1
                continue
            inc = normalize_income(payload)
            if inc:
                inc["raw_id"] = rid
                db.upsert_income(conn, inc)
                n_income += 1
    print(f"Ingested {n_trades} trades, {n_options} option trades, "
          f"{n_fx} FX events, {n_income} income events.")


def renormalize() -> None:
    """Rebuild trades/option_trades from stored raw_activities.

    Deletes every API-derived row and replays the immutable raw payloads
    through the CURRENT normalizers — so after any normalization fix the
    derived tables match what a fresh pull would produce, without touching
    the API. CSV-sourced rows are untouched.
    """
    db.init_db()
    n_trades = n_options = n_income = 0
    with db.connect() as conn:
        # realized_pnl references trades; it's rebuilt by acb.compute() below
        conn.execute("DELETE FROM realized_pnl")
        conn.execute("DELETE FROM trades WHERE source='ws_api'")
        conn.execute("DELETE FROM option_trades WHERE source='ws_api'")
        conn.execute("DELETE FROM income WHERE source='ws_api'")
        rows = conn.execute(
            "SELECT id, payload FROM raw_activities WHERE source='ws_api'"
        ).fetchall()
        for r in rows:
            payload = json.loads(r["payload"])
            if _is_option(payload):
                o = normalize_option(payload)
                if o:
                    o["raw_id"] = r["id"]
                    db.upsert_option_trade(conn, o)
                    n_options += 1
                continue
            t = normalize_trade(payload)
            if t:
                t["raw_id"] = r["id"]
                db.upsert_trade(conn, t)
                n_trades += 1
                continue
            inc = normalize_income(payload)
            if inc:
                inc["raw_id"] = r["id"]
                db.upsert_income(conn, inc)
                n_income += 1
        from . import acb
        acb.compute(conn)
    print(f"Rebuilt from raw: {n_trades} trades, {n_options} option trades, "
          f"{n_income} income events; realized P&L recomputed.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inspect", action="store_true",
                   help="dump raw JSON of first activities to verify field mapping")
    p.add_argument("--renormalize", action="store_true",
                   help="replay stored raw_activities through the current "
                        "normalizers (no API call)")
    args = p.parse_args()
    if args.renormalize:
        renormalize()
    else:
        pull(inspect=args.inspect)
