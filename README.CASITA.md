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
Runtime source, tests and local check scripts are unchanged.

## Current state

Apollo is now the required implementation foundation. This preparation adds
instructions and the revised plan only; it does not modify upstream runtime code,
install plugins or activate patient records, account connections or reminders.

- [Plan: categorized health logs on Apollo](docs/casita/PLAN.md)
- [Prepared implementation-session prompt](docs/casita/NEXT_SESSION_PROMPT.md)
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
