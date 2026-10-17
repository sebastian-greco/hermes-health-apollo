"""Trusted care sessions extending Apollo's existing SQLite and canonical model.

Authority objects are host capabilities, never decoded from tool/model arguments.
The host chooses a database per authorized patient scope. Public methods never
accept an actor or patient override. Categories are bounded data, never code.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from . import store


class CareError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class ConflictError(CareError):
    pass


@dataclass(frozen=True)
class Authority:
    actor_id: str
    scope_id: str
    permissions: frozenset[str]

    def __post_init__(self):
        _identifier(self.actor_id, "actor_id")
        _identifier(self.scope_id, "scope_id")
        if not isinstance(self.permissions, frozenset) or not self.permissions <= {"read", "write", "admin"}:
            raise AuthorizationError("Invalid trusted permissions")


def ensure_care_schema(conn: sqlite3.Connection) -> None:
    """Additive Apollo migration: no legacy data/table is replaced."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS care_scope (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS care_categories (
            category_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
            definition_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(category_id, version)
        );
        CREATE TABLE IF NOT EXISTS care_cases (
            case_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('issue','episode','visit')),
            revision INTEGER NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS care_case_revisions (
            case_id TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(case_id, revision), FOREIGN KEY(case_id) REFERENCES care_cases(case_id)
        );
        CREATE TABLE IF NOT EXISTS care_entries (
            entry_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            category_id TEXT NOT NULL, category_version INTEGER NOT NULL,
            occurrence_start TEXT, occurrence_end TEXT, recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(category_id, category_version) REFERENCES care_categories(category_id, version)
        );
        CREATE TABLE IF NOT EXISTS care_entry_revisions (
            entry_id TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(entry_id, revision), FOREIGN KEY(entry_id) REFERENCES care_entries(entry_id)
        );
        CREATE TABLE IF NOT EXISTS care_requests (
            actor_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            result_json TEXT NOT NULL, PRIMARY KEY(actor_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS care_food_links (
            food_id TEXT NOT NULL, case_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            source_ref TEXT NOT NULL, linked_at TEXT NOT NULL,
            PRIMARY KEY(food_id, case_id), FOREIGN KEY(food_id) REFERENCES food_logs(id),
            FOREIGN KEY(case_id) REFERENCES care_cases(case_id)
        );
        CREATE INDEX IF NOT EXISTS idx_care_entries_time ON care_entries(occurrence_start);
    """)


class CareStore:
    def __init__(self, db_path: Path | str, *, scope_id: str, adopt_existing: bool = False):
        self.db_path = Path(db_path).resolve()
        self.scope_id = _identifier(scope_id, "scope_id")
        existed = self.db_path.exists()
        store.initialize(self.db_path)
        with contextlib.closing(store.connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT scope_id FROM care_scope WHERE singleton=1").fetchone()
            if row and row[0] != self.scope_id:
                raise AuthorizationError("Database belongs to another care scope")
            if not row:
                if existed and not adopt_existing and _has_patient_data(conn):
                    raise AuthorizationError("Existing Apollo data requires trusted explicit scope adoption")
                conn.execute("INSERT INTO care_scope(singleton, scope_id) VALUES (1, ?)", (self.scope_id,))

    def session(self, authority: Authority) -> CareSession:
        if not isinstance(authority, Authority) or authority.scope_id != self.scope_id:
            raise AuthorizationError("Authority does not grant this care scope")
        return CareSession(self.db_path, authority)


class CareSession:
    """A host-owned capability. Do not expose its constructor or connection to models."""
    def __init__(self, db_path: Path, authority: Authority):
        self.db_path = Path(db_path).resolve()
        self.authority = authority

    def require(self, permission: str) -> None:
        if permission not in {"read", "write", "admin"} or permission not in self.authority.permissions:
            raise AuthorizationError(f"Care {permission} permission required")

    @contextlib.contextmanager
    def connection(self, *, permission: str = "read"):
        self.require(permission)
        conn = store.connect(self.db_path)
        try:
            if permission != "read":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT scope_id FROM care_scope WHERE singleton=1").fetchone()
            if not row or row[0] != self.authority.scope_id:
                raise AuthorizationError("Database scope binding changed")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def apollo(self, *, permission: str = "read"):
        """Trusted host adapter for legacy food/query tools; never a model tool."""
        with self.connection(permission=permission):
            pass
        with store.bind_database(self.db_path):
            yield

    def define_category(self, category_id: str, *, version: int = 1,
                        schema: dict | None = None, label: str | None = None,
                        observations: dict | None = None, expected_daily: bool = False) -> dict:
        self.require("admin")
        category_id = _identifier(category_id, "category_id")
        if type(version) is not int or version < 1 or version > 10000:
            raise CareError("Category version must be a positive integer")
        if type(expected_daily) is not bool:
            raise CareError("expected_daily must be boolean")
        schema = {} if schema is None else schema
        _check_schema(schema)
        observations = {} if observations is None else observations
        _bounded_json(observations)
        if not isinstance(observations, dict) or len(observations) > 64:
            raise CareError("observations must be a bounded field mapping")
        for field, config in observations.items():
            _identifier(field, "observation field")
            if not isinstance(config, dict) or set(config) - {"unit_field", "allowed_units", "label"}:
                raise CareError("Invalid observation configuration")
            _identifier(config.get("unit_field"), "unit_field")
            units = config.get("allowed_units")
            if not isinstance(units, list) or not units or any(not isinstance(u, str) or not u or len(u) > 64 for u in units):
                raise CareError("Numeric observations require explicit allowed units")
        definition = {"category_id": category_id, "version": version,
                      "label": _text(label or category_id, 160), "schema": schema,
                      "observations": observations, "expected_daily": bool(expected_daily)}
        with self.connection(permission="admin") as conn:
            row = conn.execute("SELECT definition_json FROM care_categories WHERE category_id=? AND version=?", (category_id, version)).fetchone()
            if row:
                if json.loads(row[0]) != definition:
                    raise ConflictError("Category versions are immutable; create a new version")
                return definition
            conn.execute("INSERT INTO care_categories VALUES (?, ?, ?, ?)", (category_id, version, _dump(definition), _now()))
        return definition

    def list_categories(self) -> list[dict]:
        with self.connection() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT definition_json FROM care_categories ORDER BY category_id, version")]

    def append_entry(self, *, category_id: str, request_id: str, source_ref: str,
                     data: dict | None = None, narrative: str = "", category_version: int = 1,
                     occurrence_start: str | None = None, occurrence_end: str | None = None,
                     uncertainty: str = "unknown", links: list[str] | None = None) -> dict:
        payload = dict(category_id=category_id, category_version=category_version,
                       data={} if data is None else data, narrative=narrative,
                       occurrence_start=occurrence_start, occurrence_end=occurrence_end,
                       uncertainty=uncertainty, links=[] if links is None else links,
                       source_ref=source_ref)
        operation = {"append_entry": payload}
        with self.connection(permission="write") as conn:
            replay = self._replay(conn, request_id, operation)
            if replay is not None:
                return replay
            payload = self._validate_entry(conn, payload)
            now = _now()
            result = {**payload, "entry_id": str(uuid4()), "revision": 1,
                      "actor_id": self.authority.actor_id, "recorded_at": now,
                      "revised_at": now, "request_id": request_id}
            conn.execute("INSERT INTO care_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (result["entry_id"], 1, category_id, category_version,
                          result["occurrence_start"], result["occurrence_end"], now, _dump(result)))
            conn.execute("INSERT INTO care_entry_revisions VALUES (?, 1, ?)", (result["entry_id"], _dump(result)))
            self._project(conn, result)
            self._remember(conn, request_id, operation, result)
            return result

    def revise_entry(self, entry_id: str, *, expected_revision: int, request_id: str,
                     source_ref: str, changes: dict, reason: str) -> dict:
        allowed = {"data", "narrative", "occurrence_start", "occurrence_end", "uncertainty", "links", "category_version"}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise CareError("Correction contains unsupported fields")
        operation = {"revise_entry": entry_id, "expected_revision": expected_revision,
                     "source_ref": source_ref, "changes": changes, "reason": reason}
        with self.connection(permission="write") as conn:
            replay = self._replay(conn, request_id, operation)
            if replay is not None:
                return replay
            old = self._entry(conn, entry_id)
            if type(expected_revision) is not int or old["revision"] != expected_revision:
                raise ConflictError("Entry revision changed")
            payload = {k: old[k] for k in ("category_id", "category_version", "data", "narrative", "occurrence_start", "occurrence_end", "uncertainty", "links", "source_ref")}
            payload.update(changes)
            payload["source_ref"] = source_ref
            payload = self._validate_entry(conn, payload)
            result = {**old, **payload, "revision": old["revision"] + 1,
                      "actor_id": self.authority.actor_id, "request_id": request_id,
                      "revised_at": _now(), "correction_reason": _text(reason, 2000, required=True)}
            conn.execute("UPDATE care_entries SET revision=?, category_version=?, occurrence_start=?, occurrence_end=?, payload_json=? WHERE entry_id=?", (result["revision"], result["category_version"], result["occurrence_start"], result["occurrence_end"], _dump(result), entry_id))
            conn.execute("INSERT INTO care_entry_revisions VALUES (?, ?, ?)", (entry_id, result["revision"], _dump(result)))
            self._project(conn, result)
            self._remember(conn, request_id, operation, result)
            return result

    def get_entry(self, entry_id: str, *, include_revisions: bool = False) -> dict:
        with self.connection() as conn:
            result = self._entry(conn, entry_id)
            if include_revisions:
                result["revisions"] = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM care_entry_revisions WHERE entry_id=? ORDER BY revision", (entry_id,))]
            return result

    def list_entries(self, *, category_id: str | None = None, case_id: str | None = None,
                     start: str | None = None, end: str | None = None,
                     include_closed: bool = True, limit: int = 500,
                     latest: bool = False) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise CareError("limit must be between 1 and 5000")
        if type(latest) is not bool:
            raise CareError("latest must be boolean")
        start, end = _range(start, end)
        with self.connection() as conn:
            if case_id:
                self._case(conn, case_id)
            order = "DESC" if latest else "ASC"
            rows = conn.execute(f"SELECT payload_json FROM care_entries ORDER BY COALESCE(occurrence_start, recorded_at) {order}, entry_id {order}")
            cases = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT case_id, payload_json FROM care_cases")}
            result = []
            for row in rows:
                item = json.loads(row[0])
                if category_id and item["category_id"] != category_id:
                    continue
                if case_id and not _linked_to(item["links"], case_id, cases):
                    continue
                if not include_closed and item["links"] and all(cases[c]["status"] == "closed" for c in item["links"]):
                    continue
                if (start or end) and not _overlaps(item, start, end):
                    continue
                result.append(item)
                if len(result) == limit:
                    break
            return result

    def create_case(self, *, kind: str, title: str, request_id: str, source_ref: str,
                    parent_id: str | None = None, occurred_at: str | None = None,
                    narrative: str = "") -> dict:
        operation = dict(create_case=kind, title=title, source_ref=source_ref,
                         parent_id=parent_id, occurred_at=occurred_at, narrative=narrative)
        with self.connection(permission="write") as conn:
            replay = self._replay(conn, request_id, operation)
            if replay is not None:
                return replay
            if kind not in {"issue", "episode", "visit"}:
                raise CareError("Case kind must be issue, episode or visit")
            if parent_id:
                parent = self._case(conn, parent_id)
                if parent["kind"] != "issue" or kind != "episode":
                    raise CareError("Only an episode may have an issue parent")
            occurred_at = _instant(occurred_at)
            if kind == "visit" and not occurred_at:
                raise CareError("A visit requires a known occurred_at")
            now = _now()
            result = dict(case_id=str(uuid4()), kind=kind, title=_text(title, 300, required=True),
                          narrative=_text(narrative, 8000), parent_id=parent_id,
                          occurred_at=occurred_at, status="open", revision=1,
                          source_ref=_text(source_ref, 1000, required=True), actor_id=self.authority.actor_id,
                          recorded_at=now, revised_at=now, request_id=request_id)
            conn.execute("INSERT INTO care_cases VALUES (?, ?, 1, ?)", (result["case_id"], kind, _dump(result)))
            conn.execute("INSERT INTO care_case_revisions VALUES (?, 1, ?)", (result["case_id"], _dump(result)))
            self._remember(conn, request_id, operation, result)
            return result

    def update_case(self, case_id: str, *, expected_revision: int, request_id: str,
                    source_ref: str, status: str | None = None,
                    title: str | None = None, narrative: str | None = None,
                    occurred_at: str | None = None, reason: str) -> dict:
        operation = dict(update_case=case_id, expected_revision=expected_revision, source_ref=source_ref,
                         status=status, title=title, narrative=narrative, occurred_at=occurred_at, reason=reason)
        with self.connection(permission="write") as conn:
            replay = self._replay(conn, request_id, operation)
            if replay is not None:
                return replay
            old = self._case(conn, case_id)
            if type(expected_revision) is not int or old["revision"] != expected_revision:
                raise ConflictError("Case revision changed")
            if status is not None and status not in {"open", "closed"}:
                raise CareError("Case status must be open or closed")
            result = {**old, "revision": old["revision"] + 1, "revised_at": _now(),
                      "actor_id": self.authority.actor_id, "source_ref": _text(source_ref, 1000, required=True),
                      "request_id": request_id, "change_reason": _text(reason, 2000, required=True)}
            if status is not None:
                result["status"] = status
            if title is not None:
                result["title"] = _text(title, 300, required=True)
            if narrative is not None:
                result["narrative"] = _text(narrative, 8000)
            if occurred_at is not None:
                result["occurred_at"] = _instant(occurred_at)
            conn.execute("UPDATE care_cases SET revision=?, payload_json=? WHERE case_id=?", (result["revision"], _dump(result), case_id))
            conn.execute("INSERT INTO care_case_revisions VALUES (?, ?, ?)", (case_id, result["revision"], _dump(result)))
            self._remember(conn, request_id, operation, result)
            return result

    def get_case(self, case_id: str, *, include_revisions: bool = False) -> dict:
        with self.connection() as conn:
            result = self._case(conn, case_id)
            if include_revisions:
                result["revisions"] = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM care_case_revisions WHERE case_id=? ORDER BY revision", (case_id,))]
            return result

    def list_cases(self, *, kind: str | None = None, include_closed: bool = True) -> list[dict]:
        with self.connection() as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM care_cases ORDER BY rowid")]
            return [r for r in rows if (kind is None or r["kind"] == kind) and (include_closed or r["status"] != "closed")]

    def link_food(self, food_id: str, *, case_id: str, source_ref: str) -> dict:
        with self.connection(permission="write") as conn:
            self._case(conn, case_id)
            if not conn.execute("SELECT 1 FROM food_logs WHERE id=?", (food_id,)).fetchone():
                raise CareError("Food record not found in this scope")
            conn.execute("INSERT OR IGNORE INTO care_food_links VALUES (?, ?, ?, ?, ?)", (food_id, case_id, self.authority.actor_id, _text(source_ref, 1000, required=True), _now()))
            return {"food_id": food_id, "case_id": case_id}

    def report(self, *, start: str | None = None, end: str | None = None,
               episode_id: str | None = None, since_visit_id: str | None = None,
               issue_id: str | None = None, max_entries: int = 1000) -> dict:
        """Counts cover all matching heads; bounded evidence reports every omission."""
        if type(max_entries) is not int or not 1 <= max_entries <= 1000:
            raise CareError("max_entries must be between 1 and 1000")
        selectors = int(start is not None) + int(episode_id is not None) + int(since_visit_id is not None)
        if selectors != 1:
            raise CareError("Choose a date range, episode, or since-visit report")
        end = end or _now()
        case_filter = episode_id or issue_id
        with self.connection() as conn:
            conn.execute("BEGIN")  # One consistent head/revision snapshot for this report.
            cases = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT case_id, payload_json FROM care_cases")}
            if episode_id and self._case(conn, episode_id)["kind"] != "episode":
                raise CareError("episode_id must refer to an episode")
            if issue_id and self._case(conn, issue_id)["kind"] != "issue":
                raise CareError("issue_id must refer to an issue")
            if since_visit_id:
                visit = self._case(conn, since_visit_id)
                if visit["kind"] != "visit":
                    raise CareError("since_visit_id must refer to a visit")
                if issue_id:
                    relevant = any(_linked_to(json.loads(r[0])["links"], issue_id, cases) and since_visit_id in json.loads(r[0])["links"] for r in conn.execute("SELECT payload_json FROM care_entries"))
                    if not relevant:
                        raise CareError("Visit has no recorded evidence linking it to this issue")
                start = visit["occurred_at"]
            if episode_id:
                start = cases[episode_id].get("occurred_at")
                if not start:
                    for row in conn.execute("SELECT payload_json FROM care_entries WHERE occurrence_start IS NOT NULL ORDER BY occurrence_start, entry_id"):
                        item = json.loads(row[0])
                        if _linked_to(item["links"], episode_id, cases):
                            start = item["occurrence_start"]
                            break
            start, end = _range(start, end)
            if start and end and (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days > 3660:
                raise CareError("Report interval exceeds ten years")
            definitions = {(r[0], r[1]): json.loads(r[2]) for r in conn.execute("SELECT category_id, version, definition_json FROM care_categories")}
            selected, unknown = [], []
            entry_count = unknown_count = uncertain_count = 0
            counts: dict[str, int] = {}
            aggregates: dict[tuple, dict] = {}
            exact_days: set[str] = set()
            category_days: dict[str, set] = {}
            for row in conn.execute("SELECT payload_json FROM care_entries ORDER BY COALESCE(occurrence_start, recorded_at), entry_id"):
                item = json.loads(row[0])
                if case_filter and not _linked_to(item["links"], case_filter, cases):
                    continue
                if item["occurrence_start"] is None:
                    unknown_count += 1
                    if len(unknown) < max_entries:
                        unknown.append(item)
                    continue
                if not _overlaps(item, start, end):
                    continue
                entry_count += 1
                if item["uncertainty"] != "exact":
                    uncertain_count += 1
                if len(selected) < max_entries:
                    selected.append(item)
                category = item["category_id"]
                counts[category] = counts.get(category, 0) + 1
                if item["uncertainty"] == "exact":
                    day = item["occurrence_start"][:10]
                    exact_days.add(day)
                    category_days.setdefault(category, set()).add(day)
                definition = definitions[(category, item["category_version"])]
                for field, config in definition["observations"].items():
                    value = item["data"].get(field)
                    if type(value) not in {int, float}:
                        continue
                    unit = item["data"][config["unit_field"]]
                    key = (category, item["category_version"], field, unit)
                    aggregate = aggregates.setdefault(key, {"category_id": category,
                        "category_version": item["category_version"], "field": field, "unit": unit,
                        "count": 0, "minimum": value, "maximum": value, "mean": 0.0})
                    aggregate["count"] += 1
                    aggregate["minimum"] = min(aggregate["minimum"], value)
                    aggregate["maximum"] = max(aggregate["maximum"], value)
                    # Weighted means avoid a growing values list and sum overflow.
                    n = aggregate["count"]
                    aggregate["mean"] = aggregate["mean"] * ((n-1)/n) + value/n
            revision_evidence, omitted_revisions = {}, {}
            for entry in selected + unknown:
                revisions = [json.loads(row[0]) for row in conn.execute(
                    "SELECT payload_json FROM care_entry_revisions WHERE entry_id=? ORDER BY revision DESC LIMIT 25",
                    (entry["entry_id"],),
                )]
                omitted_revisions[entry["entry_id"]] = max(0, entry["revision"]-len(revisions))
                revision_evidence[entry["entry_id"]] = [
                    {k: revision.get(k) for k in ("revision", "source_ref", "actor_id", "revised_at", "correction_reason")}
                    for revision in reversed(revisions)
                ]
            food_rows, food_count = [], 0
            for row in conn.execute("SELECT * FROM food_logs ORDER BY day, logged_at"):
                links = [x[0] for x in conn.execute("SELECT case_id FROM care_food_links WHERE food_id=?", (row["id"],))]
                if case_filter and not _linked_to(links, case_filter, cases):
                    continue
                if start and row["day"] < start[:10] or end and row["day"] > end[:10]:
                    continue
                food_count += 1
                if len(food_rows) < max_entries:
                    food_rows.append({"food_id": row["id"], "day": row["day"], "description": row["description"], "links": links, "source_ref": f"apollo:food_logs:{row['id']}"})
        days = _days(start, end) if start and end else []
        gap_days = [day for day in days if day not in exact_days]
        category_gaps = {}
        for definition in definitions.values():
            if definition["expected_daily"]:
                observed = category_days.get(definition["category_id"], set())
                category_gaps[definition["category_id"]] = [day for day in days if day not in observed]
        omitted = {"entries": entry_count-len(selected), "unknown_occurrence_entries": unknown_count-len(unknown),
                   "food_entries": food_count-len(food_rows)}
        return {"scope_id": self.authority.scope_id, "generated_at": _now(), "start": start, "end": end,
                "episode_id": episode_id, "since_visit_id": since_visit_id, "issue_id": issue_id,
                "entry_count": entry_count, "unknown_occurrence_count": unknown_count, "food_entry_count": food_count,
                "counts_by_category": counts, "measurements": list(aggregates.values()), "entries": selected,
                "revision_evidence": revision_evidence,
                "unknown_occurrence_entries": unknown, "food_entries": food_rows,
                "evidence": {"limit_per_kind": max_entries,
                             "revision_limit_per_entry": 25, "omitted_revisions": omitted_revisions,
                             "truncated": any(omitted.values()) or any(omitted_revisions.values()),
                             "omitted": omitted, "counts_and_aggregates_cover_all_matches": True,
                             "order": "oldest_first"},
                "gaps": {"days_without_exact_time_entries": gap_days, "expected_daily_category_gaps": category_gaps,
                         "interval_unknown": start is None,
                         "meaning": "Nessuna registrazione non significa assenza di sintomi, guarigione o dose non assunta."},
                "uncertainty": {"interval_or_approximate_entries": [r["entry_id"] for r in selected if r["uncertainty"] != "exact"],
                                "unknown_time_count": unknown_count, "interval_or_approximate_count": uncertain_count,
                                "entry_ids_limited_to_returned_evidence": True},
                "cases": [cases[c] for c in dict.fromkeys(c for r in selected + unknown for c in r["links"])],
                "clinical_inference": False}

    def _entry(self, conn, entry_id):
        row = conn.execute("SELECT payload_json FROM care_entries WHERE entry_id=?", (entry_id,)).fetchone()
        if not row:
            raise CareError("Entry not found in this scope")
        return json.loads(row[0])

    def _case(self, conn, case_id):
        row = conn.execute("SELECT payload_json FROM care_cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise CareError("Case not found in this scope")
        return json.loads(row[0])

    def _validate_entry(self, conn, payload):
        _identifier(payload["category_id"], "category_id")
        if type(payload["category_version"]) is not int:
            raise CareError("Category version must be an integer")
        row = conn.execute("SELECT definition_json FROM care_categories WHERE category_id=? AND version=?", (payload["category_id"], payload["category_version"])).fetchone()
        if not row:
            raise CareError("Category/version is not configured")
        definition = json.loads(row[0])
        data = payload["data"]
        if not isinstance(data, dict):
            raise CareError("Entry data must be an object")
        _bounded_json(data)
        _validate(data, definition["schema"])
        for field, config in definition["observations"].items():
            if field in data:
                if type(data[field]) not in {int, float}:
                    raise CareError("Measurement value must be a number, not a boolean/string")
                if data.get(config["unit_field"]) not in config["allowed_units"]:
                    raise CareError("Measurement requires an explicit compatible reported unit")
        result = dict(payload)
        result["narrative"] = _text(payload["narrative"], 8000)
        if not data and not result["narrative"].strip():
            raise CareError("Entry needs narrative or structured data")
        result["source_ref"] = _text(payload["source_ref"], 1000, required=True)
        start = _instant(payload["occurrence_start"])
        end = _instant(payload["occurrence_end"])
        uncertainty = payload["uncertainty"]
        if uncertainty not in {"exact", "approximate", "range", "unknown"}:
            raise CareError("Invalid occurrence uncertainty")
        if not start and (end or uncertainty != "unknown") or start and uncertainty == "unknown":
            raise CareError("Occurrence time and uncertainty disagree")
        if end and (not start or end < start) or uncertainty == "range" and not end:
            raise CareError("Occurrence range needs ordered start/end")
        if uncertainty == "exact" and end and end != start:
            raise CareError("Exact occurrence cannot be a time range")
        links = payload["links"]
        if not isinstance(links, list) or len(links) > 32 or any(not isinstance(c, str) for c in links):
            raise CareError("links must be a bounded case ID list")
        for case_id in links:
            self._case(conn, case_id)
        result.update(occurrence_start=start, occurrence_end=end, links=sorted(set(links)))
        return result

    def _replay(self, conn, request_id, operation):
        _text(request_id, 300, required=True)
        fingerprint = _fingerprint(operation)
        row = conn.execute("SELECT fingerprint, result_json FROM care_requests WHERE actor_id=? AND request_id=?", (self.authority.actor_id, request_id)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ConflictError("Request identity reused with different content")
            return json.loads(row[1])
        return None

    def _remember(self, conn, request_id, operation, result):
        conn.execute("INSERT INTO care_requests VALUES (?, ?, ?, ?)", (self.authority.actor_id, request_id, _fingerprint(operation), _dump(result)))

    def _project(self, conn, result):
        from .semantic_layer import project_care_entry
        project_care_entry(conn, result)


def _has_patient_data(conn):
    # Initialization creates source/schedule defaults, which are not patient data.
    infrastructure = {"schema_version", "health_sources", "sync_schedules", "sync_state", "care_scope"}
    if conn.execute("SELECT 1 FROM health_sources WHERE account_external_id IS NOT NULL OR status NOT IN ('disconnected', 'manual') OR source_slug NOT IN ('oura', 'google_calendar', 'gmail', 'manual_food', 'google_health') LIMIT 1").fetchone():
        return True
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if row[0] in infrastructure:
            continue
        quoted = row[0].replace('"', '""')
        if conn.execute(f'SELECT 1 FROM "{quoted}" LIMIT 1').fetchone():
            return True
    return False


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", value):
        raise CareError(f"Invalid {name}")
    return value


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        raise CareError("Invalid or oversized text")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise CareError("Time must be an ISO-8601 instant with timezone")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CareError("Time must be an ISO-8601 instant with timezone") from exc
    if dt.tzinfo is None:
        raise CareError("Time needs an explicit timezone")
    return dt.astimezone(timezone.utc).isoformat()


def _range(start, end):
    def normalize(v, *, end=False):
        if v is not None and isinstance(v, str) and len(v) == 10:
            try:
                day = date.fromisoformat(v)
            except ValueError as exc:
                raise CareError("Invalid report day") from exc
            return day.isoformat() + ("T23:59:59.999999+00:00" if end else "T00:00:00+00:00")
        return _instant(v)
    start, end = normalize(start), normalize(end, end=True)
    if start and end and end < start:
        raise CareError("Range end precedes start")
    return start, end


def _overlaps(item, start, end):
    occurred = item["occurrence_start"]
    return occurred is not None and (not start or (item["occurrence_end"] or occurred) >= start) and (not end or occurred <= end)


def _linked_to(links, case_id, cases):
    return case_id in links or any(cases[c].get("parent_id") == case_id for c in links)


def _days(start, end):
    current, last = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    days = []
    while current <= last:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _dump(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise CareError("Value must be finite JSON data") from exc


def _fingerprint(value):
    _bounded_json(value, max_bytes=100000)
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _bounded_json(value, *, max_bytes=65536):
    nodes = 0
    def walk(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 4096:
            raise CareError("JSON structure exceeds limits")
        if item is None or type(item) is bool:
            return
        if type(item) is int and abs(item) <= 10 ** 300:
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, str) and len(item) <= 8000:
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth+1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 160:
                    raise CareError("JSON keys must be bounded strings")
                walk(child, depth+1)
            return
        raise CareError("Value must be bounded finite JSON data")
    walk(value)
    if len(_dump(value).encode()) > max_bytes:
        raise CareError("JSON payload exceeds size limit")


_SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
                "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "title", "description"}


def _check_schema(schema):
    """Bounded declarative JSON Schema subset; unsupported keywords fail closed."""
    _bounded_json(schema, max_bytes=32768)
    def check(node):
        if not isinstance(node, dict) or set(node) - _SCHEMA_KEYS:
            raise CareError("Unsupported category schema keyword")
        if "type" in node and (not isinstance(node["type"], str) or node["type"] not in {"object", "array", "string", "integer", "number", "boolean", "null"}):
            raise CareError("Unsupported category schema type")
        props = node.get("properties", {})
        if not isinstance(props, dict):
            raise CareError("Schema properties must be an object")
        for name, child in props.items():
            _text(name, 160, required=True)
            check(child)
        required = node.get("required", [])
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            raise CareError("Schema required must be a list of field names")
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            raise CareError("additionalProperties must be boolean")
        if "items" in node:
            check(node["items"])
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise CareError("Schema enum must be a nonempty list")
        for key in ("minimum", "maximum"):
            if key in node and (type(node[key]) not in {int, float} or not math.isfinite(node[key])):
                raise CareError("Schema bounds must be finite numbers")
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                raise CareError("Schema length/item bounds must be nonnegative integers")
        for low, high in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
            if low in node and high in node and node[low] > node[high]:
                raise CareError("Schema bounds are reversed")
    check(schema)


def _validate(value, schema, path="data"):
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "integer": type(value) is int, "number": type(value) in {int, float}, "boolean": type(value) is bool,
             "null": value is None}
    if kind and not valid[kind]:
        raise CareError(f"{path} has wrong type")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise CareError(f"{path} is outside allowed values")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise CareError(f"{path} differs from required constant")
    if isinstance(value, dict):
        if set(schema.get("required", [])) - set(value):
            raise CareError(f"{path} lacks required fields")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise CareError(f"{path} has unconfigured fields")
        for k, child in value.items():
            if k in props:
                _validate(child, props[k], f"{path}.{k}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 4096):
            raise CareError(f"{path} has invalid item count")
        for child in value:
            _validate(child, schema.get("items", {}), path + "[]")
    if isinstance(value, str) and (len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 8000)):
        raise CareError(f"{path} has invalid length")
    if type(value) in {int, float} and (value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf)):
        raise CareError(f"{path} is outside bounds")
