"""Synthetic doctor reports connect Apollo logs, actual reports and documents."""
from __future__ import annotations

import importlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    name = "care_reports_test_plugin"
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(name, package)
    core = importlib.import_module(name + ".care_store")
    reminders = importlib.import_module(name + ".care_reminders")
    reports = importlib.import_module(name + ".care_reports")
    database = core.CareStore(tmp_path / "apollo.db", scope_id="synthetic-child")
    session = database.session(core.Authority("synthetic-adult-a", "synthetic-child", frozenset({"read", "write", "admin"})))
    session.define_category("journal")
    clock = types.SimpleNamespace(now=datetime(2030, 1, 2, 10, tzinfo=UTC))
    service = reminders.CareReminderService(session, clock=lambda: clock.now, authorize_current=lambda authority: True)
    return types.SimpleNamespace(core=core, reminders=reminders, reports=reports, database=database,
                                 session=session, service=service, clock=clock, tmp_path=tmp_path)


def case(world, kind="episode", request="episode", **kwargs):
    return world.session.create_case(kind=kind, title="Caso sintetico", request_id=request,
                                    source_ref="synthetic:"+request, **kwargs)


def medication(world, *, activate=False):
    schedule = world.reminders.ReminderSchedule.once(world.clock.now + timedelta(minutes=1))
    plan = world.service.propose_plan(medication="Farmaco sintetico", instructions="Istruzioni sintetiche originali.",
        schedule=schedule, source_ref="synthetic:clinician-note", plan_id="synthetic-plan")
    if activate:
        receipt = world.reminders._mint_verified_confirmation(scope_id=world.session.authority.scope_id,
            actor_id=world.session.authority.actor_id, plan_id=plan["plan_id"], version=1,
            source_ref="synthetic:explicit-confirmation", confirmed_at=world.clock.now)
        world.service.activate_plan(receipt)
    return plan


def administration(world, request="dose-1", links=None, **kwargs):
    return world.service.record_administration(plan_id="synthetic-plan", version=1,
        occurred_at=world.clock.now, source_ref="synthetic:"+request,
        report="Assunzione riferita dalla persona.", idempotency_key=request,
        case_ids=links, **kwargs)


def acknowledged(world):
    world.clock.now += timedelta(minutes=2)
    class Egress:
        def send(self, payload, *, client_request_id):
            return world.reminders.DeliveryResult("sent", provider_message_id="synthetic-reminder")
        def lookup(self, *, client_request_id):
            return world.reminders.DeliveryResult("unknown")
    assert world.service.dispatch_due(Egress())["sent"] == 1
    with world.session.connection() as conn:
        occurrence_id = conn.execute("SELECT occurrence_id FROM care_reminder_occurrences").fetchone()[0]
    world.service.acknowledge_reminder(occurrence_id=occurrence_id)
    return occurrence_id


def document(world, *, episode=None, visit=None, request="document-1"):
    pytest.importorskip("PIL")
    from PIL import Image
    docs = importlib.import_module(world.core.__package__ + ".care_documents")
    content = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(content, format="PNG")
    workflow = docs.DocumentWorkflow(world.session, runtime_root=world.tmp_path / "documents")
    job = workflow.intake([docs.DocumentInput("synthetic-note.png", "image/png", content.getvalue())],
        request_id=request, episode_id=episode, visit_id=visit)
    workflow.propose(job["job_id"], request_id="extraction", source_index=0, page_number=1,
        quote="Citazione sintetica da verificare.", proposal={"note": "Proposta non confermata", "medication": "Nessuna attivazione"})
    with world.session.connection(permission="write") as conn:
        conn.execute("UPDATE care_document_jobs SET created_at='2030-01-02T09:00:00+00:00' WHERE job_id=?", (job["job_id"],))
        receipt = {"file_id": "synthetic-drive-file", "sha256": "a"*64,
                   "recorded_at": "2030-01-02T09:01:00Z", "folder_id": "must-not-appear",
                   "upload_session_uri": "must-not-appear"}
        conn.execute("INSERT INTO care_document_uploads(job_id,item_key,sha256,mime_type,name,upload_key,folder_id,state,receipt_json,upload_session_uri) VALUES(?, 'original:0', ?, 'image/png', 'synthetic-note.png', ?, 'must-not-appear', 'uploaded', ?, 'must-not-appear')",
                     (job["job_id"], "a"*64, job["job_id"]+":upload", json.dumps(receipt)))
    return job, workflow


def test_acknowledgements_never_become_doses_and_unknown_reports_stay_unknown(world):
    medication(world, activate=True)
    acknowledged(world)
    world.session.append_entry(category_id="journal", request_id="unknown-timing", source_ref="synthetic:unknown",
        narrative="Assunzione riferita senza orario; orario sconosciuto.")
    report = world.reports.assemble_report(world.session, start="2030-01-02", end="2030-01-03")
    assert report["medication"]["reported_administrations"]["total"] == 0
    assert report["medication"]["reminder_acknowledgements"]["total"] == 1
    assert report["medication"]["reminder_acknowledgements"]["items"][0]["administration_recorded"] is False
    assert len(report["unknown_occurrence_entries"]) == 1
    assert report["clinical_inference"] is False
    assert "adherence" not in report["medication"] and "missed_doses" not in report["medication"]
    with world.session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_medication_administrations").fetchone()[0] == 0


def test_episode_reports_actual_administration_with_immutable_plan_snapshot(world):
    episode = case(world)
    medication(world, activate=True)
    acknowledged(world)
    dose = administration(world, links=[episode["case_id"]])
    world.service.revise_plan(plan_id="synthetic-plan", expected_version=1,
        medication="Farmaco sintetico aggiornato", instructions="Istruzioni sintetiche nuove.",
        schedule=world.reminders.ReminderSchedule.once(world.clock.now + timedelta(days=1)), source_ref="synthetic:revision")
    world.session.update_case(episode["case_id"], expected_revision=1, request_id="closed", source_ref="synthetic:closed", status="closed", reason="Chiusura esplicita")
    report = world.reports.assemble_report(world.session, episode_id=episode["case_id"], end="2030-01-03")
    actual = report["medication"]["reported_administrations"]
    assert actual["total"] == 1
    assert actual["items"][0]["administration_id"] == dose["administration_id"]
    assert actual["items"][0]["plan_snapshot"]["instructions"] == "Istruzioni sintetiche originali."
    assert actual["items"][0]["plan_snapshot"]["confirmation_source_ref"] == "synthetic:explicit-confirmation"
    assert actual["items"][0]["plan_version"] == 1
    assert report["medication"]["reminder_acknowledgements"]["total"] == 1
    assert report["medication"]["reminder_acknowledgements"]["items"][0]["case_relevance"] == "plan_context_only_not_an_episode_dose"


def test_case_links_validate_same_scope_and_replay_preserves_old_fingerprints(world):
    medication(world)
    episode = case(world)
    args = dict(plan_id="synthetic-plan", version=1, occurred_at=world.clock.now,
        source_ref="synthetic:replay", report="Assunzione riferita", idempotency_key="replay")
    first = world.service.record_administration(**args)
    with world.session.connection(permission="write") as conn:
        conn.execute("DROP TABLE care_medication_administration_cases")
        conn.execute("UPDATE care_reminder_schema SET version=1")
    replay = world.service.record_administration(**args, case_ids=[])
    assert replay["administration_id"] == first["administration_id"]
    with world.session.connection() as conn:
        assert conn.execute("SELECT version FROM care_reminder_schema").fetchone()[0] == 2
    with pytest.raises(world.reminders.ReminderConflict):
        world.service.record_administration(**args, case_ids=[episode["case_id"]])
    with pytest.raises(world.reminders.ReminderError):
        administration(world, request="wrong-patient", links=["foreign-case"])
    linked = administration(world, request="linked", links=[episode["case_id"], episode["case_id"]])
    assert linked["case_ids"] == [episode["case_id"]]
    assert administration(world, request="linked", links=[episode["case_id"]])["administration_id"] == linked["administration_id"]
    with world.session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_medication_administrations").fetchone()[0] == 2


def test_range_since_visit_and_episode_filter_unlinked_doses_explicitly(world):
    issue = case(world, kind="issue", request="issue")
    episode = case(world, parent_id=issue["case_id"])
    unrelated = case(world, request="unrelated")
    visit = case(world, kind="visit", request="visit", occurred_at="2030-01-02T09:00:00Z")
    world.session.append_entry(category_id="journal", request_id="visit-link", source_ref="synthetic:visit",
        narrative="Visita collegata.", links=[visit["case_id"], episode["case_id"]])
    medication(world)
    administration(world, request="relevant", links=[episode["case_id"]])
    administration(world, request="other", links=[unrelated["case_id"]])
    administration(world, request="unlinked")
    world.clock.now -= timedelta(days=10)
    administration(world, request="outside", links=[episode["case_id"]])
    report = world.reports.assemble_report(world.session, since_visit_id=visit["case_id"], issue_id=issue["case_id"], end="2030-01-03")
    assert report["medication"]["reported_administrations"]["total"] == 1
    assert report["medication"]["unlinked_administrations_in_interval"]["total"] == 1
    assert report["medication"]["reported_administrations"]["items"][0]["source_ref"] == "synthetic:relevant"


def test_shared_caregivers_one_history_and_separate_adult_scope(world):
    medication(world)
    episode = case(world)
    other = world.database.session(world.core.Authority("synthetic-adult-b", "synthetic-child", frozenset({"read", "write"})))
    administration(world, request="a", links=[episode["case_id"]])
    world.reminders.CareReminderService(other, clock=lambda: world.clock.now).record_administration(
        plan_id="synthetic-plan", version=1, occurred_at=world.clock.now, source_ref="synthetic:b",
        report="Segnalazione secondo caregiver.", idempotency_key="b", case_ids=[episode["case_id"]])
    report = world.reports.assemble_report(other, episode_id=episode["case_id"], end="2030-01-03")
    assert report["medication"]["reported_administrations"]["total"] == 2
    assert {item["reported_by"] for item in report["medication"]["reported_administrations"]["items"]} == {"synthetic-adult-a", "synthetic-adult-b"}
    separate = world.core.CareStore(world.tmp_path / "adult.db", scope_id="separate-adult").session(
        world.core.Authority("synthetic-adult-a", "separate-adult", frozenset({"read"})))
    isolated = world.reports.assemble_report(separate, start="2030-01-01", end="2030-01-03")
    assert isolated["medication"]["reported_administrations"]["total"] == 0
    assert isolated["medication"]["available"] is False
    denied = world.database.session(world.core.Authority("writer-only", "synthetic-child", frozenset({"write"})))
    with pytest.raises(PermissionError):
        world.reports.assemble_report(denied, start="2030-01-01")


def test_document_visit_relevance_proposals_and_receipts_stay_unverified(world):
    episode = case(world)
    visit = case(world, kind="visit", request="visit", occurred_at="2030-01-02T08:00:00Z")
    job, _ = document(world, episode=episode["case_id"], visit=visit["case_id"])
    report = world.reports.assemble_report(world.session, episode_id=episode["case_id"], end="2030-01-03")
    evidence = report["documents"]["linked_visit_documents"]
    assert evidence["total"] == 1
    item = evidence["items"][0]
    assert item["source_ref"] == "document:"+job["job_id"]
    assert item["clinical_occurrence_time"] is None
    assert item["time_basis"] == "linked_visit"
    assert item["proposals"][0]["clinically_verified"] is False
    assert item["proposals"][0]["state"] == "unconfirmed"
    assert item["proposals"][0]["page_number"] == 1
    assert item["uploads"][0]["receipt"]["file_id"] == "synthetic-drive-file"
    assert "must-not-appear" not in json.dumps(report)
    assert report["medication"]["reported_administrations"]["total"] == 0


def test_unknown_document_time_is_not_replaced_with_filing_time_and_range_filters(world):
    episode = case(world)
    document(world, episode=episode["case_id"])
    report = world.reports.assemble_report(world.session, start="2030-01-02", end="2030-01-02")
    unknown = report["documents"]["documents_with_unknown_clinical_time"]
    assert unknown["total"] == 1
    assert unknown["items"][0]["clinical_occurrence_unknown"] is True
    assert unknown["items"][0]["relevance_time"] is None
    assert unknown["items"][0]["recorded_at"] == "2030-01-02T09:00:00+00:00"
    outside = world.reports.assemble_report(world.session, start="2030-02-01", end="2030-02-02")
    assert outside["documents"]["documents_with_unknown_clinical_time"]["total"] == 0
    episode_report = world.reports.assemble_report(world.session, episode_id=episode["case_id"], end="2030-01-01")
    assert episode_report["documents"]["documents_with_unknown_clinical_time"]["total"] == 1


def test_extension_pagination_preserves_factual_totals_and_validates_limits(world):
    medication(world)
    for index in range(4):
        administration(world, request=f"page-{index}")
    first = world.reports.assemble_report(world.session, start="2030-01-02", end="2030-01-03", evidence_limit=2)
    second = world.reports.assemble_report(world.session, start="2030-01-02", end="2030-01-03", evidence_limit=2, evidence_offset=2)
    page_a = first["medication"]["reported_administrations"]
    page_b = second["medication"]["reported_administrations"]
    assert page_a["total"] == page_b["total"] == 4
    assert page_a["next_offset"] == 2 and page_b["next_offset"] is None
    assert page_a["omitted_after"] == page_b["omitted_before"] == 2
    assert len({item["administration_id"] for item in page_a["items"] + page_b["items"]}) == 4
    assert first["medication"]["reported_counts_by_plan_version"]["items"][0]["reported_count"] == 4
    for kwargs in ({"evidence_limit": 0}, {"evidence_limit": 501}, {"evidence_offset": -1}, {"evidence_offset": True}):
        with pytest.raises(world.core.CareError):
            world.reports.assemble_report(world.session, start="2030-01-02", **kwargs)


def test_document_excerpt_bounds_and_unknown_time_do_not_confirm_source(world):
    job, workflow = document(world)
    for index in range(27):
        workflow.propose(job["job_id"], request_id=f"proposal-{index}", source_index=0, page_number=1,
            quote="x"*1500, proposal={"large": "y"*5000})
    report = world.reports.assemble_report(world.session, start="2030-01-02", end="2030-01-03")
    item = report["documents"]["documents_with_unknown_clinical_time"]["items"][0]
    assert item["proposal_evidence"] == {"total": 28, "returned": 25, "limit": 25, "omitted": 3, "truncated": True}
    assert any(proposal["quote_truncated"] for proposal in item["proposals"])
    assert any(proposal.get("proposal_content_omitted") for proposal in item["proposals"])
    assert all(proposal["clinically_verified"] is False for proposal in item["proposals"])
    assert all(len(proposal["quote"]) <= 1000 for proposal in item["proposals"])


def test_known_visit_documents_outside_range_are_excluded_even_for_episode(world):
    episode = case(world)
    visit = case(world, kind="visit", request="visit-later", occurred_at="2030-02-01T09:00:00Z")
    document(world, episode=episode["case_id"], visit=visit["case_id"])
    report = world.reports.assemble_report(world.session, episode_id=episode["case_id"], end="2030-01-03")
    assert report["documents"]["linked_visit_documents"]["total"] == 0


def test_legal_trusted_actor_identifiers_match_reminder_authority_boundary(world):
    actor = "synthetic-caregiver@example.test"
    session = world.database.session(world.core.Authority(actor, "synthetic-child", frozenset({"read", "write", "admin"})))
    service = world.reminders.CareReminderService(session, clock=lambda: world.clock.now)
    plan = service.propose_plan(medication="Farmaco sintetico", instructions="Istruzioni riferite.",
        schedule=world.reminders.ReminderSchedule.once(world.clock.now+timedelta(minutes=1)), source_ref="synthetic:plan")
    receipt = world.reminders._mint_verified_confirmation(scope_id="synthetic-child", actor_id=actor,
        plan_id=plan["plan_id"], version=1, source_ref="synthetic:confirmation", confirmed_at=world.clock.now)
    assert service.activate_plan(receipt)["status"] == "active"
    with pytest.raises(world.reminders.ReminderError):
        service.record_administration(plan_id=plan["plan_id"], version=True, occurred_at=world.clock.now,
            source_ref="synthetic:bad", report="Segnalazione sintetica", idempotency_key="bad")
