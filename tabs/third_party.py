"""
Tab 7 — Third-Party API Monitor (inbound: merchants → QWQER DMS).

Layout:
  - Quote + Create Order: always shown side by side with sub-merchant filter active
  - Other APIs (Cancel, Modify, etc.): one at a time via dropdown, sub-merchant filter disabled
  - Each API: table of rows=merchants × cols=(Total, Success, per-QE-code..., No Body, Unknown)
"""
import io
import json
from datetime import date, time, timedelta

import pandas as pd
import streamlit as st

from config import get_es_client
from queries.db_queries import fetch_merchant_hierarchy
from queries.es_queries import (
    tp_columnar_query,
    _TP_PATHS,
    _TP_SUBMERCHANT_OPS,
    _ALL_QE_CODES,
)

_COMBO_OPS = ["Quote", "Create Order"]
_OTHER_OPS = [op for op in _TP_PATHS if op not in _COMBO_OPS]

# Only monitor these key merchants — others are low-volume or internal
_KEY_MERCHANT_PATTERNS = ["pidge", "adloggs", "prorouting", "shiprocket", "uengage"]


def _filter_key_merchants(hier):
    """Return only hierarchy entries whose parent name matches a key merchant pattern."""
    return {
        name: subs
        for name, subs in hier.items()
        if any(p in name.lower() for p in _KEY_MERCHANT_PATTERNS)
    }


def _render_samples(err_ref):
    if not err_ref:
        return
    with st.expander(f"Error Reference — {len(err_ref)} error type(s)", expanded=False):
        for err in err_ref:
            sev = "🔴" if err["count"] > 100 else "🟠" if err["count"] > 10 else "🟡"
            st.markdown(f"**{sev} {err['code']}: {err['label']}** — {err['count']:,} occurrences")
            for j, s in enumerate(err.get("samples", [])):
                body = s.get("response_body", "")
                ts   = s.get("@timestamp", "")
                who  = s.get("user", "")
                path = s.get("request_path", "")
                st.caption(f"Sample {j+1} · {ts} · {who} · {path}")
                if body:
                    try:
                        st.json(json.loads(body) if isinstance(body, str) else body)
                    except Exception:
                        st.code(str(body)[:400], language="text")
                else:
                    st.caption("_(no response body)_")
            st.divider()


def _render_op_result(op, op_data):
    if "error" in op_data:
        st.error(f"**{op}**: {op_data['error']}")
        return

    rows    = op_data["rows"]
    meta    = op_data["meta"]
    err_ref = op_data["error_ref"]

    st.markdown(f"### {op}")
    m1, m2, m3, m4 = st.columns(4)
    suc_pct = round(meta["success"] / meta["total"] * 100, 1) if meta["total"] else 0
    m1.metric("Total Requests", f"{meta['total']:,}")
    m2.metric("Success",        f"{meta['success']:,}")
    m3.metric("Failed",         f"{meta['failed']:,}")
    m4.metric("Success Rate",   f"{suc_pct}%")

    if rows:
        df = pd.DataFrame(rows).fillna(0)
        for col in df.columns:
            if col not in ("Merchant", "Success %"):
                df[col] = df[col].astype(int)
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={"Success %": st.column_config.NumberColumn(format="%.1f%%")},
        )
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        st.download_button(
            f"⬇️ Download {op} CSV", data=buf.getvalue(),
            file_name=f"3p_{op.lower().replace(' ', '_')}.csv",
            mime="text/csv", key=f"tp_dl_{op}",
        )
    else:
        st.info("No data for this operation in the selected period.")

    _render_samples(err_ref)


def render(tab):
    with tab:
        st.subheader("Third-Party API Monitor")
        st.caption(
            "Inbound API calls from merchants/clients to QWQER DMS. "
            "**Quote + Create** always shown together (sub-merchant filter active). "
            "Other APIs: one at a time (sub-merchant filter disabled)."
        )

        es_client, _ = get_es_client()
        if es_client is None:
            st.warning("Elasticsearch not configured. Add credentials to `config.json`.")
            return

        # ── Merchant filter (key merchants only) ─────────────────────────────
        full_hier = fetch_merchant_hierarchy()
        hier      = _filter_key_merchants(full_hier)
        parents   = sorted(hier.keys())

        col_a, col_b = st.columns([1, 2])
        with col_a:
            tp_parents_sel = st.multiselect(
                "Parent Merchant(s)",
                parents,
                default=parents,   # default = all key merchants selected
                key="tp_parents",
                help="Pidge · Adloggs · Prorouting · Shiprocket · Uengage",
            )
        with col_b:
            # Sub-merchants only shown when exactly one parent is selected
            subs_available = []
            for p in (tp_parents_sel or parents):
                subs_available.extend(hier.get(p, []))
            subs_available = sorted(set(subs_available))
            tp_subs_sel = st.multiselect(
                "Sub-Merchant(s)  _(Quote + Create only)_",
                subs_available,
                default=subs_available,
                key="tp_subs",
                help="Sub-merchants of selected parents.",
            )

        # For Quote/Create: filter on both parents AND their sub-merchants
        # For other APIs: filter on parent names only (sub-merchant field is irrelevant)
        merchant_filter_combo = list(set(tp_parents_sel) | set(tp_subs_sel)) or None
        merchant_filter_other = list(set(tp_parents_sel)) or None

        # ── Other API selector ───────────────────────────────────────────────
        other_api = st.selectbox(
            "Other API to analyse (sub-merchant filter disabled for these)",
            ["— None —"] + _OTHER_OPS,
            key="tp_other_op",
        )

        # ── Date / time form ─────────────────────────────────────────────────
        with st.form("tp_form"):
            c1, c2 = st.columns(2)
            with c1:
                tp_date_from = st.date_input("From Date", value=date.today() - timedelta(days=1), key="tp_date_from")
                tp_date_to   = st.date_input("To Date",   value=date.today(),                    key="tp_date_to")
            with c2:
                tp_time_from = st.time_input("From (IST)", value=time(0, 0),   key="tp_time_from")
                tp_time_to   = st.time_input("To (IST)",   value=time(23, 59), key="tp_time_to")

            tp_fetch_btn = st.form_submit_button("▶ Fetch", type="primary")

        if tp_fetch_btn:
            if tp_date_from > tp_date_to:
                st.error("'From Date' must be on or before 'To Date'.")
            else:
                import pytz
                from datetime import datetime
                IST = pytz.timezone("Asia/Kolkata")
                s_utc = IST.localize(datetime.combine(tp_date_from, tp_time_from)).astimezone(pytz.utc).isoformat()
                e_utc = IST.localize(datetime.combine(tp_date_to,   tp_time_to  )).astimezone(pytz.utc).isoformat()

                ops_to_run = list(_COMBO_OPS)
                merchant_map = {op: merchant_filter_combo for op in _COMBO_OPS}

                if other_api and other_api != "— None —":
                    ops_to_run.append(other_api)
                    merchant_map[other_api] = merchant_filter_other

                # Run one query per distinct merchant filter set
                result_combo, result_other = {}, {}
                with st.spinner("Querying Elasticsearch…"):
                    data_combo, err = tp_columnar_query(s_utc, e_utc, _COMBO_OPS, merchant_filter_combo)
                    if data_combo:
                        result_combo = data_combo

                    if other_api and other_api != "— None —":
                        data_other, err2 = tp_columnar_query(s_utc, e_utc, [other_api], merchant_filter_other)
                        if data_other:
                            result_other = data_other

                st.session_state["tp_result"] = {
                    "combo":      result_combo,
                    "other":      result_other,
                    "other_api":  other_api,
                    "parents":    tp_parents_sel,
                    "subs":       tp_subs_sel,
                    "from":       tp_date_from,
                    "to":         tp_date_to,
                }

        if "tp_result" in st.session_state and not tp_fetch_btn:
            r = st.session_state["tp_result"]
            st.info(
                f"{r['from']} → {r['to']}  ·  "
                f"{'|'.join(r['parents']) or 'All Merchants'}. "
                "Click Fetch to refresh."
            )

        if "tp_result" in st.session_state:
            r = st.session_state["tp_result"]

            st.markdown("---")
            st.markdown("#### Quote + Create Order")
            for op in _COMBO_OPS:
                if op in r["combo"]:
                    _render_op_result(op, r["combo"][op])

            if r.get("other_api") and r["other_api"] != "— None —":
                st.markdown("---")
                op = r["other_api"]
                if op in r["other"]:
                    st.markdown(f"#### {op}  _(sub-merchant filter disabled)_")
                    _render_op_result(op, r["other"][op])
