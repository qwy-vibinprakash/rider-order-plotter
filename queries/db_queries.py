"""
All PostgreSQL query functions — attendance, orders, reports, merchants, zones.
"""
import math
from collections import defaultdict

import psycopg2.extras
import streamlit as st

from config import get_conn

# ---------------------------------------------------------------------------
# Constants shared across tabs
# ---------------------------------------------------------------------------

ORDER_STATUS_LABELS = {
    1: "NEW", 2: "ACCEPTED", 3: "PICKED UP", 4: "DELIVERED",
    5: "CANCELLED", 6: "UNDELIVERED", 7: "RTW", 8: "RTS",
    9: "RTAA", 10: "SCHEDULED", 11: "REACHED PICKUP",
    12: "REACHED DELIVERY", 13: "ABSCONDED",
}

SESSION_PALETTE = ["#1f77b4", "#9467bd", "#2ca02c", "#8c564b", "#e377c2", "#17becf"]

CATEGORY_STYLES = [
    ("Notified",             "green",  "circle"),
    ("Not Notified",         "red",    "circle-open"),
    ("Blacklisted Customer", "orange", "x"),
]

CHUNK_SIZE = 75


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
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
# Region / Zone
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def get_regions():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, region_name FROM orders_regions WHERE active_status = true ORDER BY region_name"
            )
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
    if not region_ids_tuple:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, region_id FROM orders_zone WHERE deleted IS NULL AND region_id = ANY(%s) ORDER BY name",
                (list(region_ids_tuple),),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Merchant hierarchy (for Third-Party tab)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def fetch_merchant_hierarchy():
    """Returns {parent_name: [sub_name, ...], ...}."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT p.name AS parent_name, c.name AS child_name
                FROM client_client p
                LEFT JOIN client_client c
                    ON c.parent_id = p.id AND c.is_sub_merchant = true
                WHERE p.is_sub_merchant = false
                ORDER BY p.name, c.name
            """)
            rows = cur.fetchall()
        conn.close()
        hierarchy = {}
        for r in rows:
            pname = r["parent_name"]
            if pname not in hierarchy:
                hierarchy[pname] = []
            if r["child_name"]:
                hierarchy[pname].append(r["child_name"])
        return hierarchy
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Rider resolution
# ---------------------------------------------------------------------------

def resolve_rider(rider_key):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM rider_riderregistration WHERE rider_key = %s", (rider_key.strip(),))
            row = cur.fetchone()
            return row[0] if row else None


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Orders (Rider Plot)
# ---------------------------------------------------------------------------

def fetch_orders_raw(rider_db_id, start_utc, end_utc, region_id=None, zone_id=None):
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
# Notified Orders Report (3-query split)
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
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_SQL_ATTENDANCE, (att_date,))
            riders = [dict(r) for r in cur.fetchall()]

        if not riders:
            return [], []

        rider_ids = [r["rider_id"] for r in riders]
        rider_id_set = set(rider_ids)
        rider_key_map = {r["rider_id"]: r["rider_key"] for r in riders}

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_SQL_ORDERS, (start_utc, end_utc, rider_ids))
            orders_raw = cur.fetchall()

            cur.execute(_SQL_BLACKLISTED, (rider_ids,))
            blacklisted_map = {r["rider_id"]: r["names"] for r in cur.fetchall()}

    rider_order_keys = defaultdict(list)
    rider_customers  = defaultdict(set)
    plot_orders      = []
    seen_keys        = set()

    for o in orders_raw:
        notified = o["notified_rider_list"] or []
        customer = o["customer_name"] or ""
        for rid in notified:
            if rid in rider_id_set:
                rider_order_keys[rid].append(o["order_key"])
                if customer:
                    rider_customers[rid].add(customer)

        if o["order_key"] not in seen_keys:
            lat = o["pickup_lat"]
            lng = o["pickup_lng"]
            if lat and lng and lat != 0 and lng != 0:
                notified_keys = [rider_key_map[rid] for rid in notified if rid in rider_id_set]
                plot_orders.append({
                    "order_key":       o["order_key"],
                    "pickup_lat":      lat,
                    "pickup_lng":      lng,
                    "pickup_name":     o["pickup_name"] or "",
                    "customer_name":   customer,
                    "time_ist":        o["created_ist"].strftime("%H:%M:%S") if o["created_ist"] else "",
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
# Zone Coverage
# ---------------------------------------------------------------------------

def fetch_zone_coverage(start_utc, end_utc, region_ids, zone_ids):
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


# ---------------------------------------------------------------------------
# Rider Presence
# ---------------------------------------------------------------------------

def fetch_zone_centroids(start_utc, end_utc, region_ids, zone_ids):
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
# Overall Report (per-day stats)
# ---------------------------------------------------------------------------

def fetch_daily_stats(day_date, start_utc, end_utc, region_ids):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT o.id)                                                                         AS total_orders,
                    COUNT(DISTINCT o.id) FILTER (WHERE cardinality(o.notified_rider_list) > 0)                  AS notified_orders,
                    COUNT(DISTINCT o.id) FILTER (WHERE COALESCE(cardinality(o.notified_rider_list), 0) = 0)     AS not_notified_orders,
                    COUNT(rid.val)                                                                                AS total_notifications,
                    COUNT(DISTINCT rid.val)                                                                       AS riders_with_orders
                FROM orders_order o
                LEFT JOIN LATERAL unnest(o.notified_rider_list) AS rid(val) ON true
                WHERE o.draft_order = false
                  AND o.created_on >= %s AND o.created_on < %s
                  AND o.region_id = ANY(%s::integer[])
            """, (start_utc, end_utc, region_ids))
            o = dict(cur.fetchone())

            cur.execute("""
                SELECT COUNT(DISTINCT rider_id) AS cnt
                FROM rider_riderattendancedetails
                WHERE date = %s
            """, (day_date,))
            riders_punched_in = int(cur.fetchone()[0] or 0)

    riders_with_orders = int(o["riders_with_orders"] or 0)
    return {
        "date":                day_date,
        "total_orders":        int(o["total_orders"] or 0),
        "notified_orders":     int(o["notified_orders"] or 0),
        "not_notified_orders": int(o["not_notified_orders"] or 0),
        "total_notifications": int(o["total_notifications"] or 0),
        "riders_with_orders":  riders_with_orders,
        "riders_punched_in":   riders_punched_in,
        "riders_no_orders":    max(riders_punched_in - riders_with_orders, 0),
    }
