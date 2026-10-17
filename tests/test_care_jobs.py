"""Durable child lifecycle tests over real scoped Apollo databases."""
from __future__ import annotations

from datetime import UTC
import importlib
import sys
import types

import pytest


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def modules():
    package = types.ModuleType("care_jobs_tests")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    runtime = importlib.import_module(package.__name__ + ".care_runtime")
    jobs = importlib.import_module(package.__name__ + ".care_jobs")
    return runtime, jobs


def setup(modules, tmp_path, *, shared=False):
    runtime_module, jobs_module = modules
    bindings = [runtime_module.Binding("waha", "adult-a", "private-a", "adult-a",
        frozenset({"read", "write", "admin"}))]
    if shared:
        bindings.extend(runtime_module.Binding("waha", actor, "child-group", "child",
            frozenset({"read", "write", "admin"})) for actor in ("adult-a", "adult-b"))
    runtime = runtime_module.CareRuntime(tmp_path / "runtime", bindings=lambda: list(bindings),
        llm=object(), clock=lambda: __import__("datetime").datetime(2030, 1, 2, 10, tzinfo=UTC))
    return runtime, jobs_module.CareJobs(runtime), bindings


def origin(modules, runtime, message_id="message-a", text="Registra questa informazione sanitaria.",
           *, actor="adult-a", chat="private-a"):
    value = modules[0].Origin("waha", actor, chat, message_id, "session-" + message_id,
        text, "2030-01-02T10:00:00+00:00")
    runtime.admit(value)
    return value


def start(jobs, request, *, launch="launch-1", delegation="delegation-1", session="child-session-1"):
    job = jobs.admit(request)
    claimed = jobs.claim_launch(request, job["job_id"], launch_id=launch)
    bound = jobs.bind_child(request, job["job_id"], launch,
        child_run_id=delegation, child_session_id=session)
    assert claimed["status"] == "running"
    assert bound["cancel_required"] is False
    return job["job_id"]


def test_admission_is_persisted_idempotent_and_source_is_recoverable(modules, tmp_path):
    runtime, jobs, bindings = setup(modules, tmp_path)
    first = origin(modules, runtime, text="Ho la febbre da ieri.")

    accepted = jobs.admit(first)
    replay = jobs.admit(first)
    assert accepted["status"] == "queued"
    assert accepted["duplicate"] is False
    assert replay["job_id"] == accepted["job_id"]
    assert replay["duplicate"] is True
    assert jobs.source(first, accepted["job_id"]) == first
    assert len(jobs.list(first)) == 1

    # A new Hermes process/runtime can recover the already persisted receipt
    # without re-launching or creating a parallel health database.
    runtime2, jobs2, _ = setup(modules, tmp_path)
    replay_after_restart = jobs2.admit(first)
    assert replay_after_restart["job_id"] == accepted["job_id"]
    assert replay_after_restart["duplicate"] is True
    assert len(list((tmp_path / "runtime").rglob("apollo.sqlite3"))) == 1


def test_native_child_binding_completion_and_completion_replay(modules, tmp_path):
    runtime, jobs, _ = setup(modules, tmp_path)
    request = origin(modules, runtime)
    job_id = start(jobs, request)
    assert jobs.authorize_child(request, job_id, "launch-1",
        child_run_id="delegation-1", child_session_id="child-session-1") is True

    result = jobs.complete(request, job_id, "launch-1", child_run_id="delegation-1",
        outcome="completed", summary="Ho registrato il sintomo riferito.",
        result={"saved_count": 1})
    assert result["published"] is True
    assert result["status"] == "completed"
    assert result["result"] == {"saved_count": 1}
    replay = jobs.complete(request, job_id, "launch-1", child_run_id="delegation-1",
        outcome="completed", summary="Ho registrato il sintomo riferito.", result={"saved_count": 1})
    assert replay["replayed"] is True
    with pytest.raises(PermissionError):
        jobs.authorize_child(request, job_id, "another-launch", child_run_id="delegation-1")


def test_cancellation_prevents_late_completion_and_requires_host_ack(modules, tmp_path):
    runtime, jobs, _ = setup(modules, tmp_path)
    request = origin(modules, runtime)
    job_id = start(jobs, request)

    requested = jobs.cancel(request, job_id)
    assert requested["status"] == "cancel_requested"
    assert requested["cancel_requested"] is True
    with pytest.raises(PermissionError):
        jobs.authorize_child(request, job_id, "launch-1", child_run_id="delegation-1")
    late = jobs.complete(request, job_id, "launch-1", child_run_id="delegation-1",
        outcome="completed", summary="Late child result", result={"should_not_publish": True})
    assert late == {"published": False, "status": "suppressed_cancelled"}

    cancelled = jobs.ack_cancelled(request, job_id, "launch-1", child_run_id="delegation-1")
    assert cancelled["status"] == "cancelled"
    assert jobs.get(request, job_id)["status"] == "cancelled"


def test_queued_cancel_never_claims_or_launches(modules, tmp_path):
    runtime, jobs, _ = setup(modules, tmp_path)
    request = origin(modules, runtime)
    job = jobs.admit(request)
    assert jobs.cancel(request, job["job_id"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        jobs.claim_launch(request, job["job_id"], launch_id="launch-1")


def test_stale_running_child_becomes_unknown_and_is_never_relaunched(modules, tmp_path):
    runtime, jobs, _ = setup(modules, tmp_path)
    request = origin(modules, runtime)
    job_id = start(jobs, request)

    unknown = jobs.interrupt(request, job_id, launch_id="launch-1", reason="host_restarted")
    assert unknown["status"] == "unknown"
    assert "verificare gli effetti" in unknown["summary"]
    assert jobs.interrupt(request, job_id, launch_id="launch-1")["replayed"] is True
    with pytest.raises(ValueError):
        jobs.claim_launch(request, job_id, launch_id="launch-2")


def test_scope_recovery_keeps_registered_children_and_never_relaunches_stale_ones(modules, tmp_path):
    runtime, jobs, bindings = setup(modules, tmp_path)
    request_a = origin(modules, runtime, "message-a")
    job_a = start(jobs, request_a, launch="launch-a", delegation="child-a", session="session-a")
    request_b = origin(modules, runtime, "message-b")
    job_b = start(jobs, request_b, launch="launch-b", delegation="child-b", session="session-b")
    jobs.cancel(request_b, job_b)

    # Host recovery only needs a currently authorized read binding; it is not
    # a clinical-data mutation exposed to the user or model.
    bindings[:] = [modules[0].Binding("waha", "adult-a", "private-a", "adult-a",
        frozenset({"read"}))]

    recovered = jobs.reconcile_scope("adult-a", active_child_ids={"child-a"})
    assert recovered == {"unknown": 0, "cancelled": 1, "retained": 1}
    assert jobs.get(request_a, job_a)["state"] == "running"
    assert jobs.get(request_b, job_b)["state"] == "cancelled"

    stopped = jobs.reconcile_scope("adult-a", active_child_ids=set())
    assert stopped == {"unknown": 1, "cancelled": 0, "retained": 0}
    assert jobs.get(request_a, job_a)["state"] == "unknown"
    with pytest.raises(ValueError):
        jobs.claim_launch(request_a, job_a, launch_id="launch-a-retry")


def test_shared_scope_caregiver_can_read_cancel_but_source_grant_is_rechecked(modules, tmp_path):
    runtime, jobs, bindings = setup(modules, tmp_path, shared=True)
    request_a = origin(modules, runtime, "group-message", actor="adult-a", chat="child-group")
    job_id = start(jobs, request_a)
    request_b = origin(modules, runtime, "cancel-message", text="Annulla il lavoro salute.",
                       actor="adult-b", chat="child-group")

    assert jobs.get(request_b, job_id)["status"] == "running"
    assert jobs.cancel(request_b, job_id)["status"] == "cancel_requested"

    # The current requesting caregiver still has access, but removal of the
    # original sender's exact grant suppresses child completion publication.
    bindings[:] = [binding for binding in bindings if binding.actor_id != "adult-a"]
    result = jobs.complete(request_b, job_id, "launch-1", child_run_id="delegation-1",
        outcome="completed", summary="Questo risultato non deve essere inoltrato.")
    assert result["published"] is False
    assert result["status"] == "suppressed_revoked"


def test_followup_keeps_only_bounded_health_job_messages_and_history(modules, tmp_path):
    runtime, jobs, _ = setup(modules, tmp_path)
    first = origin(modules, runtime, text="Quando è iniziato il sintomo?")
    job = jobs.admit(first)
    launch = jobs.claim_launch(first, job["job_id"], launch_id="launch-1")
    jobs.bind_child(first, job["job_id"], "launch-1", child_run_id="delegation-1")
    waiting = jobs.complete(first, job["job_id"], "launch-1", child_run_id="delegation-1",
        outcome="awaiting_user", summary="Serve un chiarimento.", question="A che ora è iniziato?")
    assert waiting["status"] == "awaiting_user"
    followup = origin(modules, runtime, "followup-message", text="Verso le 08:30, misurato al risveglio.")
    resumed = jobs.continue_job(followup, job["job_id"])
    assert resumed["status"] == "queued"
    assert jobs.continue_job(followup, job["job_id"])["duplicate"] is True
    messages = jobs.input_messages(followup, job["job_id"], max_messages=3, max_bytes=4000)
    assert [message["text"] for message in messages] == [first.text, followup.text]
    history = jobs.history(followup, job["job_id"], limit=3, max_bytes=4000)
    assert len(history) <= 3
    assert any(item["question"] == "A che ora è iniziato?" for item in history)
    assert all("original_message" not in item and "transcript" not in item for item in history)


def test_revoked_requesting_actor_and_scope_spoof_are_denied(modules, tmp_path):
    runtime, jobs, bindings = setup(modules, tmp_path, shared=True)
    request = origin(modules, runtime, "group-message", actor="adult-a", chat="child-group")
    job_id = jobs.admit(request)["job_id"]
    forged = modules[0].Origin("waha", "adult-b", "private-a", "forged", "native-forged",
        "show job", "2030-01-02T10:00:00+00:00")
    with pytest.raises(PermissionError):
        jobs.get(forged, job_id)
    bindings[:] = [binding for binding in bindings if binding.actor_id != "adult-a"]
    authorized_shared_actor = origin(modules, runtime, "shared-read", actor="adult-b", chat="child-group")
    with pytest.raises(PermissionError):
        jobs.get(authorized_shared_actor, job_id)
