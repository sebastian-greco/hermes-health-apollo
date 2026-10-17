# Private document intake and Drive filing

`care_documents.py` extends the same initialized Apollo database selected by an
existing authorized `CareSession`. The host supplies the session and a private
absolute runtime directory. It supplies the Drive folder and current OAuth token
provider separately; these are never arguments a model can override. The module
reads no credentials, patient files, environment tokens or live accounts.

Install the optional `documents` dependencies in the isolated host environment:
Pillow for decoding/EXIF/cropping/PDF output, NumPy for deterministic line-projection
deskew, and pypdfium2 for bounded PDF rendering. Missing dependencies produce a
clear error. They are not generative image models or OCR services.

## Host API

```python
adapter = GoogleDriveAdapter(
    folder_id=trusted_scope_config.drive_folder_id,
    token_provider=host_managed_current_access_token,
    # optional injectable transport(method, url, headers, body) -> HTTPResponse
)
workflow = DocumentWorkflow(authorized_session,
                            runtime_root=private_runtime_path, drive=adapter)
job = workflow.intake([DocumentInput("synthetic.png", "image/png", uploaded_bytes)],
                      request_id=trusted_transport_message_id,
                      episode_id=validated_episode_id)
job = workflow.prepare(job["job_id"])
job = workflow.upload(job["job_id"])
```

The host obtains upload bytes from an authorized bounded media transport. Intake
accepts bytes, never arbitrary file paths, URLs, folder IDs, scope IDs or actors.
A writer may initialize document storage; a read-only session may open existing
storage and retrieve authorized jobs and media, but cannot mutate them.

- `intake(sources, request_id, episode_id=None, visit_id=None)` saves immutable
  originals, source rows and source metadata in Apollo `raw_records`, linked by
  Apollo `record_lineage`. Replaying a request from the same actor returns its
  original job; changed content or links conflict. Episode/visit existence and
  kind are checked in the same scoped database.
- `prepare(job_id, pages=None)` creates an ordered legible raster PDF, normalized
  PNG page previews and transformation provenance. `PageEdit(source_index,
  page_number=1, rotate=0, crop=None, deskew=True)` expresses page order and
  explicit geometry. Every original page must occur exactly once. The plan is
  immutable once processing begins; a changed plan requires a new intake request.
  A crash resumes the saved plan even when the caller omits `pages`.
- `artifact(job_id, source_index=None, page_number=None)` returns authorized bytes,
  MIME type and hash: default derivative PDF, original by zero-based source index,
  or output PNG by one-based page number. Hosts transport these as media, not
  JSON text or unrestricted filesystem paths.
- `get(job_id)` returns state, sources, provenance JSON, source reference,
  unconfirmed proposals and upload receipts. It never returns resumable upload
  session URIs. Runtime job data itself remains private.
- `propose(job_id, request_id, source_index, page_number, quote, proposal)` saves a
  bounded unconfirmed extraction with source page and quoted evidence. Extraction
  may come from a separately approved host OCR/model path; this module performs
  none. A source quote is evidence to review, not proof OCR read it correctly.
  This API cannot create/confirm a medicine plan or activate reminders. Reviewed
  facts can subsequently use the normal generic journal with
  `source_ref="document:<job_id>"`; confirmed plans use the separate medication
  workflow and authorization.

Use the host job worker to perform intake → prepare → upload and to return the
resulting receipt. No polling daemon or live integration is installed here.

## Preservation and limits

Original bytes are content addressed by SHA-256 under a host-owned mode-0700
scope directory. Files are mode 0600, atomically published, synced and verified
before reading or upload. Source names cannot contain path separators/control
characters. Symlink directories/artifacts, unsupported types and mismatched MIME
signatures fail closed. Only single-image PNG/JPEG and PDFs are accepted: at most
25 MiB per source, 60 MiB per batch, 20 pages, 20 million pixels per page and
80 million source pixels per rendered batch. Each output artifact is capped at
100 MiB. Runtime storage needs an operator retention/backup policy; no medical
original is automatically deleted. Interrupted writes can leave private temporary
or unreferenced content-addressed files, which require a host maintenance policy.

EXIF orientation and explicit right-angle rotation are deterministic. Explicit
crop coordinates refer to the oriented/rotated page and require human review:
cropping can remove clinically relevant margins. The default preserves all
margins. Deskew estimates a conservative small angle from line projections,
requires a material confidence gain and expands onto a white canvas. It never
reconstructs characters, invents missing text or guesses 90/180-degree orientation
from text. Default ordering follows the batch and PDF page order. Scans whose
orientation is not represented by EXIF require an explicit rotation. PDFs are
rasterized at 150 DPI, preserving the original PDF separately; searchable/vector
PDF semantics are not carried into the derivative. Review the PNG previews for
legibility, order and crop before clinical use.

## Durable upload and reconciliation

Each original and the derivative has a durable upload item in
`care_document_uploads` in Apollo's scoped database. Stable client upload identity
and local artifact hash are written as private Drive `appProperties`. Search is
restricted to the configured folder, ignores trashed files, and requires complete
results. Metadata receipts must match the same folder, client identity and hash.
These hashes record the locally verified intended bytes, not an independent
Google content-checksum assertion. Multiple matching files require an operator;
no duplicate is silently chosen or removed. Folder changes block an existing job.

Files through 5 MiB use multipart upload. Larger files use Drive's resumable
protocol: save the validated session URI in the same database before sending file
bytes; after a restart query its byte range and continue the suffix. The session
URI is private runtime data and is excluded from public job responses. HTTP
requests are HTTPS to `www.googleapis.com`; transport redirects are rejected,
including token-bearing resumable requests. Error diagnostics suppress token,
response-body and transport-exception content.

On an uncertain multipart send, the next attempt searches before sending again.
An empty search leaves the item uncertain, because Drive indexing may lag. It
does not automatically resend. `upload(..., retry_uncertain=True)` is an explicit
trusted operator decision accepting possible duplicate creation after a complete
search. It must not become a model default. Expired/rejected resumable sessions
require operator reconciliation; the implementation intentionally retains the
session and evidence rather than deleting or silently replacing them. This is
replay protection and reconciliation, not an exactly-once guarantee.

The OAuth consent, chosen existing patient folder and account permissions remain
external host setup. No live Google request, consent, upload, real patient intake
or production activation was performed by these tests.

Protocol references: [Drive upload modes and resumable protocol](https://developers.google.com/workspace/drive/api/guides/manage-uploads),
[folder-bounded file search](https://developers.google.com/workspace/drive/api/guides/search-files),
[private appProperties](https://developers.google.com/workspace/drive/api/guides/properties).

## Synthetic validation and rollback

Focused tests drive real PNG/JPEG/PDF libraries and an injectable HTTP transport,
covering original identity, page geometry/order/deskew/EXIF, bounded malicious
intake, cross-scope and permission rejection, source/lineage/proposal persistence,
Drive request shapes and receipts, redirection/folder rejection, replay, uncertain
send reconciliation and resumable progress after process reconstruction. They use
only generated synthetic pages and throwaway Apollo databases.

Before activation, use a synthetic document with the actual approved account and
folder: confirm both original and derivative receipts; compare previews and the
Drive PDF; interrupt a transfer and inspect the durable job before retrying.
Expected non-actions: no medication plan, reminders, dose record, inferred
clinical fact or deletion. Restore the prior source commit to roll back code;
retain the scoped database, originals and upload receipts for reconciliation.
