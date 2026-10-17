# Plan: extend Apollo with a flexible care journal

Decision: 2026-10-17. This supersedes the previous standalone-core assignment.
The required base is the existing Apollo repository and its data infrastructure.
No further implementation session is launched by this planning change.

## 1. Preserve and establish the base

- Keep the completed prior implementation at `5b28958` in its private repository
  and `codex/archive-pre-apollo-20261017` branch. Nothing is discarded or reset.
- Start this private derivative from reviewed Apollo source
  `9f2db670a2669c9f3401fecb3b547ed52bcb2c3b`, retain its history and MIT license,
  and keep the original project as `upstream` for deliberate future updates.
- Extend the existing `store.py` migrations, `food.py`, source/lineage helpers,
  `semantic_layer.py`, queries and plugin initialization. Do not reimplement
  those foundations in a parallel database or replace them with healthcare-mcp.
- Record a reuse map before code changes: existing module, behavior reused,
  smallest extension and compatibility check. Reuse useful prior tests and
  boundary logic selectively, without importing the old separate datastore.

Apollo's current canonical tables already have JSON attributes and numeric/text
observations; its main store is schema version 8. These are extension points,
not proof that arbitrary diary writes, episode links or patient ACLs exist.
Choose the smallest additive schema/projection changes within that database.

## 2. Generic categorized health log

Use one common log model and append/query/correct interface. Category examples
are bowel observations, blood pressure, blood glucose, temperature, pain or a
user-defined diary topic. These are configurable categories, not mandatory
separate code modules, tables or model tools.

| Common field | Purpose |
| --- | --- |
| Stable entry ID and revision | Retrieval, correction and replay handling |
| Patient/scope and authorized actor | Trusted runtime ownership and reporter |
| Category ID and category schema version | Stable interpretation of structured payload |
| Occurrence instant/range and uncertainty | When the reported event happened; unknown stays unknown |
| Recorded time | When the system accepted it, independent of occurrence time |
| Narrative | Original report/context even if no structured value is supplied |
| Source reference and request identity | Message/document provenance and transport replay protection |
| Issue/episode/visit links | Connect entries to ongoing concerns or bounded episodes |
| Structured JSON data | Category-specific fields, values, units and qualifiers |

Illustrative synthetic payloads:

```json
[
  {
    "category": "blood_pressure",
    "category_schema_version": 1,
    "text": "Misurazione a riposo.",
    "data": {"systolic": 118, "diastolic": 76, "unit": "mmHg"}
  },
  {
    "category": "blood_glucose",
    "category_schema_version": 1,
    "text": "Misurazione riportata dalla persona.",
    "data": {"value": 5.4, "unit": "mmol/L"}
  },
  {
    "category": "bowel",
    "category_schema_version": 1,
    "text": "Aggiornamento del diario.",
    "data": {"occurred": true, "description": "Descrizione riferita"}
  }
]
```

These fragments illustrate category data, not the trusted ownership envelope.
Actual time/source/patient fields are supplied or resolved by validated intake;
a model-supplied patient ID does not grant access. A category such as glucose is
distinct from a diagnosed condition; a log must not imply a diabetes diagnosis.

Category definitions can provide optional JSON Schema validation, field labels,
units and display/aggregation hints. Keep payload size/depth limits and version
definitions; schema updates must not reinterpret existing entries silently.
A new custom category can begin as narrative plus JSON without a software
release. Store private category preferences in runtime data, not patient facts
in Git. Category definitions are data, never executable code or SQL.

For structured numerical use, validate types and require explicit compatible
units. Preserve the reported value and unit; no silent mg/dL/mmol/L or other
conversion. Do not require a complete questionnaire for a useful narrative
entry. Ask for details only when the requested recording/report/reminder needs
them. Unknown data cannot become zero, a negative symptom or a missed dose.

Integrate new log entries into Apollo's event/observation projections so existing
queries can retrieve them. Check projection rebuilds preserve new categories;
do not create a second unrelated journal that Apollo cannot query. Keep the
existing food entry path and link its records to issues/episodes where relevant.

Medication plans, actual administrations and scheduled reminders remain
different records/states. A generic category called "medicine" cannot activate
a treatment plan, authorize dosing or bypass verified-reminder requirements.
Likewise, category flexibility cannot widen patient or caregiver access.

## 3. Patient scopes and connected history

Use one implementation for separate adult records and the shared child record.
First verify whether one Apollo database/profile per authorized care scope is
the simplest safe deployment, given global keys in existing food/Oura tables.
If a shared database is selected, patient-aware keys and access checks are
required on every old and new read/write/projection. Record the choice before
enrolling real data; source IDs/privacy labels are not authorization.

An ongoing issue can exist without a diagnosis. Bounded episodes can link to it.
An entry belongs to the patient and can link to several relevant issues without
duplication. Opening, closing, reopening and correcting episodes preserves
history and evidence. Silence is not recovery; closure does not cancel an
unrelated medicine plan. Both child caregivers read/update one shared history.

## 4. Dedicated specialist and first useful workflow

Keep the general Casita conversation as the interface. It loads only a small
health dispatcher; the specialist loads its own instructions, tools and relevant
Apollo-backed history. Use trusted actor/route bindings and explicit tool/MCP
selection. Normal Hermes delegation inherits parent tools, so verify the actual
named-runner boundary rather than declaring a prompt to be isolation.

Use Hermes 0.22.2 source `d923a5eb487a17c0a68801bba9ef0f56c81040a8`
as the production compatibility baseline. The public child lifecycle API only
narrows parent capabilities; it cannot grant an independent health catalog the
parent never loads. Document the minimal supported runner/companion integration
if needed. Keep host-managed inference authentication and provider portability.

First demonstrate: natural message -> validated generic log saved in Apollo ->
issue/episode link -> correction/retrieval -> doctor summary. A summary supports
one episode, a date range or since the relevant visit, with factual aggregates,
source links and explicit missing-data intervals. It uses category metadata and
general queries rather than a separately hard-coded bowel-report feature.

Start with the adult pilot, but design shared child actors and concurrency from
the first schema/tests. Return concise results or necessary clarifications to
Casita, not the full specialist transcript. Durable facts and pending actions
outlive each agent run and model/provider change.

## 5. Time-saving document and reminder extensions

- Photo batches: preserve originals; crop/rotate/deskew/order into a legible
  derivative; never regenerate clinical text. File automatically to an existing
  configured patient Drive folder, retain upload receipts and link source,
  extracted facts and proposed plans to the relevant episode/visit.
- Medicine/follow-up reminders: derive only from confirmed instructions or an
  explicit unambiguous request; persist exact deadlines, cancellation/version
  and outbox state. Delivery/read/acknowledgement is not a dose reported taken.
- Shared child care: coordinate generic group/media transport work early. Both
  authorized caregivers see one corrected episode and administration history.
  Keep domain logic outside WAHA; reuse existing central egress.

Optional HA sleep/heart-rate context comes later. No watch integration, Google
Health OAuth, new appointment subsystem or speculative cause-finding is needed
to deliver the journal, doctor summary, document filing or useful reminders.

## Review and implementation boundaries

At this preparation stage only docs/instructions are changed. Keep the existing
upstream runtime and license intact. The next implementation session should
begin with the reuse map and first workflow above, then run meaningful upstream
and new regression tests using synthetic data and isolated Hermes profiles.
Do not install this checkout into live Hermes, connect real accounts, read
patient files or alter HA configuration while developing source.

Do not start by porting all previously written code. Selectively reuse verified
grant/replay/report tests or adapters only where they extend Apollo cleanly.
The user should review this Apollo-based plan before a new coding run is started.
