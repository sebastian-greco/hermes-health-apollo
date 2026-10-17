# Trusted Apollo care core

`care_store.py` extends the existing Apollo database. `store.initialize()` now
adds schema v9 without replacing v8 tables. Each authorized care scope gets its
own Apollo-initialized SQLite file: adult records remain separate; caregivers
of one child use the same file with distinct actor capabilities. Legacy global
food/Oura keys therefore remain within a single patient's scope.

The host constructs `Authority(actor_id, scope_id, frozenset(permissions))` from
verified actor/route grants. Permissions are the explicit independent strings
`read`, `write`, `admin`; admin is category administration, not implicit access.
Never decode Authority, scope database paths, actor IDs or permissions from
model tool arguments. This is an in-process host capability, not a signed token.
The host must recheck grants when starting a request and before external egress;
revoke/replace cached sessions after grant changes. Reminder egress additionally
requires an injected fresh authorization callback.

```python
from .care_store import Authority, CareStore

# Synthetic example; production paths and route grants are trusted host config.
patient = CareStore(runtime_path / "synthetic-child.db", scope_id="synthetic-child")
session = patient.session(Authority("synthetic-caregiver", "synthetic-child",
                                   frozenset({"read", "write", "admin"})))
session.define_category("daily_note", expected_daily=True)
issue = session.create_case(kind="issue", title="Questione riferita",
                           request_id="message-1:0", source_ref="message:1")
episode = session.create_case(kind="episode", title="Episodio riferito",
                             parent_id=issue["case_id"],
                             request_id="message-1:1", source_ref="message:1")
entry = session.append_entry(category_id="daily_note", narrative="Nota riferita.",
                             links=[episode["case_id"]], request_id="message-1:2",
                             source_ref="message:1")
report = session.report(episode_id=episode["case_id"])
```

A database gets an immutable `care_scope(singleton=1, scope_id)` marker checked
on each connection. Binding a different scope fails. A populated unbound Apollo
database requires the trusted `adopt_existing=True` flag after ownership review;
this flag is never a model argument. Initialization rejects a newer unknown
schema rather than downgrading it. Existing data and default source definitions
are retained. No real patient data or custom private category definitions belong
in Git.

## Stable session interfaces

- `require(permission)` checks the trusted capability.
- `connection(permission="read")` is a context manager returning SQLite Row
  results with foreign keys. Writes/admin use `BEGIN IMMEDIATE`, and all paths
  commit or roll back and close. Adapters ensure their additional tables lazily
  in this database. Do not expose this raw connection as a model tool.
- `apollo(permission="read")` guards the scope and binds the same database for
  legacy Apollo food/query calls via a ContextVar. The guard connection closes
  before yielding, avoiding nested-write locks. It never mutates `os.environ`.
  Context managers must be entered per worker/thread; bindings are not global.
- `define_category(category_id, version=1, schema=None, label=None,
  observations=None, expected_daily=False)` requires admin. Definitions are
  immutable by `(category_id, version)`. `list_categories()` requires read.
- `append_entry(category_id, request_id, source_ref, data=None, narrative="",
  category_version=1, occurrence_start=None, occurrence_end=None,
  uncertainty="unknown", links=None)` requires write. Arguments are keyword
  only. Unknown occurrence stays null. `recorded_at` is separate.
- `revise_entry(entry_id, expected_revision, request_id, source_ref, changes,
  reason)` appends an immutable revision and changes the current head atomically.
  `changes` permits data, narrative, occurrence times, uncertainty, links and
  category version. Actor and record identity cannot be changed by a caller.
- `get_entry(entry_id, include_revisions=False)` and
  `list_entries(category_id=None, case_id=None, start=None, end=None,
  include_closed=True, limit=500, latest=False)` read current heads.
  `latest=True` returns the newest heads first without scanning all results into
  memory; the default oldest-first order remains compatible. A case filter for an issue
  includes entries linked to its child episodes. A ranged listing excludes
  unknown occurrence; doctor reports return unknown-time evidence separately.
- `create_case(kind, title, request_id, source_ref, parent_id=None,
  occurred_at=None, narrative="")` creates an issue, episode or visit.
  Only episodes may have an issue parent. Visits require an explicit timestamp.
- `update_case(case_id, expected_revision, request_id, source_ref, reason,
  status=None, title=None, narrative=None, occurred_at=None)` corrects, closes
  or reopens explicitly while preserving case revisions. Silence has no effect.
- `get_case(case_id, include_revisions=False)` and
  `list_cases(kind=None, include_closed=True)` retain closed history by default.
- `link_food(food_id, case_id, source_ref)` links existing Apollo food rows;
  repeated identical links are harmless. It never creates a second food log.
- `report(start=None, end=None, episode_id=None, since_visit_id=None,
  issue_id=None, max_entries=1000)` requires one selector: start of date range, episode, or visit.
  `issue_id` can narrow a range/since-visit report. A visit used with an issue
  must have explicit shared entry-link evidence connecting them. No visit's
  clinical relevance is inferred from recency alone.

Append/correction/case writes atomically store actor-specific request fingerprints
and exact results. An identical request replays its original response even after
later corrections; reusing its identity with different content fails. Distinct
actors may report separate events using the same transport ID. Pass stable
transport-derived per-operation IDs, not a new ID for each model retry. Save
multi-step command plans before executing them; completed operations replay and
unfinished operations resume without holding a transaction across inference or
network calls. Optimistic revisions reject concurrent stale corrections.

## Category definitions and measurements

No dedicated bowel/glucose/etc tables, parsers or tools are needed. Narrative
plus arbitrary bounded JSON can start a custom category. The supported JSON
Schema subset is declarative: type, properties, required, additionalProperties
(boolean), items, enum, const, minimum/maximum, minLength/maxLength,
minItems/maxItems, title and description. Unsupported keywords, references,
code and remote schemas fail closed. This bounded subset intentionally avoids
unbounded schema evaluation and additional dependencies. Payloads are finite
JSON objects capped at 64 KiB, depth 12 and 4,096 nodes; schemas at 32 KiB.
Strings cap at 8,000 characters and keys at 160. Numeric booleans are rejected.

To give numeric fields measurement semantics and report aggregates, configure
`observations={"value": {"unit_field": "unit", "allowed_units": ["mmol/L"]}}`.
Such a field requires an explicit reported compatible unit; no unit inference
or conversion occurs. Multiple units aggregate separately. Unconfigured JSON
numbers remain source data and receive no inferred units/clinical meaning or
measurement aggregate. Category metadata does not establish a diagnosis and
cannot activate a medication plan or reminder. Expected daily gaps are an
explicit category preference, not a clinical monitoring prescription.

## Canonical projection and doctor reports

Every current care entry projects into Apollo `events`; configured numerical
measurements project into `observations`. Revisions reuse Apollo source/raw
record/lineage helpers, preserving sensitive provenance and immutable revision
links. Projection refreshes include custom categories and corrected timestamps;
no extra journal database or replaced food path exists.

Apollo canonical event timestamps are mandatory. For unknown occurrence only,
its recorded timestamp supplies that index, with `occurrence_unknown=true` and
`canonical_time_basis="recorded"` in attributes. The care API/report never treats
this index as an occurrence. Uncertain observations carry `quality="uncertain"`.
Consumers using canonical facts must respect these attributes.

Reports contain factual entry/category counts, current corrected evidence,
source references, linked cases, food links and numeric counts/min/max/means
partitioned by explicit unit and immutable category version. Revision evidence
keeps original/corrected source references visible without double-counting. They make no diagnoses or causal claims. Reports
include unknown occurrence separately and flag approximate/ranged entries.
Days without exact-time logs and opt-in daily category gaps are explicit;
missing data never becomes zero, a negative symptom, recovery or a missed dose.
Closed episodes remain available. All report dates are normalized to UTC;
explicit offset timestamps are accepted, date-only ranges cover UTC days.
Maximum report range is ten years. Evidence is capped at 1,000 current entries,
1,000 unknown-time entries and 1,000 food entries (a lower `max_entries` can be
requested). Revision evidence includes at most the latest 25 revisions per
returned entry. `evidence.truncated` and exact omission counters make partial
evidence explicit. Factual counts, numeric aggregates, uncertainty totals and
missing-day calculations cover **all** matching current heads, including omitted
evidence. Counts never silently describe only the returned page. Read snapshots
keep a report consistent with concurrent caregiver corrections. An undated episode's interval remains unknown.

Synthetic verification covers legacy v8 preservation, future-schema refusal,
custom versioned categories, malformed/deep/oversized JSON, units, trusted
scope/grants, shared caregivers, replay and conflicting replay, revisions,
closure/reopening, relevant visits, unknown-time gaps, canonical rebuilds,
concurrent scoped food writes and exactly-once concurrent transport replay.
No live HA/Hermes/account/document activation occurs in these checks.
