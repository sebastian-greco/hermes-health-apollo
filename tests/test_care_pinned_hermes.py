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
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

assert not any(k in os.environ for k in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','GOOGLE_APPLICATION_CREDENTIALS'))
def deny_network(*args, **kwargs):
    raise AssertionError('Network forbidden in synthetic SDK test')

with patch.object(socket.socket,'connect',deny_network), patch.object(socket.socket,'connect_ex',deny_network), patch.object(socket,'create_connection',deny_network):
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.delegation_context import delegated_child_context
    from gateway import session_context as sc
    from hermes_cli import config
    from hermes_cli.plugin_dev import _doctor_runtime
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    configuration={'plugins': {'entries': {'health-data': {'settings': {'care': {
        'enabled': True, 'bindings': [{'platform':'waha','actor_id':'actor-a','conversation_id':'private-a',
        'scope_id':'scope-a','permissions':['read','write','admin']}]}}}}}}
    original_load=PluginManager._load_plugin
    def configured_load(manager, manifest):
        path=config.get_config_path()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(configuration))
        return original_load(manager,manifest)

    with patch.object(PluginManager,'_load_plugin',configured_load), _doctor_runtime(Path(sys.argv[1])) as loaded:
        module=loaded.manager._plugins['health-data'].module
        runtime=module.care_plugin.active_runtime
        dispatcher=module.care_plugin.active_dispatcher
        assert registry.get_entry('consult_health').toolset=='casita_health'
        assert registry.get_entry('health_append_entry').toolset=='casita_health_specialist'
        assert registry.get_entry('health_finish').toolset=='casita_health_specialist'
        assert 'health_query' not in loaded.registered_tools
        parent=SimpleNamespace(session_id='actual-parent-session')
        class Runner:
            def __init__(self): self.calls=[]
            def dispatch(self,parent_agent,**kwargs):
                self.calls.append(kwargs)
                self.child=SimpleNamespace(session_id='native-child-session',_subagent_id='sa-health-child',is_interrupted=lambda:False)
                dispatcher._started(kwargs['run_id'],self.child)
                return {'status':'dispatched','delegation_id':'native-delegation'}
            def cancel(self,*args,**kwargs): pass
        runner=Runner()
        dispatcher._runner=runner
        def dispatch(args=None):
            return json.loads(registry.dispatch('consult_health',args or {'request':'Registra'}))
        def bind(**overrides):
            values=dict(platform='waha',user_id='actor-a',chat_id='private-a',message_id='source-a',session_key='native-session-a')
            values.update(overrides)
            return sc.set_session_vars(**values)
        def refused(value): assert value.get('outcome')=='refused',value
        with bind_subagent_parent(parent):
            refused(dispatch())
            with patch.dict(os.environ,{'HERMES_SESSION_PLATFORM':'waha','HERMES_SESSION_USER_ID':'actor-a','HERMES_SESSION_CHAT_ID':'private-a','HERMES_SESSION_MESSAGE_ID':'source-a','HERMES_SESSION_KEY':'native-session-a'}):
                refused(dispatch())
            tokens=bind()
            try: refused(dispatch())
            finally: sc.clear_session_vars(tokens)
        asyncio.run(loaded.manager.ainvoke_hook('post_gateway_admission',session_key='native-session-a',platform='waha',
            source={'user_id':'actor-a','chat_id':'private-a'},message_id='source-a',text='Nota salute sintetica'))
        database=next(Path(os.environ['HERMES_HOME']).rglob('apollo.sqlite3'))
        with sqlite3.connect(database) as conn:
            assert conn.execute('SELECT COUNT(*) FROM care_ingress').fetchone()[0]==0
        tokens=bind()
        try:
            with bind_subagent_parent(parent):
                refused(dispatch({'request':'Leggi','scope_id':'other-patient'}))
                with delegated_child_context(): refused(dispatch())
                ack=dispatch()
                assert ack['outcome']=='delegated',ack
                assert dispatch()['outcome']=='existing'
            assert len(runner.calls)==1
            # Actual plugin registration reads host active-agent context; no
            # parent_agent kwarg is passed by registry/model_tools.
            with bind_subagent_parent(SimpleNamespace(session_id='spoof',_subagent_id='spoof')):
                result=json.loads(registry.dispatch('health_append_entry',{'action_id':'note-a','args':{'category_id':'journal','narrative':'No'}}))
                assert result['status']=='refused'
            with bind_subagent_parent(runner.child):
                saved=json.loads(registry.dispatch('health_append_entry',{'action_id':'note-a','args':{'category_id':'journal','narrative':'Nota sintetica salvata'}}))
                assert saved['entry_id'],saved
                finished=json.loads(registry.dispatch('health_finish',{}))
                assert finished['status']=='finished',finished
            completion=dispatcher._finished(ack['job_id'],{'status':'completed','summary':'UNVERIFIED MODEL CLAIM'})
            assert 'UNVERIFIED' not in completion['summary'] and 'Nota sintetica' not in completion['summary']
        finally: sc.clear_session_vars(tokens)
        # Native internal completion has no human message/admission receipt.
        tokens=bind(message_id='')
        try:
            with bind_subagent_parent(parent):
                status=dispatch({'request':'Risultato','action':'status','job_id':ack['job_id']})
                assert status['state']=='completed',status
                assert status['receipts'][0]['result']['entry_id']==saved['entry_id']
                configuration['plugins']['entries']['health-data']['settings']['care']['bindings']=[]
                config.get_config_path().write_text(json.dumps(configuration))
                refused(dispatch({'request':'Risultato','action':'status','job_id':ack['job_id']}))
        finally: sc.clear_session_vars(tokens)
        with sqlite3.connect(database) as conn:
            assert conn.execute('SELECT COUNT(*) FROM care_entries').fetchone()[0]==1
            assert conn.execute('SELECT COUNT(*) FROM care_jobs').fetchone()[0]==1
        print(json.dumps({'native_admission':'passed','native_registry_host_identity':'passed',
            'parent_internal_status_fresh_grants':'passed','apollo_journal':'passed'}))
'''
