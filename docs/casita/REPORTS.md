# Connected factual doctor reports

`care_reports.assemble_report(session, ...)` extends the existing Apollo-backed
`CareSession.report`. The journal, food records, care cases, medication records
and document provenance remain in the same trusted per-patient Apollo database.
There is no replacement datastore, network lookup, generated clinical conclusion,
medication activation or reminder send in report assembly.

The trusted host supplies a `CareSession`; patient, actor and database paths are
never report arguments. A read grant is required. Shared caregivers see the
same corrected child history; separate adult databases remain separate. The
runtime refreshes route grants before invoking this assembler.

```python
from .care_reports import assemble_report

# Synthetic IDs returned by authorized history lookup.
report = assemble_report(session, episode_id="synthetic-episode-id",
                         end="2030-01-03", evidence_limit=100)
```

The same selectors as the core report are supported: a `start`/`end` range,
`episode_id`, or `since_visit_id`, with optional `issue_id`. Core validation
requires one selector; issue relevance for a visit must have recorded link
support. `max_entries` (default/max 1,000) retains core journal caps. Extension
`evidence_limit` defaults to 100 and permits 1–500; `evidence_offset` defaults to
zero and permits up to 1,000,000. Dates/instants normalize to UTC.

## Reported administrations and reminder acknowledgements

The `medication` section separates:

- `reported_administrations`: explicit human reports within the occurrence
  interval, with source, reporter, reported time, occurrence time, case links,
  and an immutable referenced plan-version snapshot. The snapshot preserves
  the original medication/instructions/schedule, plan source and confirmation
  evidence even after a later revision or cancellation.
- `reported_counts_by_plan_version`: factual report counts partitioned by exact
  plan/version. These count reports, not clinically verified doses or adherence.
- `unlinked_administrations_in_interval`: reports within the interval whose
  episode/issue relevance has not been established. They are not silently
  attached to the selected case. Reports explicitly linked to a different case
  are excluded from a case report.
- `reminder_acknowledgements`: acknowledgements dated by their acknowledgement
  timestamp, with `administration_recorded=false`. In an episode/issue report,
  a matching plan/version with explicitly linked reported administrations
  supplies plan context only; it does not establish an episode dose.
- `unassigned_reminder_acknowledgements`: acknowledgements without that case
  context. Manual reminder acknowledgements can appear here. No acknowledgement,
  delivery state, read receipt or silence creates an administration.

An absent administration record never becomes a missed dose, zero actual doses,
recovery or adherence estimate. The API requires known occurrence timestamps for
actual administration records. An undated narrative report remains visible in
core `unknown_occurrence_entries`; the assembler never manufactures a dose time
from the reminder's scheduled/acknowledged time. `available=false` distinguishes
an uninitialized medication ledger from evidence about a person's actual care.

`record_administration(..., case_ids=None)` now accepts up to 32 existing scoped
issue/episode/visit IDs. Its additive reminder schema v2 table
`care_medication_administration_cases` stores links in the same transaction as
the report/replay receipt. Linked issue queries include child episodes. Link IDs
are validated in the selected database, and duplicate IDs normalize harmlessly.
Changing links under a replay identity conflicts. Empty links preserve existing
v1 request fingerprints; migration never reinterprets or moves old reports.
The trusted identifier alphabet matches core Authority, including `@` in actor
IDs; IDs are parameterized data and never filesystem destinations.

## Document evidence and date meaning

The `documents` section includes `linked_visit_documents` and
`documents_with_unknown_clinical_time`. It returns safe source/page metadata,
unconfirmed extraction quotes/proposals and upload receipt identities. It omits
folder bindings, resumable upload URIs, credentials, raw transport errors and
arbitrary network URLs.

For a range/since-visit report, a linked visit supplies a known **relevance**
time; otherwise selection uses document recording time and labels the clinical
occurrence unknown. A linked visit is never asserted to be the document's
clinical occurrence. A case report selects explicit episode links, child episodes
of an issue, or visits connected through recorded journal links. Known linked
visit times obey the requested time interval. Undated documents explicitly
linked to an episode remain available even if filed later, with
`recorded_in_requested_interval=false`; filing time never replaces clinical time.

Each quoted extraction remains `clinically_verified=false`, with source index,
page number and proposal state. Uploading a document does not verify its text,
confirm a prescription, create a dose report or activate treatment. A quote may
be incorrect OCR/model output; the preserved original page remains the evidence
for human review. The report includes at most the first 25 proposals per document
and counts omissions. Quotes cap at 1,000 characters with `quote_truncated=true`;
proposal payloads above 4,000 bytes are omitted with an explicit flag. Their
complete source-linked proposal remains retrievable through the document API.

## Completeness, pagination and concurrency

Every extension section is a page containing `items`, `total`, `returned`,
`offset`, `limit`, `next_offset`, `omitted_before`, `omitted_after`, `truncated`
and ordering. The same offset is applied independently to each extension
section. Totals and per-plan counts are computed across all matching rows,
regardless of omitted evidence. Per-plan group evidence itself is paginated.
Retrieve later pages by repeating the same selectors with the returned offset;
concurrent new records can change offset pages between calls, so retain the
report generation time when exporting. Core journal evidence has its own caps
and omission counters; the extension offset does not page the core journal.

The journal core uses its consistent read snapshot; medication/documents then
share a second read snapshot. The result explicitly records this boundary in
`report_sections.journal_and_extensions_use_separate_read_snapshots`. No write
transaction spans inference, document transport or report assembly. A source
record can therefore be corrected between these snapshots; the returned revision
and plan version identify which evidence was read. Request a fresh report after
concurrent caregiver changes when a single exported view matters.

The runtime can retain a large report as a private scoped artifact and return a
bounded reference/summary. This does not publish or send a report. Review source
links, unconfirmed excerpts, uncertainty and explicit omissions before sharing
with a doctor. Source rollback uses the prior Git commit; all existing patient
records and provenance remain intact.

Synthetic checks cover reminder acknowledgement versus administration, unknown
occurrence, closed case retrieval, original plan instructions after revision,
case-link migration/replay conflicts, relevant visits/issues, shared caregivers,
adult isolation, unconfirmed document extraction, receipt metadata exclusion,
range selection and bounded pagination/excerpts. No real clinical/account data
or live reminder/Drive operation is used.
