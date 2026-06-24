"""Tab 3 — Zone Coverage."""
import io
from datetime import date, time

import pandas as pd
import pytz
import streamlit as st

from queries.db_queries import (
    get_regions, get_zones_for_regions,
    fetch_zone_coverage,
)

IST = pytz.timezone("Asia/Kolkata")


def render(tab):
    with tab:
        st.header("Zone Coverage Report")
        st.caption("Order notification coverage by region/zone. Only draft_order=false orders counted.")

        with st.expander("⚙️ Coverage Settings", expanded="coverage_result" not in st.session_state):
            cov_c1, cov_c2, cov_c3 = st.columns([2, 1, 1])
            with cov_c1:
                cov_date = st.date_input("Date", value=date.today(), key="cov_date")
            with cov_c2:
                cov_from = st.time_input("From (IST)", value=time(0, 0), key="cov_from")
            with cov_c3:
                cov_to = st.time_input("To (IST)", value=time(23, 59), key="cov_to")

            all_regions = get_regions()
            region_name_to_id = {rg[1]: rg[0] for rg in all_regions}
            cov_sel_regions = st.multiselect("Regions *", list(region_name_to_id.keys()), key="cov_regions")
            cov_region_ids  = [region_name_to_id[n] for n in cov_sel_regions]

            cov_zone_ids = []
            if cov_region_ids:
                all_zones = get_zones_for_regions(tuple(cov_region_ids))
                region_id_to_name = {rg[0]: rg[1] for rg in all_regions}
                zone_options = {
                    f"{z[1]} ({region_id_to_name.get(z[2], z[2])})": z[0]
                    for z in all_zones
                }
                if zone_options:
                    cov_sel_zones = st.multiselect(
                        "Zones (leave empty for all zones in selected regions)",
                        list(zone_options.keys()), key="cov_zones",
                    )
                    cov_zone_ids = [zone_options[n] for n in cov_sel_zones]
                else:
                    st.caption("No zones found for selected regions.")

            cov_fetch_btn = st.button("▶ Fetch Coverage", type="primary", key="cov_fetch_btn")

        cov_sort = st.radio(
            "Sort by Coverage %",
            ["Default (Region → Zone)", "Lowest coverage first", "Highest coverage first"],
            horizontal=True, key="cov_sort",
        )

        cov_view_regions = None
        if "coverage_result" in st.session_state:
            _avail = sorted(st.session_state["coverage_result"]["df"]["Region"].unique().tolist())
            if len(_avail) > 1:
                cov_view_regions = st.multiselect("View by Region", _avail, default=_avail, key="cov_view_regions")

        if cov_fetch_btn:
            if cov_from >= cov_to:
                st.error("'From' must be before 'To'.")
            elif not cov_region_ids:
                st.error("Select at least one region.")
            else:
                from datetime import datetime
                cov_start_utc = IST.localize(datetime.combine(cov_date, cov_from)).astimezone(pytz.utc)
                cov_end_utc   = IST.localize(datetime.combine(cov_date, cov_to)).astimezone(pytz.utc)

                with st.spinner("Fetching coverage data…"):
                    cov_rows = fetch_zone_coverage(cov_start_utc, cov_end_utc, cov_region_ids, cov_zone_ids)

                if not cov_rows:
                    st.warning("No orders found for the selected filters.")
                else:
                    df_cov = pd.DataFrame([{
                        "Region":       r["region_name"],
                        "Zone":         r["zone_name"],
                        "Total Orders": int(r["total_orders"]),
                        "Notified":     int(r["notified"]),
                        "Not Notified": int(r["not_notified"]),
                        "Coverage %":   round(int(r["notified"]) / int(r["total_orders"]) * 100, 1)
                                        if int(r["total_orders"]) > 0 else 0.0,
                    } for r in cov_rows])

                    totals = {
                        "Total Orders": df_cov["Total Orders"].sum(),
                        "Notified":     df_cov["Notified"].sum(),
                        "Not Notified": df_cov["Not Notified"].sum(),
                        "Coverage %":   round(df_cov["Notified"].sum() / df_cov["Total Orders"].sum() * 100, 1)
                                        if df_cov["Total Orders"].sum() > 0 else 0.0,
                    }
                    st.session_state["coverage_result"] = {"df": df_cov, "date": cov_date, "totals": totals}

        def _show_cov(df_cov, totals, date_label, view_regions):
            t1, t2, t3, t4 = st.columns(4)
            t1.metric("Total Orders",  int(totals["Total Orders"]))
            t2.metric("Notified",      int(totals["Notified"]))
            t3.metric("Not Notified",  int(totals["Not Notified"]))
            t4.metric("Coverage %",    f"{totals['Coverage %']}%")

            if cov_sort == "Lowest coverage first":
                df_display = df_cov.sort_values("Coverage %", ascending=True)
            elif cov_sort == "Highest coverage first":
                df_display = df_cov.sort_values("Coverage %", ascending=False)
            else:
                df_display = df_cov
            if view_regions:
                df_display = df_display[df_display["Region"].isin(view_regions)]

            st.dataframe(df_display, use_container_width=True, hide_index=True)
            cov_csv = io.StringIO()
            df_display.to_csv(cov_csv, index=False)
            st.download_button("⬇️ Download CSV", data=cov_csv.getvalue(),
                               file_name=f"zone_coverage_{date_label}.csv", mime="text/csv")

        if cov_fetch_btn and "coverage_result" in st.session_state:
            r = st.session_state["coverage_result"]
            _show_cov(r["df"], r["totals"], r["date"], cov_view_regions)
        elif "coverage_result" in st.session_state and not cov_fetch_btn:
            r = st.session_state["coverage_result"]
            st.info(f"Cached coverage for **{r['date']}**. Click '▶ Fetch Coverage' to refresh.")
            _show_cov(r["df"], r["totals"], r["date"], cov_view_regions)
