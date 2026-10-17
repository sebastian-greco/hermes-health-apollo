# Casita care operator setup

This plugin keeps Apollo's database, documents, OAuth state and tokens on the
Hermes host. Casita stays the conversational interface. For a health task,
Hermes starts a native background child with its own bounded health context and
restricted tool catalog; the child uses Hermes' configured model and provider
credentials. Apollo stores the job and verified action receipts. The native
completion wakeup carries a job reference, and Casita must retrieve the current
result through a fresh, authorized status read.

This install procedure is for the private Hermes instance on the Home Assistant
mini PC. It does not install Hermes on a personal computer and does not connect
to a real account during development or testing.

## Install the pinned plugin

Hermes supports private Git repositories; this plugin does not need to become
public. Authenticate GitHub in Hermes's own protected plugin/Git credential
setup, then install the exact final reviewed private commit (replace the
placeholder only after branch review):

```sh
hermes plugins install https://github.com/sebastian-greco/hermes-health-apollo --ref REVIEWED_COMMIT
hermes plugins enable health-data
```

This source line uses plugin version `0.1.0+casita.2` and the Hermes 0.22.2
native-child compatibility surface. The native installer handles declared
dependencies. A host-accessible local Git checkout can instead be installed with its `file:///absolute/path` URL and the
same exact commit. The source-copy `make install-local` workflow is a developer
alternative; it does not install this derivative's added runtime dependencies.
Keep the remote private. Do not install
from a public PyPI package: this private Casita derivative is not published
there. The installed host must match the Hermes SDK version pinned by the plugin
release (currently Hermes 0.22.2); update and validate both together.

The install should report the source commit and whether the checkout was dirty.
Use a clean committed revision for the deployed profile. The plugin's SQLite
database and generated artifacts are stored under Hermes' private profile data
directory, not in this source tree or HA's `.storage`. Back up that Hermes data
directory along with the private source repository. Do not copy tokens or health
records into Git.

## Enable one private binding

Configure the plugin's native Hermes settings with trusted bindings. Every
`care:` fragment below belongs under
`plugins.entries.health-data.settings`, not the root of Hermes configuration.
Enable `health-data` in the existing plugin list. Add the `casita_health`
toolset to the existing `platform_toolsets.waha` list while preserving its
current toolsets; otherwise WhatsApp's allowlist will hide the parent
`consult_health` tool. Do not add `casita_health_specialist` to Casita's general
toolset. Hermes loads that second catalog only for the named health child.
Apply these through Hermes's native configuration interface. A binding is a
host-maintained identity and permission grant; it is never inferred from
the model's text. Use the actual WAHA actor and native Hermes chat alias from the
existing integration when configuring the live profile. The following values
are deliberately synthetic and will not route to any real conversation:

```yaml
care:
  enabled: true
  timezone: Europe/Rome
  bindings:
    - platform: waha
      actor_id: actor-example
      conversation_id: native-chat-example
      scope_id: sebastian-private-example
      permissions: [read, write, admin]
  reminders:
    enabled: false
```

Start with a single private scope for Sebastian. Give Lucila a distinct private
scope and binding later, after choosing separate Hermes profiles/stores or
fencing general explicit child-transcript reads as described in
`HERMES_COMPATIBILITY.md`. Apollo grants do not restrict Hermes' own transcript
reader. A shared Elio-care group must be an intentionally
configured shared scope with both authorized caregivers; do not point private
adult bindings at that shared scope. The host checks WhatsApp sender and
conversation identity against these configured bindings before admitting work
or returning its status. Never accept a patient, actor or scope ID from the
model as an authorization decision.

## Using the background health specialist

Casita's parent tool is `consult_health` in `casita_health`. It accepts
`action: consult`, `status` or `cancel`. A consult uses the exact admitted human
message as its source; the short `request` field is only a routing hint. Hermes
returns a job ID promptly while a native child works in the background. When
Hermes wakes Casita after completion, the wakeup contains only that reference;
Casita must call `consult_health` with `action: status` and the same `job_id`
before reporting saved data or asking the returned question.

Synthetic tool argument examples:

```json
{"request":"Registra il sintomo riferito","action":"consult"}
{"request":"","action":"status","job_id":"0123456789abcdef0123456789abcdef"}
{"request":"","action":"cancel","job_id":"0123456789abcdef0123456789abcdef"}
```

The child has its own `casita_health_specialist` catalog: it can read/write the
configured Apollo scope through bounded `health_*` operations and must call
`health_finish`. It does not receive Casita's full chat transcript, general
memory, house-control tools, shell, browser, delegation or message-sending
tools. It receives only the admitted health message plus bounded relevant
Apollo context. The plugin stores the source/job/action receipts in the existing
scope database, not a second health database.

Job states include `queued`, `running`, `awaiting_user`, `completed`,
`cancel_requested`, `cancelled`, `failed` and `unknown`. A repeated source
message returns its original job. On process/plugin restart, abandoned running
jobs become `unknown` and are not relaunched automatically; inspect the status
and verify any uncertain effects before starting new work. Cancellation revokes
child actions immediately, asks Hermes to interrupt the native child, and
suppresses late results. These states do not establish whether a medicine was
taken; reminder acknowledgment and actual reported administration remain
separate.

Hermes' operator command is `hermes health-care status`. It displays binding
indices and permission flags, never actor IDs, conversation IDs, scope names,
health facts, paths, OAuth credentials or reminder contents. Other commands use
the current trusted binding index shown by the command; they do not accept a
patient or database path from arguments.

## Google Drive setup

First create a Google Desktop OAuth client for the account that owns the
existing filing folder, enable the Drive API and add the account as a test user
if Google's consent setup requires it. Put the downloaded client JSON in a
private host path, outside Git, with owner-only permissions. The configured
folder ID and client file path are operator-managed host settings, not model
arguments. A synthetic configuration example is:

```yaml
care:
  drive:
    sebastian-private-example:
      client_file: /private/casita/google-desktop-client.json
      folder_id: synthetic-folder-id
      scope_mode: selected_folder
```

Then run `hermes health-care drive status --binding 1`, followed by
`hermes health-care drive begin --binding 1`. Open the printed Google consent
URL, approve the requested scope, and copy the full redirect URL from the
browser address bar. Run `hermes health-care drive finish --binding 1` in an
interactive terminal and paste that URL at the hidden prompt. The redirect uses
loopback; the browser may show a connection error because the Hermes host is not
the computer running your browser. The complete redirected URL can still be
copied. Do not paste the callback, authorization code, client JSON, or tokens in
chat or logs. Finally run
`hermes health-care drive verify --binding 1` to make the explicit read-only
folder capability check.

The narrow `selected_folder` scope requires Google to grant this app access to
the existing folder through an appropriate Google file picker or sharing flow.
This release does not implement Picker UI. A folder ID alone does not grant
access. The broader `existing_folder` mode requires explicit approval and
requests account-wide Drive access; use it only after reviewing the separate
scope disclosure in `DRIVE_AUTH.md`.

Each patient scope has a separate local OAuth token. Repeat consent for another
scope if that scope needs its own Drive identity or folder. Existing Drive files
are never scanned automatically.

## Local reports and document intake

`hermes health-care report --binding 1 --start 2026-10-01 --output /private/path/report.json`
exports a selected report into a new file with mode `0600`. Reports contain
sensitive information. Choose a private output directory and preserve the
result securely. Existing output files are never replaced.

For operator-supplied documents, use
`hermes health-care documents intake --binding 1 /private/inbox/referto.pdf`.
The accepted formats are PDF, JPEG and PNG. Intake records source metadata and
stores bytes under the selected local patient scope. Preparing a normalized PDF
and uploading it to Drive are separate explicit operations:

```sh
hermes health-care documents intake --binding 1 /private/inbox/referto.pdf
hermes health-care documents prepare --binding 1 --job-id JOB_ID
hermes health-care documents upload --binding 1 --job-id JOB_ID
```

The combined intake command also accepts `--prepare --upload` together to
request both actions explicitly. A failed or uncertain Drive upload must be
reconciled before retry. The current WAHA integration carries text only; it does
not deliver WhatsApp images or document bytes to this workflow. WhatsApp media
intake needs a separately reviewed bounded attachment transport before it can
be enabled.

## Reminders and live activation

Reminder delivery stays disabled until the operator configures the HA egress
route and explicitly enables its worker. Its route maps the native Hermes chat
alias separately from the opaque Home Assistant conversation destination:

```yaml
care:
  reminders:
    enabled: false
    ha_url: https://home-assistant.example.invalid
    routes:
      - actor_id: actor-example
        scope_id: sebastian-private-example
        config_entry_id: synthetic-ha-entry
        conversation_id: synthetic-ha-direct-conversation
        chat_id: native-chat-example
```

These example values must be replaced with reviewed values from the existing
WAHA/HA setup; do not put tokens in this file. The worker reads the protected
`WAHA_HA_TOKEN` through Hermes' scoped secret store. Care plan activation and
manual reminder creation require a matching raw human confirmation; a model
cannot activate either by asserting that consent was given. Reminder egress is
only to the configured conversation and status `accepted` means queued by HA,
not delivered or read.

No account consent, live reminder, appointment, device action or HA reload has
been performed by this setup guide. After configuration, verify local status,
Drive consent/folder access if needed, and reminder readiness before using the
assistant with real health information. Keep the first real use user-initiated
and check the returned record and destination yourself.
