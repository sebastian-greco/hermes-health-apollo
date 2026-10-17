"""Native dispatcher domain contract: real Apollo, no paid inference/accounts."""
import importlib
import json
from pathlib import Path
import sys
import types

import pytest


@pytest.fixture
def setup(tmp_path):
    package = types.ModuleType("care_native_dispatch_tests")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    dispatch = importlib.import_module(package.__name__ + ".care_dispatch")
    runtime_mod = importlib.import_module(package.__name__ + ".care_runtime")
    routes = [runtime_mod.Binding("waha", "adult-a", "private-a", "scope-a", frozenset({"read", "write", "admin"})),
              runtime_mod.Binding("waha", "adult-b", "private-b", "scope-b", frozenset({"read", "write", "admin"}))]
    class NoPlanner:
        def complete_structured(self, **kwargs):
            pytest.fail("Native health actions must not call a hidden planner")
    runtime = runtime_mod.CareRuntime(tmp_path, bindings=lambda: routes, llm=NoPlanner())
    bridge = dispatch.HealthDispatcher(object(), runtime)
    class Runner:
        def __init__(self):
            self.calls = []
            self.children = {}
        def dispatch(self, parent, *, session_key, goal, context, run_id):
            child = types.SimpleNamespace(session_id="child-session-" + run_id, _subagent_id="sa-" + run_id,
                interrupted=False)
            child.is_interrupted = lambda: child.interrupted
            self.children[run_id] = child
            self.calls.append(json.loads(context))
            bridge._started(run_id, child)
            return {"status": "dispatched", "delegation_id": "native-" + run_id}
        def cancel(self, run_id, reason):
            if run_id in self.children:
                self.children[run_id].interrupted = True
    runner = Runner()
    bridge._runner = runner
    def origin(message="source-1", text="Nota salute sintetica", actor="adult-a", chat="private-a"):
        source = runtime_mod.Origin("waha", actor, chat, message, "native-parent", text, "2030-01-02T10:00:00Z")
        runtime.admit(source)
        return source
    return bridge, runner, runtime, routes, origin


def call(bridge, child, op="append_entry", action_id="note-1", args=None):
    return json.loads(bridge.action({"action_id": action_id, "op": op,
        "args": {"category_id": "journal", "narrative": "Nota sintetica"} if args is None else args}, parent_agent=child))


def test_native_job_immediate_ack_real_apollo_receipts_and_replay(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    assert ack["outcome"] == "delegated"
    child = runner.children[ack["job_id"]]
    saved = call(bridge, child)
    assert saved["entry_id"]
    assert call(bridge, child) == saved
    assert len(runtime.session(source).list_entries()) == 1
    assert json.loads(bridge.finish({}, parent_agent=child))["status"] == "finished"
    completion = bridge._finished(ack["job_id"], {"status": "completed", "summary": "UNVERIFIED CLAIM"})
    assert "UNVERIFIED" not in completion["summary"]
    assert "Nota sintetica" not in completion["summary"]
    assert "entry_id" not in completion["summary"]
    assert json.loads(completion["summary"])["health_job_id"] == ack["job_id"]
    status = bridge.status(source, ack["job_id"])
    assert status["state"] == "completed"
    assert status["receipts"][0]["result"]["entry_id"] == saved["entry_id"]
    replay = bridge.dispatch(source, "Ripeti", parent_agent=object())
    assert replay["outcome"] == "existing"
    assert len(runner.calls) == 1
    assert call(bridge, child)["status"] == "refused"  # retired child capability
    assert not bridge._launches and not bridge._children and not bridge._cancelled


def test_wrong_child_parent_and_other_patient_cannot_access(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    spoof = types.SimpleNamespace(**child.__dict__)
    assert call(bridge, spoof)["status"] == "refused"
    assert call(bridge, object())["status"] == "refused"
    other = origin("other-1", actor="adult-b", chat="private-b")
    with pytest.raises(ValueError):
        bridge.status(other, ack["job_id"])
    assert runtime.session(source).list_entries() == []


def test_cancel_precedes_native_interrupt_and_suppresses_late_results(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    cancelled = bridge.cancel(source, ack["job_id"])
    assert cancelled["state"] == "cancel_requested"
    assert call(bridge, child)["status"] == "refused"
    result = bridge._finished(ack["job_id"], {"status": "completed", "summary": "late claim"})
    assert result["status"] == "cancelled"
    assert bridge.status(source, ack["job_id"])["state"] == "cancelled"
    assert runtime.session(source).list_entries() == []


def test_revocation_blocks_actions_and_queued_completion_contains_no_clinical_details(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    call(bridge, child)
    json.loads(bridge.finish({"question": "Domanda clinica privata"}, parent_agent=child))
    completion = bridge._finished(ack["job_id"], {"status": "completed"})
    assert completion["status"] == "completed"
    assert "Domanda clinica privata" not in completion["summary"]
    routes.clear()
    with pytest.raises(PermissionError):
        bridge.status(source, ack["job_id"])


def test_uncertain_action_cannot_be_repeated_with_new_id(setup, monkeypatch):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    original_execute = runtime._execute
    def uncertain(*args, **kwargs):
        original_execute(*args, **kwargs)
        raise RuntimeError("synthetic crash after commit")
    monkeypatch.setattr(runtime, "_execute", uncertain)
    assert call(bridge, child)["status"] == "refused"
    monkeypatch.setattr(runtime, "_execute", original_execute)
    assert call(bridge, child, action_id="bypass")["status"] == "refused"
    assert len(runtime.session(source).list_entries()) == 1
    bridge.finish({}, parent_agent=child)
    result = bridge._finished(ack["job_id"], {"status": "completed"})
    assert json.loads(result["summary"])["state"] == "unknown"


def test_next_health_request_has_bounded_previous_health_question_not_parent_context(setup):
    bridge, runner, runtime, routes, origin = setup
    first = origin(text="Nota salute originale sintetica")
    ack = bridge.dispatch(first, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    bridge.finish({"question": "Quale orario?"}, parent_agent=child)
    bridge._finished(ack["job_id"], {"status": "completed"})
    second = origin("source-2", text="Alle nove")
    bridge.dispatch(second, "Risposta", parent_agent=object())
    assert runner.calls[-1]["previous_health_requests"][0]["question"] == "Quale orario?"
    assert runner.calls[-1]["previous_health_requests"][0]["original_message"] == first.text


def test_unload_stops_child_even_after_source_is_revoked(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    routes.clear()
    bridge.close()
    assert runner.children[ack["job_id"]].interrupted


def test_cancellation_racing_completion_cannot_publish_receipts(setup, monkeypatch):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    child = runner.children[ack["job_id"]]
    saved = call(bridge, child)
    bridge.finish({}, parent_agent=child)
    complete = bridge.jobs.complete
    def cancel_then_complete(*args, **kwargs):
        bridge.jobs.cancel(source, ack["job_id"])
        return complete(*args, **kwargs)
    monkeypatch.setattr(bridge.jobs, "complete", cancel_then_complete)
    result = bridge._finished(ack["job_id"], {"status": "completed"})
    assert result["status"] == "error"
    assert saved["entry_id"] not in result["summary"]
    assert bridge.status(source, ack["job_id"])["state"] == "cancelled"


def test_native_stop_closes_job_as_cancelled(setup):
    bridge, runner, runtime, routes, origin = setup
    source = origin()
    ack = bridge.dispatch(source, "Registra", parent_agent=object())
    runner.cancel(ack["job_id"], "Native /stop")
    result = bridge._finished(ack["job_id"], {"status": "interrupted"})
    assert result["status"] == "cancelled"
    assert bridge.status(source, ack["job_id"])["state"] == "cancelled"
