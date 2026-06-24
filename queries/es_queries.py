"""
Elasticsearch query functions — TEV Dashboard, Third-Party API Monitor, TPL Monitor.

SUCCESS DETECTION LOGIC
-----------------------
Third Party (QWQER format):
  Success = response body contains phrase "error false"
            (QWQER success: {"message":"Success","data":{...},"is_success":true,"error":false})
  Error   = response body contains a QE/BE error code token (e.g. "QE801")
  No body = response_body field absent
  Unknown = body present, no "error false" phrase, no known QE code (Swiggy/other format)

TEV TPL (white-label, same QWQER format):
  Same detection as Third Party above.

Yumove TPL:
  Success     = HTTP status 200 AND response body does NOT contain token "reason_id"
  Error       = response body contains token "reason_id" (is_serviceable=false responses)
  No response = response_status_code field absent (timeout)
  NOTE: ES standard tokenizer keeps "reason_id" as ONE token (underscore = connector
  punctuation, not a word boundary per Unicode UAX#29). Use match("reason_id") NOT
  match_phrase("reason id") — the phrase query finds zero results.

EK Bharat TPL:
  Success = response body contains phrase "status true"
            ({"status":true,"code":200,"message":"..."})
  Error   = grouped by error message text from known EK Bharat error patterns
  No body = response_body absent
"""

import re

import streamlit as st

from config import get_es_client

# ---------------------------------------------------------------------------
# TEV Dashboard constants
# ---------------------------------------------------------------------------

_TEV_BASE = "https://dms-api.tevhrsolutions.in"

_TEV_SECTIONS = {
    "Quote":         [f"{_TEV_BASE}/v2/client/price-calculate/", f"{_TEV_BASE}/client/price-calculate/"],
    "Order Create":  [f"{_TEV_BASE}/v2/client/order/", f"{_TEV_BASE}/v2/client/fifo-order/", f"{_TEV_BASE}/swiggy/create"],
    "FIFO Modify":   [f"{_TEV_BASE}/v2/client/fifo/modify/"],
    "Normal Modify": [f"{_TEV_BASE}/v2/client/order/modify/"],
    "Cancel":        [f"{_TEV_BASE}/v2/client/order/cancel/", f"{_TEV_BASE}/client/order/cancel/"],
}

_TEV_ERROR_CODES = [
    "QE800", "QE801", "QE802", "QE807", "QE825", "QE847",
    "QE400", "QE401", "QE402", "QE429",
    "QE844", "QE891", "QE921", "QE922", "QE923",
    "QS890", "QS913",
]

_TEV_ERROR_LABELS = {
    "QE800": "Validation Error",
    "QE801": "Region Not Serviceable",
    "QE802": "Service Unavailable",
    "QE807": "Dup Merchant Order ID",
    "QE825": "Dup Merchant Order ID (v2)",
    "QE847": "Drop Region N/A",
    "QE400": "No API Key",
    "QE401": "Invalid API Key",
    "QE402": "Inactive Account",
    "QE429": "Rate Limited",
    "QE844": "Modify Limit Exceeded",
    "QE891": "TPL Modify Denied",
    "QE921": "Not FIFO Order",
    "QE922": "No FIFO Permission",
    "QE923": "Already Modified",
    "QS890": "TPL Cancel Failed",
    "QS913": "Already Picked Up (Modify)",
    "_status_500": "Server Error (500)",
    "_status_none": "Timeout / No Response",
}


def tev_query(urls, start_utc, end_utc, interval):
    """
    Per-time-bucket breakdown for TEV sections.
    Returns (rows, meta_dict) or (None, error_string).
    """
    es, index = get_es_client()
    if es is None:
        return None, "Elasticsearch not configured"

    error_filters = {
        code: {"match": {"response_body": code}} for code in _TEV_ERROR_CODES
    }
    error_filters["_status_500"] = {"term": {"response_status_code.keyword": "500"}}
    error_filters["_status_none"] = {"bool": {"must_not": {"exists": {"field": "response_status_code"}}}}

    hist = {
        "field": "@timestamp",
        ("fixed_interval" if interval in ("1h", "15m", "30m") else "calendar_interval"): interval,
        "time_zone": "Asia/Kolkata",
        "min_doc_count": 0,
    }

    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": [
                {"term": {"ENV.keyword": "prod"}},
                {"term": {"log_type.keyword": "TPL API Request"}},
                {"terms": {"request_url.keyword": urls}},
                {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
            ]}},
            "aggs": {
                "over_time": {
                    "date_histogram": hist,
                    "aggs": {
                        "failure_breakdown": {"filters": {"filters": error_filters}},
                    }
                }
            }
        })
    except Exception as ex:
        return None, str(ex)

    total, success, failed = 0, 0, 0
    rows = []
    for bucket in resp["aggregations"]["over_time"]["buckets"]:
        ts_ms = bucket["key"]
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
            "%d %b %H:%M" if interval == "1h" else "%d %b %Y"
        )
        t = bucket["doc_count"]
        breakdown = bucket["failure_breakdown"]["buckets"]
        f = sum(b["doc_count"] for b in breakdown.values())
        s = t - f
        total += t; success += s; failed += f
        rows.append({
            "ts": ts,
            "total": t,
            "success": s,
            "failed": f,
            "failure_breakdown": {k: b["doc_count"] for k, b in breakdown.items()},
        })

    meta = {"total": total, "success": success, "failed": failed,
            "error_labels": _TEV_ERROR_LABELS}
    return rows, meta


# ---------------------------------------------------------------------------
# Third-Party API Monitor constants
# ---------------------------------------------------------------------------

_TP_PATHS = {
    "Quote":        ["/client/price-calculate/", "/v2/client/price-calculate/"],
    "Create Order": ["/v2/client/order/", "/client/order/create/"],
    "Cancel":       ["/client/order/cancel/", "/v2/client/order/cancel/"],
    "Modify":       ["/v2/client/order/modify/", "/client/order/modify/"],
    "FIFO Create":  ["/v2/client/fifo-order/"],
    "FIFO Modify":  ["/v2/client/fifo/modify/"],
    "Track":        ["/v2/client/order/track/", "/client/order/track/"],
    "Order Details":["/v2/client/order/details/", "/client/order/details/"],
    "Webhook":      ["/client/webhook/", "/v2/client/webhook/"],
}

# Sub-merchant filter applies only to these operations
_TP_SUBMERCHANT_OPS = {"Quote", "Create Order"}

# Complete QE/BE error code map (from client/common/errors.py)
_ALL_QE_CODES = {
    "QE000": "Something Went Wrong",
    "QE400": "No API Key",
    "QE401": "Invalid API Key",
    "QE402": "Inactive Account",
    "QE403": "Invalid Order Key",
    "QE404": "No API Permission",
    "QE405": "No Courier Permission",
    "QE429": "Rate Limited",
    "QE800": "Validation Error",
    "QE801": "Region Not Serviceable",
    "QE802": "Service Unavailable",
    "QE803": "Intercity Unavailable",
    "QE804": "Weight Unavailable",
    "QE805": "Track Not Available",
    "QE806": "Location Fetch Fail",
    "QE807": "Dup Merchant Order ID",
    "QE808": "Invalid Time",
    "QE809": "Google API Fail",
    "QE810": "No Payment Permission",
    "QE811": "Address Fail",
    "QE812": "Item Category Desc Required",
    "QE813": "Distance Limit Exceeded",
    "QE814": "Amount Collection N/A",
    "QE815": "Amount Collection Limit",
    "QE816": "Credit Limit Exceeded",
    "QE817": "Credit 70% Warning",
    "QE818": "Invalid Version",
    "QE819": "Sub-Merchant No Order",
    "QE820": "Sub-Merchant No Permission",
    "QE821": "Insufficient Wallet",
    "QE822": "Amount Missing",
    "QE823": "Network Order ID Exists",
    "QE824": "Dup Network Order ID",
    "QE825": "Dup Merchant Order ID (v2)",
    "QE832": "Order Completed",
    "QE833": "Already Picked Up (Cancel)",
    "QE840": "Modify Amount Fail",
    "QE842": "Payment Initiated",
    "QE843": "Location Mod Unavailable",
    "QE844": "Modify Limit Exceeded",
    "QE846": "Delivery Stop",
    "QE847": "Drop Region N/A",
    "QE848": "Below Min Distance",
    "QE861": "Pincode Denied",
    "QS890": "TPL Cancel Failed",
    "QE891": "TPL Modify Denied",
    "QE901": "No SKU Permission",
    "QE902": "No SKU Modify Permission",
    "QE903": "No SKU Items",
    "QE904": "SKU Qty Zero",
    "QE905": "No Total Weight",
    "QE906": "SKU Modify Not Allowed",
    "QE907": "Invalid Selling Price",
    "QE908": "Invalid MRP",
    "QE909": "Invalid Total Price",
    "QE910": "Amount Negative",
    "QS911": "SKU Too Long",
    "QS912": "Item Name Too Long",
    "QS913": "Already Picked Up (Modify)",
    "QS914": "Wallet Timeout",
    "QS915": "Order Not Found",
    "QE916": "Region Restriction",
    "QE917": "Pincode Restriction",
    "QE918": "Empty Pincode",
    "QE919": "Pickup Time Error",
    "QE920": "Dup Store Order ID",
    "QE921": "Not FIFO Order",
    "QE922": "No FIFO Permission",
    "QE923": "Already Modified",
    "QE924": "Invalid FIFO Payment",
    "QE930": "Dup Order Address",
    "BE700": "Batch Not Enabled",
    "BE701": "Batch Region N/A",
    "BE702": "Batch Min Order",
    "BE703": "Batch Max Order",
    "BE704": "Batch Max Weight",
    "BE705": "Batch Max Distance",
    "BE706": "Batch Data Error",
    "BE707": "Batch Modify Denied",
    "BE708": "Batch Not Found",
    "BE709": "Batch Completed Cancel",
    "BE710": "Batch Picked Up Cancel",
    "BE711": "Batch Already Cancelled",
}


@st.cache_data(ttl=300)
def tp_fetch_users(start_utc, end_utc):
    es, index = get_es_client()
    if es is None:
        return []
    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": [
                {"term": {"ENV.keyword": "prod"}},
                {"term": {"log_type.keyword": "Third Party Request"}},
                {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
            ]}},
            "aggs": {"users": {"terms": {"field": "user.keyword", "size": 200}}}
        })
        return sorted(b["key"] for b in resp["aggregations"]["users"]["buckets"] if b["key"])
    except Exception:
        return []


def tp_columnar_query(start_utc, end_utc, operations, merchant_names=None):
    """
    Per-operation columnar breakdown.
    Returns {op: {"rows": [...], "error_ref": [...], "meta": {...}}} or (None, err).

    Success detection: QWQER success responses always have "error":false as the last
    field — after ES tokenisation the phrase "error false" (tokens: error → false)
    uniquely identifies success. Error responses have "is_success":false followed by
    "error":{...code...}, so "error false" does NOT appear (false precedes error).

    Error detection: match on the QE/BE code token (e.g. "QE801") — each code is a
    distinct alphanumeric token that won't appear in success responses.
    """
    es, index = get_es_client()
    if es is None:
        return None, "Elasticsearch not configured"

    must_base = [
        {"term": {"ENV.keyword": "prod"}},
        {"term": {"log_type.keyword": "Third Party Request"}},
        {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
    ]
    if merchant_names:
        must_base.append({"terms": {"user.keyword": merchant_names}})

    # Success = "error false" phrase in body (QWQER success format)
    success_f = {"match_phrase": {"response_body": "error false"}}

    # Per QE/BE code filters — match on the code token (e.g. "QE801")
    code_filters = {code: {"match": {"response_body": code}} for code in _ALL_QE_CODES}

    no_body_f = {"bool": {"must_not": {"exists": {"field": "response_body"}}}}

    # Unknown = body present + not success + no known error code (Swiggy/other format)
    unknown_f = {
        "bool": {
            "must":     [{"exists": {"field": "response_body"}}],
            "must_not": [{"match_phrase": {"response_body": "error false"}}]
                        + [{"match": {"response_body": code}} for code in _ALL_QE_CODES],
        }
    }

    sample_src = {"_source": ["response_body", "request_path", "user", "@timestamp"], "size": 3}

    results = {}
    for op in operations:
        paths = _TP_PATHS.get(op, [])
        if not paths:
            continue
        must = must_base + [{"terms": {"request_path.keyword": paths}}]
        try:
            resp = es.search(index=index, size=0, **{
                "query": {"bool": {"must": must}},
                "aggs": {
                    "by_merchant": {
                        "terms": {"field": "user.keyword", "size": 200},
                        "aggs": {
                            "success":     {"filter": success_f},
                            "known_codes": {"filters": {"filters": code_filters}},
                            "no_body":     {"filter": no_body_f},
                            "unknown":     {"filter": unknown_f},
                        }
                    },
                    "global_codes": {
                        "filters": {
                            "filters": {
                                **code_filters,
                                "_no_body":  no_body_f,
                                "_unknown":  unknown_f,
                            }
                        },
                        "aggs": {"samples": {"top_hits": {**sample_src}}}
                    },
                    "global_success": {"filter": success_f},
                }
            })
        except Exception as ex:
            results[op] = {"error": str(ex)}
            continue

        total = resp["hits"]["total"]["value"]
        grand_success = resp["aggregations"]["global_success"]["doc_count"]

        rows = []
        for mb in resp["aggregations"]["by_merchant"]["buckets"]:
            if not mb["key"]:
                continue
            t       = mb["doc_count"]
            success = mb["success"]["doc_count"]
            no_b    = mb["no_body"]["doc_count"]
            unknown = mb["unknown"]["doc_count"]
            errors  = t - success - no_b - unknown  # known QE errors

            row = {
                "Merchant":  mb["key"],
                "Total":     t,
                "Success":   success,
                "Errors":    errors,
                "Success %": round(success / t * 100, 1) if t else 0,
            }

            code_bkts = mb["known_codes"]["buckets"]
            for code, label in _ALL_QE_CODES.items():
                cnt = code_bkts.get(code, {}).get("doc_count", 0)
                if cnt:
                    row[f"{code}: {label}"] = cnt

            if no_b:
                row["No Response Body"] = no_b
            if unknown:
                row["Unknown Format"] = unknown
            rows.append(row)
        rows.sort(key=lambda r: -r["Total"])

        gb = resp["aggregations"]["global_codes"]["buckets"]
        error_ref = []
        for code, label in _ALL_QE_CODES.items():
            cnt = gb.get(code, {}).get("doc_count", 0)
            if cnt:
                samps = [h["_source"] for h in gb[code]["samples"]["hits"]["hits"]]
                error_ref.append({"code": code, "label": label, "count": cnt, "samples": samps})
        for key, lbl in [("_no_body", "No Response Body"), ("_unknown", "Unknown Format")]:
            cnt = gb.get(key, {}).get("doc_count", 0)
            if cnt:
                samps = [h["_source"] for h in gb[key]["samples"]["hits"]["hits"]]
                error_ref.append({"code": key, "label": lbl, "count": cnt, "samples": samps})
        error_ref.sort(key=lambda x: -x["count"])

        grand_failed = total - grand_success
        results[op] = {
            "rows":      rows,
            "error_ref": error_ref,
            "meta":      {"total": total, "success": grand_success, "failed": grand_failed},
        }

    return results, None


# ---------------------------------------------------------------------------
# TPL Monitor — provider discovery + per-provider queries
# ---------------------------------------------------------------------------

_TPL_PROVIDER_LABELS = {
    "tevhr solutions": "TEV (White-Label DMS)",
    "yumove":          "Yumove (Yulu)",
    "ek_bharat":       "EK Bharat (Adloggs)",
}


@st.cache_data(ttl=300)
def tpl_fetch_providers():
    es, index = get_es_client()
    if es is None:
        return list(_TPL_PROVIDER_LABELS.keys())
    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": [
                {"term": {"ENV.keyword": "prod"}},
                {"term": {"log_type.keyword": "TPL API Request"}},
                {"range": {"@timestamp": {"gte": "now-30d", "lte": "now"}}},
            ]}},
            "aggs": {"providers": {"terms": {"field": "provider.keyword", "size": 50}}}
        })
        found = [b["key"] for b in resp["aggregations"]["providers"]["buckets"] if b["key"]]
        return found or list(_TPL_PROVIDER_LABELS.keys())
    except Exception:
        return list(_TPL_PROVIDER_LABELS.keys())


def _normalize_tpl_url(url: str) -> str:
    """Strip order IDs / numeric keys from TPL URLs before grouping."""
    # Swiggy: /swiggy/240908689658651/cancel → /swiggy/*/cancel
    url = re.sub(r'/swiggy/[^/]+/(cancel|status)', r'/swiggy/*/\1', url)
    # Track: /v1/order/91579396/track → /v1/order/*/track
    url = re.sub(r'/order/\d{6,}/(\w+)', r'/order/*/\1', url)
    return url


def _tpl_op_from_url(url: str) -> str:
    url = url.lower()
    if "price-calculate" in url or "quotes" in url:
        return "Quote"
    if "fifo/modify" in url or "order/modify" in url:
        return "Modify"
    if "fifo-order" in url:
        return "FIFO Create"
    if "swiggy/create" in url:
        return "Swiggy Create"
    if "swiggy" in url and "cancel" in url:
        return "Swiggy Cancel"
    if "/v2/client/order/" in url or "order/create" in url or "/v2/create" in url:
        return "Create"
    if "service/availability" in url:
        return "Quote / Availability"
    if "cancel" in url:
        return "Cancel"
    if "update" in url:
        return "Update"
    if "track" in url:
        return "Track"
    return "Other"


def tpl_columnar_query(start_utc, end_utc, provider):
    """
    Dispatch to provider-specific query. Returns (op_rows, error_detail, meta, None)
    or (None, None, None, err_str).
    """
    if provider == "yumove":
        return _tpl_yumove_query(start_utc, end_utc)
    if provider == "ek_bharat":
        return _tpl_ekbharat_query(start_utc, end_utc)
    # TEV (tevhr solutions) and any other provider: QWQER format
    return _tpl_qwqer_format_query(start_utc, end_utc, provider)


# --- TEV / QWQER-format providers ---

def _tpl_qwqer_format_query(start_utc, end_utc, provider):
    """
    TEV white-label and any other QWQER-format TPL provider.
    Success detection: "error false" phrase (same as Third Party tab).
    Error detection: QE/BE code tokens.
    URL normalization: strip Swiggy order IDs.
    """
    es, index = get_es_client()
    if es is None:
        return None, None, None, "Elasticsearch not configured"

    must = [
        {"term": {"ENV.keyword": "prod"}},
        {"term": {"log_type.keyword": "TPL API Request"}},
        {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
        {"term": {"provider.keyword": provider}},
    ]

    success_f  = {"match_phrase": {"response_body": "error false"}}
    no_body_f  = {"bool": {"must_not": {"exists": {"field": "response_body"}}}}
    code_filters = {code: {"match": {"response_body": code}} for code in _ALL_QE_CODES}
    unknown_f  = {
        "bool": {
            "must":     [{"exists": {"field": "response_body"}}],
            "must_not": [{"match_phrase": {"response_body": "error false"}}]
                        + [{"match": {"response_body": c}} for c in _ALL_QE_CODES],
        }
    }

    sample_src = {"_source": ["response_body", "request_url", "provider", "@timestamp",
                               "response_status_code"], "size": 3}
    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": must}},
            "aggs": {
                "by_url": {
                    "terms": {"field": "request_url.keyword", "size": 100},
                    "aggs": {
                        "success":     {"filter": success_f},
                        "known_codes": {"filters": {"filters": code_filters}},
                        "no_body":     {"filter": no_body_f},
                        "unknown":     {"filter": unknown_f},
                    }
                },
                "global_codes": {
                    "filters": {"filters": {**code_filters, "_no_body": no_body_f, "_unknown": unknown_f}},
                    "aggs": {"samples": {"top_hits": {**sample_src}}}
                },
                "global_success": {"filter": success_f},
            }
        })
    except Exception as ex:
        return None, None, None, str(ex)

    total = resp["hits"]["total"]["value"]
    grand_success = resp["aggregations"]["global_success"]["doc_count"]

    # Normalise URLs and merge buckets
    url_merged = {}
    for b in resp["aggregations"]["by_url"]["buckets"]:
        raw_url = b["key"]
        norm    = _normalize_tpl_url(raw_url)
        op      = _tpl_op_from_url(norm)
        key     = (op, norm)
        if key not in url_merged:
            url_merged[key] = {"op": op, "url": norm, "total": 0, "success": 0, "known": 0, "no_body": 0, "unknown": 0, "codes": {}}
        m = url_merged[key]
        m["total"]   += b["doc_count"]
        m["success"] += b["success"]["doc_count"]
        m["no_body"] += b["no_body"]["doc_count"]
        m["unknown"] += b["unknown"]["doc_count"]
        m["known"]   += sum(bk["doc_count"] for bk in b["known_codes"]["buckets"].values())
        for code, bk in b["known_codes"]["buckets"].items():
            m["codes"][code] = m["codes"].get(code, 0) + bk["doc_count"]

    op_rows = []
    for (op, norm), m in url_merged.items():
        t = m["total"]
        row = {
            "Operation":   m["op"],
            "URL":         m["url"],
            "Total":       t,
            "Success":     m["success"],
            "Errors":      m["known"],
            "No Body":     m["no_body"],
            "Unknown":     m["unknown"],
            "Success %":   round(m["success"] / t * 100, 1) if t else 0,
        }
        for code, cnt in sorted(m["codes"].items(), key=lambda x: -x[1]):
            if cnt:
                row[f"{code}: {_ALL_QE_CODES.get(code, code)}"] = cnt
        op_rows.append(row)
    op_rows.sort(key=lambda r: -r["Total"])

    gb = resp["aggregations"]["global_codes"]["buckets"]
    error_detail = []
    for code, label in _ALL_QE_CODES.items():
        cnt = gb.get(code, {}).get("doc_count", 0)
        if cnt:
            samps = [h["_source"] for h in gb[code]["samples"]["hits"]["hits"]]
            error_detail.append({"code": code, "label": label, "count": cnt, "samples": samps})
    for key, lbl in [("_no_body", "No Response Body"), ("_unknown", "Unknown Format")]:
        cnt = gb.get(key, {}).get("doc_count", 0)
        if cnt:
            samps = [h["_source"] for h in gb[key]["samples"]["hits"]["hits"]]
            error_detail.append({"code": key, "label": lbl, "count": cnt, "samples": samps})
    error_detail.sort(key=lambda x: -x["count"])

    meta = {"total": total, "success": grand_success, "failed": total - grand_success}
    return op_rows, error_detail, meta, None


# --- Yumove ---
#
# Yumove response format:
#   Success:      HTTP 200 + response body does NOT contain token "reason_id"
#   Error:        response body contains token "reason_id" (is_serviceable=false responses)
#   No response:  response_status_code absent (timeout)
#
# Important: match("reason_id") not match_phrase("reason id") — underscore is not a
# word boundary in Unicode UAX#29 so "reason_id" is ONE token, not two.
#
# Known reason_ids (from Yumove API doc + observed logs):
#   Quote endpoint errors (is_serviceable=false):
#     204 "DROP is not Serviceable"
#     205 "PICKUP is not Serviceable"
#     206 "PICKUP and DROP are not Serviceable"
#     208 "No delivery partners are available"
#     209 "No active delivery partners nearby"
#     210 "Not serviceable at the moment"
#     218 "Maximum serviceable distance exceeded"  (observed)
#   Order-level errors:
#     101 "Order already picked up (cancel)"
#     102 "Order already picked up (update)"
#     103 "Order not found"
#     104 "Missing API key"
#     105 "Invalid API key"
#     109 "Quote validity expired"
#     110 "Cannot cancel all items"
#     113 "Request unfulfilled"
#     114 "Invalid coordinates"
#     117 "Maximum order amount exceeded"
#     118 "Maximum distance exceeded"

_YUMOVE_REASON_MSGS = {
    "204": ("DROP Not Serviceable",           "DROP is not Serviceable"),
    "205": ("PICKUP Not Serviceable",         "PICKUP is not Serviceable"),
    "206": ("PICKUP & DROP Not Serviceable",  "PICKUP and DROP"),
    "208": ("No Partners Available",          "no delivery partners are available"),
    "209": ("No Active Partners Nearby",      "no active delivery partners"),
    "210": ("Not Serviceable Now",            "Not serviceable at the moment"),
    "218": ("Max Serviceable Distance",       "Maximum serviceable distance exceeded"),
    "101": ("Already Picked Up (Cancel)",     "already picked up"),
    "102": ("Already Picked Up (Update)",     "already been picked"),
    "103": ("Order Not Found",                "Order not found"),
    "104": ("Missing API Key",                "missing api key"),
    "105": ("Invalid API Key",                "invalid api key"),
    "109": ("Quote Expired",                  "quote validity"),
    "110": ("Cannot Cancel All Items",        "cancel all items"),
    "113": ("Request Unfulfilled",            "unfulfilled"),
    "114": ("Invalid Coordinates",            "invalid coordinates"),
    "117": ("Max Order Amount Exceeded",      "max amount"),
    "118": ("Max Distance Exceeded",          "max distance"),
}


def _tpl_yumove_query(start_utc, end_utc):
    es, index = get_es_client()
    if es is None:
        return None, None, None, "Elasticsearch not configured"

    must = [
        {"term": {"ENV.keyword": "prod"}},
        {"term": {"log_type.keyword": "TPL API Request"}},
        {"term": {"provider.keyword": "yumove"}},
        {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
    ]

    # "reason_id" token: kept as ONE token by ES standard tokenizer (underscore is a
    # connector, not a word boundary). match("reason_id") works; match_phrase("reason id") does not.
    error_f      = {"match": {"response_body": "reason_id"}}
    no_resp_f    = {"bool": {"must_not": {"exists": {"field": "response_status_code"}}}}
    success_f    = {"bool": {
        "must":     [{"term": {"response_status_code.keyword": "200"}}],
        "must_not": [{"match": {"response_body": "reason_id"}}],
    }}

    reason_filters = {
        rid: {"match_phrase": {"response_body": msg}}
        for rid, (label, msg) in _YUMOVE_REASON_MSGS.items()
    }

    sample_src = {"_source": ["response_body", "request_url", "@timestamp",
                               "response_status_code"], "size": 3}
    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": must}},
            "aggs": {
                "by_url": {
                    "terms": {"field": "request_url.keyword", "size": 50},
                    "aggs": {
                        "success":    {"filter": success_f},
                        "errors":     {"filter": error_f},
                        "no_response":{"filter": no_resp_f},
                        # Per-reason breakdown per URL
                        "by_reason":  {"filters": {"filters": reason_filters}},
                    }
                },
                "global_success":    {"filter": success_f},
                "global_error":      {"filter": error_f},
                "global_no_resp":    {"filter": no_resp_f},
                # Global per-reason with samples for the error detail panel
                "by_reason": {
                    "filters": {"filters": reason_filters},
                    "aggs": {"samples": {"top_hits": {**sample_src}}}
                },
                "error_samples": {
                    "filter": error_f,
                    "aggs":   {"hits": {"top_hits": {**sample_src}}}
                },
            }
        })
    except Exception as ex:
        return None, None, None, str(ex)

    total        = resp["hits"]["total"]["value"]
    grand_success= resp["aggregations"]["global_success"]["doc_count"]
    grand_error  = resp["aggregations"]["global_error"]["doc_count"]
    grand_no_resp= resp["aggregations"]["global_no_resp"]["doc_count"]

    op_rows = []
    for b in resp["aggregations"]["by_url"]["buckets"]:
        raw_url = b["key"]
        norm    = _normalize_tpl_url(raw_url)
        op      = _tpl_op_from_url(norm)
        t       = b["doc_count"]
        s       = b["success"]["doc_count"]
        e       = b["errors"]["doc_count"]
        nr      = b["no_response"]["doc_count"]
        row = {
            "Operation": op,
            "URL":       norm,
            "Total":     t,
            "Success":   s,
            "Errors":    e,
            "No Response": nr,
            "Success %": round(s / t * 100, 1) if t else 0,
        }
        # Add individual reason columns (only non-zero)
        per_url_reasons = b["by_reason"]["buckets"]
        for rid, (label, _) in _YUMOVE_REASON_MSGS.items():
            cnt = per_url_reasons.get(rid, {}).get("doc_count", 0)
            if cnt:
                row[f"r{rid}: {label}"] = cnt
        op_rows.append(row)
    op_rows.sort(key=lambda r: -r["Total"])

    # Global per-reason breakdown with samples (shown in error detail panel)
    rb = resp["aggregations"]["by_reason"]["buckets"]
    error_detail = []
    known_sum    = 0
    for rid, (label, _) in _YUMOVE_REASON_MSGS.items():
        cnt  = rb.get(rid, {}).get("doc_count", 0)
        if cnt:
            samps = [h["_source"] for h in rb[rid]["samples"]["hits"]["hits"]]
            error_detail.append({"code": f"reason_{rid}", "label": f"reason_id {rid}: {label}",
                                  "count": cnt, "samples": samps})
            known_sum += cnt
    other_err = grand_error - known_sum
    if other_err > 0:
        samps = [h["_source"] for h in
                 resp["aggregations"]["error_samples"]["hits"]["hits"]["hits"]]
        error_detail.append({"code": "reason_other", "label": "Other reason_id (unknown)",
                              "count": other_err, "samples": samps})
    if grand_no_resp:
        error_detail.append({"code": "no_response", "label": "No Response (timeout)",
                              "count": grand_no_resp, "samples": []})
    error_detail.sort(key=lambda x: -x["count"])

    meta = {"total": total, "success": grand_success,
            "failed": grand_error + grand_no_resp}
    return op_rows, error_detail, meta, None


# --- EK Bharat (Adloggs) ---
#
# EK Bharat response format:
#   Success:  {"status":true,"code":200,"message":"...","data":{...}}
#   Error:    {"status":false,"code":202,"message":"error message","data":{...}}
#   Auth err: HTTP 400 body (no status field) with "Auth Error"
#
# Detection: "status true" phrase → success; "status false" → error.
# Error breakdown by message text (unique per error type).

_EKB_ERROR_MSGS = {
    "service_unavailable": ("Service Not Available",         "service not available"),
    "currently_unavailable":("Currently Unavailable (data.code)",  "currently_service_not_available"),
    "already_picked_up":   ("Already Picked Up",             "already picked up"),
    "already_delivered":   ("Already Delivered",             "already delivered"),
    "invalid_order":       ("Invalid Order UUID",            "Invalid order uuid"),
    "riders_unavailable":  ("Riders Not Available",          "Riders not available"),
    "distance_too_long":   ("Distance Too Long",             "Distance too long"),
    "auth_error":          ("Auth Error (HTTP 400)",          "Auth Error"),
    "pickup_missing":      ("Pickup Address Missing",        "Pickup address missing"),
    "wallet_not_enabled":  ("Wallet Not Enabled",            "Wallet not enabled"),
}


def _tpl_ekbharat_query(start_utc, end_utc):
    es, index = get_es_client()
    if es is None:
        return None, None, None, "Elasticsearch not configured"

    must = [
        {"term": {"ENV.keyword": "prod"}},
        {"term": {"log_type.keyword": "TPL API Request"}},
        {"term": {"provider.keyword": "ek_bharat"}},
        {"range": {"@timestamp": {"gte": start_utc, "lte": end_utc}}},
    ]

    success_f    = {"match_phrase": {"response_body": "status true"}}
    no_body_f    = {"bool": {"must_not": {"exists": {"field": "response_body"}}}}
    # Error = body exists + not success
    error_f      = {"bool": {
        "must":     [{"exists": {"field": "response_body"}}],
        "must_not": [{"match_phrase": {"response_body": "status true"}}],
    }}

    msg_filters = {
        key: {"match_phrase": {"response_body": phrase}}
        for key, (label, phrase) in _EKB_ERROR_MSGS.items()
    }

    sample_src = {"_source": ["response_body", "request_url", "@timestamp",
                               "response_status_code"], "size": 3}
    try:
        resp = es.search(index=index, size=0, **{
            "query": {"bool": {"must": must}},
            "aggs": {
                "by_url": {
                    "terms": {"field": "request_url.keyword", "size": 50},
                    "aggs": {
                        "success": {"filter": success_f},
                        "errors":  {"filter": error_f},
                        "no_body": {"filter": no_body_f},
                        # Per-error-message breakdown per URL
                        "by_msg":  {"filters": {"filters": msg_filters}},
                    }
                },
                "global_success": {"filter": success_f},
                "global_error":   {"filter": error_f},
                "global_no_body": {"filter": no_body_f},
                # Global per-message with samples for the error detail panel
                "by_msg": {
                    "filters": {"filters": msg_filters},
                    "aggs": {"samples": {"top_hits": {**sample_src}}}
                },
                "error_samples": {
                    "filter": error_f,
                    "aggs":   {"hits": {"top_hits": {**sample_src}}}
                },
            }
        })
    except Exception as ex:
        return None, None, None, str(ex)

    total        = resp["hits"]["total"]["value"]
    grand_success= resp["aggregations"]["global_success"]["doc_count"]
    grand_error  = resp["aggregations"]["global_error"]["doc_count"]
    grand_no_body= resp["aggregations"]["global_no_body"]["doc_count"]

    op_rows = []
    for b in resp["aggregations"]["by_url"]["buckets"]:
        raw_url = b["key"]
        norm = raw_url.replace("https://app.adloggs.com/aa/oporder", "")
        op   = _tpl_op_from_url(raw_url)
        t    = b["doc_count"]
        s    = b["success"]["doc_count"]
        e    = b["errors"]["doc_count"]
        nb   = b["no_body"]["doc_count"]
        row = {
            "Operation": op,
            "URL":       norm or raw_url,
            "Total":     t,
            "Success":   s,
            "Errors":    e,
            "No Body":   nb,
            "Success %": round(s / t * 100, 1) if t else 0,
        }
        # Add individual error-message columns (only non-zero)
        per_url_msgs = b["by_msg"]["buckets"]
        for key, (label, _) in _EKB_ERROR_MSGS.items():
            cnt = per_url_msgs.get(key, {}).get("doc_count", 0)
            if cnt:
                row[label] = cnt
        op_rows.append(row)
    op_rows.sort(key=lambda r: -r["Total"])

    # Global per-message breakdown with samples (shown in error detail panel)
    mb = resp["aggregations"]["by_msg"]["buckets"]
    error_detail = []
    known_sum    = 0
    for key, (label, _) in _EKB_ERROR_MSGS.items():
        cnt = mb.get(key, {}).get("doc_count", 0)
        if cnt:
            samps = [h["_source"] for h in mb[key]["samples"]["hits"]["hits"]]
            error_detail.append({"code": key, "label": label, "count": cnt, "samples": samps})
            known_sum += cnt
    other_err = grand_error - known_sum
    if other_err > 0:
        samps = [h["_source"] for h in
                 resp["aggregations"]["error_samples"]["hits"]["hits"]["hits"]]
        error_detail.append({"code": "other", "label": "Other Error (unknown message)",
                              "count": other_err, "samples": samps})
    if grand_no_body:
        error_detail.append({"code": "no_body", "label": "No Response Body",
                              "count": grand_no_body, "samples": []})
    error_detail.sort(key=lambda x: -x["count"])

    meta = {"total": total, "success": grand_success,
            "failed": grand_error + grand_no_body}
    return op_rows, error_detail, meta, None
