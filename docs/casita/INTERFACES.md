# Casita health interface contract

This describes the current native background-specialist source design. It is
not evidence that a final plugin revision has passed its release review or is
enabled in the live Hermes profile. See [delivery status](STATUS.md) before
installation.

## Components

| Component | Contract |
| --- | --- |
| WAHA | Supplies a message event and trusted native sender/conversation identity. The current adapter is text-only. |
| Casita parent | Sees one `consult_health` tool in `casita_health`. It remains responsible for ordinary conversation and for explaining the specialist's verified result. |
| Hermes native child | A real Hermes `AIAgent` child, launched through the pinned 0.22.2 async delegation lifecycle. It has the `casita_health_specialist` catalog and a bounded Italian specialist prompt. |
| Apollo store | One existing SQLite database per configured scope. It stores health records plus durable job, selected input, action replay and completion receipts; no second job datastore is created. |
| Parent wakeup | Contains a job reference and an instruction to re-read status. It never contains the source message, clinical result, follow-up question or child transcript. |

## Parent tool

Tool name: `consult_health`; toolset: `casita_health`.

The current schema accepts:

```json
{
  "request": "brief routing hint; the admitted original message is authoritative",
  "action": "consult | status | cancel",
  "job_id": "optional persisted job reference"
}
```

`request` is required by the current schema. For `status` and `cancel`, it is
ignored; provide an empty string. A consult is accepted only for an admitted
human message whose source is still in the bounded host admission store and
whose exact actor, platform and native conversation match a configured binding.
The model cannot provide or override the patient scope, actor, route or job's
source identity.

On a new consult, the parent:

1. Persists the selected source receipt and creates an idempotent queued job in
   that source's Apollo scope.
2. Loads bounded, scope-authorized Apollo context, the exact original message,
   attachment references, local received time and short previous-health-request
   context. This is not Casita's general transcript or chat-memory store.
3. Claims the durable job before launching a child, then returns a job reference
   promptly. Duplicate delivery of the same source returns the existing job.

On `status`, Casita receives the job state plus bounded operation receipts and a
follow-up question if one was explicitly returned. A list request (no job ID)
returns recent job IDs and states only. Reads recheck the current requesting
actor's scope and the original source grant. On `cancel`, the current requester
needs write permission; job state is changed before native child interruption
is requested.

## Child tools and data boundary

The Hermes child has only the child toolset `casita_health_specialist`:

- One `health_<operation>` tool per bounded Apollo operation in
  `care_specialist.ACTION_SCHEMAS`, such as record/category/case/report,
  document and reminder operations.
- `health_finish`, which ends the specialist turn and may include one concise
  clarification question.

The child has no delegation or general memory tool, no shell, browser, home
control, email, Drive search, WAHA send or generic MCP tool. Drive/file access is
available only through the bounded document actions and host-configured scope.
It uses Hermes' currently selected model/provider credentials; model/provider
overrides are not accepted as tool arguments. The plugin calls no second model
planner while the child executes its action round.

The child sees only the bounded specialist context and its selected health
message. It must treat source text and document contents as data, use explicit
units/timestamps, avoid inventing doses or reported administrations, and ask
when required details are ambiguous. It cannot activate treatment plans or
manual reminders by claiming a person confirmed them. Activation requires the
matching exact raw human confirmation receipt handled by the host.

Each tool call is bound to the exact host-created child object, native child
session and run/job ID. Every action uses the source's currently configured
scope and a stable `action_id` with an argument fingerprint. Reusing an ID with
different arguments is refused. A prepared write with uncertain outcome blocks
blind retry under a new action ID. Receipts store verified operation outputs;
the child’s free-form prose and transcript are not completion evidence.

## Job lifecycle and restart behavior

Persisted states are `queued`, `running`, `awaiting_user`, `completed`,
`cancel_requested`, `cancelled`, `failed` and `unknown`.

```text
queued ──claim──> running ──verified finish──> completed
                       ├──clarification─────> awaiting_user
                       ├──cancel────────────> cancel_requested ─────> cancelled
                       └──lost/uncertain────> unknown
```

The job store has a primitive to attach a same-scope follow-up to an
`awaiting_user` job and re-queue it, but the current native dispatcher has not
wired that path yet. For now, a human reply starts a new health request; bounded
recent health context can help Casita understand the prior question, but the
child does not resume in-place.

The diagram shows the ordinary path; stale child completion after cancellation
or grant revocation is suppressed. A repeated exact completion is idempotent;
different completion data after commit conflicts. Restart recovery compares
persisted work with Hermes' live child registry. A newly registered plugin has
no surviving child capability, so an abandoned `running` job becomes `unknown`;
an abandoned `cancel_requested` job becomes `cancelled`. Neither is relaunched.
Queued-but-unclaimed work remains queued and requires a later explicit new
request/decision; it is not automatically executed by startup recovery.

Unknown means the system cannot establish what happened. It is not success,
failure or proof a clinical effect did or did not occur. Operators and users
must reconcile uncertainty before repeating consequential work. Reminder
acknowledgment is not a reported dose; an actual administration is a separate
record.

## Notifications and delivery semantics

The native async wakeup contains only `{health_job_id, state}` and an instruction
for Casita to fetch status. The status tool rechecks current route permissions;
if the route was revoked, Casita must not repeat or reveal stored details.
Completion of a Hermes child means only that the child run ended. The job may
still be `unknown` if the finish receipt is missing, an action is uncertain, or
the native result is not successful. No claim of WhatsApp delivery/read status
is made by this child contract.

Background health jobs are distinct from the independently configured reminder
worker and its HA central egress. Reminder delivery remains disabled unless an
operator explicitly configures and enables it.

## Native version pin

The child construction shim follows Hermes source revision
`d923a5eb487a17c0a68801bba9ef0f56c81040a8` (0.22.2). Hermes owns the actual
agent loop, child context/session, completion queue, progress hooks and native
interrupt. Keep the plugin pin and this compatibility shim in sync; a Hermes
update requires rerunning the branch's native compatibility tests and reviewing
the actual child tool catalog before deployment.
