# Bundle format 1.0

A bundle is a directory, or a `.tar.gz` of one, containing `manifest.json` and the RDF files it lists. The JSON Schema is [`src/rdf_kg_mcp/manifest.schema.json`](../src/rdf_kg_mcp/manifest.schema.json); `rdf-kg-mcp verify` checks it.

## Manifest

```json
{
  "format_version": "1.0",
  "dataset": "pets",
  "version": "2026-01-01",
  "description": "A small example dataset.",
  "license": "CC0-1.0",
  "graphs": {
    "asserted": "https://example.org/graph/asserted",
    "inferred": "https://example.org/graph/inferred",
    "ontology": "https://example.org/graph/ontology",
    "derived": "https://example.org/graph/derived"
  },
  "graph_descriptions": {
    "derived": "Findings computed from the asserted and inferred graphs."
  },
  "files": {
    "asserted.nq": { "sha256": "…", "quads": 73 },
    "derived.nq": { "sha256": "…", "quads": 4 },
    "inferred.nq": { "sha256": "…", "quads": 34 },
    "ontology/pets.ttl": { "sha256": "…", "triples": 27, "graph": "https://example.org/graph/ontology" }
  },
  "prefixes": { "ex": "https://example.org/vocab#", "id": "https://example.org/id/" },
  "label_predicates": ["rdfs:label"],
  "example_queries": [
    { "title": "Dogs and owners", "query": "SELECT ?dog ?owner WHERE { ?dog ex:ownedBy ?owner }" }
  ],
  "queries": [
    {
      "name": "dogs_named",
      "title": "Dogs whose label contains some text",
      "description": "Dogs whose label contains the text, in IRI order.",
      "sparql": "SELECT ?dog ?label WHERE { ?dog a ex:Dog ; rdfs:label ?label FILTER(CONTAINS(?label, $text)) } ORDER BY ?dog LIMIT $limit",
      "params": [
        { "name": "text", "type": "string", "required": true, "description": "Text to look for." },
        { "name": "limit", "type": "integer", "default": 20 }
      ]
    },
    {
      "name": "findings",
      "title": "Findings, optionally about one resource",
      "sparql": "SELECT ?finding ?about WHERE { GRAPH <https://example.org/graph/derived> { ?finding ex:about ?about } FILTER(COALESCE(?about = $about, true)) }",
      "params": [{ "name": "about", "type": "iri", "description": "Only findings about this resource." }]
    }
  ],
  "inferred": { "reasoner": { "name": "owlrl", "version": "7.6.2", "profile": "OWL 2 RL" } },
  "shacl": { "conforms": true }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `format_version` | Yes | `"1.0"` |
| `dataset` | Yes | Stable dataset name |
| `version` | No | Version of the data; `release_id` is accepted when `version` is absent |
| `files` | Yes | Relative path to `{sha256, quads or triples, graph}` |
| `graphs` | No | Role name to named-graph IRI |
| `graph_descriptions` | No | Role name to a short description of the graph, shown by `dataset_info` and in the server instructions. Defaults exist for `asserted`, `inferred` and `ontology` |
| `prefixes` | No | Prefix to namespace; used to shorten IRIs in results and predeclared in SPARQL |
| `label_predicates` | No | Predicates searched by `find_entity` (IRIs or prefixed names). Default: `rdfs:label`, `skos:prefLabel`, `skos:altLabel`, `schema:name`, `dcterms:title` |
| `search` | No | `{"text_predicates": [...]}`: extra literal predicates indexed by `search`, beyond labels, `skos:altLabel` and descriptions |
| `draft` | No | How the dataset's records are written, for `serve --drafts`; see [Draft block](#draft-block) |
| `example_queries` | No | `{title, query, description}` items offered as a prompt |
| `queries` | No | Named, parameterised queries served by `list_queries` and `run_query`; see [Named queries](#named-queries) |
| `inferred`, `shacl` | No | Free-form records of how inferences and validation were produced, shown by `dataset_info` |
| `title`, `description`, `license`, `homepage` | No | Shown by `dataset_info` |

Other fields are allowed and passed through.

## Files

- Paths are relative, use `/`, and cannot contain `..`.
- The extension sets the format: `.nq` (N-Quads), `.trig` (TriG), `.nt` (N-Triples), `.ttl` (Turtle), `.rdf` or `.owl` (RDF/XML).
- Quad files carry their own graph names. A triples file goes into the named graph in its `graph` field, or the default graph without one.
- `sha256` is the hex SHA-256 of the file bytes. `quads` or `triples` is the number of statements; `verify` compares it unless `--quick`.
- Producers should write statements sorted and deterministically, so the same input always gives the same bytes and checksums.

## Graph roles

The server gives three roles a meaning:

| Role | Holds | Effect |
| --- | --- | --- |
| `asserted` | Source facts | Listed first by `describe_resource` |
| `inferred` | Facts derived from the asserted graph and the ontology | Marked `inferred`; excluded by `graphs: ["asserted"]` |
| `ontology` | Classes, properties and axioms | Excluded from instance and statement counts in `describe_schema`; used for labels, definitions and axioms |

Other role names, such as `derived`, are allowed. They are shown as given, listed after `inferred` by `describe_resource`, and can be used in the `graphs` argument of `sparql_query`. A graph that a file loads but no role declares is shown as `other`. The `inferred` graph should hold only statements that are not in the `asserted` graph.

Bundles written before `graph_descriptions` existed still work: the three standard roles get their default descriptions and other roles have none.

## Named queries

Each item in `queries`:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Identifier used with `run_query`: letters, digits and `_`, not starting with a digit. Unique in the bundle |
| `title` | Yes | One line saying what the query answers |
| `description` | No | Longer explanation, including what each row means |
| `sparql` | Yes | A SELECT, ASK, CONSTRUCT or DESCRIBE query. Bundle prefixes are predeclared |
| `params` | No | Parameters, below |

Each item in `params`:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Same rules as a query name. Written in the query as `$name` |
| `type` | Yes | `iri`, `string` or `integer` |
| `description` | No | What the value means |
| `required` | No | `true` if the caller must give a value. Default `false` |
| `default` | No | Value used when the caller gives none: a string for `iri` and `string`, a number for `integer` |

Rules for query authors:

- Write each parameter as `$name` in the query, and use `?name` for ordinary variables. `$name` inside a string, an IRI or a comment is not replaced.
- Do not put a parameter in the `SELECT` list or in `BOUND()`. When it has a value, it is replaced by a constant there, which is a syntax error.
- An optional parameter with no value and no default stays an unbound variable. Write the filter so that both cases work, for example `FILTER(COALESCE(?about = $about, true))`.
- An `integer` parameter can be used in `LIMIT` and `OFFSET`.

`verify` reports a duplicate query or parameter name, a declared parameter that the query does not use as `$name`, a default of the wrong type, and a query that is not read-only.

## Draft block

`serve --drafts` lets a local client add new records to a private overlay and export them as a batch for the dataset's own import. The server knows nothing about any dataset's records except what this block says. A client can also pass the block in a file with `--draft-config`, for a bundle built before it had one.

```json
"draft": {
  "iri_base": "https://example.org/id/",
  "id_field": "id",
  "type_field": "kind",
  "label_field": "name",
  "record_schema": { "type": "object", "required": ["id", "kind", "name"], "properties": { "...": {} } },
  "kinds": { "dog": "ex:Dog", "person": "ex:Person" },
  "draft_kinds": ["dog"],
  "links": { "field": "links", "relation": "relation", "target": "target_id" },
  "link_rules": { "owned_by": { "from": ["dog"], "to": ["person"] } },
  "references": [{ "field": "source_ids", "kinds": ["source"] }],
  "sort_within": ["attributes"],
  "sort_arrays": ["links"]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `iri_base` | Yes | A record's IRI is `iri_base` followed by its id. A draft whose IRI is the subject of any statement in the bundle is refused: the overlay only adds records |
| `id_field`, `type_field` | Yes | The fields holding a record's id and kind |
| `label_field` | No | The field holding its name, used to find possible duplicates |
| `record_schema` | No | JSON Schema (2020-12) each draft must pass. The order of its top-level `properties` is the field order on export |
| `kinds` | Yes | Kind to the one class that marks records of that kind in the asserted graph. A class may belong to one kind only. Used to find the kind of a link target |
| `draft_kinds` | Yes | The kinds that may be drafted |
| `links` | No | The list field holding links, and the keys of a link's relation and target id |
| `link_rules` | No | Relation to the kinds it may link from and to. When present, any other relation is refused |
| `references` | No | List fields of record ids that must name records of the given kinds |
| `sort_within` | No | Fields whose object keys are sorted at every depth on export, in UTF-16 code unit order (as JavaScript sorts strings) |
| `sort_arrays` | No | List fields written sorted on export: the links field by relation then target, others by their JSON text |

An exported line is `json.dumps` with no spaces and non-ASCII kept, so the same overlay always gives the same bytes and the same content in any key order gives the same bytes. Integers beyond 2^53 and non-finite numbers are refused, as I-JSON ([RFC 7493](https://www.rfc-editor.org/rfc/rfc7493)) requires, because readers that hold numbers as doubles would not read them back exactly. A dataset whose import parses and re-serialises each record (as Rewire's `records add` does) fixes number formatting itself; if the import kept the line's bytes, it would need its own serialiser.

## Archives

`rdf-kg-mcp pack` writes `manifest.json` first and then the listed files in sorted order, with zero timestamps and owners, so the archive bytes are reproducible. Readers also accept an archive whose contents sit in a single top-level directory. Links, absolute paths and `..` entries are refused.

## Remote bundles

A URL ending in `.tar.gz` or `.tgz` is downloaded and extracted. Any other URL is treated as a manifest (`…/manifest.json`, or a directory URL to which `manifest.json` is appended); the files it lists are fetched relative to it. Files already in the cache whose checksums match are not downloaded again.
