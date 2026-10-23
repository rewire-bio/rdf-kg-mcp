"""Read-only SPARQL with a row cap and a timeout.

pyoxigraph cannot cancel a running query, so queries run in a worker process with its own
copy of the store. A query that runs past the timeout kills the worker; the next query
starts a fresh one.
"""

from __future__ import annotations

import multiprocessing
import re
import threading
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pyoxigraph as ox

from .bundle import Bundle, NamedQuery, QueryParam, open_bundle
from .graph import GraphError, KnowledgeGraph, load_store

_STRIP = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'  # long double-quoted string
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"  # long single-quoted string
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|<[^<>\"{}|^`\\\s]*>"  # IRI
    r"|#[^\n]*",  # comment
    re.S,
)
_PLACEHOLDER = re.compile(_STRIP.pattern + r"|\$([A-Za-z_][A-Za-z0-9_]*)", re.S)
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_SERVICE = re.compile(r"\bSERVICE\b", re.I)
_FORM = re.compile(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", re.I)
_UPDATE = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b", re.I)


class QueryError(ValueError):
    """The query was refused, did not parse, or failed."""


class QueryTimeout(QueryError):
    pass


def check_query(query: str) -> None:
    """Refuse updates and federated SERVICE calls (which would let the server make outbound requests)."""
    code = _STRIP.sub(" ", query)
    if _SERVICE.search(code):
        raise QueryError("SERVICE (federated query) is not allowed: this server only queries its own bundle.")
    if not _FORM.search(code):
        if _UPDATE.search(code):
            raise QueryError("This server is read-only: SPARQL Update is not allowed.")
        raise QueryError("Only SELECT, ASK, CONSTRUCT and DESCRIBE queries are allowed.")


def placeholders(sparql: str) -> set[str]:
    """Names written as $name in the query, outside strings, IRIs and comments."""
    return {m.group(1) for m in _PLACEHOLDER.finditer(sparql) if m.group(1)}


def _term(kg: KnowledgeGraph, param: QueryParam, value: Any) -> str:
    """Validate one parameter value and return it as a SPARQL term."""
    if param.type == "iri":
        if not isinstance(value, str):
            raise QueryError(f"Parameter {param.name} must be an IRI or prefixed name.")
        try:
            return str(ox.NamedNode(kg.expand(value)))
        except ValueError as error:
            raise QueryError(f"Parameter {param.name}: {error}") from error
    if param.type == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not _INTEGER.match(str(value)):
            raise QueryError(f"Parameter {param.name} must be an integer.")
        return str(int(value))
    if not isinstance(value, str):
        raise QueryError(f"Parameter {param.name} must be a string.")
    return str(ox.Literal(value))


def bind_params(kg: KnowledgeGraph, query: NamedQuery, params: dict[str, Any] | None) -> str:
    """Put validated parameter values into a named query. IRIs become <...>, strings become escaped
    literals and integers bare digits. An optional parameter with no value and no default stays an
    unbound variable."""
    given = dict(params or {})
    declared = {p.name: p for p in query.params}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        known = ", ".join(declared) or "none"
        raise QueryError(f"Unknown parameter(s) for {query.name}: {', '.join(unknown)}. Declared: {known}")
    terms: dict[str, str] = {}
    for param in query.params:
        value = given.get(param.name, param.default)
        if value is None:
            if param.required:
                raise QueryError(f"Parameter {param.name} is required for {query.name}.")
            continue
        terms[param.name] = _term(kg, param, value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return terms.get(name, match.group(0)) if name else match.group(0)

    bound = _PLACEHOLDER.sub(replace, query.sparql)
    check_query(bound)
    return bound


def run_query(
    kg: KnowledgeGraph, query: str, max_rows: int, graphs: list[str] | None = None
) -> dict[str, Any]:
    """Execute a checked query against the union of graphs (or only the given roles or IRIs)."""
    check_query(query)
    options: dict[str, Any] = {"prefixes": kg.prefixes}
    if graphs:
        iris = []
        for name in graphs:
            iri = kg.bundle.graphs.get(name)
            if iri is None and ":" not in name:
                roles = ", ".join(kg.bundle.graphs) or "none"
                raise GraphError(f"Unknown graph role {name!r}. Roles in this bundle: {roles}")
            iri = iri or kg.expand(name)
            iris.append(ox.NamedNode(iri))
        options["default_graph"] = iris
    else:
        options["use_default_graph_as_union"] = True
    try:
        result = kg.store.query(query, **options)
    except SyntaxError as error:
        raise QueryError(f"SPARQL syntax error: {error}") from error
    except (OSError, ValueError, RuntimeError) as error:
        raise QueryError(f"Query failed: {error}") from error
    try:
        if isinstance(result, ox.QueryBoolean):
            return {"type": "boolean", "boolean": bool(result)}
        if isinstance(result, ox.QuerySolutions):
            variables = [v.value for v in result.variables]
            rows: list[dict[str, str]] = []
            truncated = False
            for solution in result:
                if len(rows) == max_rows:
                    truncated = True
                    break
                row = {}
                for name in variables:
                    value = solution[name]
                    if value is not None:
                        row[name] = kg.term(value)
                rows.append(row)
            return {"type": "select", "variables": variables, "rows": rows, "truncated": truncated}
        triples: list[list[str]] = []
        truncated = False
        for triple in result:
            if len(triples) == max_rows:
                truncated = True
                break
            triples.append([kg.term(triple.subject), kg.term(triple.predicate), kg.term(triple.object)])
        return {"type": "graph", "triples": triples, "truncated": truncated}
    except (OSError, ValueError, RuntimeError) as error:
        raise QueryError(f"Query failed: {error}") from error


def _worker(conn: Connection, root: str, store_dir: str | None) -> None:
    bundle = open_bundle(root)
    kg = KnowledgeGraph(bundle, load_store(bundle, Path(store_dir) if store_dir else None))
    conn.send(("ready", None))
    while True:
        try:
            message = conn.recv()
        except EOFError:
            return
        query, max_rows, graphs = message
        try:
            conn.send(("ok", run_query(kg, query, max_rows, graphs)))
        except (QueryError, GraphError) as error:
            conn.send(("error", str(error)))
        except Exception as error:
            conn.send(("error", f"{type(error).__name__}: {error}"))


class QueryRunner:
    """Runs queries in a separate process so a slow query can be stopped."""

    def __init__(self, bundle: Bundle, store_dir: Path | None, timeout: float, start_timeout: float = 600):
        self.root = str(bundle.root)
        self.store_dir = str(store_dir) if store_dir else None
        self.timeout = timeout
        self.start_timeout = start_timeout
        self._lock = threading.Lock()
        self._process: BaseProcess | None = None
        self._conn: Connection | None = None

    def _start(self) -> Connection:
        if self._conn is not None and self._process is not None and self._process.is_alive():
            return self._conn
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_worker, args=(child, self.root, self.store_dir), daemon=True)
        process.start()
        child.close()
        if not parent.poll(self.start_timeout):
            process.kill()
            raise QueryError("The query worker did not start in time.")
        try:
            parent.recv()
        except EOFError as error:
            raise QueryError("The query worker failed to start.") from error
        self._process, self._conn = process, parent
        return parent

    def _stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.join(5)
        if self._conn is not None:
            self._conn.close()
        self._process = self._conn = None

    def run(self, query: str, max_rows: int, graphs: list[str] | None = None) -> dict[str, Any]:
        check_query(query)  # refuse early without touching the worker
        with self._lock:
            conn = self._start()
            conn.send((query, max_rows, graphs))
            if not conn.poll(self.timeout):
                self._stop()
                raise QueryTimeout(
                    f"Query exceeded the {self.timeout:g}s timeout and was stopped. "
                    "Add LIMIT, narrow the patterns, or avoid unbounded property paths."
                )
            try:
                status, payload = conn.recv()
            except EOFError as error:
                self._stop()
                raise QueryError("The query worker stopped unexpectedly.") from error
        if status != "ok":
            raise QueryError(payload)
        return payload

    def close(self) -> None:
        with self._lock:
            self._stop()
