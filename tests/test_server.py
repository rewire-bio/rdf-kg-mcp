from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from rdf_kg_mcp.bundle import Bundle, open_bundle
from rdf_kg_mcp.graph import KnowledgeGraph, load_store
from rdf_kg_mcp.query import QueryRunner
from rdf_kg_mcp.server import build_server, instructions

from .conftest import DERIVED

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
        "list_queries",
        "run_query",
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


async def test_derived_graph_by_role(client: Client) -> None:
    info = await call(client, "dataset_info")
    assert {
        k: v
        for k, v in next(g for g in info["graphs"] if g["role"] == "derived").items()
        if k != "description"
    } == {"iri": DERIVED, "role": "derived", "statements": 4}
    rows = await call(client, "sparql_query", query="SELECT ?f WHERE { ?f a ex:Finding }", graphs=["derived"])
    assert len(rows["rows"]) == 2
    assert "Unknown graph role" in await error_text(
        client, "sparql_query", query="ASK { ?s ?p ?o }", graphs=["missing"]
    )


async def test_list_and_run_named_queries(client: Client) -> None:
    listed = (await call(client, "list_queries"))["queries"]
    assert [q["name"] for q in listed] == ["owner_of", "dogs_named", "findings"]
    assert listed[1]["params"] == [
        {"name": "text", "type": "string", "description": "", "required": True},
        {"name": "limit", "type": "integer", "description": "", "required": False, "default": 3},
    ]
    result = await call(client, "run_query", name="owner_of", params={"pet": "id:rex"})
    assert result["query"] == "owner_of" and result["rows"] == [{"owner": "id:alice"}]
    assert "<https://example.org/id/rex>" in result["sparql"]
    capped = await call(
        client, "run_query", name="dogs_named", params={"text": "Dog", "limit": 100}, max_rows=4
    )
    assert len(capped["rows"]) == 4 and capped["truncated"]
    assert len((await call(client, "run_query", name="findings"))["rows"]) == 2


async def test_named_query_mistakes_are_explained(client: Client) -> None:
    assert "Named queries in this bundle: owner_of" in await error_text(client, "run_query", name="nope")
    assert "pet is required" in await error_text(client, "run_query", name="owner_of")
    assert "Not an IRI" in await error_text(
        client, "run_query", name="owner_of", params={"pet": "https://x.org/a> } DROP ALL #"}
    )
    assert "must be an integer" in await error_text(
        client, "run_query", name="dogs_named", params={"text": "x", "limit": "1 } DELETE"}
    )


async def test_old_manifest_has_no_named_queries(fresh_bundle_dir: Path) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    for field in ("queries", "graph_descriptions", "example_queries"):
        manifest.pop(field)
    manifest["graphs"].pop("derived")
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    bundle = open_bundle(str(fresh_bundle_dir))
    kg = KnowledgeGraph(bundle, load_store(bundle))
    async with Client(build_server(kg, None)) as c:
        assert (await call(c, "list_queries")) == {"queries": []}
        assert "Named queries in this bundle: none" in await error_text(c, "run_query", name="x")
        info = await call(c, "dataset_info")
        assert info["queries"] == [] and info["example_queries"] == []
        # The derived file is still loaded but has no declared role.
        assert {g["role"] for g in info["graphs"]} == {"asserted", "inferred", "ontology", "other"}
        prompt = await c.get_prompt("example_queries")
        assert "no example queries" in prompt.messages[0].content.text  # type: ignore[union-attr]


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
    text = prompt.messages[0].content.text  # type: ignore[union-attr]
    assert "Dogs and owners" in text and "run_query(name='owner_of')" in text


async def test_queries_through_the_worker(kg: KnowledgeGraph, bundle: Bundle) -> None:
    runner = QueryRunner(bundle, None, timeout=20)
    try:
        async with Client(build_server(kg, runner)) as c:
            result = await call(c, "sparql_query", query="ASK { id:alice ex:owns id:rex }")
            assert result == {"type": "boolean", "boolean": True}
            assert "syntax" in await error_text(c, "sparql_query", query="SELECT ?x WHERE {")
            named = await call(c, "run_query", name="owner_of", params={"pet": "id:rex"})
            assert named["rows"] == [{"owner": "id:alice"}]
    finally:
        runner.close()


def test_instructions_describe_declared_roles_and_queries(kg: KnowledgeGraph) -> None:
    text = instructions(kg)
    assert f"- derived = <{DERIVED}>: Findings computed from the other graphs." in text
    assert "- asserted = <https://example.org/graph/asserted>: Source facts." in text
    assert "named queries (owner_of, dogs_named, findings)" in text
