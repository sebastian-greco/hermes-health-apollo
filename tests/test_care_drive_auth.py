from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.setdefault("hermes_plugins.health_data", types.ModuleType("hermes_plugins.health_data"))
    package.__path__ = [str(ROOT)]
    return importlib.import_module("hermes_plugins.health_data." + name)


@pytest.fixture
def setup(tmp_path):
    auth = load("care_drive_auth")
    core = load("care_store")
    session = core.CareStore(tmp_path / "apollo.db", scope_id="synthetic-adult").session(
        core.Authority("synthetic-actor", "synthetic-adult", frozenset({"read", "write", "admin"})))
    clock = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc)]
    calls = []

    def post(url, data, headers=None):
        calls.append((url, dict(data)))
        if data["grant_type"] == "authorization_code":
            return {"access_token": "synthetic-drive-access", "refresh_token": "synthetic-drive-refresh",
                    "scope": auth.DRIVE_FILE_SCOPE, "token_type": "Bearer", "expires_in": 3600}
        return {"access_token": "synthetic-refreshed-access", "expires_in": 3600}

    client = auth.DesktopClient("synthetic-desktop-client", "synthetic-client-secret")
    manager = auth.DriveOAuth(session, runtime_root=tmp_path / "private-auth", client=client,
                             folder_id="synthetic_folder", http_post=post, now=lambda: clock[0])
    return auth, core, session, manager, clock, calls


def callback(manager, consent, *, code="synthetic-code", state=None, redirect=None):
    params = parse_qs(urlsplit(consent["authorization_url"]).query)
    return (redirect or params["redirect_uri"][0]) + "?" + urlencode({
        "code": code, "state": state or params["state"][0]})


def connect(manager):
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    return consent, manager.finish(callback(manager, consent))


def test_begin_uses_only_drive_scope_pkce_random_state_and_private_files(setup):
    auth, _, _, manager, _, calls = setup
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    parts = urlsplit(consent["authorization_url"])
    assert parts.scheme == "https" and parts.netloc == "accounts.google.com"
    params = parse_qs(parts.query)
    assert params["scope"] == [auth.DRIVE_FILE_SCOPE]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "include_granted_scopes" not in params
    assert "client_secret" not in params
    state = json.loads(manager._state_file.read_text())
    verifier = state["verifier"]
    import base64
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert params["code_challenge"] == [expected]
    assert "verifier" not in json.dumps(consent)
    assert manager.status() == {"connected": False, "consent_pending": True,
                                "reconnect_required": False, "scope_mode": "selected_folder"}
    assert manager._state_file.stat().st_mode & 0o077 == 0
    assert manager._state_file.parent.stat().st_mode & 0o077 == 0
    assert not calls


def test_finish_exchanges_pkce_once_safe_result_and_provider_is_private(setup):
    auth, _, _, manager, _, calls = setup
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    original = json.loads(manager._state_file.read_text())
    result = manager.finish(callback(manager, consent))
    assert result == {"connected": True, "folder_verified": False, "scope_mode": "selected_folder"}
    assert calls[0][0] == auth.TOKEN_URL
    assert calls[0][1]["code_verifier"] == original["verifier"]
    assert calls[0][1]["redirect_uri"] == "http://127.0.0.1:43210/"
    assert manager._token_file.stat().st_mode & 0o077 == 0
    assert json.loads(manager._state_file.read_text())["status"] == "completed"
    assert "verifier" not in manager._state_file.read_text()
    assert manager.token_provider() == "synthetic-drive-access"
    assert "synthetic-" not in json.dumps(result)
    assert "synthetic-" not in json.dumps(manager.status())
    with pytest.raises(auth.DriveAuthError, match="session unavailable"):
        manager.finish(callback(manager, consent))
    assert len(calls) == 1


@pytest.mark.parametrize("value", [
    "synthetic-code",
    "http://127.0.0.1:43210/?code=synthetic-code",
    "http://127.0.0.1:43210/?code=one&code=two&state=synthetic-invalid-state",
    "https://example.invalid/?code=one&state=synthetic-invalid-state",
    "http://127.0.0.1:43211/?code=one&state=synthetic-invalid-state",
    "http://127.0.0.1:43210/other?code=one&state=synthetic-invalid-state",
    "http://127.0.0.1:43210/?code=one&state=synthetic-invalid-state#fragment",
])
def test_callback_requires_exact_loopback_destination_and_state(setup, value):
    auth, _, _, manager, _, calls = setup
    manager.begin(redirect_uri="http://127.0.0.1:43210/")
    with pytest.raises(auth.DriveAuthError):
        manager.finish(value)
    assert not calls
    assert not manager._token_file.exists()


def test_state_mismatch_expiry_and_declined_consent_do_not_call_google(setup):
    auth, _, _, manager, clock, calls = setup
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    with pytest.raises(auth.DriveAuthError, match="state mismatch"):
        manager.finish(callback(manager, consent, state="x" * 43))
    denied = "http://127.0.0.1:43210/?" + urlencode({"error": "access_denied", "state": "x" * 43})
    with pytest.raises(auth.DriveAuthError, match="declined"):
        manager.finish(denied)
    clock[0] += timedelta(minutes=11)
    with pytest.raises(auth.DriveAuthError, match="expired"):
        manager.finish(callback(manager, consent))
    assert not calls


def test_uncertain_exchange_is_consumed_and_cannot_replay(setup):
    auth, _, _, manager, _, _ = setup
    calls = []

    def interrupted(*args):
        calls.append(1)
        raise OSError("synthetic-code-and-client-secret must not leak")

    manager._post = interrupted
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    with pytest.raises(auth.DriveAuthError, match="exchange failed") as error:
        manager.finish(callback(manager, consent))
    assert "synthetic" not in str(error.value)
    with pytest.raises(auth.DriveAuthError, match="session unavailable"):
        manager.finish(callback(manager, consent))
    assert len(calls) == 1


def test_refresh_reuses_apollo_lock_pending_and_preserves_refresh_token(setup):
    auth, _, session, manager, clock, calls = setup
    connect(manager)
    clock[0] += timedelta(hours=2)
    seen = []
    original_post = manager._post

    def checking_post(url, data, headers=None):
        assert auth.oauth_token.pending_path(manager._token_file).exists()
        seen.append(data)
        return original_post(url, data, headers)

    restarted = auth.DriveOAuth(session, runtime_root=manager.root.parent.parent,
                               client=manager.client, folder_id="synthetic_folder",
                               http_post=checking_post, now=lambda: clock[0])
    assert restarted.token_provider() == "synthetic-refreshed-access"
    token = json.loads(manager._token_file.read_text())
    assert token["refresh_token"] == "synthetic-drive-refresh"
    assert token["care_drive_binding"] == manager._binding
    assert not auth.oauth_token.pending_path(manager._token_file).exists()
    assert seen[0]["grant_type"] == "refresh_token"
    assert len(calls) == 2
    assert restarted.token_provider() == "synthetic-refreshed-access"
    assert len(calls) == 2


def test_refresh_revocation_is_safe_and_diagnostics_do_not_leak(setup):
    auth, _, _, manager, clock, _ = setup
    connect(manager)
    clock[0] += timedelta(hours=2)
    manager._post = lambda *args: {"error": "invalid_grant", "error_description": "synthetic-secret"}
    with pytest.raises(auth.DriveAuthError, match="token unavailable") as error:
        manager.token_provider()
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-" not in json.dumps(manager.status())
    assert auth.oauth_token.pending_path(manager._token_file).stat().st_mode & 0o077 == 0


def test_existing_folder_scope_requires_explicit_broad_approval(setup):
    auth, _, session, manager, _, _ = setup
    with pytest.raises(auth.DriveAuthError, match="explicit approval"):
        auth.DriveOAuth(session, runtime_root=manager.root.parent.parent, client=manager.client,
                        folder_id="synthetic_folder", scope_mode="existing_folder")
    broad = auth.DriveOAuth(session, runtime_root=manager.root.parent.parent, client=manager.client,
                           folder_id="synthetic_folder", scope_mode="existing_folder", full_drive_approved=True)
    consent = broad.begin(redirect_uri="http://127.0.0.1:43210/")
    assert parse_qs(urlsplit(consent["authorization_url"]).query)["scope"] == [auth.DRIVE_SCOPE]


def test_token_scope_escalation_or_configuration_swap_is_rejected(setup):
    auth, _, session, manager, _, calls = setup
    manager._post = lambda *args: {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                                  "scope": auth.DRIVE_SCOPE, "expires_in": 3600}
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    with pytest.raises(auth.DriveAuthError, match="exchange failed"):
        manager.finish(callback(manager, consent))
    assert not manager._token_file.exists()
    manager._post = lambda *args: {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                                  "scope": auth.DRIVE_FILE_SCOPE, "expires_in": 3600}
    connect(manager)
    swapped = auth.DriveOAuth(session, runtime_root=manager.root.parent.parent, client=manager.client,
                             folder_id="different_folder", http_post=lambda *args: pytest.fail("Must not contact token endpoint"))
    with pytest.raises(auth.DriveAuthError, match="another trusted configuration"):
        swapped.token_provider()
    assert swapped.status()["reconnect_required"] is True


def test_folder_check_uses_exact_folder_and_capability_without_upload(setup):
    auth, _, _, manager, _, _ = setup
    docs = load("care_documents")
    connect(manager)
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        assert method == "GET" and urlsplit(url).path.endswith("/synthetic_folder")
        return docs.HTTPResponse(200, {}, json.dumps({"id": "synthetic_folder", "mimeType": "application/vnd.google-apps.folder",
                                                     "capabilities": {"canAddChildren": True}}).encode())

    assert manager.verify_folder(transport=transport)["folder_verified"] is True
    assert len(calls) == 1
    with pytest.raises(auth.DriveAuthError, match="not accessible"):
        manager.verify_folder(transport=lambda *args: docs.HTTPResponse(200, {}, json.dumps({
            "id": "synthetic_folder", "mimeType": "application/vnd.google-apps.folder",
            "capabilities": {"canAddChildren": False}}).encode()))


def test_scope_marker_permission_and_symlink_protect_tokens(setup, tmp_path):
    auth, core, session, manager, _, _ = setup
    connect(manager)
    manager._token_file.unlink()
    external = tmp_path / "external"
    external.write_text("{}")
    manager._token_file.symlink_to(external)
    with pytest.raises(auth.DriveAuthError, match="Unsafe OAuth runtime file"):
        manager.token_provider()
    manager._token_file.unlink()
    with session.connection(permission="write") as conn:
        conn.execute("UPDATE care_scope SET scope_id='foreign-scope'")
    with pytest.raises(PermissionError, match="scope binding changed"):
        manager.status()


@pytest.mark.parametrize("uri", ["https://example.invalid/", "http://localhost:43210/", "http://0.0.0.0:43210/",
                                 "http://127.0.0.1:80/", "http://127.0.0.1:43210/path", "http://127.0.0.1:43210/?x=y"])
def test_unsafe_redirect_uris_are_refused_before_oauth_state(setup, uri):
    auth, _, _, manager, _, calls = setup
    with pytest.raises(auth.DriveAuthError):
        manager.begin(redirect_uri=uri)
    assert not calls
    assert not manager._state_file.exists()


def test_token_http_transport_rejects_redirect_without_following(setup, monkeypatch):
    auth, *_ = setup
    from urllib.error import HTTPError
    import io
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise HTTPError(auth.TOKEN_URL, 302, "redirect", {"Location": "https://example.invalid/"}, io.BytesIO(b""))

    monkeypatch.setattr(auth, "build_opener", lambda *args: Opener())
    with pytest.raises(auth.DriveAuthError, match="redirect rejected"):
        auth.token_http_post(auth.TOKEN_URL, {"code": "synthetic-code"})
    assert calls == [auth.TOKEN_URL]
    with pytest.raises(auth.DriveAuthError, match="destination rejected"):
        auth.token_http_post("https://example.invalid/", {})


def test_loopback_receiver_binds_only_loopback_and_suppresses_callback_logs(setup, monkeypatch):
    auth, *_ = setup
    captured = {}

    class FakeServer:
        server_port = 43210

        def __init__(self, address, handler):
            captured["address"] = address
            captured["handler"] = handler

        def server_close(self):
            captured["closed"] = True

        def handle_request(self):
            receiver._callback_url = "http://127.0.0.1:43210/?code=synthetic&state=synthetic-state"

    monkeypatch.setattr(auth, "HTTPServer", FakeServer)
    with auth.LoopbackReceiver() as receiver:
        assert captured["address"] == ("127.0.0.1", 0)
        assert receiver.redirect_uri == "http://127.0.0.1:43210/"
        assert "code=synthetic" in receiver.receive(timeout_seconds=1)
        assert captured["handler"].log_message(None, "sensitive-path") is None
    assert captured["closed"] is True


def test_from_config_reads_only_protected_explicit_desktop_client_file(setup, tmp_path):
    auth, _, session, _, clock, calls = setup
    client_file = tmp_path / "operator-client.json"
    client_file.write_text(json.dumps({"installed": {"client_id": "synthetic-configured-client",
                                                    "client_secret": "synthetic-protected-client-secret",
                                                    "token_uri": "https://example.invalid/ignored"}}))
    client_file.chmod(0o600)
    manager = auth.DriveOAuth.from_config(session, {"client_file": str(client_file), "folder_id": "synthetic_folder"},
                                         runtime_root=tmp_path / "config-auth", now=lambda: clock[0])
    consent = manager.begin(redirect_uri="http://127.0.0.1:43210/")
    assert parse_qs(urlsplit(consent["authorization_url"]).query)["client_id"] == ["synthetic-configured-client"]
    assert "synthetic-protected-client-secret" not in json.dumps(consent)
    assert not manager._token_file.exists()
    assert not calls
    assert repr(manager.client) == "DesktopClient()"
    client_file.chmod(0o644)
    with pytest.raises(auth.DriveAuthError, match="must be private"):
        auth.DriveOAuth.from_config(session, {"client_file": str(client_file), "folder_id": "synthetic_folder"},
                                   runtime_root=tmp_path / "config-auth")
    with pytest.raises(auth.DriveAuthError, match="embedded credentials"):
        auth.DriveOAuth.from_config(session, {"client_secret": "synthetic-secret", "folder_id": "synthetic_folder"},
                                   runtime_root=tmp_path / "config-auth")


def test_revocation_persists_safe_reconnect_status_and_stops_refresh_replay(setup):
    auth, _, _, manager, clock, _ = setup
    connect(manager)
    clock[0] += timedelta(hours=2)
    calls = []

    def revoked(*args):
        calls.append(1)
        return {"error": "invalid_grant"}

    manager._post = revoked
    with pytest.raises(auth.DriveAuthError):
        manager.token_provider()
    assert manager.status()["reconnect_required"] is True
    assert manager.status()["connected"] is False
    with pytest.raises(auth.DriveAuthError, match="expired"):
        manager.token_provider()
    assert calls == [1]
