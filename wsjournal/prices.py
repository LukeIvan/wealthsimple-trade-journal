"""Local price cache.

`refresh()` pulls daily closes from Yahoo's chart endpoint for every symbol
you hold or have traded (shares and option underlyings), so the dashboard's
performance section can plot your entries/exits against the actual price
series — and still works offline from cache.

Listing resolution is price-fit based: the WS feed reports bare tickers that
collide across exchanges — CDRs on Cboe Canada (AVGO.NE, INTC.NE, ...) share
tickers with NASDAQ/NYSE stocks, and the trade currency says nothing (it's
always the account currency, CAD). The journal's own fills are ground truth:
every candidate listing (bare US, .TO, .V, .CN, .NE) is scored by how well
its same-day closes explain the symbol's actual trade prices, and the best
fit wins. Resolutions persist in symbol_listings (inspect/redo with
`python -m wsjournal.prices resolve`); SYMBOL_OVERRIDES pins a symbol
manually and always wins. The currency stored alongside each close is the
listing's quote currency, NOT the trade currency.

Each refresh also caches the USD/CAD rate (symbol 'USDCAD=X') so unrealized
P&L can convert USD quotes when trades carry no fx_rate.

Manual fallback (no internet / delisted symbol):
    python -m wsjournal.prices set SHOP 2026-06-09 105.32 USD
"""

from __future__ import annotations

import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

from . import db

SYMBOL_OVERRIDES: dict[str, str] = {
    # For tickers that exist on multiple exchanges the probe picks the US
    # listing first — pin colliding symbols here. Diagnostic: a trade price
    # wildly off the cached close (see README) means a wrong listing.
    "MU": "MU.TO",  # TSX listing, NOT Micron (NASDAQ)
}

FX_SYMBOL = "USDCAD=X"  # CAD per 1 USD


def _candidates(symbol: str) -> list[str]:
    if symbol in SYMBOL_OVERRIDES:
        return [SYMBOL_OVERRIDES[symbol]]
    base = symbol.replace(".", "-")  # PGI.UN -> PGI-UN
    # bare US listing first, then Canadian exchanges: TSX, TSX-V, CSE, NEO
    return [symbol] + [f"{base}{sfx}" for sfx in (".TO", ".V", ".CN", ".NE")]


def _chart_closes(ysym: str, start: date,
                  interval: str = "1d") -> tuple[list[tuple[str, float]], str] | None:
    """Closes from Yahoo's chart endpoint (no cookie/crumb needed — also
    sidesteps DNS-blocked fc.yahoo.com that breaks yfinance).

    interval '1d' labels bars with ISO dates; intraday intervals ('60m', …)
    label with full ISO UTC timestamps. Yahoo serves hourly bars for roughly
    the last 730 days only.

    Returns ([(label, close), ...], quote_currency) or None on failure.
    """
    try:
        from curl_cffi import requests as creq
    except ImportError:
        sys.exit("pip install curl_cffi")

    try:
        r = creq.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
            params={
                "period1": int(time.mktime(start.timetuple())),
                "period2": int(time.time()),
                "interval": interval,
            },
            impersonate="chrome",
            timeout=15,
        )
        result = r.json()["chart"]["result"][0]
        currency = str(result["meta"].get("currency") or "").upper()
        closes = result["indicators"]["quote"][0]["close"]
        stamps = result["timestamp"]
    except Exception:
        return None
    if interval == "1d":
        def label(t): return date.fromtimestamp(t).isoformat()
    else:
        def label(t): return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
    pairs = [(label(t), float(c)) for t, c in zip(stamps, closes) if c is not None]
    return (pairs, currency) if pairs else None


def _journal_symbols(conn) -> list:
    """Symbols to price: share trades plus option underlyings."""
    return conn.execute(
        "SELECT symbol, MIN(first) AS first FROM ("
        "  SELECT symbol, MIN(executed_at) AS first FROM trades GROUP BY symbol"
        "  UNION ALL"
        "  SELECT underlying AS symbol, MIN(occurred_at) AS first"
        "    FROM option_trades GROUP BY underlying"
        ") GROUP BY symbol"
    ).fetchall()


def _resolve_symbol(conn, symbol: str, start: date):
    """Pick the Yahoo listing for a journal symbol.

    Fetches every candidate and scores each by how well its same-day closes
    explain the symbol's actual trade prices (expected CAD price = close x
    USDCAD for USD listings, close itself for CAD listings). The fills are
    ground truth — a CDR bought at 15 CAD can't be the 1,300-USD US listing.

    Returns (yahoo_symbol, currency, method, score, daily_pairs) or None.
    """
    if symbol in SYMBOL_OVERRIDES:
        ysym = SYMBOL_OVERRIDES[symbol]
        got = _chart_closes(ysym, start)
        if not got:
            print(f"  ! override {ysym} for {symbol} returned no data")
            return None
        return ysym, got[1], "override", None, got[0]

    with_data = []
    for ysym in _candidates(symbol):
        got = _chart_closes(ysym, start)
        if got:
            with_data.append((ysym, got[1], got[0]))
    if not with_data:
        return None
    if len(with_data) == 1:
        ysym, ccy, pairs = with_data[0]
        return ysym, ccy, "only_candidate", None, pairs

    trades = conn.execute(
        "SELECT substr(executed_at,1,10) AS day, price FROM trades "
        "WHERE symbol=?", (symbol,)).fetchall()
    fx = {r["date"]: r["close"] for r in conn.execute(
        "SELECT date, close FROM prices WHERE symbol=?", (FX_SYMBOL,))}

    best = None
    for ysym, ccy, pairs in with_data:
        closes = dict(pairs)
        errs = []
        for t in trades:
            close = closes.get(t["day"])
            conv = fx.get(t["day"]) if ccy == "USD" else 1.0
            if not close or not conv or t["price"] <= 0:
                continue
            errs.append(abs(math.log(t["price"] / (close * conv))))
        if not errs:
            continue
        errs.sort()
        score = errs[len(errs) // 2]
        if best is None or score < best[3]:
            best = (ysym, ccy, pairs, score)

    if best is None:
        ysym, ccy, pairs = with_data[0]
        print(f"  ! {symbol}: no trades matchable to candidate closes; "
              f"falling back to {ysym}")
        return ysym, ccy, "fallback", None, pairs

    ysym, ccy, pairs, score = best
    if score > 0.25:
        print(f"  ! {symbol}: best listing {ysym} fits trades poorly "
              f"(score {score:.2f}) — splits? Pin in SYMBOL_OVERRIDES if wrong")
    return ysym, ccy, "price_fit", round(score, 4), pairs


def _store_listing(conn, symbol: str, ysym: str, ccy: str,
                   method: str, score) -> None:
    prev = conn.execute(
        "SELECT yahoo_symbol FROM symbol_listings WHERE symbol=?", (symbol,)
    ).fetchone()
    if prev is None or prev["yahoo_symbol"] != ysym:
        # first resolution or listing changed: cached rows may belong to the
        # wrong listing — purge so they get rebuilt from the right one
        conn.execute("DELETE FROM prices WHERE symbol=?", (symbol,))
        conn.execute("DELETE FROM prices_intraday WHERE symbol=?", (symbol,))
    conn.execute(
        "INSERT OR REPLACE INTO symbol_listings VALUES (?,?,?,?,?,?)",
        (symbol, ysym, ccy, method, score,
         datetime.now(timezone.utc).isoformat()))


def refresh(days: int = 400) -> None:
    db.init_db()
    default_start = date.today() - timedelta(days=days)
    with db.connect() as conn:
        symbols = _journal_symbols(conn)

        # FX first: it must reach the earliest trade (fxdrag matches every
        # trade to a same-day mid), and listing resolution scores USD
        # candidates against it
        fx_start = default_start
        firsts = [date.fromisoformat(str(s["first"])[:10]) for s in symbols]
        if firsts:
            fx_start = min(fx_start, min(firsts))
        fetched = _chart_closes(FX_SYMBOL, fx_start)
        if fetched:
            pairs, _ = fetched
            conn.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                [(FX_SYMBOL, d, close, "CAD") for d, close in pairs],
            )
            print(f"  cached {len(pairs)} USD/CAD rates ({FX_SYMBOL})")
        else:
            print(f"  ! could not refresh {FX_SYMBOL}")

        for s in symbols:
            start = max(date.fromisoformat(str(s["first"])[:10]), default_start)
            listing = conn.execute(
                "SELECT yahoo_symbol FROM symbol_listings WHERE symbol=?",
                (s["symbol"],)).fetchone()
            if listing:
                ysym = listing["yahoo_symbol"]
                got = _chart_closes(ysym, start)
                if not got:
                    print(f"  ! no data for {s['symbol']} ({ysym})")
                    continue
                pairs, ccy = got
            else:
                resolved = _resolve_symbol(conn, s["symbol"], start)
                if not resolved:
                    print(f"  ! no data for {s['symbol']} "
                          f"(tried {', '.join(_candidates(s['symbol']))}); "
                          "add a SYMBOL_OVERRIDES entry?")
                    continue
                ysym, ccy, method, score, pairs = resolved
                _store_listing(conn, s["symbol"], ysym, ccy, method, score)
            conn.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                [(s["symbol"], d, close, ccy) for d, close in pairs],
            )
            print(f"  cached {len(pairs)} closes for {s['symbol']} "
                  f"({ysym}, {ccy})")


def resolve_all(days: int = 400) -> None:
    """Wipe stored listings, re-resolve every symbol, print the table."""
    db.init_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM symbol_listings")
    refresh(days)
    with db.connect() as conn:
        print("\nresolved listings:")
        for r in conn.execute("SELECT * FROM symbol_listings ORDER BY symbol"):
            score = f"{r['score']:.3f}" if r["score"] is not None else "—"
            print(f"  {r['symbol']:<8} → {r['yahoo_symbol']:<11} "
                  f"{r['quote_currency']}  [{r['method']}, score {score}]")


def refresh_intraday(days: int = 14) -> None:
    """Cache hourly closes for the recent window (dashboard granularity)."""
    db.init_db()
    start = date.today() - timedelta(days=days)
    with db.connect() as conn:
        for s in _journal_symbols(conn):
            listing = conn.execute(
                "SELECT yahoo_symbol FROM symbol_listings WHERE symbol=?",
                (s["symbol"],)).fetchone()
            if not listing:
                continue  # resolved on the next daily refresh
            fetched = _chart_closes(listing["yahoo_symbol"], start,
                                    interval="60m")
            if fetched:
                bars, currency = fetched
                conn.executemany(
                    "INSERT OR REPLACE INTO prices_intraday VALUES (?,?,?,?)",
                    [(s["symbol"], ts, close, currency) for ts, close in bars],
                )
                print(f"  cached {len(bars)} hourly bars for {s['symbol']} "
                      f"({listing['yahoo_symbol']})")
            else:
                print(f"  ! no hourly data for {s['symbol']} "
                      f"({listing['yahoo_symbol']})")
        # hourly mid rates let fxdrag match recent trades precisely
        fetched = _chart_closes(FX_SYMBOL, start, interval="60m")
        if fetched:
            bars, _ = fetched
            conn.executemany(
                "INSERT OR REPLACE INTO prices_intraday VALUES (?,?,?,?)",
                [(FX_SYMBOL, ts, close, "CAD") for ts, close in bars],
            )
            print(f"  cached {len(bars)} hourly USD/CAD rates ({FX_SYMBOL})")
        # prune bars older than the viewing window so the table stays small
        cutoff = datetime.now(timezone.utc) - timedelta(days=days + 7)
        conn.execute("DELETE FROM prices_intraday WHERE ts < ?",
                     (cutoff.isoformat(),))


def set_price(symbol: str, day: str, close: float, currency: str) -> None:
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                     (symbol.upper(), day, close, currency.upper()))
    print(f"set {symbol} {day} = {close} {currency}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "set":
        set_price(sys.argv[2], sys.argv[3], float(sys.argv[4]),
                  sys.argv[5] if len(sys.argv) > 5 else "CAD")
    elif len(sys.argv) >= 2 and sys.argv[1] == "intraday":
        refresh_intraday(int(sys.argv[2]) if len(sys.argv) > 2 else 14)
    elif len(sys.argv) >= 2 and sys.argv[1] == "resolve":
        resolve_all()
        refresh_intraday()
    else:
        refresh()
        refresh_intraday()
