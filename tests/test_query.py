from __future__ import annotations

import time

import pytest

from rdf_kg_mcp.bundle import Bundle
from rdf_kg_mcp.graph import KnowledgeGraph
from rdf_kg_mcp.query import QueryError, QueryRunner, QueryTimeout, check_query, run_query

SLOW = "SELECT (COUNT(*) AS ?n) WHERE { ?a ?b ?c . ?d ?e ?f . ?g ?h ?i . ?j ?k ?l . ?m ?n2 ?o }"


@pytest.mark.parametrize(
    "query",
    [
        "INSERT DATA { <a:b> <a:b> <a:b> }",
        "DELETE WHERE { ?s ?p ?o }",
        "LOAD <http://example.org/data.ttl>",
        "CLEAR ALL",
        "SELECT * WHERE { SERVICE <http://example.org/sparql> { ?s ?p ?o } }",
        "select * where { service <http://example.org/sparql> { ?s ?p ?o } }",
        "",
    ],
)
def test_refused_queries(query: str) -> None:
    with pytest.raises(QueryError):
        check_query(query)


@pytest.mark.parametrize(
    "query",
    [
        'SELECT ?s WHERE { ?s ?p "SERVICE" }',
        "SELECT ?s WHERE { ?s <http://example.org/SERVICE> ?o }",
        "# SERVICE in a comment\nASK { ?s ?p ?o }",
        "PREFIX ex: <https://example.org/vocab#> DESCRIBE ex:Dog",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    ],
)
def test_allowed_queries(query: str) -> None:
    check_query(query)


def test_select_uses_prefixes_and_union_of_graphs(kg: KnowledgeGraph) -> None:
    result = run_query(kg, "SELECT ?x WHERE { ?x a ex:Animal } ORDER BY ?x", 100)
    assert result["type"] == "select" and result["variables"] == ["x"]
    assert len(result["rows"]) == 31 and result["rows"][0] == {"x": "id:dog01"}
    assert result["truncated"] is False


def test_graph_restriction_by_role(kg: KnowledgeGraph) -> None:
    asserted = run_query(kg, "SELECT ?x WHERE { ?x a ex:Animal }", 100, graphs=["asserted"])
    assert asserted["rows"] == []
    both = run_query(kg, "SELECT ?x WHERE { ?x ex:owns ?y }", 100, graphs=["asserted", "inferred"])
    assert both["rows"] == [{"x": "id:alice"}]


def test_row_cap_truncates(kg: KnowledgeGraph) -> None:
    result = run_query(kg, "SELECT ?x WHERE { ?x a ex:Dog }", 10)
    assert len(result["rows"]) == 10 and result["truncated"] is True


def test_unbound_variables_are_omitted(kg: KnowledgeGraph) -> None:
    result = run_query(kg, "SELECT ?x ?w WHERE { ?x ex:ownedBy ?o OPTIONAL { ?o ex:weight ?w } }", 10)
    assert result["rows"] == [{"x": "id:rex"}]


def test_ask_construct_and_literals(kg: KnowledgeGraph) -> None:
    assert run_query(kg, "ASK { id:rex ex:petLivesIn id:leeds }", 1) == {"type": "boolean", "boolean": True}
    graph = run_query(kg, "CONSTRUCT { ?s ex:weight ?w } WHERE { ?s ex:weight ?w }", 10)
    assert graph == {
        "type": "graph",
        "triples": [["id:rex", "ex:weight", '"30.5"^^xsd:decimal']],
        "truncated": False,
    }


def test_syntax_error_is_reported(kg: KnowledgeGraph) -> None:
    with pytest.raises(QueryError, match="syntax"):
        run_query(kg, "SELECT ?x WHERE { ?x a }", 10)


def test_runner_stops_slow_queries_and_recovers(bundle: Bundle) -> None:
    runner = QueryRunner(bundle, None, timeout=1.0)
    try:
        assert runner.run("ASK { ?s ?p ?o }", 1)["boolean"] is True
        started = time.monotonic()
        with pytest.raises(QueryTimeout):
            runner.run(SLOW, 1)
        assert time.monotonic() - started < 10
        assert runner.run("SELECT ?x WHERE { ?x ex:owns ?y }", 5)["rows"] == [{"x": "id:alice"}]
        with pytest.raises(QueryError, match="syntax"):
            runner.run("SELECT nonsense", 5)
        with pytest.raises(QueryError, match="read-only"):
            runner.run("INSERT DATA { <a:b> <a:b> <a:b> }", 5)
    finally:
        runner.close()
