from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rdf_kg_mcp.bundle import Bundle, open_bundle, write_manifest
from rdf_kg_mcp.graph import KnowledgeGraph, load_store

EX = "https://example.org/vocab#"
ID = "https://example.org/id/"
ASSERTED = "https://example.org/graph/asserted"
INFERRED = "https://example.org/graph/inferred"
ONTOLOGY = "https://example.org/graph/ontology"
DERIVED = "https://example.org/graph/derived"
QUERIES = [
    {
        "name": "owner_of",
        "title": "Owner of a pet",
        "description": "Who owns the given pet.",
        "sparql": "SELECT ?owner WHERE { $pet ex:ownedBy ?owner }",
        "params": [{"name": "pet", "type": "iri", "required": True, "description": "The pet."}],
    },
    {
        "name": "dogs_named",
        "title": "Dogs whose label contains some text",
        "sparql": "SELECT ?dog ?label WHERE { ?dog a ex:Dog ; rdfs:label ?label "
        "FILTER(CONTAINS(?label, $text)) } ORDER BY ?dog LIMIT $limit",
        "params": [
            {"name": "text", "type": "string", "required": True},
            {"name": "limit", "type": "integer", "default": 3},
        ],
    },
    {
        "name": "findings",
        "title": "Findings, optionally about one resource",
        "sparql": "SELECT ?finding ?about WHERE { GRAPH <https://example.org/graph/derived> "
        "{ ?finding ex:about ?about } FILTER(COALESCE(?about = $about, true)) } ORDER BY ?finding",
        "params": [{"name": "about", "type": "iri"}],
    },
]

ONTOLOGY_TTL = """\
@prefix ex: <https://example.org/vocab#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Animal a owl:Class ; rdfs:label "Animal" ; rdfs:comment "A living creature." .
ex:Dog a owl:Class ; rdfs:subClassOf ex:Animal ; rdfs:label "Dog" .
ex:Person a owl:Class ; rdfs:label "Person" .
ex:Unused a owl:Class ; rdfs:label "Unused class" .
ex:owns a owl:ObjectProperty ; rdfs:label "owns" ; owl:inverseOf ex:ownedBy .
ex:ownedBy a owl:ObjectProperty ; rdfs:label "owned by" .
ex:livesIn a owl:ObjectProperty ; rdfs:label "lives in" .
ex:petLivesIn a owl:ObjectProperty ; rdfs:label "pet lives in" ;
    owl:propertyChainAxiom ( ex:ownedBy ex:livesIn ) .
ex:partOf a owl:ObjectProperty, owl:TransitiveProperty ; rdfs:label "part of" .
"""


def _quad(s: str, p: str, o: str, g: str) -> str:
    return f"{s} {p} {o} <{g}> ."


def _asserted() -> list[str]:
    a = ASSERTED
    lines = [
        _quad(f"<{ID}rex>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Dog>", a),
        _quad(f"<{ID}rex>", "<http://www.w3.org/2000/01/rdf-schema#label>", '"Rex"', a),
        _quad(f"<{ID}rex>", "<http://www.w3.org/2000/01/rdf-schema#comment>", '"Good dog"@en', a),
        _quad(f"<{ID}rex>", f"<{EX}weight>", '"30.5"^^<http://www.w3.org/2001/XMLSchema#decimal>', a),
        _quad(f"<{ID}rex>", f"<{EX}ownedBy>", f"<{ID}alice>", a),
        _quad(f"<{ID}alice>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Person>", a),
        _quad(f"<{ID}alice>", "<http://www.w3.org/2000/01/rdf-schema#label>", '"Alice Smith"', a),
        _quad(f"<{ID}alice>", f"<{EX}livesIn>", f"<{ID}leeds>", a),
        _quad(f"<{ID}leeds>", "<http://www.w3.org/2000/01/rdf-schema#label>", '"Leeds"', a),
        _quad(f"<{ID}leeds>", f"<{EX}partOf>", f"<{ID}england>", a),
        _quad(f"<{ID}england>", "<http://www.w3.org/2000/01/rdf-schema#label>", '"England"', a),
        _quad(f"<{ID}england>", f"<{EX}partOf>", f"<{ID}uk>", a),
        _quad(f"<{ID}uk>", "<http://www.w3.org/2000/01/rdf-schema#label>", '"United Kingdom"', a),
    ]
    for i in range(1, 31):
        lines.append(
            _quad(f"<{ID}dog{i:02d}>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Dog>", a)
        )
        lines.append(
            _quad(f"<{ID}dog{i:02d}>", "<http://www.w3.org/2000/01/rdf-schema#label>", f'"Dog {i:02d}"', a)
        )
    return sorted(lines)


def _inferred() -> list[str]:
    g = INFERRED
    lines = [
        _quad(f"<{ID}rex>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Animal>", g),
        _quad(f"<{ID}alice>", f"<{EX}owns>", f"<{ID}rex>", g),
        _quad(f"<{ID}rex>", f"<{EX}petLivesIn>", f"<{ID}leeds>", g),
        _quad(f"<{ID}leeds>", f"<{EX}partOf>", f"<{ID}uk>", g),
    ]
    for i in range(1, 31):
        lines.append(
            _quad(
                f"<{ID}dog{i:02d}>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Animal>", g
            )
        )
    return sorted(lines)


def _derived() -> list[str]:
    g = DERIVED
    return sorted(
        [
            _quad(
                f"<{ID}finding1>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Finding>", g
            ),
            _quad(f"<{ID}finding1>", f"<{EX}about>", f"<{ID}rex>", g),
            _quad(
                f"<{ID}finding2>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", f"<{EX}Finding>", g
            ),
            _quad(f"<{ID}finding2>", f"<{EX}about>", f"<{ID}alice>", g),
        ]
    )


def make_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "asserted.nq").write_text("\n".join(_asserted()) + "\n")
    (root / "inferred.nq").write_text("\n".join(_inferred()) + "\n")
    (root / "derived.nq").write_text("\n".join(_derived()) + "\n")
    (root / "ontology").mkdir(exist_ok=True)
    (root / "ontology" / "pets.ttl").write_text(ONTOLOGY_TTL)
    write_manifest(
        root,
        {
            "dataset": "pets",
            "version": "2026-01-01",
            "description": "A tiny test dataset.",
            "license": "CC0-1.0",
            "graphs": {"asserted": ASSERTED, "inferred": INFERRED, "ontology": ONTOLOGY, "derived": DERIVED},
            "graph_descriptions": {"derived": "Findings computed from the other graphs."},
            "prefixes": {"ex": EX, "id": ID},
            "files": {"ontology/pets.ttl": {"graph": ONTOLOGY}},
            "inferred": {"reasoner": {"name": "hand-written", "profile": "OWL 2 RL"}},
            "example_queries": [
                {"title": "Dogs and owners", "query": "SELECT ?dog ?owner WHERE { ?dog ex:ownedBy ?owner }"}
            ],
            "queries": QUERIES,
        },
    )
    return root


@pytest.fixture(scope="session")
def bundle_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_bundle(tmp_path_factory.mktemp("bundle"))


@pytest.fixture
def fresh_bundle_dir(bundle_dir: Path, tmp_path: Path) -> Path:
    """A copy that a test may modify."""
    copy = tmp_path / "bundle"
    shutil.copytree(bundle_dir, copy)
    return copy


@pytest.fixture(scope="session")
def bundle(bundle_dir: Path) -> Bundle:
    return open_bundle(str(bundle_dir))


@pytest.fixture(scope="session")
def kg(bundle: Bundle) -> KnowledgeGraph:
    return KnowledgeGraph(bundle, load_store(bundle))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDF_KG_MCP_CACHE", str(tmp_path / "cache"))
