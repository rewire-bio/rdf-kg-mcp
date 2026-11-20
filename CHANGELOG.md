# Changelog

## 0.4.0

### Added

- `draft_change` changes fields of an existing record. The server needs the store's committed lines for this, which the KG does not hold: pass them with `serve --drafts --draft-records PATH` (a `.jsonl` or `.jsonl.gz` file or a directory of them; repeatable). A change takes the record id, a list of JSON Pointer changes and the `base_sha256` of the committed line it was based on, and is refused when that digest does not match, so a stale base cannot overwrite newer data. Keys the change does not touch keep their place and their bytes: a changed value stays where it was, a new key is appended to its object, and nothing is sorted.
- `draft_diff` fills `changed`: per record, the base and new digests and a field-level diff (`path`, `change`, `before`, `after`).
- `draft_export` writes `changes.jsonl` beside `batch.jsonl` when there are changes: one `{id, base_sha256, sha256, record}` line per changed record, sorted by id, for the dataset's own change command. `batch.jsonl` is written as before.
- Link targets and references found in the store records count as known, with their kind, even when the release lacks them.

### Changed

- `draft_upsert` refuses an id that is in the store records, as it refuses one in the release.

## 0.3.1

### Fixed

- A draft whose link target or reference was not usable in an IRI (`"esm 2"`) was stored and then broke `draft_upsert`, `draft_diff` and `draft_export` with a bare "Error executing tool" until it was removed. Such a target is now a listed problem, and a draft id that cannot be part of an IRI is refused with a message before anything is stored.
- Link targets and references are checked against the id field's schema in `record_schema`. A sub-resource IRI under `iri_base` (`record/missing/field`) is a problem, not a release record of unknown kind.
- Exported numbers are written as JavaScript's `JSON.stringify` writes them (`0.00002`, not `2e-05`; `1`, not `1.0`), so a batch line is the line a JavaScript store writes and its digest is the record's digest. `1` and `1.0` in a draft are now the same value (`unchanged`).
- A refused `draft_export` removes `batch.jsonl` and `summary.json` left by an earlier export, and lists them in `removed`. Exported files get the usual mode for the umask (0644 by default), not 0600.
- Possible duplicates: search hits count only when their label or alias shares at least half the words of one name with the other, so unrelated high-scoring records are no longer listed. Drafts with half their words in common are listed as `near name`.
- `record_schema` problems are cut to 300 characters.
- `serve --drafts` on a bundle with no draft block stops before loading the graph.

## 0.3.0

### Added

- Draft tools, with `serve --drafts`: `draft_upsert`, `draft_remove`, `draft_diff` and `draft_export`. A local client adds new records, as the JSON its dataset's store uses, to a private in-memory overlay. Each draft is checked against an optional JSON Schema, the kind list, link rules and the kinds of link targets in the release, and gets a list of possible duplicates. `draft_export` writes a deterministic `batch.jsonl` and a `summary.json` to `--draft-dir`. The overlay only adds records; records already in the release are refused. Drafts are not visible to the read tools.
- Optional manifest field `draft` describes a dataset's records for the draft tools. `--draft-config` supplies it for a bundle that lacks it. `verify` checks it.
- `--drafts` is refused over `streamable-http`.
- `search` also matches labels and aliases squashed (letters and digits only, case folded), so "ESM2" finds "ESM-2" and the reverse.
- `scripts/search_recall.py` measures search recall at k against gold pairs on a real bundle, with load time, index time and peak memory.

- `search` tool: ranks entities by text match over labels, `skos:altLabel` aliases and descriptions, with an optional class filter (inferred types count) and paging. The index is built at start-up. Ranking is BM25-style with light stemming and prefix matching, a class-rarity prior and at most three results per class among the leading places.
- Optional manifest field `search.text_predicates` names extra literal predicates to index.

## 0.2.0

### Added

- Named queries. A bundle manifest can carry parameterised SPARQL queries in a new `queries` field. The new `list_queries` tool shows them, and `run_query` runs one by name with parameters. Parameters are typed (`iri`, `string` or `integer`) and validated before they go into the query. Named queries run with the same read-only checks, timeout and row cap as `sparql_query`.
- Any graph role. A bundle can declare graph roles beyond `asserted`, `inferred` and `ontology`, such as `derived`. Declared roles are listed by `dataset_info`, labelled in `describe_resource` and accepted by `sparql_query(graphs=[...])`.
- A new optional `graph_descriptions` manifest field describes each role. `asserted`, `inferred` and `ontology` have default descriptions.
- `verify` checks named queries: duplicate names, unused parameters, defaults of the wrong type, and queries that are not read-only.

### Changed

- `sparql_query` refuses an unknown graph role and lists the roles the bundle declares. It used to report a prefix error.
- The `example_queries` prompt also lists named queries.

Manifests without the new fields work as before. `format_version` is still `1.0`.

## 0.1.0

First release: five read-only tools (`dataset_info`, `describe_schema`, `find_entity`, `describe_resource`, `sparql_query`) over a checksummed RDF bundle.
