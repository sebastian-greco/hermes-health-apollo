"""No-network proof using the pinned Hermes agent loop, tool dispatch and async lane."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIN = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"


@unittest.skipUnless(os.environ.get("HERMES_SOURCE"), "set HERMES_SOURCE for the pinned native agent test")
class NativeHealthCompatibility(unittest.TestCase):
    def test_real_native_child_loop_async_delivery_identity_and_cancellation(self):
        self._run_script(SCRIPT, '"native_child": "passed"')

    def test_production_registered_tools_execute_in_real_native_child(self):
        self._run_script(PLUGIN_SCRIPT, '"production_native_child": "passed"', plugin=True)

    def _run_script(self, script, expected, *, plugin=False):
        source = Path(os.environ["HERMES_SOURCE"]).resolve()
        git_env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.assertEqual(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                                 env=git_env, text=True).strip(), PIN)
        subprocess.run(["git", "-C", str(source), "diff", "--exit-code", "HEAD"],
                       env=git_env, check=True, capture_output=True)
        with tempfile.TemporaryDirectory(prefix="apollo-native-child-") as isolated:
            shutil.copy2(ROOT / "care_native.py", Path(isolated) / "care_native.py")
            plugin_path = Path(isolated) / "health-data"
            if plugin:
                plugin_path.mkdir()
                for module in ROOT.glob("*.py"):
                    shutil.copy2(module, plugin_path / module.name)
                shutil.copy2(ROOT / "plugin.yaml", plugin_path / "plugin.yaml")
                for package in ("health_data_assets", "skills"):
                    shutil.copytree(ROOT / package, plugin_path / package,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            paths = [str(source)]
            if os.environ.get("HERMES_COMPAT_EXTRA_PATH"):
                paths.append(os.environ["HERMES_COMPAT_EXTRA_PATH"])
            env = {"PATH": os.defpath, "HOME": isolated, "HERMES_HOME": isolated,
                   "XDG_CONFIG_HOME": isolated + "/xdg", "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                   "HERMES_BUNDLED_PLUGINS": isolated + "/empty", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join(paths)}
            result = subprocess.run([sys.executable, "-c", script, str(plugin_path)], env=env, cwd=isolated,
                                    text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(expected, result.stdout)
            print(result.stdout.strip())


SCRIPT = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

def deny_network(*args, **kwargs):
    raise AssertionError("No network is allowed in native compatibility tests")

assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"} & set(os.environ)

with patch.object(socket.socket, "connect", deny_network), \
     patch.object(socket.socket, "connect_ex", deny_network), \
     patch.object(socket, "create_connection", deny_network):
    from care_native import NativeHealthRunner
    from agent.delegation_context import is_delegated_child_context
    from agent.subagent_lifecycle import get_active_subagent_parent
    from gateway.session_context import get_session_env, reset_session_vars, set_session_vars
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools import async_delegation as lane
    from tools.process_registry import process_registry
    from tools.registry import registry
    import model_tools

    Path("config.yaml").write_text(json.dumps({"delegation": {"max_concurrent_children": 1},
                                              "context": {"engine": "compressor"}}))
    # These are decoys: none may appear in any child inference request.
    Path("SOUL.md").write_text("HOUSE_SOUL_PRIVATE_CANARY")
    Path("AGENTS.md").write_text("HOUSE_WORKSPACE_PRIVATE_CANARY")
    Path("MEMORY.md").write_text("HOUSE_MEMORY_PRIVATE_CANARY")
    bound, completed, calls, actions, progress, grants = {}, [], [], [], [], {}
    entered, release = threading.Event(), threading.Event()
    profile = ContextVar("synthetic_health_profile", default="absent")
    profile.set("synthetic-profile")
    tokens = set_session_vars(platform="waha", user_id="synthetic-adult", chat_id="synthetic-private",
                              session_key="synthetic-route", message_id="synthetic-message")

    def action(args):
        child = get_active_subagent_parent()
        run_id = next((key for key, value in bound.items() if value is child), "missing")
        child._care_authorize()
        assert runner.authorize_child(run_id, child=child, session_id=child.session_id,
                                      task_id=child._current_task_id)
        assert is_delegated_child_context()
        assert profile.get() == "synthetic-profile"
        assert get_session_env("HERMES_SESSION_USER_ID") == "synthetic-adult"
        actions.append(args)
        return json.dumps({"receipt": "synthetic-verified-receipt"})

    def register(name, toolset, handler):
        registry.register(name=name, toolset=toolset, schema={"name": name, "description": "Synthetic tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}, handler=handler)

    register("consult_health", "casita_health", lambda args: "dispatcher")
    register("health_action", "casita_health_specialist", action)

    db = SessionDB(db_path=Path("state.db").resolve())
    parent = AIAgent(model="synthetic-model", provider="openai", api_key="synthetic-test-key",
        base_url="https://provider.example.test/v1", enabled_toolsets=["casita_health"],
        disabled_toolsets=[], quiet_mode=True, skip_context_files=True, skip_memory=True,
        session_db=db, platform="waha", prefill_messages=[{"role": "assistant", "content": "HOUSE_PREFILL_PRIVATE_CANARY"}],
        ephemeral_system_prompt="HOUSE_PERSONA_PRIVATE_CANARY", tool_progress_callback=lambda *a, **kw: progress.append((a, kw)))
    parent._ensure_db_session()  # a live parent turn already owns its durable session
    parent._memory_manager = Mock()
    parent.session_estimated_cost_usd = 0.0
    saved_catalog, saved_enabled = set(parent.valid_tool_names), list(parent.enabled_toolsets)
    parent_tools = list(model_tools._last_resolved_tool_names)
    raw_parent = model_tools.get_tool_definitions(enabled_toolsets=["casita_health"],
                                                quiet_mode=True, skip_tool_search_assembly=True)
    assert {tool["function"]["name"] for tool in raw_parent} == {"consult_health"}
    model_tools._last_resolved_tool_names = list(parent_tools)
    assert "health_action" not in saved_catalog

    def on_start(run_id, child):
        assert isinstance(child, AIAgent)
        assert child is not parent
        bound[run_id] = child
        grants[run_id] = True
        def authorize():
            if not grants[run_id] or not runner.authorize_child(run_id, child=child):
                raise PermissionError("Synthetic health authority revoked")
        child._care_authorize = authorize
        child._disable_streaming = True  # deterministic provider is non-streaming
        assert child.prefill_messages == []
        assert child.enabled_toolsets == ["casita_health_specialist"]
        assert child.valid_tool_names == {"health_action"}
        assert child._delegate_role == "leaf"
        assert child._memory_manager is None
        assert child._session_db is not parent._session_db
        assert child._session_db.db_path == parent._session_db.db_path
        assert child._delegate_parent_ref() is parent
        assert child.model == parent.model and child.provider == parent.provider
        assert child.api_key == parent.api_key and child.base_url == parent.base_url

    def on_finish(run_id, raw):
        completed.append((run_id, raw))
        return {"summary": "Verified health receipts: " + str(len(actions)) + "; job " + run_id,
                "status": raw.get("status", "error")}

    runner = NativeHealthRunner(on_start=on_start, on_finish=on_finish,
        instructions="OWN_HEALTH_PERSONA. Use the health tool once and then finish.")

    def response(content=None, tool=False):
        msg = NS(role="assistant", content=content, tool_calls=([NS(id="synthetic-call", type="function",
            function=NS(name="health_action", arguments="{}"))] if tool else None))
        return NS(id="synthetic-response", model="synthetic-model", choices=[NS(message=msg,
            finish_reason="tool_calls" if tool else "stop")], usage=NS(prompt_tokens=40, completion_tokens=5, total_tokens=45))

    def provider(child, kwargs, **unused):
        assert isinstance(child, AIAgent) and child is not parent
        assert get_active_subagent_parent() is child
        assert is_delegated_child_context() and profile.get() == "synthetic-profile"
        calls.append(kwargs)
        wire = json.dumps(kwargs)
        assert "OWN_HEALTH_PERSONA" in wire and "PRIVATE_PATIENT_CONTEXT_CANARY" in wire
        assert not any(marker in wire for marker in ("HOUSE_SOUL", "HOUSE_WORKSPACE", "HOUSE_MEMORY", "HOUSE_PREFILL", "HOUSE_PERSONA"))
        assert {tool["function"]["name"] for tool in kwargs["tools"]} == {"health_action"}
        entered.set()
        assert release.wait(10), "Test release was not delivered"
        tool_done = any(m.get("role") == "tool" for m in kwargs["messages"])
        if tool_done:
            child.session_estimated_cost_usd = 0.125
        run_id = next(key for key, value in bound.items() if value is child)
        if run_id == "synthetic-job-revocation":
            grants[run_id] = False
        return response("RAW_UNVERIFIED_CLINICAL_CLAIM_CANARY" if tool_done else None, tool=not tool_done)

    def dispatch(run_id):
        return runner.dispatch(parent, session_key="synthetic-route", goal="Record a synthetic observation",
                               context="PRIVATE_PATIENT_CONTEXT_CANARY", run_id=run_id)

    def terminal(handle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = lane.get_durable_delegation(handle["delegation_id"])
            if row and row["state"] not in {"running", "finalizing"}:
                return row
            time.sleep(0.01)
        raise AssertionError("Native completion did not settle")

    with patch("agent.chat_completion_helpers._dispatch_nonstreaming_api_request", provider):
        handle = dispatch("synthetic-job-one")
        assert handle["status"] == "dispatched", handle
        assert entered.wait(5)
        # dispatch returned while the real child model call is still blocked.
        assert lane.get_durable_delegation(handle["delegation_id"])["state"] == "running"
        child = bound["synthetic-job-one"]
        assert child not in parent._active_children  # background ownership transferred
        assert runner.authorize_child("synthetic-job-one", child=child)
        assert not runner.authorize_child("synthetic-job-one", child=parent)
        assert not runner.authorize_child("synthetic-job-one", session_id=child.session_id)
        assert not runner.authorize_child("synthetic-job-one", child=child, session_id=parent.session_id)
        assert not runner.authorize_child("synthetic-job-one", child=child, task_id="forged-task")
        assert dispatch("synthetic-job-one") == handle
        rejected = dispatch("synthetic-job-capacity")
        assert rejected["status"] == "rejected", rejected
        assert not runner.authorize_child("synthetic-job-capacity", child=bound["synthetic-job-capacity"])
        assert len(calls) == 1 and not actions  # rejection never entered a model/tool loop
        assert set(parent.valid_tool_names) == saved_catalog and parent.enabled_toolsets == saved_enabled
        assert model_tools._last_resolved_tool_names == parent_tools
        release.set()
        row = terminal(handle)
        assert row["state"] == "completed", row
        assert actions == [{}], (actions, completed)
        assert row["result"]["summary"] == "Verified health receipts: 1; job synthetic-job-one"
        assert parent.session_estimated_cost_usd == 0.125, parent.session_estimated_cost_usd
        parent._memory_manager.on_delegation.assert_not_called()
        assert not runner.authorize_child("synthetic-job-one", child=child)
        assert runner.status("synthetic-job-one")["status"] == "completed"
        assert runner._runs["synthetic-job-one"].child is None
        event = process_registry.completion_queue.get(timeout=5)
        assert event["type"] == "async_delegation" and event["delegation_id"] == handle["delegation_id"]
        assert event["session_key"] == "synthetic-route" and event["parent_session_id"] == parent.session_id
        assert event["context"] is None and event["goal"] == "Health request synthetic-job-one"
        assert "RAW_UNVERIFIED" not in json.dumps(event) and "PRIVATE_PATIENT" not in json.dumps(event)
        assert "RAW_UNVERIFIED" not in json.dumps(progress) and "PRIVATE_PATIENT" not in json.dumps(progress)
        assert completed[0][1]["summary"] == "RAW_UNVERIFIED_CLINICAL_CLAIM_CANARY"
        traced = db.get_session(child.session_id)
        assert traced["parent_session_id"] == parent.session_id, traced
        transcript = db.get_messages(child.session_id)
        assert any(m.get("role") == "tool" for m in transcript), transcript
        assert "HOUSE_" not in json.dumps(transcript)
        # Native session cancellation invokes the runner's revocation callback.
        entered.clear(); release.clear()
        cancelled = dispatch("synthetic-job-cancel")
        assert entered.wait(5)
        cancel_child = bound["synthetic-job-cancel"]
        assert lane.interrupt_for_session(session_key="synthetic-route", reason="synthetic test") == 1
        assert not runner.authorize_child("synthetic-job-cancel", child=cancel_child)
        release.set()
        cancelled_row = terminal(cancelled)
        assert cancelled_row["state"] == "interrupted", cancelled_row
        assert len(actions) == 1  # interrupted model result cannot execute a health action
        assert not runner.cancel("synthetic-job-one")
        before_revocation = len(calls)
        revoked = dispatch("synthetic-job-revocation")
        revoked_row = terminal(revoked)
        assert revoked_row["state"] == "interrupted", revoked_row
        assert len(calls) == before_revocation + 1, "Revocation must prevent the next provider request"
        assert len(actions) == 1

    with patch("tools.delegate_tool._lease_child_credential", side_effect=RuntimeError("PRIVATE_PROVIDER_FAILURE_CANARY")):
        broken = dispatch("synthetic-job-lease-failure")
        failed = terminal(broken)
        assert failed["state"] == "error", failed
        assert "PRIVATE_PROVIDER" not in json.dumps(failed)
        assert completed[-1][0] == "synthetic-job-lease-failure"
        assert runner._runs["synthetic-job-lease-failure"].child is None
        assert not runner.authorize_child("synthetic-job-lease-failure", child=bound["synthetic-job-lease-failure"])

    # Unsupported host workspace/context modes fail closed before any callback/model call.
    with patch("tools.delegate_tool._get_worktree_isolation", return_value=True):
        refused = dispatch("synthetic-job-worktree")
        assert refused["status"] == "rejected" and "synthetic-job-worktree" not in bound
    with patch("hermes_cli.config.load_config_readonly", return_value={"context": {"engine": "synthetic-custom"}}):
        refused = dispatch("synthetic-job-context-engine")
        assert refused["status"] == "rejected" and "synthetic-job-context-engine" not in bound
    assert set(parent.valid_tool_names) == saved_catalog and parent.enabled_toolsets == saved_enabled
    parent._memory_manager = None
    parent.close(); db.close()
    reset_session_vars()
    lane._reset_for_tests()
    print(json.dumps({"native_child": "passed", "real_child_tool_calls": len(actions),
                      "durable_completion": "passed", "native_cancellation": "passed"}))
'''


PLUGIN_SCRIPT = r'''
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

def deny_network(*args, **kwargs):
    raise AssertionError("No network is allowed in the production native fixture")

with patch.object(socket.socket, "connect", deny_network), \
     patch.object(socket.socket, "connect_ex", deny_network), \
     patch.object(socket, "create_connection", deny_network):
    from agent.delegation_context import is_delegated_child_context
    from agent.subagent_lifecycle import bind_subagent_parent, get_active_subagent_parent
    from gateway import session_context as sc
    from hermes_cli import config
    from hermes_cli.plugin_dev import _doctor_runtime
    from hermes_cli.plugins import PluginManager
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools import async_delegation as lane
    from tools.process_registry import process_registry
    from tools.registry import registry

    configuration = {"tools": {"tool_search": {"enabled": "off"}}, "context": {"engine": "compressor"},
        "plugins": {"entries": {"health-data": {"settings": {"care": {"enabled": True,
        "bindings": [{"platform": "waha", "actor_id": "synthetic-adult", "conversation_id": "synthetic-private",
        "scope_id": "synthetic-scope", "permissions": ["read", "write", "admin"]}]}}}}}}
    load_plugin = PluginManager._load_plugin
    def configured_load(manager, manifest):
        config.get_config_path().write_text(json.dumps(configuration))
        return load_plugin(manager, manifest)

    with patch.object(PluginManager, "_load_plugin", configured_load), _doctor_runtime(Path(sys.argv[1])) as sdk:
        module = sdk.manager._plugins["health-data"].module
        runtime = module.care_plugin.active_runtime
        dispatcher = module.care_plugin.active_dispatcher
        assert registry.get_entry("health_append_entry").toolset == "casita_health_specialist"
        assert registry.get_entry("consult_health").toolset == "casita_health"
        entered, release = threading.Event(), threading.Event()
        calls, parent_calls, children, action_errors = [], [], {}, []
        execute = runtime._execute
        def observed_execute(*args, **kwargs):
            try:
                return execute(*args, **kwargs)
            except Exception as exc:
                action_errors.append((type(exc).__name__, str(exc)))
                raise
        runtime._execute = observed_execute
        original_start = dispatcher.runner.on_start
        def started(job_id, child):
            original_start(job_id, child)  # real durable job/source/child binding
            child._disable_streaming = True
            children[job_id] = child
        dispatcher.runner.on_start = started

        admitted_text = "Registra nota sintetica del 2 gennaio 2030 alle 10 UTC e prepara un riepilogo. PRIVATE_ORIGINAL_MESSAGE_CANARY"
        asyncio.run(sdk.manager.ainvoke_hook("post_gateway_admission", platform="waha",
            session_key="synthetic-route", source={"user_id": "synthetic-adult", "chat_id": "synthetic-private"},
            message_id="synthetic-message", text=admitted_text))
        origin = runtime.admitted(platform="waha", actor_id="synthetic-adult", conversation_id="synthetic-private",
            message_id="synthetic-message", session_key="synthetic-route")
        tokens = sc.set_session_vars(platform="waha", user_id="synthetic-adult", chat_id="synthetic-private",
                                    session_key="synthetic-route", message_id="synthetic-message")
        db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "state.db")
        parent = AIAgent(model="synthetic-model", provider="openai", api_key="synthetic-test-key",
            base_url="https://provider.example.test/v1", session_db=db, platform="waha", quiet_mode=True,
            enabled_toolsets=["casita_health"], skip_context_files=True, skip_memory=True,
            ephemeral_system_prompt="HOUSE_GENERAL_PERSONA_CANARY",
            prefill_messages=[{"role": "assistant", "content": "HOUSE_PREFILL_CANARY"}])
        parent._disable_streaming = True
        assert parent.valid_tool_names == {"consult_health"}, parent.valid_tool_names

        def response(name=None, args=None, text=None):
            tool_calls = [NS(id="synthetic-call-" + name, type="function",
                             function=NS(name=name, arguments=json.dumps(args or {})))] if name else None
            return NS(id="synthetic-response", model="synthetic-model", choices=[NS(
                message=NS(role="assistant", content=text, tool_calls=tool_calls),
                finish_reason="tool_calls" if name else "stop")],
                usage=NS(prompt_tokens=80, completion_tokens=10, total_tokens=90))

        steps = [
            ("health_append_entry", {"action_id": "entry", "args": {"category_id": "journal",
                "narrative": "Nota sintetica autorizzata.", "occurrence_start": "2030-01-02T10:00:00Z", "uncertainty": "exact"}}),
            ("health_report", {"action_id": "report", "args": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-03T00:00:00Z"}}),
            ("health_finish", {}),
        ]

        def provider(agent, kwargs, **unused):
            assert get_active_subagent_parent() is agent
            if agent is parent:
                assert not is_delegated_child_context()
                parent_calls.append(kwargs)
                assert {t["function"]["name"] for t in kwargs["tools"]} == {"consult_health"}
                if len(parent_calls) == 1:
                    return response("consult_health", {"request": "Registra e riepiloga"})
                return response(text="Lo specialista salute sta lavorando.")
            assert isinstance(agent, AIAgent) and is_delegated_child_context()
            assert agent in children.values()
            wire = json.dumps(kwargs)
            assert "PRIVATE_ORIGINAL_MESSAGE_CANARY" in wire
            assert "HOUSE_GENERAL_PERSONA" not in wire and "HOUSE_PREFILL" not in wire
            catalog = {t["function"]["name"] for t in kwargs["tools"]}
            assert "health_append_entry" in catalog and "health_finish" in catalog
            assert all(name.startswith("health_") for name in catalog)
            assert not {"consult_health", "terminal", "delegate_task", "memory", "send_message"} & catalog
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                assert release.wait(15)
            if steps:
                name, args = steps.pop(0)
                return response(name, args)
            return response(text="RAW_UNVERIFIED_FINAL_CLAIM_CANARY")

        with patch("agent.chat_completion_helpers._dispatch_nonstreaming_api_request", provider):
            # Both the parent consult and child actions use the real native turn
            # loop and model_tools -> registry plugin handler dispatch.
            parent_result = parent.run_conversation(user_message=admitted_text, task_id="synthetic-parent-task")
            assert parent_result.get("completed"), parent_result
            assert entered.wait(5)
            assert len(parent_calls) == 2 and len(calls) == 1
            jobs = dispatcher.jobs.list(origin)
            assert len(jobs) == 1 and jobs[0]["state"] == "running", jobs
            job_id = jobs[0]["job_id"]
            child = children[job_id]
            assert child not in parent._active_children
            native_handle = dispatcher.runner._runs[job_id].handle
            assert native_handle["status"] == "dispatched"
            assert lane.get_durable_delegation(native_handle["delegation_id"])["state"] == "running"
            # Catalog filtering is supplemented by the actual handler's exact
            # host-object check even when registry.dispatch bypasses schemas.
            illegal = {"action_id": "forged", "args": {"category_id": "journal", "narrative": "Must not save"}}
            with bind_subagent_parent(parent):
                assert json.loads(registry.dispatch("health_append_entry", illegal))["status"] == "refused"
            spoof = NS(session_id=child.session_id, _subagent_id=child._subagent_id)
            with bind_subagent_parent(spoof):
                assert json.loads(registry.dispatch("health_append_entry", illegal))["status"] == "refused"
            release.set()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                native = lane.get_durable_delegation(native_handle["delegation_id"])
                if native and native["state"] not in {"running", "finalizing"}:
                    break
                time.sleep(0.01)
            assert native["state"] == "completed", (native, dispatcher.status(origin, job_id), action_errors,
                [m for m in calls[-1]["messages"] if m.get("role") == "tool"])
            assert not steps and len(calls) == 4
            with bind_subagent_parent(parent):
                status = json.loads(registry.dispatch("consult_health", {"request": "Esito", "action": "status", "job_id": job_id}))
            assert status["state"] == "completed", status
            assert [row["operation"] for row in status["receipts"]] == ["append_entry", "report"], status
            assert status["receipts"][1]["result"]["entry_count"] == 1, status
            assert status["question"] is None
            assert "RAW_UNVERIFIED" not in json.dumps(native)
            assert "PRIVATE_ORIGINAL" not in json.dumps(native)
            event = process_registry.completion_queue.get(timeout=5)
            assert event["type"] == "async_delegation" and event["session_key"] == "synthetic-route"
            assert event["context"] is None
            wakeup = json.loads(event["summary"])
            assert wakeup["health_job_id"] == job_id and wakeup["state"] == "completed"
            assert dispatcher._children == {} and dispatcher._launches == {}
            session = runtime.session(origin)
            with sqlite3.connect(session.db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM events WHERE provider='care_journal'").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM care_job_actions WHERE state='done'").fetchone()[0] == 2
            child_trace = db.get_session(child.session_id)
            assert child_trace["parent_session_id"] == parent.session_id
            assert child_trace["source"] == "subagent"
        parent.close(); db.close()
        sc.clear_session_vars(tokens)
        lane._reset_for_tests()
        print(json.dumps({"production_native_child": "passed", "actual_parent_consult": "passed",
                          "actual_child_tool_actions": 3, "apollo_projection": "passed"}))
'''
