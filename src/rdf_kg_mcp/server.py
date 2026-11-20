"""MCP server exposing a knowledge-graph bundle through read-only tools."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .drafts import DraftError, DraftOverlay
from .graph import GraphError, KnowledgeGraph
from .query import QueryError, QueryRunner, bind_params
from .query import run_query as execute_query

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MAX_ROWS_CEILING = 10_000


@contextmanager
def caller_errors() -> Iterator[None]:
    """Report mistakes in the caller's input to the model; anything else stays a generic error."""
    try:
        yield
    except (GraphError, QueryError, DraftError) as error:
        raise ToolError(str(error)) from error


DRAFT_INSTRUCTIONS = """

Drafts are on for this session. draft_upsert adds a new record to a private overlay, as the JSON the
dataset's store uses (not RDF); it returns problems, warnings and possible duplicates for you to rule
on. Records already in the release cannot be drafted. Drafts are not visible to search, find_entity,
describe_resource or sparql_query. draft_diff lists the drafts with their checks, draft_remove drops
one, and draft_export writes the batch and a summary. Checks here are advisory; the dataset's own
import is the gate."""


def instructions(kg: KnowledgeGraph, drafts: bool = False) -> str:
    bundle = kg.bundle
    descriptions = bundle.graph_descriptions
    roles = "\n".join(
        f"- {role} = <{iri}>" + (f": {descriptions[role]}" if role in descriptions else "")
        for role, iri in sorted(bundle.graphs.items())
    )
    queries = ""
    if bundle.queries:
        names = ", ".join(q.name for q in bundle.queries)
        queries = (
            f"\n\nThe dataset ships named queries ({names}). list_queries shows what each answers and its "
            "parameters; run_query runs one. Prefer them when they fit the question."
        )
    access = "Knowledge graph with a draft overlay" if drafts else "Read-only knowledge graph"
    return f"""{access}: {bundle.dataset} {bundle.version or ""}.
{bundle.manifest.get("description") or ""}

Named graphs by role:
{roles or "none declared"}
Queries see the union of all graphs unless restricted with the graphs argument, so inferred facts are
already included.

Suggested order: describe_schema to learn the classes and properties (including inverse properties and
property chains that produce shortcuts), search to find entities by topic words, find_entity to turn a
known name into an IRI, describe_resource to see
everything about one IRI, then sparql_query for joins, filters and counts. Prefixes from describe_schema
are predeclared in SPARQL. Results are capped; add LIMIT and ORDER BY for stable paging.{queries}""" + (
        DRAFT_INSTRUCTIONS if drafts else ""
    )


def build_server(
    kg: KnowledgeGraph,
    runner: QueryRunner | None,
    max_rows: int = 1000,
    drafts: DraftOverlay | None = None,
) -> MCPServer:
    """Create the server. Without a runner, queries run in-process with no timeout (used in tests).
    With drafts, the four draft tools are added; the release graphs stay read-only either way."""
    server = MCPServer(
        name="rdf-kg-mcp", version=__version__, instructions=instructions(kg, drafts is not None)
    )
    kg.search_index  # noqa: B018 - build the search index at start-up, not on the first call
    queries = {q.name: q for q in kg.bundle.queries}

    async def execute(query: str, rows: int, graphs: list[str] | None) -> dict[str, Any]:
        if runner is None:
            return execute_query(kg, query, rows, graphs)
        return await anyio.to_thread.run_sync(runner.run, query, rows, graphs)

    @server.tool(annotations=READ_ONLY)
    def dataset_info() -> dict[str, Any]:
        """Dataset name, version, checksums, named graphs with their roles and sizes, prefixes,
        how inferences were produced, validation status, example query titles and named query names."""
        return kg.info()

    @server.tool(annotations=READ_ONLY)
    def describe_schema(include_unused: bool = False, limit: int = 200) -> dict[str, Any]:
        """Classes (with instance counts) and properties (with asserted and inferred statement
        counts, labels, definitions, superproperties, inverses, property chains, domains and ranges),
        most used first. Set include_unused to also list ontology terms with no data."""
        return kg.describe_schema(include_unused=include_unused, limit=max(1, limit))

    @server.tool(annotations=READ_ONLY)
    def find_entity(text: str, type: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Find resources whose label matches text (case-insensitive; exact, then prefix, then word,
        then substring matches). Optionally restrict to a class, given as an IRI or prefixed name;
        inferred types count. Returns IRIs to use with describe_resource and sparql_query."""
        with caller_errors():
            return kg.find_entity(text, type=type, limit=min(max(1, limit), 200))

    @server.tool(annotations=READ_ONLY)
    def search(text: str, types: list[str] | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Rank entities by how well text matches their labels, aliases (skos:altLabel) and
        descriptions (rdfs:comment, skos:definition, dcterms:description, IAO definition), plus any
        text predicates the bundle declares. Words match whole or as prefixes after light stemming,
        so "splice" finds "splicing" and "SpliceAI". Entities of rare classes rank above the many
        records of common classes, and no class fills more than three of the leading places.
        types restricts results to entities with any of these classes (IRIs or prefixed names;
        inferred types count). Use offset to page."""
        with caller_errors():
            return kg.search(text, types=types, limit=min(max(1, limit), 200), offset=max(0, offset))

    @server.tool(annotations=READ_ONLY)
    def describe_resource(
        iri: str, direction: Literal["out", "in", "both"] = "both", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """All statements about one resource: outgoing (it as subject) and incoming (it as object),
        each marked with the roles of the graphs it is in (such as asserted, inferred, ontology).
        Asserted statements come first. Accepts a full IRI or prefixed name. Use offset to page
        through large lists."""
        with caller_errors():
            return kg.describe_resource(
                iri, direction=direction, limit=min(max(0, limit), 500), offset=max(0, offset)
            )

    @server.tool(annotations=READ_ONLY)
    async def sparql_query(
        query: str, max_rows: int = max_rows, graphs: list[str] | None = None
    ) -> dict[str, Any]:
        """Run a read-only SPARQL 1.1 query (SELECT, ASK, CONSTRUCT or DESCRIBE). The default graph is
        the union of all graphs, so inferred facts are included; pass graphs (roles listed by
        dataset_info, such as ["asserted"], or graph IRIs) to query only those. Bundle prefixes are
        predeclared. Rows beyond max_rows are dropped and truncated is set; use LIMIT/OFFSET with
        ORDER BY to page."""
        rows = min(max(1, max_rows), MAX_ROWS_CEILING)
        with caller_errors():
            return await execute(query, rows, graphs)

    @server.tool(annotations=READ_ONLY)
    def list_queries() -> dict[str, Any]:
        """Named queries supplied with the dataset: name, title, description, parameters (name,
        type iri, string or integer, required, default) and the SPARQL text. Run one with run_query."""
        return {"queries": [q.summary() for q in kg.bundle.queries]}

    @server.tool(annotations=READ_ONLY)
    async def run_query(
        name: str, params: dict[str, Any] | None = None, max_rows: int = max_rows
    ) -> dict[str, Any]:
        """Run a named query from list_queries. params maps parameter names to values: IRIs as full
        IRIs or prefixed names, strings as plain text, integers as numbers. Same read-only rules,
        timeout and row cap as sparql_query. The result includes the SPARQL that ran."""
        rows = min(max(1, max_rows), MAX_ROWS_CEILING)
        with caller_errors():
            named = queries.get(name)
            if named is None:
                known = ", ".join(queries) or "none"
                raise QueryError(f"Unknown query {name!r}. Named queries in this bundle: {known}")
            sparql = bind_params(kg, named, params)
            return {"query": name, "sparql": sparql, **await execute(sparql, rows, None)}

    if drafts is not None:
        _add_draft_tools(server, drafts)

    @server.resource("kg://manifest", mime_type="application/json", description="The bundle manifest.")
    def manifest() -> str:
        return json.dumps(kg.bundle.manifest, indent=2)

    @server.resource(
        "kg://schema", mime_type="application/json", description="Classes and properties in use."
    )
    def schema() -> str:
        return json.dumps(kg.describe_schema(), indent=2)

    @server.prompt(description="Example and named SPARQL queries supplied with the dataset.")
    def example_queries() -> str:
        examples = kg.bundle.example_queries
        if not examples and not queries:
            return "This bundle has no example queries. Start with describe_schema."
        parts = [f"Example queries for {kg.bundle.dataset}. Prefixes are predeclared."]
        for example in examples:
            parts.append(
                f"## {example['title']}\n{example.get('description', '')}\n```sparql\n{example['query']}\n```"
            )
        for named in queries.values():
            params = ", ".join(f"{p.name} ({p.type})" for p in named.params) or "none"
            parts.append(
                f"## {named.title}\nRun with run_query(name={named.name!r}). Parameters: {params}.\n"
                f"{named.description}\n```sparql\n{named.sparql}\n```"
            )
        return "\n\n".join(parts)

    return server


def _add_draft_tools(server: MCPServer, drafts: DraftOverlay) -> None:
    config = drafts.config
    kinds = ", ".join(config.draft_kinds)
    links = (
        f" Links go in {config.links.field!r} as objects with {config.links.relation!r} and "
        f"{config.links.target!r}; targets may be release records or other drafts."
        if config.links
        else ""
    )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
        description=(
            "Add a new record to this session's draft overlay, or replace a draft with the same "
            f"{config.id_field!r}. Pass the whole record as the dataset's store writes it (JSON, not RDF); "
            f"{config.type_field!r} must be one of: {kinds}.{links} Refused if the id is already in the "
            "release. Returns status (added, replaced or unchanged), problems (the import will refuse "
            "the record), warnings (it may not: a link target newer than the release, for example) and "
            "possible_duplicates: the top search hits of the same kind whose names share at least half "
            "their words with the label, any release record or draft with the same name ignoring case, "
            "spaces and punctuation, and drafts with half their words in common. Rule on each before "
            "exporting."
        ),
    )
    def draft_upsert(record: dict[str, Any]) -> dict[str, Any]:
        with caller_errors():
            return drafts.upsert(record)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        )
    )
    def draft_remove(id: str) -> dict[str, Any]:
        """Drop a draft from the overlay. Drafts that link to it then carry a warning."""
        return drafts.remove(id)

    @server.tool(annotations=READ_ONLY)
    def draft_diff() -> dict[str, Any]:
        """Every draft compared with the release: in this version all are additions (changed is
        always empty). Per record: kind, label, links, problems, warnings and possible duplicates,
        checked again across the whole overlay."""
        return drafts.diff()

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        )
    )
    def draft_export() -> dict[str, Any]:
        """Export the drafts as batch.jsonl (one record per line, sorted by id, deterministic bytes,
        numbers written as JavaScript writes them) and summary.json (the diff and the batch
        checksum). Refused while any draft has problems; a refusal also removes the files of an
        earlier export, so the directory never holds a stale batch.
        Files are written only to the directory the server was started with (--draft-dir), and are
        also returned inline."""
        return drafts.export()
