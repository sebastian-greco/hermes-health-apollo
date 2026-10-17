"""Host-only, scope-bound Google Drive desktop OAuth and token-provider bridge.

No credentials are discovered or copied from other integrations. Apollo's shared
OAuth primitives supply PKCE, private atomic files, locks, exchange and refresh.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import oauth_token

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
MAX_AUTH_FILE = 64 * 1024


class DriveAuthError(RuntimeError):
    """Public messages contain no credentials, codes, callbacks or provider bodies."""


@dataclass(frozen=True)
class DesktopClient:
    client_id: str = field(repr=False)
    client_secret: str = field(default="", repr=False)

    def __post_init__(self):
        if (not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 1024
                or any(c.isspace() for c in self.client_id)
                or not isinstance(self.client_secret, str) or len(self.client_secret) > 4096
                or any(c.isspace() for c in self.client_secret)):
            raise DriveAuthError("Invalid host-supplied desktop client")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token_http_post(url, data, headers=None):
    """Real form POST to the fixed Google token endpoint, bounded/no redirects."""
    if url != TOKEN_URL:
        raise DriveAuthError("OAuth token destination rejected")
    request = Request(url, data=urlencode(data).encode(), method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        response = build_opener(_NoRedirect).open(request, timeout=30)
    except HTTPError as exc:
        response = exc
    except Exception:
        raise DriveAuthError("Google token request failed; reconnect if necessary") from None
    try:
        with response:
            if 300 <= response.status < 400:
                raise DriveAuthError("OAuth redirect rejected")
            payload = response.read(MAX_AUTH_FILE + 1)
            if len(payload) > MAX_AUTH_FILE:
                raise DriveAuthError("OAuth response exceeds limit")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise DriveAuthError("Invalid OAuth response")
            if not 200 <= response.status < 300 and "error" not in result:
                raise DriveAuthError("Google token request rejected")
            return result
    except DriveAuthError:
        raise
    except Exception:
        raise DriveAuthError("Invalid OAuth response") from None


class DriveOAuth:
    """Trusted setup/host object; never register its methods as model tools.

    ``selected_folder`` requests only drive.file. The host must arrange Google
    Picker/app selection of the existing configured folder. ``existing_folder``
    requests full Drive scope only after explicit host/user approval. Neither
    mode widens itself if access is denied.
    """

    def __init__(self, session, *, runtime_root: Path, client: DesktopClient, folder_id: str,
                 scope_mode: str = "selected_folder", full_drive_approved: bool = False,
                 http_post=None, now=None):
        from .care_store import CareSession
        from .care_documents import ID
        if not isinstance(session, CareSession):
            raise PermissionError("An authorized care session is required")
        session.require("write")
        if not isinstance(client, DesktopClient) or not isinstance(folder_id, str) or not ID.fullmatch(folder_id):
            raise DriveAuthError("Invalid trusted Drive configuration")
        if scope_mode == "selected_folder":
            scopes = (DRIVE_FILE_SCOPE,)
        elif scope_mode == "existing_folder" and full_drive_approved is True:
            scopes = (DRIVE_SCOPE,)
        else:
            raise DriveAuthError("Existing-folder full Drive scope requires explicit approval; prefer selected-folder access")
        if not isinstance(runtime_root, Path) or not runtime_root.is_absolute():
            raise DriveAuthError("A trusted absolute OAuth runtime root is required")
        if runtime_root.resolve().is_relative_to(Path(__file__).resolve().parent):
            raise DriveAuthError("OAuth runtime must remain outside the plugin source tree")
        self.session = session
        self.client = client
        self.folder_id = folder_id
        self.scopes = scopes
        self.scope_mode = scope_mode
        scope_hash = hashlib.sha256(session.authority.scope_id.encode()).hexdigest()
        self.root = runtime_root / scope_hash / "drive_oauth"
        self._token_file = self.root / "drive_token.json"
        self._state_file = self.root / "consent_state.json"
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._post = http_post or token_http_post
        self._binding = {"scope_hash": scope_hash,
                         "client_hash": hashlib.sha256(client.client_id.encode()).hexdigest(),
                         "folder_hash": hashlib.sha256(folder_id.encode()).hexdigest(),
                         "scopes": list(scopes)}
        self._guard("write", create=True)

    @classmethod
    def from_config(cls, session, config, *, runtime_root: Path, http_post=None, now=None):
        """Load only the explicitly configured operator-owned Desktop client JSON.

        Ordinary YAML contains file/folder pointers and scope choice, never client
        secrets or tokens. This trusted setup method is not a model tool.
        """
        allowed = {"client_file", "folder_id", "scope_mode", "full_drive_approved"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise DriveAuthError("Drive configuration must use a protected client_file; embedded credentials are rejected")
        from .care_store import CareSession
        if not isinstance(session, CareSession):
            raise PermissionError("An authorized care session is required")
        session.require("write")
        with session.connection(permission="write"):
            pass
        value = config.get("client_file")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise DriveAuthError("Configure an absolute protected Desktop client JSON file")
        path = Path(value)
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise DriveAuthError("Unsafe configured Desktop client file")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_size > MAX_AUTH_FILE):
                    raise DriveAuthError("Desktop client file must be private, bounded and operator owned")
                raw = source.read(MAX_AUTH_FILE + 1)
                if len(raw) > MAX_AUTH_FILE:
                    raise DriveAuthError("Desktop client file exceeds size limit")
                payload = json.loads(raw)
                client_data = payload.get("installed") if isinstance(payload, dict) else None
                if not isinstance(client_data, dict):
                    raise DriveAuthError("A Google Desktop app client JSON is required")
                client = DesktopClient(client_data.get("client_id"), client_data.get("client_secret", ""))
        except DriveAuthError:
            raise
        except Exception:
            raise DriveAuthError("Configured Desktop client JSON unavailable or invalid") from None
        return cls(session, runtime_root=runtime_root, client=client, folder_id=config.get("folder_id"),
                   scope_mode=config.get("scope_mode", "selected_folder"),
                   full_drive_approved=config.get("full_drive_approved", False), http_post=http_post, now=now)

    def _guard(self, permission="write", *, create=False):
        # This validates the immutable Apollo scope before touching OAuth state.
        with self.session.connection(permission=permission):
            pass
        for path in reversed([self.root, *self.root.parents]):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise DriveAuthError("Unsafe OAuth runtime directory")
            elif create:
                path.mkdir(mode=0o700)
            else:
                raise DriveAuthError("OAuth runtime directory unavailable")
        info = self.root.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise DriveAuthError("OAuth runtime directory must be private and host owned")
        for path in (self._token_file, self._state_file, oauth_token.pending_path(self._token_file),
                     oauth_token.lock_path(self._token_file), oauth_token.lock_path(self._state_file)):
            if path.is_symlink():
                raise DriveAuthError("Unsafe OAuth runtime file")
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > MAX_AUTH_FILE:
                    raise DriveAuthError("Unsafe OAuth runtime file")
                # Shared token_lock creates locks with ambient umask; they carry
                # no token data, but narrow their permissions on each operation.
                if path.name.endswith(".lock"):
                    path.chmod(0o600)
                elif info.st_mode & 0o077:
                    raise DriveAuthError("OAuth runtime file must be private")

    def _load(self, path):
        try:
            data = oauth_token.load_token(path, missing_exc=DriveAuthError,
                                         missing_message="Drive consent required", invalid_message="Invalid private OAuth state")
            if path.stat().st_size > MAX_AUTH_FILE:
                raise ValueError
            return data
        except Exception:
            raise DriveAuthError("Drive consent required or private OAuth state invalid") from None

    def begin(self, *, redirect_uri: str):
        """Begin one short-lived desktop consent. Return only to trusted setup UI."""
        self._guard("admin")
        _loopback_uri(redirect_uri)
        verifier, challenge = oauth_token.generate_pkce_pair()
        state = secrets.token_urlsafe(32)
        deadline = self._now() + timedelta(minutes=10)
        pending = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                   "expires_at": deadline.isoformat(), "binding": self._binding, "status": "pending"}
        with oauth_token.token_lock(self._state_file):
            oauth_token.write_json_atomic(self._state_file, pending, private=True)
        oauth_token.lock_path(self._state_file).chmod(0o600)
        params = {"client_id": self.client.client_id, "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": " ".join(self.scopes), "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256",
                  "access_type": "offline", "prompt": "consent"}
        return {"authorization_url": AUTH_URL + "?" + urlencode(params),
                "expires_at": deadline.isoformat(), "scope_mode": self.scope_mode,
                "requested_scopes": list(self.scopes)}

    def finish(self, callback_url: str):
        """Validate complete loopback callback + state, consume it, save private token.

        Raw codes are intentionally refused because they lose the returned-state
        binding. A failed/uncertain exchange needs a new consent, never a blind
        authorization-code retry.
        """
        self._guard("admin")
        with oauth_token.token_lock(self._state_file):
            pending = self._load(self._state_file)
            if pending.get("status") != "pending" or pending.get("binding") != self._binding:
                raise DriveAuthError("OAuth session unavailable or configuration changed; begin again")
            try:
                deadline = datetime.fromisoformat(pending["expires_at"])
                if deadline.tzinfo is None or self._now() >= deadline:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise DriveAuthError("OAuth session expired; begin again") from None
            code, returned_state = _callback(callback_url, pending["redirect_uri"])
            if not secrets.compare_digest(returned_state, pending.get("state", "")):
                raise DriveAuthError("OAuth state mismatch; begin again")
            # Consume before the endpoint call, persisting failure/replay evidence.
            oauth_token.write_json_atomic(self._state_file, {**pending, "status": "consumed"}, private=True)
            try:
                token = oauth_token.exchange_code_for_token(
                    token_url=TOKEN_URL, client_id=self.client.client_id, client_secret=self.client.client_secret,
                    code=code, redirect_uri=pending["redirect_uri"], http_post=self._safe_post,
                    now=self._now, api_error_exc=DriveAuthError, error_message="Google consent exchange rejected",
                    missing_tokens_message="Offline Drive consent did not return required tokens; begin again",
                    extra_fields={"code_verifier": pending["verifier"]})
                self._validate_token(token)
                token["care_drive_binding"] = self._binding
                with oauth_token.token_lock(self._token_file):
                    oauth_token.save_token(token, self._token_file)
                    oauth_token.pending_path(self._token_file).unlink(missing_ok=True)
                oauth_token.lock_path(self._token_file).chmod(0o600)
            except Exception:
                raise DriveAuthError("Drive consent exchange failed; begin again") from None
            # PKCE material and codes do not remain in completed consent state.
            oauth_token.write_json_atomic(self._state_file, {"status": "completed", "binding": self._binding}, private=True)
        return {"connected": True, "folder_verified": False, "scope_mode": self.scope_mode}

    def _safe_post(self, url, data, headers=None):
        if url != TOKEN_URL:
            raise DriveAuthError("OAuth destination rejected")
        try:
            current = None
            if data.get("grant_type") == "refresh_token":
                # The shared refresh primitive already holds the token lock.
                # Recheck binding inside that lock before submitting credentials.
                current = self._load(self._token_file)
                if (current.get("care_drive_binding") != self._binding
                        or data.get("refresh_token") != current.get("refresh_token")):
                    raise DriveAuthError("Drive refresh configuration changed")
            response = self._post(url, data, headers)
            if not isinstance(response, dict) or len(json.dumps(response).encode()) > MAX_AUTH_FILE:
                raise ValueError
            if response.get("error"):
                if response["error"] == "invalid_grant" and current is not None:
                    oauth_token.save_token({**current, "needs_reauthorization": True}, self._token_file)
                return {"error": "invalid_grant" if response["error"] == "invalid_grant" else "oauth_error"}
            result = {k: v for k, v in response.items() if k in {
                "access_token", "refresh_token", "expires_in", "scope", "token_type"}}
            if "scope" in result and set(str(result["scope"]).split()) != set(self.scopes):
                raise ValueError
            for key in ("access_token", "refresh_token"):
                if key in result:
                    _credential(result[key])
            if "expires_in" in result:
                value = result["expires_in"]
                if type(value) is not int or not 1 <= value <= 86400:
                    raise ValueError
            if result.get("token_type", "Bearer") != "Bearer":
                raise ValueError
            return result
        except Exception:
            raise DriveAuthError("Google token response invalid or unavailable") from None

    def _validate_token(self, token):
        _credential(token.get("access_token"))
        _credential(token.get("refresh_token"))
        if not isinstance(token.get("scope"), str) or set(token["scope"].split()) != set(self.scopes):
            raise DriveAuthError("Drive token scopes do not match approved access")
        if token.get("token_type", "Bearer") != "Bearer":
            raise DriveAuthError("Unsupported Drive token type")

    def _access_token(self):
        """Credential-bearing host capability; never return this from model tools."""
        self._guard()
        current = self._load(self._token_file)
        if current.get("care_drive_binding") != self._binding:
            raise DriveAuthError("Drive token belongs to another trusted configuration; reconnect")
        self._validate_token(current)
        if current.get("needs_reauthorization"):
            raise DriveAuthError("Drive consent expired; reconnect")
        try:
            refreshed = oauth_token.refresh_access_token(
                token_file=self._token_file, token_url=TOKEN_URL,
                client_id=self.client.client_id, client_secret=self.client.client_secret,
                http_post=self._safe_post, now=self._now,
                not_connected_exc=DriveAuthError, api_error_exc=DriveAuthError,
                missing_token_message="Drive consent required", invalid_token_message="Drive consent required",
                missing_refresh_message="Offline Drive consent required", invalid_grant_message="Drive consent expired; reconnect",
                no_access_token_message="Drive refresh failed; reconnect")
            if refreshed.get("care_drive_binding") != self._binding:
                raise DriveAuthError("Drive token binding changed")
            self._validate_token(refreshed)
            return refreshed["access_token"]
        except Exception:
            raise DriveAuthError("Drive token unavailable; reconnect if necessary") from None
        finally:
            if oauth_token.lock_path(self._token_file).exists():
                oauth_token.lock_path(self._token_file).chmod(0o600)

    def token_provider(self):
        """Return current access-token string only to trusted GoogleDriveAdapter.

        Pass the bound method, ``token_provider=auth.token_provider``. Never call
        it from a model tool or put its result into diagnostics.
        """
        return self._access_token()

    def verify_folder(self, *, transport=None):
        """Read-only remote capability check after consent/Picker, called by host."""
        from .care_documents import GoogleDriveAdapter
        self._guard("admin")
        adapter = GoogleDriveAdapter(folder_id=self.folder_id, token_provider=self._access_token, transport=transport)
        result = adapter._request("GET", "https://www.googleapis.com/drive/v3/files/" + self.folder_id + "?" + urlencode({
            "fields": "id,mimeType,trashed,capabilities(canAddChildren)", "supportsAllDrives": "true"}))
        if (result.get("id") != self.folder_id or result.get("mimeType") != "application/vnd.google-apps.folder"
                or result.get("trashed", False) or not isinstance(result.get("capabilities"), dict)
                or result["capabilities"].get("canAddChildren") is not True):
            raise DriveAuthError("Configured folder is not accessible and writable; complete selection/permissions")
        return {"folder_verified": True, "scope_mode": self.scope_mode}

    def status(self):
        """Safe local diagnostics: no token, client, folder, path, code or callback."""
        self._guard("read")
        connected = False
        pending = False
        reconnect = False
        if self._token_file.exists():
            try:
                token = self._load(self._token_file)
                self._validate_token(token)
                connected = token.get("care_drive_binding") == self._binding and not token.get("needs_reauthorization", False)
                reconnect = not connected
            except DriveAuthError:
                reconnect = True
        if self._state_file.exists():
            try:
                state = self._load(self._state_file)
                expiry = datetime.fromisoformat(state.get("expires_at", ""))
                pending = state.get("status") == "pending" and expiry > self._now() and state.get("binding") == self._binding
            except (ValueError, TypeError, DriveAuthError):
                pass
        return {"connected": connected, "consent_pending": pending,
                "reconnect_required": reconnect, "scope_mode": self.scope_mode}


class LoopbackReceiver:
    """Optional desktop callback receiver; binds only IPv4 loopback, never logs.

    Hosts may instead paste the complete redirected loopback URL into finish().
    Keep the listener on the same machine as the system browser, or deliberately
    forward its port; a browser's localhost does not point to a remote server.
    """
    def __enter__(self):
        receiver = self
        self._callback_url = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                expected_host = "127.0.0.1:" + str(receiver._server.server_port)
                if (self.headers.get("Host") != expected_host or self.client_address[0] != "127.0.0.1"
                        or len(self.path) > 16000 or urlsplit(self.path).path != "/"):
                    self.send_error(400)
                    return
                receiver._callback_url = receiver.redirect_uri + self.path.lstrip("/")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write("Autorizzazione ricevuta. Torna alla configurazione privata.".encode())

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = "http://127.0.0.1:" + str(self._server.server_port) + "/"
        return self

    def receive(self, *, timeout_seconds=60):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise DriveAuthError("Loopback wait must be between 1 and 60 seconds")
        deadline = time.monotonic() + timeout_seconds
        while self._callback_url is None and time.monotonic() < deadline:
            self._server.timeout = min(1, max(0.01, deadline - time.monotonic()))
            self._server.handle_request()
        if self._callback_url is None:
            raise DriveAuthError("Desktop consent callback not received; paste the full redirect or wait again")
        return self._callback_url

    def __exit__(self, *args):
        self._server.server_close()


def _loopback_uri(uri):
    if not isinstance(uri, str) or len(uri) > 500:
        raise DriveAuthError("A trusted desktop loopback redirect is required")
    parsed = urlsplit(uri)
    try:
        port = parsed.port
    except ValueError:
        raise DriveAuthError("Invalid desktop loopback port") from None
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
            or not port or not 1024 <= port <= 65535 or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise DriveAuthError("Only an explicit desktop loopback IP redirect is allowed")


def _callback(value, redirect_uri):
    if not isinstance(value, str) or len(value) > 16000:
        raise DriveAuthError("Paste the full desktop callback URL including state")
    parsed = urlsplit(value)
    expected = urlsplit(redirect_uri)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
        raise DriveAuthError("Desktop callback destination mismatch")
    try:
        query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=10)
    except ValueError:
        raise DriveAuthError("Invalid desktop callback query") from None
    if query.get("error"):
        raise DriveAuthError("Google consent was declined or unavailable")
    if len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1:
        raise DriveAuthError("Desktop callback must include one code and returned state")
    code, state = query["code"][0], query["state"][0]
    if (not code or len(code) > 8192 or any(c.isspace() for c in code)
            or not 20 <= len(state) <= 200 or any(c.isspace() for c in state)):
        raise DriveAuthError("Invalid desktop callback values")
    return code, state


def _credential(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(c.isspace() for c in value):
        raise DriveAuthError("Invalid Drive credential response")
