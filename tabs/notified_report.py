"""Tab 2 — Notified Orders Report."""
import io
import math
from datetime import date, time

import plotly.graph_objects as go
import pytz
import streamlit as st

from queries.db_queries import (
    fetch_report, to_km_offset, circle_xy, haversine_km,
    SESSION_PALETTE, CATEGORY_STYLES,
)
import pandas as pd

IST = pytz.timezone("Asia/Kolkata")


class ProgressLog:
    def __init__(self, container):
        self._ph = container.empty()
        self._steps = []

    def __call__(self, msg, icon="🔄"):
        if self._steps and self._steps[-1][0] == "🔄":
            self._steps[-1] = ("✅", self._steps[-1][1])
        self._steps.append((icon, msg))
        self._render()

    def _render(self):
        self._ph.markdown("\n\n".join(f"{ico} {txt}" for ico, txt in self._steps))

    def done(self, msg=None):
        if self._steps and self._steps[-1][0] == "🔄":
            self._steps[-1] = ("✅", self._steps[-1][1])
        if msg:
            self._steps.append(("✅", msg))
        self._render()

    def warn(self, msg):
        self._steps.append(("⛔", msg))
        self._render()


def make_report_plot(plot_orders):
    if not plot_orders:
        return None
    ref_lat = sum(o["pickup_lat"] for o in plot_orders) / len(plot_orders)
    ref_lng = sum(o["pickup_lng"] for o in plot_orders) / len(plot_orders)

    offsets = []
    for o in plot_orders:
        dx, dy = to_km_offset(ref_lat, ref_lng, o["pickup_lat"], o["pickup_lng"])
        offsets.append({**o, "dx": dx, "dy": dy, "dist_km": round(math.sqrt(dx**2 + dy**2), 3)})

    circles = go.Figure()
    for r_km in [1, 2, 3, 5, 10, 25]:
        cx, cy = circle_xy(r_km)
        circles.add_trace(go.Scatter(
            x=cx, y=cy, mode="lines",
            line=dict(color="lightgrey", width=1, dash="dot"),
            showlegend=False, hoverinfo="skip",
        ))
    circles.add_trace(go.Scatter(
        x=[o["dx"] for o in offsets],
        y=[o["dy"] for o in offsets],
        mode="markers",
        name=f"Notified ({len(offsets)})",
        marker=dict(color="green", symbol="circle", size=10, line=dict(width=1, color="green")),
        text=[
            f"<b>{o['order_key']}</b>  {o['time_ist']}<br>"
            f"Customer: {o['customer_name']}<br>"
            f"Pickup: {o['pickup_name']}<br>"
            f"Notified to: {o['notified_riders']}"
            for o in offsets
        ],
        hovertemplate="%{text}<extra></extra>",
    ))
    all_x = [o["dx"] for o in offsets]
    all_y = [o["dy"] for o in offsets]
    axis_range = min(max([abs(v) for v in all_x + all_y] + [2]) * 1.2, 100)
    circles.update_layout(
        title=f"Notified Orders — Pickup Locations ({len(offsets)} orders)",
        xaxis=dict(scaleanchor="y", scaleratio=1, range=[-axis_range, axis_range],
                   zeroline=True, zerolinecolor="lightgrey", gridcolor="whitesmoke"),
        yaxis=dict(range=[-axis_range, axis_range],
                   zeroline=True, zerolinecolor="lightgrey", gridcolor="whitesmoke"),
        height=500, hovermode="closest", plot_bgcolor="white",
    )
    return circles


def render(tab):
    with tab:
        st.header("Notified Orders Report")
        st.caption("Riders fetched from attendance table. Orders filtered on created_on (indexed).")

        with st.expander("⚙️ Report Settings", expanded="report_result" not in st.session_state):
            rc1, rc2, rc3 = st.columns([2, 1, 1])
            with rc1:
                r_date = st.date_input("Date", value=date.today(), key="r_date")
            with rc2:
                r_from = st.time_input("From (IST)", value=time(0, 0), key="r_from")
            with rc3:
                r_to = st.time_input("To (IST)", value=time(23, 59), key="r_to")
            fetch_btn = st.button("▶ Fetch Report", type="primary", key="fetch_report_btn")

        if fetch_btn:
            if r_from >= r_to:
                st.error("'From' must be before 'To'.")
            else:
                from datetime import datetime
                data_col, prog_col = st.columns([4, 1])
                with prog_col:
                    st.markdown("**Progress**")
                log = ProgressLog(prog_col)

                r_start_utc = IST.localize(datetime.combine(r_date, r_from)).astimezone(pytz.utc)
                r_end_utc   = IST.localize(datetime.combine(r_date, r_to)).astimezone(pytz.utc)

                log("Fetching attendance + orders…")
                rows, plot_orders = fetch_report(r_date, r_start_utc, r_end_utc)
                log(f"{len(rows)} riders found")

                if not rows:
                    data_col.warning(f"No riders with attendance on {r_date}.")
                    log.warn("No data")
                else:
                    log("Building report…")
                    total_riders       = len(rows)
                    total_notified_cnt = sum(r["total_notified"] for r in rows)
                    riders_with_orders = sum(1 for r in rows if r["total_notified"] > 0)

                    m1, m2, m3 = data_col.columns(3)
                    m1.metric("Riders Punched In", total_riders)
                    m2.metric("Riders with Orders", riders_with_orders)
                    m3.metric("Total Notifications", total_notified_cnt)

                    df_report = pd.DataFrame([{
                        "Rider Key":        r["rider_key"],
                        "Sessions":         r["punch_count"],
                        "Active (open)":    int(r["active_sessions"] or 0),
                        "Hours Worked":     float(r["total_hours"] or 0),
                        "Orders Notified":  r["total_notified"],
                        "Order Keys":       r["notified_orders"] or "",
                        "Unique Customers": r["unique_customers"] or "",
                        "Blacklisted":      r["blacklisted_customers"] or "",
                    } for r in rows])

                    data_col.dataframe(df_report, use_container_width=True, hide_index=True)

                    csv_buf = io.StringIO()
                    df_report.to_csv(csv_buf, index=False)
                    data_col.download_button("⬇️ Download CSV", data=csv_buf.getvalue(),
                                             file_name=f"notified_orders_{r_date}.csv",
                                             mime="text/csv")

                    if plot_orders:
                        log(f"Plotting {len(plot_orders)} notified orders…")
                        rep_fig = make_report_plot(plot_orders)
                        if rep_fig:
                            data_col.plotly_chart(rep_fig, use_container_width=True, key="fig_report_live")

                    log.done("Report complete ✓")
                    st.session_state["report_result"] = {
                        "df": df_report, "date": r_date,
                        "metrics": (total_riders, riders_with_orders, total_notified_cnt),
                        "plot_orders": plot_orders,
                    }

        elif "report_result" in st.session_state and not fetch_btn:
            r = st.session_state["report_result"]
            st.info(f"Cached report for **{r['date']}**. Click '▶ Fetch Report' to refresh.")
            tr, rwo, tn = r["metrics"]
            m1, m2, m3 = st.columns(3)
            m1.metric("Riders Punched In", tr)
            m2.metric("Riders with Orders", rwo)
            m3.metric("Total Notifications", tn)
            st.dataframe(r["df"], use_container_width=True, hide_index=True)
            csv_buf = io.StringIO()
            r["df"].to_csv(csv_buf, index=False)
            st.download_button("⬇️ Download CSV", data=csv_buf.getvalue(),
                               file_name=f"notified_orders_{r['date']}.csv", mime="text/csv")
            if r.get("plot_orders"):
                rep_fig = make_report_plot(r["plot_orders"])
                if rep_fig:
                    st.plotly_chart(rep_fig, use_container_width=True, key="fig_report_cached")
