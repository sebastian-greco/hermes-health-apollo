"""Opt-in real pinned Hermes SDK integration; inference is synthetic, network denied."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PIN = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("HERMES_SOURCE"), "set HERMES_SOURCE to the clean pinned Hermes checkout")
class PinnedHermesCare(unittest.TestCase):
    def test_real_sdk_admission_registry_context_and_scoped_apollo_workflow(self):
        source = Path(os.environ["HERMES_SOURCE"]).resolve()
        git_env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, env=git_env).strip()
        self.assertEqual(sha, PIN)
        subprocess.run(["git", "-C", str(source), "diff", "--exit-code", "HEAD"],
                       env=git_env, check=True, capture_output=True)
        with tempfile.TemporaryDirectory(prefix="apollo-care-hermes-compat-") as isolated:
            plugin = Path(isolated) / "health-data"
            plugin.mkdir()
            # Stage distributable flat modules and static assets, never Git,
            # compatibility checkouts, virtualenvs, patient data or local config.
            for module in ROOT.glob("*.py"):
                shutil.copy2(module, plugin / module.name)
            shutil.copy2(ROOT / "plugin.yaml", plugin / "plugin.yaml")
            for package in ("health_data_assets", "skills"):
                shutil.copytree(ROOT / package, plugin / package,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            # Only explicitly selected dependency paths are carried into the
            # child. Provider credentials and the caller's HOME are absent.
            python_paths = [str(source)]
            if os.environ.get("HERMES_COMPAT_EXTRA_PATH"):
                python_paths.append(os.environ["HERMES_COMPAT_EXTRA_PATH"])
            env = {"PATH": os.defpath, "HOME": isolated, "XDG_CONFIG_HOME": isolated + "/xdg",
                   "HERMES_HOME": isolated, "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                   "HERMES_BUNDLED_PLUGINS": isolated + "/empty", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join(python_paths)}
            completed = subprocess.run([sys.executable, "-c", SCRIPT, str(plugin), PIN],
                env=env, cwd=isolated, capture_output=True, text=True, timeout=90)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn('"native_admission": "passed"', completed.stdout)
            print(completed.stdout.strip())


SCRIPT = r'''
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

# No provider or account credentials are inherited by this process.
assert "OPENAI_API_KEY" not in os.environ
assert "ANTHROPIC_API_KEY" not in os.environ
assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ

def deny_network(*args, **kwargs):
    raise AssertionError("Network is forbidden in synthetic SDK compatibility test")

def plan(*commands, done=True):
    return {"commands": [{"op": op, "args": args} for op, args in commands],
            "question": None, "done": done}

responses = [
    plan(("create_case", {"kind": "issue", "title": "Questione sintetica"}),
         ("create_case", {"kind": "episode", "title": "Episodio sintetico", "parent_id": "$0"}),
         ("append_entry", {"category_id": "journal", "narrative": "Osservazione riferita sintetica.",
             "occurrence_start": "2030-01-02T10:00:00Z", "uncertainty": "exact", "links": ["$1"]}),
         ("log_food", {"day": "2030-01-02", "description": "Pasto riferito sintetico.", "case_id": "$1"}),
         done=False),
    plan(("get_entry", {"entry_id": "$2"}),
         ("revise_entry", {"entry_id": "$2", "expected_revision": 1,
             "changes": {"narrative": "Osservazione corretta sintetica."}, "reason": "Chiarimento sintetico"}),
         ("report", {"episode_id": "$1", "end": "2030-01-03"})),
]
calls, facades = [], []
revoke_after_inference = False

def deterministic_provider(**kwargs):
    calls.append(kwargs)
    assert responses, "Unexpected inference call (denial/replay should not infer)"
    response = responses.pop(0)
    if revoke_after_inference:
        configuration["plugins"]["entries"]["health-data"]["settings"]["care"]["bindings"][0]["permissions"] = ["read"]
        config.get_config_path().write_text(json.dumps(configuration))
    return ("synthetic-provider", "synthetic-model", SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(response)))]))

with patch.object(socket.socket, "connect", deny_network), \
     patch.object(socket.socket, "connect_ex", deny_network), \
     patch.object(socket, "create_connection", deny_network):
    from agent.plugin_llm import PluginLlm
    from agent.delegation_context import delegated_child_context
    from gateway import session_context as sc
    from hermes_cli import config
    from hermes_cli.plugin_dev import _doctor_runtime
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    original_load = PluginManager._load_plugin
    original_llm_init = PluginLlm.__init__
    configuration = {"plugins": {"entries": {"health-data": {"settings": {"care": {
        "enabled": True, "bindings": [{"platform": "waha", "actor_id": "synthetic-adult",
        "conversation_id": "synthetic-private", "scope_id": "synthetic-scope",
        "permissions": ["read", "write", "admin"]}]}}}}}}

    def configured_load(manager, manifest):
        # Doctor creates its own throwaway home before loading. Write a real
        # config there BEFORE original native registration, without mocking ctx.
        config_path = config.get_config_path()
        assert Path(os.environ["HERMES_HOME"]) in config_path.parents
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(configuration))  # JSON is valid YAML.
        return original_load(manager, manifest)

    def deterministic_llm_init(self, *args, **kwargs):
        kwargs["sync_caller"] = deterministic_provider
        original_llm_init(self, *args, **kwargs)
        facades.append(self)

    with patch.object(PluginManager, "_load_plugin", configured_load), \
         patch.object(PluginLlm, "__init__", deterministic_llm_init), \
         _doctor_runtime(Path(sys.argv[1])) as runtime:
        assert runtime.manifest.name == "health-data"
        assert set(runtime.registered_tools) == {"consult_health"}, runtime.registered_tools
        assert set(runtime.registered_hooks) == {"post_gateway_admission"}, runtime.registered_hooks
        entry = registry.get_entry("consult_health")
        assert entry.toolset == "casita_health"
        assert set(entry.schema["parameters"]["properties"]) == {"request"}
        assert len(facades) == 1, "ctx.llm must provide the real host-owned facade"
        assert not {"health_query", "health_log_food", "health_event_query", "append_entry", "activate_plan"} & set(registry.get_all_tool_names())

        def dispatch(args=None):
            raw = registry.dispatch("consult_health", {"request": "Registra e riepiloga"} if args is None else args)
            return json.loads(raw) if isinstance(raw, str) else raw

        def refused(result):
            assert result.get("outcome") == "refused", result
            assert result.get("code") == "care_request_refused", result

        def bind(**overrides):
            values = dict(platform="waha", user_id="synthetic-adult", chat_id="synthetic-private",
                          message_id="synthetic-message", session_key="synthetic-native-session")
            values.update(overrides)
            return sc.set_session_vars(**values)

        # An environment-only identity is insufficient, even if the process has
        # a plausible route. ContextVars must be bound by the actual host turn.
        with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": "waha",
            "HERMES_SESSION_USER_ID": "synthetic-adult", "HERMES_SESSION_CHAT_ID": "synthetic-private",
            "HERMES_SESSION_MESSAGE_ID": "synthetic-message", "HERMES_SESSION_KEY": "synthetic-native-session"}):
            refused(dispatch())
        tokens = bind()
        try:
            refused(dispatch())  # Native identity still has no admission receipt.
        finally:
            sc.clear_session_vars(tokens)
        assert calls == []

        source = {"user_id": "synthetic-adult", "chat_id": "synthetic-private"}
        message = "Registra questa osservazione sintetica, collega un episodio, correggila e riepiloga."
        # Actual async native hook dispatch; no direct runtime.admit test bypass.
        hook_results = asyncio.run(runtime.manager.ainvoke_hook("post_gateway_admission",
            session_key="synthetic-native-session", platform="waha", source=source,
            message_id="synthetic-message", text=message))
        assert hook_results == [], hook_results
        home = Path(os.environ["HERMES_HOME"])
        databases = list(home.rglob("apollo.sqlite3"))
        assert len(databases) == 1, databases
        database = databases[0]
        module = runtime.manager._plugins["health-data"].module
        native_runtime = module.care_plugin.active_runtime
        admitted = native_runtime.admitted(platform="waha", actor_id="synthetic-adult",
            conversation_id="synthetic-private", message_id="synthetic-message",
            session_key="synthetic-native-session")
        assert admitted.text == message
        with sqlite3.connect(database) as connection:
            # Admission alone must not retain general Casita messages in health
            # storage; only the later selected consult persists this receipt.
            assert connection.execute("SELECT COUNT(*) FROM care_ingress").fetchone()[0] == 0

        for overrides in ({"user_id": "synthetic-attacker"}, {"chat_id": "other-private"},
                          {"message_id": "unadmitted-message"}, {"session_key": "unadmitted-session"}):
            tokens = bind(**overrides)
            try:
                refused(dispatch())
            finally:
                sc.clear_session_vars(tokens)
        tokens = bind()
        try:
            refused(dispatch({"request": "Leggi", "scope_id": "another-patient"}))
            with delegated_child_context():
                refused(dispatch())
            with patch.dict(os.environ, {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
                refused(dispatch())
            # A plain worker lacks ContextVars; the supported host propagation
            # explicitly copies context into the synchronous tool worker.
            with ThreadPoolExecutor(max_workers=1) as pool:
                refused(pool.submit(dispatch).result())
                completed = pool.submit(copy_context().run, dispatch).result()
            assert completed["outcome"] == "succeeded", completed
            assert completed["replayed"] is False
            replayed = dispatch()
            assert replayed["replayed"] is True, replayed
            assert len(calls) == 2 and responses == []
        finally:
            sc.clear_session_vars(tokens)
        refused(dispatch())  # Cleared worker context cannot reuse an old receipt.

        # The real config reader must refresh grants after inference, not only
        # a test binding callback. The provider removes write permission before
        # returning a valid plan; no patient modification may follow.
        asyncio.run(runtime.manager.ainvoke_hook("post_gateway_admission",
            session_key="synthetic-native-session", platform="waha", source=source,
            message_id="synthetic-revoked", text="Nota sintetica dopo revoca"))
        responses.append(plan(("append_entry", {"category_id": "journal", "narrative": "Questa nota non deve essere salvata."})))
        revoke_after_inference = True
        tokens = bind(message_id="synthetic-revoked")
        try:
            revoked = dispatch()
            assert revoked["outcome"] == "needs_clarification", revoked
            assert revoked["results"][0]["status"] == "refused", revoked
            assert len(calls) == 3 and responses == []
        finally:
            sc.clear_session_vars(tokens)

        # Evidence is the actual Apollo schema and canonical projection, not a
        # mock datastore or replacement implementation from the archived repo.
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM care_ingress").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM care_entry_revisions").fetchone()[0] == 2
            saved = json.loads(connection.execute("SELECT payload_json FROM care_entries").fetchone()[0])
            assert saved["narrative"] == "Osservazione corretta sintetica."
            assert saved["actor_id"] == "synthetic-adult"
            assert saved["revision"] == 2
            assert connection.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
            assert connection.execute("SELECT items_json FROM food_logs").fetchone()[0] is None
            assert connection.execute("SELECT COUNT(*) FROM care_food_links").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM events WHERE provider='care_journal'").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM raw_records WHERE provider='care_journal'").fetchone()[0] == 2
            final_state = json.loads(connection.execute("SELECT state_json FROM care_dispatch").fetchone()[0])
            report = final_state["outputs"][-1]
            assert report["entry_count"] == 1
            assert report["clinical_inference"] is False
            assert len(report["food_entries"]) == 1
            assert report["entries"][0]["revision"] == 2

        # The real facade gates routing and builds provider-neutral structured
        # output. Only its transport caller is synthetic; no raw auth is used.
        for call in calls:
            assert call["provider_override"] is None and call["model_override"] is None
            assert call["profile_override"] is None and call["task"] is None
            assert call["max_tokens"] == 4000
            schema = call["extra_body"]["response_format"]["json_schema"]["schema"]
            assert set(schema["properties"]) == {"commands", "question", "done"}
            catalog = json.dumps(schema)
            assert "terminal" not in catalog and "ha_call_service" not in catalog
            assert '"activate_plan"' not in catalog and '"confirm_original"' not in catalog
        first_input = json.loads(calls[0]["messages"][-1]["content"][-1]["text"])
        assert first_input["original_message"] == message
        assert "append_entry" in first_input["available_actions"]
        assert "terminal" not in first_input["available_actions"]
        assert len(json.dumps(first_input).encode()) <= 32000

        print(json.dumps({"hermes_sha": sys.argv[2], "network": "blocked", "inference": "deterministic",
            "tools": list(runtime.registered_tools), "native_admission": "passed",
            "native_registry_dispatch": "passed", "ctx_llm_facade": "passed",
            "thread_context": "passed", "delegated_denial": "passed", "fresh_grants": "passed",
            "apollo_write_correction_food_report": "passed", "replay": "passed"}))
'''
