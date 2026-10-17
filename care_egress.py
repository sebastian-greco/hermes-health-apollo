"""Scoped HTTP client for Casita's central Home Assistant conversation egress.

The adapter calls only ``script.casita_conversation_egress``. It never accepts
destination IDs from a reminder/model payload, and it treats a transport failure
after request submission may have started as uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit, urlunsplit

from .care_reminders import DeliveryResult

MAX_RESPONSE_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 4096
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_ACTOR_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,128}\Z")


class CareEgressError(ValueError):
    """A local configuration or payload error safe to show to the host."""


@dataclass(frozen=True)
class TrustedConversation:
    """Host-configured route and patient binding; never model supplied."""

    actor_id: str = field(repr=False)
    scope_id: str = field(repr=False)
    config_entry_id: str = field(repr=False)
    conversation_id: str = field(repr=False)

    def __post_init__(self):
        if (any(not isinstance(value, str) or not _ACTOR_ID.fullmatch(value) for value in (self.actor_id, self.scope_id))
                or any(not isinstance(value, str) or not _ID.fullmatch(value)
                       for value in (self.config_entry_id, self.conversation_id))):
            raise CareEgressError("invalid trusted conversation binding")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_post_json(url: str, headers: dict[str, str], body: bytes,
                    timeout: float) -> tuple[int, bytes]:
    """POST to HA without redirects and with a strict response-size bound."""
    request = Request(url, data=body, method="POST", headers=headers)
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    except Exception as exc:
        # Do not include exception strings: urllib errors can contain private URLs.
        raise OSError(type(exc).__name__) from None
    with response:
        if 300 <= response.status < 400:
            return response.status, b""
        data = response.read(MAX_RESPONSE_BYTES + 1)
        return response.status, data


class HAConversationEgress:
    """Use the fixed HA script with trusted credentials and a fresh route check.

    ``authorize_current`` receives actor ID, scope ID, and the complete trusted
    route on every operation immediately before HTTP egress. The host should
    rebuild routes from current bindings; a stale callback must fail closed.
    ``http_post`` is an injectable transport for tests and accepts
    ``(url, headers, encoded_json, timeout)``.
    """

    def __init__(self, *, ha_url: str, token_provider: Callable[[], str],
                 route: TrustedConversation,
                 authorize_current: Callable[[str, str, TrustedConversation], bool] | None,
                 http_post: Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]] | None = None,
                 timeout: float = 15.0):
        if not isinstance(route, TrustedConversation):
            raise CareEgressError("trusted conversation binding is required")
        if not callable(token_provider):
            raise CareEgressError("host token provider is required")
        if authorize_current is not None and not callable(authorize_current):
            raise CareEgressError("route authorization callback is invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise CareEgressError("HTTP timeout must be between one and sixty seconds")
        self.url = _service_url(ha_url)
        self.token_provider = token_provider
        self.route = route
        self.authorize_current = authorize_current
        self.http_post = http_post or _http_post_json
        self.timeout = float(timeout)

    def send(self, payload: dict[str, Any], *, client_request_id: str) -> DeliveryResult:
        """Submit one immutable keyed reminder and preserve HA/WAHA semantics."""
        try:
            request_data = self._request_data(payload, client_request_id)
        except CareEgressError:
            return DeliveryResult("not_sent", detail="invalid_egress_payload")
        if not self._authorized():
            return DeliveryResult("not_sent", detail="current_route_not_authorized")
        try:
            token = self.token_provider()
        except Exception:
            return DeliveryResult("not_sent", detail="ha_token_unavailable")
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            return DeliveryResult("not_sent", detail="ha_token_unavailable")

        body = json.dumps(request_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if not self._authorized():
            return DeliveryResult("not_sent", detail="current_route_not_authorized")
        try:
            status, raw = self.http_post(self.url, headers, body, self.timeout)
        except Exception:
            # The request may have reached HA/WAHA even if the client saw an error.
            return DeliveryResult("unknown", detail="ha_request_outcome_uncertain")
        if len(raw) > MAX_RESPONSE_BYTES:
            return DeliveryResult("unknown", detail="ha_response_too_large")
        if not 200 <= status < 300:
            # The action may have run before HA or the connection failed.
            return DeliveryResult("unknown", detail=f"ha_http_{status}")
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return DeliveryResult("unknown", detail="ha_response_invalid")
        result = response.get("service_response") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return DeliveryResult("unknown", detail="ha_service_response_missing")
        if (result.get("client_request_id") != client_request_id
                or result.get("conversation_id") != self.route.conversation_id):
            return DeliveryResult("unknown", detail="ha_response_correlation_mismatch")
        status_text = result.get("status")
        if not isinstance(status_text, str):
            return DeliveryResult("unknown", detail="ha_response_status_unrecognized")
        if status_text == "accepted":
            message_id = result.get("message_id")
            if message_id is not None and (
                not isinstance(message_id, str) or not _ID.fullmatch(message_id)
            ):
                return DeliveryResult("unknown", detail="ha_response_message_id_invalid")
            replayed = result.get("replayed")
            if replayed is not None and type(replayed) is not bool:
                return DeliveryResult("unknown", detail="ha_response_replay_flag_invalid")
            # 'sent' in the reminder ledger means accepted by the central route;
            # it does not mean delivered or read by WhatsApp.
            return DeliveryResult("sent", provider_message_id=message_id,
                                  detail="accepted_replayed" if replayed else "accepted")
        if status_text == "failed":
            return DeliveryResult("not_sent", detail="central_egress_failed")
        if status_text in {"pending", "unknown"}:
            return DeliveryResult("unknown", detail=f"central_egress_{status_text}")
        return DeliveryResult("unknown", detail="ha_response_status_unrecognized")

    def lookup(self, *, client_request_id: str) -> DeliveryResult:
        """Report unknown: the deployed central contract has no lookup endpoint."""
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 128:
            return DeliveryResult("unknown", detail="invalid_lookup_key")
        # Reissuing send with the same key could recover a settled WAHA result,
        # but this lookup contract has no immutable payload to replay. Do not
        # construct one from current state or silently alter the original.
        return DeliveryResult("unknown", detail="central_egress_lookup_unavailable")

    def _authorized(self) -> bool:
        if self.authorize_current is None:
            return False
        try:
            return self.authorize_current(
                self.route.actor_id, self.route.scope_id, self.route
            ) is True
        except Exception:
            return False

    def _request_data(self, payload: dict[str, Any], client_request_id: str) -> dict[str, Any]:
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 128:
            raise CareEgressError("invalid stable request ID")
        if not isinstance(payload, dict):
            raise CareEgressError("reminder payload must be an object")
        if payload.get("reminder_kind") not in {"medication", "manual"}:
            raise CareEgressError("unsupported reminder kind")
        message = payload.get("message")
        if not isinstance(message, str) or not 1 <= len(message) <= MAX_MESSAGE_CHARS:
            raise CareEgressError("reminder message is outside the supported size")
        return {
            "schema_version": "casita-egress-v1",
            "config_entry_id": self.route.config_entry_id,
            "conversation_id": self.route.conversation_id,
            "client_request_id": client_request_id,
            "message": message,
            "origin": "application",
            # Health content must not become an outbound-observation transcript.
            "content_redacted": True,
        }


def _service_url(ha_url: str) -> str:
    if not isinstance(ha_url, str) or len(ha_url) > 2048:
        raise CareEgressError("host HA URL is invalid")
    try:
        parsed = urlsplit(ha_url)
        # Current Casita configuration uses WSS for the authenticated HA host.
        # REST calls use HTTPS on the same authority; never downgrade a token.
        if (parsed.scheme not in {"wss", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise CareEgressError("HA URL must be a credential-free WSS/HTTPS authority")
        # Force validation of an explicitly configured port before credentials
        # are read or a request is attempted.
        _ = parsed.port
        netloc = parsed.netloc
    except ValueError:
        raise CareEgressError("host HA URL is invalid") from None
    return urlunsplit(("https", netloc, "/api/services/script/casita_conversation_egress",
                       "return_response", ""))
