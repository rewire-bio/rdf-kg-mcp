from __future__ import annotations

from pathlib import Path

import pytest

from rdf_kg_mcp.bundle import open_bundle
from rdf_kg_mcp.graph import LOADED_MARKER, GraphError, KnowledgeGraph, load_store

from .conftest import EX, ID


def test_info_reports_graph_roles_and_sizes(kg: KnowledgeGraph) -> None:
    info = kg.info()
    assert info["dataset"] == "pets" and info["version"] == "2026-01-01"
    roles = {g["role"]: g["statements"] for g in info["graphs"]}
    assert roles == {"asserted": 73, "inferred": 34, "ontology": roles["ontology"]}
    assert info["example_queries"] == ["Dogs and owners"]
    assert info["prefixes"]["ex"] == EX


def test_compact_and_expand(kg: KnowledgeGraph) -> None:
    assert kg.compact(f"{EX}Dog") == "ex:Dog"
    assert kg.compact("https://elsewhere.org/a/b") == "<https://elsewhere.org/a/b>"
    assert kg.expand("ex:Dog") == f"{EX}Dog"
    assert kg.expand(f"<{EX}Dog>") == f"{EX}Dog"
    assert kg.expand(f"{EX}Dog") == f"{EX}Dog"
    for bad in ["nope:Dog", "Dog", "", "_:b1", "ex:has space"]:
        with pytest.raises(GraphError):
            kg.expand(bad)


def test_find_entity_ranks_exact_then_prefix_then_word(kg: KnowledgeGraph) -> None:
    results = kg.find_entity("dog")["results"]
    assert results[0]["id"] == "ex:Dog" and results[0]["match"] == "exact"
    assert results[1]["label"] == "Dog 01" and results[1]["match"] == "prefix"
    assert [r["match"] for r in kg.find_entity("smith")["results"]] == ["word"]
    assert kg.find_entity("ngdo")["results"][0]["label"] == "United Kingdom"


def test_find_entity_type_filter_uses_inferred_types(kg: KnowledgeGraph) -> None:
    found = kg.find_entity("rex", type="ex:Animal")
    assert [r["id"] for r in found["results"]] == ["id:rex"]
    assert "ex:Animal" in found["results"][0]["types"]
    assert kg.find_entity("rex", type="ex:Person")["total"] == 0


def test_find_entity_limit_and_total(kg: KnowledgeGraph) -> None:
    found = kg.find_entity("dog", limit=5)
    assert found["total"] == 31 and len(found["results"]) == 5


def test_describe_resource_marks_asserted_and_inferred(kg: KnowledgeGraph) -> None:
    rex = kg.describe_resource("id:rex")
    out = {(s["predicate"], s["object"]): s["graphs"] for s in rex["outgoing"]["statements"]}
    assert out[("rdf:type", "ex:Dog")] == ["asserted"]
    assert out[("rdf:type", "ex:Animal")] == ["inferred"]
    assert out[("ex:petLivesIn", "id:leeds")] == ["inferred"]
    assert out[("ex:weight", '"30.5"^^xsd:decimal')] == ["asserted"]
    assert out[("rdfs:comment", '"Good dog"@en')] == ["asserted"]
    graphs = [s["graphs"][0] for s in rex["outgoing"]["statements"]]
    assert graphs == sorted(graphs)  # asserted first
    incoming = rex["incoming"]["statements"]
    assert incoming == [
        {
            "predicate": "ex:owns",
            "subject": "id:alice",
            "graphs": ["inferred"],
            "subject_label": "Alice Smith",
        }
    ]


def test_describe_resource_pages(kg: KnowledgeGraph) -> None:
    animal = kg.describe_resource("ex:Animal", direction="in", limit=10, offset=5)
    assert animal["incoming"]["total"] == 32  # 31 animals plus ex:Dog subClassOf
    assert animal["incoming"]["returned"] == 10 and "outgoing" not in animal


def test_describe_unknown_resource_explains(kg: KnowledgeGraph) -> None:
    assert "find_entity" in kg.describe_resource(f"{ID}nobody")["note"]
    with pytest.raises(GraphError):
        kg.describe_resource("id:rex", direction="sideways")


def test_schema_describes_classes_and_property_axioms(kg: KnowledgeGraph) -> None:
    schema = kg.describe_schema()
    classes = {c["id"]: c for c in schema["classes"]}
    assert classes["ex:Dog"]["instances"] == 31
    assert classes["ex:Animal"]["instances"] == 31
    assert classes["ex:Dog"]["subclass_of"] == ["ex:Animal"]
    assert classes["ex:Animal"]["description"] == "A living creature."
    assert "ex:Unused" not in classes and "owl:Class" not in classes
    props = {p["id"]: p for p in schema["properties"]}
    assert props["ex:petLivesIn"]["chain"] == ["ex:ownedBy", "ex:livesIn"]
    assert props["ex:petLivesIn"]["by_graph"] == {"inferred": 1}
    assert props["ex:owns"]["inverse_of"] == ["ex:ownedBy"]
    assert props["ex:ownedBy"]["inverse_of"] == ["ex:owns"]
    assert props["ex:partOf"]["characteristics"] == ["transitive"]
    assert props["ex:partOf"]["by_graph"] == {"asserted": 2, "inferred": 1}
    assert "ex:Unused" in {c["id"] for c in kg.describe_schema(include_unused=True)["classes"]}


def test_disk_store_is_reused(bundle_dir: Path, tmp_path: Path) -> None:
    bundle = open_bundle(str(bundle_dir))
    first = load_store(bundle, tmp_path)
    assert len(first) == len(KnowledgeGraph(bundle, first).store)
    del first
    marker = next(tmp_path.glob(f"store-*/{LOADED_MARKER}"))
    stamp = marker.stat().st_mtime_ns
    second = load_store(bundle, tmp_path)
    assert marker.stat().st_mtime_ns == stamp
    assert len(second) > 100
