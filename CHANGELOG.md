# Changelog

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
