# Hermes native health child compatibility

The native health specialist targets Hermes **0.22.2**, source revision
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`. The implementation is a
version-pinned compatibility shim around Hermes internals; it does not use
`PluginContext.llm` structured completion and does not implement a second model
loop.

## Integration design

Hermes registers one parent tool, `consult_health` in `casita_health`. When a
trusted, admitted WAHA message is selected, the plugin persists the Apollo job
receipt and constructs a real Hermes `AIAgent` child. The child is launched via
Hermes' own single-child execution path and durable asynchronous delegation
queue. Hermes continues to own the agent loop, native session/transcript,
provider credential leasing, progress lifecycle, cancellation and parent
completion delivery.

The shim subclasses Hermes' `AIAgent` only to control prompt assembly and the
health-specific authorization/context-budget stop behavior. It uses Hermes'
native child runtime resolution with no model/provider/base URL/API key/ACP
override. The child inherits the active parent's configured model and native
credentials. It is a native Hermes agent, not a prompt-only skill or custom
planner.

The child gets the dedicated `casita_health_specialist` toolset containing only
bounded `health_*` Apollo actions and `health_finish`. The parent catalog is not
mutated to make those tools available: the shim selects the child-only raw
Hermes schemas for this child and restores the global tool-resolution state.
The child disables delegation and general memory, skips soul/workspace/context
files and parent prefill, and assembles its own health persona with bounded
Apollo context. It does not inherit Casita's full chat transcript, house
controls, shell, browser, general MCP/account tools or message-sending tools.

The child has its own Hermes session and transcript in the Hermes session
database. Its clinical context is not automatically inserted into Casita's
general memory manager. Apollo's scoped SQLite remains the durable health record
and job/action-receipt store. On completion the native queue receives only a
small job reference; the parent must retrieve current status under a fresh
trusted route before it can report verified receipts or a question.

## Authorization and failure boundaries

- Before launch, the host binds the durable Apollo job to the exact native child
  object, Hermes session ID and native subagent/task identity. Model arguments
  cannot supply or forge those values.
- Before each inference request and tool action, the host checks that the child
  object remains current and that the exact source actor/route grant still
  authorizes the scope. Revocation signals native interruption; a withdrawn
  grant does not wait through Hermes' provider retry ladder.
- A child can run at most 12 model iterations by default. Its selected health
  context is bounded before launch. If Hermes reaches a context-compression
  boundary, the shim stops the run instead of sending private context through
  an unguarded auxiliary compression provider. Already committed Apollo records
  remain; the job is left uncertain as appropriate and is never silently
  retried or relaunched after restart.
- The native async ledger and progress callbacks carry neutral job metadata,
  never the admitted message, clinical results, child prose or transcript. The
  child transcript itself remains in Hermes' child session storage.
- Cancellation first changes Apollo job state/revokes actions, then asks Hermes
  to interrupt the child. A late child result after cancellation or grant
  revocation is suppressed. Restart recovery marks abandoned running work
  `unknown`; it does not infer that an external effect did or did not happen.

The source continues to use provider/model selection from the Hermes profile.
This does not mean the plugin can use every subscription's authorization method:
the child uses the provider credentials Hermes has already resolved for the
parent session.

## Compatibility evidence and tests

Plugin release `0.1.0+casita.3` fixes activation on this baseline: Hermes' native
workspace resolver considers plugin extras alongside core's dependency groups,
even when development tools are not selected for installation. Apollo's former
`pytest>=8,<9` constraint conflicted with Hermes' `pytest==9.1.1` pin. The plugin
now supports pytest 8 and 9; core's dependencies and installer are unchanged.

Admission was reproduced with the real pinned `pm.workspace._generate_pyproject`
and uv `0.12.3`, seeded with the unchanged Hermes release lock. The old metadata
fails with that exact pytest conflict; the corrected metadata locks 330 packages,
preserving every existing core package version. The added compatibility check
reads the pytest pin from the supplied Hermes source rather than duplicating it.

The focused compatibility tests use the actual pinned Hermes source with
network access denied and synthetic model responses. They are intended to
exercise these distinct layers:

- `tests/test_care_pinned_hermes.py` uses Hermes Plugin Doctor, actual plugin
  discovery/registration, native hook admission, registry dispatch and
  host-bound ContextVars. A fake runner isolates the admission and Apollo
  authorization protocol without pretending to test background execution.
- `tests/test_care_native.py` constructs a real pinned Hermes parent and child
  `AIAgent`, drives Hermes' native model/tool loop with a deterministic
  non-streaming transport, and exercises its async completion queue, child
  identity, cancellation, credential inheritance and context isolation.
- The production-plugin fixture in that file loads the staged plugin with
  Hermes Plugin Doctor, runs the actual parent `consult_health` and registered
  child `health_*` handlers, verifies Apollo writes/projections and retrieves
  status. Network sockets are denied; responses and patient details are
  synthetic.
- `tests/test_care_plugin.py` checks the separate reminder-worker setup and
  fixed central HA egress mapping with synthetic host adapters. A separate
  private Home Assistant app/observer patch owns the WAHA communication ledger;
  it is not agent logic and is not made live by these Hermes tests.

To run the SDK-bound tests, provide a clean checkout of the exact Hermes pin.
With `HERMES_SOURCE` unset they are skipped:

```sh
HERMES_SOURCE=/path/to/clean/pinned-hermes \
  python -m pytest -o addopts='' tests/test_care_pinned_hermes.py tests/test_care_native.py -q -s
```

`HERMES_COMPAT_EXTRA_PATH` may add an explicitly selected development
site-packages directory when the pinned SDK and synthetic dependencies are in
separate environments. The final revision's focused and full suite results must
be evaluated together: `0.1.0+casita.3` passed 377 tests with zero failures,
errors or skips on Python 3.14.8 and Hermes' exact pytest 9.1.1 pin, including
both real-native execution fixtures.
Wheel/sdist and secret scans passed. The app-side observer passed 66 component
checks (including its real native-ledger probe) plus six native server checks.
These are synthetic source checks, not a rebuilt/deployed image or real accounts.

## Deployment limits

Native child transcripts are Hermes profile-level data. The parent's
`session_search` hides subagents from browsing/discovery, but an explicit known
session ID can still read a child transcript without Apollo scope checks.
Apollo grant revocation therefore does not retract existing Hermes transcripts.
This pilot enrolls one adult. Before private adult scopes share one Hermes
profile, fence those general transcript reads or use separate profiles/stores;
Apollo's patient grants alone do not supply that broader isolation.

This is a source-level Hermes integration, not evidence of a live cloud model
request or production activation. Tests do not prove real subscription login,
Google consent/Drive access, real WAHA delivery, medical correctness, or
WhatsApp image/document/group transport. The current WAHA ingress remains
text-only. Those require their separate configured services, consent and
reviewed end-to-end verification. No real health records, reminders or
messages are created by the synthetic tests.
