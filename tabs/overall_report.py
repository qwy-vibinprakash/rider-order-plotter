"""Tab 5 — Overall Report (day-by-day summary)."""
import io
from datetime import date, time, timedelta

import pandas as pd
import pytz
import streamlit as st

from queries.db_queries import get_regions, fetch_daily_stats

IST = pytz.timezone("Asia/Kolkata")

_OVERALL_DESC = {
    "Date":                 "Calendar date (IST)",
    "Total Orders":         "Non-draft orders in selected regions",
    "Riders Punched In":    "Unique riders with any attendance that day (all regions)",
    "Riders with Orders":   "Riders who received ≥1 order notification",
    "Riders - No Orders":   "Punched in but received zero notifications",
    "Total Notifications":  "Sum of all rider pushes (1 order can push to many riders)",
    "Notified Orders":      "Orders that reached ≥1 rider",
    "Not Notified Orders":  "Orders with no rider notifications at all",
}


def _overall_df(rows):
    return pd.DataFrame([{
        "Date":                 r["date"].strftime("%Y-%m-%d"),
        "Total Orders":         r["total_orders"],
        "Riders Punched In":    r["riders_punched_in"],
        "Riders with Orders":   r["riders_with_orders"],
        "Riders - No Orders":   r["riders_no_orders"],
        "Total Notifications":  r["total_notifications"],
        "Notified Orders":      r["notified_orders"],
        "Not Notified Orders":  r["not_notified_orders"],
    } for r in rows])


def _overall_csv(df):
    buf = io.StringIO()
    buf.write(",".join(f'"{c}"' for c in df.columns) + "\n")
    buf.write(",".join(f'"{_OVERALL_DESC.get(c, "")}"' for c in df.columns) + "\n")
    df.to_csv(buf, index=False, header=False)
    return buf.getvalue()


def render(tab):
    with tab:
        st.header("Overall Report")
        st.caption("Day-by-day summary fetched one day at a time. Riders Punched In is system-wide.")

        with st.expander("ℹ️ Column descriptions", expanded=False):
            for col, desc in _OVERALL_DESC.items():
                st.markdown(f"**{col}** — {desc}")

        with st.expander("⚙️ Report Settings", expanded="overall_result" not in st.session_state):
            ov_c1, ov_c2 = st.columns(2)
            with ov_c1:
                ov_start = st.date_input("From Date", value=date.today() - timedelta(days=6), key="ov_start")
            with ov_c2:
                ov_end = st.date_input("To Date", value=date.today(), key="ov_end")

            ov_all_regions = get_regions()
            ov_region_name_to_id = {rg[1]: rg[0] for rg in ov_all_regions}
            ov_sel_regions = st.multiselect(
                "Regions * (orders filtered; riders punched-in counts all regions)",
                list(ov_region_name_to_id.keys()), key="ov_regions",
            )
            ov_region_ids = [ov_region_name_to_id[n] for n in ov_sel_regions]
            ov_fetch_btn = st.button("▶ Fetch Report", type="primary", key="ov_fetch_btn")

        if ov_fetch_btn:
            if ov_start > ov_end:
                st.error("'From Date' must be on or before 'To Date'.")
            elif not ov_region_ids:
                st.error("Select at least one region.")
            else:
                from datetime import datetime
                total_days = (ov_end - ov_start).days + 1
                table_col, prog_col = st.columns([4, 1])
                with prog_col:
                    st.markdown("**Progress**")
                    prog_bar = st.progress(0)
                    prog_log = st.empty()

                df_ph   = table_col.empty()
                rows    = []
                current = ov_start

                while current <= ov_end:
                    day_num = (current - ov_start).days + 1
                    prog_log.markdown(f"🔄 {current}  ({day_num}/{total_days})")
                    prog_bar.progress(day_num / total_days)

                    s_utc = IST.localize(datetime.combine(current, time(0, 0))).astimezone(pytz.utc)
                    e_utc = IST.localize(datetime.combine(current + timedelta(days=1), time(0, 0))).astimezone(pytz.utc)

                    rows.append(fetch_daily_stats(current, s_utc, e_utc, ov_region_ids))
                    df_ph.dataframe(_overall_df(rows), use_container_width=True, hide_index=True)
                    current += timedelta(days=1)

                prog_log.markdown("✅ Done")
                prog_bar.progress(1.0)

                final_df = _overall_df(rows)
                m1, m2, m3, m4 = table_col.columns(4)
                m1.metric("Total Orders",        int(final_df["Total Orders"].sum()))
                m2.metric("Notified Orders",     int(final_df["Notified Orders"].sum()))
                m3.metric("Not Notified",        int(final_df["Not Notified Orders"].sum()))
                m4.metric("Total Notifications", int(final_df["Total Notifications"].sum()))

                table_col.download_button(
                    "⬇️ Download CSV", data=_overall_csv(final_df),
                    file_name=f"overall_{ov_start}_{ov_end}.csv", mime="text/csv",
                )

                st.session_state["overall_result"] = {
                    "df": final_df, "start": ov_start, "end": ov_end,
                    "metrics": (
                        int(final_df["Total Orders"].sum()),
                        int(final_df["Notified Orders"].sum()),
                        int(final_df["Not Notified Orders"].sum()),
                        int(final_df["Total Notifications"].sum()),
                    ),
                }

        elif "overall_result" in st.session_state and not ov_fetch_btn:
            r = st.session_state["overall_result"]
            st.info(f"Cached report **{r['start']} → {r['end']}**. Click '▶ Fetch Report' to refresh.")
            tot_ord, not_ord, nn_ord, tot_notif = r["metrics"]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Orders",        tot_ord)
            m2.metric("Notified Orders",     not_ord)
            m3.metric("Not Notified",        nn_ord)
            m4.metric("Total Notifications", tot_notif)
            st.dataframe(r["df"], use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Download CSV", data=_overall_csv(r["df"]),
                file_name=f"overall_{r['start']}_{r['end']}.csv", mime="text/csv",
            )
