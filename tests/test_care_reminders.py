from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    package_name = "hermes_plugins.health_data"
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}", ROOT / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeEgress:
    def __init__(self, responses=None, lookup_responses=None, on_send=None):
        self.responses = list(responses or [])
        self.lookup_responses = list(lookup_responses or [])
        self.on_send = on_send
        self.sent = []
        self.lookups = []

    def send(self, payload, *, client_request_id):
        self.sent.append((payload, client_request_id))
        if self.on_send:
            self.on_send()
        if self.responses:
            return self.responses.pop(0)
        return reminders.DeliveryResult("sent", provider_message_id="synthetic-message")

    def lookup(self, *, client_request_id):
        self.lookups.append(client_request_id)
        if self.lookup_responses:
            return self.lookup_responses.pop(0)
        return reminders.DeliveryResult("unknown")


store = load_module("store")
care_store = load_module("care_store")
reminders = load_module("care_reminders")


@pytest.fixture()
def world(tmp_path):
    db_path = tmp_path / "health.db"
    authority = care_store.Authority(
        actor_id="adult-1", scope_id="patient-private", permissions=frozenset({"read", "write", "admin"})
    )
    database = care_store.CareStore(db_path, scope_id="patient-private")
    session = database.session(authority)
    clock = Clock(datetime(2026, 3, 27, 8, 0, tzinfo=UTC))
    service = reminders.CareReminderService(
        session, clock=clock, authorize_current=lambda authority: True
    )
    return db_path, session, clock, service


def _daily_schedule(module, *, day="2026-03-28", zone="Europe/Rome", at="08:00"):
    return module.ReminderSchedule.daily(
        times=[at], timezone=zone, start_date=date.fromisoformat(day)
    )


def _propose_and_activate(world, *, schedule=None):
    _, session, clock, service = world
    proposal = service.propose_plan(
        medication="Prescribed medicine",
        instructions="Use the clinician's recorded instructions.",
        schedule=schedule or _daily_schedule(reminders),
        source_ref="synthetic-doctor-note:1",
        plan_id="plan-1",
    )
    receipt = reminders._mint_verified_confirmation(
        scope_id=session.authority.scope_id,
        actor_id=session.authority.actor_id,
        plan_id=proposal["plan_id"],
        version=proposal["version"],
        source_ref="synthetic-user-confirmation:1",
        confirmed_at=clock(),
    )
    activated = service.activate_plan(receipt)
    return proposal, receipt, activated


def test_plan_requires_trusted_exact_confirmation_and_stays_draft(world):
    _, session, clock, service = world
    proposal = service.propose_plan(
        medication="Medicine A", instructions="As documented by clinician.",
        schedule=_daily_schedule(reminders), source_ref="synthetic-source:plan",
        plan_id="plan-confirm",
    )
    assert proposal["status"] == "proposed"
    assert service.get_plan(plan_id="plan-confirm")["status"] == "proposed"
    with sqlite3.connect(session.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM care_reminder_schedules").fetchone()[0] == 0

    wrong_actor = reminders._mint_verified_confirmation(
        scope_id=session.authority.scope_id, actor_id="different-adult", plan_id="plan-confirm",
        version=1, source_ref="synthetic-user-confirmation:2", confirmed_at=clock(),
    )
    with pytest.raises(reminders.ReminderError, match="actor or scope"):
        service.activate_plan(wrong_actor)
    with pytest.raises(reminders.ReminderError, match="receipt"):
        service.activate_plan(True)

    valid = reminders._mint_verified_confirmation(
        scope_id=session.authority.scope_id, actor_id=session.authority.actor_id,
        plan_id="plan-confirm", version=1, source_ref="synthetic-user-confirmation:3",
        confirmed_at=clock(),
    )
    assert service.activate_plan(valid)["status"] == "active"


def test_exact_activation_receipt_replay_returns_existing_schedule(world):
    _, session, clock, service = world
    _, receipt, activated = _propose_and_activate(world)

    replay = service.activate_plan(receipt)

    assert replay["schedule_id"] == activated["schedule_id"]
    assert replay["occurrences_created"] == 0
    with sqlite3.connect(session.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM care_reminder_schedules WHERE plan_id='plan-1'"
        ).fetchone()[0] == 1
    different = reminders._mint_verified_confirmation(
        scope_id=session.authority.scope_id,
        actor_id=session.authority.actor_id,
        plan_id="plan-1",
        version=1,
        source_ref="synthetic-user-confirmation:another-event",
        confirmed_at=clock(),
    )
    with pytest.raises(reminders.ReminderConflict, match="different receipt"):
        service.activate_plan(different)


def test_acknowledging_reminder_is_not_a_medication_administration(world):
    _, session, clock, service = world
    _, _, activated = _propose_and_activate(world)
    clock.value = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    egress = FakeEgress()
    result = service.dispatch_due(egress, now=clock(), lease_seconds=30)
    assert result["sent"] == 1
    # Sent reminders are no longer upcoming, so retrieve the durable occurrence from SQLite.
    with sqlite3.connect(session.db_path) as conn:
        occurrence_id = conn.execute(
            "SELECT occurrence_id FROM care_reminder_occurrences WHERE schedule_id=? AND status='sent'",
            (activated["schedule_id"],),
        ).fetchone()[0]
    acknowledgement = service.acknowledge_reminder(occurrence_id=occurrence_id)
    assert acknowledgement["administration_recorded"] is False
    with sqlite3.connect(session.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM care_medication_administrations").fetchone()[0] == 0

    first = service.record_administration(
        plan_id="plan-1", version=1, occurred_at=clock(), source_ref="synthetic-message:1",
        report="User reported taking the medicine.", idempotency_key="message-1",
    )
    replay = service.record_administration(
        plan_id="plan-1", version=1, occurred_at=clock(), source_ref="synthetic-message:1",
        report="User reported taking the medicine.", idempotency_key="message-1",
    )
    assert replay["administration_id"] == first["administration_id"]
    separately_reported = service.record_administration(
        plan_id="plan-1", version=1, occurred_at=clock(), source_ref="synthetic-message:2",
        report="A separate report of taking the medicine.", idempotency_key="message-2",
    )
    assert separately_reported["administration_id"] != first["administration_id"]
    caregiver = care_store.CareStore(session.db_path, scope_id="patient-private").session(
        care_store.Authority(
            "adult-2", "patient-private", frozenset({"read", "write", "admin"})
        )
    )
    second_caregiver = reminders.CareReminderService(caregiver, clock=clock).record_administration(
        plan_id="plan-1", version=1, occurred_at=clock(), source_ref="synthetic-message:1",
        report="User reported taking the medicine.", idempotency_key="message-1",
    )
    assert second_caregiver["administration_id"] != first["administration_id"]


def test_cancellation_during_send_is_not_overwritten_by_late_result(world):
    _, session, clock, service = world
    _, _, activated = _propose_and_activate(world)
    clock.value = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    egress = FakeEgress(
        on_send=lambda: service.cancel_plan(plan_id="plan-1", reason="clinician stopped plan")
    )
    result = service.dispatch_due(egress, now=clock(), lease_seconds=30)
    assert result["cancelled"] == 1
    with sqlite3.connect(session.db_path) as conn:
        occurrence = conn.execute(
            "SELECT status FROM care_reminder_occurrences WHERE schedule_id=?",
            (activated["schedule_id"],),
        ).fetchone()[0]
        attempt = conn.execute(
            "SELECT status, provider_message_id FROM care_reminder_attempts"
        ).fetchone()
    assert occurrence == "cancelled"
    assert attempt == ("sent_after_cancel", "synthetic-message")


def test_unknown_send_is_reconciled_before_retry(world):
    _, session, clock, service = world
    _, _, activated = _propose_and_activate(world)
    clock.value = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    egress = FakeEgress(
        responses=[reminders.DeliveryResult("unknown")],
        lookup_responses=[reminders.DeliveryResult("unknown"), reminders.DeliveryResult("not_sent")],
        )
    assert service.dispatch_due(egress, now=clock(), lease_seconds=30)["unknown"] == 1
    request_id = egress.sent[0][1]
    assert service.dispatch_due(egress, now=clock(), lease_seconds=30)["sent"] == 0
    assert len(egress.sent) == 1
    assert service.reconcile_unknown(egress)["unknown"] == 1
    assert service.reconcile_unknown(egress)["not_sent"] == 1
    assert service.dispatch_due(egress, now=clock(), lease_seconds=30)["sent"] == 1
    assert egress.sent[1][1] == request_id
    with sqlite3.connect(session.db_path) as conn:
        row = conn.execute(
            "SELECT status FROM care_reminder_occurrences WHERE schedule_id=? AND scheduled_at<=?",
            (activated["schedule_id"], reminders._aware_iso(clock())),
        ).fetchone()
    assert row[0] == "sent"


def test_expired_worker_lease_becomes_unknown_without_blind_resend(world):
    _, session, clock, service = world
    _, _, activated = _propose_and_activate(world)
    clock.value = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    with sqlite3.connect(session.db_path) as conn:
        occurrence = conn.execute(
            "SELECT occurrence_id, client_request_id FROM care_reminder_occurrences "
            "WHERE schedule_id=? AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1",
            (activated["schedule_id"], reminders._aware_iso(clock())),
        ).fetchone()
        conn.execute(
            "UPDATE care_reminder_occurrences SET status='leased', lease_token='worker-crashed', "
            "lease_until='2026-03-28T06:59:00Z' WHERE occurrence_id=?",
            (occurrence[0],),
        )
        conn.execute(
            """INSERT INTO care_reminder_attempts
               (scope_id, attempt_id, occurrence_id, client_request_id, status, started_at)
               VALUES (?, 'attempt-crashed', ?, ?, 'started', '2026-03-28T06:58:00Z')""",
            (session.authority.scope_id, occurrence[0], occurrence[1]),
        )
    egress = FakeEgress(lookup_responses=[reminders.DeliveryResult("unknown")])
    assert service.dispatch_due(egress, now=clock(), lease_seconds=30)["sent"] == 0
    assert egress.sent == []
    assert service.reconcile_unknown(egress)["stale"] == 1
    with sqlite3.connect(session.db_path) as conn:
        states = conn.execute(
            "SELECT o.status, a.status FROM care_reminder_occurrences o "
            "JOIN care_reminder_attempts a USING (scope_id, occurrence_id) WHERE o.occurrence_id=?",
            (occurrence[0],),
        ).fetchone()
    assert states == ("unknown", "unknown_stale")
    assert egress.sent == []


def test_dst_gap_skips_nonexistent_time_and_fold_uses_first_instance():
    gap = reminders._resolve_wall_time(
        date(2026, 3, 29), "02:30", reminders.ZoneInfo("Europe/Rome")
    )
    fold = reminders._resolve_wall_time(
        date(2026, 10, 25), "02:30", reminders.ZoneInfo("Europe/Rome")
    )
    assert gap is None
    assert fold == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_manual_reminder_replay_is_idempotent_and_scope_permissions_apply(world):
    _, session, clock, service = world
    at = clock() + timedelta(hours=1)
    first = service.create_manual_reminder(
        message="Follow-up appointment", at=at, request_ref="synthetic-message:remind-1"
    )
    replay = service.create_manual_reminder(
        message="Follow-up appointment", at=at, request_ref="synthetic-message:remind-1"
    )
    assert replay["schedule_id"] == first["schedule_id"]
    with pytest.raises(reminders.ReminderConflict, match="different reminder"):
        service.create_manual_reminder(
            message="Different request", at=at, request_ref="synthetic-message:remind-1"
        )

    read_only = care_store.CareStore(session.db_path, scope_id="patient-private").session(
        care_store.Authority("reader", "patient-private", frozenset({"read"}))
    )
    restricted = reminders.CareReminderService(read_only, clock=clock)
    with pytest.raises(care_store.AuthorizationError):
        restricted.cancel_manual_reminder(schedule_id=first["schedule_id"])
    assert restricted.list_upcoming(now=clock())


def test_dispatch_fails_closed_when_current_route_grant_is_revoked(world):
    _, session, clock, service = world
    _, _, activated = _propose_and_activate(world)
    clock.value = datetime(2026, 3, 28, 7, 0, tzinfo=UTC)
    revoked = reminders.CareReminderService(
        session, clock=clock, authorize_current=lambda authority: False
    )
    egress = FakeEgress()
    result = revoked.dispatch_due(egress, now=clock(), lease_seconds=30)
    assert result["cancelled"] == 1
    assert egress.sent == []
    with sqlite3.connect(session.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM care_reminder_occurrences WHERE schedule_id=? "
            "AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1",
            (activated["schedule_id"], reminders._aware_iso(clock())),
        ).fetchone()[0]
    assert status == "cancelled"


def test_schedule_requires_timezone_aware_instants_and_valid_local_times():
    with pytest.raises(reminders.ReminderError, match="offset"):
        reminders.ReminderSchedule.once(datetime(2026, 1, 1, 8)).validate()
    invalid = reminders.ReminderSchedule.daily(
        times=["25:00"], timezone="Europe/Rome", start_date="2026-01-01"
    )
    with pytest.raises(reminders.ReminderError, match="HH:MM"):
        invalid.validate()
