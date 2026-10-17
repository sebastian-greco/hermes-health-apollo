"""Doctor-facing evidence assembled from the same authorized Apollo database.

A reported administration, a reminder acknowledgement and an unconfirmed
extraction remain different facts. This module reads records; it never creates
clinical facts, confirms a plan, uploads a file or contacts a provider.
"""
from __future__ import annotations

import json

from .care_store import CareError, CareSession, _instant


class _Page:
    def __init__(self, limit, offset):
        self.limit, self.offset = limit, offset
        self.total, self.items = 0, []

    def add(self, value):
        if self.offset <= self.total < self.offset + self.limit:
            self.items.append(value)
        self.total += 1

    def result(self):
        returned = len(self.items)
        before = min(self.offset, self.total)
        after = max(0, self.total-before-returned)
        return {"items": self.items, "total": self.total, "returned": returned,
                "limit": self.limit, "offset": self.offset,
                "omitted_before": before, "omitted_after": after,
                "truncated": before > 0 or after > 0,
                "next_offset": self.offset+returned if after else None,
                "order": "oldest_first"}


def assemble_report(session: CareSession, *, start: str | None = None,
                    end: str | None = None, episode_id: str | None = None,
                    since_visit_id: str | None = None, issue_id: str | None = None,
                    max_entries: int = 1000, evidence_limit: int = 100,
                    evidence_offset: int = 0) -> dict:
    """Extend the core factual report; pagination never changes factual totals.

    ``evidence_offset`` applies independently to each extension section, not to
    core journal evidence. Core caps/omissions remain under its ``evidence`` key.
    """
    if not isinstance(session, CareSession):
        raise PermissionError("A trusted scoped care session is required")
    session.require("read")
    if type(evidence_limit) is not int or not 1 <= evidence_limit <= 500:
        raise CareError("evidence_limit must be between 1 and 500")
    if type(evidence_offset) is not int or not 0 <= evidence_offset <= 1000000:
        raise CareError("evidence_offset must be between 0 and 1000000")
    report = session.report(start=start, end=end, episode_id=episode_id,
                            since_visit_id=since_visit_id, issue_id=issue_id,
                            max_entries=max_entries)
    start, end = report["start"], report["end"]
    case_filter = episode_id or issue_id
    with session.connection() as conn:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cases = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT case_id,payload_json FROM care_cases")}
        medicine = _medication_evidence(conn, session.authority.scope_id, cases,
            case_filter, start, end, tables, evidence_limit, evidence_offset)
        documents = _document_evidence(conn, cases, case_filter, start, end, tables,
            evidence_limit, evidence_offset, episode_report=episode_id is not None)
    return {**report, "medication": medicine, "documents": documents,
            "report_sections": {"clinical_inference": False,
                "journal_and_extensions_use_separate_read_snapshots": True,
                "meaning": "Assunzioni riferite, conferme dei promemoria e proposte estratte sono evidenze distinte. Nessuna aderenza o dose mancante è dedotta."}}


def _matches(links, case_filter, cases):
    return not case_filter or case_filter in links or any(cases.get(link, {}).get("parent_id") == case_filter for link in links)


def _within(value, start, end):
    instant = _instant(value)
    return instant is not None and (start is None or instant >= start) and (end is None or instant <= end)


def _medication_evidence(conn, scope, cases, case_filter, start, end, tables, limit, offset):
    administrations, unlinked = _Page(limit, offset), _Page(limit, offset)
    acknowledgements, unassigned_ack = _Page(limit, offset), _Page(limit, offset)
    initialized = "care_medication_administrations" in tables
    linked_plan_versions = set()
    count_by_plan: dict[tuple, dict] = {}
    if initialized:
        for row in conn.execute("SELECT * FROM care_medication_administrations WHERE scope_id=? ORDER BY substr(occurred_at,1,19), CASE WHEN substr(occurred_at,20,1)='.' THEN substr(occurred_at,21,6) ELSE '000000' END,administration_id", (scope,)):
            if not _within(row["occurred_at"], start, end):
                continue
            links = []
            if "care_medication_administration_cases" in tables:
                links = [item[0] for item in conn.execute(
                    "SELECT case_id FROM care_medication_administration_cases WHERE scope_id=? AND administration_id=? ORDER BY case_id",
                    (scope, row["administration_id"]))]
            relevant = _matches(links, case_filter, cases)
            if not relevant and links:
                continue
            plan = conn.execute("""SELECT medication,instructions,schedule_json,source_ref,status,
                confirmed_by,confirmed_at,confirmation_source_ref FROM care_medication_plan_versions
                WHERE scope_id=? AND plan_id=? AND version=?""", (scope, row["plan_id"], row["plan_version"])).fetchone()
            item = {key: row[key] for key in ("administration_id", "plan_id", "plan_version", "occurred_at",
                    "reported_at", "reported_by", "source_ref", "report")}
            item.update(case_ids=links, evidence_kind="human_reported_administration",
                        occurrence_time_basis="reported_occurrence", clinically_verified=False)
            item["plan_snapshot"] = None if plan is None else {
                "medication": plan["medication"], "instructions": plan["instructions"],
                "schedule": json.loads(plan["schedule_json"]), "source_ref": plan["source_ref"],
                "version_status": plan["status"], "confirmed_by": plan["confirmed_by"],
                "confirmed_at": plan["confirmed_at"], "confirmation_source_ref": plan["confirmation_source_ref"]}
            if case_filter and not links:
                item["case_relevance"] = "unassigned"
                unlinked.add(item)
                continue
            administrations.add(item)
            key = (row["plan_id"], row["plan_version"])
            linked_plan_versions.add(key)
            count = count_by_plan.setdefault(key, {"plan_id": key[0], "version": key[1],
                "medication": plan["medication"] if plan else None, "reported_count": 0})
            count["reported_count"] += 1
    if "care_reminder_occurrences" in tables:
        for row in conn.execute("SELECT occurrence_id,plan_id,plan_version,scheduled_at,acknowledged_by,acknowledged_at FROM care_reminder_occurrences WHERE scope_id=? AND acknowledged_at IS NOT NULL ORDER BY substr(acknowledged_at,1,19), CASE WHEN substr(acknowledged_at,20,1)='.' THEN substr(acknowledged_at,21,6) ELSE '000000' END,occurrence_id", (scope,)):
            if not _within(row["acknowledged_at"], start, end):
                continue
            item = dict(row)
            item.update(evidence_kind="reminder_acknowledgement", administration_recorded=False,
                        time_basis="acknowledgement_time")
            if case_filter:
                key = (row["plan_id"], row["plan_version"])
                if key not in linked_plan_versions:
                    item["case_relevance"] = "unassigned"
                    unassigned_ack.add(item)
                    continue
                item["case_relevance"] = "plan_context_only_not_an_episode_dose"
            acknowledgements.add(item)
    plan_counts = _Page(limit, offset)
    for count in count_by_plan.values():
        plan_counts.add(count)
    return {"available": initialized, "reported_administrations": administrations.result(),
            "reported_counts_by_plan_version": plan_counts.result(),
            "unlinked_administrations_in_interval": unlinked.result(),
            "reminder_acknowledgements": acknowledgements.result(),
            "unassigned_reminder_acknowledgements": unassigned_ack.result(),
            "gaps": {"absence_of_report_is_not_a_missed_dose": True,
                "dose_times_unknown_are_not_inferred_from_acknowledgements": True,
                "administration_links_available": "care_medication_administration_cases" in tables,
                "plans_without_recorded_administrations_not_inferred": True,
                "case_relevance_of_acknowledgements_is_not_proven": case_filter is not None},
            "meaning": "Il conteggio riguarda assunzioni riferite, non dosi verificate. Una conferma del promemoria non è un'assunzione. Nessun calcolo di aderenza."}


def _document_evidence(conn, cases, case_filter, start, end, tables, limit, offset, *, episode_report):
    dated, unknown = _Page(limit, offset), _Page(limit, offset)
    initialized = "care_document_jobs" in tables
    related_visits = set()
    if case_filter:
        for row in conn.execute("SELECT payload_json FROM care_entries"):
            links = json.loads(row[0])["links"]
            if _matches(links, case_filter, cases):
                related_visits.update(link for link in links if cases.get(link, {}).get("kind") == "visit")
    if initialized:
        for row in conn.execute("SELECT job_id,state,created_at,episode_id,visit_id FROM care_document_jobs ORDER BY created_at,job_id"):
            links = [value for value in (row["episode_id"], row["visit_id"]) if value]
            if case_filter and not (_matches(links, case_filter, cases) or row["visit_id"] in related_visits):
                continue
            visit_time = cases.get(row["visit_id"], {}).get("occurred_at")
            if visit_time and not _within(visit_time, start, end):
                continue
            if not visit_time and not episode_report and not _within(row["created_at"], start, end):
                continue
            job = dict(row)
            job.update(source_ref="document:"+row["job_id"],
                clinical_occurrence_time=None, clinical_occurrence_unknown=True,
                relevance_time=visit_time, time_basis="linked_visit" if visit_time else "recorded_only",
                recorded_at=row["created_at"], extraction_is_verified=False,
                recorded_in_requested_interval=_within(row["created_at"], start, end))
            job["sources"] = [dict(item) for item in conn.execute(
                "SELECT source_index,name,mime_type,sha256,page_count,raw_record_id FROM care_document_sources WHERE job_id=? ORDER BY source_index", (row["job_id"],))]
            proposals = conn.execute("SELECT proposal_id,source_index,page_number,quote,proposal_json,state,created_at FROM care_document_proposals WHERE job_id=? ORDER BY created_at,proposal_id LIMIT 25", (row["job_id"],)).fetchall()
            proposal_count = conn.execute("SELECT COUNT(*) FROM care_document_proposals WHERE job_id=?", (row["job_id"],)).fetchone()[0]
            job["proposals"] = []
            for proposal in proposals:
                excerpt = {key: proposal[key] for key in ("proposal_id", "source_index", "page_number", "state", "created_at")}
                excerpt.update(quote=proposal["quote"][:1000], quote_truncated=len(proposal["quote"]) > 1000,
                    clinically_verified=False, evidence_kind="unconfirmed_extraction_proposal")
                payload = proposal["proposal_json"]
                if len(payload.encode()) <= 4000:
                    excerpt["proposal"] = json.loads(payload)
                else:
                    excerpt.update(proposal_content_omitted=True, meaning="Proposta completa disponibile nel documento originale da verificare.")
                job["proposals"].append(excerpt)
            job["proposal_evidence"] = {"total": proposal_count, "returned": len(proposals), "limit": 25,
                "omitted": proposal_count-len(proposals), "truncated": proposal_count > len(proposals)}
            job["uploads"] = []
            for upload in conn.execute("SELECT item_key,state,receipt_json FROM care_document_uploads WHERE job_id=? ORDER BY item_key", (row["job_id"],)):
                receipt = json.loads(upload["receipt_json"]) if upload["receipt_json"] else None
                # Never copy folder bindings, upload session URIs, tokens or raw errors.
                clean_receipt = None if receipt is None else {key: receipt[key] for key in ("file_id", "sha256", "recorded_at") if key in receipt}
                job["uploads"].append({"item_key": upload["item_key"], "state": upload["state"], "receipt": clean_receipt})
            (dated if visit_time else unknown).add(job)
    return {"available": initialized, "linked_visit_documents": dated.result(),
            "documents_with_unknown_clinical_time": unknown.result(),
            "time_selection": "Linked visit relevance time, otherwise recording time only; an episode includes all explicitly linked documents regardless of filing date.",
            "proposals_are_unconfirmed": True,
            "meaning": "Citazioni e dati estratti sono proposte non verificate. Il caricamento del file non conferma fatti clinici o prescrizioni."}
