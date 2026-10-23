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
    except (GraphError, QueryError) as error:
        raise ToolError(str(error)) from error


def instructions(kg: KnowledgeGraph) -> str:
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
    return f"""Read-only knowledge graph: {bundle.dataset} {bundle.version or ""}.
{bundle.manifest.get("description") or ""}

Named graphs by role:
{roles or "none declared"}
Queries see the union of all graphs unless restricted with the graphs argument, so inferred facts are
already included.

Suggested order: describe_schema to learn the classes and properties (including inverse properties and
property chains that produce shortcuts), find_entity to turn names into IRIs, describe_resource to see
everything about one IRI, then sparql_query for joins, filters and counts. Prefixes from describe_schema
are predeclared in SPARQL. Results are capped; add LIMIT and ORDER BY for stable paging.{queries}"""


def build_server(kg: KnowledgeGraph, runner: QueryRunner | None, max_rows: int = 1000) -> MCPServer:
    """Create the server. Without a runner, queries run in-process with no timeout (used in tests)."""
    server = MCPServer(name="rdf-kg-mcp", version=__version__, instructions=instructions(kg))
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
