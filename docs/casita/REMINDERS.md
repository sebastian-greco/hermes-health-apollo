# Medication and reminder lifecycle

This module adds a durable medication-plan and reminder outbox inside Apollo's
existing scoped SQLite database. It is deliberately not registered as a Hermes
tool and does not install a cron job or send a message on its own. A future trusted
host integration must bind an authenticated `CareSession`, choose which operations
the specialist can call, and inject the central WAHA egress configured for the
intended recipient.

## Medication plan states

`propose_plan` stores a draft version with the medication name, clinician/user
instructions, source reference, and an explicitly structured schedule. It does
not create reminder schedules or activate treatment. `activate_plan` accepts only
a `VerifiedPlanConfirmation` receipt minted from an original, explicit human
confirmation by trusted ingress. The model must not create that receipt or call
activation directly. The receipt is bound to one actor, scope, plan, and version;
revised text requires a new confirmation. `revise_plan` supersedes the current
version, cancels its pending reminders, and stores a new draft. `cancel_plan`
disables reminders and retains past plan versions and reported administrations.
If trusted ingress crashes after activating a plan but before acknowledging the
command, replaying the exact same persisted confirmation returns the existing
schedule. A different confirmation, revised version, or cancelled plan cannot
use that replay path.

Instructions are stored as given. This code does not calculate doses, infer
frequency from free text, recommend catch-up doses, or automatically administer
PRN medicine. A reminder acknowledgement records only that the reminder was
acknowledged. `record_administration` is a separate write for an explicit,
source-linked human report. A delivery, read receipt, or reminder acknowledgement
never creates an administration record. Reusing the same idempotency key with
the same report from the same authenticated actor replays the original result;
using it for different content is a conflict. Caregiver idempotency keys are
actor-scoped. Separate message IDs remain separate reports, because similar
reports cannot safely be deduplicated as suspected duplicate doses.

Plan proposal, revision, activation, and cancellation all require a scoped
`CareSession` capability. Actor and scope come from that trusted session, never
from tool/model arguments. Reminder rows also carry the scope ID and all reads and
writes filter it. The host must bind each WhatsApp sender/group route to the
correct care scope and permissions; this module does not establish that mapping.

## Schedule and time behavior

Schedules are structured rather than interpreted from prose. One-time reminders
require a timezone-aware ISO timestamp. Daily medication reminders require IANA
timezone plus one or more local `HH:MM` times and a start date. The generated
occurrence stores its exact UTC deadline and local recurrence key. During a DST
spring-forward gap, a nonexistent wall time is skipped. During a repeated
fall-back hour, the first occurrence is chosen, so one daily reminder is created
for that wall time. The original timezone and local time remain in the versioned
schedule. Changing either requires a new plan version and confirmation.

Occurrences are materialized ahead for eight days and persist in SQLite, so a
worker restart does not lose already materialized reminders. When the worker is
down longer than the configured lateness window (30 minutes by default), overdue
ready occurrences expire. The code never converts an expired dose reminder into
an instruction to take a missed dose. Daily recurrence materialization skips dates
older than the lateness window instead of backfilling an unbounded history. A
host-managed `run_forever(stop_event, wake_event, egress)` worker pumps due work
on startup, waits until the next exact occurrence or materialization deadline,
and wakes early when the host signals `wake_event` after a plan change. Expired
leases are recovered as uncertain sends and looked up, never blindly resent.
Remaining uncertain sends are reconciled in bounded batches with exponential
backoff from one minute up to thirty minutes. When stopping, the host sets both
the stop and wake events so a sleeping worker exits immediately. This is an
in-process service lifecycle, not an installed service, cron job, or HA polling
automation; the host remains responsible for starting and stopping it and for
signaling writes.

Each send gets a stable `client_request_id`. The outbox leases an occurrence,
requires an injected `authorize_current(authority)` callback that checks the
current trusted route grant, and rechecks it plus the plan version, schedule
generation and current plan status just before egress. Without that callback,
dispatch fails closed. It then records the transport result. A known `not_sent`
can be retried explicitly with the same request ID. An `unknown` result is looked
up through egress before any retry; an unknown lookup remains blocked. This is
not exactly-once delivery: an external system may accept a message and lose its
response. Cancellation immediately disables a schedule and marks pending,
leased, and uncertain occurrences cancelled. A late transport result cannot
reopen or mark the occurrence sent, but a message already accepted externally
cannot be recalled; the attempt audit records a known late send separately.

The injected egress adapter must target only its preconfigured recipient. It
must not accept arbitrary destination IDs from the model or reminder payload.
Recipient binding, the freshness callback implementation, actual HA/WAHA setup,
and any live notification delivery are outside this source-only phase. The host
must stop dispatch and revoke/remap its authority when a route grant changes.

## Public service surface

- Admin path: `propose_plan`, `revise_plan`, trusted `activate_plan`.
- Authorized write path: `cancel_plan`, `cancel_manual_reminder`,
  `create_manual_reminder` for an explicit non-medical request,
  `acknowledge_reminder`, `record_administration`, and controlled dispatch.
- Read path: `get_plan`, `list_upcoming`, and `get_reminder` (including attempt
  history).
- Worker path: `run_forever(stop_event, wake_event, egress)` for a host-managed
  lifecycle, plus `dispatch_due(egress)` and `reconcile_unknown(egress)` for
  focused host integration or testing.

All operations use the same Apollo database and an authorized `CareSession`.
Reminder schema creation is additive and is executed inside the session's
transaction. This implementation has synthetic SQLite tests only; Hermes tool
registration, trusted WhatsApp confirmation handling, a concrete egress adapter,
host lifecycle/wake wiring, migration against a real Apollo database, and user
acceptance remain separate integration work.

The additive reminder schema v2 adds optional `case_ids` to
`record_administration`: up to 32 existing issue/episode/visit IDs in the same
scoped Apollo database. Links commit atomically with the human report and enter
its replay fingerprint. Empty links preserve pre-v2 replay identities. The
connected doctor report assembler shows actual reports with their original
plan-version evidence and keeps reminder acknowledgements separate; see
[connected reports](REPORTS.md). No case link changes reminder or treatment state.
