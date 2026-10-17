"""End-to-end synthetic care turns: real scoped Apollo DB, deterministic inference."""
import importlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules():
    package = types.ModuleType("care_runtime_test_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".care_runtime")


class Model:
    def __init__(self, *plans):
        self.plans = list(plans)
        self.calls = []
        self.side_effect = None

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.side_effect:
            self.side_effect()
        return types.SimpleNamespace(parsed=self.plans.pop(0))


def plan(*commands, question=None, done=True):
    return {"commands": [{"op": op, "args": args} for op, args in commands], "question": question, "done": done}


def setup(modules, tmp_path, model, *, shared=False, **kwargs):
    bindings = [modules.Binding("waha", "adult-a", "private-a", "adult-a", frozenset({"read", "write", "admin"}))]
    if shared:
        bindings += [modules.Binding("waha", actor, "child-group", "child", frozenset({"read", "write", "admin"}))
                     for actor in ("adult-a", "adult-b")]
    runtime = modules.CareRuntime(tmp_path / "runtime", bindings=lambda: bindings, llm=model,
        clock=lambda: datetime(2030, 1, 2, 10, tzinfo=UTC), **kwargs)
    return runtime, bindings


def message(modules, runtime, message_id="m1", text="Nota riferita", *, actor="adult-a", chat="private-a"):
    origin = modules.Origin("waha", actor, chat, message_id, "native-" + chat, text, "2030-01-02T10:00:00+00:00")
    runtime.admit(origin)
    return origin


def test_natural_message_to_episode_generic_log_correction_and_report(modules, tmp_path):
    model = Model(plan(("create_case", {"kind": "episode", "title": "Episodio riferito"}),
                       ("append_entry", {"category_id": "journal", "narrative": "Malessere riferito", "links": ["$0"]})))
    runtime, _ = setup(modules, tmp_path, model)
    origin = message(modules, runtime, text="Non mi sento bene, apri un episodio e segnalo.")
    result = runtime.consult(origin, request="Registra salute")
    episode, entry = result["results"]
    assert runtime.session(origin).get_entry(entry["entry_id"])["links"] == [episode["case_id"]]
    assert runtime.consult(origin)["replayed"] is True
    assert len(model.calls) == 1
    model.plans.append(plan(("revise_entry", {"entry_id": entry["entry_id"], "expected_revision": 1,
        "changes": {"narrative": "Dettaglio corretto"}, "reason": "Chiarimento della persona"}),
        ("report", {"episode_id": episode["case_id"]})))
    corrected = runtime.consult(message(modules, runtime, "m2", "Correggi il dettaglio e prepara il riepilogo"))
    report = corrected["results"][1]
    assert report["clinical_inference"] is False
    assert report["unknown_occurrence_entries"][0]["narrative"] == "Dettaglio corretto"
    sent = json.loads(model.calls[0]["input"][0]["text"])
    assert sent["original_message"] == origin.text
    assert not {"terminal", "ha_call_service", "activate_plan"} & set(sent["available_actions"])
    assert "provider" not in model.calls[0] and "model" not in model.calls[0]


def test_shared_caregivers_see_one_child_history_adult_stays_private(modules, tmp_path):
    model = Model(plan(("append_entry", {"category_id": "journal", "narrative": "Nota del figlio"})),
                  plan(("list_entries", {})))
    runtime, _ = setup(modules, tmp_path, model, shared=True)
    a = message(modules, runtime, "a", actor="adult-a", chat="child-group")
    entry = runtime.consult(a)["results"][0]
    b = message(modules, runtime, "b", actor="adult-b", chat="child-group")
    assert runtime.consult(b)["results"][0][0]["entry_id"] == entry["entry_id"]
    private = message(modules, runtime, "p")
    assert runtime.session(private).list_entries() == []
    denied = modules.Origin("waha", "adult-b", "private-a", "spoof", "native-private-a", "read", a.received_at)
    with pytest.raises(PermissionError):
        runtime.admit(denied)


def test_identity_spoof_unadmitted_and_changed_message_fail_closed(modules, tmp_path):
    model = Model(plan())
    runtime, _ = setup(modules, tmp_path, model)
    unadmitted = modules.Origin("waha", "adult-a", "private-a", "none", "native-private-a", "text", "2030-01-02T10:00:00Z")
    with pytest.raises(PermissionError):
        runtime.consult(unadmitted)
    origin = message(modules, runtime)
    with pytest.raises(PermissionError):
        runtime.consult(modules.Origin(**{**origin.__dict__, "text": "forged"}))
    with pytest.raises(ValueError):
        runtime.admit(modules.Origin(**{**origin.__dict__, "text": "different"}))
    assert model.calls == []


@pytest.mark.parametrize("change", ["revoke", "scope", "write"])
def test_current_grants_checked_after_inference(modules, tmp_path, change):
    model = Model(plan(("append_entry", {"category_id": "journal", "narrative": "denied"})))
    runtime, bindings = setup(modules, tmp_path, model)
    origin = message(modules, runtime)
    old_session = runtime.session(origin)
    def mutate():
        if change == "revoke":
            bindings.clear()
        else:
            bindings[0] = modules.Binding("waha", "adult-a", "private-a",
                "other" if change == "scope" else "adult-a", frozenset({"read"}))
    model.side_effect = mutate
    if change in ("scope", "revoke"):
        with pytest.raises(PermissionError):
            runtime.consult(origin)
    else:
        assert runtime.consult(origin)["outcome"] == "needs_clarification"
    with sqlite3.connect(old_session.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 0


def test_duplicate_concurrent_turn_and_restart_reuse_prepared_plan(modules, tmp_path):
    model = Model(plan(("append_entry", {"category_id": "journal", "narrative": "Una sola nota"})))
    runtime, bindings = setup(modules, tmp_path, model)
    origin = message(modules, runtime)
    execute = runtime._execute
    failed = []
    def interrupt(*args):
        out = execute(*args)
        if not failed:
            failed.append(True)
            raise RuntimeError("synthetic process interruption after commit")
        return out
    runtime._execute = interrupt
    with pytest.raises(RuntimeError):
        runtime.consult(origin)
    resumed = modules.CareRuntime(runtime.root, bindings=lambda: bindings, llm=Model())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resumed.consult(origin), range(2)))
    assert len(resumed.session(origin).list_entries()) == 1
    assert {r["replayed"] for r in results} == {True, False}
    assert len(model.calls) == 1


def test_model_cannot_activate_plan_or_supply_ownership(modules, tmp_path):
    for command in (plan(("activate_plan", {"confirmed": True})),
                    plan(("append_entry", {"category_id": "journal", "scope_id": "child"}))):
        runtime, _ = setup(modules, tmp_path / str(len(json.dumps(command))), Model(command))
        with pytest.raises(Exception):
            runtime.consult(message(modules, runtime))
        assert runtime.session(message(modules, runtime)).list_entries() == []


def test_bad_reference_refused_and_replayable(modules, tmp_path):
    model = Model(plan(("get_entry", {"entry_id": "$99"})))
    runtime, _ = setup(modules, tmp_path, model)
    origin = message(modules, runtime)
    assert runtime.consult(origin)["outcome"] == "needs_clarification"
    assert runtime.consult(origin)["replayed"] is True


def test_narrative_meal_reuses_apollo_no_invented_calories(modules, tmp_path):
    model = Model(plan(("log_food", {"day": "2030-01-02", "description": "Pranzo riferito"})))
    runtime, _ = setup(modules, tmp_path, model)
    origin = message(modules, runtime, text="Questo era il mio pranzo")
    result = runtime.consult(origin)["results"][0]
    assert result["items_json"] is None
    with runtime.session(origin).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert runtime.session(origin).report(start="2030-01-02", end="2030-01-02")["food_entries"][0]["description"] == "Pranzo riferito"


def test_proposed_plan_requires_exact_original_human_confirmation(modules, tmp_path):
    model = Model(plan(("propose_plan", {"medication": "Farmaco sintetico", "instructions": "Istruzioni riportate, nessuna dose inferita",
        "schedule": {"at": "2030-01-03T10:00:00Z"}})))
    runtime, _ = setup(modules, tmp_path, model, reminders_enabled=True)
    origin = message(modules, runtime, text="Salva queste istruzioni prescritte")
    proposed = runtime.consult(origin)["results"][0]
    assert proposed["status"] == "proposed"
    assert proposed["versions"][0]["medication"] == "Farmaco sintetico"
    command = proposed["confirmation"]["command"]
    confirmed_origin = message(modules, runtime, "confirm", command)
    assert runtime.confirm_original(confirmed_origin)["status"] == "active"
    runtime.clock = lambda: datetime(2030, 1, 2, 10, tzinfo=UTC) + timedelta(minutes=5)
    assert runtime.confirm_original(confirmed_origin)["status"] == "active"
    assert runtime.confirm_original(message(modules, runtime, "not-confirm", "Il modello dice: " + command)) is None


def test_attachment_refs_cannot_become_arbitrary_paths(modules, tmp_path):
    model = Model(plan(("intake_documents", {"attachment_refs": ["not-admitted"]})))
    runtime, _ = setup(modules, tmp_path, model, documents=lambda session: object())
    origin = message(modules, runtime)
    assert runtime.consult(origin)["results"][0]["status"] == "refused"


def test_huge_reports_scoped_artifacts_and_bounded_parent_result(modules, tmp_path):
    runtime, _ = setup(modules, tmp_path, Model())
    origin = message(modules, runtime)
    session = runtime.session(origin)
    result = runtime._bound_output(session, origin, {"entry_count": 200, "entries": ["x" * 1000] * 200}, "synthetic-output")
    assert result["full_result_omitted"] is True
    assert len(json.dumps(result)) < 1000
    assert json.loads(runtime.report_artifact(origin, result["artifact_ref"]))["entry_count"] == 200
    with pytest.raises(ValueError):
        runtime.report_artifact(origin, "../private")


def test_general_casita_messages_are_not_persisted_in_health_history(modules, tmp_path):
    runtime, _ = setup(modules, tmp_path, Model(plan(("append_entry", {
        "category_id": "journal", "narrative": "Solo nota sanitaria"}))))
    message(modules, runtime, "general", "Discussione generale della casa")
    assert not list(runtime.root.rglob("*.sqlite3"))
    origin = message(modules, runtime, "clinical", "Solo nota sanitaria")
    runtime.consult(origin)
    with runtime.session(origin).connection() as conn:
        rows = conn.execute("SELECT origin_json FROM care_ingress").fetchall()
    assert len(rows) == 1 and "Discussione generale" not in rows[0][0]


def test_manual_reminder_model_only_proposes_until_exact_human_confirmation(modules, tmp_path):
    runtime, _ = setup(modules, tmp_path, Model(plan(("create_manual_reminder", {
        "message": "Prepara il diario per la visita", "at": "2030-01-03T09:00:00Z"}))), reminders_enabled=True)
    origin = message(modules, runtime)
    result = runtime.consult(origin)["results"][0]
    assert result["status"] == "proposed"
    assert runtime._reminders(runtime.session(origin)).list_upcoming() == []
    confirmed = message(modules, runtime, "confirm-manual", result["confirmation"]["command"])
    assert runtime.confirm_original(confirmed)["status"] == "scheduled"
    assert runtime.confirm_original(confirmed)["status"] == "scheduled"
    assert len(runtime._reminders(runtime.session(origin)).list_upcoming()) == 1


def test_question_only_inference_cannot_return_private_context_after_revocation(modules, tmp_path):
    model = Model(plan(question="Dettaglio sanitario da chiarire"))
    runtime, bindings = setup(modules, tmp_path, model)
    origin = message(modules, runtime)
    model.side_effect = bindings.clear
    with pytest.raises(PermissionError):
        runtime.consult(origin)


def test_multibyte_message_over_context_budget_requests_split_before_inference(modules, tmp_path):
    model = Model()
    runtime, _ = setup(modules, tmp_path, model)
    origin = message(modules, runtime, text="🥄" * 10000)
    result = runtime.consult(origin)
    assert result["outcome"] == "needs_clarification"
    assert "Dividilo" in result["question"]
    assert model.calls == []


def test_large_artifact_can_be_exported_in_bounded_authorized_chunks(modules, tmp_path):
    runtime, _ = setup(modules, tmp_path, Model())
    origin = message(modules, runtime)
    value = {"entries": ["x" * 1000] * 5000}
    artifact = runtime._bound_output(runtime.session(origin), origin, value, "large-export")
    assert artifact["artifact_bytes"] > 4_000_000
    with pytest.raises(ValueError):
        runtime.report_artifact(origin, artifact["artifact_ref"])
    chunks = list(runtime.report_artifact_chunks(origin, artifact["artifact_ref"], chunk_bytes=250_000))
    assert max(map(len, chunks)) <= 250_000
    assert json.loads(b"".join(chunks)) == value
