"""
DMS Support Tool — entry point.
Each tab is implemented in tabs/. Queries are in queries/.
"""

import streamlit as st

from config import load_config

st.set_page_config(page_title="DMS Support Tool", layout="wide")


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------

def _check_password() -> bool:
    if st.session_state.get("_authenticated"):
        return True

    try:
        expected = st.secrets["app_password"]
    except Exception:
        cfg = load_config()
        expected = cfg.get("app_password")

    if not expected:
        return True

    st.title("DMS Support Tool")
    st.markdown("---")
    col, _ = st.columns([1, 2])
    with col:
        pwd = st.text_input("Password", type="password", key="_pwd_input")
        if st.button("Login"):
            if pwd == expected:
                st.session_state["_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()

st.title("DMS Support Tool")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

(
    tab_plot, tab_report, tab_coverage, tab_presence,
    tab_overall, tab_tev, tab_3p, tab_tpl,
) = st.tabs([
    "🗺 Rider Plot",
    "📊 Notified Orders Report",
    "📋 Zone Coverage",
    "👥 Rider Presence",
    "📈 Overall Report",
    "📡 TEV Dashboard",
    "🌐 Third-Party API",
    "🚚 TPL Monitor",
])

# Import and render each tab
from tabs.rider_plot       import render as render_rider_plot
from tabs.notified_report  import render as render_notified_report
from tabs.zone_coverage    import render as render_zone_coverage
from tabs.rider_presence   import render as render_rider_presence
from tabs.overall_report   import render as render_overall_report
from tabs.tev_dashboard    import render as render_tev
from tabs.third_party      import render as render_third_party
from tabs.tpl_monitor      import render as render_tpl

render_rider_plot(tab_plot)
render_notified_report(tab_report)
render_zone_coverage(tab_coverage)
render_rider_presence(tab_presence)
render_overall_report(tab_overall)
render_tev(tab_tev)
render_third_party(tab_3p)
render_tpl(tab_tpl)
