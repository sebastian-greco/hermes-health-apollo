# Apollo reuse map

| Existing Apollo module | Reused behavior | Additive extension | Compatibility evidence |
| --- | --- | --- | --- |
| `store.py` | SQLite initialization, migrations, WAL, private files, original tables | v9 care tables; explicit/context-local database binding | v8 upgrade retains food; isolated concurrent scopes |
| `semantic_layer.py` | Canonical events, observations, projection rebuilds | generic categorized care projection alongside food | rebuild preserves custom categories and corrections |
| `food.py` | Existing food validation and storage | trusted scoped execution and case links; no new food datastore | original food/query tests and scoped food regression |
| `query.py` | Apollo canonical query machinery | same scoped database context, care event/observation rows | canonical queries see care records |
| Apollo source provenance | source reference and row identity | immutable revision actor/source/request evidence | replay conflict and revision tests |

One Apollo-initialized database per trusted patient/care scope is the deployment
boundary. Legacy globally keyed food/Oura tables therefore cannot mix patients.
The session binding is context-local, never an environment-variable mutation.
Shared child caregivers receive distinct actor authorities for the same scope
and database. Scope binding fails closed when a database already belongs to a
different scope; existing unbound populated databases require explicit adoption.

Only synthetic fixtures are used. Runtime category preferences and patient data
remain outside Git. No live accounts, documents, devices or services are used.
