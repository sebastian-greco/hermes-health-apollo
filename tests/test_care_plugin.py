"""Native host lifecycle and opaque destination binding without live services."""
import importlib
from pathlib import Path
import sys
import threading
import types

import pytest


@pytest.fixture
def plugin():
    package = types.ModuleType("care_plugin_lifecycle_tests")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".care_plugin")


class Context:
    def __init__(self, root):
        self.state = types.SimpleNamespace(data_dir=root)
        self.llm = object()
        self.config = {"enabled": True, "bindings": [{"platform": "waha", "actor_id": "actor-a",
            "conversation_id": "native-chat-alias", "scope_id": "adult-a",
            "permissions": ["read", "write", "admin"]}], "reminders": {
            "enabled": True, "ha_url": "https://ha.example.test", "routes": [{
                "actor_id": "actor-a", "scope_id": "adult-a", "config_entry_id": "ha-config",
                "conversation_id": "direct-opaque-ha-id", "chat_id": "native-chat-alias"}]}}
        self.tools, self.hooks, self.unload = {}, {}, []

    def get_config(self, key, default=None):
        return self.config if key == "care" else self.config.get(key.split(".")[-1], default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_cli_command(self, *args, **kwargs):
        pass

    def on_unload(self, fn):
        self.unload.append(fn)


def fake_native(monkeypatch, plugin):
    services, egresses = [], []
    class Service:
        def __init__(self, session, authorize_current):
            self.session, self.authorize_current = session, authorize_current
            self.started, self.stopped = threading.Event(), threading.Event()
            services.append(self)
        def run_forever(self, stop, wake, egress):
            self.started.set()
            stop.wait(5)
            self.stopped.set()
    class Egress:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            egresses.append(self)
    package = plugin.__package__
    reminders = importlib.import_module(package + ".care_reminders")
    egress = importlib.import_module(package + ".care_egress")
    monkeypatch.setattr(reminders, "CareReminderService", Service)
    monkeypatch.setattr(egress, "HAConversationEgress", Egress)
    fake = types.ModuleType("agent.secret_scope")
    fake.get_scoped_secret = lambda *args: "synthetic-token"
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    return services, egresses


def test_worker_native_alias_maps_to_fixed_ha_conversation_and_stops(plugin, tmp_path, monkeypatch):
    services, egresses = fake_native(monkeypatch, plugin)
    ctx = Context(tmp_path)
    runtime = plugin.register(ctx)
    try:
        assert services[0].started.wait(2)
        assert set(ctx.tools) == {"consult_health"}
        assert runtime.reminders_enabled is True
        egress = egresses[0]
        assert egress.route.conversation_id == "direct-opaque-ha-id"
        assert egress.authorize_current("actor-a", "adult-a", egress.route) is True
        ctx.config["bindings"][0]["permissions"] = ["read"]
        assert egress.authorize_current("actor-a", "adult-a", egress.route) is False
        assert services[0].authorize_current(services[0].session.authority) is False
    finally:
        ctx.unload[0]()
    assert services[0].stopped.is_set()
    assert runtime.reminders_enabled is False


def test_duplicate_scope_or_mismatched_alias_fails_closed_before_threads(plugin, tmp_path, monkeypatch):
    services, _ = fake_native(monkeypatch, plugin)
    ctx = Context(tmp_path)
    ctx.config["reminders"]["routes"].append(dict(ctx.config["reminders"]["routes"][0]))
    with pytest.raises(ValueError):
        plugin.register(ctx)
    assert not any(s.started.is_set() for s in services)


def test_changed_host_or_removed_route_revokes_existing_egress(plugin, tmp_path, monkeypatch):
    _, egresses = fake_native(monkeypatch, plugin)
    ctx = Context(tmp_path)
    plugin.register(ctx)
    try:
        client = egresses[0]
        ctx.config["reminders"] = {**ctx.config["reminders"], "ha_url": "https://other.example.test"}
        assert client.authorize_current("actor-a", "adult-a", client.route) is False
        ctx.config["reminders"] = {**ctx.config["reminders"], "ha_url": "https://ha.example.test", "routes": []}
        assert client.authorize_current("actor-a", "adult-a", client.route) is False
    finally:
        ctx.unload[0]()
