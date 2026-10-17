# Casita's private Apollo derivative

This repository is a private Git copy of
[Infinite-Labs-AI/hermes-health-apollo](https://github.com/Infinite-Labs-AI/hermes-health-apollo),
starting at `9f2db670a2669c9f3401fecb3b547ed52bcb2c3b` (Apollo 0.1.0).
The complete ancestry of that baseline and its MIT license are retained.
`upstream` tracks the original project; `origin` is the private household copy.
Read the original [README](README.md) for Apollo's existing functionality.

The original scheduled GitHub workflow is preserved verbatim under
`.github/upstream-workflows/eval.yml` and in upstream history. It is inactive in
this private copy: the current GitHub credential cannot create active workflow
files, and account permissions have not been expanded for this preparation.
The original workflow remains inactive; local validation is recorded below.

## Current state

The user explicitly authorized the phased implementation on 2026-10-17. Branch
`codex/end-to-end` now extends Apollo's existing storage, migrations, provenance,
projections and food path. It adds scoped generic logs, connected episodes,
doctor reports, a separate specialist context, documents/Drive and durable
verified reminders. Source implementation and deployment readiness are distinct;
no production plugin, patient records or account connections are activated here.

- [Plan: categorized health logs on Apollo](docs/casita/PLAN.md)
- [Current delivery status and remaining deployment dependencies](docs/casita/STATUS.md)
- [Private installation and operator guide](docs/casita/INSTALL.md)
- [Continuation brief](docs/casita/NEXT_SESSION_PROMPT.md)
- [Developer boundaries](AGENTS.md)

The earlier standalone implementation remains intact in private repository
`sebastian-greco/hermes-casita-health` at commit `5b28958`, also preserved as
`codex/archive-pre-apollo-20261017`. It is reference work, not this project's
storage foundation. Reuse useful code/tests selectively after reviewing how
they fit Apollo; do not copy its separate SQLite database wholesale.

## Product direction

Casita delegates to a dedicated health specialist. The specialist uses Apollo's
local store and queries, extended with generic categorized logs, ongoing issues,
illness episodes, visits, doctor summaries, documents and verified reminders.
Common entry fields plus versioned JSON support many diary/measurement types
without hard-coding a feature for every symptom or condition.

Food logging remains reusable Apollo functionality. Watch imports and a new
appointment subsystem are deferred. The first useful flow is conversation ->
saved log linked to a person/episode -> retrievable doctor-ready summary.

Apollo supplies persistent health-data infrastructure. Hermes continues to
provide agent execution and conversation-memory facilities; this project does
not assume Apollo already implements a complete clinical memory or family ACL.
