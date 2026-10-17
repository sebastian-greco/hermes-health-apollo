from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    package_name = "hermes_plugins.health_data"
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}", ROOT / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


care_egress = load_module("care_egress")


class FakeHTTP:
    def __init__(self, status=200, response=None, error=None):
        self.status = status
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body), timeout))
        if self.error:
            raise self.error
        return self.status, json.dumps(self.response).encode()


def envelope(status="accepted", *, key="stable-key", conversation="private-chat",
             message_id="msg_123", replayed=False):
    return {"changed_states": [], "service_response": {
        "conversation_id": conversation,
        "client_request_id": key,
        "status": status,
        "message_id": message_id,
        "replayed": replayed,
    }}


@pytest.fixture
def route():
    return care_egress.TrustedConversation(
        actor_id="adult-1", scope_id="patient-private",
        config_entry_id="entry-123", conversation_id="private-chat",
    )


def make_egress(route, http, authorize=lambda actor, scope, current: True):
    return care_egress.HAConversationEgress(
        ha_url="wss://ha.example.test",
        token_provider=lambda: "synthetic-token",
        route=route,
        authorize_current=authorize,
        http_post=http,
    )


def test_accepted_response_preserves_key_route_and_redacts_health_content(route):
    http = FakeHTTP(response=envelope(message_id=None, replayed=True))
    observed = []
    client = make_egress(route, http, lambda actor, scope, current:
                         observed.append((actor, scope, current)) or True)
    payload = {"reminder_kind": "medication", "message": "Promemoria farmaco: prova."}

    result = client.send(payload, client_request_id="stable-key")

    assert result.status == "sent"
    assert result.provider_message_id is None
    assert result.detail == "accepted_replayed"
    assert len(observed) == 2
    assert observed[0] == ("adult-1", "patient-private", route)
    url, headers, body, timeout = http.calls[0]
    assert url == "https://ha.example.test/api/services/script/casita_conversation_egress?return_response"
    assert headers["Authorization"] == "Bearer synthetic-token"
    assert timeout == 15
    assert body == {
        "schema_version": "casita-egress-v1",
        "config_entry_id": "entry-123",
        "conversation_id": "private-chat",
        "client_request_id": "stable-key",
        "message": payload["message"],
        "origin": "application",
        "content_redacted": True,
    }


@pytest.mark.parametrize("remote_status,expected", [
    ("accepted", "sent"), ("failed", "not_sent"),
    ("pending", "unknown"), ("unknown", "unknown"),
])
def test_central_status_mapping(route, remote_status, expected):
    client = make_egress(route, FakeHTTP(response=envelope(remote_status)))
    result = client.send({"reminder_kind": "manual", "message": "Promemoria."},
                         client_request_id="stable-key")
    assert result.status == expected


def test_timeout_and_http_error_are_uncertain_and_lookup_has_no_fake_answer(route):
    http = FakeHTTP(error=TimeoutError("synthetic timeout"))
    client = make_egress(route, http)

    result = client.send({"reminder_kind": "manual", "message": "Promemoria."},
                         client_request_id="stable-key")
    attempted = len(http.calls)
    lookup = client.lookup(client_request_id="stable-key")

    assert result.status == "unknown"
    assert "timeout" not in (result.detail or "")
    assert lookup.status == "unknown"
    assert lookup.detail == "central_egress_lookup_unavailable"
    assert attempted == 1
    assert len(http.calls) == attempted


def test_fresh_route_revocation_prevents_http_call_and_token_read(route):
    http = FakeHTTP(response=envelope())
    token_reads = []
    client = care_egress.HAConversationEgress(
        ha_url="wss://ha.example.test", token_provider=lambda: token_reads.append(True) or "token",
        route=route, authorize_current=lambda actor, scope, current: False, http_post=http,
    )

    result = client.send({"reminder_kind": "medication", "message": "Promemoria."},
                         client_request_id="stable-key")

    assert result.status == "not_sent"
    assert result.detail == "current_route_not_authorized"
    assert http.calls == []
    assert token_reads == []


def test_route_revoked_after_token_read_is_rechecked_before_http(route):
    http = FakeHTTP(response=envelope())
    checks = []

    def authorize(actor, scope, current):
        checks.append((actor, scope, current))
        return len(checks) == 1

    client = make_egress(route, http, authorize)
    result = client.send({"reminder_kind": "manual", "message": "Promemoria."},
                         client_request_id="stable-key")

    assert result.status == "not_sent"
    assert result.detail == "current_route_not_authorized"
    assert len(checks) == 2
    assert http.calls == []


def test_missing_or_untrusted_route_authorization_fails_closed(route):
    http = FakeHTTP(response=envelope())
    client = care_egress.HAConversationEgress(
        ha_url="wss://ha.example.test", token_provider=lambda: "token",
        route=route, authorize_current=None, http_post=http,
    )

    assert client.send({"reminder_kind": "manual", "message": "Hi"},
                       client_request_id="stable-key").status == "not_sent"
    assert http.calls == []


@pytest.mark.parametrize("response", [
    {"changed_states": [], "service_response": {"status": "accepted"}},
    envelope(key="wrong-key"), envelope(conversation="wrong-chat"),
    envelope(message_id=42), envelope(replayed="yes"),
])
def test_unmatched_or_malformed_success_response_remains_unknown(route, response):
    client = make_egress(route, FakeHTTP(response=response))
    result = client.send({"reminder_kind": "manual", "message": "Promemoria."},
                         client_request_id="stable-key")
    assert result.status == "unknown"


@pytest.mark.parametrize("url", [
    "http://ha.example.test", "wss://user:password@ha.example.test",
    "wss://ha.example.test/private/path", "wss://ha.example.test?token=secret",
    "wss://ha.example.test:invalid",
])
def test_transport_rejects_insecure_or_ambiguous_host_url(route, url):
    with pytest.raises(care_egress.CareEgressError):
        care_egress.HAConversationEgress(
            ha_url=url, token_provider=lambda: "token", route=route,
            authorize_current=lambda *_: True, http_post=FakeHTTP(),
        )


def test_invalid_payload_and_missing_token_do_not_call_http(route):
    http = FakeHTTP(response=envelope())
    client = make_egress(route, http)
    assert client.send({"reminder_kind": "manual", "message": ""},
                       client_request_id="stable-key").status == "not_sent"
    no_token = care_egress.HAConversationEgress(
        ha_url="wss://ha.example.test", token_provider=lambda: "",
        route=route, authorize_current=lambda *_: True, http_post=http,
    )
    assert no_token.send({"reminder_kind": "manual", "message": "Promemoria."},
                         client_request_id="stable-key").status == "not_sent"
    assert http.calls == []
