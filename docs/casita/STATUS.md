# Delivery status — 2026-10-17

The user authorized end-to-end Apollo-based source implementation. Work is on
`codex/end-to-end`; this document tracks the source design, not production
activation. Native background-specialist version `0.1.0+casita.2` passed the
full 376-test gate with zero failures, errors or skips, including actual pinned
Hermes parent/child execution. Wheel/sdist, secrets and diff gates also passed.
The separate HA package passed 66 WAHA component/native-ledger checks and six
native server checks. The Docker image has not been rebuilt or deployed here.

## Source architecture now implemented

| Area | Current source behavior |
| --- | --- |
| Apollo data | Reuses Apollo's scoped SQLite database, migrations, provenance, generic categorized records, episodes, reports, documents, Drive support and verified reminders. |
| Parent Casita | Receives only `consult_health` in the `casita_health` toolset. It admits the source message, creates a durable job receipt and returns a reference promptly. |
| Health specialist | Hermes 0.22.2 creates a real native child with its own `casita_health_specialist` toolset. It runs through Hermes' native model/child lifecycle; no second planner is called inside the child. |
| Specialist context | A bounded Apollo-backed snapshot and the admitted source message are supplied to the child. Casita's full chat transcript, general memory, shell, home controls, delegation and general account tools are not copied into its tool catalog. |
| Job lifecycle | Apollo persists job status, source identity, native child IDs, action receipts, completion receipts, questions, cancellation and bounded status history in the same per-scope database. |
| Parent completion | The native asynchronous queue receives a small job reference only. Casita must make a fresh `consult_health` status read to retrieve verified action receipts or a follow-up question. |

The native model child itself is Hermes-managed, not a home-grown runner. The
plugin supplies a version-pinned compatibility shim to construct the named
child and select its restricted catalog. Each child action checks the exact
host-created child and current source grant. The parent receives verified
operation receipts, not free-form child claims or its transcript.

## Job and failure behavior

- A duplicate admitted source returns the existing job instead of starting a
  second child. Each operation uses a stable action ID and stored fingerprint;
  changing arguments under an existing ID is refused.
- An action whose side effect has an uncertain result is not blindly repeated.
  The specialist must preserve that uncertainty for Casita/operator review.
- User cancellation revokes child actions before requesting Hermes to interrupt
  the child. A late completion after cancellation or source-grant revocation is
  suppressed.
- A plugin/process restart does not restart health work automatically. New
  registration reconciles jobs against an empty surviving-child registry:
  orphaned running jobs become `unknown`, and abandoned cancellation requests
  become `cancelled`. The record and completed action receipts remain available
  for an authorized status check.
- The job store has a same-scope follow-up persistence primitive, but the native
  dispatcher does not yet route a new human message back into that same job.
  Until that integration is completed, treat a reply as a new health request;
  prior health context may be included, but it is not a resumed child run.
- No provider credential or provider/model override is accepted from Casita or
  the child. Hermes continues to supply the selected model and authentication.
- Child transcripts remain in Hermes' profile database. Apollo API grants do
  not fence explicit native session reads. This is a single-adult pilot; private
  family enrollment needs a profile/store or transcript-access boundary first.

## Not enabled or proved by source work

- No production Hermes plugin revision has been installed or activated by this
  documentation update. Use the final reviewed private commit when supplied.
- Real actor/scope bindings, person records, Google consent, Drive folder access
  and reminder egress still require operator setup. Keep secrets and private
  identifiers out of Git.
- The current WAHA input adapter is text-only. WhatsApp image/document bytes and
  two-parent group transport are not implemented here; local document intake
  and operator-triggered workflows do not imply live WhatsApp media support.
- No real clinical records, reminders, messages, cloud-model calls or external
  account operations were performed by this source work.
- The source does not claim automatic extraction of real prescriptions, medical
  correctness, or a live doctor/appointment booking integration.

Before deployment, rerun focused tests on the final branch, inspect the clean
wheel and plugin manifest, validate against the pinned Hermes 0.22.2 source,
and review the exact commit/diff. Then configure one private binding and enable
the feature deliberately. Live activation and account consent remain separate
operator actions.
