"""
Tab 8 — TPL Monitor (outbound: QWQER → TEV / Yumove / EK Bharat).
Select one provider at a time. Provider list fetched live from ELK.
Per-provider error detection is specific to each provider's API format.
"""
import io
import json
from datetime import date, time, timedelta

import pandas as pd
import streamlit as st

from config import get_es_client
from queries.es_queries import (
    tpl_fetch_providers,
    tpl_columnar_query,
    _TPL_PROVIDER_LABELS,
)


def _render_error_detail(error_detail, key_prefix):
    if not error_detail:
        return
    st.subheader("Error Breakdown")
    total_errs = sum(e["count"] for e in error_detail)
    st.markdown(f"**{total_errs:,} errors** across **{len(error_detail)}** distinct type(s)")

    with st.expander("Sample Responses per Error Type", expanded=False):
        for err in error_detail:
            sev = "🔴" if err["count"] > 100 else "🟠" if err["count"] > 10 else "🟡"
            st.markdown(f"**{sev} {err['label']}** — {err['count']:,}")
            for j, s in enumerate(err.get("samples", [])):
                body = s.get("response_body", "")
                ts   = s.get("@timestamp", "")
                url  = s.get("request_url", "")
                sc   = s.get("response_status_code", "")
                st.caption(f"Sample {j+1} · {ts} · {url} · HTTP {sc}")
                if body:
                    try:
                        st.json(json.loads(body) if isinstance(body, str) else body)
                    except Exception:
                        st.code(str(body)[:400], language="text")
                else:
                    st.caption("_(no response body)_")
                if j < len(err.get("samples", [])) - 1:
                    st.divider()
            st.divider()


def render(tab):
    with tab:
        st.subheader("TPL Monitor")
        st.caption(
            "Outbound calls QWQER → external delivery providers. "
            "Select one provider at a time. Provider list fetched live from ELK (ENV=prod)."
        )

        es_client, _ = get_es_client()
        if es_client is None:
            st.warning("Elasticsearch not configured. Add credentials to `config.json`.")
            return

        live_providers = tpl_fetch_providers()

        with st.form("tpl_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                tpl_date_from = st.date_input("From Date", value=date.today() - timedelta(days=1), key="tpl_date_from")
                tpl_date_to   = st.date_input("To Date",   value=date.today(),                    key="tpl_date_to")
            with c2:
                tpl_time_from = st.time_input("From (IST)", value=time(0, 0),   key="tpl_time_from")
                tpl_time_to   = st.time_input("To (IST)",   value=time(23, 59), key="tpl_time_to")
            with c3:
                tpl_provider = st.selectbox(
                    "Provider",
                    live_providers,
                    key="tpl_provider",
                    format_func=lambda x: _TPL_PROVIDER_LABELS.get(x, x),
                )
            tpl_fetch_btn = st.form_submit_button("▶ Fetch", type="primary")

        if tpl_fetch_btn:
            if tpl_date_from > tpl_date_to:
                st.error("'From Date' must be on or before 'To Date'.")
            else:
                import pytz
                from datetime import datetime
                IST = pytz.timezone("Asia/Kolkata")
                s_utc = IST.localize(datetime.combine(tpl_date_from, tpl_time_from)).astimezone(pytz.utc).isoformat()
                e_utc = IST.localize(datetime.combine(tpl_date_to,   tpl_time_to  )).astimezone(pytz.utc).isoformat()

                prov_label = _TPL_PROVIDER_LABELS.get(tpl_provider, tpl_provider)
                with st.spinner(f"Querying {prov_label}…"):
                    op_rows, error_detail, meta, err = tpl_columnar_query(s_utc, e_utc, tpl_provider)

                if err:
                    st.error(f"Query failed: {err}")
                else:
                    st.session_state["tpl_result"] = {
                        "op_rows":       op_rows,
                        "error_detail":  error_detail,
                        "meta":          meta,
                        "provider":      tpl_provider,
                        "provider_label": prov_label,
                        "from":          tpl_date_from,
                        "to":            tpl_date_to,
                    }

        if "tpl_result" in st.session_state and not tpl_fetch_btn:
            r = st.session_state["tpl_result"]
            st.info(f"Showing **{r['provider_label']}** · {r['from']} → {r['to']}. Click Fetch to refresh.")

        if "tpl_result" in st.session_state:
            r            = st.session_state["tpl_result"]
            meta         = r["meta"]
            op_rows      = r["op_rows"]
            error_detail = r["error_detail"]
            prov_label   = r["provider_label"]

            # ── Summary metrics ──────────────────────────────────────────────
            m1, m2, m3, m4 = st.columns(4)
            suc_pct = round(meta["success"] / meta["total"] * 100, 1) if meta["total"] else 0
            m1.metric("Total Calls",  f"{meta['total']:,}")
            m2.metric("Success",      f"{meta['success']:,}")
            m3.metric("Errors",       f"{meta['failed']:,}")
            m4.metric("Success Rate", f"{suc_pct}%")

            # ── Per-API breakdown with individual error splits ────────────────
            if op_rows:
                st.subheader("Per-API Breakdown")
                st.caption(
                    "Each row = one API endpoint. Error columns are individual reason codes / "
                    "messages — only non-zero columns appear. Scroll right for error details."
                )
                df_op = pd.DataFrame(op_rows).fillna(0)
                for col in df_op.columns:
                    if col not in ("Operation", "URL", "Success %"):
                        try:
                            df_op[col] = df_op[col].astype(int)
                        except Exception:
                            pass
                st.dataframe(
                    df_op, use_container_width=True, hide_index=True,
                    column_config={"Success %": st.column_config.NumberColumn(format="%.1f%%")},
                )

                buf = io.StringIO()
                df_op.to_csv(buf, index=False)
                st.download_button(
                    "⬇️ Download CSV",
                    data=buf.getvalue(),
                    file_name=f"tpl_{r['provider']}_{r['from']}_{r['to']}.csv",
                    mime="text/csv",
                )

            # ── Error sample panel ───────────────────────────────────────────
            _render_error_detail(error_detail, key_prefix=f"tpl_{r['provider']}")
