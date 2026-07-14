"""Streamlit dashboard for the Wealthsimple trade journal.

    pip install streamlit plotly
    streamlit run dashboard.py

Three sections, matching the project plan:
  1. Journal     — filterable trade log + notes (yours and agent-written)
  2. Performance — per-symbol price chart with your buys/sells overlaid,
                   plus realized/unrealized metrics
  3. Summary     — portfolio-level P&L, fees, FX drag
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
    # the position (the exit cost you), so it reads red; negative = avoided
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
               "amount_cad", "disallowed_cad"}
_QTY_COLS = {"quantity", "contracts", "position_qty", "net_contracts"}


def _money_fmt(v) -> str:
    # sign before the $ so losses read -$11.96, not $-11.96
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


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

_, _ctrl_col = st.columns([10, 1])
with _ctrl_col.popover("⚙", use_container_width=True):
    st.markdown("**Data controls**")
    if st.button("⬇ Run API ingest",
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
    if st.button("↻ Refresh price cache",
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
    st.dataframe(
        view[["executed_at", "symbol", "action", "quantity", "price",
              "currency", "gross_amount", "fx_cost_cad", "book_cost_cad",
              "acb_per_share_cad", "realized_cad",
              "mtm_delta_cad", "total_pnl_cad", "flat_move_cad", "source"]]
        .style.map(_action_style, subset=["action"])
        .map(_pnl_style, subset=["realized_cad", "mtm_delta_cad",
                                 "total_pnl_cad"])
        .map(_flat_style, subset=["flat_move_cad"])
        .format(_table_fmt(view), na_rep=""),
        use_container_width=True, hide_index=True)
    st.caption("`fx_cost_cad` = Wealthsimple's ~1.5% currency-conversion fee "
               "modeled per USD-listed trade — it's embedded in the CAD "
               "amount and already inside your P&L (informational, not an "
               "extra charge); blank = CAD-listed/CDR, no conversion. "
               "Ledger columns: `realized_cad` = gain locked in "
               "by the sell; `mtm_delta_cad` = market move on held shares "
               "since the symbol's previous trade; `total_pnl_cad` = "
               "cumulative realized + unrealized marked at the fill price; "
               "`flat_move_cad` = move missed (+, red) or avoided (−, green) "
               "between a full exit and the re-entry buy.")

    if not opt_trades.empty:
        st.subheader("Option trades")
        oview = opt_trades.copy()
        if sym != "All":
            oview = oview[oview.underlying == sym]
        if act != "All":
            oview = oview[oview.action == act]
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
    psym = st.selectbox("Pick a symbol", perf_syms, key="perf_sym")
    t = trades[trades.symbol == psym].sort_values("executed_at")
    px = prices[prices.symbol == psym].sort_values("date")
    osel = opt_trades[opt_trades.underlying == psym].sort_values("occurred_at")
    ocontracts = [c for c in opt_result["contracts"]
                  if c["underlying"] == psym]

    m1, m2, m3, m4, m5 = st.columns(5)
    lots = pd.DataFrame(result["realized"])
    sym_lots = lots[lots.symbol == psym] if not lots.empty else pd.DataFrame()
    pos = next((p for p in positions if p["symbol"] == psym), None)
    _signed_metric(m1, "Realized P&L (CAD)",
                   sym_lots.gain_cad.sum() if not sym_lots.empty else 0.0)
    m2.metric("Open quantity", f"{pos['quantity']:g}" if pos else "0")
    m3.metric("ACB / share (CAD)",
              f"{pos['acb_per_share_cad']:,.4f}" if pos else "—")
    _signed_metric(m4, "Unrealized (CAD)",
                   pos.get("unrealized_gain_cad") if pos else None)
    if len(px) >= 2:
        last, prev = float(px.close.iloc[-1]), float(px.close.iloc[-2])
        m5.metric(f"Last close ({px.currency.iloc[-1]})", f"{last:,.2f}",
                  delta=f"{(last / prev - 1) * 100:+.2f}%")
    else:
        m5.metric("Last close", "—")

    if ocontracts:
        o_open = [c for c in ocontracts if c["status"] == "open"]
        o1, o2, o3 = st.columns(3)
        _signed_metric(o1, "Options premium realized (CAD)",
                       sum(c["net_premium_cad"] for c in ocontracts
                           if c["status"] in ("closed", "expired")))
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

        # Trade prices are in the account currency (CAD); quotes are in the
        # listing's currency. Convert markers so they sit on the price line.
        trade_ccy = tm.currency.iloc[0] if not tm.empty else quote_ccy
        marker_scale, converted = 1.0, False
        if not tm.empty and trade_ccy != quote_ccy:
            usdcad = latest_usdcad()
            if usdcad and trade_ccy == "CAD" and quote_ccy == "USD":
                marker_scale, converted = 1.0 / usdcad, True
            elif usdcad and trade_ccy == "USD" and quote_ccy == "CAD":
                marker_scale, converted = usdcad, True

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_close, y=y_close, name="Close",
                                 mode="lines", line=dict(width=1.5)))
        buys = tm[tm.action == "BUY"]
        sells = tm[tm.action == "SELL"]
        fig.add_trace(go.Scatter(
            x=marker_x(buys), y=buys.price * marker_scale, mode="markers",
            name="Buys",
            marker=dict(symbol="triangle-up", size=11, color=POS)))
        fig.add_trace(go.Scatter(
            x=marker_x(sells), y=sells.price * marker_scale, mode="markers",
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

        fig.update_layout(height=450, margin=dict(t=20, b=20),
                          yaxis_title=f"Price ({quote_ccy})")
        st.plotly_chart(fig, use_container_width=True)
        if converted:
            st.caption(f"Buy/sell markers converted {trade_ccy} → {quote_ccy} "
                       "at today's USD/CAD rate (per-trade rates unavailable); "
                       "marker placement is approximate.")
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
    net_income_cad = income.amount_cad.sum() if not income.empty else 0.0
    c1, c2, c3, c4, c5 = st.columns(5, gap="medium")
    _signed_metric(c1, "Total realized gain (CAD)", s["realized_gain_cad"])
    _signed_metric(c2, "Total unrealized gain (CAD)",
                   total_unrealized if positions else None)
    _signed_metric(c3, "FX conversion drag (CAD)", -fx_result['modeled_drag_cad'])
    _signed_metric(c4, "Options premium realized (CAD)",
                   os_["realized_premium_cad"])
    _signed_metric(c5, "Net income (CAD)", net_income_cad)
    emp = fx_result["empirical"]
    fx_note = (f"FX drag modeled at {fx_result['fee_rate']:.2%} on "
               f"{fx_result['usd_volume_cad']:,.0f} CAD of USD-listing volume "
               f"({fx_result['n_usd_trades']} trades, "
               f"{fx_result['n_usd_option_fills']} option fills). "
               "Already embedded in realized gains — shown for awareness, "
               "not subtracted again.")
    if emp["hourly_median_premium"] is not None:
        fx_note += (f" Observed premium on {emp['n_hourly']} hourly-matched "
                    f"trades: {emp['hourly_median_premium']:.2%}.")
    st.caption(fx_note)

    st.subheader("Open positions")
    pos_df = pd.DataFrame(positions)
    if pos_df.empty:
        st.caption("No open positions.")
    else:
        col_cfg = {}
        if "market_value_cad" in pos_df.columns:
            total_mv = pos_df.market_value_cad.fillna(0).sum()
            if total_mv:
                pos_df["weight"] = pos_df.market_value_cad.fillna(0) / total_mv
                col_cfg["weight"] = st.column_config.ProgressColumn(
                    "weight", min_value=0.0, max_value=1.0, format="percent")
        sty = pos_df.style.apply(_warn_row_style, axis=1)
        if "unrealized_gain_cad" in pos_df.columns:
            sty = sty.map(_pnl_style, subset=["unrealized_gain_cad"])
        sty = sty.format(_table_fmt(pos_df), na_rep="")
        st.dataframe(sty, use_container_width=True, hide_index=True,
                     column_config=col_cfg)

    if opt_result["contracts"]:
        st.subheader("Option contracts")
        odf = pd.DataFrame(opt_result["contracts"])
        odf["warnings"] = odf.warnings.map("; ".join)
        st.dataframe(
            odf.style.apply(_warn_row_style, axis=1)
            .map(_pnl_style, subset=["net_premium_cad"])
            .map(_status_style, subset=["status"])
            .format(_table_fmt(odf), na_rep=""),
            use_container_width=True, hide_index=True)
        if (odf.status == "expired").any():
            st.warning("Expired contracts assume worthless expiry — "
                       "assignment/exercise isn't tracked yet; verify manually.")

    st.subheader("Realized P&L by symbol")
    if not lots.empty:
        by_sym = lots.groupby("symbol").gain_cad.sum().sort_values()
        figb = go.Figure(go.Bar(
            x=by_sym.values, y=by_sym.index, orientation="h",
            marker_color=[POS if v >= 0 else NEG for v in by_sym.values]))
        figb.update_layout(height=max(240, 28 * len(by_sym)),
                           margin=dict(t=10, b=10),
                           xaxis_title="Realized gain (CAD)")
        st.plotly_chart(figb, use_container_width=True)

    st.subheader("Realized lots (ACB method)")
    if not lots.empty:
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
        losses = lots[lots.gain_cad < 0]
        if not losses.empty:
            if "is_superficial" in losses.columns:
                n_sfl = int(losses.is_superficial.sum())
                if n_sfl:
                    st.warning(
                        f"{n_sfl} loss lot(s) flagged as superficial — a "
                        f"repurchase of the same symbol was found within ±30 "
                        f"days and the position was still held at the 30-day "
                        f"cutoff. The disallowed loss is NOT yet added back to "
                        f"ACB — reconcile `disallowed_cad` manually on your T1.")
                unconfirmed = losses[~losses.is_superficial]
                if not unconfirmed.empty:
                    st.warning(
                        f"{len(unconfirmed)} loss lot(s) had no detected "
                        f"repurchase in this account within ±30 days. Affiliated "
                        f"persons' accounts are not checked — verify manually "
                        f"before claiming on your T1.")
            else:
                st.warning("Loss lots above should be checked against the "
                           "superficial-loss rule (repurchase within ±30 days) "
                           "before claiming on taxes.")
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

        divs_cad = inc_f.loc[inc_f.income_type == "DIVIDEND", "amount_cad"].sum()
        int_cad = inc_f.loc[inc_f.income_type == "INTEREST", "amount_cad"].sum()
        tax_cad = inc_f.loc[inc_f.income_type == "NON_RESIDENT_TAX", "amount_cad"].sum()
        net_cad = divs_cad + int_cad + tax_cad
        ic1, ic2, ic3, ic4 = st.columns(4)
        _signed_metric(ic1, "Dividends received (CAD)", divs_cad)
        _signed_metric(ic2, "Interest received (CAD)", int_cad)
        _signed_metric(ic3, "Withholding tax (CAD)", tax_cad)
        _signed_metric(ic4, "Net income (CAD)", net_cad)

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