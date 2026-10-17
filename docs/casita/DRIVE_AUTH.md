# Host-managed Drive consent and token bridge

`care_drive_auth.py` is a trusted setup/host interface, never a model tool. It
supports a Google OAuth **Desktop app** client with PKCE S256, returned-state
validation, a ten-minute single-use consent session and offline refresh. It reuses
Apollo `oauth_token` primitives for PKCE, atomic private JSON, file locks, code
exchange, expiry and refresh sidecars. It does not import Google Health, request
watch/health scopes, scan environment credentials or copy another integration's
tokens. Setup and development tests have not connected any real Google account.

## Trusted configuration

The ordinary host configuration stores only protected-file pointers and scope
choices. The client file must be an absolute, nonsymlink, operator-owned,
mode-0600 Google **installed/Desktop** JSON file, at most 64 KiB. Its custom auth
or token endpoint fields are ignored; fixed Google endpoints are used. Embedded
client secrets, access tokens and refresh tokens in YAML are rejected.

```yaml
care:
  drive:
    client_file: /PRIVATE_RUNTIME/google-desktop-client.json
    folder_id: CONFIGURED_EXISTING_FOLDER
    scope_mode: selected_folder
```

Synthetic placeholders above are not deployable patient configuration. The host
calls `DriveOAuth.from_config(session, drive_config, runtime_root=private_path)`.
The runtime directory must remain outside the source tree. Tokens and short-lived
PKCE state live under a private directory derived from the authorized care scope;
the token binds to scope, client, folder and approved scopes. Configuration swaps
and the Apollo scope marker are checked before returning or refreshing tokens.
Tokens never appear in setup diagnostics, callback responses, exceptions or logs.

Pass the **bound method** `auth.token_provider` as the adapter's callable:

```python
auth = DriveOAuth.from_config(authorized_session, trusted_drive_config,
                             runtime_root=private_runtime_root)
adapter = GoogleDriveAdapter(folder_id=auth.folder_id,
                            token_provider=auth.token_provider)
```

`token_provider()` returns the current access-token string to the trusted adapter
only. It is a credential-bearing capability; never expose its return value to a
model or include it in status output. The factory reads only the explicitly
configured client file. Direct `DesktopClient(client_id, client_secret="")`
construction also supports a host credential manager without any client file.

## Folder access and minimum scopes

`scope_mode: selected_folder` requests only
`https://www.googleapis.com/auth/drive.file`. Google grants this scope access to
files the app created or that the user selected/shared with the app through
Picker or an appropriate app file picker. A configured folder ID alone does not
grant app access. Complete actual folder selection with the same Google project,
client and user, then call `auth.verify_folder()` to check the exact existing
folder is accessible, is not trashed and has `canAddChildren=true`. This module
does not claim to implement Google Picker UI. Newly filed app-created documents
remain available to the app for upload reconciliation; selecting a folder does
not authorize reading every pre-existing child document. See Google's
[Drive scope guide](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
and [desktop/mobile Picker integration](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker).

For an arbitrary existing folder supplied only by ID without Picker/app-file
selection, the supported alternative is explicit
`scope_mode: existing_folder` **and** `full_drive_approved: true`. It requests the
single broad `https://www.googleapis.com/auth/drive` scope, which Google classifies
as restricted and which permits account-wide Drive file access. This flag must
represent an actual reviewed user decision; source implementation does not grant
that consent. The filing adapter still enforces the configured folder locally,
but this is not an OAuth folder boundary. There is no automatic scope escalation
when a narrow token cannot access a folder. A read-only or app-data scope cannot
file documents into the requested normal existing folder. Google's
[scope restrictions and verification requirements](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
apply; review applicable private/testing/public-app requirements during setup.

## Desktop consent

Create a Desktop app OAuth client, enable Drive API, configure its consent screen
and applicable test users, and protect the downloaded client JSON outside Git.
The setup host needs `admin` authorization. Normal document uploads need `write`;
safe local status needs `read`. The flow requests no incremental combined scopes.

```python
with LoopbackReceiver() as receiver:
    consent = auth.begin(redirect_uri=receiver.redirect_uri)
    trusted_setup_ui.open_system_browser(consent["authorization_url"])
    callback_url = receiver.receive(timeout_seconds=60)
    result = auth.finish(callback_url)
# If folder selection is needed, complete the approved Picker workflow here.
result = auth.verify_folder()
```

The receiver binds only `127.0.0.1` on a random available port, validates Host and
peer, never logs request paths, and returns a static no-store response. A setup UI
may instead paste the **complete redirected loopback URL** into `finish()`. Raw
codes, missing/duplicate state/code, mismatched ports/paths, remote destinations,
fragments and expired sessions are rejected. Loopback consent must run where the
browser can reach the listener; a browser's localhost does not refer to a remote
server. Use an explicitly reviewed port-forward or local setup host if needed.
No deprecated OOB redirect or externally exposed callback server is used. See
Google's [desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app)
and [loopback support](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration).

Consent state is consumed before code exchange, so failed or uncertain exchanges
need a new `begin()` rather than replaying a code. Successful completion removes
PKCE material from saved consent state. Token endpoint requests are bounded,
fixed-destination HTTPS with redirects rejected. Tokens are private and refreshed
through Apollo's serialized lock/sidecar algorithm; refresh-token rotation is
preserved. `invalid_grant` persists a safe reauthorization-needed flag and stops
repeated refresh attempts. Host diagnostics report connection/pending/reconnect
booleans and scope mode, never identifiers or credentials. They do not promise a
future API request or access token will remain valid.

## Synthetic verification and external steps

Tests inject token/Drive HTTP responses and a fake loopback server. They exercise
consent URL and PKCE, state/expiry/single-use checks, strict callback destination,
private files, restart refresh and revocation, binding/scope escalation rejection,
protected Desktop client-file loading, no-redirect token transport, folder
capabilities and silent loopback handling. No OAuth browser was opened and no
account, network authorization request or patient file was used.

Actual client registration, user account consent, optional Picker folder
selection, restricted-scope approval when chosen, and a synthetic real-folder
upload remain external deployment checks. Source code can complete the HTTP
protocol and persist its receipts; it cannot grant Google account consent.
