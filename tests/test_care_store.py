"""Synthetic care scopes exercise real Apollo storage, food and rebuilds."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def core(tmp_path):
    package_name = "synthetic_apollo_care"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    care = importlib.import_module(f"{package_name}.care_store")
    db = care.CareStore(tmp_path / "adult.db", scope_id="adult")
    owner = db.session(care.Authority("caregiver-a", "adult", frozenset({"read", "write", "admin"})))
    owner.define_category("daily_note", expected_daily=True)
    owner.define_category("measurement", schema={"type": "object", "properties": {"value": {"type": "number"}, "unit": {"type": "string"}}, "additionalProperties": False}, observations={"value": {"unit_field": "unit", "allowed_units": ["mmol/L", "mg/dL"]}})
    return care, db, owner


def append(session, *, request_id="msg-1", **kwargs):
    return session.append_entry(category_id="daily_note", request_id=request_id,
                                source_ref="synthetic-message:1", narrative="Nota riferita.", **kwargs)


def test_generic_configuration_immutable_schema_and_bounded_data(core):
    care, _, session = core
    session.define_category("custom_topic", schema={"type": "object", "required": ["level"], "properties": {"level": {"type": "integer", "minimum": 1, "maximum": 5}}, "additionalProperties": False})
    result = session.append_entry(category_id="custom_topic", request_id="custom", source_ref="synthetic-message:2", data={"level": 3})
    assert result["data"] == {"level": 3}
    assert result["occurrence_start"] is None
    with pytest.raises(care.ConflictError):
        session.define_category("custom_topic", label="Changed interpretation")
    for data in ({"level": True}, {"level": 6}, {"level": "3"}, {"level": 3, "unconfigured": 1}):
        with pytest.raises(care.CareError):
            session.append_entry(category_id="custom_topic", request_id="invalid", source_ref="synthetic:bad", data=data)
    with pytest.raises(care.CareError):
        session.define_category("remote", schema={"$ref": "https://example.invalid/schema"})
    nested = {}
    for _ in range(14):
        nested = {"nested": nested}
    with pytest.raises(care.CareError):
        append(session, data=nested)
    with pytest.raises(care.CareError):
        append(session, data={"value": float("nan")})
    with pytest.raises(care.CareError):
        append(session, data={"large": "x" * 8001})


def test_measurement_units_explicit_and_not_silently_converted(core):
    care, _, session = core
    for data in ({"value": 5.4}, {"value": 5.4, "unit": "incompatible"}, {"value": True, "unit": "mmol/L"}):
        with pytest.raises(care.CareError):
            session.append_entry(category_id="measurement", request_id="bad-units", source_ref="synthetic:bad", data=data)
    session.append_entry(category_id="measurement", request_id="a", source_ref="synthetic:a", data={"value": 5.4, "unit": "mmol/L"}, occurrence_start="2026-06-01T12:00:00Z", uncertainty="exact")
    session.append_entry(category_id="measurement", request_id="b", source_ref="synthetic:b", data={"value": 100, "unit": "mg/dL"}, occurrence_start="2026-06-01T14:00:00Z", uncertainty="exact")
    report = session.report(start="2026-06-01", end="2026-06-01")
    assert {r["unit"]: r["mean"] for r in report["measurements"]} == {"mmol/L": 5.4, "mg/dL": 100}


def test_trusted_scope_grants_and_shared_caregivers(core, tmp_path):
    care, db, session = core
    with pytest.raises(care.AuthorizationError):
        db.session(care.Authority("other", "other-patient", frozenset({"read"})))
    with pytest.raises(care.AuthorizationError):
        care.CareStore(db.db_path, scope_id="different")
    reader = db.session(care.Authority("reader", "adult", frozenset({"read"})))
    with pytest.raises(care.AuthorizationError):
        append(reader)
    writer = db.session(care.Authority("caregiver-b", "adult", frozenset({"read", "write"})))
    entry = append(session)
    assert writer.get_entry(entry["entry_id"]) == entry
    corrected = writer.revise_entry(entry["entry_id"], expected_revision=1, request_id="b-correction", source_ref="synthetic:b", changes={"narrative": "Correzione condivisa."}, reason="Chiarimento")
    assert session.get_entry(entry["entry_id"])["actor_id"] == "caregiver-b"
    assert corrected["revision"] == 2
    with pytest.raises(care.AuthorizationError):
        writer.define_category("bypass")
    separate = care.CareStore(tmp_path / "other.db", scope_id="other").session(care.Authority("caregiver-a", "other", frozenset({"read"})))
    with pytest.raises(care.CareError):
        separate.get_entry(entry["entry_id"])
    with session.connection(permission="write") as conn:
        conn.execute("UPDATE care_scope SET scope_id='changed'")
    with pytest.raises(care.AuthorizationError):
        session.list_entries()


def test_replay_corrections_audit_and_optimistic_concurrency(core):
    care, _, session = core
    entry = append(session)
    assert append(session) == entry
    with pytest.raises(care.ConflictError):
        append(session, data={"different": True})
    correction = dict(expected_revision=1, request_id="revision", source_ref="synthetic:correction", changes={"narrative": "Corretto."}, reason="Dettaglio corretto")
    revised = session.revise_entry(entry["entry_id"], **correction)
    assert session.revise_entry(entry["entry_id"], **correction) == revised
    with pytest.raises(care.ConflictError):
        session.revise_entry(entry["entry_id"], **{**correction, "request_id": "stale"})
    history = session.get_entry(entry["entry_id"], include_revisions=True)["revisions"]
    assert [h["narrative"] for h in history] == ["Nota riferita.", "Corretto."]
    assert history[0]["source_ref"] == "synthetic-message:1"
    assert history[1]["recorded_at"] == history[0]["recorded_at"]
    with session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_records WHERE provider='care_journal'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM record_lineage WHERE canonical_table='care_entry_revisions'").fetchone()[0] == 2


def test_issue_episode_closure_reopen_and_relevant_visit_reports(core):
    care, _, session = core
    issue_args = dict(kind="issue", title="Questione riferita", request_id="issue", source_ref="synthetic:issue")
    issue = session.create_case(**issue_args)
    assert session.create_case(**issue_args) == issue
    episode = session.create_case(kind="episode", title="Episodio sintetico", request_id="episode", source_ref="synthetic:episode", parent_id=issue["case_id"], occurred_at="2026-06-01T00:00:00Z")
    visit = session.create_case(kind="visit", title="Visita sintetica", request_id="visit", source_ref="synthetic:visit", occurred_at="2026-06-02T10:00:00Z")
    append(session, request_id="old", occurrence_start="2026-06-01T10:00:00Z", uncertainty="exact", links=[episode["case_id"]])
    entry = append(session, request_id="new", occurrence_start="2026-06-03T10:00:00Z", uncertainty="exact", links=[episode["case_id"], issue["case_id"], visit["case_id"]])
    closed = session.update_case(episode["case_id"], expected_revision=1, request_id="close", source_ref="synthetic:close", status="closed", reason="Chiusura esplicita")
    assert session.list_cases(kind="episode", include_closed=False) == []
    assert len(session.list_entries(case_id=episode["case_id"])) == 2
    assert session.report(episode_id=episode["case_id"], end="2026-06-04")["entry_count"] == 2
    report = session.report(since_visit_id=visit["case_id"], issue_id=issue["case_id"], end="2026-06-04")
    assert [r["entry_id"] for r in report["entries"]] == [entry["entry_id"]]
    assert report["clinical_inference"] is False
    assert closed["status"] == "closed"
    session.update_case(episode["case_id"], expected_revision=2, request_id="reopen", source_ref="synthetic:reopen", status="open", reason="Nuove osservazioni")
    assert len(session.get_case(episode["case_id"], include_revisions=True)["revisions"]) == 3
    unrelated = session.create_case(kind="visit", title="Altra visita", request_id="other-visit", source_ref="synthetic:other-visit", occurred_at="2026-06-02T10:00:00Z")
    with pytest.raises(care.CareError):
        session.report(since_visit_id=unrelated["case_id"], issue_id=issue["case_id"])


def test_unknown_approximate_ranges_gaps_and_no_zero_inference(core):
    care, _, session = core
    unknown = append(session, request_id="unknown")
    interval = append(session, request_id="range", occurrence_start="2026-06-01T00:00:00Z", occurrence_end="2026-06-03T20:00:00Z", uncertainty="range")
    exact = append(session, request_id="known", occurrence_start="2026-06-02T12:00:00+02:00", uncertainty="exact")
    report = session.report(start="2026-06-02", end="2026-06-04")
    assert report["entry_count"] == 2
    assert report["counts_by_category"] == {"daily_note": 2}
    assert [r["entry_id"] for r in report["unknown_occurrence_entries"]] == [unknown["entry_id"]]
    assert report["uncertainty"]["interval_or_approximate_entries"] == [interval["entry_id"]]
    assert report["gaps"]["days_without_exact_time_entries"] == ["2026-06-03", "2026-06-04"]
    assert report["gaps"]["expected_daily_category_gaps"]["daily_note"] == ["2026-06-03", "2026-06-04"]
    assert exact["occurrence_start"] == "2026-06-02T10:00:00+00:00"
    with pytest.raises(care.CareError):
        append(session, request_id="naive", occurrence_start="2026-06-02T12:00:00", uncertainty="exact")
    with pytest.raises(care.CareError):
        append(session, request_id="false-unknown", occurrence_start="2026-06-02T12:00:00Z")


def test_projection_rebuild_and_correction_preserve_custom_categories(core):
    _, _, session = core
    entry = session.append_entry(category_id="measurement", request_id="measure", source_ref="synthetic:measurement", data={"value": 5.4, "unit": "mmol/L"}, occurrence_start="2026-06-01T12:00:00Z", uncertainty="exact")
    package = session.__class__.__module__.rsplit(".", 1)[0]
    semantic = importlib.import_module(f"{package}.semantic_layer")
    with session.apollo():
        semantic.refresh_canonical_facts(start="2026-06-01", end="2026-06-04")
    with session.connection() as conn:
        assert conn.execute("SELECT signal_name, value_number, unit FROM observations").fetchall()[0][:] == ("care.measurement.value", 5.4, "mmol/L")
    session.revise_entry(entry["entry_id"], expected_revision=1, request_id="move", source_ref="synthetic:corrected-time", changes={"occurrence_start": "2026-06-03T12:00:00Z", "data": {"value": 6.1, "unit": "mmol/L"}}, reason="Correzione")
    with session.apollo():
        semantic.refresh_canonical_facts(start="2026-06-01", end="2026-06-04")
        semantic.refresh_canonical_facts(start="2026-06-01", end="2026-06-04")
    with session.connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT day, value_number FROM observations WHERE provider='care_journal'")] == [("2026-06-03", 6.1)]
        attrs = json.loads(conn.execute("SELECT attributes_json FROM events WHERE provider='care_journal'").fetchone()[0])
        assert attrs["revision"] == 2


def test_apollo_context_binding_is_nested_thread_local_and_food_preserved(core, tmp_path):
    care, _, session = core
    package = care.__name__.rsplit(".", 1)[0]
    food = importlib.import_module(f"{package}.food")
    apollo = importlib.import_module(f"{package}.store")
    baseline = apollo.database_path()
    other = care.CareStore(tmp_path / "child.db", scope_id="child").session(care.Authority("caregiver-a", "child", frozenset({"read", "write", "admin"})))
    def food_for(current, description):
        with current.apollo(permission="write"):
            return food.log_food("2026-06-02", description, {"items": [{"name": "pasto"}], "total_estimated_calories": 500})
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda args: food_for(*args), [(session, "adult meal"), (other, "child meal")]))
    assert apollo.database_path() == baseline
    for current, description in ((session, "adult meal"), (other, "child meal")):
        with current.connection() as conn:
            assert [r[0] for r in conn.execute("SELECT description FROM food_logs")] == [description]
    episode = session.create_case(kind="episode", title="Episodio", request_id="ep", source_ref="synthetic:ep")
    session.link_food(result[0]["id"], case_id=episode["case_id"], source_ref="synthetic:link")
    session.link_food(result[0]["id"], case_id=episode["case_id"], source_ref="synthetic:link")
    assert len(session.report(episode_id=episode["case_id"], end="2026-06-03")["food_entries"]) == 1
    with apollo.bind_database(session.db_path):
        with apollo.bind_database(other.db_path):
            assert apollo.database_path() == other.db_path
        assert apollo.database_path() == session.db_path


def test_v8_migration_retains_legacy_data_and_requires_explicit_adoption(core, tmp_path):
    care, _, _ = core
    path = tmp_path / "legacy.db"
    care.store.initialize(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=8")
        conn.execute("INSERT INTO food_logs VALUES ('legacy-food', '2026-06-01', '2026-06-01T12:00:00Z', 'Synthetic legacy meal', '{}')")
    with pytest.raises(care.AuthorizationError):
        care.CareStore(path, scope_id="legacy")
    bound = care.CareStore(path, scope_id="legacy", adopt_existing=True)
    reader = bound.session(care.Authority("owner", "legacy", frozenset({"read"})))
    with reader.connection() as conn:
        assert conn.execute("SELECT description FROM food_logs WHERE id='legacy-food'").fetchone()[0] == "Synthetic legacy meal"
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
    care.CareStore(path, scope_id="legacy")


def test_concurrent_shared_scope_replay_is_exactly_once(core):
    _, _, session = core
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: append(session), range(8)))
    assert len({r["entry_id"] for r in results}) == 1
    assert len(session.list_entries()) == 1


def test_future_schema_is_not_downgraded(core, tmp_path):
    care, _, _ = core
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (999)")
        conn.execute("CREATE TABLE future_patient_data(value TEXT)")
        conn.execute("INSERT INTO future_patient_data VALUES ('synthetic')")
    with pytest.raises(RuntimeError, match="newer"):
        care.store.initialize(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 999
        assert conn.execute("SELECT value FROM future_patient_data").fetchone()[0] == "synthetic"


def test_blank_initialized_apollo_can_bind_without_adoption(core, tmp_path):
    care, _, _ = core
    path = tmp_path / "blank.db"
    care.store.initialize(path)
    care.CareStore(path, scope_id="blank")


def test_scope_adoption_preserves_v8_canonical_rows_and_projection(core, tmp_path):
    care, _, _ = core
    path = tmp_path / "existing.db"
    care.store.initialize(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=8")
        conn.execute("INSERT INTO observations(observation_id,provider,signal_name,value_number,effective_start,day,source_table,source_row_id) VALUES ('synthetic-observation','test','synthetic',12,'2026-06-01T12:00:00Z','2026-06-01','synthetic','row')")
    with pytest.raises(care.AuthorizationError):
        care.CareStore(path, scope_id="existing")
    care.CareStore(path, scope_id="existing", adopt_existing=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value_number FROM observations WHERE observation_id='synthetic-observation'").fetchone()[0] == 12


def test_versioned_measurements_and_report_revision_sources(core):
    _, _, session = core
    session.define_category("measurement", version=2, observations={"value": {"unit_field": "unit", "allowed_units": ["mmol/L"]}})
    first = session.append_entry(category_id="measurement", request_id="v1", source_ref="synthetic:v1", data={"value": 5.4, "unit": "mmol/L"}, occurrence_start="2026-06-01T12:00:00Z", uncertainty="exact")
    session.append_entry(category_id="measurement", category_version=2, request_id="v2", source_ref="synthetic:v2", data={"value": 6.1, "unit": "mmol/L"}, occurrence_start="2026-06-01T13:00:00Z", uncertainty="exact")
    session.revise_entry(first["entry_id"], expected_revision=1, request_id="source-correct", source_ref="synthetic:correction", changes={"narrative": "Correzione documentata"}, reason="Chiarimento")
    report = session.report(start="2026-06-01", end="2026-06-01")
    assert {r["category_version"] for r in report["measurements"]} == {1, 2}
    assert all(r["count"] == 1 for r in report["measurements"])
    assert [r["source_ref"] for r in report["revision_evidence"][first["entry_id"]]] == ["synthetic:v1", "synthetic:correction"]


def test_latest_entries_are_newest_and_default_order_remains_compatible(core):
    _, _, session = core
    for index in range(35):
        append(session, request_id=f"recent-{index}", occurrence_start=f"2026-06-01T12:{index:02d}:00Z", uncertainty="exact")
    oldest = session.list_entries(limit=30)
    latest = session.list_entries(limit=30, latest=True)
    assert len(oldest) == len(latest) == 30
    assert oldest[0]["request_id"] == "recent-0"
    assert oldest[-1]["request_id"] == "recent-29"
    assert latest[0]["request_id"] == "recent-34"
    assert latest[-1]["request_id"] == "recent-5"


def test_report_evidence_caps_preserve_full_counts_aggregates_gaps(core):
    care, _, session = core
    for index in range(5):
        session.append_entry(category_id="measurement", request_id=f"bounded-{index}",
            source_ref=f"synthetic:{index}", data={"value": index+1, "unit": "mmol/L"},
            occurrence_start=f"2026-06-0{1+index%2}T12:00:00Z", uncertainty="exact")
        append(session, request_id=f"undated-{index}")
    with session.apollo(permission="write"):
        food = importlib.import_module(care.__name__.rsplit(".", 1)[0] + ".food")
        for index in range(5):
            food.log_food("2026-06-01", f"Synthetic meal {index}", {"items": [], "total_estimated_calories": 500})
    report = session.report(start="2026-06-01", end="2026-06-02", max_entries=1)
    assert report["entry_count"] == report["unknown_occurrence_count"] == report["food_entry_count"] == 5
    assert len(report["entries"]) == len(report["unknown_occurrence_entries"]) == len(report["food_entries"]) == 1
    assert report["evidence"]["truncated"] is True
    assert report["evidence"]["omitted"] == {"entries": 4, "unknown_occurrence_entries": 4, "food_entries": 4}
    assert report["measurements"][0]["count"] == 5
    assert report["measurements"][0]["mean"] == pytest.approx(3)
    assert report["counts_by_category"] == {"measurement": 5}
    assert report["gaps"]["days_without_exact_time_entries"] == []
    with pytest.raises(care.CareError):
        session.report(start="2026-06-01", max_entries=1001)


def test_report_caps_revision_evidence_and_explicitly_reports_omissions(core):
    _, _, session = core
    entry = append(session, occurrence_start="2026-06-01T12:00:00Z", uncertainty="exact")
    for revision in range(1, 29):
        session.revise_entry(entry["entry_id"], expected_revision=revision,
            request_id=f"revision-{revision}", source_ref=f"synthetic:revision-{revision}",
            changes={"narrative": f"Nota aggiornata {revision}"}, reason="Correzione sintetica")
    report = session.report(start="2026-06-01", end="2026-06-01")
    assert report["entry_count"] == 1
    assert len(report["revision_evidence"][entry["entry_id"]]) == 25
    assert report["revision_evidence"][entry["entry_id"]][-1]["revision"] == 29
    assert report["evidence"]["omitted_revisions"][entry["entry_id"]] == 4
    assert report["evidence"]["truncated"] is True


@pytest.mark.parametrize("schema", [
    {"type": ["number", "null"]},
    {"type": "string", "maxLength": -1},
    {"type": "array", "maxItems": 1.5},
    {"type": "number", "minimum": 10, "maximum": 1},
])
def test_schema_rejects_unsupported_types_and_invalid_bounds(core, schema):
    care, _, session = core
    with pytest.raises(care.CareError):
        session.define_category("invalid-schema", schema=schema)
