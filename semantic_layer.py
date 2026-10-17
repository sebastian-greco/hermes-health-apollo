from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import store


def ensure_canonical_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            attributes_json TEXT NOT NULL DEFAULT '{}',
            privacy_class TEXT NOT NULL DEFAULT 'private',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(entity_type, canonical_key)
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            day TEXT NOT NULL,
            title TEXT,
            status TEXT,
            attributes_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, source_table, source_row_id)
        );

        CREATE TABLE IF NOT EXISTS event_entities (
            event_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            role TEXT NOT NULL,
            participation_start_ts TEXT,
            participation_end_ts TEXT,
            attributes_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (event_id, entity_id, role),
            FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE,
            FOREIGN KEY(entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            signal_name TEXT NOT NULL,
            code TEXT,
            unit TEXT,
            value_number REAL,
            value_text TEXT,
            effective_start TEXT NOT NULL,
            effective_end TEXT,
            day TEXT NOT NULL,
            source_event_id TEXT,
            source_entity_id TEXT,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            quality TEXT NOT NULL DEFAULT 'reported',
            attributes_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS features (
            feature_id TEXT PRIMARY KEY,
            feature_key TEXT NOT NULL,
            grain TEXT NOT NULL,
            entity_scope TEXT NOT NULL DEFAULT 'global',
            entity_id TEXT,
            feature_ts TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            value_number REAL,
            value_text TEXT,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            feature_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_features_lookup
            ON features(feature_key, grain, entity_scope, COALESCE(entity_id, ''), feature_ts, feature_version);

        CREATE TABLE IF NOT EXISTS analysis_runs (
            run_id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            question TEXT,
            args_json TEXT NOT NULL,
            code_version TEXT NOT NULL,
            data_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            coverage_json TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_type_day ON events(event_type, day);
        CREATE INDEX IF NOT EXISTS idx_observations_signal_day ON observations(signal_name, day);
        """
    )


def refresh_canonical_facts(*, start: str, end: str) -> dict:
    store.initialize()
    with sqlite3.connect(store.database_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_canonical_schema(conn)
        conn.execute(
            """
            DELETE FROM event_entities
            WHERE event_id IN (SELECT event_id FROM events WHERE day BETWEEN ? AND ?)
            """,
            (start, end),
        )
        conn.execute("DELETE FROM observations WHERE day BETWEEN ? AND ?", (start, end))
        conn.execute("DELETE FROM events WHERE day BETWEEN ? AND ?", (start, end))
        _project_food_logs(conn, start, end)
        _project_care_entries(conn, start, end)
        _project_oura_workouts(conn, start, end)
        _project_sleep_sessions(conn, start, end)
        _project_daily_rollups(conn, start, end)
        conn.commit()
    return {"ok": True, "start": start, "end": end}


def _project_food_logs(conn: sqlite3.Connection, start: str, end: str) -> None:
    rows = conn.execute(
        """
        SELECT id, day, logged_at, description, items_json
        FROM food_logs
        WHERE day BETWEEN ? AND ?
        ORDER BY day, logged_at
        """,
        (start, end),
    ).fetchall()
    for row in rows:
        payload = _json_loads(row["items_json"])
        event_id = f"meal:{row['id']}"
        _upsert_event(
            conn,
            event_id,
            "meal",
            "manual_food",
            "food_logs",
            row["id"],
            row["logged_at"],
            None,
            row["day"],
            row["description"],
            payload,
        )
        for item in payload.get("items", []):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            entity_id = f"food:{_slug(name)}"
            _upsert_entity(conn, entity_id, "food", _slug(name), name, item)
            _upsert_event_entity(conn, event_id, entity_id, "food", item)


def _project_care_entries(conn: sqlite3.Connection, start: str, end: str) -> None:
    rows = conn.execute(
        "SELECT payload_json FROM care_entries WHERE "
        "substr(COALESCE(occurrence_start, recorded_at), 1, 10) BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    for row in rows:
        project_care_entry(conn, json.loads(row[0]))


def project_care_entry(conn: sqlite3.Connection, entry: dict) -> None:
    """Project the current immutable-revision head using Apollo's canonical model.

    Unknown occurrence uses recorded time only for the required canonical index;
    explicit attributes and quality prevent it becoming a known occurrence.
    """
    from . import sync_control

    source_id = sync_control.ensure_source(
        conn, source_slug="care_journal", provider="manual", connection_name="Care journal",
        status="manual", sync_mode="manual",
    )
    raw_id = sync_control.persist_raw_record(
        conn, source_id=source_id, sync_batch_id=None, provider="care_journal",
        object_type="care_entry_revision", external_id=f"{entry['entry_id']}:{entry['revision']}",
        payload=entry, source_updated_at=entry["revised_at"], privacy_tier="sensitive",
    )
    sync_control.attach_lineage(
        conn, canonical_table="care_entry_revisions",
        canonical_id=f"{entry['entry_id']}:{entry['revision']}", raw_record_id=raw_id,
    )
    event_id = f"care:{entry['entry_id']}"
    conn.execute("DELETE FROM observations WHERE source_event_id=?", (event_id,))
    conn.execute("DELETE FROM event_entities WHERE event_id=?", (event_id,))
    timestamp = entry["occurrence_start"] or entry["recorded_at"]
    attributes = {**entry, "occurrence_unknown": entry["occurrence_start"] is None,
                  "canonical_time_basis": "occurrence" if entry["occurrence_start"] else "recorded"}
    _upsert_event(conn, event_id, "care_log", "care_journal", "care_entries",
                  entry["entry_id"], timestamp, entry["occurrence_end"], timestamp[:10],
                  entry["narrative"] or entry["category_id"], attributes)
    definition_row = conn.execute(
        "SELECT definition_json FROM care_categories WHERE category_id=? AND version=?",
        (entry["category_id"], entry["category_version"]),
    ).fetchone()
    definition = json.loads(definition_row[0])
    for field, config in definition["observations"].items():
        value = entry["data"].get(field)
        if type(value) not in {int, float}:
            continue
        _upsert_observation(conn, f"{event_id}:{field}", "care_journal",
                            f"care.{entry['category_id']}.{field}", field,
                            entry["data"][config["unit_field"]], value, None,
                            timestamp, entry["occurrence_end"], timestamp[:10],
                            event_id, None, "care_entries", entry["entry_id"],
                            "reported" if entry["uncertainty"] == "exact" else "uncertain",
                            {"revision": entry["revision"], "source_ref": entry["source_ref"],
                             "uncertainty": entry["uncertainty"],
                             "canonical_time_basis": attributes["canonical_time_basis"]})
    for case_id in entry["links"]:
        row = conn.execute("SELECT payload_json FROM care_cases WHERE case_id=?", (case_id,)).fetchone()
        case = json.loads(row[0])
        entity_id = f"care-case:{case_id}"
        _upsert_entity(conn, entity_id, f"care_{case['kind']}", case_id, case["title"], case)
        _upsert_event_entity(conn, event_id, entity_id, case["kind"], {})


def _project_oura_workouts(conn: sqlite3.Connection, start: str, end: str) -> None:
    rows = conn.execute(
        """
        SELECT id, day, activity, calories, distance, intensity, label, source, start_datetime, end_datetime
        FROM oura_workouts
        WHERE day BETWEEN ? AND ?
        ORDER BY day, start_datetime
        """,
        (start, end),
    ).fetchall()
    for row in rows:
        event_id = f"workout:{row['id']}"
        _upsert_event(
            conn,
            event_id,
            "workout",
            "oura",
            "oura_workouts",
            row["id"],
            row["start_datetime"] or f"{row['day']}T00:00:00",
            row["end_datetime"],
            row["day"],
            row["activity"],
            _row_attrs(row, {"id", "day", "start_datetime", "end_datetime"}),
        )


def _project_sleep_sessions(conn: sqlite3.Connection, start: str, end: str) -> None:
    rows = conn.execute(
        """
        SELECT id, day, type, bedtime_start, bedtime_end,
               total_sleep_duration_seconds, deep_sleep_duration_seconds
        FROM oura_sleep_sessions
        WHERE day BETWEEN ? AND ?
        ORDER BY day, bedtime_start
        """,
        (start, end),
    ).fetchall()
    projected_days = set()
    for row in rows:
        projected_days.add(row["day"])
        _upsert_event(
            conn,
            f"sleep-session:{row['id']}",
            "sleep_session",
            "oura",
            "oura_sleep_sessions",
            row["id"],
            row["bedtime_start"] or f"{row['day']}T00:00:00",
            row["bedtime_end"],
            row["day"],
            row["type"],
            _row_attrs(row, {"id", "day", "bedtime_start", "bedtime_end"}),
        )
    daily_rows = conn.execute(
        """
        SELECT day, primary_bedtime_start, primary_bedtime_end,
               total_sleep_duration_seconds, deep_sleep_duration_seconds
        FROM oura_daily
        WHERE day BETWEEN ? AND ?
        ORDER BY day
        """,
        (start, end),
    ).fetchall()
    for row in daily_rows:
        if row["day"] in projected_days or not row["primary_bedtime_start"]:
            continue
        _upsert_event(
            conn,
            f"sleep-session:{row['day']}",
            "sleep_session",
            "oura",
            "oura_daily",
            row["day"],
            row["primary_bedtime_start"],
            row["primary_bedtime_end"],
            row["day"],
            "primary_sleep",
            _row_attrs(row, {"day", "primary_bedtime_start", "primary_bedtime_end"}),
        )


def _project_daily_rollups(conn: sqlite3.Connection, start: str, end: str) -> None:
    rows = conn.execute(
        """
        SELECT od.day, od.sleep_score, od.readiness_score, od.stress_high_seconds,
               od.recovery_high_seconds, od.total_sleep_duration_seconds,
               od.deep_sleep_duration_seconds, od.primary_bedtime_start,
               cd.meeting_count, cd.meeting_minutes, ed.received_count
        FROM oura_daily od
        LEFT JOIN calendar_daily cd ON cd.day = od.day
        LEFT JOIN email_daily ed ON ed.day = od.day
        WHERE od.day BETWEEN ? AND ?
        ORDER BY od.day
        """,
        (start, end),
    ).fetchall()
    for row in rows:
        day = row["day"]
        _upsert_event(
            conn,
            f"calendar-day:{day}",
            "calendar_day",
            "google_workspace",
            "calendar_daily",
            day,
            f"{day}T00:00:00",
            f"{day}T23:59:59",
            day,
            None,
            {"meeting_count": row["meeting_count"], "meeting_minutes": row["meeting_minutes"]},
        )
        _upsert_event(
            conn,
            f"email-day:{day}",
            "email_day",
            "google_workspace",
            "email_daily",
            day,
            f"{day}T00:00:00",
            f"{day}T23:59:59",
            day,
            None,
            {"received_count": row["received_count"]},
        )
        for signal, unit, value in (
            ("sleep_score", "score", row["sleep_score"]),
            ("readiness_score", "score", row["readiness_score"]),
            ("stress_high_seconds", "s", row["stress_high_seconds"]),
            ("recovery_high_seconds", "s", row["recovery_high_seconds"]),
            ("sleep_duration_seconds", "s", row["total_sleep_duration_seconds"]),
            ("deep_sleep_seconds", "s", row["deep_sleep_duration_seconds"]),
        ):
            _upsert_observation(
                conn,
                f"{signal}:{day}",
                "oura",
                signal,
                unit,
                unit,
                value,
                None,
                f"{day}T00:00:00",
                f"{day}T23:59:59",
                day,
                None,
                None,
                "oura_daily",
                day,
                "reported",
                {},
            )


def _upsert_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    entity_type: str,
    canonical_key: str,
    display_name: str,
    attributes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO entities(entity_id, entity_type, canonical_key, display_name, attributes_json, privacy_class)
        VALUES (?, ?, ?, ?, ?, 'private')
        ON CONFLICT(entity_type, canonical_key) DO UPDATE SET
            display_name = excluded.display_name,
            attributes_json = excluded.attributes_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (entity_id, entity_type, canonical_key, display_name, json.dumps(attributes, sort_keys=True)),
    )


def _upsert_event(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    provider: str,
    source_table: str,
    source_row_id: str,
    start_ts: str,
    end_ts: str | None,
    day: str,
    title: str | None,
    attributes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO events(
            event_id, event_type, provider, source_table, source_row_id,
            start_ts, end_ts, day, title, attributes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, source_table, source_row_id) DO UPDATE SET
            event_type = excluded.event_type,
            start_ts = excluded.start_ts,
            end_ts = excluded.end_ts,
            day = excluded.day,
            title = excluded.title,
            attributes_json = excluded.attributes_json
        """,
        (
            event_id,
            event_type,
            provider,
            source_table,
            source_row_id,
            start_ts,
            end_ts,
            day,
            title,
            json.dumps(attributes, sort_keys=True),
        ),
    )


def _upsert_event_entity(
    conn: sqlite3.Connection,
    event_id: str,
    entity_id: str,
    role: str,
    attributes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO event_entities(event_id, entity_id, role, attributes_json)
        VALUES (?, ?, ?, ?)
        """,
        (event_id, entity_id, role, json.dumps(attributes, sort_keys=True)),
    )


def _upsert_observation(
    conn: sqlite3.Connection,
    observation_id: str,
    provider: str,
    signal_name: str,
    code: str | None,
    unit: str | None,
    value_number: float | int | None,
    value_text: str | None,
    effective_start: str,
    effective_end: str | None,
    day: str,
    source_event_id: str | None,
    source_entity_id: str | None,
    source_table: str,
    source_row_id: str,
    quality: str,
    attributes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO observations(
            observation_id, provider, signal_name, code, unit, value_number,
            value_text, effective_start, effective_end, day, source_event_id,
            source_entity_id, source_table, source_row_id, quality, attributes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            provider,
            signal_name,
            code,
            unit,
            value_number,
            value_text,
            effective_start,
            effective_end,
            day,
            source_event_id,
            source_entity_id,
            source_table,
            source_row_id,
            quality,
            json.dumps(attributes, sort_keys=True),
        ),
    )


def iter_days(start: str, end: str) -> list[str]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    days = []
    current = start_day
    while current <= end_day:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _row_attrs(row: sqlite3.Row, excluded: set[str]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key not in excluded}


def _slug(value: str) -> str:
    return "-".join(value.lower().strip().split())
