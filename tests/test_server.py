from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import Client

from rdf_kg_mcp.bundle import Bundle
from rdf_kg_mcp.graph import KnowledgeGraph
from rdf_kg_mcp.query import QueryRunner
from rdf_kg_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(kg: KnowledgeGraph) -> AsyncIterator[Client]:
    async with Client(build_server(kg, None, max_rows=20)) as c:
        yield c


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def error_text(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text  # type: ignore[union-attr]


async def test_tools_are_listed_as_read_only(client: Client) -> None:
    tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == [
        "dataset_info",
        "describe_schema",
        "find_entity",
        "describe_resource",
        "sparql_query",
    ]
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description


async def test_workflow_from_name_to_inferred_fact(client: Client) -> None:
    info = await call(client, "dataset_info")
    assert info["dataset"] == "pets"
    schema = await call(client, "describe_schema")
    assert "ex:petLivesIn" in {p["id"] for p in schema["properties"]}
    found = await call(client, "find_entity", text="Rex", type="ex:Animal")
    rex = found["results"][0]["id"]
    described = await call(client, "describe_resource", iri=rex, direction="out")
    assert {
        "predicate": "ex:petLivesIn",
        "object": "id:leeds",
        "graphs": ["inferred"],
        "object_label": "Leeds",
    } in (described["outgoing"]["statements"])
    rows = await call(
        client,
        "sparql_query",
        query=f"SELECT ?place WHERE {{ {rex} ex:petLivesIn/ex:partOf ?place }} ORDER BY ?place",
    )
    # Leeds is part of England (asserted) and of the UK (inferred by transitivity).
    assert rows["rows"] == [{"place": "id:england"}, {"place": "id:uk"}]


async def test_sparql_row_cap_and_graph_restriction(client: Client) -> None:
    capped = await call(client, "sparql_query", query="SELECT ?d WHERE { ?d a ex:Dog }")
    assert len(capped["rows"]) == 20 and capped["truncated"]
    asserted = await call(
        client, "sparql_query", query="SELECT ?d WHERE { ?d a ex:Animal }", graphs=["asserted"]
    )
    assert asserted["rows"] == []


async def test_caller_mistakes_are_explained(client: Client) -> None:
    assert "read-only" in await error_text(client, "sparql_query", query="INSERT DATA { <a:b> <a:b> <a:b> }")
    assert "SERVICE" in await error_text(
        client, "sparql_query", query="SELECT * WHERE { SERVICE <http://example.org/s> { ?s ?p ?o } }"
    )
    assert "Known prefixes" in await error_text(client, "describe_resource", iri="nope:x")
    assert "empty" in await error_text(client, "find_entity", text="  ")


async def test_resources_and_prompt(client: Client) -> None:
    resources = {str(r.uri) for r in (await client.list_resources()).resources}
    assert resources == {"kg://manifest", "kg://schema"}
    manifest = await client.read_resource("kg://manifest")
    assert json.loads(manifest.contents[0].text)["dataset"] == "pets"  # type: ignore[union-attr]
    prompt = await client.get_prompt("example_queries")
    assert "Dogs and owners" in prompt.messages[0].content.text  # type: ignore[union-attr]


async def test_queries_through_the_worker(kg: KnowledgeGraph, bundle: Bundle) -> None:
    runner = QueryRunner(bundle, None, timeout=20)
    try:
        async with Client(build_server(kg, runner)) as c:
            result = await call(c, "sparql_query", query="ASK { id:alice ex:owns id:rex }")
            assert result == {"type": "boolean", "boolean": True}
            assert "syntax" in await error_text(c, "sparql_query", query="SELECT ?x WHERE {")
    finally:
        runner.close()
