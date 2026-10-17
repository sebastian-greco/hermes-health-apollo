# Central HA conversation egress

`care_egress.py` implements the reminder outbox's `ReminderEgress` interface
using the existing Home Assistant REST call to the single reviewed action
`script.casita_conversation_egress`. It does not call WAHA directly, use a
`notify` entity, or choose a destination from reminder/model input. The route
is host-configured as a `TrustedConversation` containing the actor, care scope,
WAHA config entry, and private conversation ID.

## Request and response contract

The HA script expects `casita-egress-v1` fields: `schema_version`,
`config_entry_id`, `conversation_id`, `client_request_id`, `message`,
`origin: application`, and boolean `content_redacted`. This adapter takes the
exact final text from the durable reminder payload, preserves its stable
`client_request_id`, fixes route fields from trusted configuration, and always
sets `content_redacted: true` because health reminder contents must not be
copied into any future outbound-observation transcript. The HTTP request is
`POST /api/services/script/casita_conversation_egress?return_response`; the
same host authority is converted from the configured `wss://` URL to HTTPS.
Redirects and URL credentials, query strings, fragments, and base paths are
rejected so the bearer token cannot be forwarded to another endpoint.

The HA REST response wraps the script's keyed WAHA result in `service_response`.
The adapter requires both returned `client_request_id` and `conversation_id` to
match the request. `accepted` maps to the outbox's internal `sent` state, which
means accepted by the central egress only; it is not a WhatsApp delivery or read
receipt. A returned `failed` maps to definitive `not_sent`. `pending`,
`unknown`, an unexpected response, HTTP error, timeout, or connection failure
maps to `unknown` unless the adapter knows it did not begin the request (for
example, stale authorization or unavailable token before HTTP). It never
reports uncertain outcomes as success.

The current HA contract has no lookup operation by `client_request_id`. The
adapter's `lookup()` therefore returns `unknown` without network activity. It
does not make a new-key resend or reconstruct a keyed send without the original
payload. As a result, an uncertain request remains blocked until an operator
can establish its outcome through the HA/WAHA records, or a future reviewed
lookup contract is added. Exactly-once delivery is not claimed.

## Trust and credential handling

The host provides the HA URL and a callable token provider; no token is loaded
from this repository, environment, tool argument, or model context. The token
is fetched only after the current-route callback approves the exact actor,
scope, and configured route. Missing callback or a stale/revoked grant fails
closed before token access or HTTP. The callback must re-read current bindings,
not trust the route captured when a plan was scheduled. The adapter repeats
this check for every send.

The bearer token is an ordinary Home Assistant access token, not a per-script
capability. A process holding it can potentially call other HA APIs, so the
host's process/tool boundary remains important. The adapter sends it only to
the configured HTTPS authority, disables HTTP redirects, bounds response size,
and never logs headers, token, request body, or provider response body. The
current HA script enforces the route allowlist as a second check.

This source phase uses an injected HTTP transport in tests and has no connection
to live HA, WAHA, or household accounts. Runtime construction, protected token
provider, route refresh, scheduler lifecycle, and live acceptance remain host
integration responsibilities.
