# Bounded document vision extraction

`care_extract.extract_document(workflow, llm, job_id, *, request_id, pages=None,
authorize_current=None)` sends prepared page previews to the existing host-managed
Hermes inference facade. It saves source-linked **unconfirmed proposals** in
Apollo's scoped database. It does not append journal entries, create/confirm
medicine plans, activate reminders, report doses taken or execute document text.

The trusted host supplies the already-authorized `DocumentWorkflow`, its own
`hostctx.llm`, a stable transport/action request identity and preferably its
current-grant callback. Neither a model nor a document supplies a scope, actor,
path, provider credential or Drive folder. The runtime passes
`authorize_current=runtime.authority_current` to recheck grants before reading
media, calling inference and persisting results. Every database connection also
checks the immutable Apollo scope marker. An already authorized request cannot
recall image bytes sent before a later revocation, but it saves no new proposal
after the revocation check fails.

```python
job = workflow.prepare(job_id)
result = extract_document(workflow, hostctx.llm, job["job_id"],
                          request_id=stable_action_id, pages=[1, 2],
                          authorize_current=runtime.authority_current)
```

Use the reviewed provider/model configured by the host. This is actual image
inference when enabled, so prepared medical-page images go to that host-selected
provider under the host's authentication and privacy setup. There is no separate
provider login, model override, speculative text-only fallback or network account
setup in this module. A provider that cannot accept vision returns a safe failure;
the module creates no proposals from that failed response.

## Verified native image contract

The compatibility baseline is Hermes 0.22.2 source
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`, specifically
`agent/plugin_llm.py`: `PluginLlmImageInput`, `_normalize_input_block`,
`_image_part`, `_build_structured_messages` and `complete_structured`.
The plugin-boundary input uses the actual facade shape:

```python
{"type": "image", "url": "data:image/png;base64,...",
 "mime_type": "image/png", "file_name": "page_1.png"}
```

The facade subsequently builds the provider's OpenAI-shaped `image_url` content
part. Passing that downstream shape directly as plugin input would be incorrect.
Each image follows a text block identifying its output page number. Images come
only from `workflow.artifact(job_id, page_number=N)`, whose hash is rechecked
against the immutable preparation manifest. Arbitrary source files, image URLs,
patient identifiers or model-selected hashes cannot enter this path.

## Bounds and source semantics

A call sends at most five distinct output pages, at most 8 MiB per PNG and 20 MiB
of raw image bytes in total. Base64 encoding adds its normal transport overhead.
No resizing, character regeneration or geometric transform happens in extraction;
the deterministic document preparation owns those transformations and preserves
the original. Documents longer than five pages require explicit batches; there
is no silent truncation. The receipt lists selected and remaining pages, and
retains unreadable pages and model-reported warnings.

The output schema requires a bounded list of proposals with output page, kind,
excerpt, literal quote, reading confidence between zero and one, and the constant
state `unconfirmed`. Optional structured data is a bounded scalar mapping.
Confidence is a provider estimate, not clinical verification or proof the quote
is accurate. Unreadable pages cannot simultaneously carry proposals. Page
numbers must refer to a page actually sent. Quotes/excerpts must contain text;
nonfinite numbers, extra fields, confirmed states, nested unbounded data and
out-of-batch citations fail validation before proposal writes. The plugin
mandatorily validates the parsed response with JSON Schema even if the facade
would skip optional validation. Output is capped at 64 KiB, twenty proposals and
ten short warnings, with a 14,000-byte bound per proposal before fixed source
provenance is added; inference requests a 6,000-token output limit and 60-second
host timeout.

The saved proposal records the original source index/page and links the prepared
output page/hash, extraction run and pipeline version. Thus reordered or cropped
output pages still point back to the correct immutable original. Model-returned
patient or source-selector fields are rejected; the trusted manifest supplies
provenance. An extracted medication instruction remains an unverified reading of
a document. Human review against the linked original and the separate verified
plan workflow remain necessary before any treatment/reminder action.

## Durable replay and failure

`care_document_extractions` lives in the same scoped Apollo SQLite database as
document jobs and generic records. Request identity is bound to actor, job,
prepared derivative, selected page order/hashes and extraction version. Changed
content/page selections under the same request conflict. A private nonblocking
POSIX advisory request lock prevents concurrent inference for one request.

The complete validated provider response is saved before applying proposals.
Each call to `workflow.propose` has a deterministic extraction request reference.
If the process stops during proposal application, replay resumes the saved
response and reuses prior proposals; it does not invoke vision again. A completed
request returns its durable receipt. A crash before saving a validated response
may repeat inference and its cost, but cannot promote facts. Provider/validation
failures save only a safe error code and no proposals from the invalid response.
A provider may still hallucinate a schema-valid quote; source links and explicit
unconfirmed state expose that limitation rather than claiming verified OCR.

Private zero-content request lock files and extraction responses need the same
host retention/backup policy as other document artifacts. No independent journal,
background poll, account request or production installation is created.

## Synthetic checks and external validation

Tests generate images and use the actual image → PDF → PNG-preview workflow,
then inject a deterministic vision facade. They verify real image blocks,
mandatory schema validation, page/source mapping after reordering, replay after
partial proposal writes, bounds, unreadable pages, lack of journal/plan effects,
provider-without-vision failure, concurrency, foreign-scope rejection and grant
revocation during inference. With `HERMES_SOURCE` pointing at the clean pinned
source, an additional check loads the actual `agent/plugin_llm.py` facade and
verifies its downstream image content using an injected caller. It sends no
network request and requires no Hermes installation.

Before production use, submit a synthetic page through the approved real vision
provider, compare quote/excerpt/page mapping against the preview and original,
and confirm that the only persisted results are unconfirmed proposals. A provider
that silently ignores images cannot be identified reliably from a valid JSON
shape alone; this real synthetic check is required for each configured vision
route. No real patient document, provider account or network call was used in
source-development tests.
