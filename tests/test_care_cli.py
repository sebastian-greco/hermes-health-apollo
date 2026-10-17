"""Synthetic tests for the operator-only care CLI; no accounts or HA calls."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import importlib
import json
from pathlib import Path
import stat
import sys
import types

import pytest


@pytest.fixture
def cli():
    package = types.ModuleType("care_cli_tests")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".care_cli")


class Binding:
    platform = "waha"
    actor_id = "actor-private"
    conversation_id = "native-chat-private"
    scope_id = "patient-private"
    permissions = frozenset({"read", "write", "admin"})


class Session:
    authority = types.SimpleNamespace(scope_id="patient-private", permissions=frozenset({"read", "write", "admin"}))

    def __init__(self):
        self.calls = []

    def require(self, permission):
        assert permission in self.authority.permissions

    def report(self, **kwargs):
        self.calls.append(kwargs)
        return {"entry_count": 1, "entries": [{"sensitive": "synthetic-health-data"}]}


class Runtime:
    def __init__(self):
        self.root = Path("/private/casita")
        self.reminders_enabled = False
        self.bound = [Binding()]
        self.session_calls = []
        self.test_session = Session()
        self.documents = None

    def bindings(self):
        return self.bound

    def _binding(self, origin):
        return self.bound[0]

    def session(self, origin):
        self.session_calls.append(origin)
        assert (origin.actor_id, origin.conversation_id) == ("actor-private", "native-chat-private")
        return self.test_session


def settings():
    return {"enabled": True,
            "bindings": [{"platform": "waha", "actor_id": "actor-private",
                          "conversation_id": "native-chat-private", "scope_id": "patient-private",
                          "permissions": ["read", "write", "admin"]}],
            "drive": {"patient-private": {"client_file": "/protected/private.json",
                                              "folder_id": "sensitive-folder-id",
                                              "scope_mode": "selected_folder"}},
            "reminders": {"enabled": False, "ha_url": "https://private-ha", "routes": []}}


def test_status_only_exposes_safe_configuration_counts(cli, capsys):
    result = cli.status(runtime=Runtime(), settings=settings)
    encoded = json.dumps(result)
    assert result["binding_count"] == 1
    assert result["drive_configured_binding_count"] == 1
    assert "actor-private" not in encoded
    assert "patient-private" not in encoded
    assert "sensitive-folder-id" not in encoded
    assert "private-ha" not in encoded


def test_registration_uses_native_cli_setup_contract(cli, capsys):
    class Ctx:
        def register_cli_command(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    ctx = Ctx()
    callback = cli.register_cli(ctx, Runtime(), settings)
    assert ctx.args[0] == "health-care"
    parser = argparse.ArgumentParser()
    ctx.args[2](parser)
    args = parser.parse_args(["status"])
    assert args.func(args) == 0
    assert '"binding_count": 1' in capsys.readouterr().out
    assert callback


def test_report_is_private_exclusive_and_not_printed_to_stdout(cli, tmp_path, capsys):
    module = importlib.import_module(cli.__package__ + ".care_runtime")
    binding = module.Binding("waha", "actor-private", "native-chat-private", "patient-private",
                             frozenset({"read", "write", "admin"}))
    runtime = module.CareRuntime(tmp_path / "runtime", bindings=lambda: [binding], llm=object())
    origin = module.Origin("waha", "actor-private", "native-chat-private", "synthetic", "native",
                           "Nota riferita", "2026-10-01T12:00:00Z")
    runtime.session(origin).append_entry(category_id="journal", request_id="synthetic-entry", source_ref="synthetic",
        narrative="synthetic-health-data", occurrence_start="2026-10-01T12:00:00Z", uncertainty="exact")
    output = tmp_path / "health-report.json"
    args = argparse.Namespace(action="report", binding=1, output=str(output), start="2026-10-01", end=None,
                              episode_id=None, since_visit_id=None, issue_id=None)
    result = cli.dispatch(args, runtime=runtime, settings=settings)
    assert result["entry_count"] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "synthetic-health-data" in output.read_text()
    assert "synthetic-health-data" not in capsys.readouterr().out
    with pytest.raises(cli.CareCLIError):
        cli.dispatch(args, runtime=runtime, settings=settings)


def test_report_requires_current_read_binding_and_does_not_accept_identity(cli, tmp_path):
    runtime = Runtime()
    runtime.bound[0].permissions = frozenset({"write"})
    args = argparse.Namespace(action="report", binding=1, output=str(tmp_path / "report.json"), start="2026-10-01",
                              end=None, episode_id=None, since_visit_id=None, issue_id=None)
    with pytest.raises(PermissionError):
        cli.dispatch(args, runtime=runtime, settings=settings)
    assert not (tmp_path / "report.json").exists()


def test_document_file_batch_is_explicit_and_upload_requires_prepare(cli, tmp_path):
    runtime = Runtime()
    class Workflow:
        def __init__(self):
            self.calls = []
        def intake(self, sources, *, request_id):
            self.calls.append(("intake", sources, request_id))
            return {"job_id": "job-synthetic", "state": "received", "sources": sources}
        def prepare(self, job_id):
            self.calls.append(("prepare", job_id))
            return {"job_id": job_id, "state": "prepared"}
        def upload(self, job_id):
            self.calls.append(("upload", job_id))
            return {"job_id": job_id, "state": "uploaded"}
    workflow = Workflow()
    runtime.documents = lambda session: workflow
    source = tmp_path / "test.pdf"
    source.write_bytes(b"%PDF synthetic")
    base = dict(action="documents", document_action="intake", binding=1, files=[str(source)],
                request_id="synthetic-request", prepare=False, upload=False)
    result = cli.dispatch(argparse.Namespace(**base), runtime=runtime, settings=settings)
    assert result["source_count"] == 1
    assert [call[0] for call in workflow.calls] == ["intake"]
    with pytest.raises(cli.CareCLIError):
        cli.dispatch(argparse.Namespace(**{**base, "upload": True}), runtime=runtime, settings=settings)
    assert [call[0] for call in workflow.calls] == ["intake"]
    result = cli.dispatch(argparse.Namespace(**{**base, "prepare": True, "upload": True}),
                          runtime=runtime, settings=settings)
    assert result["uploaded"] is True
    assert [call[0] for call in workflow.calls] == ["intake", "intake", "prepare", "upload"]


def test_document_loader_rejects_symlink_and_unsupported_file(cli, tmp_path):
    real = tmp_path / "source.pdf"
    real.write_bytes(b"%PDF synthetic")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    with pytest.raises(cli.CareCLIError):
        cli._load_sources([str(link)])
    text = tmp_path / "notes.txt"
    text.write_text("synthetic")
    with pytest.raises(cli.CareCLIError):
        cli._load_sources([str(text)])


def test_drive_oauth_callback_is_hidden_and_result_redacted(cli, monkeypatch):
    calls = []
    class Auth:
        @classmethod
        def from_config(cls, session, config, *, runtime_root):
            calls.append((session, config, runtime_root))
            return cls()
        def finish(self, callback):
            assert callback == "http://127.0.0.1:8765/?code=very-secret-code&state=secret-state"
            return {"connected": True, "scope_mode": "selected_folder", "token": "must-not-leak"}
    module = importlib.import_module(cli.__package__ + ".care_drive_auth")
    monkeypatch.setattr(module, "DriveOAuth", Auth)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "getpass", types.SimpleNamespace(getpass=lambda prompt: "http://127.0.0.1:8765/?code=very-secret-code&state=secret-state"))
    args = argparse.Namespace(action="drive", drive_action="finish", binding=1)
    result = cli.dispatch(args, runtime=Runtime(), settings=settings)
    encoded = json.dumps(result)
    assert result["drive"]["connected"] is True
    assert "very-secret-code" not in encoded and "must-not-leak" not in encoded
    assert len(calls) == 1


def test_callback_errors_are_redacted(cli, capsys):
    class Ctx:
        def register_cli_command(self, *args, **kwargs):
            self.callback = args[2]
    ctx = Ctx()
    callback = cli.register_cli(ctx, Runtime(), settings)
    result = callback(argparse.Namespace(action="invalid"))
    assert result == 2
    output = capsys.readouterr().out
    assert "operation_failed" in output
    assert "Traceback" not in output
