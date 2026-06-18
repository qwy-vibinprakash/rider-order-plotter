"""
DMS Support Tool
Two independent tabs — Rider Plot and Notified Orders Report.
All results stored in st.session_state so switching tabs never loses data.

Optimisations applied:
- Orders filtered on created_on (db_index=True + partial composite index) NOT created_on_original (no index)
- notified_rider_list uses the GIN index via @> operator
- Attendance query resolves rider_id first, then queries attendance by rider_id (FK index hit)
- Report query uses CTE for blacklisted customers instead of a correlated subquery per rider
- Orders fetched in one DB round-trip; chart streamed by processing in Python chunks
- Stop button saves partial state to session_state so the user can halt and keep what's loaded
"""

import io
import json
import math
import os
from datetime import date, datetime, time

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import pytz
import streamlit as st

IST = pytz.timezone("Asia/Kolkata")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CHUNK_SIZE = 75  # orders processed per chart refresh cycle

ORDER_STATUS_LABELS = {
    1: "NEW", 2: "ACCEPTED", 3: "PICKED UP", 4: "DELIVERED",
    5: "CANCELLED", 6: "UNDELIVERED", 7: "RTW", 8: "RTS",
    9: "RTAA", 10: "SCHEDULED", 11: "REACHED PICKUP",
    12: "REACHED DELIVERY", 13: "ABSCONDED",
}
SESSION_PALETTE = ["#1f77b4", "#9467bd", "#2ca02c", "#8c564b", "#e377c2", "#17becf"]
CATEGORY_STYLES = [
    ("Notified",            "green",  "circle"),
    ("Not Notified",        "red",    "circle-open"),
    ("Blacklisted Customer","orange", "x"),
]


# ---------------------------------------------------------------------------
# Config + DB
# ---------------------------------------------------------------------------

def load_config():
    # Streamlit Cloud: read from st.secrets if config.json is absent
    if hasattr(st, "secrets") and "database" in st.secrets:
        return {"database": dict(st.secrets["database"])}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        st.error("config.json not found and no [database] section in st.secrets.")
        st.stop()


@st.cache_resource
def get_db_config():
    return load_config()["database"]


def get_conn():
    cfg = get_db_config()
    return psycopg2.connect(**cfg, connect_timeout=10)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def to_km_offset(ref_lat, ref_lng, pt_lat, pt_lng):
    dx = haversine_km(ref_lat, ref_lng, ref_lat, pt_lng) * (1 if float(pt_lng) >= float(ref_lng) else -1)
    dy = haversine_km(ref_lat, ref_lng, pt_lat, ref_lng) * (1 if float(pt_lat) >= float(ref_lat) else -1)
    return dx, dy


def circle_xy(radius_km, n=300):
    angles = [2 * math.pi * i / n for i in range(n + 1)]
    return [radius_km * math.cos(a) for a in angles], [radius_km * math.sin(a) for a in angles]


def parse_coord_lines(text):
    coords = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            raise ValueError(f"Bad line: {line!r} — expected 'lat, lng'")
        coords.append((float(parts[0]), float(parts[1])))
    return coords


# ---------------------------------------------------------------------------
# DB — shared
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def get_regions():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, region_name FROM orders_regions WHERE active_status = true ORDER BY region_name")
            return cur.fetchall()


@st.cache_data(ttl=300)
def get_zones(region_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM orders_zone WHERE deleted IS NULL AND region_id = %s ORDER BY name",
                (region_id,),
            )
            return cur.fetchall()


@st.cache_data(ttl=300)
def get_zones_for_regions(region_ids_tuple):
    """Returns list of (zone_id, zone_name, region_id) for multiple regions. Tuple arg for cache hashability."""
    if not region_ids_tuple:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, region_id FROM orders_zone WHERE deleted IS NULL AND region_id = ANY(%s) ORDER BY name",
                (list(region_ids_tuple),),
            )
            return cur.fetchall()


def resolve_rider(rider_key):
    """Returns (rider_db_id, rider_key) or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM rider_riderregistration WHERE rider_key = %s", (rider_key.strip(),))
            row = cur.fetchone()
            return row[0] if row else None


# ---------------------------------------------------------------------------
# DB — Rider Plot
# Optimisation: filter on created_on (db_index=True, partial composite index)
#               not created_on_original (no index).
#               notified_rider_list uses GIN index via @> operator.
# ---------------------------------------------------------------------------

def load_attendance_sessions(rider_db_id):
    """
    Optimised: resolve rider_id first (caller does it), query attendance by rider_id
    directly (FK auto-index), avoiding the join with rider_riderregistration.
    """
    sql = """
        SELECT
            punch_in_coordinates,
            punch_out_coordinates,
            punch_in_time  AT TIME ZONE 'Asia/Kolkata' AS pin_ist,
            punch_out_time AT TIME ZONE 'Asia/Kolkata' AS pout_ist,
            punch_in_time                              AS pin_utc,
            punch_out_time                             AS pout_utc
        FROM rider_riderattendancedetails
        WHERE rider_id = %s
          AND date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
        ORDER BY punch_in_time NULLS LAST
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (rider_db_id,))
            return [dict(r) for r in cur.fetchall()]


def load_attendance_sessions_for_date(rider_db_id, att_date):
    sql = """
        SELECT
            punch_in_coordinates,
            punch_out_coordinates,
            punch_in_time  AT TIME ZONE 'Asia/Kolkata' AS pin_ist,
            punch_out_time AT TIME ZONE 'Asia/Kolkata' AS pout_ist,
            punch_in_time                              AS pin_utc,
            punch_out_time                             AS pout_utc
        FROM rider_riderattendancedetails
        WHERE rider_id = %s AND date = %s
        ORDER BY punch_in_time NULLS LAST
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (rider_db_id, att_date))
            return [dict(r) for r in cur.fetchall()]


def fetch_orders_raw(rider_db_id, start_utc, end_utc, region_id=None, zone_id=None):
    """
    Fetch raw order rows in one round-trip.
    Uses created_on (db_index, partial composite index) NOT created_on_original (no index).
    notified_rider_list @> uses the GIN index.
    """
    extra_clauses, extra_params = [], []
    if region_id:
        extra_clauses.append("AND o.region_id = %s")
        extra_params.append(region_id)
    if zone_id:
        extra_clauses.append("AND o.zone_id = %s")
        extra_params.append(zone_id)

    sql = f"""
        SELECT
            o.order_key,
            o.order_status,
            (o.from_address->>'latitude')::float  AS pickup_lat,
            (o.from_address->>'longitude')::float AS pickup_lng,
            o.from_address->>'name'               AS pickup_name,
            o.notified_rider_list,
            c.name                                AS customer_name,
            rg.region_name,
            z.name                                AS zone_name,
            o.created_on AT TIME ZONE 'Asia/Kolkata' AS created_ist,
            CASE WHEN bl.id IS NOT NULL THEN true ELSE false END AS is_blacklisted
        FROM orders_order o
        LEFT JOIN customer_customer c   ON c.id  = o.customer_id
        LEFT JOIN orders_regions rg     ON rg.id = o.region_id
        LEFT JOIN orders_zone z         ON z.id  = o.zone_id
        LEFT JOIN customer_customerriderblacklist bl
               ON bl.customer_id = o.customer_id AND bl.rider_id = %s
        WHERE o.draft_order = false
          AND o.created_on >= %s
          AND o.created_on <  %s
          AND (o.from_address->>'latitude')  IS NOT NULL
          AND (o.from_address->>'longitude') IS NOT NULL
          AND (o.from_address->>'latitude')::float  != 0
          AND (o.from_address->>'longitude')::float != 0
          {' '.join(extra_clauses)}
        ORDER BY o.created_on
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, [rider_db_id, start_utc, end_utc] + extra_params)
            return cur.fetchall()


def process_row(r, rider_db_id):
    notified = bool(r["notified_rider_list"] and rider_db_id in r["notified_rider_list"])
    ist_ts = r["created_ist"].strftime("%H:%M:%S") if r["created_ist"] else ""
    return {
        "order_key":      r["order_key"],
        "status_label":   ORDER_STATUS_LABELS.get(r["order_status"], str(r["order_status"])),
        "pickup_lat":     r["pickup_lat"],
        "pickup_lng":     r["pickup_lng"],
        "pickup_name":    r["pickup_name"] or "",
        "customer_name":  r["customer_name"] or "",
        "region":         r["region_name"] or "",
        "zone":           r["zone_name"] or "",
        "time_ist":       ist_ts,
        "is_blacklisted": r["is_blacklisted"],
        "is_notified":    notified,
    }


def categorise(o):
    if o["is_blacklisted"]:
        return "Blacklisted Customer"
    if o["is_notified"]:
        return "Notified"
    return "Not Notified"


# ---------------------------------------------------------------------------
# DB — Report (split into 3 targeted queries merged in Python)
#
# Why split instead of one big CTE+JOIN:
#   Q1 (attendance): date scan — unavoidable, no index on `date`, but small result
#   Q2 (orders): ONE scan of the time window using notified_rider_list && ARRAY[all_rider_ids]
#       The && (overlap) GIN operator fires once for ALL riders instead of @> per rider
#       inside a nested CTE join. created_on hits the partial composite index.
#   Q3 (blacklisted): ANY(array) for all riders in one shot
#   Python merge: trivial dict lookups — zero extra DB round-trips
# ---------------------------------------------------------------------------

_SQL_ATTENDANCE = """
    SELECT
        r.id                                                          AS rider_id,
        r.rider_key,
        COUNT(a.id)                                                   AS punch_count,
        SUM(CASE WHEN a.punch_out_time IS NULL THEN 1 ELSE 0 END)    AS active_sessions,
        ROUND(
            SUM(
                CASE
                    WHEN a.punch_in_time IS NOT NULL AND a.punch_out_time IS NOT NULL
                    THEN GREATEST(0, EXTRACT(EPOCH FROM (a.punch_out_time - a.punch_in_time)))
                    ELSE 0
                END
            ) / 3600.0, 2
        )                                                             AS total_hours
    FROM rider_riderattendancedetails a
    JOIN rider_riderregistration r ON r.id = a.rider_id
    WHERE a.date = %s
    GROUP BY r.id, r.rider_key
    ORDER BY r.rider_key
"""

# && = GIN overlap operator — one scan for all riders, not @> per rider.
# draft_order=false + created_on range activates the partial composite index.
_SQL_ORDERS = """
    SELECT
        o.order_key,
        o.notified_rider_list,
        c.name                                AS customer_name,
        (o.from_address->>'latitude')::float  AS pickup_lat,
        (o.from_address->>'longitude')::float AS pickup_lng,
        o.from_address->>'name'               AS pickup_name,
        o.created_on AT TIME ZONE 'Asia/Kolkata' AS created_ist
    FROM orders_order o
    LEFT JOIN customer_customer c ON c.id = o.customer_id
    WHERE o.draft_order = false
      AND o.created_on >= %s
      AND o.created_on <  %s
      AND o.notified_rider_list && %s::integer[]
"""

_SQL_BLACKLISTED = """
    SELECT bl.rider_id, string_agg(DISTINCT c.name, ', ') AS names
    FROM customer_customerriderblacklist bl
    JOIN customer_customer c ON c.id = bl.customer_id
    WHERE bl.rider_id = ANY(%s::integer[])
    GROUP BY bl.rider_id
"""


def fetch_report(att_date, start_utc, end_utc):
    """Returns (rider_rows, plot_orders). plot_orders are notified orders with valid coords."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            # Q1 — riders who punched in on this date
            cur.execute(_SQL_ATTENDANCE, (att_date,))
            riders = [dict(r) for r in cur.fetchall()]

        if not riders:
            return [], []

        rider_ids = [r["rider_id"] for r in riders]
        rider_id_set = set(rider_ids)
        rider_key_map = {r["rider_id"]: r["rider_key"] for r in riders}

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            # Q2 — all orders in window that notified at least one of these riders
            cur.execute(_SQL_ORDERS, (start_utc, end_utc, rider_ids))
            orders_raw = cur.fetchall()

            # Q3 — blacklisted customers per rider (one shot)
            cur.execute(_SQL_BLACKLISTED, (rider_ids,))
            blacklisted_map = {r["rider_id"]: r["names"] for r in cur.fetchall()}

    # Python merge — distribute each order to the riders it actually notified
    from collections import defaultdict
    rider_order_keys  = defaultdict(list)
    rider_customers   = defaultdict(set)
    plot_orders       = []
    seen_keys         = set()

    for o in orders_raw:
        notified = o["notified_rider_list"] or []
        customer = o["customer_name"] or ""
        for rid in notified:
            if rid in rider_id_set:
                rider_order_keys[rid].append(o["order_key"])
                if customer:
                    rider_customers[rid].add(customer)

        # Collect for plot — one entry per unique order (skip bad/missing coords)
        if o["order_key"] not in seen_keys:
            lat = o["pickup_lat"]
            lng = o["pickup_lng"]
            if lat and lng and lat != 0 and lng != 0:
                notified_keys = [rider_key_map[rid] for rid in notified if rid in rider_id_set]
                plot_orders.append({
                    "order_key":      o["order_key"],
                    "pickup_lat":     lat,
                    "pickup_lng":     lng,
                    "pickup_name":    o["pickup_name"] or "",
                    "customer_name":  customer,
                    "time_ist":       o["created_ist"].strftime("%H:%M:%S") if o["created_ist"] else "",
                    "notified_riders": ", ".join(notified_keys),
                })
            seen_keys.add(o["order_key"])

    rider_rows = [
        {
            "rider_key":             r["rider_key"],
            "punch_count":           r["punch_count"],
            "active_sessions":       r["active_sessions"],
            "total_hours":           r["total_hours"],
            "total_notified":        len(rider_order_keys[r["rider_id"]]),
            "notified_orders":       ", ".join(rider_order_keys[r["rider_id"]]),
            "unique_customers":      ", ".join(sorted(rider_customers[r["rider_id"]])),
            "blacklisted_customers": blacklisted_map.get(r["rider_id"], ""),
        }
        for r in riders
    ]
    return rider_rows, plot_orders


# ---------------------------------------------------------------------------
# DB — Zone Coverage
# ---------------------------------------------------------------------------

def fetch_zone_coverage(start_utc, end_utc, region_ids, zone_ids):
    """
    Returns per-zone order counts split into notified / not-notified.
    notified  = notified_rider_list has at least one rider id
    not notified = notified_rider_list is NULL or empty array
    Only draft_order=false orders are counted.
    zone_ids=[] means all zones in the selected regions.
    """
    zone_clause = "AND o.zone_id = ANY(%s::integer[])" if zone_ids else ""
    params = [start_utc, end_utc, region_ids]
    if zone_ids:
        params.append(zone_ids)

    sql = f"""
        SELECT
            rg.region_name,
            COALESCE(z.name, '(No Zone)')                                                             AS zone_name,
            COUNT(*)                                                                                   AS total_orders,
            SUM(CASE WHEN cardinality(o.notified_rider_list) > 0                THEN 1 ELSE 0 END)   AS notified,
            SUM(CASE WHEN COALESCE(cardinality(o.notified_rider_list), 0) = 0   THEN 1 ELSE 0 END)   AS not_notified
        FROM orders_order o
        LEFT JOIN orders_regions rg ON rg.id = o.region_id
        LEFT JOIN orders_zone    z  ON z.id  = o.zone_id
        WHERE o.draft_order = false
          AND o.created_on >= %s
          AND o.created_on <  %s
          AND o.region_id = ANY(%s::integer[])
          {zone_clause}
        GROUP BY rg.region_name, z.name
        ORDER BY rg.region_name, z.name
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def fetch_zone_centroids(start_utc, end_utc, region_ids, zone_ids):
    """
    Compute average pickup lat/lng per zone from orders in the time window.
    Used as a proxy zone centre to assign rider punch-in locations to zones.
    Returns {zone_name: (center_lat, center_lng)}.
    """
    zone_clause = "AND o.zone_id = ANY(%s::integer[])" if zone_ids else ""
    params = [start_utc, end_utc, region_ids]
    if zone_ids:
        params.append(zone_ids)

    sql = f"""
        SELECT
            z.name                                        AS zone_name,
            AVG((o.from_address->>'latitude')::float)     AS center_lat,
            AVG((o.from_address->>'longitude')::float)    AS center_lng
        FROM orders_order o
        JOIN orders_zone z ON z.id = o.zone_id
        WHERE o.draft_order = false
          AND o.created_on >= %s
          AND o.created_on <  %s
          AND o.region_id = ANY(%s::integer[])
          {zone_clause}
          AND (o.from_address->>'latitude')::float  != 0
          AND (o.from_address->>'longitude')::float != 0
        GROUP BY z.name
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            return {r["zone_name"]: (r["center_lat"], r["center_lng"]) for r in cur.fetchall()}


def fetch_punched_in_coords(att_date):
    """
    Returns one punch-in coordinate per rider (earliest punch-in of the day).
    Riders without coordinates or with zero-coords are excluded.
    """
    sql = """
        SELECT DISTINCT ON (a.rider_id)
            (a.punch_in_coordinates->>'latitude')::float  AS lat,
            (a.punch_in_coordinates->>'longitude')::float AS lng
        FROM rider_riderattendancedetails a
        WHERE a.date = %s
          AND a.punch_in_coordinates IS NOT NULL
          AND (a.punch_in_coordinates->>'latitude')  IS NOT NULL
          AND (a.punch_in_coordinates->>'longitude') IS NOT NULL
          AND (a.punch_in_coordinates->>'latitude')::float  != 0
          AND (a.punch_in_coordinates->>'longitude')::float != 0
        ORDER BY a.rider_id, a.punch_in_time
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (att_date,))
            return [dict(r) for r in cur.fetchall()]


def fetch_punched_out_coords(att_date):
    """Returns one punch-out coordinate per rider (latest punch-out of the day)."""
    sql = """
        SELECT DISTINCT ON (a.rider_id)
            (a.punch_out_coordinates->>'latitude')::float  AS lat,
            (a.punch_out_coordinates->>'longitude')::float AS lng
        FROM rider_riderattendancedetails a
        WHERE a.date = %s
          AND a.punch_out_coordinates IS NOT NULL
          AND (a.punch_out_coordinates->>'latitude')  IS NOT NULL
          AND (a.punch_out_coordinates->>'longitude') IS NOT NULL
          AND (a.punch_out_coordinates->>'latitude')::float  != 0
          AND (a.punch_out_coordinates->>'longitude')::float != 0
        ORDER BY a.rider_id, a.punch_out_time DESC NULLS LAST
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (att_date,))
            return [dict(r) for r in cur.fetchall()]


def assign_to_nearest_zone(rider_coords, zone_centroids):
    """
    For each rider punch-in, find the nearest zone centroid via haversine.
    Only assigns if closest centroid is within 50 km (filters out-of-region riders).
    Returns {zone_name: rider_count}.
    """
    from collections import defaultdict
    counts = defaultdict(int)
    for r in rider_coords:
        best_zone, best_dist = None, float("inf")
        for zone_name, (clat, clng) in zone_centroids.items():
            d = haversine_km(r["lat"], r["lng"], clat, clng)
            if d < best_dist:
                best_dist = d
                best_zone = zone_name
        if best_zone and best_dist <= 50:
            counts[best_zone] += 1
    return counts


# ---------------------------------------------------------------------------
# Progressive chart helpers
# ---------------------------------------------------------------------------

def make_base_figure(title=""):
    """Circles-only base figure drawn once."""
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
    """Star markers + dashed path lines for all rider points. Reused across renders."""
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
    """
    Build a fresh figure with circles + current order traces + rider markers.
    Always creates a new figure — Plotly forbids replacing fig.data with new
    trace objects in-place (only permutations of existing traces are allowed).
    """
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

    # Dynamic axis range — capped at 100 km; zero-coord SQL guard prevents extreme outlier blow-up
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


def make_report_plot(plot_orders):
    """
    Plot all notified order pickup locations centred on their geographic centroid.
    Returns None if no plottable orders.
    """
    if not plot_orders:
        return None

    ref_lat = sum(o["pickup_lat"] for o in plot_orders) / len(plot_orders)
    ref_lng = sum(o["pickup_lng"] for o in plot_orders) / len(plot_orders)

    orders_with_offsets = []
    for o in plot_orders:
        dx, dy = to_km_offset(ref_lat, ref_lng, o["pickup_lat"], o["pickup_lng"])
        orders_with_offsets.append({
            **o,
            "dx": dx,
            "dy": dy,
            "dist_km": round(math.sqrt(dx ** 2 + dy ** 2), 3),
            "is_blacklisted": False,
            "is_notified": True,
            "status_label": "",
            "category": "Notified",
        })

    fig = make_base_figure(f"Notified Orders — Pickup Locations ({len(plot_orders)} orders)")

    fig.add_trace(go.Scatter(
        x=[o["dx"] for o in orders_with_offsets],
        y=[o["dy"] for o in orders_with_offsets],
        mode="markers",
        name=f"Notified ({len(orders_with_offsets)})",
        marker=dict(color="green", symbol="circle", size=10,
                    line=dict(width=1, color="green")),
        text=[
            f"<b>{o['order_key']}</b>  {o['time_ist']}<br>"
            f"Customer: {o['customer_name']}<br>"
            f"Pickup: {o['pickup_name']}<br>"
            f"Notified to: {o['notified_riders']}"
            for o in orders_with_offsets
        ],
        hovertemplate="%{text}<extra></extra>",
    ))

    all_x = [o["dx"] for o in orders_with_offsets]
    all_y = [o["dy"] for o in orders_with_offsets]
    axis_range = min(max([abs(v) for v in all_x + all_y] + [2]) * 1.2, 100)
    fig.update_layout(
        xaxis=dict(range=[-axis_range, axis_range]),
        yaxis=dict(range=[-axis_range, axis_range]),
    )
    return fig


# ---------------------------------------------------------------------------
# Progress log
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Region / Zone widgets
# ---------------------------------------------------------------------------

def region_zone_widgets(key_prefix):
    regions = get_regions()
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


# ---------------------------------------------------------------------------
# Streaming plot runner
# ---------------------------------------------------------------------------

def run_streaming_plot(rider_key, rider_db_id, rider_points, sessions_to_plot,
                       region_id, zone_id, chart_col, prog_col):
    """
    Fetch orders in one DB round-trip, then process + render in CHUNK_SIZE batches.
    Saves partial state to session_state after each chunk — Stop button works by
    triggering a Streamlit rerun that shows whatever was last saved.
    """
    log = ProgressLog(prog_col)
    chart_ph = chart_col.empty()
    stop_ph  = chart_col.empty()

    stop_ph.button("⛔ Stop loading", key="stop_btn",
                   help="Stops after the current chunk and shows partial results")

    ref_lat, ref_lng = rider_points[0]["lat"], rider_points[0]["lng"]
    title = f"Rider {rider_key} — {' + '.join(s['label'] for s in sessions_to_plot)}"

    # Step 1: plot rider location immediately before any DB fetch
    log("Plotting rider location…")
    fig0 = make_base_figure(title)
    fig0.add_traces(_rider_traces(rider_points, ref_lat, ref_lng))
    chart_ph.plotly_chart(fig0, use_container_width=True, key="fig_rider_only")

    # Step 2: fetch all orders in one shot
    all_raw = []
    for sess in sessions_to_plot:
        log(f"Querying DB for {sess['label']}…")
        batch_raw = fetch_orders_raw(rider_db_id, sess["start_utc"], sess["end_utc"],
                                     region_id, zone_id)
        log(f"{len(batch_raw)} orders in {sess['label']}")
        all_raw.extend(batch_raw)

    # Deduplicate by order_key (sessions may overlap)
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

    # Step 3: process + render in chunks so the chart builds progressively
    acc_by_cat = {"Notified": [], "Not Notified": [], "Blacklisted Customer": []}
    all_orders = []

    for chunk_start in range(0, total, CHUNK_SIZE):
        chunk_raw = unique_raw[chunk_start: chunk_start + CHUNK_SIZE]

        for r in chunk_raw:
            o = process_row(r, rider_db_id)
            o["dx"], o["dy"] = to_km_offset(ref_lat, ref_lng, o["pickup_lat"], o["pickup_lng"])
            dists = [haversine_km(rp["lat"], rp["lng"], o["pickup_lat"], o["pickup_lng"])
                     for rp in rider_points]
            o["dist_km"] = round(min(dists), 3)
            o["category"] = categorise(o)
            acc_by_cat[o["category"]].append(o)
            all_orders.append(o)

        loaded = chunk_start + len(chunk_raw)
        log(f"Plotting {loaded}/{total}…")

        st.session_state["plot_result"] = {
            "orders": list(all_orders),
            "rider_points": rider_points,
            "rider_key": rider_key,
            "session_label": title,
            "partial": loaded < total,
        }

        fig = rebuild_order_traces(acc_by_cat, rider_points, ref_lat, ref_lng, title)
        chart_ph.plotly_chart(fig, use_container_width=True, key=f"fig_chunk_{loaded}")

    stop_ph.empty()
    log.done(f"All {total} orders plotted ✓")
    st.session_state["plot_result"]["partial"] = False




# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

st.set_page_config(page_title="DMS Support Tool", layout="wide")
st.title("DMS Support Tool")

tab_plot, tab_report, tab_coverage = st.tabs(["🗺 Rider Plot", "📊 Notified Orders Report", "📋 Zone Coverage"])


# ===========================================================================
# TAB 1 — RIDER PLOT
# ===========================================================================

with tab_plot:

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

    # ---- Execute ----
    if plot_btn:
        errors = []
        if not p_rider_key.strip():
            errors.append("Rider key is required.")

        rider_points   = []
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
                        "end_utc":   s["pout_utc"] or datetime.now(pytz.utc),
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
            st.warning(f"Partial load stopped — showing {len(r['orders'])} orders. Click 'Plot Orders' to reload.")
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


# ===========================================================================
# TAB 2 — NOTIFIED ORDERS REPORT
# ===========================================================================

with tab_report:
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
            data_col, prog_col = st.columns([4, 1])
            with prog_col:
                st.markdown("**Progress**")
            log = ProgressLog(prog_col)

            r_start_utc = IST.localize(datetime.combine(r_date, r_from)).astimezone(pytz.utc)
            r_end_utc   = IST.localize(datetime.combine(r_date, r_to)).astimezone(pytz.utc)

            log("Fetching attendance + orders (single query)…")
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
                    "Rider Key":          r["rider_key"],
                    "Sessions":           r["punch_count"],
                    "Active (open)":      int(r["active_sessions"] or 0),
                    "Hours Worked":       float(r["total_hours"] or 0),
                    "Orders Notified":    r["total_notified"],
                    "Order Keys":         r["notified_orders"] or "",
                    "Unique Customers":   r["unique_customers"] or "",
                    "Blacklisted":        r["blacklisted_customers"] or "",
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
            st.plotly_chart(rep_fig, use_container_width=True, key="fig_report_cached")


# ===========================================================================
# TAB 3 — ZONE COVERAGE
# ===========================================================================

with tab_coverage:
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
        cov_sel_regions = st.multiselect(
            "Regions *",
            options=list(region_name_to_id.keys()),
            key="cov_regions",
        )
        cov_region_ids = [region_name_to_id[n] for n in cov_sel_regions]

        cov_zone_ids = []
        if cov_region_ids:
            all_zones = get_zones_for_regions(tuple(cov_region_ids))
            # Build label "ZoneName (RegionName)" to disambiguate across regions
            region_id_to_name = {rg[0]: rg[1] for rg in all_regions}
            zone_options = {
                f"{z[1]} ({region_id_to_name.get(z[2], z[2])})": z[0]
                for z in all_zones
            }
            if zone_options:
                cov_sel_zones = st.multiselect(
                    "Zones (leave empty for all zones in selected regions)",
                    options=list(zone_options.keys()),
                    key="cov_zones",
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

    if cov_fetch_btn:
        if cov_from >= cov_to:
            st.error("'From' must be before 'To'.")
        elif not cov_region_ids:
            st.error("Select at least one region.")
        else:
            cov_start_utc = IST.localize(datetime.combine(cov_date, cov_from)).astimezone(pytz.utc)
            cov_end_utc   = IST.localize(datetime.combine(cov_date, cov_to)).astimezone(pytz.utc)

            with st.spinner("Fetching coverage data…"):
                cov_rows       = fetch_zone_coverage(cov_start_utc, cov_end_utc, cov_region_ids, cov_zone_ids)
                zone_centroids = fetch_zone_centroids(cov_start_utc, cov_end_utc, cov_region_ids, cov_zone_ids)
                rider_coords   = fetch_punched_in_coords(cov_date)

            zone_rider_counts = assign_to_nearest_zone(rider_coords, zone_centroids)

            if not cov_rows:
                st.warning("No orders found for the selected filters.")
            else:
                df_cov = pd.DataFrame([{
                    "Region":             r["region_name"],
                    "Zone":               r["zone_name"],
                    "Riders Punched In":  zone_rider_counts.get(r["zone_name"], 0),
                    "Total Orders":       int(r["total_orders"]),
                    "Notified":           int(r["notified"]),
                    "Not Notified":       int(r["not_notified"]),
                    "Coverage %":         round(int(r["notified"]) / int(r["total_orders"]) * 100, 1)
                                          if int(r["total_orders"]) > 0 else 0.0,
                } for r in cov_rows])

                totals = {
                    "Total Orders":      df_cov["Total Orders"].sum(),
                    "Notified":          df_cov["Notified"].sum(),
                    "Not Notified":      df_cov["Not Notified"].sum(),
                    "Coverage %":        round(df_cov["Notified"].sum() / df_cov["Total Orders"].sum() * 100, 1)
                                         if df_cov["Total Orders"].sum() > 0 else 0.0,
                    "Riders Punched In": int(df_cov["Riders Punched In"].sum()),
                }

                t1, t2, t3, t4, t5 = st.columns(5)
                t1.metric("Total Orders",      int(totals["Total Orders"]))
                t2.metric("Notified",          int(totals["Notified"]))
                t3.metric("Not Notified",      int(totals["Not Notified"]))
                t4.metric("Coverage %",        f"{totals['Coverage %']}%")
                t5.metric("Riders Punched In", totals["Riders Punched In"])

                if cov_sort == "Lowest coverage first":
                    df_display = df_cov.sort_values("Coverage %", ascending=True)
                elif cov_sort == "Highest coverage first":
                    df_display = df_cov.sort_values("Coverage %", ascending=False)
                else:
                    df_display = df_cov

                st.dataframe(df_display, use_container_width=True, hide_index=True)

                cov_csv = io.StringIO()
                df_display.to_csv(cov_csv, index=False)
                st.download_button("⬇️ Download CSV", data=cov_csv.getvalue(),
                                   file_name=f"zone_coverage_{cov_date}.csv", mime="text/csv")

                st.session_state["coverage_result"] = {
                    "df": df_cov, "date": cov_date, "totals": totals,
                }

    elif "coverage_result" in st.session_state and not cov_fetch_btn:
        r = st.session_state["coverage_result"]
        st.info(f"Cached coverage for **{r['date']}**. Click '▶ Fetch Coverage' to refresh.")
        totals = r["totals"]
        t1, t2, t3, t4, t5 = st.columns(5)
        t1.metric("Total Orders",      int(totals["Total Orders"]))
        t2.metric("Notified",          int(totals["Notified"]))
        t3.metric("Not Notified",      int(totals["Not Notified"]))
        t4.metric("Coverage %",        f"{totals['Coverage %']}%")
        t5.metric("Riders Punched In", totals.get("Riders Punched In", "—"))

        if cov_sort == "Lowest coverage first":
            df_display = r["df"].sort_values("Coverage %", ascending=True)
        elif cov_sort == "Highest coverage first":
            df_display = r["df"].sort_values("Coverage %", ascending=False)
        else:
            df_display = r["df"]

        st.dataframe(df_display, use_container_width=True, hide_index=True)
        cov_csv = io.StringIO()
        df_display.to_csv(cov_csv, index=False)
        st.download_button("⬇️ Download CSV", data=cov_csv.getvalue(),
                           file_name=f"zone_coverage_{r['date']}.csv", mime="text/csv")
