# Pinned Hermes compatibility evidence

The care integration was exercised against the real Hermes **0.22.2 source
baseline** at `d923a5eb487a17c0a68801bba9ef0f56c81040a8`. The opt-in
`tests/test_care_pinned_hermes.py` checks the checkout revision and requires its
tracked files to be clean. It uses the actual plugin discovery, registration,
configuration reader, admission hook delivery, tool registry, ContextVars and
host LLM facade. It does not import the archived standalone care implementation.

## Reproduction

Use an isolated development environment containing the pinned Hermes SDK and
this plugin's declared dependencies. With `HERMES_SOURCE` unset, ordinary test
runs skip this external compatibility test.

```sh
HERMES_SOURCE=/path/to/clean/pinned-hermes \
  python -m pytest -o addopts='' tests/test_care_pinned_hermes.py -q -s
```

`HERMES_COMPAT_EXTRA_PATH` optionally names an explicitly chosen development
site-packages directory when the pinned SDK environment and synthetic test
dependencies are separate. It is passed as a Python import path, never an
account/configuration path. The verification run here used the existing
throwaway `.venv-compat` interpreter and development packages, with Python 3.14.
This proves the pinned SDK interfaces and source workflow in that environment;
it does **not** claim a package installation test on every supported Python.
Apollo's package metadata currently declares Python 3.11–3.13. Target-interpreter
installation and real deployment remain separate checks.

The test stages only root Python modules, `plugin.yaml`, package assets and
skills into a temporary directory. Hermes Plugin Doctor makes a second temporary
profile and loads that staged directory using its native loader. A small loader
bootstrap writes actual synthetic `config.yaml` before original registration;
`PluginContext.get_config` and host config refresh are never mocked. The child
process has an explicit environment allowlist, temporary HOME/XDG/Hermes homes,
no inherited provider credentials and no project/bundled plugins. Socket connect,
connect_ex and create_connection are denied. All records, actors, routes, meals
and descriptions are synthetic, and temporary files are removed afterward.

## Verified boundaries

- With `plugins.entries.health-data.settings.care.enabled=true`, native
  registration exposes only `consult_health` in toolset `casita_health` and
  registers `post_gateway_admission`. Its sole model argument is `request`.
  Apollo's legacy general tools and the specialist's internal actions are not
  registered in the parent's catalog in care mode.
- Actual `PluginManager.ainvoke_hook("post_gateway_admission", ...)` creates a
  trusted admission receipt in bounded process memory. Only messages selected
  for care or explicit confirmations are subsequently persisted; unrelated
  general Casita conversation is not copied into health storage. Supplying native session identity without that
  receipt fails. Environment-only identity also fails.
- Actual registry dispatch reads host-bound session ContextVars. Unknown actor,
  chat, message or native session keys and model-supplied scope overrides fail
  before inference. Cleared contexts cannot reuse a prior receipt.
- Both in-process `delegated_child_context()` and a subprocess delegation marker
  fail closed, even with otherwise valid copied native session identity. The
  test exposed the initial process-marker gap; the bridge now uses the pinned
  SDK's `is_delegated_child_process_context()` predicate.
- A plain worker thread lacks a bound native turn and fails. Explicit
  `copy_context().run(...)` propagation succeeds through synchronous native tool
  dispatch. Thus the host's actual worker context behavior matters; process-wide
  environment values are not a substitute for native ContextVars.
- Actual `ctx.llm` is a real `PluginLlm`. Only its low-level inference caller is
  injected with deterministic responses. Provider/model/profile overrides and
  auxiliary task overrides remain unset, and the real facade builds/validates
  the provider-neutral structured action schema. The specialist schema contains
  no terminal, HA device action, activation or confirmation command.
- The native turn creates an issue and episode, saves a generic log in Apollo,
  reuses Apollo's food path for a narrative meal without fabricated calories,
  retrieves and corrects the log, and returns a factual episode report. SQLite
  assertions verify current heads, immutable revisions, source/raw lineage,
  canonical events and connected food links. Replay performs no new inference
  or duplicate writes.
- During a later real facade call, the synthetic provider changes the actual
  temporary config to remove write permission. The refreshed host binding
  rejects the proposed write after inference, leaving the patient history
  unchanged. This checks actual config cache refresh as well as runtime guards.

## Supported integration shape and limits

The general Casita agent receives one dispatcher. Trusted native admission and
route bindings select a per-scope Apollo database. A bounded specialist prompt
and finite action schema run through the host-managed `ctx.llm` facade; trusted
Python handlers execute authorized operations. This is an explicit specialist
runner, not a `delegate_task` child inheriting all parent tools. Ordinary Hermes
child lifecycle APIs cannot grant a private health catalog the parent never
loads; prompt text alone does not establish an isolation boundary.

The directory plugin and packaged entrypoint share the same source modules.
This SDK test stages the installable directory form; wheel contents/entrypoint
registration are covered by the separate package tests. Existing Apollo mode
remains available when care mode is disabled, and no live profile is changed by
these tests.

This evidence does not exercise network inference, Google consent/Drive filing,
actual reminder delivery, native media bytes, a live gateway conversation or
production activation. Those require explicit configured adapters/account
consent and reviewed deployment. The current text-only WAHA path cannot supply
attachments; document intake remains unavailable until a trusted media host
provides admitted bounded bytes. No claim of successful real account upload or
notification delivery follows from a deterministic no-network test.
