from __future__ import annotations

from pathlib import Path

import pytest

from rdf_kg_mcp.bundle import open_bundle, write_manifest
from rdf_kg_mcp.graph import GraphError, KnowledgeGraph, load_store
from rdf_kg_mcp.search import stem

EX = "https://example.org/vocab#"
ID = "https://example.org/id/"
ASSERTED = "https://example.org/graph/asserted"
INFERRED = "https://example.org/graph/inferred"
ONTOLOGY = "https://example.org/graph/ontology"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
DESCRIPTION = "http://purl.org/dc/terms/description"

ONTOLOGY_TTL = """\
@prefix ex: <https://example.org/vocab#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:UseCase a owl:Class ; rdfs:label "Splice use case class" .
ex:Model a owl:Class ; rdfs:subClassOf ex:Software ; rdfs:label "Model" .
ex:Record a owl:Class ; rdfs:label "Record" .
"""


def _q(s: str, p: str, o: str, g: str = ASSERTED) -> str:
    obj = o if o.startswith("<") else f'"{o}"'
    return f"<{ID}{s}> <{p}> {obj} <{g}> ."


def _t(s: str, cls: str, g: str = ASSERTED) -> str:
    return _q(s, RDF_TYPE, f"<{EX}{cls}>", g)


def make_search_bundle(root: Path, extra_manifest: dict | None = None) -> Path:
    asserted = [
        _t("uc-splicing", "UseCase"),
        _q("uc-splicing", LABEL, "Prioritise variants for splicing experiments"),
        _t("uc-other", "UseCase"),
        _q("uc-other", LABEL, "Interpret germline variants"),
        _t("spliceai", "Model"),
        _q("spliceai", LABEL, "SpliceAI"),
        _q("spliceai", DESCRIPTION, "Predicts splice acceptor and donor changes."),
        _t("zeta", "Model"),
        _q("zeta", LABEL, "Model Z"),
        _q("zeta", ALT, "Quokka"),
        _t("kin", "Model"),
        _q("kin", LABEL, "Model K"),
        _q("kin", DESCRIPTION, "Scores kinetochore assembly."),
        _t("genome-model", "Model"),
        _q("genome-model", LABEL, "Genome model"),
        _q("note", f"{EX}topic", "Wombat burrows"),
        _q("note", LABEL, "A note"),
    ]
    for i in range(40):
        asserted += [_t(f"r{i}", "Record"), _q(f"r{i}", LABEL, f"Splice site result {i}")]
    inferred = [_t("spliceai", "Software", INFERRED), _t("zeta", "Software", INFERRED)]
    (root / "ontology").mkdir(parents=True, exist_ok=True)
    (root / "asserted.nq").write_text("\n".join(sorted(asserted)) + "\n")
    (root / "inferred.nq").write_text("\n".join(sorted(inferred)) + "\n")
    (root / "ontology" / "o.ttl").write_text(ONTOLOGY_TTL)
    write_manifest(
        root,
        {
            "dataset": "search-test",
            "graphs": {"asserted": ASSERTED, "inferred": INFERRED, "ontology": ONTOLOGY},
            "prefixes": {"ex": EX, "id": ID},
            "files": {"ontology/o.ttl": {"graph": ONTOLOGY}},
            **(extra_manifest or {}),
        },
    )
    return root


def graph(root: Path) -> KnowledgeGraph:
    bundle = open_bundle(str(root))
    return KnowledgeGraph(bundle, load_store(bundle))


@pytest.fixture(scope="module")
def kg(tmp_path_factory: pytest.TempPathFactory) -> KnowledgeGraph:
    return graph(make_search_bundle(tmp_path_factory.mktemp("search")))


def ids(found: dict) -> list[str]:
    return [r["id"] for r in found["results"]]


def test_stem_keeps_short_words() -> None:
    assert stem("splicing") == stem("splice") == stem("spliced") == "splic"
    assert stem("gene") == "gene" and stem("genes") == "gene"


def test_hub_entities_outrank_many_records(kg: KnowledgeGraph) -> None:
    found = kg.search("splice")
    assert ids(found)[:2] == ["id:spliceai", "id:uc-splicing"]
    assert found["total"] == 42
    # No class fills more than three of the leading places.
    assert sum(r["id"].startswith("id:r") for r in found["results"][:5]) <= 3


def test_stem_and_prefix_matches(kg: KnowledgeGraph) -> None:
    by_id = {r["id"]: r for r in kg.search("splicing")["results"]}
    assert "id:uc-splicing" in by_id and "id:spliceai" in by_id
    assert "id:spliceai" in ids(kg.search("splic"))


def test_short_words_are_not_prefix_expanded(kg: KnowledgeGraph) -> None:
    assert kg.search("gen")["total"] == 0
    assert ids(kg.search("genome")) == ["id:genome-model"]


def test_alias_and_description_only_matches(kg: KnowledgeGraph) -> None:
    alias = kg.search("quokka")["results"]
    assert [r["id"] for r in alias] == ["id:zeta"] and alias[0]["matched"] == ["alias"]
    described = kg.search("kinetochore")["results"]
    assert [r["id"] for r in described] == ["id:kin"]
    assert described[0]["matched"] == ["description"]


def test_type_filter_uses_inferred_types(kg: KnowledgeGraph) -> None:
    found = kg.search("splice", types=["ex:Software"])
    assert ids(found) == ["id:spliceai"] and found["types"] == ["ex:Software"]
    assert kg.search("splice", types=["ex:UseCase", "ex:Model"])["total"] == 2


def test_ontology_graph_is_not_searched(kg: KnowledgeGraph) -> None:
    assert "ex:UseCase" not in ids(kg.search("use case class"))


def test_paging(kg: KnowledgeGraph) -> None:
    first = kg.search("splice", limit=10)
    second = kg.search("splice", limit=10, offset=10)
    assert len(first["results"]) == len(second["results"]) == 10
    assert not set(ids(first)) & set(ids(second))


def test_empty_text_is_refused(kg: KnowledgeGraph) -> None:
    with pytest.raises(GraphError, match="letter or digit"):
        kg.search(" -- ")


def test_declared_text_predicates_are_indexed(tmp_path: Path) -> None:
    plain = graph(make_search_bundle(tmp_path / "plain"))
    assert plain.search("wombat")["total"] == 0
    declared = graph(make_search_bundle(tmp_path / "declared", {"search": {"text_predicates": ["ex:topic"]}}))
    found = declared.search("wombat")["results"]
    assert [r["id"] for r in found] == ["id:note"] and found[0]["matched"] == ["text"]
