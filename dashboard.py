"""Streamlit dashboard for the Wealthsimple trade journal.

    pip install streamlit plotly
    streamlit run dashboard.py

Four sections:
  1. Journal     — fill blotter, option trades, notes
  2. Performance — per-symbol price chart with buys/sells overlaid
  3. Summary     — portfolio P&L, dividends, open positions
  4. Income      — dividends, interest, withholding
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from wsjournal import acb, db, fxdrag, options

st.set_page_config(page_title="WS Trade Journal", layout="wide")
db.init_db()


@st.cache_data(ttl=60)
def load():
    with db.connect() as conn:
        trades = pd.read_sql_query(
            "SELECT * FROM trades ORDER BY executed_at DESC", conn)
        opt_trades = pd.read_sql_query(
            "SELECT * FROM option_trades ORDER BY occurred_at DESC", conn)
        notes = pd.read_sql_query(
            "SELECT * FROM notes ORDER BY created_at DESC", conn)
        prices = pd.read_sql_query("SELECT * FROM prices", conn)
        iprices = pd.read_sql_query("SELECT * FROM prices_intraday", conn)
        listings = pd.read_sql_query("SELECT * FROM symbol_listings", conn)
        result = acb.compute(conn)
        positions = acb.unrealized(conn, result["open_positions"])
        opt_result = options.compute(conn)
        fx_result = fxdrag.compute(conn)
        ledger = pd.DataFrame(acb.trade_ledger(conn))
        # modeled embedded FX conversion cost per USD-listing trade
        # (NaN for CAD listings/CDRs — no conversion happened)
        trades["fx_cost_cad"] = trades.id.map(fxdrag.per_trade_costs(conn))
        income = pd.read_sql_query(
            "SELECT * FROM income ORDER BY occurred_at DESC", conn)
    # real datetime dtype so table sorting is temporal (string sort breaks
    # the moment formats vary) and seconds-precision display
    trades["executed_at"] = pd.to_datetime(
        trades.executed_at, utc=True, format="mixed").dt.floor("s")
    if not opt_trades.empty:
        opt_trades["occurred_at"] = pd.to_datetime(
            opt_trades.occurred_at, utc=True, format="mixed").dt.floor("s")
    if not iprices.empty:
        iprices["ts"] = pd.to_datetime(iprices.ts, utc=True, format="mixed")
    if not ledger.empty:
        ledger["executed_at"] = pd.to_datetime(
            ledger.executed_at, utc=True, format="mixed").dt.floor("s")
    if not income.empty:
        income["occurred_at"] = pd.to_datetime(
            income.occurred_at, utc=True, format="mixed").dt.floor("s")
    return (trades, opt_trades, notes, prices, iprices, listings,
            result, positions, opt_result, fx_result, ledger, income)


(trades, opt_trades, notes, prices, iprices, listings,
 result, positions, opt_result, fx_result, ledger, income) = load()

# Gross dividends by symbol (CAD). Withholding stays its own series so a
# total-return figure can subtract it without changing the dividend card.
if income.empty:
    div_by_symbol = pd.Series(dtype=float)
    tax_by_symbol = pd.Series(dtype=float)
else:
    div_by_symbol = (income.loc[income.income_type == "DIVIDEND"]
                     .groupby("symbol")["amount_cad"].sum())
    tax_by_symbol = (income.loc[income.income_type == "NON_RESIDENT_TAX"]
                     .groupby("symbol")["amount_cad"].sum())


def latest_usdcad() -> float | None:
    fx = prices[prices.symbol == "USDCAD=X"].sort_values("date")
    return float(fx.close.iloc[-1]) if not fx.empty else None


# ------------------------------------------------------------------ styling
POS, NEG = "#16a34a", "#dc2626"                       # gain green / loss red
WARN_BG = "background-color: rgba(217,119,6,0.15)"    # amber row tint
STATUS_COLORS = {"open": "#d97706", "closed": "#6b7280", "expired": "#dc2626"}


def _pnl_style(v):
    if pd.isna(v) or v == 0:
        return ""
    return f"color: {POS}" if v > 0 else f"color: {NEG}"


def _action_style(v):
    return (f"color: {POS}" if v == "BUY"
            else f"color: {NEG}" if v == "SELL" else "")


def _status_style(v):
    c = STATUS_COLORS.get(v)
    return f"color: {c}" if c else ""


def _warn_row_style(row):
    return [WARN_BG if bool(row.get("warnings")) else ""] * len(row)


def _flat_style(v):
    # inverted: a positive flat-period move is a gain MISSED while out of
    # the position (the exit cost you), so it reads red; negative = avoided.
    # The journal ledger pre-formats this column as text so Streamlit does
    # not render a missing value as the word "None".
    if isinstance(v, str):
        if not v or v in ("$0.00", "-$0.00"):
            return ""
        return f"color: {POS}" if v.startswith("-") else f"color: {NEG}"
    if pd.isna(v) or v == 0:
        return ""
    return f"color: {NEG}" if v > 0 else f"color: {POS}"


# display formatting: money to cents; quantities keep natural precision
# (fractional shares are real) but drop trailing float noise
_MONEY_COLS = {"price", "fee", "gross_amount", "premium", "strike",
               "fx_cost_cad", "book_cost_cad",
               "realized_cad", "unrealized_cad", "total_pnl_cad",
               "mtm_delta_cad", "flat_move_cad", "acb_per_share_cad",
               "acb_cad", "proceeds_cad", "gain_cad", "market_value_cad",
               "unrealized_gain_cad", "last_price", "net_premium_cad",
               "amount_cad", "total_cad", "dividends_cad", "disallowed_cad"}
_QTY_COLS = {"quantity", "contracts", "position_qty", "net_contracts"}


def _money_fmt(v) -> str:
    # sign before the $ so losses read -$11.96, not $-11.96
    if pd.isna(v):
        return ""
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _md_money(v) -> str:
    """Money for captions. Streamlit treats $...$ as LaTeX."""
    return _money_fmt(v).replace("$", "\\$")


def _income_sum(frame: pd.DataFrame, income_type: str) -> float:
    if frame.empty:
        return 0.0
    return float(frame.loc[frame.income_type == income_type, "amount_cad"].sum())


def _naive_utc(s: pd.Series) -> pd.Series:
    """UTC wall time as YYYY-MM-DD HH:MM. That form sorts chronologically."""
    ts = (pd.to_datetime(s, utc=True).dt.tz_convert("UTC")
          .dt.tz_localize(None).dt.floor("min"))
    return ts.dt.strftime("%Y-%m-%d %H:%M")


def _show_options(odf: pd.DataFrame) -> None:
    if odf.empty:
        return
    st.dataframe(
        odf.style.apply(_warn_row_style, axis=1)
        .map(_pnl_style, subset=["net_premium_cad"])
        .map(_status_style, subset=["status"])
        .format(_table_fmt(odf), na_rep=""),
        use_container_width=True, hide_index=True)


def _table_fmt(df) -> dict:
    fmt = {c: _money_fmt for c in df.columns if c in _MONEY_COLS}
    fmt.update({c: "{:g}" for c in df.columns if c in _QTY_COLS})
    return fmt


def _signed_metric(col, label, value):
    """Full-size green/red metric — matches st.metric height but colors the value."""
    if value is None:
        col.metric(label, "—")
        return
    color = POS if value >= 0 else NEG
    sign = "+" if value >= 0 else "-"
    col.markdown(
        f'<div style="margin-bottom:1rem">'
        f'<p style="font-size:.875rem;margin:0 0 .25rem;opacity:.7">{label}</p>'
        f'<p style="font-size:2.25rem;font-weight:700;line-height:1;color:{color};margin:0">'
        f'{sign}${abs(value):,.2f}</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _nearest_close(close_ser: pd.Series, when: pd.Series) -> pd.Series:
    """Close nearest in time to each timestamp — anchors event markers to
    the plotted price line (option fills have no underlying price of their
    own)."""
    idx = close_ser.index.get_indexer(when, method="nearest")
    return pd.Series(close_ser.values[idx], index=when.index)


def _holding_spans(t: pd.DataFrame) -> list[tuple]:
    """(start, end) spans where the running share quantity is > 0;
    end is None while the position is still open."""
    spans, qty, start = [], 0.0, None
    for _, r in t.sort_values("executed_at").iterrows():
        qty += r.quantity if r.action == "BUY" else -r.quantity
        if qty > 1e-9 and start is None:
            start = r.executed_at
        elif qty <= 1e-9 and start is not None:
            spans.append((start, r.executed_at))
            start = None
    if start is not None:
        spans.append((start, None))
    return spans


# user-drawn banner (U+2571/U+2572 diagonals); must render in a no-wrap
# <pre> — the clamp() scales it to the viewport so ~250 cols never wrap
BANNER = """\
$$╲      $$╲  $$$$$$╲        $$$$$$$$╲                       $$╲                    $$$$$╲                                                   $$╲ 
$$ │ $╲  $$ │$$  __$$╲       ╲__$$  __│                      $$ │                   ╲__$$ │                                                  $$ │
$$ │$$$╲ $$ │$$ ╱  ╲__│         $$ │ $$$$$$╲  $$$$$$╲   $$$$$$$ │ $$$$$$╲              $$ │ $$$$$$╲  $$╲   $$╲  $$$$$$╲  $$$$$$$╲   $$$$$$╲  $$ │
$$ $$ $$╲$$ │╲$$$$$$╲           $$ │$$  __$$╲ ╲____$$╲ $$  __$$ │$$  __$$╲             $$ │$$  __$$╲ $$ │  $$ │$$  __$$╲ $$  __$$╲  ╲____$$╲ $$ │
$$$$  _$$$$ │ ╲____$$╲          $$ │$$ │  ╲__│$$$$$$$ │$$ ╱  $$ │$$$$$$$$ │      $$╲   $$ │$$ ╱  $$ │$$ │  $$ │$$ │  ╲__│$$ │  $$ │ $$$$$$$ │$$ │
$$$  ╱ ╲$$$ │$$╲   $$ │         $$ │$$ │     $$  __$$ │$$ │  $$ │$$   ____│      $$ │  $$ │$$ │  $$ │$$ │  $$ │$$ │      $$ │  $$ │$$  __$$ │$$ │
$$  ╱   ╲$$ │╲$$$$$$  │         $$ │$$ │     ╲$$$$$$$ │╲$$$$$$$ │╲$$$$$$$╲       ╲$$$$$$  │╲$$$$$$  │╲$$$$$$  │$$ │      $$ │  $$ │╲$$$$$$$ │$$ │
╲__╱     ╲__│ ╲______╱          ╲__│╲__│      ╲_______│ ╲_______│ ╲_______│       ╲______╱  ╲______╱  ╲______╱ ╲__│      ╲__│  ╲__│ ╲_______│╲__│
                                                                                                                                                 
                                                                                                                                                                                                                                                                                               
"""

st.html(
    "<div style='display:flex; justify-content:center;'>"
    "<pre style='font-family:monospace; line-height:1.15; margin:0 0 1rem;"
    " white-space:pre; overflow:hidden;"
    " font-size:clamp(3.5px, 0.55vw, 8px);'>"
    + BANNER + "</pre></div>")

if trades.empty:
    st.info("No trades yet. Run `python -m wsjournal.ingest_api` or "
            "`python -m wsjournal.ingest_csv <file.csv>` first.")
    st.stop()

tab_journal, tab_perf, tab_summary, tab_income = st.tabs(
    ["Journal", "Performance", "Summary", "Income"], on_change="rerun")

# ----------------------------------------------------------------- Journal
with tab_journal:
    c1, c2, c3 = st.columns(3)
    sym = c1.selectbox("Symbol", ["All"] + sorted(trades.symbol.unique()))
    act = c2.selectbox("Action", ["All", "BUY", "SELL"])
    src = c3.selectbox("Source", ["All"] + sorted(trades.source.unique()))

    view = trades.copy()
    if sym != "All":
        view = view[view.symbol == sym]
    if act != "All":
        view = view[view.action == act]
    if src != "All":
        view = view[view.source == src]

    view = view.merge(
        ledger[["trade_id", "book_cost_cad", "acb_per_share_cad",
                "realized_cad", "mtm_delta_cad",
                "total_pnl_cad", "flat_move_cad"]],
        left_on="id", right_on="trade_id", how="left")
    fill_cols = ["executed_at", "symbol", "action", "quantity", "price",
                 "currency", "gross_amount", "fx_cost_cad"]
    fill = view[fill_cols].copy()
    fill["executed_at"] = _naive_utc(fill["executed_at"])
    # Streamlit renders NaN as the word "None". Format the missing fee as blank
    # here; the styler would hand the raw null through.
    fill["fx_cost_cad"] = [
        "" if pd.isna(v) else _money_fmt(v) for v in fill["fx_cost_cad"]]
    fill_fmt = _table_fmt(fill)
    fill_fmt.pop("fx_cost_cad", None)
    st.dataframe(
        fill.style.map(_action_style, subset=["action"])
        .format(fill_fmt, na_rep=""),
        use_container_width=True, hide_index=True)
    st.caption("FX cost is Wealthsimple's ~1.5% conversion fee on CAD-settled "
               "USD listings. It is already inside P&L. Blank means no conversion.")

    with st.expander("Mark-to-market ledger"):
        led_cols = ["executed_at", "symbol", "action", "quantity",
                    "book_cost_cad", "acb_per_share_cad", "realized_cad",
                    "mtm_delta_cad", "total_pnl_cad", "flat_move_cad"]
        led = view[led_cols].copy()
        led["executed_at"] = _naive_utc(led["executed_at"])
        led["flat_move_cad"] = [
            "" if pd.isna(v) else _money_fmt(v) for v in led["flat_move_cad"]]
        led_fmt = _table_fmt(led)
        led_fmt.pop("flat_move_cad", None)
        st.dataframe(
            led.style.map(_action_style, subset=["action"])
            .map(_pnl_style, subset=["realized_cad", "mtm_delta_cad",
                                     "total_pnl_cad"])
            .map(_flat_style, subset=["flat_move_cad"])
            .format(led_fmt, na_rep=""),
            use_container_width=True, hide_index=True)
        st.caption("`realized_cad` is the gain locked in by the sell. "
                   "`mtm_delta_cad` is the market move on held shares since "
                   "the symbol's previous trade. `total_pnl_cad` is cumulative "
                   "realized plus unrealized, marked at the fill price. "
                   "`flat_move_cad` is the move missed (+, red) or avoided "
                   "(−, green) between a full exit and the re-entry buy.")

    if not opt_trades.empty:
        st.subheader("Option trades")
        oview = opt_trades.copy()
        if sym != "All":
            oview = oview[oview.underlying == sym]
        if act != "All":
            oview = oview[oview.action == act]
        oview["occurred_at"] = _naive_utc(oview["occurred_at"])
        st.dataframe(
            oview[["occurred_at", "underlying", "contract_type", "strike",
                   "expiry", "action", "contracts", "premium", "currency"]]
            .style.map(_action_style, subset=["action"])
            .format(_table_fmt(oview), na_rep=""),
            use_container_width=True, hide_index=True)

    st.subheader("Notes")
    with st.form("note", clear_on_submit=True):
        n1, n2 = st.columns([1, 3])
        note_sym = n1.text_input("Symbol (optional)")
        note_tag = n1.selectbox("Tag", ["thesis", "mistake", "review", "idea"])
        note_body = n2.text_area("Note")
        if st.form_submit_button("Save note") and note_body.strip():
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO notes (created_at, symbol, tag, body, author) "
                    "VALUES (?,?,?,?,'me')",
                    (datetime.now(timezone.utc).isoformat(),
                     note_sym.upper() or None, note_tag, note_body))
            st.cache_data.clear()
            st.rerun()

    for _, n in notes.iterrows():
        who = "🤖" if n.author == "agent" else "✍️"
        st.markdown(
            f"{who} **{n.symbol or '—'}** · `{n.tag or 'note'}` · "
            f"{str(n.created_at)[:16]}\n\n> {n.body}")

# -------------------------------------------------------------- Performance
with tab_perf:
    # option-only underlyings (no share trades) must show up too
    perf_syms = sorted(set(trades.symbol.unique())
                       | set(opt_trades.underlying.unique()))
    held = [p for p in positions if p.get("market_value_cad")]
    default_sym = (max(held, key=lambda p: p["market_value_cad"])["symbol"]
                   if held else perf_syms[0])
    default_idx = perf_syms.index(default_sym) if default_sym in perf_syms else 0
    psym = st.selectbox("Pick a symbol", perf_syms, index=default_idx,
                        key="perf_sym")
    t = trades[trades.symbol == psym].sort_values("executed_at")
    px = prices[prices.symbol == psym].sort_values("date")
    osel = opt_trades[opt_trades.underlying == psym].sort_values("occurred_at")
    ocontracts = [c for c in opt_result["contracts"]
                  if c["underlying"] == psym]

    lots = pd.DataFrame(result["realized"])
    sym_lots = lots[lots.symbol == psym] if not lots.empty else pd.DataFrame()
    pos = next((p for p in positions if p["symbol"] == psym), None)
    realized = float(sym_lots.gain_cad.sum()) if not sym_lots.empty else 0.0
    # No open position means nothing left to mark. A position with no cached
    # price has an unknown mark, so the total stays blank rather than treating
    # the unrealized leg as zero.
    if pos is None:
        unreal = 0.0
    else:
        unreal = pos.get("unrealized_gain_cad")
        unreal = None if unreal is None else float(unreal)
    divs = float(div_by_symbol.get(psym, 0.0))
    withholding = float(tax_by_symbol.get(psym, 0.0))
    opt_realized = sum(c["net_premium_cad"] for c in ocontracts
                       if c["status"] in ("closed", "expired"))
    total_return = (None if unreal is None else
                    realized + unreal + divs + withholding + opt_realized)

    tr, _ = st.columns([1, 3])
    _signed_metric(tr, "Total return (CAD)", total_return)
    st.caption("Realized share P&L + unrealized + dividends − withholding "
               "+ realized option premium. Open option premium is not included.")

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    _signed_metric(m1, "Realized P&L (CAD)", realized)
    m2.metric("Open quantity", f"{pos['quantity']:g}" if pos else "0")
    m3.metric("ACB / share (CAD)",
              f"{pos['acb_per_share_cad']:,.4f}" if pos else "—")
    _signed_metric(m4, "Unrealized (CAD)", unreal if pos else None)
    _signed_metric(m6, "Dividends received (CAD)", divs)
    if len(px) >= 2:
        last, prev = float(px.close.iloc[-1]), float(px.close.iloc[-2])
        m5.metric(f"Last close ({px.currency.iloc[-1]})", f"{last:,.2f}",
                  delta=f"{(last / prev - 1) * 100:+.2f}%")
    else:
        m5.metric("Last close", "—")

    if ocontracts:
        o_open = [c for c in ocontracts if c["status"] == "open"]
        o1, o2, o3 = st.columns(3)
        _signed_metric(o1, "Options premium realized (CAD)", opt_realized)
        o2.metric("Open option contracts",
                  f"{sum(abs(c['net_contracts']) for c in o_open):g}")
        _signed_metric(o3, "Open net premium (CAD)",
                       sum(c["net_premium_cad"] for c in o_open)
                       if o_open else 0.0)

    li = listings[listings.symbol == psym] if not listings.empty else listings
    if not li.empty:
        st.caption(f"Prices: **{li.yahoo_symbol.iloc[0]}** "
                   f"({li.quote_currency.iloc[0]}), resolved by "
                   f"{li.method.iloc[0].replace('_', ' ')}"
                   + (" — CAD-quoted Canadian listing/CDR, not the US ticker"
                      if li.quote_currency.iloc[0] == "CAD"
                      and "." in str(li.yahoo_symbol.iloc[0]) else ""))

    pxh = (iprices[iprices.symbol == psym].sort_values("ts")
           if not iprices.empty else iprices)
    gran = st.radio("Granularity", ["Daily", "Hourly (recent)"],
                    horizontal=True, key="perf_gran")
    use_hourly = gran.startswith("Hourly") and not pxh.empty
    if gran.startswith("Hourly") and pxh.empty:
        st.info("No hourly bars cached for this symbol — run "
                "`python -m wsjournal.prices` (refreshes daily + hourly). "
                "Showing daily instead.")

    if px.empty:
        st.warning("No cached prices — run `python -m wsjournal.prices` "
                   "or set prices manually.")
    else:
        if use_hourly:
            x_close, y_close = pxh.ts, pxh.close
            quote_ccy = pxh.currency.iloc[-1]
            # markers at the actual execution timestamp, within the window
            tm = t[t.executed_at >= pxh.ts.min()]
            om = osel[osel.occurred_at >= pxh.ts.min()] if not osel.empty else osel
            close_ser = pd.Series(pxh.close.values, index=pd.DatetimeIndex(pxh.ts))
            d_lo, d_hi = str(pxh.ts.min())[:10], str(pxh.ts.max())[:10]
            def marker_x(df): return df.executed_at
            def event_x(s): return s
        else:
            x_close, y_close = px.date, px.close
            quote_ccy = px.currency.iloc[-1]
            tm = t
            om = osel
            close_ser = pd.Series(px.close.values,
                                  index=pd.to_datetime(px.date, utc=True))
            d_lo, d_hi = px.date.iloc[0], px.date.iloc[-1]
            def marker_x(df): return df.executed_at.dt.strftime("%Y-%m-%d")
            def event_x(s): return s.dt.strftime("%Y-%m-%d") if hasattr(s, "dt") \
                else s.strftime("%Y-%m-%d")

        # Each trade's price is in ITS OWN settlement currency — CAD
        # pre-hybrid, natively USD post-2026-07-01 for some symbols — while
        # quotes are in the listing's currency. A symbol's trade history can
        # span the switch, so scale per row, not once for the whole window.
        usdcad = latest_usdcad()
        converted = False
        if tm.empty:
            marker_scale = pd.Series(dtype=float)
        else:
            marker_scale = pd.Series(1.0, index=tm.index)
            if usdcad:
                to_usd = (tm.currency == "CAD") & (quote_ccy == "USD")
                to_cad = (tm.currency == "USD") & (quote_ccy == "CAD")
                marker_scale[to_usd] = 1.0 / usdcad
                marker_scale[to_cad] = usdcad
                converted = bool((to_usd | to_cad).any())
        tm = tm.assign(_marker_scale=marker_scale)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_close, y=y_close, name="Close",
                                 mode="lines", line=dict(width=1.5)))
        buys = tm[tm.action == "BUY"]
        sells = tm[tm.action == "SELL"]
        fig.add_trace(go.Scatter(
            x=marker_x(buys), y=buys.price * buys._marker_scale, mode="markers",
            name="Buys",
            marker=dict(symbol="triangle-up", size=11, color=POS)))
        fig.add_trace(go.Scatter(
            x=marker_x(sells), y=sells.price * sells._marker_scale, mode="markers",
            name="Sells",
            marker=dict(symbol="triangle-down", size=11, color=NEG)))

        # option fills anchored to the price line (premiums aren't underlying
        # prices, so the nearest close is the only sane y-position)
        if not om.empty:
            oy = _nearest_close(close_ser, om.occurred_at)
            ocd = om[["action", "contracts", "contract_type", "strike",
                      "expiry", "premium"]]
            for act, shape, color, name in (
                    ("BUY", "diamond", POS, "Option buys"),
                    ("SELL", "diamond", NEG, "Option sells"),
                    ("EXPIRE", "x", "#6b7280", "Option expiries")):
                sub = om[om.action == act]
                if sub.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=event_x(sub.occurred_at), y=oy.loc[sub.index],
                    mode="markers", name=name,
                    marker=dict(symbol=shape, size=10, color=color,
                                line=dict(width=1, color="white")),
                    customdata=ocd.loc[sub.index].values,
                    hovertemplate=("%{customdata[0]} %{customdata[1]:g} × "
                                   "%{customdata[2]} %{customdata[3]} "
                                   "exp %{customdata[4]}<br>"
                                   "premium %{customdata[5]:,.2f} CAD"
                                   "<extra></extra>")))

        # shade the periods where shares were held
        for s0, s1 in _holding_spans(t):
            if str(s1 or "9")[:10] < d_lo:
                continue  # span closed before the view window
            x0 = event_x(max(s0, pd.Timestamp(d_lo, tz="UTC")))
            x1 = event_x(min(s1, pd.Timestamp(d_hi + "T23:59", tz="UTC"))
                         if s1 is not None
                         else pd.Timestamp(d_hi + "T23:59", tz="UTC"))
            fig.add_vrect(x0=x0, x1=x1, fillcolor="rgba(59,130,246,0.07)",
                          line_width=0, layer="below")

        # strike lines (colored by contract status) + expiry markers
        strikes_skipped = 0
        expiries: set[str] = set()
        lo, hi = float(y_close.min()), float(y_close.max())
        for c in ocontracts:
            if not (0.2 * lo <= c["strike"] <= 5 * hi):
                strikes_skipped += 1
                continue
            color = STATUS_COLORS.get(c["status"], "#6b7280")
            fig.add_hline(
                y=c["strike"], line_dash="dot", line_width=1,
                line_color=color, annotation_position="right",
                annotation_font_size=10, annotation_font_color=color,
                annotation_text=(("C" if c["contract_type"] == "call" else "P")
                                 + f" {c['strike']:g} {c['expiry']}"))
            expiries.add(str(c["expiry"]))
        for exp in sorted(expiries):
            if d_lo <= exp <= d_hi:
                fig.add_vline(x=exp, line_dash="dash", line_width=1,
                              line_color="#9ca3af", annotation_text=f"exp {exp}",
                              annotation_position="top",
                              annotation_font_size=10,
                              annotation_font_color="#9ca3af")

        fig.update_layout(
            height=450, margin=dict(t=40, b=20),
            yaxis_title=f"Price ({quote_ccy})",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
        st.plotly_chart(fig, use_container_width=True)
        if converted:
            st.caption(f"Buy/sell markers converted to {quote_ccy} at "
                       "today's USD/CAD rate where a trade's own currency "
                       "differs from the quote (per-trade historical rates "
                       "unavailable); marker placement is approximate.")
        if strikes_skipped:
            st.caption(f"⚠️ {strikes_skipped} option strike line(s) not drawn — "
                       "far outside the plotted price range, so the option "
                       "likely underlies a different listing than the plotted "
                       "one (e.g. a US-listed option while the shares resolve "
                       "to a Canadian listing).")

    # shares vs options outcome on one timeline — the same ticker can have
    # wildly different results in each
    if not sym_lots.empty or not osel.empty:
        st.subheader("Cumulative realized P&L")
        figc = go.Figure()
        ev: list[tuple] = []
        if not sym_lots.empty:
            sl = sym_lots.copy()
            sl["sold_at"] = pd.to_datetime(sl.sold_at, utc=True, format="mixed")
            sl = sl.sort_values("sold_at")
            figc.add_trace(go.Scatter(
                x=sl.sold_at, y=sl.gain_cad.cumsum(),
                name="Shares realized (ACB)", mode="lines+markers",
                line=dict(shape="hv", color="#3b82f6")))
            ev += list(zip(sl.sold_at, sl.gain_cad))
        if not osel.empty:
            cash = osel.apply(
                lambda r: r.premium if r.action == "SELL"
                else -r.premium if r.action == "BUY" else 0.0, axis=1)
            figc.add_trace(go.Scatter(
                x=osel.occurred_at, y=cash.cumsum(),
                name="Options net premium", mode="lines+markers",
                line=dict(shape="hv", color="#9333ea")))
            ev += list(zip(osel.occurred_at, cash))
        if not sym_lots.empty and not osel.empty:
            ev.sort(key=lambda p: p[0])
            figc.add_trace(go.Scatter(
                x=[p[0] for p in ev],
                y=pd.Series([p[1] for p in ev]).cumsum(),
                name="Total", mode="lines",
                line=dict(shape="hv", dash="dash", color="#6b7280")))
        figc.add_hline(y=0, line_width=1, line_color="#9ca3af")
        figc.update_layout(height=300, margin=dict(t=20, b=20),
                           yaxis_title="Cumulative CAD")
        st.plotly_chart(figc, use_container_width=True)
        if not osel.empty:
            st.caption("Options trace is net premium cash flow (SELL +, "
                       "BUY −); it equals realized premium once contracts "
                       "close or expire.")

    if not t.empty:
        st.subheader("Trade ledger")
        tl = (ledger[ledger.symbol == psym].sort_values("executed_at")
              .merge(trades[["id", "fx_cost_cad"]],
                     left_on="trade_id", right_on="id", how="left"))
        st.dataframe(
            tl[["executed_at", "action", "quantity", "price", "fx_cost_cad",
                "position_qty", "book_cost_cad", "acb_per_share_cad",
                "realized_cad",
                "unrealized_cad", "total_pnl_cad", "mtm_delta_cad",
                "flat_move_cad"]]
            .style.map(_action_style, subset=["action"])
            .map(_pnl_style, subset=["realized_cad", "unrealized_cad",
                                     "total_pnl_cad", "mtm_delta_cad"])
            .map(_flat_style, subset=["flat_move_cad"])
            .format(_table_fmt(tl), na_rep=""),
            use_container_width=True, hide_index=True)
        st.caption("Unrealized is marked at each fill's own price (a fill is "
                   "a ground-truth mark at that instant). `mtm_delta_cad` is "
                   "the market move on held shares between consecutive trades "
                   "— a fill itself never changes total P&L at execution. "
                   "`flat_move_cad` shows what a full exit missed (+, red) or "
                   "avoided (−, green) by the time you re-entered.")
    if not osel.empty:
        st.subheader("Option fills")
        st.dataframe(
            osel[["occurred_at", "contract_type", "strike", "expiry",
                  "action", "contracts", "premium", "currency"]]
            .style.map(_action_style, subset=["action"])
            .format(_table_fmt(osel), na_rep=""),
            use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ Summary
with tab_summary:
    s = result["summary"]
    os_ = opt_result["summary"]
    total_unrealized = sum(p.get("unrealized_gain_cad") or 0 for p in positions)
    divs_cad = _income_sum(income, "DIVIDEND")
    int_cad = _income_sum(income, "INTEREST")
    tax_cad = _income_sum(income, "NON_RESIDENT_TAX")
    fee_cad = _income_sum(income, "FEE")
    promo_cad = _income_sum(income, "PROMOTION")

    pos_df = pd.DataFrame(positions)
    total_mv = None
    if not pos_df.empty and "market_value_cad" in pos_df.columns:
        mv = pos_df.market_value_cad.fillna(0)
        if float(mv.sum()):
            total_mv = float(mv.sum())
            pos_df["weight"] = mv / total_mv
            pos_df = pos_df.sort_values(
                "market_value_cad", ascending=False, na_position="last")
    if not pos_df.empty:
        pos_df["dividends_cad"] = pos_df.symbol.map(div_by_symbol).fillna(0.0)

    st.markdown("**Trading**")
    c1, c2, c3, c4 = st.columns(4, gap="medium")
    _signed_metric(c1, "Realized (shares)", s["realized_gain_cad"])
    _signed_metric(c2, "Unrealized",
                   total_unrealized if positions else None)
    _signed_metric(c3, "Options premium", os_["realized_premium_cad"])
    if total_mv is None:
        c4.metric("Market value", "—")
    else:
        c4.metric("Market value", f"${total_mv:,.2f}")

    st.markdown("**Income and costs**")
    d1, d2, d3, d4 = st.columns(4, gap="medium")
    _signed_metric(d1, "Dividends received", divs_cad)
    _signed_metric(d2, "Interest", int_cad)
    _signed_metric(d3, "Withholding tax", tax_cad)
    _signed_metric(d4, "FX conversion drag", -fx_result["modeled_drag_cad"])
    emp = fx_result["empirical"]
    note = (
        "Dividends are gross (cash, DIY, and manufactured / securities-lending) "
        "and are not reduced by withholding. "
        f"Fees ({_md_money(fee_cad)}) and promotions ({_md_money(promo_cad)}) "
        "are excluded — see the Income tab. "
        f"FX drag is modeled at {fx_result['fee_rate']:.2%} on "
        f"{fx_result['usd_volume_cad']:,.0f} CAD of USD-listing volume "
        f"({fx_result['n_usd_trades']} trades, "
        f"{fx_result['n_usd_option_fills']} option fills) and is already "
        "embedded in realized gains — shown for awareness, not subtracted."
    )
    if emp["hourly_median_premium"] is not None:
        note += (f" Observed premium on {emp['n_hourly']} hourly-matched "
                 f"trades: {emp['hourly_median_premium']:.2%}.")
    st.caption(note)

    st.subheader("Open positions")
    if pos_df.empty:
        st.caption("No open positions.")
    else:
        lead = ["symbol", "quantity", "market_value_cad", "unrealized_gain_cad",
                "weight", "dividends_cad", "acb_per_share_cad", "last_price"]
        show_cols = [c for c in lead if c in pos_df.columns]
        style_cols = show_cols + (["warnings"] if "warnings" in pos_df.columns
                                   else [])
        shown = pos_df[style_cols]
        sty = shown.style.apply(_warn_row_style, axis=1)
        if "unrealized_gain_cad" in shown.columns:
            sty = sty.map(_pnl_style, subset=["unrealized_gain_cad"])
        sty = sty.format(_table_fmt(shown), na_rep="")
        col_cfg = {}
        if "weight" in shown.columns:
            col_cfg["weight"] = st.column_config.ProgressColumn(
                "weight", min_value=0.0, max_value=1.0, format="percent")
        if "warnings" in shown.columns:
            col_cfg["warnings"] = None
        st.dataframe(sty, use_container_width=True, hide_index=True,
                     column_config=col_cfg)

    if opt_result["contracts"]:
        st.subheader("Option contracts")
        odf = pd.DataFrame(opt_result["contracts"])
        odf["warnings"] = odf.warnings.map("; ".join)
        open_odf = odf[odf.status == "open"]
        closed_odf = odf[odf.status != "open"]
        if open_odf.empty:
            st.caption("No open option contracts.")
        else:
            _show_options(open_odf)
        if (odf.status == "expired").any():
            st.warning("Expired contracts assume worthless expiry — "
                       "assignment/exercise isn't tracked yet; verify manually.")
        if not closed_odf.empty:
            with st.expander(
                    f"Closed and expired contracts ({len(closed_odf)})"):
                _show_options(closed_odf)

    st.subheader("Realized P&L by symbol")
    if not lots.empty:
        by_sym = lots.groupby("symbol").gain_cad.sum().sort_values()
        winners = by_sym[by_sym > 0].nlargest(10)
        losers = by_sym[by_sym < 0].nsmallest(10)
        show = pd.concat([losers, winners]).sort_values()
        if not show.empty:
            figb = go.Figure(go.Bar(
                x=show.values, y=show.index, orientation="h",
                marker_color=[POS if v >= 0 else NEG for v in show.values]))
            figb.update_layout(height=max(240, 28 * len(show)),
                               margin=dict(t=10, b=10),
                               xaxis_title="Realized gain (CAD)")
            st.plotly_chart(figb, use_container_width=True)
            st.caption("Largest winners and losers, up to ten of each.")
        full = (by_sym.sort_values(ascending=False)
                .rename("gain_cad").reset_index())
        with st.expander(f"All symbols ({len(full)})"):
            st.dataframe(
                full.style.map(_pnl_style, subset=["gain_cad"])
                .format(_table_fmt(full), na_rep=""),
                use_container_width=True, hide_index=True)

        losses = lots[lots.gain_cad < 0]
        if (not losses.empty and "is_superficial" in losses.columns
                and int(losses.is_superficial.sum())):
            n_sfl = int(losses.is_superficial.sum())
            st.warning(
                f"{n_sfl} loss lot(s) flagged as superficial — a "
                f"repurchase of the same symbol was found within ±30 "
                f"days and the position was still held at the 30-day "
                f"cutoff. The disallowed loss is NOT yet added back to "
                f"ACB — reconcile `disallowed_cad` manually on your T1.")
        with st.expander("Tax lots (ACB)"):
            sty = lots.style.map(_pnl_style, subset=["gain_cad"])
            if "is_superficial" in lots.columns:
                sty = sty.apply(
                    lambda row: [WARN_BG if row.get("is_superficial") else ""
                                 for _ in row],
                    axis=1)
                if "disallowed_cad" in lots.columns:
                    sty = sty.map(_pnl_style, subset=["disallowed_cad"])
            st.dataframe(sty.format(_table_fmt(lots), na_rep=""),
                         use_container_width=True, hide_index=True)
            if not losses.empty:
                if "is_superficial" in losses.columns:
                    unconfirmed = losses[~losses.is_superficial]
                    if not unconfirmed.empty:
                        st.warning(
                            f"{len(unconfirmed)} loss lot(s) had no detected "
                            f"repurchase in this account within ±30 days. "
                            f"Affiliated persons' accounts are not checked — "
                            f"verify manually before claiming on your T1.")
                else:
                    st.warning(
                        "Loss lots in this table should be checked against "
                        "the superficial-loss rule (repurchase within ±30 "
                        "days) before claiming on taxes.")
    else:
        st.caption("No closed lots yet.")

with tab_income:
    st.subheader("Income")
    if income.empty:
        st.caption("No income events yet. Run `python -m wsjournal.ingest_api --renormalize` to populate.")
    else:
        years_avail = sorted(income.occurred_at.dt.year.unique(), reverse=True)
        year_sel = st.selectbox("Year", ["All"] + [str(y) for y in years_avail],
                                key="income_year")
        inc_f = (income if year_sel == "All"
                 else income[income.occurred_at.dt.year == int(year_sel)])

        divs_cad = _income_sum(inc_f, "DIVIDEND")
        int_cad = _income_sum(inc_f, "INTEREST")
        tax_cad = _income_sum(inc_f, "NON_RESIDENT_TAX")
        net_cad = divs_cad + int_cad + tax_cad
        ic1, ic2, ic3, ic4 = st.columns(4)
        _signed_metric(ic1, "Dividends received (CAD)", divs_cad)
        _signed_metric(ic2, "Interest received (CAD)", int_cad)
        _signed_metric(ic3, "Withholding tax (CAD)", tax_cad)
        _signed_metric(ic4, "Net income (CAD)", net_cad)
        st.caption(
            "Net income is dividends + interest + withholding. "
            f"Fees ({_md_money(_income_sum(inc_f, 'FEE'))}) and promotions "
            f"({_md_money(_income_sum(inc_f, 'PROMOTION'))}) are under "
            "All income events and are not included.")

        inc_sorted = inc_f.sort_values("occurred_at")
        if not inc_sorted.empty:
            figI = go.Figure()
            _INC_COLORS = {"DIVIDEND": "#16a34a", "INTEREST": "#3b82f6",
                           "NON_RESIDENT_TAX": "#dc2626"}
            for itype, icolor in _INC_COLORS.items():
                sub = inc_sorted[inc_sorted.income_type == itype]
                if not sub.empty:
                    figI.add_trace(go.Scatter(
                        x=sub.occurred_at, y=sub.amount_cad.cumsum(),
                        name=itype.replace("_", " ").title(),
                        mode="lines+markers",
                        line=dict(shape="hv", color=icolor)))
            figI.add_hline(y=0, line_width=1, line_color="#9ca3af")
            figI.update_layout(height=280, margin=dict(t=20, b=20),
                               yaxis_title="Cumulative CAD")
            st.plotly_chart(figI, use_container_width=True)

        div_df = (inc_f[inc_f.income_type == "DIVIDEND"]
                  .groupby("symbol", dropna=False)["amount_cad"]
                  .sum()
                  .sort_values(ascending=False)
                  .reset_index()
                  .rename(columns={"amount_cad": "total_cad"}))
        if not div_df.empty:
            st.caption("Dividends by symbol (CAD)")
            st.dataframe(
                div_df.style.map(_pnl_style, subset=["total_cad"])
                .format(_table_fmt(div_df), na_rep=""),
                use_container_width=True, hide_index=True)

        if not inc_sorted.empty:
            with st.expander("Monthly breakdown"):
                monthly = (inc_sorted
                           .assign(month=inc_sorted.occurred_at.dt.to_period("M").astype(str))
                           .groupby(["month", "income_type"])["amount_cad"]
                           .sum().unstack(fill_value=0.0).reset_index())
                st.dataframe(
                    monthly.style.format(
                        {c: _money_fmt for c in monthly.columns if c != "month"},
                        na_rep=""),
                    use_container_width=True, hide_index=True)

        with st.expander("All income events"):
            all_inc = inc_f[["occurred_at", "income_type", "sub_type",
                              "symbol", "amount_cad", "account_id"]].copy()
            st.dataframe(
                all_inc.style.map(_pnl_style, subset=["amount_cad"])
                .format(_table_fmt(all_inc), na_rep=""),
                use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Data controls
st.divider()
with st.expander("Data controls"):
    _dc1, _dc2 = st.columns(2)
    if _dc1.button("⬇ Run API ingest",
                   help="Pull new activities from Wealthsimple "
                        "(requires cached auth tokens — see terminal on first run)",
                   use_container_width=True):
        with st.spinner("Fetching activities — this may take 1-2 minutes…"):
            _r = subprocess.run(
                [sys.executable, "-m", "wsjournal.ingest_api"],
                capture_output=True, text=True,
            )
        if _r.returncode == 0:
            st.success(_r.stdout or "Ingest complete.")
            st.cache_data.clear()
            st.rerun()
        else:
            _out = (_r.stdout + _r.stderr).strip()
            st.error(f"Ingest failed.\n\n```\n{_out}\n```")
            if "authenticat" in _out.lower() or "eoferror" in _out.lower():
                st.info("Token cache missing or expired — run "
                        "`python -m wsjournal.ingest_api` in a terminal "
                        "to re-authenticate, then retry here.")
    if _dc2.button("↻ Refresh price cache",
                   help="Update daily and hourly closes from Yahoo Finance (~2-5 min)",
                   use_container_width=True):
        with st.spinner("Fetching prices from Yahoo Finance…"):
            _r = subprocess.run(
                [sys.executable, "-m", "wsjournal.prices"],
                capture_output=True, text=True,
            )
        if _r.returncode == 0:
            st.success(_r.stdout or "Price cache updated.")
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(f"Price refresh failed.\n\n```\n{_r.stderr or _r.stdout}\n```")