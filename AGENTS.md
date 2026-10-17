# Casita development on Hermes Health Apollo

This private derivative starts from Infinite-Labs-AI/hermes-health-apollo at
`9f2db670a2669c9f3401fecb3b547ed52bcb2c3b`. Read `README.CASITA.md`,
`docs/casita/PLAN.md` and upstream `CONTRIBUTING.md` before implementation.

## Confirmation and current authorization

Treat brainstorming, questions, preferences and conditional suggestions as
discussion. Get explicit confirmation before repository/fork creation/deletion,
visibility changes, changing the agreed foundation, discarding/reorganizing
work or launching an independent implementation session. Verify a conditional
premise before acting. Do not infer blanket approval from a past preference.

On 2026-10-17 the user explicitly authorized end-to-end implementation of this
Apollo-based plan, including sub-agent delegation, phased work, review, tests,
fixes and continued iteration through completion. This authorization permits
task branches and reviewed commits/pushes to this private origin. It does not
authorize publishing a public fork merely because the user thought private
plugins might be unsupported: private/local Hermes installs are supported.
Keep real account consent, new structural decisions and live activation separate.

## Apollo foundation

Extend Apollo's existing code, SQLite store/migrations, source provenance,
semantic events/observations, queries and food logging. Do not create a second
independent health datastore, port healthcare-mcp or replace Apollo with the
earlier from-scratch care service. If a concrete limitation requires a change
of foundation, report it before changing direction.

Use a generic categorized health log: common patient/time/source/episode fields
plus bounded versioned JSON data and category-defined validation. New diary
categories must be configurable without a new table/tool/parser for each one.
Food can keep its existing Apollo implementation. Verified medication plans,
actual administrations and reminder delivery retain distinct semantics.

Keep Casita's parent catalog small; the health specialist loads its own context
and care tools. Trusted actor/route bindings enforce patient grants. Profile
names, privacy metadata or model-supplied IDs are not authorization. Use the
same code for all patients and the shared child scope.

## Development boundary

All writes belong to this Git root. The surrounding `/homeassistant` directory
is production HA configuration: do not edit it, `/addons`, live profiles,
`.storage`, running services or devices. Do not install or run Hermes on the
personal computer. A throwaway test home and synthetic fixtures on this server
are permitted; real patient files and account credentials are not.

Keep runtime records, custom private category definitions, tokens, folder IDs
and medical documents outside Git. Preserve upstream MIT attribution and keep
`upstream` read-only in practice: push only to the private `origin` repository.
No upstream publication or production activation is authorized by source work.

## Workflow

Inspect Git status/root and preserve others' changes. Search existing issues
before implementation. Use a `codex/` task branch, focused diffs and meaningful
tests following upstream contribution guidance. For documentation-only changes,
`git diff --check` and a sensitive-data review are sufficient. Commit all task
changes before handoff. Push task branches only to the private origin; no merge,
tag, history rewrite or live activation without a separate request.

The current task is the explicitly authorized end-to-end implementation and
review. Continue through generic logs, connected history/reports, specialist
integration, document/Drive intake, verified reminders, shared caregivers and
installation/compatibility evidence. Report genuine external consent or live
activation blockers accurately; do not stop merely after phase one.
