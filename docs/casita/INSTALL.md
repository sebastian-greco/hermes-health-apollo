# Casita care operator setup

This plugin keeps Apollo's database, documents, OAuth state and tokens on the
Hermes host. Cloud model calls receive only the bounded specialist context;
Google OAuth, reports, file intake and reminder delivery are host operations.
This install procedure is for the private Hermes instance on the Home Assistant
mini PC. It does not install Hermes on a personal computer and does not connect
to a real account during development or testing.

## Install the pinned plugin

Use the private `sebastian-greco/hermes-health-apollo` source checkout on the
HA/Hermes host. Keep its Git remote private and install the exact reviewed
commit into the active Hermes profile using the repository's local install
workflow (`make install-local`, then `make verify-local-install`). Do not install
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

Configure the plugin's native Hermes settings with trusted bindings. A binding
is a host-maintained identity and permission grant; it is never inferred from
the model's text. Use the actual WAHA actor and native Hermes chat alias from the
existing integration when configuring the live profile. The following values
are deliberately synthetic and will not route to any real conversation:

```yaml
care:
  enabled: true
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
scope and binding later. A shared Elio-care group must be an intentionally
configured shared scope with both authorized caregivers; do not point private
adult bindings at that shared scope. The host must check WhatsApp sender and
conversation identity against these configured bindings before invoking the
care specialist.

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
