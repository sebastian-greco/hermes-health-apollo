# Delivery status — 2026-10-17

The user explicitly approved end-to-end Apollo-based source implementation,
parallel collaborators, reviews and fixes. Active branch: `codex/end-to-end`.
No independent desktop session, new repository, public fork, visibility change,
production plugin install, account consent or service restart was performed.

## Implemented source

| Phase | Result |
| --- | --- |
| Apollo reuse | Existing SQLite migrations, source/raw-record lineage, semantic events/observations, food and queries extended; no replacement datastore |
| Generic records | Versioned category definitions/JSON, explicit units, immutable corrections, replay protection, issues/episodes/visits and closure history |
| Family permissions | Separate scope databases, exact trusted actor/conversation grants, fresh checks at storage/egress boundaries, shared-caregiver access to one child history |
| Specialist | One parent tool, `consult_health`; separate bounded Italian prompt/context/action catalog through Hermes's supported host inference facade; no provider/credential override |
| Reports | Episode/range/relevant-visit reports with corrected aggregates, provenance, gaps, actual administrations and distinct acknowledgements, unconfirmed documents, bounded evidence and private exports |
| Documents | Immutable originals, ordered crop/rotate/deskew PDF, private page previews, bounded actual vision extraction with unconfirmed source-linked proposals |
| Drive | Protected OAuth using Apollo primitives; configured folders; upload receipts, uncertain reconciliation and resumable large transfers with fresh permission checks |
| Reminders | Exact persisted deadlines, fixed schedules/DST policy, immutable confirmation receipts, separate administrations, leases/outbox, cancellation, restart hydration and central HA egress |
| Operator workflow | Native `health-care` CLI for safe status, reports, Drive consent/verification and explicit local document intake/prepare/upload |

The specialist uses bounded structured completion/action rounds. It is not a
native general-purpose Hermes background child and does not inherit Casita's
transcript, shell, home controls or account tools. Long-term facts and pending
work live in Apollo independently of provider/model/context compaction.
The reminder worker runs independently of a model turn; ordinary specialist
requests currently execute synchronously within the parent delegation tool.

General Casita messages are kept only in bounded ephemeral admission memory.
Only messages actually delegated to health or explicit health confirmations
are persisted in the health ingress journal. Clinical data, artifacts, OAuth
state/tokens and private category definitions stay outside Git.

## Actual boundaries

- Medication activation is absent from the model action catalog. The exact
  proposal content is returned for review; a matching original human command
  confirms that actor/scope/route/plan/version. All model-proposed manual
  reminders likewise require exact human confirmation before scheduling.
- A confirmed reminder does not establish medical correctness; no dose is
  inferred. Acknowledging, submitting or reading a reminder is not taking it.
- HA `accepted` means accepted by the central communication route. Its current
  contract has no lookup endpoint; ambiguous submissions remain uncertain and
  are never blindly resent. No delivered/read claim is made.
- Extraction is a proposal even when its structured output validates. It never
  activates medicine or silently changes the journal. Source review is needed.
- Large reports have scoped streaming export. Bounded model/parent results
  include omission and evidence-count indicators, not fabricated completeness.

## Deployment dependencies, still open

1. Review/install this exact private plugin revision in the existing Hermes
   profile; enable care settings and add `casita_health` to WhatsApp's current
   toolset list. Native plugin/Git auth supports private sources. A Codex model
   subscription login does not automatically provide GitHub repository auth.
2. Enroll the real private route(s) through native Hermes configuration. No
   real patient IDs, grant maps or medical records were enrolled by these tests.
3. Google Desktop client setup and real user consent remain external. Narrow
   `drive.file` needs app-selected access to the existing folder; this release
   has no Picker UI. A folder ID alone cannot grant access. Broader Drive scope
   requires an explicit informed operator decision; no automatic escalation.
4. Current WAHA adapter ingress is private-text-only, and central HA egress is
   allowlisted to the existing private route. WhatsApp photo bytes and the
   two-parent group require generic media/group transport work in those
   repositories. Local operator document intake and the scoped host attachment
   API exist; that is not a claim of live WhatsApp photo/group support.
5. Enable/configure the reminder worker only after reviewing its actual central
   HA destination, fresh bindings and protected token. No real reminders were
   scheduled or sent. Production reload/restart remains a separate operation.

## Validation

Synthetic checks cover persistence/rebuilds, generic categories, concurrent
caregivers, revisions/replays, permission revocation during inference and token
refresh, document processing/Drive failures, confirmation replay under a moving
clock, reminder cancellation/restart/DST, report evidence and package loading.
The actual pinned Hermes SDK is `d923a5eb487a17c0a68801bba9ef0f56c81040a8`
(0.22.2); native registry/admission/ContextVar/LLM-facade checks use deterministic
inference with networking blocked. Live cloud-model accuracy, actual Google
consent/folder access, WhatsApp media/group transport and production activation
are not proved by these checks. The full suite passed 355 tests with zero skips,
failures or errors, including the clean installed-wheel care-mode check. A final
local-time context fix passed 23 focused runtime/plugin/native-SDK checks.
Source/wheel/sdist secret and artifact checks passed. Final commit and evidence
are recorded in the review.

Source rollback is a reviewed prior Git revision. Runtime data needs its own
private backup; do not downgrade migrated databases blindly. Restore the matching
private data backup when rolling back across incompatible schema versions.
