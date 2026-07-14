"""Fallback ingestion: official Wealthsimple monthly-statement CSVs.

Wealthsimple lets you bulk-download monthly statements as CSV from
Documents in your web profile. Column names have changed over time and vary
by account type, so this importer uses a tolerant header map. If your file
has different headers, add aliases to HEADER_ALIASES — that's the only place
that needs editing.

Usage:
    python -m wsjournal.ingest_csv path/to/statement.csv [more.csv ...]
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone

import pandas as pd

from . import db

HEADER_ALIASES = {
    "date": {"date", "transaction date", "process date", "settlement date"},
    "type": {"transaction type", "type", "activity", "description type"},
    "symbol": {"symbol", "ticker", "security symbol"},
    "description": {"description", "security name", "details"},
    "quantity": {"quantity", "shares", "units"},
    "price": {"price", "price per share", "unit price"},
    "amount": {"amount", "net amount", "total", "value"},
    "currency": {"currency", "ccy"},
    "fee": {"fee", "fees", "commission"},
    "account": {"account", "account id", "account number"},
}

BUY_WORDS = {"buy", "purchase", "bought"}
SELL_WORDS = {"sell", "sale", "sold"}

# Maps lowercase CSV type substrings → canonical income_type.
# Checked in order; first match wins.
CSV_INCOME_TYPES = [
    ("dividend", "DIVIDEND"),
    ("interest", "INTEREST"),
    ("non-resident tax", "NON_RESIDENT_TAX"),
    ("non resident tax", "NON_RESIDENT_TAX"),
    ("withholding tax", "NON_RESIDENT_TAX"),
    ("management fee", "FEE"),
    ("fee", "FEE"),
]


def _canon(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        for canon, aliases in HEADER_ALIASES.items():
            if key in aliases:
                rename[col] = canon
    return df.rename(columns=rename)


def load_csv(path: str) -> tuple[int, int]:
    """Return (n_trades, n_income) imported from path."""
    df = _canon(pd.read_csv(path))
    required = {"date", "type"}
    if not required.issubset(df.columns):
        print(f"  ! {path}: couldn't find columns {required - set(df.columns)}; "
              f"got {list(df.columns)}. Add aliases in HEADER_ALIASES.")
        return 0, 0

    now = datetime.now(timezone.utc).isoformat()
    n_trades = 0
    n_income = 0
    with db.connect() as conn:
        for _, row in df.iterrows():
            typ = str(row.get("type", "")).strip().lower()
            action = ("BUY" if any(w in typ for w in BUY_WORDS)
                      else "SELL" if any(w in typ for w in SELL_WORDS)
                      else None)
            payload = {k: (None if pd.isna(v) else str(v))
                       for k, v in row.items()}
            rid = hashlib.sha1(repr(sorted(payload.items())).encode()).hexdigest()[:24]

            if action:
                qty = pd.to_numeric(row.get("quantity"), errors="coerce")
                price = pd.to_numeric(row.get("price"), errors="coerce")
                amount = pd.to_numeric(row.get("amount"), errors="coerce")
                if pd.isna(qty):
                    continue
                qty = abs(float(qty))
                if pd.isna(price) and not pd.isna(amount) and qty:
                    price = abs(float(amount)) / qty
                if pd.isna(price):
                    continue
                db.insert_raw(conn, rid, "csv", now, payload)
                db.upsert_trade(conn, {
                    "id": rid,
                    "account_id": str(row.get("account", "csv-import")),
                    "executed_at": str(pd.to_datetime(row["date"]).date()),
                    "symbol": str(row.get("symbol", "UNKNOWN")).strip().upper(),
                    "description": payload.get("description"),
                    "action": action,
                    "quantity": qty,
                    "price": float(price),
                    "currency": str(row.get("currency", "CAD")).strip().upper(),
                    "fx_rate": None,
                    "fee": float(pd.to_numeric(row.get("fee"), errors="coerce") or 0),
                    "gross_amount": round(qty * float(price), 6),
                    "source": "csv",
                    "raw_id": rid,
                })
                n_trades += 1
                continue

            income_type = next(
                (itype for kw, itype in CSV_INCOME_TYPES if kw in typ), None)
            if income_type:
                amount = pd.to_numeric(row.get("amount"), errors="coerce")
                if pd.isna(amount) or amount == 0:
                    continue
                raw_sym = str(row.get("symbol", "")).strip().upper() or None
                db.insert_raw(conn, rid, "csv", now, payload)
                db.upsert_income(conn, {
                    "id": rid,
                    "account_id": str(row.get("account", "csv-import")),
                    "occurred_at": str(pd.to_datetime(row["date"]).date()),
                    "income_type": income_type,
                    "sub_type": None,
                    "symbol": (raw_sym if income_type in
                               ("DIVIDEND", "NON_RESIDENT_TAX") else None),
                    "amount_cad": float(amount),
                    "source": "csv",
                    "raw_id": rid,
                })
                n_income += 1

    return n_trades, n_income


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db.init_db()
    totals = [load_csv(p) for p in sys.argv[1:]]
    n_trades = sum(t for t, _ in totals)
    n_income = sum(i for _, i in totals)
    print(f"Imported {n_trades} trades, {n_income} income events.")
