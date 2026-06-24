"""Tab 1 — Rider Plot."""
import math
from datetime import date, time

import plotly.graph_objects as go
import pytz
import streamlit as st

from queries.db_queries import (
    SESSION_PALETTE, CATEGORY_STYLES, CHUNK_SIZE,
    resolve_rider, load_attendance_sessions_for_date,
    fetch_orders_raw, process_row, categorise,
    get_regions, get_zones,
    haversine_km, to_km_offset, circle_xy, parse_coord_lines,
)

IST = pytz.timezone("Asia/Kolkata")


def make_base_figure(title=""):
    fig = go.Figure()
    for r_km in [1, 2, 3, 5, 10, 25, 50, 100]:
        cx, cy = circle_xy(r_km)
        fig.add_trace(go.Scatter(
            x=cx, y=cy, mode="lines",
            line=dict(color="lightgrey", width=1, dash="dot"),
            showlegend=False, hoverinfo="skip",
        ))
        fig.add_annotation(x=0, y=r_km + 0.05, text=f"{r_km} km",
                           showarrow=False, font=dict(size=9, color="grey"))
    fig.update_layout(
        title=title,
        xaxis_title="← West  |  East (km) →",
        yaxis_title="← South  |  North (km) →",
        xaxis=dict(scaleanchor="y", scaleratio=1, range=[-3, 3],
                   zeroline=True, zerolinecolor="lightgrey", gridcolor="whitesmoke"),
        yaxis=dict(range=[-3, 3],
                   zeroline=True, zerolinecolor="lightgrey", gridcolor="whitesmoke"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=650, hovermode="closest", plot_bgcolor="white",
    )
    return fig


def _rider_traces(rider_points, ref_lat, ref_lng):
    traces = []
    for rp in rider_points:
        dx, dy = to_km_offset(ref_lat, ref_lng, rp["lat"], rp["lng"])
        traces.append(go.Scatter(
            x=[dx], y=[dy], mode="markers+text",
            marker=dict(symbol="star", color=rp["color"], size=20,
                        line=dict(width=1, color="white")),
            text=[rp["label"]], textposition="top center",
            name=rp["label"],
            hovertemplate=f"<b>{rp['label']}</b><br>Lat: {rp['lat']}<br>Lng: {rp['lng']}<extra></extra>",
        ))
    for i in range(0, len(rider_points) - 1, 2):
        a, b = rider_points[i], rider_points[i + 1]
        ax, ay = to_km_offset(ref_lat, ref_lng, a["lat"], a["lng"])
        bx, by = to_km_offset(ref_lat, ref_lng, b["lat"], b["lng"])
        traces.append(go.Scatter(
            x=[ax, bx], y=[ay, by], mode="lines",
            line=dict(color=a["color"], width=1.5, dash="dash"),
            showlegend=False, hoverinfo="skip",
        ))
    return traces


def rebuild_order_traces(acc_by_cat, rider_points, ref_lat, ref_lng, title=""):
    fig = make_base_figure(title)
    extra = []
    for cat_label, cat_color, cat_symbol in CATEGORY_STYLES:
        subset = acc_by_cat.get(cat_label, [])
        if not subset:
            continue
        extra.append(go.Scatter(
            x=[o["dx"] for o in subset],
            y=[o["dy"] for o in subset],
            mode="markers",
            name=f"{cat_label} ({len(subset)})",
            marker=dict(color=cat_color, symbol=cat_symbol, size=11,
                        line=dict(width=1.5, color=cat_color)),
            text=[
                f"<b>{o['order_key']}</b>  {o['time_ist']}<br>"
                f"Customer: {o['customer_name']}<br>"
                f"Pickup: {o['pickup_name']}<br>"
                f"Dist: {o['dist_km']} km  |  {o['status_label']}<br>"
                f"{'⛔ Blacklisted' if o['is_blacklisted'] else ('✅ Notified' if o['is_notified'] else '🔴 Not Notified')}"
                for o in subset
            ],
            hovertemplate="%{text}<extra></extra>",
        ))
    extra.extend(_rider_traces(rider_points, ref_lat, ref_lng))
    if extra:
        fig.add_traces(extra)
    all_x = [o["dx"] for orders in acc_by_cat.values() for o in orders]
    all_y = [o["dy"] for orders in acc_by_cat.values() for o in orders]
    for rp in rider_points:
        dx, dy = to_km_offset(ref_lat, ref_lng, rp["lat"], rp["lng"])
        all_x.append(dx); all_y.append(dy)
    axis_range = min(max([abs(v) for v in all_x + all_y] + [2]) * 1.2, 100)
    fig.update_layout(
        xaxis=dict(range=[-axis_range, axis_range]),
        yaxis=dict(range=[-axis_range, axis_range]),
    )
    return fig


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


def region_zone_widgets(key_prefix):
    regions   = get_regions()
    region_map = {rg[1]: rg[0] for rg in regions}
    region_sel = st.selectbox("Region (optional)", ["— All —"] + list(region_map.keys()),
                              key=f"{key_prefix}_region")
    region_id = region_map.get(region_sel) if region_sel != "— All —" else None
    zone_id = None
    if region_id:
        zones = get_zones(region_id)
        if zones:
            zone_map = {z[1]: z[0] for z in zones}
            zone_sel = st.selectbox("Zone (optional)", ["— All —"] + list(zone_map.keys()),
                                    key=f"{key_prefix}_zone")
            zone_id = zone_map.get(zone_sel) if zone_sel != "— All —" else None
        else:
            st.caption("No zones for this region.")
    return region_id, zone_id


def run_streaming_plot(rider_key, rider_db_id, rider_points, sessions_to_plot,
                       region_id, zone_id, chart_col, prog_col):
    log      = ProgressLog(prog_col)
    chart_ph = chart_col.empty()
    stop_ph  = chart_col.empty()
    stop_ph.button("⛔ Stop loading", key="stop_btn")

    ref_lat, ref_lng = rider_points[0]["lat"], rider_points[0]["lng"]
    title = f"Rider {rider_key} — {' + '.join(s['label'] for s in sessions_to_plot)}"

    log("Plotting rider location…")
    fig0 = make_base_figure(title)
    fig0.add_traces(_rider_traces(rider_points, ref_lat, ref_lng))
    chart_ph.plotly_chart(fig0, use_container_width=True, key="fig_rider_only")

    all_raw = []
    for sess in sessions_to_plot:
        log(f"Querying DB for {sess['label']}…")
        batch = fetch_orders_raw(rider_db_id, sess["start_utc"], sess["end_utc"], region_id, zone_id)
        log(f"{len(batch)} orders in {sess['label']}")
        all_raw.extend(batch)

    seen = set()
    unique_raw = []
    for r in all_raw:
        if r["order_key"] not in seen:
            seen.add(r["order_key"])
            unique_raw.append(r)

    total = len(unique_raw)
    if total == 0:
        chart_col.warning("No orders found for selected sessions.")
        log.warn("No orders — nothing to plot")
        return

    log(f"Found {total} orders — streaming to chart…")
    acc_by_cat = {"Notified": [], "Not Notified": [], "Blacklisted Customer": []}
    all_orders = []

    for chunk_start in range(0, total, CHUNK_SIZE):
        chunk = unique_raw[chunk_start: chunk_start + CHUNK_SIZE]
        for r in chunk:
            o = process_row(r, rider_db_id)
            o["dx"], o["dy"] = to_km_offset(ref_lat, ref_lng, o["pickup_lat"], o["pickup_lng"])
            dists = [haversine_km(rp["lat"], rp["lng"], o["pickup_lat"], o["pickup_lng"])
                     for rp in rider_points]
            o["dist_km"]  = round(min(dists), 3)
            o["category"] = categorise(o)
            acc_by_cat[o["category"]].append(o)
            all_orders.append(o)

        loaded = chunk_start + len(chunk)
        log(f"Plotting {loaded}/{total}…")
        st.session_state["plot_result"] = {
            "orders":        list(all_orders),
            "rider_points":  rider_points,
            "rider_key":     rider_key,
            "session_label": title,
            "partial":       loaded < total,
        }
        fig = rebuild_order_traces(acc_by_cat, rider_points, ref_lat, ref_lng, title)
        chart_ph.plotly_chart(fig, use_container_width=True, key=f"fig_chunk_{loaded}")

    stop_ph.empty()
    log.done(f"All {total} orders plotted ✓")
    st.session_state["plot_result"]["partial"] = False


def render(tab):
    with tab:
        with st.expander("⚙️ Plot Settings", expanded="plot_result" not in st.session_state):
            inp_col, _, filt_col = st.columns([2, 0.2, 1])

            with inp_col:
                p_rider_key = st.text_input("Rider Key *", key="p_rider_key", placeholder="e.g. 94702")
                p_source = st.radio("Location source", ["Manual Entry", "Fetch from Attendance"],
                                    key="p_source", horizontal=True)

                if p_source == "Manual Entry":
                    p_coords = st.text_area(
                        "Coordinates — one lat, lng per line",
                        placeholder="12.345678, 77.654321\n12.456789, 77.789012",
                        height=110, key="p_coords",
                    )
                    p_date = st.date_input("Date", value=date.today(), key="p_date")
                    c1, c2 = st.columns(2)
                    p_from = c1.time_input("From (IST)", value=time(7, 0), key="p_from")
                    p_to   = c2.time_input("To (IST)",   value=time(7, 30), key="p_to")
                else:
                    p_att_date = st.date_input("Attendance date", value=date.today(), key="p_att_date")
                    load_btn = st.button("Load Sessions", key="load_sessions_btn")

                    if load_btn:
                        if not p_rider_key.strip():
                            st.error("Enter a rider key first.")
                        else:
                            rider_db_id_tmp = resolve_rider(p_rider_key)
                            if not rider_db_id_tmp:
                                st.error(f"Rider key {p_rider_key!r} not found.")
                            else:
                                with st.spinner("Loading sessions…"):
                                    rows = load_attendance_sessions_for_date(rider_db_id_tmp, p_att_date)
                                if not rows:
                                    st.warning(f"No attendance on {p_att_date} for rider {p_rider_key}.")
                                else:
                                    sessions = []
                                    for i, r in enumerate(rows):
                                        pin_str  = r["pin_ist"].strftime("%H:%M")  if r["pin_ist"]  else "?"
                                        pout_str = r["pout_ist"].strftime("%H:%M") if r["pout_ist"] else "active"
                                        sessions.append({
                                            "label":       f"Session {i+1}: {pin_str} → {pout_str}",
                                            "pin_utc":     r["pin_utc"],
                                            "pout_utc":    r["pout_utc"],
                                            "pin_coords":  r["punch_in_coordinates"],
                                            "pout_coords": r["punch_out_coordinates"],
                                            "color":       SESSION_PALETTE[i % len(SESSION_PALETTE)],
                                        })
                                    st.session_state.update({
                                        "att_sessions":       sessions,
                                        "att_sessions_rider": p_rider_key,
                                        "att_sessions_date":  p_att_date,
                                        "att_rider_db_id":    rider_db_id_tmp,
                                    })

                    att_ready = (
                        "att_sessions" in st.session_state
                        and st.session_state.get("att_sessions_rider") == p_rider_key
                        and st.session_state.get("att_sessions_date") == p_att_date
                    )
                    if att_ready:
                        sessions_loaded = st.session_state["att_sessions"]
                        session_labels  = [s["label"] for s in sessions_loaded]
                        st.success(f"{len(sessions_loaded)} session(s) — select which to plot:")
                        p_sel_labels = st.multiselect("Sessions", session_labels,
                                                      default=session_labels, key="p_sel_sessions")

            with filt_col:
                st.markdown("**Filters (optional)**")
                p_region_id, p_zone_id = region_zone_widgets("plot")

            plot_btn = st.button("▶ Plot Orders", type="primary", key="plot_btn")

        if plot_btn:
            errors = []
            if not p_rider_key.strip():
                errors.append("Rider key is required.")

            rider_points     = []
            sessions_to_plot = []

            if p_source == "Manual Entry":
                if p_from >= p_to:
                    errors.append("'From' must be before 'To'.")
                if not p_coords.strip():
                    errors.append("Coordinates are required.")
                else:
                    try:
                        parsed = parse_coord_lines(p_coords)
                        if not parsed:
                            errors.append("No valid coordinates found.")
                        for i, (lat, lng) in enumerate(parsed):
                            rider_points.append({
                                "label": f"Pt {i+1} ({lat}, {lng})",
                                "lat": lat, "lng": lng,
                                "color": SESSION_PALETTE[i % len(SESSION_PALETTE)],
                            })
                        from datetime import datetime
                        sessions_to_plot.append({
                            "start_utc": IST.localize(datetime.combine(p_date, p_from)).astimezone(pytz.utc),
                            "end_utc":   IST.localize(datetime.combine(p_date, p_to)).astimezone(pytz.utc),
                            "label":     f"{p_from.strftime('%H:%M')}–{p_to.strftime('%H:%M')} IST",
                        })
                    except ValueError as e:
                        errors.append(str(e))

                if not errors:
                    rider_db_id = resolve_rider(p_rider_key)
                    if not rider_db_id:
                        errors.append(f"Rider key {p_rider_key!r} not found in DB.")
            else:
                att_ready = (
                    "att_sessions" in st.session_state
                    and st.session_state.get("att_sessions_rider") == p_rider_key
                    and st.session_state.get("att_sessions_date") == p_att_date
                )
                if not att_ready:
                    errors.append("Load sessions first.")
                elif not p_sel_labels:
                    errors.append("Select at least one session.")
                else:
                    rider_db_id = st.session_state["att_rider_db_id"]
                    selected_sessions = [s for s in st.session_state["att_sessions"]
                                         if s["label"] in p_sel_labels]
                    for s in selected_sessions:
                        if s["pin_coords"] and s["pin_coords"].get("latitude"):
                            rider_points.append({
                                "label": f"In {s['label']}",
                                "lat": float(s["pin_coords"]["latitude"]),
                                "lng": float(s["pin_coords"]["longitude"]),
                                "color": s["color"],
                            })
                        if s["pout_coords"] and s["pout_coords"].get("latitude"):
                            rider_points.append({
                                "label": f"Out {s['label']}",
                                "lat": float(s["pout_coords"]["latitude"]),
                                "lng": float(s["pout_coords"]["longitude"]),
                                "color": s["color"],
                            })
                        sessions_to_plot.append({
                            "start_utc": s["pin_utc"],
                            "end_utc":   s["pout_utc"] or __import__("datetime").datetime.now(pytz.utc),
                            "label":     s["label"],
                        })

            for e in errors:
                st.error(e)

            if not errors and rider_points and sessions_to_plot:
                chart_col, prog_col = st.columns([4, 1])
                with prog_col:
                    st.markdown("**Progress**")
                run_streaming_plot(
                    p_rider_key, rider_db_id, rider_points, sessions_to_plot,
                    p_region_id, p_zone_id, chart_col, prog_col,
                )

        elif "plot_result" in st.session_state:
            r = st.session_state["plot_result"]
            if r.get("partial"):
                st.warning(f"Partial load — showing {len(r['orders'])} orders. Click 'Plot Orders' to reload.")
            else:
                st.info(f"Showing result for Rider **{r['rider_key']}**. Click '▶ Plot Orders' to refresh.")

            if r["orders"]:
                ref_lat = r["rider_points"][0]["lat"]
                ref_lng = r["rider_points"][0]["lng"]
                acc_by_cat = {"Notified": [], "Not Notified": [], "Blacklisted Customer": []}
                for o in r["orders"]:
                    acc_by_cat[o["category"]].append(o)
                fig = rebuild_order_traces(acc_by_cat, r["rider_points"], ref_lat, ref_lng, r["session_label"])
                st.plotly_chart(fig, use_container_width=True, key="fig_cached")
