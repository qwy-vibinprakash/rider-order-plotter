"""Tab 6 — TEV Dashboard (outbound: QWQER → TEV white-label DMS, via ES TPL logs)."""
import io
from datetime import date, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import get_es_client
from queries.es_queries import tev_query, _TEV_SECTIONS, _TEV_ERROR_LABELS


def _bar_chart(rows, section_name, interval):
    labels       = [r["ts"] for r in rows]
    success_vals = [r["success"] for r in rows]
    failed_vals  = [r["failed"]  for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Success", x=labels, y=success_vals, marker_color="#2ca02c",
        hovertemplate="%{x}<br>Success: %{y}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Failed", x=labels, y=failed_vals, marker_color="#d62728",
        hovertemplate="%{x}<br>Failed: %{y}<extra></extra>",
    ))
    tick_fmt = "%d %b %H:%M" if interval == "1h" else "%d %b %Y"
    fig.update_layout(
        barmode="stack",
        title=f"{section_name} — {'Hourly' if interval == '1h' else 'Daily'} Volume",
        xaxis_title="Time (IST)", yaxis_title="Requests",
        xaxis=dict(tickformat=tick_fmt),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=380, plot_bgcolor="white", paper_bgcolor="white",
    )
    return fig


def _failure_table(rows, error_labels):
    combined = {}
    for r in rows:
        for code, cnt in r["failure_breakdown"].items():
            combined[code] = combined.get(code, 0) + cnt
    if not combined:
        return None
    return pd.DataFrame([
        {"Error Code": code, "Reason": error_labels.get(code, code), "Count": cnt}
        for code, cnt in sorted(combined.items(), key=lambda x: -x[1])
    ])


def render(tab):
    with tab:
        st.header("TEV Dashboard — Tevhrsolutions External DMS")
        st.caption("Outbound TPL API calls QWQER → TEV, pulled from Elasticsearch (ENV=prod).")

        es_client, _ = get_es_client()
        if es_client is None:
            st.warning("Elasticsearch not configured. Add credentials to `config.json`.")
            return

        with st.expander("⚙️ Dashboard Settings", expanded="tev_result" not in st.session_state):
            tev_c1, tev_c2, tev_c3, tev_c4 = st.columns([2, 1, 1, 1])
            with tev_c1:
                tev_date_from = st.date_input("From Date", value=date.today() - timedelta(days=1), key="tev_date_from")
                tev_date_to   = st.date_input("To Date",   value=date.today(),                    key="tev_date_to")
            with tev_c2:
                tev_time_from = st.time_input("From (IST)", value=time(0, 0),   key="tev_time_from")
            with tev_c3:
                tev_time_to   = st.time_input("To (IST)",   value=time(23, 59), key="tev_time_to")
            with tev_c4:
                tev_interval = st.radio("Granularity", ["Hour", "Day"], key="tev_interval", horizontal=True)

            tev_sections = st.multiselect(
                "Sections",
                list(_TEV_SECTIONS.keys()),
                default=list(_TEV_SECTIONS.keys()),
                key="tev_sections",
            )
            tev_fetch_btn = st.form_submit_button("▶ Fetch TEV Data", type="primary") \
                if False else st.button("▶ Fetch TEV Data", type="primary", key="tev_fetch_btn")

        if tev_fetch_btn:
            if tev_date_from > tev_date_to:
                st.error("'From Date' must be on or before 'To Date'.")
            elif not tev_sections:
                st.error("Select at least one section.")
            else:
                import pytz
                from datetime import datetime
                IST = pytz.timezone("Asia/Kolkata")
                es_interval = "1h" if tev_interval == "Hour" else "day"
                start_utc = IST.localize(datetime.combine(tev_date_from, tev_time_from)).astimezone(pytz.utc).isoformat()
                end_utc   = IST.localize(datetime.combine(tev_date_to,   tev_time_to  )).astimezone(pytz.utc).isoformat()

                results, errors = {}, {}
                with st.spinner("Querying Elasticsearch…"):
                    for section in tev_sections:
                        rows, meta = tev_query(_TEV_SECTIONS[section], start_utc, end_utc, es_interval)
                        if rows is None:
                            errors[section] = meta
                        else:
                            results[section] = {"rows": rows, "meta": meta}

                st.session_state["tev_result"] = {
                    "results":  results,
                    "errors":   errors,
                    "interval": es_interval,
                    "from":     tev_date_from,
                    "to":       tev_date_to,
                }

        if "tev_result" in st.session_state and not tev_fetch_btn:
            r = st.session_state["tev_result"]
            st.info(
                f"Data for **{r['from']} → {r['to']}** by **{'hour' if r['interval'] == '1h' else 'day'}**. "
                "Click '▶ Fetch TEV Data' to refresh."
            )

        if "tev_result" in st.session_state:
            r        = st.session_state["tev_result"]
            results  = r["results"]
            errors   = r["errors"]
            interval = r["interval"]

            for section, err in errors.items():
                st.error(f"**{section}**: ES query failed — {err}")

            all_csv_rows = []
            for section, data in results.items():
                rows = data["rows"]
                meta = data["meta"]
                err_labels = meta.get("error_labels", _TEV_ERROR_LABELS)

                st.subheader(section)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total",   meta["total"])
                m2.metric("Success", meta["success"])
                m3.metric("Failed",  meta["failed"])
                suc_rate = round(meta["success"] / meta["total"] * 100, 1) if meta["total"] else 0
                m4.metric("Success Rate", f"{suc_rate}%")

                if rows:
                    fig = _bar_chart(rows, section, interval)
                    st.plotly_chart(fig, use_container_width=True, key=f"tev_chart_{section}")

                    fail_df = _failure_table(rows, err_labels)
                    if fail_df is not None:
                        with st.expander("Failure breakdown"):
                            st.dataframe(fail_df, use_container_width=True, hide_index=True)

                    for row in rows:
                        all_csv_rows.append({
                            "Section": section, "Time": row["ts"],
                            "Total": row["total"], "Success": row["success"], "Failed": row["failed"],
                        })
                else:
                    st.info("No data in this period.")
                st.divider()

            if all_csv_rows:
                buf = io.StringIO()
                pd.DataFrame(all_csv_rows).to_csv(buf, index=False)
                st.download_button(
                    "⬇️ Download CSV", data=buf.getvalue(),
                    file_name=f"tev_dashboard_{r['from']}_{r['to']}.csv",
                    mime="text/csv",
                )
