#!/usr/bin/env python3
"""Generate KOPIYKA report (index.html) from Databricks.

Group: KOPIYKA — covers three brands inside the same network:
  - KOPIYKA  (full-format stores)
  - KOPIYKA MINI  (compact-format stores)
  - SANTIM  (express stores)

All data is fetched with `dim_provider_v2.group_name = 'KOPIYKA'` filter.

Outputs a single self-contained `index.html` with four tabs:
Monthly / Weekly / Stores / Failed orders list (weekly, exportable).
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from databricks import sql as dbsql

_ROOT = Path(__file__).parent


def _load_dotenv():
    """Load Reports/KOPIYKA/.env into os.environ (not committed to git)."""
    env_file = _ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"Missing {name}. Create {_ROOT / '.env'} from .env.example "
            f"or export the variable.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


DATABRICKS_HOST = _require_env("DATABRICKS_HOST")
DATABRICKS_TOKEN = _require_env("DATABRICKS_TOKEN")
if DATABRICKS_TOKEN.startswith("your") or len(DATABRICKS_TOKEN) < 32:
    print(
        "DATABRICKS_TOKEN у .env — заглушка з .env.example, не справжній токен.\n"
        "Databricks → User Settings → Developer → Access tokens → Generate new token.\n"
        "Вставте в .env рядок DATABRICKS_TOKEN=dapi... (без лапок).",
        file=sys.stderr,
    )
    sys.exit(1)
DATABRICKS_HTTP_PATH = os.environ.get("DATABRICKS_HTTP_PATH", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

PARTNER_NAME = "KOPIYKA"
PARTNER_DISPLAY_NAME = "KOPIYKA"
PARTNER_BRANDS_LABEL = "KOPIYKA · KOPIYKA MINI · SANTIM"

TEMPLATE_PATH = Path(__file__).parent / "template.html"
OUTPUT_PATH = Path(__file__).parent / "index.html"
DATA_PATH = Path(__file__).parent / "report_data.json"


def _connect_kwargs():
    """Extra connect args. Set DATABRICKS_TLS_NO_VERIFY=1 in .env on Mac behind corporate SSL proxy."""
    kwargs = {}
    if os.environ.get("DATABRICKS_TLS_NO_VERIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        kwargs["_tls_no_verify"] = True
    return kwargs


def get_connection():
    extra = _connect_kwargs()
    if DATABRICKS_HTTP_PATH:
        return dbsql.connect(
            server_hostname=DATABRICKS_HOST,
            http_path=DATABRICKS_HTTP_PATH,
            access_token=DATABRICKS_TOKEN,
            **extra,
        )
    return dbsql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=f"/sql/1.0/warehouses/{DATABRICKS_WAREHOUSE_ID}",
        access_token=DATABRICKS_TOKEN,
        **extra,
    )


def run_query(cursor, query):
    cursor.execute(query)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def to_serializable(rows):
    out = []
    for row in rows:
        d = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif hasattr(v, "as_py"):
                d[k] = v.as_py()
            elif hasattr(v, "__float__"):
                d[k] = float(v)
            elif hasattr(v, "__int__"):
                d[k] = int(v)
            else:
                d[k] = v
        out.append(d)
    return out


def _current_month_period() -> str:
    today = datetime.now().date()
    return f"{today.year:04d}-{today.month:02d}"


def _monthly_boundaries():
    """Return (start, end) for last 4 completed calendar months (excludes current month)."""
    today = datetime.now().date()
    first_of_current = today.replace(day=1)
    last_completed = first_of_current - timedelta(days=1)
    year, month = last_completed.year, last_completed.month - 3
    while month <= 0:
        month += 12
        year -= 1
    first_month_start = datetime(year, month, 1).date()
    return str(first_month_start), str(last_completed)


def _filter_completed_months(rows):
    """Drop yyyy-MM periods on or after the current calendar month (safety net)."""
    cutoff = _current_month_period()
    return [
        r
        for r in rows
        if not (p := str(r.get("period", "")))
        or len(p) != 7
        or p[4] != "-"
        or p < cutoff
    ]


SUCCESS_RESOLUTION_REASONS = (
    "automatically_succeeded",
    "manually_succeeded_by_cs",
)


def _week_boundaries():
    """Return (Monday of earliest week, Sunday end of latest complete week)."""
    today = datetime.now().date()
    last_sunday = today - timedelta(days=today.isoweekday())
    last_monday = last_sunday - timedelta(days=6)
    first_monday = last_monday - timedelta(days=21)  # 4 complete weeks
    return str(first_monday), str(last_sunday)


MONTHLY_START, MONTHLY_END = _monthly_boundaries()
WEEKLY_START, WEEKLY_END = _week_boundaries()

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

NETWORK_STORES = f"""
SELECT
    p.provider_id,
    p.provider_name,
    p.brand_name,
    p.city_name
FROM hive_metastore.ng_delivery_spark.dim_provider_v2 p
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND (
    (p.provider_status = 'active' AND p.lifecycle_status = 'ready_for_work')
    OR (p.provider_status = 'hidden' AND p.lifecycle_status = 'hidden')
  )
ORDER BY p.brand_name, p.provider_name
LIMIT 500
"""

NETWORK_BRAND_BREAKDOWN = f"""
SELECT
    p.brand_name,
    COUNT(*) AS stores
FROM hive_metastore.ng_delivery_spark.dim_provider_v2 p
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND (
    (p.provider_status = 'active' AND p.lifecycle_status = 'ready_for_work')
    OR (p.provider_status = 'hidden' AND p.lifecycle_status = 'hidden')
  )
GROUP BY p.brand_name
ORDER BY stores DESC
LIMIT 20
"""

FINANCIAL_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah,
    SUM(f.provider_price_before_discount) / NULLIF(COUNT(*), 0) AS merchant_price_per_order,
    SUM(f.order_gmv) AS gmv_uah,
    SUM(f.order_gmv) / NULLIF(COUNT(*), 0) AS aov_uah,
    COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS users_activated,
    COUNT(DISTINCT f.user_id) AS active_users,
    SUM(f.total_refunded_amount) / NULLIF(SUM(f.order_gmv), 0) * 100 AS refund_rate_pct
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{MONTHLY_START}'
  AND f.order_created_date <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

FINANCIAL_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah,
    SUM(f.provider_price_before_discount) / NULLIF(COUNT(*), 0) AS merchant_price_per_order,
    SUM(f.order_gmv) AS gmv_uah,
    SUM(f.order_gmv) / NULLIF(COUNT(*), 0) AS aov_uah,
    COUNT(DISTINCT CASE WHEN f.is_first_delivery_order THEN f.user_id END) AS users_activated,
    COUNT(DISTINCT f.user_id) AS active_users,
    SUM(f.total_refunded_amount) / NULLIF(SUM(f.order_gmv), 0) * 100 AS refund_rate_pct
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

OPERATIONAL_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS delivered_orders,
    COUNT(DISTINCT f.provider_id) AS stores_with_orders,
    SUM(CASE WHEN f.is_honey_order THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS honey_order_rate,
    SUM(CASE WHEN f.is_bad_order THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS bad_order_rate,
    SUM(CASE WHEN f.is_order_delivered_5_min_late THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS late_delivery_rate,
    SUM(CASE WHEN f.is_order_late_to_partner_5_min THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS late_pickup_rate,
    AVG(f.order_delivery_minutes) AS avg_delivery_minutes,
    AVG(f.courier_delivery_time_min) AS avg_courier_delivery_min
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{MONTHLY_START}'
  AND f.order_created_date <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

OPERATIONAL_WEEKLY = OPERATIONAL_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{MONTHLY_START}'\n  AND f.order_created_date <= '{MONTHLY_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

REPLACEMENT_ADJUSTMENT_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM') AS period,
    ROUND(SUM(f.order_item_adjustment_rate_value * f.order_item_adjustment_rate_weight)
        / NULLIF(SUM(f.order_item_adjustment_rate_weight), 0) * 100, 2) AS adjustment_rate,
    ROUND(SUM(f.order_item_replacement_rate_value * f.order_item_replacement_rate_weight)
        / NULLIF(SUM(f.order_item_replacement_rate_weight), 0) * 100, 2) AS replacement_rate
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{MONTHLY_START}'
  AND f.metric_timestamp_local <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

REPLACEMENT_ADJUSTMENT_WEEKLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM-dd') AS period,
    ROUND(SUM(f.order_item_adjustment_rate_value * f.order_item_adjustment_rate_weight)
        / NULLIF(SUM(f.order_item_adjustment_rate_weight), 0) * 100, 2) AS adjustment_rate,
    ROUND(SUM(f.order_item_replacement_rate_value * f.order_item_replacement_rate_weight)
        / NULLIF(SUM(f.order_item_replacement_rate_weight), 0) * 100, 2) AS replacement_rate
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{WEEKLY_START}'
  AND f.metric_timestamp_local <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

FAILED_ORDERS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    COUNT(*) AS total_placed,
    SUM(CASE WHEN f.order_state = 'delivered' THEN 1 ELSE 0 END) AS delivered,
    SUM(CASE WHEN f.order_state != 'delivered' THEN 1 ELSE 0 END) AS failed_total,
    SUM(CASE WHEN f.order_state != 'delivered' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) * 100 AS failed_rate_pct
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{MONTHLY_START}'
  AND f.order_created_date <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

FAILED_ORDERS_WEEKLY = FAILED_ORDERS_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{MONTHLY_START}'\n  AND f.order_created_date <= '{MONTHLY_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

FAILED_REASONS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    r.reason,
    r.actor_type,
    COUNT(*) AS cnt
FROM hive_metastore.ng_delivery_spark.delivery_order_order_resolution r
    JOIN hive_metastore.ng_delivery_spark.fact_order_delivery f ON r.order_id = f.order_id
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{MONTHLY_START}'
  AND f.order_created_date <= '{MONTHLY_END}'
  AND f.order_state != 'delivered'
  AND r.reason NOT IN ({", ".join(repr(x) for x in SUCCESS_RESOLUTION_REASONS)})
GROUP BY 1, r.reason, r.actor_type
ORDER BY 1, cnt DESC
"""

FAILED_REASONS_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    r.reason,
    r.actor_type,
    COUNT(*) AS cnt
FROM hive_metastore.ng_delivery_spark.delivery_order_order_resolution r
    JOIN hive_metastore.ng_delivery_spark.fact_order_delivery f ON r.order_id = f.order_id
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
  AND f.order_state != 'delivered'
  AND r.reason NOT IN ({", ".join(repr(x) for x in SUCCESS_RESOLUTION_REASONS)})
GROUP BY 1, r.reason, r.actor_type
ORDER BY 1, cnt DESC
"""

_success_filter = ", ".join(repr(x) for x in SUCCESS_RESOLUTION_REASONS)
FAILED_ORDERS_LIST_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    f.order_reference_id,
    f.provider_id,
    p.provider_name,
    p.brand_name,
    f.order_state,
    CAST(f.order_created_date AS STRING) AS order_created_at,
    array_join(
        sort_array(collect_set(concat(r.reason, ' · ', r.actor_type))),
        '; '
    ) AS cancellation_detail
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
    LEFT JOIN hive_metastore.ng_delivery_spark.delivery_order_order_resolution r
        ON r.order_id = f.order_id
        AND r.reason NOT IN ({_success_filter})
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
  AND f.order_state != 'delivered'
GROUP BY
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd'),
    f.order_reference_id,
    f.provider_id,
    p.provider_name,
    p.brand_name,
    f.order_state,
    f.order_created_date
ORDER BY p.brand_name, p.provider_name, f.order_created_date DESC
LIMIT 5000
"""

CAMPAIGNS_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period,
    SUM(f.total_order_item_discount) AS campaigns_discount_uah,
    SUM(f.total_order_item_discount)
        - SUM(f.provider_price_before_discount - f.provider_price_after_discount) AS bolt_spend_uah,
    SUM(f.provider_price_before_discount - f.provider_price_after_discount) AS merchant_spend_uah,
    COUNT(CASE WHEN f.total_order_item_discount > 0 THEN 1 END) AS campaign_orders
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{MONTHLY_START}'
  AND f.order_created_date <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

CAMPAIGNS_WEEKLY = CAMPAIGNS_MONTHLY.replace(
    "DATE_FORMAT(f.order_created_date, 'yyyy-MM') AS period",
    "DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period",
).replace(
    f"AND f.order_created_date >= '{MONTHLY_START}'\n  AND f.order_created_date <= '{MONTHLY_END}'",
    f"AND f.order_created_date >= '{WEEKLY_START}'\n  AND f.order_created_date <= '{WEEKLY_END}'",
)

ACCEPTANCE_AVAILABILITY_MONTHLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM') AS period,
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{MONTHLY_START}'
  AND f.metric_timestamp_local <= '{MONTHLY_END}'
GROUP BY 1
ORDER BY 1
"""

ACCEPTANCE_AVAILABILITY_WEEKLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM-dd') AS period,
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{WEEKLY_START}'
  AND f.metric_timestamp_local <= '{WEEKLY_END}'
GROUP BY 1
ORDER BY 1
"""

ACCEPTANCE_AVAILABILITY_CURRENT = f"""
SELECT
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= DATE_SUB(CURRENT_DATE(), 7)
"""

STORE_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', f.order_created_date), 'yyyy-MM-dd') AS period,
    f.provider_id,
    f.provider_name,
    p.brand_name,
    f.city_name,
    COUNT(*) AS orders,
    SUM(f.provider_price_before_discount) AS merchant_price_uah,
    SUM(f.provider_price_before_discount) / NULLIF(COUNT(*), 0) AS aov_uah
FROM hive_metastore.ng_delivery_spark.fact_order_delivery f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.order_state = 'delivered'
  AND f.order_created_date >= '{WEEKLY_START}'
  AND f.order_created_date <= '{WEEKLY_END}'
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, orders DESC
LIMIT 5000
"""

STORE_QUALITY_WEEKLY = f"""
SELECT
    DATE_FORMAT(f.metric_timestamp_local, 'yyyy-MM-dd') AS period,
    f.provider_id,
    p.provider_name,
    ROUND(SUM(f.provider_active_rate_value * f.provider_active_rate_weight)
        / NULLIF(SUM(f.provider_active_rate_weight), 0) * 100, 1) AS availability_rate,
    ROUND(SUM(f.provider_acceptance_rate_value * f.provider_acceptance_rate_weight)
        / NULLIF(SUM(f.provider_acceptance_rate_weight), 0) * 100, 1) AS acceptance_rate,
    ROUND(SUM(f.provider_rating_per_order_value * f.provider_rating_per_order_weight)
        / NULLIF(SUM(f.provider_rating_per_order_weight), 0), 3) AS avg_rating
FROM hive_metastore.ng_delivery_spark.fact_provider_weekly f
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON f.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND f.metric_timestamp_local >= '{WEEKLY_START}'
  AND f.metric_timestamp_local <= '{WEEKLY_END}'
GROUP BY 1, 2, 3
ORDER BY 1, 3
LIMIT 5000
"""

CUSTOMER_REVIEWS_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', r.created_date), 'yyyy-MM-dd') AS period,
    p.provider_id,
    p.provider_name,
    p.brand_name,
    p.city_name,
    r.rating_value,
    r.comment,
    CAST(r.created AS STRING) AS created_at,
    f.order_reference_id
FROM hive_metastore.ng_delivery_spark.delivery_rating_provider_rating_history r
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON r.provider_id = p.provider_id
    LEFT JOIN hive_metastore.ng_delivery_spark.fact_order_delivery f ON r.order_id = f.order_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND r.created_date >= '{WEEKLY_START}'
  AND r.created_date <= '{WEEKLY_END}'
  AND r.comment IS NOT NULL
  AND LENGTH(TRIM(r.comment)) > 0
  AND COALESCE(r.ignore_rating, false) = false
ORDER BY r.created DESC
LIMIT 2000
"""

STORE_RATINGS_WEEKLY = f"""
SELECT
    DATE_FORMAT(DATE_TRUNC('week', r.created_date), 'yyyy-MM-dd') AS period,
    p.provider_id,
    AVG(r.rating_value) AS avg_review_rating,
    COUNT(*) AS reviews_count,
    SUM(CASE WHEN r.comment IS NOT NULL AND LENGTH(TRIM(r.comment)) > 0 THEN 1 ELSE 0 END) AS comments_count
FROM hive_metastore.ng_delivery_spark.delivery_rating_provider_rating_history r
    JOIN hive_metastore.ng_delivery_spark.dim_provider_v2 p ON r.provider_id = p.provider_id
WHERE p.country_code = 'ua'
  AND p.group_name = '{PARTNER_NAME}'
  AND r.created_date >= '{WEEKLY_START}'
  AND r.created_date <= '{WEEKLY_END}'
  AND COALESCE(r.ignore_rating, false) = false
GROUP BY 1, 2
ORDER BY 1, 2
LIMIT 5000
"""


def main():
    print(f"Partner: {PARTNER_NAME} ({PARTNER_BRANDS_LABEL})")
    print(f"Monthly window: {MONTHLY_START} — {MONTHLY_END}")
    print(f"Weekly window: {WEEKLY_START} — {WEEKLY_END}")
    print("Connecting to Databricks...")
    conn = get_connection()
    cursor = conn.cursor()

    print("Fetching financial data...")
    fin_m = to_serializable(run_query(cursor, FINANCIAL_MONTHLY))
    fin_w = to_serializable(run_query(cursor, FINANCIAL_WEEKLY))

    print("Fetching operational data...")
    ops_m = to_serializable(run_query(cursor, OPERATIONAL_MONTHLY))
    ops_w = to_serializable(run_query(cursor, OPERATIONAL_WEEKLY))

    print("Fetching replacement/adjustment rates...")
    repl_m = to_serializable(run_query(cursor, REPLACEMENT_ADJUSTMENT_MONTHLY))
    repl_w = to_serializable(run_query(cursor, REPLACEMENT_ADJUSTMENT_WEEKLY))

    print("Fetching failed orders...")
    fail_m = to_serializable(run_query(cursor, FAILED_ORDERS_MONTHLY))
    fail_w = to_serializable(run_query(cursor, FAILED_ORDERS_WEEKLY))

    print("Fetching failed order reasons...")
    fail_reasons_m = to_serializable(run_query(cursor, FAILED_REASONS_MONTHLY))
    fail_reasons_w = to_serializable(run_query(cursor, FAILED_REASONS_WEEKLY))

    print("Fetching failed orders list (weekly)...")
    failed_orders_list = to_serializable(run_query(cursor, FAILED_ORDERS_LIST_WEEKLY))

    print("Fetching campaign data...")
    camp_m = to_serializable(run_query(cursor, CAMPAIGNS_MONTHLY))
    camp_w = to_serializable(run_query(cursor, CAMPAIGNS_WEEKLY))

    print("Fetching acceptance/availability...")
    aa_current = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY_CURRENT))
    aa_m = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY_MONTHLY))
    aa_w = to_serializable(run_query(cursor, ACCEPTANCE_AVAILABILITY_WEEKLY))

    print("Fetching network stores (Bolt catalogue)...")
    network_stores = to_serializable(run_query(cursor, NETWORK_STORES))
    network_store_count = len(network_stores)
    network_brand_breakdown = to_serializable(run_query(cursor, NETWORK_BRAND_BREAKDOWN))

    print("Fetching store-level weekly orders...")
    store_weekly = to_serializable(run_query(cursor, STORE_WEEKLY))

    print("Fetching store-level weekly quality (rating/availability)...")
    store_quality = to_serializable(run_query(cursor, STORE_QUALITY_WEEKLY))

    print("Fetching store-level weekly review counts/avg...")
    store_ratings = to_serializable(run_query(cursor, STORE_RATINGS_WEEKLY))

    print("Fetching customer text reviews...")
    customer_reviews = to_serializable(run_query(cursor, CUSTOMER_REVIEWS_WEEKLY))

    quality_map = {(q["period"], q["provider_id"]): q for q in store_quality}
    ratings_map = {(r["period"], r["provider_id"]): r for r in store_ratings}

    for entry in store_weekly:
        key = (entry["period"], entry["provider_id"])
        q = quality_map.get(key, {})
        r = ratings_map.get(key, {})
        entry["availability_rate"] = q.get("availability_rate")
        entry["acceptance_rate"] = q.get("acceptance_rate")
        entry["avg_rating"] = q.get("avg_rating")
        entry["avg_review_rating"] = r.get("avg_review_rating")
        entry["reviews_count"] = r.get("reviews_count", 0)
        entry["comments_count"] = r.get("comments_count", 0)

    fin_m = _filter_completed_months(fin_m)
    ops_m = _filter_completed_months(ops_m)
    repl_m = _filter_completed_months(repl_m)
    fail_m = _filter_completed_months(fail_m)
    fail_reasons_m = _filter_completed_months(fail_reasons_m)
    camp_m = _filter_completed_months(camp_m)
    aa_m = _filter_completed_months(aa_m)

    for row in ops_m:
        row["network_stores"] = network_store_count
        row["active_stores"] = network_store_count
    for row in ops_w:
        row["network_stores"] = network_store_count
        row["active_stores"] = network_store_count

    cursor.close()
    conn.close()

    cities = sorted({s.get("city_name") for s in network_stores if s.get("city_name")})
    city_label = " · ".join(cities) if cities else ""

    report_data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "monthly_start": MONTHLY_START,
        "monthly_end": MONTHLY_END,
        "weekly_start": WEEKLY_START,
        "weekly_end": WEEKLY_END,
        "partner_name": PARTNER_NAME,
        "partner_display_name": PARTNER_DISPLAY_NAME,
        "partner_brands_label": PARTNER_BRANDS_LABEL,
        "city": city_label,
        "monthly": {
            "financial": fin_m,
            "operational": ops_m,
            "replacement_adjustment": repl_m,
            "failed_orders": fail_m,
            "failed_reasons": fail_reasons_m,
            "campaigns": camp_m,
            "acceptance_availability": aa_m,
        },
        "weekly": {
            "financial": fin_w,
            "operational": ops_w,
            "replacement_adjustment": repl_w,
            "failed_orders": fail_w,
            "failed_reasons": fail_reasons_w,
            "campaigns": camp_w,
            "acceptance_availability": aa_w,
        },
        "acceptance_current": aa_current,
        "network_stores": network_stores,
        "network_store_count": network_store_count,
        "network_brand_breakdown": network_brand_breakdown,
        "store_weekly": store_weekly,
        "customer_reviews": customer_reviews,
        "failed_orders_list": failed_orders_list,
    }

    DATA_PATH.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Data saved to {DATA_PATH}")

    print("Generating index.html...")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    js_data = f"const REPORT_DATA = {json.dumps(report_data, ensure_ascii=False, default=str)};"
    html = template.replace("/*__REPORT_DATA__*/", js_data)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Done! Report written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
