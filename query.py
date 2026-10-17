from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Iterable

from . import store


def sleep_consistency_minutes(values: Iterable[str]) -> float | None:
    minutes = [_minutes_since_noon(value) for value in values if value]
    if len(minutes) < 2:
        return None
    mean = sum(minutes) / len(minutes)
    variance = sum((value - mean) ** 2 for value in minutes) / len(minutes)
    return math.sqrt(variance)


def health_query(args: dict) -> dict:
    query_type = args.get("query_type", "recent")
    if query_type == "date_range":
        return _date_range(args["start"], args["end"])
    if query_type == "recent":
        limit = int(args.get("limit", 7))
        return _recent(limit)
    if query_type == "stress_days":
        limit = int(args.get("limit", 10))
        return _stress_days(limit)
    if query_type == "correlate":
        return _correlate(args)
    if query_type == "heart_rate":
        return _heart_rate(args)
    if query_type == "workouts":
        return _workouts(args)
    if query_type == "sessions":
        return _sessions(args)
    if query_type == "tags":
        return _tags(args)
    if query_type == "coverage":
        return _coverage()
    raise ValueError(f"Unsupported health query_type: {query_type}")


def _date_range(start: str, end: str) -> dict:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                d.*,
                CASE
                    WHEN food.food_entry_count IS NULL THEN 0
                    WHEN food.unestimated_entry_count > 0 THEN NULL
                    ELSE food.total_estimated_calories
                END AS food_total_estimated_calories,
                CASE strftime('%w', d.day)
                    WHEN '0' THEN 'Sunday'
                    WHEN '1' THEN 'Monday'
                    WHEN '2' THEN 'Tuesday'
                    WHEN '3' THEN 'Wednesday'
                    WHEN '4' THEN 'Thursday'
                    WHEN '5' THEN 'Friday'
                    WHEN '6' THEN 'Saturday'
                END AS day_of_week
            FROM daily_overview d
            LEFT JOIN (
                SELECT
                    day,
                    COUNT(*) AS food_entry_count,
                    SUM(CASE
                        WHEN items_json IS NULL THEN 1
                        WHEN json_type(items_json, '$.total_estimated_calories')
                             NOT IN ('integer', 'real') THEN 1
                        ELSE 0
                    END) AS unestimated_entry_count,
                    SUM(
                        CAST(
                            json_extract(items_json, '$.total_estimated_calories')
                            AS INTEGER
                        )
                    ) AS total_estimated_calories
                FROM food_logs
                GROUP BY day
            ) food ON food.day = d.day
            WHERE d.day BETWEEN ? AND ?
            ORDER BY d.day
            """,
            (start, end),
        ).fetchall()
    return _daily_payload([_row_dict(row) for row in rows])


def _recent(limit: int) -> dict:
    with store.connect() as conn:
        bounds = conn.execute(
            "SELECT day FROM oura_daily ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not bounds:
        return {"days": []}
    days = [row[0] for row in bounds]
    return _date_range(min(days), max(days))


def _stress_days(limit: int) -> dict:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                d.*,
                CASE
                    WHEN food.food_entry_count IS NULL THEN 0
                    WHEN food.unestimated_entry_count > 0 THEN NULL
                    ELSE food.total_estimated_calories
                END AS food_total_estimated_calories,
                CASE strftime('%w', d.day)
                    WHEN '0' THEN 'Sunday'
                    WHEN '1' THEN 'Monday'
                    WHEN '2' THEN 'Tuesday'
                    WHEN '3' THEN 'Wednesday'
                    WHEN '4' THEN 'Thursday'
                    WHEN '5' THEN 'Friday'
                    WHEN '6' THEN 'Saturday'
                END AS day_of_week
            FROM daily_overview d
            LEFT JOIN (
                SELECT
                    day,
                    COUNT(*) AS food_entry_count,
                    SUM(CASE
                        WHEN items_json IS NULL THEN 1
                        WHEN json_type(items_json, '$.total_estimated_calories')
                             NOT IN ('integer', 'real') THEN 1
                        ELSE 0
                    END) AS unestimated_entry_count,
                    SUM(
                        CAST(
                            json_extract(items_json, '$.total_estimated_calories')
                            AS INTEGER
                        )
                    ) AS total_estimated_calories
                FROM food_logs
                GROUP BY day
            ) food ON food.day = d.day
            ORDER BY d.stress_high_seconds DESC, d.day DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return _daily_payload([_row_dict(row) for row in rows])


def _correlate(args: dict) -> dict:
    left = args["left"]
    right = args["right"]
    shift_days = int(args.get("shift_days", args.get("shift", 0)))
    start, end = _query_bounds(args)
    days = _date_range(start, end)["days"]
    rows_by_day = {row["day"]: row for row in days}
    pairs = []

    for left_row in days:
        right_day = (
            date.fromisoformat(left_row["day"]) + timedelta(days=shift_days)
        ).isoformat()
        right_row = rows_by_day.get(right_day)
        if right_row is None:
            continue
        left_value = _metric_value(left_row, left)
        right_value = _metric_value(right_row, right)
        if left_value is None or right_value is None:
            continue
        pairs.append(
            {
                "left_day": left_row["day"],
                "right_day": right_row["day"],
                "left_value": left_value,
                "right_value": right_value,
            }
        )

    return {
        "left": left,
        "right": right,
        "shift_days": shift_days,
        "pairs": pairs,
        "correlation": _pearson(
            [pair["left_value"] for pair in pairs],
            [pair["right_value"] for pair in pairs],
        ),
    }


def _heart_rate(args: dict) -> dict:
    limit = int(args.get("limit", 200))
    source = args.get("source")
    filters: list[str] = []
    params: list[object] = []
    if args.get("start") and args.get("end"):
        filters.append("substr(timestamp, 1, 10) BETWEEN ? AND ?")
        params.extend([args["start"], args["end"]])
    if source:
        filters.append("source = ?")
        params.append(str(source))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT timestamp, timestamp_unix, bpm, source, producer_timestamp
            FROM oura_heart_rate
            {where}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    samples = [_row_dict(row) for row in reversed(rows)]
    bpms = [int(sample["bpm"]) for sample in samples if sample.get("bpm") is not None]
    by_source: dict[str, int] = {}
    for sample in samples:
        sample_source = str(sample.get("source") or "unknown")
        by_source[sample_source] = by_source.get(sample_source, 0) + 1
    return {
        "samples": samples,
        "sample_count": len(samples),
        "min_bpm": min(bpms) if bpms else None,
        "max_bpm": max(bpms) if bpms else None,
        "avg_bpm": (sum(bpms) / len(bpms)) if bpms else None,
        "by_source": by_source,
    }


def _workouts(args: dict) -> dict:
    return {
        "workouts": _document_rows(
            table="oura_workouts",
            date_column="day",
            columns=(
                "id",
                "day",
                "activity",
                "calories",
                "distance",
                "intensity",
                "label",
                "source",
                "start_datetime",
                "end_datetime",
            ),
            args=args,
        )
    }


def _sessions(args: dict) -> dict:
    return {
        "sessions": _document_rows(
            table="oura_sessions",
            date_column="day",
            columns=(
                "id",
                "day",
                "type",
                "start_datetime",
                "end_datetime",
                "mood",
                "heart_rate_json",
                "heart_rate_variability_json",
                "motion_count_json",
            ),
            args=args,
        )
    }


def _tags(args: dict) -> dict:
    return {
        "tags": _document_rows(
            table="oura_tags",
            date_column="day",
            columns=("id", "day", "text", "timestamp", "tags_json"),
            args=args,
        ),
        "enhanced_tags": _document_rows(
            table="oura_enhanced_tags",
            date_column="start_day",
            columns=(
                "id",
                "start_day",
                "end_day",
                "start_time",
                "end_time",
                "tag_type_code",
                "comment",
                "custom_name",
            ),
            args=args,
        ),
    }


def _coverage() -> dict:
    tables = {
        "daily": ("oura_daily", "day"),
        "sleep_sessions": ("oura_sleep_sessions", "day"),
        "heart_rate": ("oura_heart_rate", "substr(timestamp, 1, 10)"),
        "ring_battery": ("oura_ring_battery", "substr(timestamp, 1, 10)"),
        "workouts": ("oura_workouts", "day"),
        "sessions": ("oura_sessions", "day"),
        "tags": ("oura_tags", "day"),
        "enhanced_tags": ("oura_enhanced_tags", "start_day"),
        "daily_resilience": ("oura_daily_resilience", "day"),
        "daily_cardiovascular_age": ("oura_daily_cardiovascular_age", "day"),
        "vo2_max": ("oura_vo2_max", "day"),
        "sleep_time": ("oura_sleep_time", "day"),
        "rest_mode_periods": ("oura_rest_mode_periods", "start_day"),
        "ring_configuration": ("oura_ring_configuration", None),
        "personal_info": ("oura_personal_info", None),
    }
    coverage = {}
    with store.connect() as conn:
        for name, (table, date_expr) in tables.items():
            if date_expr:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS row_count,
                           MIN({date_expr}) AS first_day,
                           MAX({date_expr}) AS last_day
                    FROM {table}
                    """
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) AS row_count FROM {table}"
                ).fetchone()
            coverage[name] = _row_dict(row)
    return {"coverage": coverage}


def _document_rows(
    *,
    table: str,
    date_column: str,
    columns: tuple[str, ...],
    args: dict,
) -> list[dict]:
    limit = int(args.get("limit", 50))
    filters: list[str] = []
    params: list[object] = []
    if args.get("start") and args.get("end"):
        filters.append(f"{date_column} BETWEEN ? AND ?")
        params.extend([args["start"], args["end"]])
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM {table}
            {where}
            ORDER BY {date_column} DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [_decode_json_fields(_row_dict(row)) for row in rows]


def _query_bounds(args: dict) -> tuple[str, str]:
    if "start" in args and "end" in args:
        return args["start"], args["end"]
    limit = int(args.get("limit", 30))
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT day FROM oura_daily ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        today = date.today().isoformat()
        return today, today
    days = [row[0] for row in rows]
    return min(days), max(days)


def _metric_value(row: dict, metric: str) -> int | float | str | None:
    aliases = {
        "email_count": "received_count",
        "emails": "received_count",
        "food_calories": "food_total_estimated_calories",
        "calories": "food_total_estimated_calories",
        "resting_hr_bpm": "resting_heart_rate",
        "deep_sleep_seconds": "deep_sleep_duration_seconds",
        "bedtime": "primary_bedtime_start",
        "bedtime_start": "primary_bedtime_start",
        "bedtime_minutes_since_noon": "primary_bedtime_start",
    }
    key = aliases.get(metric, metric)
    if key not in row:
        raise ValueError(f"Unsupported correlation metric: {metric}")
    value = row[key]
    if value is None:
        return None
    if key == "primary_bedtime_start" and (
        metric in {"bedtime", "bedtime_start", "bedtime_minutes_since_noon"}
        or isinstance(value, str)
    ):
        return _minutes_since_noon(str(value))
    if not isinstance(value, (int, float)):
        raise ValueError(f"Correlation metric must be numeric: {metric}")
    return value


def _pearson(
    left_values: list[int | float],
    right_values: list[int | float],
) -> float | None:
    if len(left_values) < 2 or len(right_values) < 2:
        return None
    left_mean = sum(left_values) / len(left_values)
    right_mean = sum(right_values) / len(right_values)
    numerator = sum(
        (left - left_mean) * (right - right_mean)
        for left, right in zip(left_values, right_values, strict=True)
    )
    left_variance = sum((left - left_mean) ** 2 for left in left_values)
    right_variance = sum((right - right_mean) ** 2 for right in right_values)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0:
        return None
    return numerator / denominator


def _daily_payload(days: list[dict]) -> dict:
    return {
        "days": days,
        "sleep_consistency_minutes": sleep_consistency_minutes(
            day["primary_bedtime_start"]
            for day in days
            if day.get("primary_bedtime_start")
        ),
    }


def _minutes_since_noon(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    minutes = parsed.hour * 60 + parsed.minute + parsed.second / 60
    shifted = (minutes - 12 * 60) % (24 * 60)
    return shifted


def _row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _decode_json_fields(row: dict) -> dict:
    for key, value in list(row.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                row[key[:-5]] = json.loads(value)
            except json.JSONDecodeError:
                row[key[:-5]] = value
            row.pop(key)
    return row
