# rdf-kg-mcp

Serve an RDF knowledge graph read-only over the [Model Context Protocol](https://modelcontextprotocol.io), so Claude and other MCP clients can explore and query it. A local client can also draft new records against it and export them for the dataset's own import (see [Drafts](#drafts)).

The graph is a **bundle**: a manifest plus RDF files, with a SHA-256 checksum for each file. Inference is done once, when the bundle is built, and stored as its own named graph. The server loads the bundle into an embedded [Oxigraph](https://github.com/oxigraph/oxigraph) store, so there is no database to run or host.

- Read-only: no SPARQL Update, no federated `SERVICE` calls. Drafts never touch the release graphs.
- Asserted and inferred facts are kept apart, and every statement says which graph it came from.
- Pinned versions: the server refuses a bundle whose files do not match the manifest.
- Works with any RDF dataset. Nothing in the server is specific to one domain.

```mermaid
flowchart LR
  subgraph bundle["Bundle"]
    manifest["manifest.json<br/>graph roles, prefixes,<br/>named queries, SHA-256 per file"]
    files["N-Quads and Turtle files"]
  end
  verify["verify<br/>schema, checksums"]
  subgraph store["Oxigraph store (read-only)"]
    asserted["asserted"]
    inferred["inferred"]
    ontology["ontology"]
    derived["derived or any other declared role"]
  end
  tools["MCP tools<br/>dataset_info, describe_schema,<br/>search, find_entity, describe_resource,<br/>sparql_query, list_queries, run_query"]
  manifest --> verify
  files --> verify
  verify --> store
  store --> tools
```

## Quick start

```sh
# Serve a published bundle to Claude Code (downloaded and verified on first start)
claude mcp add my-graph -- uvx rdf-kg-mcp serve https://example.org/my-graph/bundle.tar.gz

# Or a local bundle directory
uvx rdf-kg-mcp serve ./bundle
```

Until the package is on PyPI, install from GitHub instead:

```sh
uvx --from git+https://github.com/rewire-bio/rdf-kg-mcp rdf-kg-mcp serve ./bundle
```

For Claude Desktop or another client, add a server entry that runs the same command:

```json
{
  "mcpServers": {
    "my-graph": {
      "command": "uvx",
      "args": ["rdf-kg-mcp", "serve", "https://example.org/my-graph/bundle.tar.gz"]
    }
  }
}
```

## Tools

| Tool | What it returns |
| --- | --- |
| `dataset_info` | Name, version, manifest checksum, named graphs with their roles and sizes, prefixes, how inferences were produced, validation status |
| `describe_schema` | Classes with instance counts; properties with asserted and inferred counts, labels, definitions, superproperties, inverses, property chains, domains and ranges |
| `find_entity` | Resources whose label matches some text, ranked exact, prefix, word, then substring; optionally limited to a class (inferred types count) |
| `search` | Entities ranked by how well some words match their labels, aliases, descriptions and any text predicates the bundle declares; optionally limited to classes (inferred types count) |
| `describe_resource` | Every statement about one IRI, outgoing and incoming, each marked asserted, inferred or ontology; paginated |
| `sparql_query` | A read-only SPARQL 1.1 query (SELECT, ASK, CONSTRUCT, DESCRIBE) with a row cap and a timeout |
| `list_queries` | The bundle's named queries: name, title, description, parameters and SPARQL text |
| `run_query` | One named query with its parameters bound, under the same rules as `sparql_query` |

The server also offers the manifest and schema as resources (`kg://manifest`, `kg://schema`) and the bundle's example and named queries as a prompt.

By default `sparql_query` treats the union of all graphs as the default graph, so inferred facts are included. The bundle's prefixes are predeclared, so queries can use them without `PREFIX` lines.

### Search

`search` is for finding entities by topic when you do not know their names. The index is built once when the server starts, from labels (`label_predicates`, except `skos:altLabel`), aliases (`skos:altLabel`), descriptions (`rdfs:comment`, `skos:definition`, `dcterms:description`, IAO definition) and any `search.text_predicates` the manifest declares. Ontology-graph statements are not indexed.

- Words are case-folded and lightly stemmed, and a word of four or more characters also matches longer words that start with it, so `splice` finds "splicing" and "SpliceAI".
- Scores follow BM25: rare words count more, and a short label that matches beats a long description that mentions the word. Label matches weigh 3, aliases 2, descriptions and declared text 1.
- The score is multiplied by a class-rarity prior, so a handful of entities of a rare class rank above thousands of records of a common class that repeat the same word. No class takes more than three of the leading places.
- `types` keeps only entities with one of the given classes; `limit` and `offset` page through results. Each result lists the fields that matched and its score.

- Labels and aliases are also indexed squashed: case folded, with everything but letters and digits removed. The query is squashed too, whole and per word, and a squashed term matches only a whole squashed label. So `ESM2` finds "ESM-2" and `ESM-2` finds "ESM2".

`find_entity` remains the tool for an exact or partial name.

`scripts/search_recall.py` measures recall at k on a real bundle from gold pairs the caller supplies (a SPARQL file or JSON Lines), and reports load time, index time and peak memory on the same run.

### Drafts

`serve --drafts` adds four tools that let one local client build new records against the release and export them as a batch for the dataset's own import. The records are the JSON the dataset's store uses, not RDF. The bundle manifest's `draft` block (or `--draft-config`) says how records are shaped; nothing about any one dataset is in the server ([docs/bundle-format.md](docs/bundle-format.md#draft-block)).

| Tool | What it does |
| --- | --- |
| `draft_upsert` | Adds a new record to the overlay, or replaces a draft with the same id. Refused if the id is already in the release. Returns the status (`added`, `replaced` or `unchanged`), problems, warnings and possible duplicates |
| `draft_remove` | Drops a draft |
| `draft_diff` | Every draft with its links, problems, warnings and possible duplicates, checked again across the whole overlay |
| `draft_export` | Writes `batch.jsonl` (one record per line, sorted by id) and `summary.json` (the diff and the batch checksum) to `--draft-dir`, and returns both inline. Refused while any draft has problems |

Checks, in order:

1. The record against `record_schema` (shape, required and unknown fields, enumerations, id pattern).
2. The kind is known and draftable.
3. Numbers: integers beyond 2^53 and non-finite numbers are refused.
4. Links: no duplicates, no link to itself, a known relation, and the relation allowed from this kind to the target's kind. The target's kind comes from its asserted type in the release, or from the draft.
5. Reference fields name records of the allowed kinds.

A link target found in neither the release nor the drafts is a warning, not a problem, because the store may have gained it after the release was cut. Possible duplicates are the top three `search` hits of the same kind on the record's label, plus the release records and drafts whose squashed label is the same (release matches capped at ten, same kind first; `same_name_omitted` counts the rest). The caller rules on each one.

All of this is advisory. The dataset's own import is the gate.

What drafts do not do, in this version:

- Change existing records. `draft_diff` always returns an empty `changed` list.
- Appear in `search`, `find_entity`, `describe_resource` or `sparql_query`. Drafts are not projected into RDF, so they get no inferred statements either.
- Persist. The overlay is gone when the process exits; export first.

`--drafts` is refused over `streamable-http`, so a hosted server stays read-only. Files are written only under `--draft-dir`; `draft_export` takes no path.

### Named graphs

The manifest maps role names to named-graph IRIs. `asserted`, `inferred` and `ontology` have a fixed meaning to the server. A bundle can declare other roles too, such as `derived` for findings computed from the data. Every declared role:

- is listed by `dataset_info` with its size and description;
- labels statements in `describe_resource`;
- can be passed to `sparql_query`, for example `graphs: ["derived"]` or `graphs: ["asserted", "inferred"]`. Graph IRIs work too.

An unknown role name is refused with the list of roles the bundle declares.

### Named queries

A bundle can ship parameterised SPARQL queries in its manifest, so clients can ask the questions the dataset was built to answer without writing SPARQL. `list_queries` shows them and `run_query` runs one:

```json
{ "name": "owner_of", "params": { "pet": "id:rex" }, "max_rows": 100 }
```

Parameters appear in the query text as `$name`. The server checks each value against its declared type before it goes into the query:

| Type | Accepts | Written into the query as |
| --- | --- | --- |
| `iri` | A full IRI, `<IRI>` or a prefixed name | `<https://...>`, after IRI validation |
| `string` | Any text | An escaped string literal |
| `integer` | A whole number, or a string of digits | Bare digits, so it also works in `LIMIT` |

Values never enter the query text without validation. An optional parameter with no value and no default is left as an unbound variable. The bound query is checked again for updates and `SERVICE`, then runs with the same timeout and row cap as `sparql_query`. The result includes the SPARQL that ran.

## Command line

```text
rdf-kg-mcp serve BUNDLE [--transport stdio|streamable-http] [--host H] [--port P]
                        [--store memory|disk] [--query-timeout SECONDS] [--max-rows N]
                        [--drafts [--draft-dir DIR] [--draft-config FILE]]
rdf-kg-mcp verify BUNDLE [--quick] [--json]
rdf-kg-mcp manifest DIR --dataset NAME [--version V] [--graph ROLE=IRI ...] [FILES ...]
rdf-kg-mcp pack DIR -o BUNDLE.tar.gz
```

`BUNDLE` is a directory, a `manifest.json`, a `.tar.gz`, or an HTTPS URL of a manifest or archive. Downloads go to `~/.cache/rdf-kg-mcp` (set `RDF_KG_MCP_CACHE` or `--cache-dir` to change it).

- `serve` verifies every checksum before loading. `--store disk` keeps an on-disk store per bundle checksum, so later starts skip loading. SPARQL queries run in a separate worker process; one that passes `--query-timeout` (default 30 seconds) is stopped and the worker restarts. `--query-timeout 0` runs queries in-process with no timeout.
- `verify` checks the manifest against the schema, each file's checksum and, unless `--quick`, each file's statement count. It exits non-zero on any problem, so producers can run it in CI.
- `manifest` writes or refreshes `manifest.json`, filling in checksums and statement counts.
- `pack` writes a byte-reproducible archive: the same bundle always gives the same bytes.

On a graph of about 400,000 statements, loading takes under half a second and each tool call under a tenth of a second; the process uses about 350 MB, plus a second copy in the query worker. On the Rewire bundle of 2.77 million statements (release `2026-11-20-72316db0764b`, 791 MB unpacked), loading took 3 to 6 seconds, the search index about 2 seconds, and peak memory was 2.1 to 2.6 GB before the query worker starts (an Apple laptop, in-memory store). A search takes 30 to 55 ms, and `serve --drafts` was ready in under 7 seconds.

## Making a bundle

Any pipeline that writes RDF files can produce a bundle. A typical layout:

```text
bundle/
  manifest.json
  asserted.nq        # source facts, in the asserted named graph
  inferred.nq        # facts derived by a reasoner, in the inferred named graph
  ontology/*.ttl     # the vocabulary and axioms, loaded into the ontology graph
```

Then:

```sh
rdf-kg-mcp manifest bundle --dataset my-graph --version 2026-10-09 \
  --graph asserted=https://example.org/graph/asserted \
  --graph inferred=https://example.org/graph/inferred \
  --graph ontology=https://example.org/graph/ontology
rdf-kg-mcp verify bundle
rdf-kg-mcp pack bundle -o my-graph-2026-10-09.tar.gz
```

Triples files (`.ttl`, `.nt`, `.rdf`, `.owl`) need a `graph` entry in the manifest to go into a named graph; the `manifest` command keeps any you add by hand. See [docs/bundle-format.md](docs/bundle-format.md) for the full format.

Why inference happens at build time, not in the server: every consumer of the bundle sees the same facts, queries with `LIMIT` and `FILTER` behave correctly, and the inferred graph can be reviewed and versioned like any other data.

## Security

- The server never writes to the store, and queries cannot modify it. Drafts live in memory, outside the store.
- `--drafts` works over stdio only. Exports go only to `--draft-dir`.
- `SERVICE` is refused, so a query cannot make the server send requests elsewhere.
- Archives with links, absolute paths or `..` entries are refused.
- Over `streamable-http` the server has no authentication of its own. Bind it to localhost (the default) or put it behind a proxy that authenticates.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

## Licence

MIT. See [LICENSE](LICENSE).
