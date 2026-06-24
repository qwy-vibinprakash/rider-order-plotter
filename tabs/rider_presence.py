"""Tab 4 — Rider Presence by Zone."""
import io
from datetime import date, time

import pandas as pd
import pytz
import streamlit as st

from queries.db_queries import (
    get_regions, get_zones_for_regions,
    fetch_zone_centroids, fetch_punched_in_coords, fetch_punched_out_coords,
    assign_to_nearest_zone,
)

IST = pytz.timezone("Asia/Kolkata")


def render(tab):
    with tab:
        st.header("Rider Presence by Zone")
        st.caption(
            "Counts riders per zone based on punch-in / punch-out coordinates. "
            "Zone assignment uses nearest zone centroid derived from orders on the same day."
        )

        with st.expander("⚙️ Presence Settings", expanded="presence_result" not in st.session_state):
            pr_c1, pr_c2, pr_c3 = st.columns([2, 1, 1])
            with pr_c1:
                pr_date = st.date_input("Date", value=date.today(), key="pr_date")
            with pr_c2:
                pr_from = st.time_input("From (IST)", value=time(0, 0), key="pr_from")
            with pr_c3:
                pr_to = st.time_input("To (IST)", value=time(23, 59), key="pr_to")

            pr_all_regions = get_regions()
            pr_region_name_to_id = {rg[1]: rg[0] for rg in pr_all_regions}
            pr_sel_regions = st.multiselect("Regions *", list(pr_region_name_to_id.keys()), key="pr_regions")
            pr_region_ids  = [pr_region_name_to_id[n] for n in pr_sel_regions]

            pr_zone_ids = []
            if pr_region_ids:
                pr_all_zones = get_zones_for_regions(tuple(pr_region_ids))
                pr_region_id_to_name = {rg[0]: rg[1] for rg in pr_all_regions}
                pr_zone_options = {
                    f"{z[1]} ({pr_region_id_to_name.get(z[2], z[2])})": z[0]
                    for z in pr_all_zones
                }
                if pr_zone_options:
                    pr_sel_zones = st.multiselect(
                        "Zones (leave empty for all)", list(pr_zone_options.keys()), key="pr_zones",
                    )
                    pr_zone_ids = [pr_zone_options[n] for n in pr_sel_zones]
                else:
                    st.caption("No zones found for selected regions.")

            pr_fetch_btn = st.button("▶ Fetch Presence", type="primary", key="pr_fetch_btn")

        if pr_fetch_btn:
            if pr_from >= pr_to:
                st.error("'From' must be before 'To'.")
            elif not pr_region_ids:
                st.error("Select at least one region.")
            else:
                from datetime import datetime
                pr_start_utc = IST.localize(datetime.combine(pr_date, pr_from)).astimezone(pytz.utc)
                pr_end_utc   = IST.localize(datetime.combine(pr_date, pr_to)).astimezone(pytz.utc)

                with st.spinner("Fetching rider presence data…"):
                    zone_centroids = fetch_zone_centroids(pr_start_utc, pr_end_utc, pr_region_ids, pr_zone_ids)
                    in_coords      = fetch_punched_in_coords(pr_date)
                    out_coords     = fetch_punched_out_coords(pr_date)

                if not zone_centroids:
                    st.warning("No order data found to derive zone locations — try a broader time window.")
                else:
                    in_counts  = assign_to_nearest_zone(in_coords,  zone_centroids)
                    out_counts = assign_to_nearest_zone(out_coords, zone_centroids)

                    all_zones = sorted(zone_centroids.keys())
                    df_pr = pd.DataFrame([{
                        "Zone":         z,
                        "Punched In":   in_counts.get(z, 0),
                        "Punched Out":  out_counts.get(z, 0),
                        "Still Active": max(in_counts.get(z, 0) - out_counts.get(z, 0), 0),
                    } for z in all_zones])

                    total_in     = len(in_coords)
                    total_out    = len(out_coords)
                    still_active = max(total_in - total_out, 0)

                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Punched In",  total_in)
                    m2.metric("Total Punched Out", total_out)
                    m3.metric("Still Active",      still_active)

                    st.dataframe(df_pr, use_container_width=True, hide_index=True)
                    pr_csv = io.StringIO()
                    df_pr.to_csv(pr_csv, index=False)
                    st.download_button("⬇️ Download CSV", data=pr_csv.getvalue(),
                                       file_name=f"rider_presence_{pr_date}.csv", mime="text/csv")

                    st.session_state["presence_result"] = {
                        "df": df_pr, "date": pr_date,
                        "metrics": (total_in, total_out, still_active),
                    }

        elif "presence_result" in st.session_state and not pr_fetch_btn:
            r = st.session_state["presence_result"]
            st.info(f"Cached presence for **{r['date']}**. Click '▶ Fetch Presence' to refresh.")
            total_in, total_out, still_active = r["metrics"]
            m1, m2, m3 = st.columns(3)
            m1.metric("Total Punched In",  total_in)
            m2.metric("Total Punched Out", total_out)
            m3.metric("Still Active",      still_active)
            st.dataframe(r["df"], use_container_width=True, hide_index=True)
            pr_csv = io.StringIO()
            r["df"].to_csv(pr_csv, index=False)
            st.download_button("⬇️ Download CSV", data=pr_csv.getvalue(),
                               file_name=f"rider_presence_{r['date']}.csv", mime="text/csv")
