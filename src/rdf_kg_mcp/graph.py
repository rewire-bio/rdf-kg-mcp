"""An Oxigraph store loaded from a bundle, with the lookups the MCP tools need."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from functools import cached_property
from pathlib import Path
from typing import Any

import pyoxigraph as ox

from .bundle import Bundle, expand_curie

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_FIRST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first"
RDF_REST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest"
RDF_NIL = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
DESCRIPTION_PREDICATES = [
    RDFS + "comment",
    "http://www.w3.org/2004/02/skos/core#definition",
    "http://purl.obolibrary.org/obo/IAO_0000115",
    "http://purl.org/dc/terms/description",
]
PROPERTY_CHARACTERISTICS = {
    OWL + "TransitiveProperty": "transitive",
    OWL + "SymmetricProperty": "symmetric",
    OWL + "FunctionalProperty": "functional",
    OWL + "InverseFunctionalProperty": "inverse functional",
}
BUILTIN_PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": RDFS,
    "owl": OWL,
    "xsd": XSD,
    "skos": "http://www.w3.org/2004/02/skos/core#",
}
_LOCAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$|^$")
_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>"{}|\\^`]+$')
IRI_SCHEMES = {"http", "https", "urn", "ftp", "file", "mailto", "tag", "did", "doi", "info"}
LOADED_MARKER = ".rdf-kg-mcp-loaded"


class GraphError(ValueError):
    """A caller asked for something the graph cannot answer (bad IRI, unknown role)."""


def load_store(bundle: Bundle, store_dir: Path | None = None) -> ox.Store:
    """Load every bundle file into a store. With store_dir, use an on-disk store cached by
    manifest checksum, so a second start with the same bundle skips loading."""
    if store_dir is None:
        store = ox.Store()
        _bulk_load(store, bundle)
        return store
    path = store_dir / f"store-{bundle.manifest_sha256[:16]}"
    marker = path / LOADED_MARKER
    if not marker.is_file() or marker.read_text() != bundle.manifest_sha256:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        store = ox.Store(str(path))
        _bulk_load(store, bundle)
        store.flush()
        store.optimize()
        del store
        marker.write_text(bundle.manifest_sha256)
    return ox.Store.read_only(str(path))


def _bulk_load(store: ox.Store, bundle: Bundle) -> None:
    for entry in bundle.files:
        file = str(bundle.root / entry.path)
        if entry.has_quads:
            store.bulk_load(path=file, format=entry.format)
        else:
            target = ox.NamedNode(entry.graph) if entry.graph else ox.DefaultGraph()
            store.bulk_load(path=file, format=entry.format, to_graph=target)


class KnowledgeGraph:
    def __init__(self, bundle: Bundle, store: ox.Store):
        self.bundle = bundle
        self.store = store
        self.prefixes = {**BUILTIN_PREFIXES, **bundle.prefixes}
        # Longest namespace first, so the most specific prefix wins when compacting.
        self._namespaces = sorted(((ns, p) for p, ns in self.prefixes.items()), key=lambda x: -len(x[0]))
        ontology = bundle.graphs.get("ontology")
        self._ontology_graph = ox.NamedNode(ontology) if ontology else None

    # Terms -----------------------------------------------------------------------------

    def compact(self, iri: str) -> str:
        for ns, prefix in self._namespaces:
            if iri.startswith(ns) and _LOCAL_NAME.match(iri[len(ns) :]):
                return f"{prefix}:{iri[len(ns) :]}"
        return f"<{iri}>"

    def expand(self, term: str) -> str:
        """Accept a full IRI, <IRI> or a prefixed name using the bundle's prefixes."""
        stripped = term.strip()
        iri = expand_curie(stripped, self.prefixes)
        unexpanded = iri == stripped
        scheme = iri.split(":", 1)[0].lower() if ":" in iri else ""
        if not _IRI.match(iri) or (unexpanded and scheme not in IRI_SCHEMES):
            known = ", ".join(sorted(self.prefixes))
            raise GraphError(f"Not an IRI or known prefixed name: {term!r}. Known prefixes: {known}")
        return iri

    def term(self, node: Any) -> str:
        """A compact Turtle-like rendering of an RDF term."""
        if isinstance(node, ox.NamedNode):
            return self.compact(node.value)
        if isinstance(node, ox.BlankNode):
            return f"_:{node.value}"
        if isinstance(node, ox.Literal):
            text = json.dumps(node.value, ensure_ascii=False)
            if node.language:
                return f"{text}@{node.language}"
            datatype = node.datatype.value
            if datatype == XSD + "string":
                return text
            return f"{text}^^{self.compact(datatype)}"
        if isinstance(node, ox.Triple):
            return f"<< {self.term(node.subject)} {self.term(node.predicate)} {self.term(node.object)} >>"
        if isinstance(node, ox.DefaultGraph):
            return "default"
        return str(node)

    def role(self, graph: Any) -> str:
        return self.bundle.role_of(graph.value if isinstance(graph, ox.NamedNode) else None)

    def data_graphs(self) -> list[Any] | None:
        """Graphs that hold data rather than ontology axioms; None means all graphs."""
        if self._ontology_graph is None:
            return None
        graphs: list[Any] = [g for g in self.store.named_graphs() if g != self._ontology_graph]
        graphs.append(ox.DefaultGraph())
        return graphs

    def _select(self, query: str, *, data_only: bool = False) -> list[ox.QuerySolution]:
        graphs = self.data_graphs() if data_only else None
        if graphs is None:
            result = self.store.query(query, use_default_graph_as_union=True)
        else:
            result = self.store.query(query, default_graph=graphs)
        assert isinstance(result, ox.QuerySolutions)
        return list(result)

    # Labels and types --------------------------------------------------------------------

    @cached_property
    def labels(self) -> dict[str, list[str]]:
        found: dict[str, set[str]] = defaultdict(set)
        for predicate in self.bundle.label_predicates:
            for quad in self.store.quads_for_pattern(None, ox.NamedNode(predicate), None):
                if isinstance(quad.subject, ox.NamedNode) and isinstance(quad.object, ox.Literal):
                    found[quad.subject.value].add(quad.object.value)
        return {iri: sorted(values) for iri, values in found.items()}

    @cached_property
    def types(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = defaultdict(set)
        for quad in self.store.quads_for_pattern(None, ox.NamedNode(RDF_TYPE), None):
            if isinstance(quad.subject, ox.NamedNode) and isinstance(quad.object, ox.NamedNode):
                found[quad.subject.value].add(quad.object.value)
        return found

    def label(self, iri: str) -> str | None:
        values = self.labels.get(iri)
        return values[0] if values else None

    def _literal(self, iri: str, predicates: list[str]) -> str | None:
        for predicate in predicates:
            for quad in self.store.quads_for_pattern(ox.NamedNode(iri), ox.NamedNode(predicate), None):
                if isinstance(quad.object, ox.Literal):
                    return quad.object.value
        return None

    def _objects(self, iri: str, predicate: str) -> list[str]:
        return sorted(
            {
                q.object.value
                for q in self.store.quads_for_pattern(ox.NamedNode(iri), ox.NamedNode(predicate), None)
                if isinstance(q.object, ox.NamedNode)
            }
        )

    # Tools -------------------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        m = self.bundle.manifest
        graphs = []
        for graph in sorted(self.store.named_graphs(), key=lambda g: g.value):
            count = sum(1 for _ in self.store.quads_for_pattern(None, None, None, graph))
            graphs.append({"iri": graph.value, "role": self.role(graph), "statements": count})
        default_count = sum(1 for _ in self.store.quads_for_pattern(None, None, None, ox.DefaultGraph()))
        if default_count:
            graphs.append({"iri": None, "role": "default", "statements": default_count})
        return {
            "dataset": self.bundle.dataset,
            "version": self.bundle.version,
            "title": m.get("title"),
            "description": m.get("description"),
            "license": m.get("license"),
            "homepage": m.get("homepage"),
            "manifest_sha256": self.bundle.manifest_sha256,
            "statements": len(self.store),
            "graphs": graphs,
            "prefixes": self.prefixes,
            "inferred": m.get("inferred"),
            "shacl": m.get("shacl"),
            "example_queries": [q["title"] for q in self.bundle.example_queries],
        }

    @cached_property
    def schema(self) -> dict[str, Any]:
        """Classes and properties used by the data, with their ontology definitions."""
        class_counts = {
            row["c"].value: int(row["n"].value)
            for row in self._select(
                "SELECT ?c (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s a ?c FILTER(isIRI(?c)) } GROUP BY ?c",
                data_only=True,
            )
        }
        property_counts: dict[str, dict[str, int]] = defaultdict(dict)
        rows = self.store.query(
            "SELECT ?p ?g (COUNT(*) AS ?n) "
            "WHERE { { GRAPH ?g { ?s ?p ?o } } UNION { ?s ?p ?o } } GROUP BY ?p ?g"
        )
        assert isinstance(rows, ox.QuerySolutions)
        for row in rows:
            graph = row["g"]
            if self._ontology_graph is not None and graph == self._ontology_graph:
                continue
            role = self.role(graph) if graph is not None else "default"
            property_counts[row["p"].value][role] = property_counts[row["p"].value].get(role, 0) + int(
                row["n"].value
            )
        ontology_classes = {
            q.subject.value
            for kind in (OWL + "Class", RDFS + "Class")
            for q in self.store.quads_for_pattern(None, ox.NamedNode(RDF_TYPE), ox.NamedNode(kind))
            if isinstance(q.subject, ox.NamedNode)
        }
        ontology_properties = {
            q.subject.value
            for kind in ("ObjectProperty", "DatatypeProperty", "AnnotationProperty")
            for q in self.store.quads_for_pattern(None, ox.NamedNode(RDF_TYPE), ox.NamedNode(OWL + kind))
            if isinstance(q.subject, ox.NamedNode)
        } | {
            q.subject.value
            for q in self.store.quads_for_pattern(
                None,
                ox.NamedNode(RDF_TYPE),
                ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"),
            )
            if isinstance(q.subject, ox.NamedNode)
        }
        classes = []
        for iri in sorted(set(class_counts) | ontology_classes):
            classes.append(
                {
                    "iri": iri,
                    "id": self.compact(iri),
                    "label": self.label(iri),
                    "description": self._literal(iri, DESCRIPTION_PREDICATES),
                    "instances": class_counts.get(iri, 0),
                    "subclass_of": [self.compact(c) for c in self._objects(iri, RDFS + "subClassOf")],
                }
            )
        properties = []
        for iri in sorted(set(property_counts) | ontology_properties):
            counts = property_counts.get(iri, {})
            characteristics = sorted(
                name for kind, name in PROPERTY_CHARACTERISTICS.items() if kind in self.types.get(iri, set())
            )
            properties.append(
                {
                    "iri": iri,
                    "id": self.compact(iri),
                    "label": self.label(iri),
                    "description": self._literal(iri, DESCRIPTION_PREDICATES),
                    "statements": sum(counts.values()),
                    "by_graph": dict(sorted(counts.items())),
                    "subproperty_of": [self.compact(p) for p in self._objects(iri, RDFS + "subPropertyOf")],
                    "inverse_of": [
                        self.compact(p)
                        for p in self._objects(iri, OWL + "inverseOf") + self._inverse_subjects(iri)
                    ],
                    "chain": [self.compact(p) for p in self._chain(iri)],
                    "characteristics": characteristics,
                    "domain": [self.compact(c) for c in self._objects(iri, RDFS + "domain")],
                    "range": [self.compact(c) for c in self._objects(iri, RDFS + "range")],
                }
            )
        return {"classes": classes, "properties": properties}

    def _inverse_subjects(self, iri: str) -> list[str]:
        return sorted(
            q.subject.value
            for q in self.store.quads_for_pattern(None, ox.NamedNode(OWL + "inverseOf"), ox.NamedNode(iri))
            if isinstance(q.subject, ox.NamedNode)
        )

    def _chain(self, iri: str) -> list[str]:
        for quad in self.store.quads_for_pattern(
            ox.NamedNode(iri), ox.NamedNode(OWL + "propertyChainAxiom"), None
        ):
            node: Any = quad.object
            chain = []
            while node is not None and not (isinstance(node, ox.NamedNode) and node.value == RDF_NIL):
                first = next(iter(self.store.quads_for_pattern(node, ox.NamedNode(RDF_FIRST), None)), None)
                rest = next(iter(self.store.quads_for_pattern(node, ox.NamedNode(RDF_REST), None)), None)
                if first is None or not isinstance(first.object, ox.NamedNode):
                    break
                chain.append(first.object.value)
                node = rest.object if rest else None
            return chain
        return []

    def describe_schema(self, include_unused: bool = False, limit: int = 200) -> dict[str, Any]:
        schema = self.schema
        classes = [c for c in schema["classes"] if include_unused or c["instances"]]
        properties = [p for p in schema["properties"] if include_unused or p["statements"]]
        classes.sort(key=lambda c: (-c["instances"], c["iri"]))
        properties.sort(key=lambda p: (-p["statements"], p["iri"]))
        compact_entries = [
            {k: v for k, v in entry.items() if v not in (None, [], {}) and k != "iri"}
            for entry in classes[:limit]
        ]
        compact_properties = [
            {k: v for k, v in entry.items() if v not in (None, [], {}) and k != "iri"}
            for entry in properties[:limit]
        ]
        return {
            "prefixes": self.prefixes,
            "graphs": self.bundle.graphs,
            "classes": compact_entries,
            "classes_total": len(classes),
            "properties": compact_properties,
            "properties_total": len(properties),
            "note": "Counts cover data graphs (not the ontology graph). by_graph splits property statements "
            "into asserted and inferred.",
        }

    def find_entity(self, text: str, type: str | None = None, limit: int = 20) -> dict[str, Any]:
        needle = text.strip().casefold()
        if not needle:
            raise GraphError("text must not be empty")
        type_iri = self.expand(type) if type else None
        word = re.compile(r"(^|\W)" + re.escape(needle))
        matches: list[tuple[int, int, str, str, str]] = []
        for iri, values in self.labels.items():
            if type_iri and type_iri not in self.types.get(iri, set()):
                continue
            best: tuple[int, str] | None = None
            for value in values:
                folded = value.casefold()
                if folded == needle:
                    rank = 0
                elif folded.startswith(needle):
                    rank = 1
                elif word.search(folded):
                    rank = 2
                elif needle in folded:
                    rank = 3
                else:
                    continue
                if best is None or (rank, value) < best:
                    best = (rank, value)
            if best is None and (iri.casefold() == needle or self.compact(iri).casefold() == needle):
                best = (0, self.label(iri) or "")
            if best is not None:
                matches.append(
                    (best[0], len(best[1]), best[1], iri, ["exact", "prefix", "word", "substring"][best[0]])
                )
        matches.sort()
        return {
            "query": text,
            "type": self.compact(type_iri) if type_iri else None,
            "total": len(matches),
            "results": [
                {
                    "id": self.compact(iri),
                    "iri": iri,
                    "label": label,
                    "types": sorted(self.compact(t) for t in self.types.get(iri, set())),
                    "match": how,
                }
                for _, _, label, iri, how in matches[: max(1, limit)]
            ],
        }

    def describe_resource(
        self, iri: str, direction: str = "both", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if direction not in ("out", "in", "both"):
            raise GraphError("direction must be 'out', 'in' or 'both'")
        node = ox.NamedNode(self.expand(iri))
        result: dict[str, Any] = {
            "id": self.compact(node.value),
            "iri": node.value,
            "label": self.label(node.value),
            "types": sorted(self.compact(t) for t in self.types.get(node.value, set())),
        }
        if direction in ("out", "both"):
            result["outgoing"] = self._edges(
                self.store.quads_for_pattern(node, None, None), "object", limit, offset
            )
        if direction in ("in", "both"):
            result["incoming"] = self._edges(
                self.store.quads_for_pattern(None, None, node), "subject", limit, offset
            )
        if not any(result.get(k, {}).get("total") for k in ("outgoing", "incoming")):
            result["note"] = "No statements mention this IRI. Use find_entity to look up IRIs by label."
        return result

    def _edges(self, quads: Any, other: str, limit: int, offset: int) -> dict[str, Any]:
        merged: dict[tuple[str, str], set[str]] = defaultdict(set)
        nodes: dict[tuple[str, str], Any] = {}
        for quad in quads:
            node = quad.object if other == "object" else quad.subject
            key = (quad.predicate.value, self.term(node))
            merged[key].add(self.role(quad.graph_name))
            nodes[key] = node
        role_order = {"asserted": 0, "default": 1, "inferred": 2, "ontology": 3}
        keys = sorted(merged, key=lambda k: (min(role_order.get(r, 4) for r in merged[k]), k))
        page = keys[offset : offset + max(0, limit)]
        edges = []
        for key in page:
            node = nodes[key]
            edge: dict[str, Any] = {
                "predicate": self.compact(key[0]),
                other: key[1],
                "graphs": sorted(merged[key]),
            }
            if isinstance(node, ox.NamedNode) and (label := self.label(node.value)):
                edge[f"{other}_label"] = label
            edges.append(edge)
        return {"total": len(keys), "offset": offset, "returned": len(edges), "statements": edges}
