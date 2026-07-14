"""SQLite layer — single source of truth for the trade journal.

Every other component (ingestion, ACB engine, Streamlit dashboard, MCP server)
reads/writes through this module. Keeping the schema here means the data store
survives even if the unofficial Wealthsimple API breaks and you fall back to
CSV imports.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get(
    "WSJ_DB_PATH", str(Path.home() / ".ws-journal" / "journal.db")
)

SCHEMA = """
-- Raw activity payloads exactly as received (API JSON or CSV row).
-- Never mutated; lets you re-derive trades if normalization logic improves.
CREATE TABLE IF NOT EXISTS raw_activities (
    id          TEXT PRIMARY KEY,          -- source activity id or synthetic hash
    source      TEXT NOT NULL,             -- 'ws_api' | 'csv'
    fetched_at  TEXT NOT NULL,             -- ISO timestamp
    payload     TEXT NOT NULL              -- raw JSON
);

-- Normalized fills/orders. One row per executed trade.
CREATE TABLE IF NOT EXISTS trades (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    executed_at  TEXT NOT NULL,            -- ISO timestamp
    symbol       TEXT NOT NULL,
    description  TEXT,
    action       TEXT NOT NULL CHECK (action IN ('BUY','SELL')),
    quantity     REAL NOT NULL,
    price        REAL NOT NULL,            -- per-share, trade currency
    currency     TEXT NOT NULL,            -- 'CAD' | 'USD'
    fx_rate      REAL,                     -- CAD per 1 unit of trade currency at execution
    fee          REAL NOT NULL DEFAULT 0,  -- explicit commissions/fees, trade currency
    gross_amount REAL NOT NULL,            -- quantity*price (trade currency)
    source       TEXT NOT NULL,
    raw_id       TEXT REFERENCES raw_activities(id)
);

-- Options contracts. One row per option activity; premium is the TOTAL cash
-- amount (already includes the x100 multiplier), trade currency. EXPIRE rows
-- come from OPTIONS_EXPIRY events (premium 0 — the contract ceased to exist).
CREATE TABLE IF NOT EXISTS option_trades (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,            -- ISO timestamp
    underlying    TEXT NOT NULL,            -- e.g. 'NOK'
    contract_type TEXT NOT NULL CHECK (contract_type IN ('call','put')),
    strike        REAL NOT NULL,
    expiry        TEXT NOT NULL,            -- ISO date
    action        TEXT NOT NULL CHECK (action IN ('BUY','SELL','EXPIRE')),
    contracts     REAL NOT NULL,
    premium       REAL NOT NULL,            -- total cash, trade currency
    currency      TEXT NOT NULL,
    source        TEXT NOT NULL,
    raw_id        TEXT REFERENCES raw_activities(id)
);

-- Currency conversions (the hidden 1.5% drag lives here).
CREATE TABLE IF NOT EXISTS fx_events (
    id            TEXT PRIMARY KEY,
    account_id    TEXT,
    occurred_at   TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    from_amount   REAL NOT NULL,
    to_amount     REAL NOT NULL,
    applied_rate  REAL NOT NULL,           -- to_amount / from_amount
    mid_rate      REAL,                    -- Bank of Canada mid-market rate, if known
    fee_cad       REAL,                    -- estimated spread cost in CAD
    source        TEXT NOT NULL
);

-- Free-form journal notes, attachable to a trade or standalone.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_id   TEXT REFERENCES trades(id),
    symbol     TEXT,
    tag        TEXT,                       -- e.g. 'thesis', 'mistake', 'review'
    body       TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT 'me'  -- 'me' | 'agent'
);

-- Daily close prices cached locally so the dashboard works offline.
CREATE TABLE IF NOT EXISTS prices (
    symbol   TEXT NOT NULL,
    date     TEXT NOT NULL,
    close    REAL NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- Which Yahoo listing each journal symbol maps to. The WS feed reports bare
-- tickers that collide across exchanges (CDRs on Cboe Canada share tickers
-- with NASDAQ/NYSE stocks); resolution is persisted here, not re-probed.
CREATE TABLE IF NOT EXISTS symbol_listings (
    symbol         TEXT PRIMARY KEY,     -- journal symbol (WS assetSymbol)
    yahoo_symbol   TEXT NOT NULL,        -- resolved listing, e.g. 'AVGO.NE'
    quote_currency TEXT NOT NULL,
    method         TEXT NOT NULL,        -- 'override'|'price_fit'|'only_candidate'|'fallback'
    score          REAL,                 -- median |ln(trade_px/expected_px)|; lower = better
    resolved_at    TEXT NOT NULL
);

-- Hourly closes for the recent window (UTC bar-start timestamps) so the
-- dashboard can chart intraday context; daily closes stay in `prices`.
CREATE TABLE IF NOT EXISTS prices_intraday (
    symbol   TEXT NOT NULL,
    ts       TEXT NOT NULL,                -- ISO UTC timestamp
    close    REAL NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (symbol, ts)
);

-- Realized P&L lots produced by the ACB engine (rebuilt on each run).
CREATE TABLE IF NOT EXISTS realized_pnl (
    trade_id      TEXT PRIMARY KEY REFERENCES trades(id),
    symbol        TEXT NOT NULL,
    sold_at       TEXT NOT NULL,
    quantity      REAL NOT NULL,
    proceeds_cad  REAL NOT NULL,
    acb_cad       REAL NOT NULL,
    gain_cad      REAL NOT NULL
);

-- Income events: dividends, interest, withholding tax, management fees.
-- amount_cad is signed: positive = inflow, negative = outflow (tax/fee).
-- All amounts are in CAD (WS converts at point of transaction).
CREATE TABLE IF NOT EXISTS income (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,      -- ISO timestamp
    income_type   TEXT NOT NULL,      -- DIVIDEND | INTEREST | NON_RESIDENT_TAX | FEE | PROMOTION
    sub_type      TEXT,               -- CASH_DIVIDEND | FPL_INTEREST | MANAGEMENT_FEE | etc.
    symbol        TEXT,               -- stock ticker; NULL for interest/fees
    amount_cad    REAL NOT NULL,      -- signed: positive=inflow, negative=outflow
    source        TEXT NOT NULL,
    raw_id        TEXT REFERENCES raw_activities(id)
);

-- Account metadata fetched from WS API (populated by --sync-accounts).
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,   -- e.g. 'tfsa-mq8XRCIVBw'
    nickname     TEXT,               -- user-facing label from WS
    account_type TEXT,               -- 'TFSA' | 'NON_REGISTERED' | 'CASH' | etc.
    currency     TEXT,
    fetched_at   TEXT NOT NULL
);

-- Live positions snapshot fetched from WS API (populated by --sync-accounts).
-- Includes transferred shares that have no BUY in the activity feed.
CREATE TABLE IF NOT EXISTS positions_live (
    account_id       TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    security_id      TEXT,
    quantity         REAL NOT NULL,
    book_value_cad   REAL,           -- WS's cost basis (includes transferred shares)
    market_value_cad REAL,
    average_price    REAL,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, executed_at);
CREATE INDEX IF NOT EXISTS idx_notes_symbol ON notes(symbol);
CREATE INDEX IF NOT EXISTS idx_options_underlying ON option_trades(underlying, expiry);
CREATE INDEX IF NOT EXISTS idx_income_symbol ON income(symbol, occurred_at);
"""


def get_db_path() -> str:
    return os.environ.get("WSJ_DB_PATH", DEFAULT_DB_PATH)


@contextmanager
def connect(db_path: str | None = None):
    path = db_path or get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    # executescript() issues an implicit COMMIT before running, which can
    # conflict with Python's sqlite3 transaction management and cause DDL
    # to not commit on some Python versions. Strip SQL comments (which may
    # contain semicolons) then execute each statement individually.
    schema_stripped = re.sub(r"--[^\n]*", "", SCHEMA)
    with connect(db_path) as conn:
        for stmt in (s.strip() for s in schema_stripped.split(";")):
            if stmt:
                conn.execute(stmt)


def upsert_trade(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute(
        """INSERT INTO trades (id, account_id, executed_at, symbol, description,
                               action, quantity, price, currency, fx_rate, fee,
                               gross_amount, source, raw_id)
           VALUES (:id,:account_id,:executed_at,:symbol,:description,:action,
                   :quantity,:price,:currency,:fx_rate,:fee,:gross_amount,
                   :source,:raw_id)
           ON CONFLICT(id) DO UPDATE SET
               fx_rate=excluded.fx_rate, fee=excluded.fee,
               description=excluded.description""",
        t,
    )


def upsert_option_trade(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute(
        """INSERT INTO option_trades (id, account_id, occurred_at, underlying,
                                      contract_type, strike, expiry, action,
                                      contracts, premium, currency, source, raw_id)
           VALUES (:id,:account_id,:occurred_at,:underlying,:contract_type,
                   :strike,:expiry,:action,:contracts,:premium,:currency,
                   :source,:raw_id)
           ON CONFLICT(id) DO UPDATE SET
               premium=excluded.premium, contracts=excluded.contracts""",
        t,
    )


def upsert_account(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO accounts (id, nickname, account_type, currency, fetched_at)
           VALUES (:id,:nickname,:account_type,:currency,:fetched_at)
           ON CONFLICT(id) DO UPDATE SET
               nickname=excluded.nickname, account_type=excluded.account_type,
               fetched_at=excluded.fetched_at""",
        row,
    )


def upsert_position_live(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO positions_live
               (account_id, symbol, security_id, quantity,
                book_value_cad, market_value_cad, average_price, fetched_at)
           VALUES (:account_id,:symbol,:security_id,:quantity,
                   :book_value_cad,:market_value_cad,:average_price,:fetched_at)
           ON CONFLICT(account_id, symbol) DO UPDATE SET
               quantity=excluded.quantity, book_value_cad=excluded.book_value_cad,
               market_value_cad=excluded.market_value_cad,
               average_price=excluded.average_price,
               fetched_at=excluded.fetched_at""",
        row,
    )


def upsert_income(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO income (id, account_id, occurred_at, income_type, sub_type,
                               symbol, amount_cad, source, raw_id)
           VALUES (:id,:account_id,:occurred_at,:income_type,:sub_type,
                   :symbol,:amount_cad,:source,:raw_id)
           ON CONFLICT(id) DO UPDATE SET amount_cad=excluded.amount_cad""",
        row,
    )


def insert_raw(conn: sqlite3.Connection, rid: str, source: str,
               fetched_at: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO raw_activities (id, source, fetched_at, payload) "
        "VALUES (?,?,?,?)",
        (rid, source, fetched_at, json.dumps(payload)),
    )
