from __future__ import annotations

import time

import pytest

from rdf_kg_mcp.bundle import Bundle, NamedQuery
from rdf_kg_mcp.graph import GraphError, KnowledgeGraph
from rdf_kg_mcp.query import (
    QueryError,
    QueryRunner,
    QueryTimeout,
    bind_params,
    check_query,
    placeholders,
    run_query,
)

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


def test_graph_restriction_by_declared_role(kg: KnowledgeGraph) -> None:
    result = run_query(kg, "SELECT ?f WHERE { ?f a ex:Finding } ORDER BY ?f", 10, graphs=["derived"])
    assert result["rows"] == [{"f": "id:finding1"}, {"f": "id:finding2"}]
    assert run_query(kg, "SELECT ?f WHERE { ?f a ex:Finding }", 10, graphs=["asserted"])["rows"] == []


def test_unknown_graph_role_lists_the_roles(kg: KnowledgeGraph) -> None:
    with pytest.raises(GraphError, match="Roles in this bundle: asserted, inferred, ontology, derived"):
        run_query(kg, "ASK { ?s ?p ?o }", 1, graphs=["nope"])


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


def named(kg: KnowledgeGraph, name: str) -> NamedQuery:
    return next(q for q in kg.bundle.queries if q.name == name)


def test_placeholders_skip_strings_iris_and_comments() -> None:
    sparql = 'SELECT * WHERE { ?s ?p "$a" ; <https://x.org/$b> $c ; ?q $cd } # $e\nLIMIT $f'
    assert placeholders(sparql) == {"c", "cd", "f"}


def test_bind_params_writes_terms(kg: KnowledgeGraph) -> None:
    sparql = bind_params(kg, named(kg, "owner_of"), {"pet": "id:rex"})
    assert sparql == "SELECT ?owner WHERE { <https://example.org/id/rex> ex:ownedBy ?owner }"
    result = run_query(kg, sparql, 10)
    assert result["rows"] == [{"owner": "id:alice"}]
    same = bind_params(kg, named(kg, "owner_of"), {"pet": "<https://example.org/id/rex>"})
    assert same == sparql


def test_bind_params_defaults_and_integers(kg: KnowledgeGraph) -> None:
    query = named(kg, "dogs_named")
    assert len(run_query(kg, bind_params(kg, query, {"text": "Dog 1"}), 100)["rows"]) == 3
    for limit in (5, "5"):
        rows = run_query(kg, bind_params(kg, query, {"text": "Dog 1", "limit": limit}), 100)["rows"]
        assert [r["label"] for r in rows] == ['"Dog 10"', '"Dog 11"', '"Dog 12"', '"Dog 13"', '"Dog 14"']


def test_optional_param_without_value_stays_unbound(kg: KnowledgeGraph) -> None:
    query = named(kg, "findings")
    everything = run_query(kg, bind_params(kg, query, {}), 10)["rows"]
    assert [r["finding"] for r in everything] == ["id:finding1", "id:finding2"]
    about_rex = run_query(kg, bind_params(kg, query, {"about": "id:rex"}), 10)["rows"]
    assert about_rex == [{"finding": "id:finding1", "about": "id:rex"}]


def test_string_params_are_escaped(kg: KnowledgeGraph) -> None:
    query = named(kg, "dogs_named")
    attack = '") } } INSERT DATA { <a:b> <a:b> <a:b> } #'
    sparql = bind_params(kg, query, {"text": attack + "\\ and a\nnewline"})
    assert "INSERT DATA" in sparql  # only inside the literal
    assert run_query(kg, sparql, 10)["rows"] == []
    assert placeholders(sparql) == set()


@pytest.mark.parametrize(
    "params, message",
    [
        ({"pet": "https://example.org/id/rex> } ; DROP ALL #"}, "pet"),
        ({"pet": "https://example.org/id/a b"}, "pet"),
        ({"pet": "https://example.org/id/x}"}, "pet"),
        ({"pet": 'https://example.org/id/x"'}, "pet"),
        ({"pet": "rex"}, "pet"),
        ({"pet": 42}, "must be an IRI"),
        ({}, "pet is required"),
        ({"pet": "id:rex", "other": "x"}, "Unknown parameter"),
    ],
)
def test_bad_iri_params_are_refused(kg: KnowledgeGraph, params: dict, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        bind_params(kg, named(kg, "owner_of"), params)


@pytest.mark.parametrize("limit", ["1; DELETE WHERE { ?s ?p ?o }", "1.5", True, 1.5, "", " 1", ["1"]])
def test_bad_integer_params_are_refused(kg: KnowledgeGraph, limit: object) -> None:
    with pytest.raises(QueryError, match="limit must be an integer"):
        bind_params(kg, named(kg, "dogs_named"), {"text": "Dog", "limit": limit})


def test_string_param_must_be_a_string(kg: KnowledgeGraph) -> None:
    with pytest.raises(QueryError, match="text must be a string"):
        bind_params(kg, named(kg, "dogs_named"), {"text": 5})


def test_bound_query_is_checked_again(kg: KnowledgeGraph) -> None:
    update = NamedQuery(name="bad", title="Bad", sparql="DELETE WHERE { ?s ?p ?o }")
    with pytest.raises(QueryError, match="read-only"):
        bind_params(kg, update, {})
