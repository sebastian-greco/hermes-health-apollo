"""Scoped medication plans and durable reminders for Casita.

This module deliberately does not register Hermes tools or create a scheduler.
Callers must supply an authorized Apollo CareSession and a transport bound to a
configured recipient. Clinical plan confirmation is a trusted-ingress operation;
it is not represented by a model-controlled boolean.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import re
import sqlite3
from typing import Any, Callable, Iterator, Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SCHEMA_VERSION = 1
_CONFIRMATION_SEAL = object()


class ReminderError(ValueError):
    """A reminder or medication operation is invalid or not authorized."""


class ReminderConflict(ReminderError):
    """The requested version or occurrence is no longer current."""


@dataclass(frozen=True)
class VerifiedPlanConfirmation:
    """Opaque receipt minted by trusted ingress after explicit human confirmation.

    Do not expose the private factory to the model action catalog. The receipt is
    bound to one authenticated actor, scope, plan and version and cannot be reused
    for a revised plan.
    """

    scope_id: str
    actor_id: str
    plan_id: str
    version: int
    source_ref: str
    confirmed_at: str
    _seal: object


def _mint_verified_confirmation(
    *,
    scope_id: str,
    actor_id: str,
    plan_id: str,
    version: int,
    source_ref: str,
    confirmed_at: datetime,
) -> VerifiedPlanConfirmation:
    """Trusted ingress helper; call only after validating the original user event."""
    if not all(_valid_id(item) for item in (scope_id, actor_id, plan_id)):
        raise ReminderError("confirmation identity is invalid")
    if not isinstance(version, int) or version < 1:
        raise ReminderError("confirmation version must be positive")
    if not isinstance(source_ref, str) or not source_ref or len(source_ref) > 512:
        raise ReminderError("confirmation source reference is required")
    return VerifiedPlanConfirmation(
        scope_id=scope_id,
        actor_id=actor_id,
        plan_id=plan_id,
        version=version,
        source_ref=source_ref,
        confirmed_at=_aware_iso(confirmed_at),
        _seal=_CONFIRMATION_SEAL,
    )


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "not_sent", "unknown"]
    provider_message_id: str | None = None
    detail: str | None = None


class ReminderEgress(Protocol):
    """Injected central egress. Implementations must use the configured target."""

    def send(self, payload: dict[str, Any], *, client_request_id: str) -> DeliveryResult:
        """Send once, returning unknown when acceptance cannot be determined."""

    def lookup(self, *, client_request_id: str) -> DeliveryResult:
        """Reconcile an uncertain request; unknown must never imply safe retry."""


@dataclass(frozen=True)
class ReminderSchedule:
    """Validated recurrence. Medication times are local wall-clock instructions."""

    kind: Literal["once", "daily"]
    timezone: str
    at: str | None = None
    times: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None

    @classmethod
    def once(cls, at: datetime) -> "ReminderSchedule":
        return cls(kind="once", timezone="UTC", at=_aware_iso(at))

    @classmethod
    def daily(
        cls,
        *,
        times: list[str] | tuple[str, ...],
        timezone: str,
        start_date: date | str,
        end_date: date | str | None = None,
    ) -> "ReminderSchedule":
        return cls(
            kind="daily",
            timezone=timezone,
            times=tuple(times),
            start_date=_date_iso(start_date),
            end_date=_date_iso(end_date) if end_date is not None else None,
        )

    def validate(self) -> dict[str, Any]:
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ReminderError("schedule timezone must be a valid IANA zone")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ReminderError("schedule timezone must be a valid IANA zone") from exc
        if self.kind == "once":
            _parse_aware(self.at)
            if self.times or self.start_date or self.end_date:
                raise ReminderError("one-time schedule has unexpected recurrence fields")
        elif self.kind == "daily":
            if self.at is not None or not isinstance(self.times, (tuple, list)) or not self.times:
                raise ReminderError("daily schedule requires one or more local times")
            if any(not isinstance(value, str) for value in self.times):
                raise ReminderError("daily times must use 24-hour HH:MM format")
            if len(set(self.times)) != len(self.times):
                raise ReminderError("daily schedule times must be unique")
            if any(not _TIME_RE.fullmatch(value) for value in self.times):
                raise ReminderError("daily times must use 24-hour HH:MM format")
            start = date.fromisoformat(self.start_date or "")
            end = date.fromisoformat(self.end_date) if self.end_date else None
            if end is not None and end < start:
                raise ReminderError("schedule end date precedes start date")
        else:
            raise ReminderError("unsupported schedule kind")
        return {
            "kind": self.kind,
            "timezone": self.timezone,
            "at": self.at,
            "times": list(self.times),
            "start_date": self.start_date,
            "end_date": self.end_date,
        }


def ensure_reminder_schema(conn: sqlite3.Connection) -> None:
    """Install additive reminder tables in the already-authorized Apollo DB."""
    ddl = """
        CREATE TABLE IF NOT EXISTS care_reminder_schema (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS care_medication_plans (
            scope_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            current_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed', 'active', 'cancelled')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope_id, plan_id)
        );
        CREATE TABLE IF NOT EXISTS care_medication_plan_versions (
            scope_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            medication TEXT NOT NULL,
            instructions TEXT NOT NULL,
            schedule_json TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed', 'active', 'superseded', 'cancelled')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            confirmed_by TEXT,
            confirmed_at TEXT,
            confirmation_source_ref TEXT,
            PRIMARY KEY(scope_id, plan_id, version),
            FOREIGN KEY(scope_id, plan_id) REFERENCES care_medication_plans(scope_id, plan_id)
        );
        CREATE TABLE IF NOT EXISTS care_reminder_schedules (
            scope_id TEXT NOT NULL,
            schedule_id TEXT NOT NULL,
            reminder_kind TEXT NOT NULL CHECK(reminder_kind IN ('medication', 'manual')),
            plan_id TEXT,
            plan_version INTEGER,
            schedule_json TEXT NOT NULL,
            message TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            generation INTEGER NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT,
            last_generated_date TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cancelled_at TEXT,
            cancel_reason TEXT,
            PRIMARY KEY(scope_id, schedule_id),
            FOREIGN KEY(scope_id, plan_id, plan_version)
                REFERENCES care_medication_plan_versions(scope_id, plan_id, version)
        );
        CREATE TABLE IF NOT EXISTS care_reminder_occurrences (
            scope_id TEXT NOT NULL,
            occurrence_id TEXT NOT NULL,
            schedule_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            plan_id TEXT,
            plan_version INTEGER,
            local_key TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'ready', 'leased', 'sent', 'acknowledged', 'cancelled',
                'expired', 'failed', 'unknown'
            )),
            client_request_id TEXT NOT NULL UNIQUE,
            lease_token TEXT,
            lease_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            acknowledged_by TEXT,
            acknowledged_at TEXT,
            provider_message_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope_id, occurrence_id),
            UNIQUE(scope_id, schedule_id, generation, local_key),
            FOREIGN KEY(scope_id, schedule_id) REFERENCES care_reminder_schedules(scope_id, schedule_id)
        );
        CREATE TABLE IF NOT EXISTS care_reminder_attempts (
            scope_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            occurrence_id TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'started', 'sent', 'not_sent', 'unknown',
                'sent_after_cancel', 'not_sent_after_cancel', 'unknown_after_cancel',
                'sent_stale', 'not_sent_stale', 'unknown_stale'
            )),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            provider_message_id TEXT,
            detail TEXT,
            PRIMARY KEY(scope_id, attempt_id),
            FOREIGN KEY(scope_id, occurrence_id)
                REFERENCES care_reminder_occurrences(scope_id, occurrence_id)
        );
        CREATE TABLE IF NOT EXISTS care_medication_administrations (
            scope_id TEXT NOT NULL,
            administration_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            reported_at TEXT NOT NULL,
            reported_by TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            report TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            PRIMARY KEY(scope_id, administration_id),
            UNIQUE(scope_id, reported_by, idempotency_key),
            FOREIGN KEY(scope_id, plan_id, plan_version)
                REFERENCES care_medication_plan_versions(scope_id, plan_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_care_reminder_due
            ON care_reminder_occurrences(scope_id, status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_care_reminder_plan
            ON care_reminder_schedules(scope_id, plan_id, enabled);
        CREATE INDEX IF NOT EXISTS idx_care_medication_admin_time
            ON care_medication_administrations(scope_id, plan_id, occurred_at);
        CREATE TABLE IF NOT EXISTS care_manual_reminder_requests (
            scope_id TEXT NOT NULL,
            schedule_id TEXT NOT NULL,
            request_ref TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            PRIMARY KEY(scope_id, schedule_id),
            UNIQUE(scope_id, requested_by, request_ref),
            FOREIGN KEY(scope_id, schedule_id)
                REFERENCES care_reminder_schedules(scope_id, schedule_id)
        );
        """
    # sqlite3.executescript() implicitly commits an open transaction. Execute each
    # additive DDL statement individually so CareSession retains one atomic write.
    for statement in ddl.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO care_reminder_schema(singleton, version) VALUES (1, ?)",
        (_SCHEMA_VERSION,),
    )
    version = conn.execute(
        "SELECT version FROM care_reminder_schema WHERE singleton = 1"
    ).fetchone()[0]
    if version > _SCHEMA_VERSION:
        raise ReminderError("database reminder schema is newer than this code")


class CareReminderService:
    """Operations over one trusted scope; no patient/actor IDs come from the model."""

    def __init__(
        self,
        session: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        authorize_current: Callable[[Any], bool] | None = None,
        max_late_seconds: int = 1800,
    ) -> None:
        self.session = session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.authorize_current = authorize_current
        if max_late_seconds < 0 or max_late_seconds > 86_400:
            raise ReminderError("late delivery window must be between 0 and 24 hours")
        self.max_late_seconds = max_late_seconds
        authority = getattr(session, "authority", None)
        self.scope_id = getattr(authority, "scope_id", None)
        self.actor_id = getattr(authority, "actor_id", None)
        if not _valid_id(self.scope_id) or not _valid_id(self.actor_id):
            raise ReminderError("trusted care authority is required")

    @contextmanager
    def _connection(self, permission: str) -> Iterator[sqlite3.Connection]:
        self.session.require(permission)
        with self.session.connection(permission=permission) as conn:
            ensure_reminder_schema(conn)
            yield conn

    def propose_plan(
        self,
        *,
        medication: str,
        instructions: str,
        schedule: ReminderSchedule,
        source_ref: str,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """Save an unverified proposal. This never schedules or sends reminders."""
        self.session.require("admin")
        medication = _required_text(medication, "medication", 200)
        instructions = _required_text(instructions, "instructions", 2000)
        source_ref = _required_text(source_ref, "source reference", 512)
        schedule_json = json.dumps(schedule.validate(), sort_keys=True, separators=(",", ":"))
        now = _aware_iso(self.clock())
        identifier = plan_id or str(uuid4())
        if not _valid_id(identifier):
            raise ReminderError("plan id is invalid")
        with self._connection("admin") as conn:
            conn.execute(
                """INSERT INTO care_medication_plans
                   (scope_id, plan_id, current_version, status, created_by, created_at, updated_at)
                   VALUES (?, ?, 1, 'proposed', ?, ?, ?)""",
                (self.scope_id, identifier, self.actor_id, now, now),
            )
            conn.execute(
                """INSERT INTO care_medication_plan_versions
                   (scope_id, plan_id, version, medication, instructions, schedule_json,
                    source_ref, status, created_by, created_at)
                   VALUES (?, ?, 1, ?, ?, ?, ?, 'proposed', ?, ?)""",
                (self.scope_id, identifier, medication, instructions, schedule_json,
                 source_ref, self.actor_id, now),
            )
        return {"plan_id": identifier, "version": 1, "status": "proposed"}

    def revise_plan(
        self,
        *,
        plan_id: str,
        expected_version: int,
        medication: str,
        instructions: str,
        schedule: ReminderSchedule,
        source_ref: str,
    ) -> dict[str, Any]:
        """Append a proposal version and immediately invalidate the previous one."""
        self.session.require("admin")
        _require_id(plan_id, "plan id")
        medication = _required_text(medication, "medication", 200)
        instructions = _required_text(instructions, "instructions", 2000)
        source_ref = _required_text(source_ref, "source reference", 512)
        schedule_json = json.dumps(schedule.validate(), sort_keys=True, separators=(",", ":"))
        now = _aware_iso(self.clock())
        with self._connection("admin") as conn:
            row = conn.execute(
                "SELECT current_version FROM care_medication_plans WHERE scope_id=? AND plan_id=?",
                (self.scope_id, plan_id),
            ).fetchone()
            if row is None:
                raise ReminderError("medication plan not found in this scope")
            if row[0] != expected_version:
                raise ReminderConflict("plan version changed; reload before editing")
            version = expected_version + 1
            conn.execute(
                "UPDATE care_medication_plan_versions SET status='superseded' "
                "WHERE scope_id=? AND plan_id=? AND version=? AND status='active'",
                (self.scope_id, plan_id, expected_version),
            )
            conn.execute(
                "UPDATE care_medication_plan_versions SET status='superseded' "
                "WHERE scope_id=? AND plan_id=? AND version=? AND status='proposed'",
                (self.scope_id, plan_id, expected_version),
            )
            conn.execute(
                "UPDATE care_medication_plans SET current_version=?, status='proposed', updated_at=? "
                "WHERE scope_id=? AND plan_id=?",
                (version, now, self.scope_id, plan_id),
            )
            self._cancel_plan_schedules(conn, plan_id, "plan revised", now)
            conn.execute(
                """INSERT INTO care_medication_plan_versions
                   (scope_id, plan_id, version, medication, instructions, schedule_json,
                    source_ref, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)""",
                (self.scope_id, plan_id, version, medication, instructions, schedule_json,
                 source_ref, self.actor_id, now),
            )
        return {"plan_id": plan_id, "version": version, "status": "proposed"}

    def activate_plan(self, confirmation: VerifiedPlanConfirmation) -> dict[str, Any]:
        """Activate only an exact, trusted-ingress confirmation receipt.

        Do not expose this method as a model tool. An operator/ingress component
        must bind the original human event to the receipt factory.
        """
        self.session.require("admin")
        if not isinstance(confirmation, VerifiedPlanConfirmation) or confirmation._seal is not _CONFIRMATION_SEAL:
            raise ReminderError("trusted confirmation receipt required")
        if (confirmation.scope_id != self.scope_id or confirmation.actor_id != self.actor_id):
            raise ReminderError("confirmation actor or scope does not match this session")
        now_dt = self._now()
        now = _aware_iso(now_dt)
        with self._connection("admin") as conn:
            row = conn.execute(
                """SELECT p.current_version, p.status, v.schedule_json, v.medication, v.instructions,
                          v.status, v.confirmed_by, v.confirmed_at, v.confirmation_source_ref
                   FROM care_medication_plans p JOIN care_medication_plan_versions v
                   ON v.scope_id=p.scope_id AND v.plan_id=p.plan_id AND v.version=p.current_version
                   WHERE p.scope_id=? AND p.plan_id=?""",
                (self.scope_id, confirmation.plan_id),
            ).fetchone()
            if row is None or row[0] != confirmation.version:
                raise ReminderConflict("confirmed plan version is no longer the current proposal")
            if confirmation.source_ref == "" or confirmation.confirmed_at > now:
                raise ReminderError("confirmation evidence is invalid")
            if row[1] == "active" and row[5] == "active":
                # The ingress may redeliver its exact command after a crash before
                # it records success. Replaying the same persisted receipt is safe;
                # a different receipt, revised plan, or cancelled plan is not.
                if (row[6], row[7], row[8]) != (
                    self.actor_id, confirmation.confirmed_at, confirmation.source_ref
                ):
                    raise ReminderConflict("active plan was confirmed by a different receipt")
                schedule_row = conn.execute(
                    "SELECT schedule_id FROM care_reminder_schedules "
                    "WHERE scope_id=? AND plan_id=? AND plan_version=? "
                    "ORDER BY created_at LIMIT 1",
                    (self.scope_id, confirmation.plan_id, confirmation.version),
                ).fetchone()
                if schedule_row is None:
                    raise ReminderConflict("active plan has no persisted reminder schedule")
                return {"plan_id": confirmation.plan_id, "version": confirmation.version,
                        "status": "active", "schedule_id": schedule_row[0],
                        "occurrences_created": 0}
            if row[1] != "proposed" or row[5] != "proposed":
                raise ReminderConflict("confirmed plan version is no longer the current proposal")
            schedule_obj = json.loads(row[2])
            schedule = _schedule_from_json(schedule_obj)
            conn.execute(
                "UPDATE care_medication_plans SET status='active', updated_at=? "
                "WHERE scope_id=? AND plan_id=? AND current_version=?",
                (now, self.scope_id, confirmation.plan_id, confirmation.version),
            )
            conn.execute(
                """UPDATE care_medication_plan_versions SET status='active', confirmed_by=?,
                   confirmed_at=?, confirmation_source_ref=? WHERE scope_id=? AND plan_id=?
                   AND version=? AND status='proposed'""",
                (self.actor_id, confirmation.confirmed_at, confirmation.source_ref,
                 self.scope_id, confirmation.plan_id, confirmation.version),
            )
            schedule_id = str(uuid4())
            start_at, end_at = _schedule_bounds(schedule)
            message = _medication_message(row[3], row[4], schedule)
            conn.execute(
                """INSERT INTO care_reminder_schedules
                   (scope_id, schedule_id, reminder_kind, plan_id, plan_version, schedule_json,
                    message, enabled, generation, start_at, end_at, created_by, created_at)
                   VALUES (?, ?, 'medication', ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)""",
                (self.scope_id, schedule_id, confirmation.plan_id, confirmation.version,
                 json.dumps(schedule_obj, sort_keys=True), message, start_at, end_at,
                 self.actor_id, now),
            )
            count = self._materialize_schedule(
                conn, schedule_id, now_dt + timedelta(days=8), now,
                not_before=now_dt - timedelta(seconds=self.max_late_seconds),
            )
        return {"plan_id": confirmation.plan_id, "version": confirmation.version,
                "status": "active", "schedule_id": schedule_id, "occurrences_created": count}

    def create_manual_reminder(
        self,
        *,
        message: str,
        at: datetime,
        request_ref: str,
    ) -> dict[str, Any]:
        """Create a one-off non-medical reminder after an explicit user request."""
        self.session.require("write")
        message = _required_text(message, "reminder message", 1000)
        request_ref = _required_text(request_ref, "request reference", 512)
        at_iso = _aware_iso(at)
        now = _aware_iso(self.clock())
        schedule = ReminderSchedule.once(_parse_aware(at_iso))
        schedule_json = json.dumps(schedule.validate(), sort_keys=True)
        schedule_id = str(uuid4())
        fingerprint = _request_fingerprint({"message": message, "schedule": json.loads(schedule_json)})
        with self._connection("write") as conn:
            previous = conn.execute(
                "SELECT schedule_id, request_fingerprint FROM care_manual_reminder_requests "
                "WHERE scope_id=? AND requested_by=? AND request_ref=?",
                (self.scope_id, self.actor_id, request_ref),
            ).fetchone()
            if previous:
                if previous[1] != fingerprint:
                    raise ReminderConflict("request reference was already used for a different reminder")
                return {"schedule_id": previous[0], "status": "scheduled", "at": at_iso}
            conn.execute(
                """INSERT INTO care_reminder_schedules
                   (scope_id, schedule_id, reminder_kind, plan_id, plan_version, schedule_json,
                    message, enabled, generation, start_at, created_by, created_at)
                   VALUES (?, ?, 'manual', NULL, NULL, ?, ?, 1, 1, ?, ?, ?)""",
                (self.scope_id, schedule_id, schedule_json, message, at_iso, self.actor_id, now),
            )
            self._materialize_schedule(
                conn, schedule_id, _parse_aware(at_iso), now,
                not_before=_parse_aware(now) - timedelta(seconds=self.max_late_seconds),
            )
            # Provenance is retained separately from the user-facing reminder text.
            conn.execute(
                "INSERT INTO care_manual_reminder_requests VALUES (?, ?, ?, ?, ?, ?)",
                (self.scope_id, schedule_id, request_ref, fingerprint, self.actor_id, now),
            )
        return {"schedule_id": schedule_id, "status": "scheduled", "at": at_iso}

    def cancel_plan(self, *, plan_id: str, reason: str) -> dict[str, Any]:
        """Cancel a plan and all pending occurrences; does not erase administration history."""
        self.session.require("write")
        _require_id(plan_id, "plan id")
        reason = _required_text(reason, "cancellation reason", 500)
        now = _aware_iso(self.clock())
        with self._connection("write") as conn:
            row = conn.execute(
                "SELECT current_version FROM care_medication_plans WHERE scope_id=? AND plan_id=?",
                (self.scope_id, plan_id),
            ).fetchone()
            if row is None:
                raise ReminderError("medication plan not found in this scope")
            conn.execute(
                "UPDATE care_medication_plans SET status='cancelled', updated_at=? WHERE scope_id=? AND plan_id=?",
                (now, self.scope_id, plan_id),
            )
            conn.execute(
                "UPDATE care_medication_plan_versions SET status='cancelled' WHERE scope_id=? AND plan_id=? AND status IN ('active','proposed')",
                (self.scope_id, plan_id),
            )
            self._cancel_plan_schedules(conn, plan_id, reason, now)
            return {"plan_id": plan_id, "version": row[0], "status": "cancelled"}

    def get_plan(self, *, plan_id: str, include_history: bool = False) -> dict[str, Any]:
        self.session.require("read")
        _require_id(plan_id, "plan id")
        with self._connection("read") as conn:
            plan = conn.execute(
                "SELECT plan_id, current_version, status, created_by, created_at, updated_at "
                "FROM care_medication_plans WHERE scope_id=? AND plan_id=?",
                (self.scope_id, plan_id),
            ).fetchone()
            if plan is None:
                raise ReminderError("medication plan not found in this scope")
            versions = conn.execute(
                """SELECT version, medication, instructions, schedule_json, source_ref, status,
                          created_by, created_at, confirmed_by, confirmed_at, confirmation_source_ref
                   FROM care_medication_plan_versions WHERE scope_id=? AND plan_id=?
                   ORDER BY version""",
                (self.scope_id, plan_id),
            ).fetchall()
        result = dict(plan)
        all_versions = []
        for row in versions:
            item = dict(row)
            item["schedule"] = json.loads(item.pop("schedule_json"))
            all_versions.append(item)
        result["versions"] = all_versions if include_history else [
            item for item in all_versions if item["version"] == result["current_version"]
        ]
        return result

    def cancel_manual_reminder(self, *, schedule_id: str, reason: str = "cancelled by user") -> dict[str, Any]:
        self.session.require("write")
        _require_id(schedule_id, "schedule id")
        reason = _required_text(reason, "cancellation reason", 500)
        now = _aware_iso(self.clock())
        with self._connection("write") as conn:
            row = conn.execute(
                "SELECT 1 FROM care_reminder_schedules WHERE scope_id=? AND schedule_id=? AND reminder_kind='manual' AND enabled=1",
                (self.scope_id, schedule_id),
            ).fetchone()
            if row is None:
                raise ReminderError("active manual reminder not found")
            self._cancel_schedule(conn, schedule_id, reason, now)
        return {"schedule_id": schedule_id, "status": "cancelled"}

    def list_upcoming(self, *, now: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.session.require("read")
        if not 1 <= limit <= 200:
            raise ReminderError("limit must be between 1 and 200")
        now_iso = _aware_iso(now or self.clock())
        with self._connection("read") as conn:
            rows = conn.execute(
                """SELECT o.occurrence_id, o.schedule_id, o.plan_id, o.plan_version,
                          o.scheduled_at, o.status, s.reminder_kind, s.message, s.enabled
                   FROM care_reminder_occurrences o JOIN care_reminder_schedules s
                     ON s.scope_id=o.scope_id AND s.schedule_id=o.schedule_id
                   WHERE o.scope_id=? AND s.enabled=1 AND o.status IN ('ready','leased','unknown')
                     AND o.scheduled_at>=? ORDER BY o.scheduled_at LIMIT ?""",
                (self.scope_id, now_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reminder(self, *, occurrence_id: str) -> dict[str, Any]:
        """Inspect the durable state and send attempts for one scoped occurrence."""
        self.session.require("read")
        _require_id(occurrence_id, "occurrence id")
        with self._connection("read") as conn:
            row = conn.execute(
                """SELECT o.occurrence_id, o.schedule_id, o.plan_id, o.plan_version,
                          o.scheduled_at, o.status, o.client_request_id, o.attempt_count,
                          o.acknowledged_by, o.acknowledged_at, o.provider_message_id,
                          o.last_error, s.reminder_kind, s.message, s.enabled
                   FROM care_reminder_occurrences o JOIN care_reminder_schedules s
                     ON s.scope_id=o.scope_id AND s.schedule_id=o.schedule_id
                   WHERE o.scope_id=? AND o.occurrence_id=?""",
                (self.scope_id, occurrence_id),
            ).fetchone()
            if row is None:
                raise ReminderError("reminder occurrence not found in this scope")
            result = dict(row)
            result["attempts"] = [dict(attempt) for attempt in conn.execute(
                """SELECT status, started_at, finished_at, provider_message_id, detail
                   FROM care_reminder_attempts WHERE scope_id=? AND occurrence_id=?
                   ORDER BY started_at""",
                (self.scope_id, occurrence_id),
            ).fetchall()]
        return result

    def acknowledge_reminder(self, *, occurrence_id: str) -> dict[str, Any]:
        """Acknowledge notification only. This never creates a dose administration."""
        self.session.require("write")
        _require_id(occurrence_id, "occurrence id")
        now = _aware_iso(self.clock())
        with self._connection("write") as conn:
            row = conn.execute(
                "SELECT status FROM care_reminder_occurrences WHERE scope_id=? AND occurrence_id=?",
                (self.scope_id, occurrence_id),
            ).fetchone()
            if row is None:
                raise ReminderError("reminder occurrence not found")
            if row[0] not in {"sent", "acknowledged"}:
                raise ReminderConflict("only a sent reminder can be acknowledged")
            conn.execute(
                """UPDATE care_reminder_occurrences SET status='acknowledged', acknowledged_by=?,
                   acknowledged_at=?, updated_at=? WHERE scope_id=? AND occurrence_id=?""",
                (self.actor_id, now, now, self.scope_id, occurrence_id),
            )
        return {"occurrence_id": occurrence_id, "status": "acknowledged",
                "administration_recorded": False}

    def retry_failed(self, *, occurrence_id: str) -> dict[str, Any]:
        """Requeue only a provider-confirmed non-send; unknown outcomes must reconcile first."""
        self.session.require("write")
        _require_id(occurrence_id, "occurrence id")
        now = _aware_iso(self.clock())
        with self._connection("write") as conn:
            row = conn.execute(
                "SELECT status, client_request_id FROM care_reminder_occurrences "
                "WHERE scope_id=? AND occurrence_id=?",
                (self.scope_id, occurrence_id),
            ).fetchone()
            if row is None:
                raise ReminderError("reminder occurrence not found")
            if row[0] != "failed":
                raise ReminderConflict("only a definitively failed send can be retried")
            if not self._occurrence_active(conn, occurrence_id):
                self._cancel_occurrence(conn, occurrence_id, now)
                raise ReminderConflict("reminder is no longer active")
            conn.execute(
                "UPDATE care_reminder_occurrences SET status='ready', last_error=NULL, updated_at=? "
                "WHERE scope_id=? AND occurrence_id=? AND status='failed'",
                (now, self.scope_id, occurrence_id),
            )
        return {"occurrence_id": occurrence_id, "status": "ready",
                "client_request_id": row[1]}

    def record_administration(
        self,
        *,
        plan_id: str,
        version: int,
        occurred_at: datetime,
        source_ref: str,
        report: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record a human-reported administration; no reminder state is consulted."""
        self.session.require("write")
        _require_id(plan_id, "plan id")
        if not isinstance(version, int) or version < 1:
            raise ReminderError("plan version must be positive")
        source_ref = _required_text(source_ref, "source reference", 512)
        report = _required_text(report, "administration report", 1000)
        idempotency_key = _required_text(idempotency_key, "idempotency key", 256)
        occurred_iso = _aware_iso(occurred_at)
        reported_iso = _aware_iso(self.clock())
        admin_id = str(uuid4())
        with self._connection("write") as conn:
            exists = conn.execute(
                "SELECT 1 FROM care_medication_plan_versions WHERE scope_id=? AND plan_id=? AND version=?",
                (self.scope_id, plan_id, version),
            ).fetchone()
            if exists is None:
                raise ReminderError("medication plan version not found in this scope")
            fingerprint = _request_fingerprint({
                "plan_id": plan_id, "version": version, "occurred_at": occurred_iso,
                "source_ref": source_ref, "report": report, "reported_by": self.actor_id,
            })
            conn.execute(
                """INSERT OR IGNORE INTO care_medication_administrations
                   (scope_id, administration_id, plan_id, plan_version, occurred_at, reported_at,
                    reported_by, source_ref, report, idempotency_key, request_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.scope_id, admin_id, plan_id, version, occurred_iso, reported_iso,
                 self.actor_id, source_ref, report, idempotency_key, fingerprint),
            )
            row = conn.execute(
                "SELECT administration_id, plan_id, plan_version, occurred_at, reported_by, source_ref, report, request_fingerprint "
                "FROM care_medication_administrations WHERE scope_id=? AND reported_by=? AND idempotency_key=?",
                (self.scope_id, self.actor_id, idempotency_key),
            ).fetchone()
            if row[7] != fingerprint:
                raise ReminderConflict("idempotency key was already used for a different report")
        result = dict(row)
        result.pop("request_fingerprint", None)
        return {**result, "status": "reported"}

    def dispatch_due(
        self,
        egress: ReminderEgress,
        *,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> dict[str, int]:
        """Materialize and send due messages through injected egress.

        A timeout/exception becomes `unknown` and is never retried until lookup
        definitively says not_sent. Database idempotency protects local replay;
        transport delivery is not claimed to be exactly once.
        """
        self.session.require("write")
        if self.authorize_current is None:
            raise ReminderError("current-grant authorization callback is required for egress")
        if not 1 <= limit <= 100 or not 15 <= lease_seconds <= 900:
            raise ReminderError("dispatch limits are outside the safe range")
        clock_now = _parse_aware(_aware_iso(now or self.clock()))
        now_iso = _aware_iso(clock_now)
        with self._connection("write") as conn:
            schedules = conn.execute(
                "SELECT schedule_id FROM care_reminder_schedules WHERE scope_id=? AND enabled=1",
                (self.scope_id,),
            ).fetchall()
            for row in schedules:
                self._materialize_schedule(
                    conn, row[0], clock_now + timedelta(days=8), now_iso,
                    not_before=clock_now - timedelta(seconds=self.max_late_seconds),
                )
            # A process restart leaves an expired lease with uncertain transport state.
            # Reconcile it explicitly; never convert expired leases back to ready.
            conn.execute(
                """UPDATE care_reminder_attempts SET status='unknown_stale'
                   WHERE scope_id=? AND status='started' AND occurrence_id IN (
                     SELECT occurrence_id FROM care_reminder_occurrences
                     WHERE scope_id=? AND status='leased' AND lease_until<?
                   )""",
                (self.scope_id, self.scope_id, now_iso),
            )
            conn.execute(
                """UPDATE care_reminder_occurrences SET status='unknown', lease_token=NULL,
                   lease_until=NULL, last_error='worker lease expired; delivery needs reconciliation',
                   updated_at=? WHERE scope_id=? AND status='leased' AND lease_until<?""",
                (now_iso, self.scope_id, now_iso),
            )
            stale = clock_now - timedelta(seconds=self.max_late_seconds)
            conn.execute(
                "UPDATE care_reminder_occurrences SET status='expired', updated_at=? "
                "WHERE scope_id=? AND status='ready' AND scheduled_at<?",
                (now_iso, self.scope_id, _aware_iso(stale)),
            )
            rows = conn.execute(
                """SELECT o.occurrence_id FROM care_reminder_occurrences o
                   JOIN care_reminder_schedules s ON s.scope_id=o.scope_id AND s.schedule_id=o.schedule_id
                   WHERE o.scope_id=? AND o.status='ready' AND s.enabled=1 AND o.scheduled_at<=?
                     AND (o.plan_id IS NULL OR EXISTS (
                       SELECT 1 FROM care_medication_plans p WHERE p.scope_id=o.scope_id
                       AND p.plan_id=o.plan_id AND p.status='active' AND p.current_version=o.plan_version))
                   ORDER BY o.scheduled_at LIMIT ?""",
                (self.scope_id, now_iso, limit),
            ).fetchall()
            ids = [row[0] for row in rows]
        results = {"sent": 0, "not_sent": 0, "unknown": 0, "cancelled": 0, "stale": 0, "expired": 0}
        for occurrence_id in ids:
            outcome = self._dispatch_one(egress, occurrence_id, clock_now, lease_seconds)
            results[outcome] += 1
        with self._connection("read") as conn:
            results["expired"] = conn.execute(
                "SELECT COUNT(*) FROM care_reminder_occurrences WHERE scope_id=? AND status='expired' AND updated_at=?",
                (self.scope_id, now_iso),
            ).fetchone()[0]
        return results

    def reconcile_unknown(self, egress: ReminderEgress, *, limit: int = 50) -> dict[str, int]:
        """Look up uncertain sends. Retry becomes possible only after a not_sent result."""
        self.session.require("write")
        if self.authorize_current is None:
            raise ReminderError("current-grant authorization callback is required for egress")
        if not 1 <= limit <= 200:
            raise ReminderError("limit must be between 1 and 200")
        now = _aware_iso(self.clock())
        with self._connection("read") as conn:
            rows = conn.execute(
                """SELECT a.occurrence_id, a.client_request_id, a.attempt_id, a.status
                   FROM care_reminder_attempts a JOIN care_reminder_occurrences o
                     ON o.scope_id=a.scope_id AND o.occurrence_id=a.occurrence_id
                   WHERE a.scope_id=? AND (
                     (o.status='unknown' AND a.status IN ('started','unknown'))
                     OR (o.status='cancelled' AND a.status IN ('started','unknown_after_cancel'))
                     OR a.status='unknown_stale'
                   )
                   ORDER BY a.started_at LIMIT ?""",
                (self.scope_id, limit),
            ).fetchall()
            items = [tuple(row) for row in rows]
        result = {"sent": 0, "not_sent": 0, "unknown": 0, "cancelled": 0, "stale": 0, "skipped": 0}
        for occurrence_id, request_id, attempt_id, prior_occurrence_status in items:
            if not self._current_authorized():
                result["skipped"] += 1
                continue
            try:
                delivery = egress.lookup(client_request_id=request_id)
            except Exception:
                delivery = DeliveryResult("unknown", detail="egress reconciliation failed")
            with self._connection("write") as conn:
                row = conn.execute(
                    "SELECT status, plan_id, plan_version FROM care_reminder_occurrences "
                    "WHERE scope_id=? AND occurrence_id=?",
                    (self.scope_id, occurrence_id),
                ).fetchone()
                if prior_occurrence_status == "unknown_stale":
                    if delivery.status == "sent":
                        status = "sent_stale"
                    elif delivery.status == "not_sent":
                        status = "not_sent_stale"
                    else:
                        status = "unknown_stale"
                    self._finish_attempt(conn, attempt_id, status, delivery, now)
                    result["stale"] += 1
                    continue
                if row is None or row[0] == "cancelled":
                    if delivery.status == "sent":
                        status = "sent_after_cancel"
                    elif delivery.status == "not_sent":
                        status = "not_sent_after_cancel"
                    else:
                        status = "unknown_after_cancel"
                    self._finish_attempt(conn, attempt_id, status, delivery, now)
                    result["cancelled"] += 1
                    continue
                if delivery.status == "sent":
                    conn.execute(
                        "UPDATE care_reminder_occurrences SET status='sent', provider_message_id=?, "
                        "lease_token=NULL, lease_until=NULL, updated_at=? WHERE scope_id=? AND occurrence_id=? AND status='unknown'",
                        (delivery.provider_message_id, now, self.scope_id, occurrence_id),
                    )
                    self._finish_attempt(conn, attempt_id, "sent", delivery, now)
                    result["sent"] += 1
                elif delivery.status == "not_sent":
                    active = self._occurrence_active(conn, occurrence_id)
                    new_status = "ready" if active else "cancelled"
                    conn.execute(
                        "UPDATE care_reminder_occurrences SET status=?, lease_token=NULL, lease_until=NULL, "
                        "last_error=?, updated_at=? WHERE scope_id=? AND occurrence_id=? AND status='unknown'",
                        (new_status, delivery.detail, now, self.scope_id, occurrence_id),
                    )
                    self._finish_attempt(conn, attempt_id, "not_sent", delivery, now)
                    result["not_sent" if active else "cancelled"] += 1
                else:
                    self._finish_attempt(conn, attempt_id, "unknown", delivery, now)
                    result["unknown"] += 1
        return result

    def _dispatch_one(self, egress: ReminderEgress, occurrence_id: str,
                      now: datetime, lease_seconds: int) -> str:
        now_iso = _aware_iso(now)
        lease_token = str(uuid4())
        with self._connection("write") as conn:
            row = conn.execute(
                """SELECT o.schedule_id, o.generation, o.plan_id, o.plan_version, o.local_key,
                          o.scheduled_at, o.client_request_id, s.message, s.enabled
                   FROM care_reminder_occurrences o JOIN care_reminder_schedules s
                   ON s.scope_id=o.scope_id AND s.schedule_id=o.schedule_id
                   WHERE o.scope_id=? AND o.occurrence_id=? AND o.status='ready'""",
                (self.scope_id, occurrence_id),
            ).fetchone()
            if row is None or not row[8] or not self._occurrence_active(conn, occurrence_id):
                if row is not None:
                    self._cancel_occurrence(conn, occurrence_id, now_iso)
                return "cancelled"
            changed = conn.execute(
                """UPDATE care_reminder_occurrences SET status='leased', lease_token=?, lease_until=?,
                   attempt_count=attempt_count+1, updated_at=? WHERE scope_id=? AND occurrence_id=? AND status='ready'""",
                (lease_token, _aware_iso(now + timedelta(seconds=lease_seconds)), now_iso,
                 self.scope_id, occurrence_id),
            ).rowcount
            if not changed:
                return "cancelled"
            attempt_id = str(uuid4())
            conn.execute(
                """INSERT INTO care_reminder_attempts
                   (scope_id, attempt_id, occurrence_id, client_request_id, status, started_at)
                   VALUES (?, ?, ?, ?, 'started', ?)""",
                (self.scope_id, attempt_id, occurrence_id, row[6], now_iso),
            )
            payload = {
                "reminder_kind": "medication" if row[2] else "manual",
                "plan_id": row[2],
                "plan_version": row[3],
                "message": row[7],
                "scheduled_at": row[5],
                "occurrence_id": occurrence_id,
            }
            client_request_id = row[6]
        # Last authorization and generation check immediately before external egress.
        self.session.require("write")
        if not self._current_authorized():
            return self._cancel_claim(occurrence_id, lease_token, now_iso)
        with self._connection("read") as conn:
            current = conn.execute(
                "SELECT lease_token, status FROM care_reminder_occurrences WHERE scope_id=? AND occurrence_id=?",
                (self.scope_id, occurrence_id),
            ).fetchone()
            active = self._occurrence_active(conn, occurrence_id)
            if current is None or current[0] != lease_token or current[1] != "leased" or not active:
                return self._cancel_claim(occurrence_id, lease_token, now_iso)
        try:
            delivery = egress.send(payload, client_request_id=client_request_id)
            if delivery.status not in {"sent", "not_sent", "unknown"}:
                delivery = DeliveryResult("unknown", detail="invalid egress response")
        except Exception as exc:
            delivery = DeliveryResult("unknown", detail=f"egress exception: {type(exc).__name__}")
        finished = _aware_iso(self._now())
        with self._connection("write") as conn:
            row = conn.execute(
                "SELECT status, lease_token FROM care_reminder_occurrences WHERE scope_id=? AND occurrence_id=?",
                (self.scope_id, occurrence_id),
            ).fetchone()
            if row is None:
                return "unknown"
            if row[0] == "cancelled" or row[1] != lease_token:
                suffix = "after_cancel" if row[0] == "cancelled" else "stale"
                outcome = delivery.status if delivery.status in {"sent", "not_sent"} else "unknown"
                attempt_status = f"{outcome}_{suffix}"
                self._finish_attempt(conn, attempt_id, attempt_status, delivery, finished)
                return "cancelled" if row[0] == "cancelled" else "stale"
            new_status = {"sent": "sent", "not_sent": "failed", "unknown": "unknown"}[delivery.status]
            conn.execute(
                """UPDATE care_reminder_occurrences SET status=?, lease_token=NULL, lease_until=NULL,
                   provider_message_id=?, last_error=?, updated_at=?
                   WHERE scope_id=? AND occurrence_id=? AND status='leased' AND lease_token=?""",
                (new_status, delivery.provider_message_id, delivery.detail, finished,
                 self.scope_id, occurrence_id, lease_token),
            )
            self._finish_attempt(conn, attempt_id, delivery.status, delivery, finished)
        return delivery.status

    def _cancel_claim(self, occurrence_id: str, lease_token: str, now: str) -> str:
        with self._connection("write") as conn:
            conn.execute(
                "UPDATE care_reminder_occurrences SET status='cancelled', lease_token=NULL, lease_until=NULL, updated_at=? "
                "WHERE scope_id=? AND occurrence_id=? AND status='leased' AND lease_token=?",
                (now, self.scope_id, occurrence_id, lease_token),
            )
        return "cancelled"

    def _materialize_schedule(self, conn: sqlite3.Connection, schedule_id: str,
                              through: datetime, now: str, *, not_before: datetime) -> int:
        row = conn.execute(
            """SELECT schedule_json, generation, plan_id, plan_version, enabled,
                      last_generated_date, start_at, end_at
               FROM care_reminder_schedules WHERE scope_id=? AND schedule_id=?""",
            (self.scope_id, schedule_id),
        ).fetchone()
        if row is None or not row[4]:
            return 0
        schedule = _schedule_from_json(json.loads(row[0]))
        occurrences: list[tuple[str, str]] = []
        if schedule.kind == "once":
            instant = _parse_aware(schedule.at)
            if instant <= through:
                occurrences.append(("once", _aware_iso(instant)))
        else:
            zone = ZoneInfo(schedule.timezone)
            through_day = through.astimezone(zone).date()
            start_day = max(
                date.fromisoformat(schedule.start_date or ""),
                not_before.astimezone(zone).date(),
            )
            if row[5]:
                start_day = max(start_day, date.fromisoformat(row[5]) + timedelta(days=1))
            end_day = date.fromisoformat(schedule.end_date) if schedule.end_date else None
            last_day = min(through_day, end_day) if end_day else through_day
            day = start_day
            while day <= last_day:
                for wall_time in schedule.times:
                    local_key = f"{day.isoformat()}T{wall_time}"
                    instant = _resolve_wall_time(day, wall_time, zone)
                    if instant is not None:
                        occurrences.append((local_key, _aware_iso(instant)))
                day += timedelta(days=1)
            if last_day >= start_day:
                conn.execute(
                    "UPDATE care_reminder_schedules SET last_generated_date=? WHERE scope_id=? AND schedule_id=?",
                    (last_day.isoformat(), self.scope_id, schedule_id),
                )
        created = 0
        for local_key, scheduled_at in occurrences:
            if row[6] and scheduled_at < row[6]:
                continue
            if row[7] and scheduled_at > row[7]:
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO care_reminder_occurrences
                   (scope_id, occurrence_id, schedule_id, generation, plan_id, plan_version,
                    local_key, scheduled_at, status, client_request_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)""",
                (self.scope_id, str(uuid4()), schedule_id, row[1], row[2], row[3], local_key,
                 scheduled_at, str(uuid4()), now, now),
            )
            created += cursor.rowcount
        return created

    def _cancel_plan_schedules(self, conn: sqlite3.Connection, plan_id: str,
                               reason: str, now: str) -> None:
        rows = conn.execute(
            "SELECT schedule_id FROM care_reminder_schedules WHERE scope_id=? AND plan_id=? AND enabled=1",
            (self.scope_id, plan_id),
        ).fetchall()
        for row in rows:
            self._cancel_schedule(conn, row[0], reason, now)

    def _cancel_schedule(self, conn: sqlite3.Connection, schedule_id: str,
                         reason: str, now: str) -> None:
        conn.execute(
            """UPDATE care_reminder_schedules SET enabled=0, generation=generation+1,
               cancelled_at=?, cancel_reason=? WHERE scope_id=? AND schedule_id=? AND enabled=1""",
            (now, reason, self.scope_id, schedule_id),
        )
        conn.execute(
            """UPDATE care_reminder_occurrences SET status='cancelled', lease_token=NULL,
               lease_until=NULL, updated_at=? WHERE scope_id=? AND schedule_id=?
               AND status IN ('ready','leased','unknown')""",
            (now, self.scope_id, schedule_id),
        )

    def _cancel_occurrence(self, conn: sqlite3.Connection, occurrence_id: str, now: str) -> None:
        conn.execute(
            "UPDATE care_reminder_occurrences SET status='cancelled', lease_token=NULL, lease_until=NULL, updated_at=? "
            "WHERE scope_id=? AND occurrence_id=? AND status IN ('ready','leased','unknown')",
            (now, self.scope_id, occurrence_id),
        )

    def _occurrence_active(self, conn: sqlite3.Connection, occurrence_id: str) -> bool:
        row = conn.execute(
            """SELECT s.enabled, s.generation, o.generation, o.plan_id, o.plan_version,
                      p.status, p.current_version
               FROM care_reminder_occurrences o JOIN care_reminder_schedules s
                 ON s.scope_id=o.scope_id AND s.schedule_id=o.schedule_id
               LEFT JOIN care_medication_plans p
                 ON p.scope_id=o.scope_id AND p.plan_id=o.plan_id
               WHERE o.scope_id=? AND o.occurrence_id=?""",
            (self.scope_id, occurrence_id),
        ).fetchone()
        if row is None or not row[0] or row[1] != row[2]:
            return False
        if row[3] is None:
            return True
        return row[5] == "active" and row[6] == row[4]

    def _finish_attempt(self, conn: sqlite3.Connection, attempt_id: str, status: str,
                        delivery: DeliveryResult, now: str) -> None:
        conn.execute(
            """UPDATE care_reminder_attempts SET status=?, finished_at=?, provider_message_id=?, detail=?
               WHERE scope_id=? AND attempt_id=?""",
            (status, now, delivery.provider_message_id, delivery.detail, self.scope_id, attempt_id),
        )

    def _finish_last_attempt(self, conn: sqlite3.Connection, occurrence_id: str, status: str,
                             delivery: DeliveryResult, now: str) -> None:
        row = conn.execute(
            "SELECT attempt_id FROM care_reminder_attempts WHERE scope_id=? AND occurrence_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (self.scope_id, occurrence_id),
        ).fetchone()
        if row:
            self._finish_attempt(conn, row[0], status, delivery, now)

    def _now(self) -> datetime:
        return _parse_aware(_aware_iso(self.clock()))

    def _current_authorized(self) -> bool:
        if self.authorize_current is None:
            return False
        try:
            return self.authorize_current(self.session.authority) is True
        except Exception:
            return False


def _schedule_from_json(value: dict[str, Any]) -> ReminderSchedule:
    schedule = ReminderSchedule(
        kind=value.get("kind"), timezone=value.get("timezone"), at=value.get("at"),
        times=tuple(value.get("times") or ()), start_date=value.get("start_date"),
        end_date=value.get("end_date"),
    )
    schedule.validate()
    return schedule


def _schedule_bounds(schedule: ReminderSchedule) -> tuple[str, str | None]:
    if schedule.kind == "once":
        return _aware_iso(_parse_aware(schedule.at)), None
    start = date.fromisoformat(schedule.start_date or "")
    return _aware_iso(datetime.combine(start, time.min, tzinfo=ZoneInfo(schedule.timezone))), None


def _resolve_wall_time(day: date, wall_time: str, zone: ZoneInfo) -> datetime | None:
    """Map local wall time to one instant: skip spring gaps, choose first fall fold."""
    parsed = time.fromisoformat(wall_time)
    naive = datetime.combine(day, parsed)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and all(item.utcoffset() != candidate.utcoffset() for item in candidates):
            candidates.append(candidate)
    if not candidates:
        return None
    chosen = min(candidates, key=lambda item: item.astimezone(UTC))
    return chosen.astimezone(UTC)


def _medication_message(medication: str, instructions: str,
                        schedule: ReminderSchedule) -> str:
    # Preserve clinician/user text; do not infer dose, route, timing or missed-dose action.
    return f"Promemoria farmaco: {medication}. {instructions}"[:1200]


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ReminderError(f"{name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ReminderError(f"{name} must contain 1 to {maximum} characters")
    return normalized


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _require_id(value: Any, name: str) -> None:
    if not _valid_id(value):
        raise ReminderError(f"{name} is invalid")


def _parse_aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ReminderError("timestamp must be an ISO-8601 string with an offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReminderError("timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReminderError("timestamp must include a timezone offset")
    return parsed


def _aware_iso(value: Any) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReminderError("datetime must include a timezone offset")
    precision = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=precision).replace("+00:00", "Z")


def _date_iso(value: date | str | None) -> str:
    if isinstance(value, datetime):
        raise ReminderError("schedule date must not include a time")
    try:
        if isinstance(value, str):
            return date.fromisoformat(value).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        raise ValueError
    except (TypeError, ValueError) as exc:
        raise ReminderError("schedule date must use YYYY-MM-DD") from exc


def _request_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
