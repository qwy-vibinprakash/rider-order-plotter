# DMS Support Tool — Developer Notes

## Purpose
Internal support tool for diagnosing rider notification issues and generating rider activity reports.
Connects directly to the production read-replica PostgreSQL DB. No data is stored — everything is runtime.

## How to run
```bash
cd /home/vibinprakash/qwytech/rider-order-plotter
source venv/bin/activate
streamlit run app.py        # opens http://localhost:8501
```

## Config (`config.json`)
```json
{
    "database": {
        "host": "prod-replica.crxsijunp2g7.ap-south-1.rds.amazonaws.com",
        "port": 5432,
        "dbname": "qwqerdb",
        "user": "mcp_read",
        "password": "..."
    }
}
```
- Use the **read-replica** host (`prod-replica.…`), not the primary (`qwqer-prod.…`) — the primary times out for external tool connections
- `connect_timeout=10` is passed to psycopg2 so bad hosts fail fast
- `@st.cache_resource` caches the config object for the process lifetime — **restart Streamlit** after editing `config.json` to pick up new credentials

---

## App Structure

### Tab 1 — Rider Plot
Visualises order pickup locations around a rider's position on a KM-scale Plotly map.

**Location Source — two modes:**

**Manual Entry**
- Paste N lat,lng pairs (one per line, `lat, lng` format)
- Pick date + From/To IST time window
- Fetches all orders in that window and plots around all entered points

**Fetch from Attendance** *(two-step)*
1. Enter rider key + date → click "Load Sessions"
   - `resolve_rider(rider_key)` resolves the rider DB id first (separate query, avoids full table join)
   - Then queries `rider_riderattendancedetails` by `rider_id` FK (auto-indexed)
   - Sessions stored in `st.session_state["att_sessions"]`
2. Multiselect shows each punch-in/out slot ("Session 1: 07:15 → 09:30")
   - Each selected session uses its own `punch_in_time → punch_out_time` as the order time window
3. Click "Plot Orders" → fetches per session, deduplicates by `order_key` in Python

**Render sequence:**
1. Rider star markers drawn immediately (before any DB fetch) so user sees their location instantly
2. DB order query runs in background
3. Orders streamed to chart in `CHUNK_SIZE=75` Python chunks — chart updates after each chunk
4. Progress log (narrow right column) shows each step live

**Stop button:**
- `st.button("⛔ Stop loading")` rendered inside a `stop_ph.empty()` placeholder during streaming
- Clicking triggers a Streamlit rerun — the chunk loop is naturally abandoned
- Partial orders are saved to `st.session_state["plot_result"]["partial"]=True` after each chunk, so stopping mid-load still shows what was loaded

**Color coding:**
- Green circle = Notified (`notified_rider_list` contained this rider's DB id)
- Red open circle = Not Notified, not blacklisted — **investigate these**
- Orange X = Blacklisted customer (explains why push was skipped)
- Star markers = rider positions (one per session, one color per session)
- Dashed line = path between punch-in and punch-out of same session

**Legend labels include counts:** "Notified (42)" / "Not Notified (280)" — visible without opening a table.

**Hover on each order:** order key (bold), customer name, pickup address, nearest distance km, order status, time IST, notified/blacklisted flag.

**Session state key:** `plot_result` — stored after every chunk; switching tabs never loses the chart.

---

### Tab 2 — Notified Orders Report
Per-rider notification summary for a date + IST time window.

**Inputs:** single date + From/To IST time.

**Data flow (3 separate queries, merged in Python — see Query Design below):**
1. Q1 → riders who punched in on the date (with session count + hours worked)
2. Q2 → all orders in the time window notified to ANY of those riders (one GIN scan)
3. Q3 → blacklisted customers for all riders (one ANY scan)
4. Python dict merge assigns orders to the right riders

**Report columns:**
| Column | Source |
|---|---|
| Rider Key | `rider_riderregistration.rider_key` |
| Sessions | COUNT of attendance rows for that date |
| Active (open) | Sessions where `punch_out_time IS NULL` (still in field) |
| Hours Worked | Sum of completed session durations only (see Hours bug below) |
| Orders Notified | COUNT of orders assigned to this rider from Q2 merge |
| Order Keys | Comma-joined order keys |
| Unique Customers | DISTINCT customer names from notified orders |
| Blacklisted | Customers who have blacklisted this rider |

**Output:** table + "Download CSV" button. Cached in `st.session_state["report_result"]`.

---

## Database Model Learnings

### `orders_order`

| Field | Index | Notes |
|---|---|---|
| `created_on` | `db_index=True` + partial composite `idx_ord_stat_created_desc (order_status, -created_on, -id) WHERE draft_order=False` | **USE THIS for time-window filters** |
| `created_on_original` | **NO INDEX** | Do NOT use for time filters — was incorrectly used before, caused slow scans |
| `notified_rider_list` | `GinIndex` | `@>` (contains single) and `&&` (overlap many) both hit this index |
| `order_status` | `db_index=True` | Part of the partial composite index |
| `draft_order` | Part of partial index condition | Always include `draft_order = false` to activate the partial index |
| `region_id`, `zone_id`, `customer_id` | FK auto-index | Add to WHERE to narrow the scan |
| `from_address` | JSONField | Keys: `latitude`, `longitude`, `name`. Some rows have `"latitude": 0` — filter these out or the km-offset blows the chart axis to thousands of km |

**Bad coordinate guard (required in every plot query):**
```sql
AND (o.from_address->>'latitude')::float  != 0
AND (o.from_address->>'longitude')::float != 0
```
Without this, even one order with lat=0/lng=0 maps to ~7000 km offset from a Bangalore rider,
stretching the chart axis so far all real nearby orders become invisible pixel dots at the origin.

### `rider_riderattendancedetails`

| Field | Index | Notes |
|---|---|---|
| `rider_id` | FK auto-index | Query by this — do NOT join through `rider_key` |
| `date` | **NO INDEX** | Sequential scan; unavoidable without adding a DB index; acceptable because result set is small per day |
| `punch_in_time` / `punch_out_time` | None | UTC DateTimeFields — convert to IST for display |
| `punch_in_coordinates` / `punch_out_coordinates` | None | JSONField — keys are `latitude` and `longitude` |
| `no_of_orders` | None | Stored integer, not used in this tool |

**No `worked_hours` stored field** — hours must be computed from `punch_out_time - punch_in_time`.

**Hours worked bug — always use `CASE WHEN` not `COALESCE(punch_out, NOW())`:**
```sql
-- ✅ CORRECT — only completed sessions
CASE
    WHEN punch_in_time IS NOT NULL AND punch_out_time IS NOT NULL
    THEN GREATEST(0, EXTRACT(EPOCH FROM (punch_out_time - punch_in_time)))
    ELSE 0
END

-- ❌ WRONG — open sessions (NULL punch_out) use NOW(), which bleeds across days
-- producing impossible 25+ hour totals if the session was from the previous day
COALESCE(punch_out_time, NOW()) - punch_in_time
```
Active sessions are counted separately in `active_sessions` column so they're not silently dropped.

### `orders_regions`
- **No SafeDeleteModel** — filter with `WHERE active_status = true` (NOT `deleted IS NULL`)

### `orders_zone`
- Uses `SafeDeleteModel` → filter with `WHERE deleted IS NULL`

### `rider_riderregistration`
- `rider_key` has no guaranteed index — always resolve to `rider_id` (PK) first, then use that in all subsequent queries

### `customer_customerriderblacklist`
- No soft-delete — presence of a row = active blacklist. No `deleted` or `active` column.

---

## Query Design

### Plot query (`fetch_orders_raw`)
One round-trip, all riders/sessions, returns raw rows for Python chunking:
```sql
WHERE o.draft_order = false
  AND o.created_on >= %s                         -- hits partial composite index
  AND o.created_on <  %s
  AND (o.from_address->>'latitude')  IS NOT NULL
  AND (o.from_address->>'longitude') IS NOT NULL
  AND (o.from_address->>'latitude')::float  != 0  -- guard against axis blow-up
  AND (o.from_address->>'longitude')::float != 0
```
Blacklist checked via LEFT JOIN so a single query gives all 3 categories (Notified / Not Notified / Blacklisted).

### Report — 3-query split (NOT a single CTE)

**Why split:**
The old single-query approach joined `orders_order` per rider inside a CTE using `@>` (contains one),
which executed the GIN lookup N times (once per rider). With 200+ riders that's 200+ index probes on
the orders table for the same time window.

**Q1 — Attendance (seqscan on `date`, acceptable):**
```sql
SELECT r.id, r.rider_key, COUNT(a.id) AS punch_count,
       SUM(CASE WHEN punch_out IS NULL THEN 1 ELSE 0 END) AS active_sessions,
       ROUND(SUM(CASE WHEN punch_in IS NOT NULL AND punch_out IS NOT NULL
                 THEN GREATEST(0, EXTRACT(EPOCH FROM (punch_out - punch_in)))
                 ELSE 0 END) / 3600.0, 2) AS total_hours
FROM rider_riderattendancedetails a
JOIN rider_riderregistration r ON r.id = a.rider_id
WHERE a.date = %s
GROUP BY r.id, r.rider_key
```

**Q2 — Orders (ONE GIN scan for all riders via `&&`):**
```sql
SELECT o.order_key, o.notified_rider_list, c.name AS customer_name
FROM orders_order o
LEFT JOIN customer_customer c ON c.id = o.customer_id
WHERE o.draft_order = false
  AND o.created_on >= %s
  AND o.created_on <  %s
  AND o.notified_rider_list && %s::integer[]   -- overlap: fires GIN once for all riders
```
`&&` (overlap) vs `@>` (contains): `&&` returns orders that notified ANY of the riders;
`@>` checks one specific rider. Using `&&` with the full rider_ids array = 1 GIN probe total.

**Q3 — Blacklisted (one FK scan for all riders):**
```sql
SELECT bl.rider_id, string_agg(DISTINCT c.name, ', ') AS names
FROM customer_customerriderblacklist bl
JOIN customer_customer c ON c.id = bl.customer_id
WHERE bl.rider_id = ANY(%s::integer[])
GROUP BY bl.rider_id
```

**Python merge:** Dict of `rider_id → [order_keys]` built by iterating Q2 results and checking
`notified_rider_list` against `rider_id_set`. O(orders × avg list length) — trivial.

---

## Plotly Patterns

**Never reassign `fig.data` with new trace objects:**
```python
fig.data = tuple(new_traces)   # ❌ raises ValueError if any trace is a new object
fig.add_traces(new_traces)     # ✅ appending new traces always works
```
Plotly only allows `fig.data =` reassignment for permutations/subsets of *existing* traces.
The `rebuild_order_traces()` function creates a fresh figure each time and uses `add_traces`.

**Axis range cap:** Always cap with `min(..., 25)` km — a single order with bad coordinates
(lat=0, lng=0) would otherwise stretch the axis to thousands of km and make all real orders invisible.

**`_rider_traces()` helper:** Factored out so rider markers can be added to the initial empty chart
(before orders are fetched) AND reused in `rebuild_order_traces()` per chunk.

---

## Streamlit Patterns

| Pattern | Why |
|---|---|
| `st.session_state["plot_result"]` / `["report_result"]` | Tab switches trigger full reruns — session_state is the only way to keep results across tabs |
| `chart_ph = col.empty()` + `chart_ph.plotly_chart(fig, key=f"fig_{n}")` | Same placeholder updated each chunk — unique `key` prevents React reconciliation artifacts |
| Save partial state after every chunk | `{"partial": True}` so Stop button rerun immediately shows what was loaded |
| `stop_ph.button(...)` in an `empty()` placeholder | Button disappears on rerun (outside of streaming context) — `stop_ph.empty()` at end of loop clears it manually too |
| `ProgressLog` class | Keeps a list of steps; marks the previous step ✅ when a new step starts; renders into a single `st.empty()` markdown block |
| `@st.cache_data(ttl=300)` on region/zone queries | 5-minute cache — regions/zones rarely change |
| `@st.cache_resource` on DB config | Config dict loaded once per process; requires **app restart** to pick up `config.json` changes |
| Attendance two-step: Load Sessions → multiselect → Plot | Separates the "which rider on which date" lookup from the "which sessions to plot" decision |
