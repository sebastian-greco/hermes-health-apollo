# Continuation brief

The user has now explicitly approved end-to-end source implementation, delegated
phases, review and fixes. Active work is on `codex/end-to-end`; read `STATUS.md`
and inspect the branch before making changes. The archived standalone assignment
must not be restarted. No independent desktop session was launched; collaborators
work under the coordinating HA session. Structural changes and live activation
still require the explicit confirmation described in `AGENTS.md`.

```text
Continue this private Apollo derivative according to AGENTS.md, STATUS.md and
docs/casita/PLAN.md. The existing Apollo code is the required base, not an
optional reference. Begin by mapping its existing store/migrations, source
lineage, semantic events/observations, queries, food path and plugin surfaces
to the smallest extensions needed. Do not create a replacement datastore or
port healthcare-mcp. Bring back concrete blockers before changing foundation.

Implement a generic health log with common identity/time/source/episode fields,
category, versioned structured JSON and configurable category validation. New
categories must not require new tables/tools or a dedicated parser/report path.
Preserve numerical values/units, provenance, revisions and missing-data meaning.
Keep food's existing Apollo implementation and distinct medication-plan,
administration and reminder semantics.

The first working slice is natural intake -> saved Apollo-backed log -> issue/
episode linking -> query/correction -> doctor summary by episode/range/relevant
visit. Integrate a dedicated health specialist with its own instructions and
tool catalog behind Casita's small dispatcher. Prove trusted patient grants and
private/shared-child contexts using synthetic fixtures and real persistence.
Record actual pinned-Hermes compatibility separately from test doubles.

Earlier source/tests are preserved in the separate private hermes-casita-health
repository at 5b28958 / codex/archive-pre-apollo-20261017. Reuse useful pieces
selectively, not its independent database. Work on a codex/ task branch, commit
and push only to the private origin. Never push to upstream, merge, change live
HA/Hermes, access real patient data or copy account credentials. Keep plan/code
status honest and report tested commits, reuse evidence and remaining gaps.
```
