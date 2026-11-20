"""A session overlay of new records, checked against the bundle and exported as a batch.

The overlay holds records as the JSON the dataset's store uses, never as RDF, so nothing is lost
in a round trip. The export is the content the caller wrote with two differences: key order (see
normalise) and number form. Numbers are written as JavaScript's JSON.stringify writes them
(0.00002, not Python's 2e-05; 1, not 1.0), so a line is byte for byte what a JavaScript store
writes for the same record, and a digest of the line is the digest that store computes. It only
adds records: a record whose id is already in the release is refused. Release graphs are never
written, drafts are not projected into RDF, and the overlay is gone when the process exits.

Everything that depends on one dataset's record shape comes from the manifest's draft block (or a
file given with --draft-config): the IRI base, the id, kind and label fields, a JSON Schema for a
record, the kind of each class, the link field and its rules, and which fields to sort on export.

Every check is advisory. The dataset's own import (run on the exported batch, in review) is
the gate. A link target that is not in the release is a warning rather than a problem, because
the store may have gained it since the release was cut. A target that could not be any record's
id (it fails the id field's schema, or is not usable in an IRI) is a problem.

Changes to existing records (draft_change) need the store's committed lines, which the KG does not
hold: the caller passes them with --draft-records. A change names the record, the base_sha256 of
the line it was based on, and field changes as JSON Pointer paths. It is applied to that line as
parsed, so every key it does not touch keeps its place: a changed value stays where it was, a new
key goes at the end of its object, and nothing is sorted. The exported changes file carries the
whole changed record with the base and new digests, for the store's own change command, which
checks the base digest again before it writes.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

import jsonschema
import pyoxigraph as ox

from .bundle import manifest_schema
from .graph import RDF_TYPE, GraphError, KnowledgeGraph
from .search import squash, words

# The largest integer every JSON implementation reads back exactly (RFC 7493, I-JSON).
MAX_SAFE_INTEGER = 2**53 - 1
DUPLICATE_SEARCH_HITS = 3
# Search hits looked at for possible duplicates, before the word-coverage filter.
DUPLICATE_SEARCH_POOL = 20
# A search hit or another draft is a possible duplicate only if at least this share of the words
# of one name appear in the other (whole, or as the start of a longer word).
MIN_NAME_COVERAGE = 0.5
MAX_PROBLEM_TEXT = 300
MAX_SAME_NAME = 10
BATCH_FILE = "batch.jsonl"
CHANGES_FILE = "changes.jsonl"
SUMMARY_FILE = "summary.json"


class DraftError(ValueError):
    """A draft call the overlay cannot accept at all (no id, an id already released)."""


@dataclass(frozen=True)
class LinkSpec:
    field: str
    relation: str
    target: str


@dataclass(frozen=True)
class DraftConfig:
    iri_base: str
    id_field: str
    type_field: str
    label_field: str | None
    record_schema: dict[str, Any] | None
    kinds: dict[str, str]  # kind to class IRI, expanded
    draft_kinds: tuple[str, ...]
    links: LinkSpec | None
    link_rules: dict[str, tuple[frozenset[str], frozenset[str]]] | None
    references: tuple[tuple[str, frozenset[str]], ...]
    sort_within: tuple[str, ...]
    sort_arrays: tuple[str, ...]

    @classmethod
    def load(cls, block: Any, kg: KnowledgeGraph) -> DraftConfig:
        """Check a draft block against the manifest schema and resolve its class names."""
        schema = manifest_schema()["properties"]["draft"]
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(block), key=str)
        if errors:
            details = "; ".join(
                f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors
            )
            raise DraftError(f"Invalid draft configuration: {details}")
        kinds = {kind: kg.expand(cls_) for kind, cls_ in block["kinds"].items()}
        shared = {c for c in kinds.values() if list(kinds.values()).count(c) > 1}
        if shared:
            raise DraftError(
                f"Invalid draft configuration: classes used by more than one kind: {sorted(shared)}"
            )
        unknown = [k for k in block["draft_kinds"] if k not in kinds]
        rules = block.get("link_rules")
        for relation, rule in (rules or {}).items():
            unknown += [f"{relation}: {k}" for k in [*rule["from"], *rule["to"]] if k not in kinds]
        for ref in block.get("references") or []:
            unknown += [f"{ref['field']}: {k}" for k in ref["kinds"] if k not in kinds]
        if unknown:
            raise DraftError(f"Invalid draft configuration: kinds not listed in kinds: {unknown}")
        record_schema = block.get("record_schema")
        if record_schema is not None:
            try:
                jsonschema.Draft202012Validator.check_schema(record_schema)
            except jsonschema.SchemaError as error:
                raise DraftError(f"Invalid draft configuration: record_schema: {error.message}") from error
        links = block.get("links")
        return cls(
            iri_base=block["iri_base"],
            id_field=block["id_field"],
            type_field=block["type_field"],
            label_field=block.get("label_field"),
            record_schema=record_schema,
            kinds=kinds,
            draft_kinds=tuple(block["draft_kinds"]),
            links=LinkSpec(links["field"], links["relation"], links["target"]) if links else None,
            link_rules=(
                {r: (frozenset(v["from"]), frozenset(v["to"])) for r, v in rules.items()}
                if rules is not None
                else None
            ),
            references=tuple((r["field"], frozenset(r["kinds"])) for r in block.get("references") or []),
            sort_within=tuple(block.get("sort_within") or []),
            sort_arrays=tuple(block.get("sort_arrays") or []),
        )

    @cached_property
    def field_order(self) -> list[str]:
        properties = (self.record_schema or {}).get("properties") or {}
        return list(properties)


# Serialisation ---------------------------------------------------------------------------


def _utf16(key: str) -> bytes:
    """Sort key matching JavaScript's default string order (UTF-16 code units)."""
    return key.encode("utf-16-be", "surrogatepass")


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys(value[k]) for k in sorted(value, key=_utf16)}
    if isinstance(value, list):
        return [_sort_keys(v) for v in value]
    return value


def js_number(value: float) -> str:
    """A float as ECMAScript Number::toString writes it, which is what JSON.stringify writes.

    Python and JavaScript print the same shortest round-trip digits but switch to exponent form
    at different points: JavaScript writes plain decimals from 1e-6 up to 1e21 (0.00001, where
    Python writes 1e-05) and drops a zero fraction (1, where Python writes 1.0). Adapted from
    rewire_evidence.canonical (rewire-bio/rewire-evidence), where it reproduces every digest in a
    JavaScript store.
    """
    if not math.isfinite(value):
        raise ValueError(f"{value!r} is not a finite number")
    if value == 0:
        return "0"  # JavaScript prints -0 as 0.
    negative = value < 0
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    significant = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    count = len(significant)
    point = exponent + len(digit_tuple)
    if count <= point <= 21:
        out = significant + "0" * (point - count)
    elif 0 < point <= 21:
        out = significant[:point] + "." + significant[point:]
    elif -6 < point <= 0:
        out = "0." + "0" * (-point) + significant
    else:
        power = point - 1
        mantissa = significant if count == 1 else significant[0] + "." + significant[1:]
        out = f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"
    return "-" + out if negative else out


def _text(value: str) -> str:
    """A JSON string. A lone UTF-16 surrogate has no UTF-8 form, so the line could not be written
    or hashed; it is refused here, before anything is stored."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        bad = value[error.start : error.end].encode("unicode_escape").decode("ascii")
        raise ValueError(
            f"the string {value[:40]!r} has a lone surrogate ({bad}), which UTF-8 cannot hold"
        ) from None
    return json.dumps(value, ensure_ascii=False)


def _json(value: Any) -> str:
    """JSON.stringify(value): keys in the order given, no spaces, JavaScript number form."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return js_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(item) for item in value) + "]"
    if isinstance(value, dict):
        members = (f"{_text(str(k))}:{_json(v)}" for k, v in value.items())
        return "{" + ",".join(members) + "}"
    raise ValueError(f"{type(value).__name__} is not a JSON value")


def normalise(record: dict[str, Any], config: DraftConfig) -> dict[str, Any]:
    """The record as it is exported: top-level fields in schema order (then the rest in the
    caller's order), keys sorted at every depth inside sort_within fields, sort_arrays sorted."""
    order = [k for k in config.field_order if k in record] + [
        k for k in record if k not in config.field_order
    ]
    out: dict[str, Any] = {}
    for key in order:
        value = record[key]
        if key in config.sort_within:
            value = _sort_keys(value)
        if key in config.sort_arrays and isinstance(value, list):
            value = sorted(value, key=lambda item: _array_key(item, key, config))
        out[key] = value
    return out


def _array_key(item: Any, field: str, config: DraftConfig) -> tuple[str, ...]:
    if config.links and field == config.links.field and isinstance(item, dict):
        relation, target = item.get(config.links.relation), item.get(config.links.target)
        return (str(relation), str(target), _json(_sort_keys(item)))
    return ("", "", _json(_sort_keys(item)))


def line(record: dict[str, Any], config: DraftConfig) -> str:
    """One export line. Equal content in any key order gives the same bytes."""
    return _json(normalise(record, config))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaseRecord:
    """A committed store line, kept as bytes: a change is applied to it and diffed against it."""

    line: str
    kind: str | None
    source: str

    @cached_property
    def sha256(self) -> str:
        return sha256_text(self.line)


def load_records(paths: list[Path], config: DraftConfig) -> dict[str, BaseRecord]:
    """The committed lines of the store's records, by id, from JSON Lines files (.jsonl or
    .jsonl.gz) or directories of them (not recursive). An id found twice is an error."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz")))
        elif path.is_file():
            files.append(path)
        else:
            raise DraftError(f"--draft-records: {path} does not exist")
    out: dict[str, BaseRecord] = {}
    for file in files:
        opener = gzip.open if file.name.endswith(".gz") else open
        with opener(file, "rt", encoding="utf-8", newline="") as handle:
            for number, raw in enumerate(handle, 1):
                text = raw.rstrip("\n")
                if not text.strip():
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as error:
                    raise DraftError(f"--draft-records: {file}:{number} is not JSON: {error}") from error
                record_id = value.get(config.id_field) if isinstance(value, dict) else None
                if not isinstance(record_id, str) or not record_id:
                    raise DraftError(f"--draft-records: {file}:{number} has no string {config.id_field!r}")
                if record_id in out:
                    raise DraftError(f"--draft-records: {record_id} is in {out[record_id].source} and {file}")
                kind = value.get(config.type_field)
                out[record_id] = BaseRecord(text, kind if isinstance(kind, str) else None, str(file))
    return out


def _pointer(path: Any) -> list[str]:
    """A JSON Pointer (RFC 6901) as its reference tokens."""
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise DraftError(f"change path {path!r} must be a JSON Pointer such as /attributes/licence")
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]
    if "" in tokens:
        raise DraftError(f"change path {path!r} has an empty key; a record field is never named ''")
    return tokens


def _path_text(tokens: list[str]) -> str:
    return "/" + "/".join(t.replace("~", "~0").replace("/", "~1") for t in tokens)


def apply_changes(record: dict[str, Any], changes: Any, protected: tuple[str, ...]) -> dict[str, Any]:
    """The record with the changes applied, in order. Each change is {"path", "value"} (set) or
    {"path", "remove": true}. Setting an existing key replaces its value where it stands; a new
    key is appended to its object, and missing parent objects are created the same way. Paths go
    through objects only: a list is replaced whole. Keys in `protected` cannot be changed.
    The caller puts keys back in the committed order with keep_order."""
    if not isinstance(changes, list) or not changes:
        raise DraftError("changes must be a non-empty list of {path, value} or {path, remove: true}")
    out = copy.deepcopy(record)
    for i, change in enumerate(changes):
        if not isinstance(change, dict) or set(change) - {"path", "value", "remove"}:
            raise DraftError(f"changes[{i}] must be {{path, value}} or {{path, remove: true}}")
        tokens = _pointer(change.get("path"))
        remove = change.get("remove") is True
        if remove == ("value" in change):
            raise DraftError(f"changes[{i}] ({change.get('path')}) needs either value or remove: true")
        if tokens[0] in protected:
            raise DraftError(f"changes[{i}]: {tokens[0]!r} cannot be changed; it names the record")
        parent: Any = out
        for depth, token in enumerate(tokens[:-1]):
            if not isinstance(parent, dict):
                raise DraftError(
                    f"changes[{i}]: {_path_text(tokens[:depth])} is not an object; replace it whole"
                )
            if token not in parent:
                if remove:
                    raise DraftError(f"changes[{i}]: {_path_text(tokens[: depth + 1])} does not exist")
                parent[token] = {}
            parent = parent[token]
        if not isinstance(parent, dict):
            raise DraftError(f"changes[{i}]: {_path_text(tokens[:-1])} is not an object; replace it whole")
        if remove:
            if tokens[-1] not in parent:
                raise DraftError(f"changes[{i}]: {change['path']} does not exist")
            del parent[tokens[-1]]
        else:
            parent[tokens[-1]] = copy.deepcopy(change["value"])
    return out


def keep_order(base: Any, changed: Any) -> Any:
    """`changed` with the keys of every object that `base` also has in `base`'s order, and keys
    `base` lacks after them in the order they were first set. A key removed and set again therefore
    goes back to its place: the order of the changes never moves a key the base has, but it does
    order the keys the change adds (/a/n1 then /a/n2 differs from /a/n2 then /a/n1)."""
    if not (isinstance(base, dict) and isinstance(changed, dict)):
        return changed
    order = [k for k in base if k in changed] + [k for k in changed if k not in base]
    return {k: keep_order(base.get(k), changed[k]) for k in order}


def field_diff(before: Any, after: Any, path: list[str] | None = None) -> list[dict[str, Any]]:
    """Leaf-level differences as {path, change, before, after}: objects are compared key by key,
    anything else (lists included) as a whole, by its exported bytes. Key order alone is no change."""
    path = path or []
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[dict[str, Any]] = []
        for key in [*before, *(k for k in after if k not in before)]:
            if key not in after:
                out.append({"path": _path_text([*path, key]), "change": "removed", "before": before[key]})
            elif key not in before:
                out.append({"path": _path_text([*path, key]), "change": "added", "after": after[key]})
            else:
                out += field_diff(before[key], after[key], [*path, key])
        return out
    if _json(before) == _json(after):
        return []
    return [{"path": _path_text(path) or "/", "change": "changed", "before": before, "after": after}]


# Overlay ---------------------------------------------------------------------------------


class DraftOverlay:
    def __init__(
        self,
        kg: KnowledgeGraph,
        config: DraftConfig,
        out_dir: Path | None = None,
        base: dict[str, BaseRecord] | None = None,
    ):
        self.kg = kg
        self.config = config
        self.out_dir = out_dir
        self.records: dict[str, dict[str, Any]] = {}
        # Committed store lines (--draft-records), and changes to them by id.
        self.base: dict[str, BaseRecord] = base or {}
        self.changes: dict[str, dict[str, Any]] = {}
        self._kind_of_class = {c: k for k, c in config.kinds.items()}
        asserted = kg.bundle.graphs.get("asserted")
        self._asserted = ox.NamedNode(asserted) if asserted else None
        self._validator = (
            jsonschema.Draft202012Validator(config.record_schema) if config.record_schema else None
        )
        id_schema = ((config.record_schema or {}).get("properties") or {}).get(config.id_field)
        # The id field's own schema checks link targets and references: a target that could not
        # be any record's id is a mistake, not a record the release lacks.
        self._id_validator = (
            self._validator.evolve(schema=id_schema)
            if self._validator is not None and isinstance(id_schema, dict)
            else None
        )

    # Release lookups -------------------------------------------------------------------

    def iri(self, record_id: str) -> str:
        return self.config.iri_base + record_id

    def _node(self, record_id: str) -> ox.NamedNode | None:
        """The record's IRI as a node, or None if the id cannot be part of an IRI."""
        try:
            return ox.NamedNode(self.iri(record_id))
        except ValueError:
            return None

    def in_release(self, record_id: str) -> bool:
        node = self._node(record_id)
        if node is None:
            return False
        return next(iter(self.kg.store.quads_for_pattern(node, None, None)), None) is not None

    def release_kind(self, record_id: str) -> str | None:
        """The kind of a release record, from its asserted types; None if no single kind fits."""
        node = self._node(record_id)
        if node is None:
            return None
        quads = self.kg.store.quads_for_pattern(node, ox.NamedNode(RDF_TYPE), None, self._asserted)
        classes = {q.object.value for q in quads if isinstance(q.object, ox.NamedNode)}
        found = {self._kind_of_class[c] for c in classes if c in self._kind_of_class}
        return found.pop() if len(found) == 1 else None

    @cached_property
    def _release_names(self) -> dict[str, set[str]]:
        """Squashed label to the release record ids carrying it."""
        names: dict[str, set[str]] = defaultdict(set)
        base = self.config.iri_base
        for iri, labels in self.kg.labels.items():
            if iri.startswith(base) and "/" not in iri[len(base) :]:
                for label in labels:
                    if key := squash(label):
                        names[key].add(iri[len(base) :])
        return names

    # Checks ------------------------------------------------------------------------------

    def check(self, record: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Problems (the import will refuse the record) and warnings (it may not)."""
        cfg = self.config
        problems: list[str] = []
        warnings: list[str] = []
        if self._validator is not None:
            for error in sorted(self._validator.iter_errors(record), key=lambda e: list(e.absolute_path)):
                where = "/".join(map(str, error.absolute_path)) or "(record)"
                problems.append(f"{where}: {_short(error.message)}")
        kind = record.get(cfg.type_field)
        if not isinstance(kind, str) or kind not in cfg.kinds:
            problems.append(f"{cfg.type_field}: {kind!r} is not a kind of this dataset")
            kind = None
        elif kind not in cfg.draft_kinds:
            problems.append(
                f"{cfg.type_field}: {kind} records cannot be drafted; draftable kinds: "
                + ", ".join(cfg.draft_kinds)
            )
        problems += _number_problems(record)
        if cfg.links:
            p, w = self._check_links(record, kind)
            problems += p
            warnings += w
        for field, allowed in cfg.references:
            values = record.get(field)
            if not isinstance(values, list):
                continue
            for i, ref in enumerate(values):
                if not isinstance(ref, str):
                    continue
                if values.index(ref) != i:
                    problems.append(f"{field}[{i}]: {ref} is listed twice")
                    continue
                if (bad := self.id_problem(ref)) is not None:
                    problems.append(f"{field}[{i}]: {bad}")
                    continue
                ref_kind, where = self._target_kind(ref)
                if where is None:
                    warnings.append(self._missing(f"{field}[{i}]", ref))
                elif ref_kind is None:
                    warnings.append(f"{field}[{i}]: the kind of release record {ref} could not be determined")
                elif ref_kind not in allowed:
                    problems.append(
                        f"{field}[{i}]: {ref} is a {ref_kind}; this field takes {', '.join(sorted(allowed))}"
                    )
        return problems, warnings

    def _check_links(self, record: dict[str, Any], kind: str | None) -> tuple[list[str], list[str]]:
        cfg = self.config
        assert cfg.links is not None
        spec = cfg.links
        problems: list[str] = []
        warnings: list[str] = []
        links = record.get(spec.field)
        if not isinstance(links, list):
            return problems, warnings
        seen: set[tuple[str, str]] = set()
        for i, link in enumerate(links):
            where = f"{spec.field}[{i}]"
            if not isinstance(link, dict):
                continue
            relation, target = link.get(spec.relation), link.get(spec.target)
            if not isinstance(relation, str) or not isinstance(target, str):
                continue
            if (relation, target) in seen:
                problems.append(f"{where}: {relation} to {target} is listed twice")
                continue
            seen.add((relation, target))
            if target == record.get(cfg.id_field):
                problems.append(f"{where}: a record cannot link to itself")
                continue
            if (bad := self.id_problem(target)) is not None:
                problems.append(f"{where}: {bad}")
                continue
            rule = cfg.link_rules.get(relation) if cfg.link_rules is not None else None
            if cfg.link_rules is not None and rule is None:
                problems.append(f"{where}: unknown relation {relation!r}")
                continue
            target_kind, found = self._target_kind(target)
            if found is None:
                warnings.append(self._missing(where, target))
                continue
            if rule is None:
                continue
            allowed_from, allowed_to = rule
            if kind is not None and kind not in allowed_from:
                problems.append(
                    f"{where}: {relation} cannot start from a {kind}; it starts from "
                    + ", ".join(sorted(allowed_from))
                )
            if target_kind is None:
                warnings.append(f"{where}: the kind of release record {target} could not be determined")
            elif target_kind not in allowed_to:
                problems.append(
                    f"{where}: {relation} cannot point to {target}, a {target_kind}; it points to "
                    + ", ".join(sorted(allowed_to))
                )
        return problems, warnings

    def id_problem(self, record_id: str) -> str | None:
        """Why record_id cannot be any record's id, or None if it can."""
        if self._node(record_id) is None:
            return f"{record_id!r} cannot be part of an IRI"
        if self._id_validator is not None:
            error = next(iter(self._id_validator.iter_errors(record_id)), None)
            if error is not None:
                return f"{record_id!r} is not a valid {self.config.id_field}: {_short(error.message)}"
        return None

    def _target_kind(self, record_id: str) -> tuple[str | None, str | None]:
        """(kind, where) for an id: where is "draft", "store" (the --draft-records lines),
        "release" or None if found in none."""
        draft = self.records.get(record_id)
        if draft is not None:
            kind = draft.get(self.config.type_field)
            return (kind if isinstance(kind, str) else None), "draft"
        if record_id in self.base:
            return self.base[record_id].kind, "store"
        if self.in_release(record_id):
            return self.release_kind(record_id), "release"
        return None, None

    def _missing(self, where: str, record_id: str) -> str:
        return (
            f"{where}: {record_id} is not in release {self.kg.bundle.version or ''} or the drafts. "
            "This is fine only if the store gained it after this release."
        ).replace("  ", " ")

    def possible_duplicates(self, record: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """Records that may be the same as this one, for the caller to rule on:
        - up to DUPLICATE_SEARCH_HITS search hits of the same kind on the label whose label or alias
          shares at least MIN_NAME_COVERAGE of its words with it (match "search");
        - release records (up to MAX_SAME_NAME, same kind first) whose squashed label equals it
          (match "same name"), and how many more were left out;
        - drafts with the same squashed label ("same name") or that share enough words ("near name").
        """
        cfg = self.config
        label = record.get(cfg.label_field) if cfg.label_field else None
        if not isinstance(label, str) or not squash(label):
            return [], 0
        own = record.get(cfg.id_field)
        kind = record.get(cfg.type_field)
        found: dict[str, dict[str, Any]] = {}
        cls_ = cfg.kinds.get(kind) if isinstance(kind, str) else None
        try:
            hits = self.kg.search(label, types=[cls_] if cls_ else None, limit=DUPLICATE_SEARCH_POOL)
        except GraphError:
            hits = {"results": []}
        rank = 0
        for hit in hits["results"]:
            record_id = self._record_id(hit["iri"])
            if record_id is None or record_id == own:
                continue
            names = self.kg.labels.get(hit["iri"], [])
            if not {"label", "alias"} & set(hit["matched"]) or not any(
                name_overlap(label, name) >= MIN_NAME_COVERAGE for name in names
            ):
                continue
            rank += 1
            if rank > DUPLICATE_SEARCH_HITS:
                break
            found[record_id] = {
                "id": record_id,
                "in": "release",
                "kind": self.release_kind(record_id),
                "label": hit["label"],
                "match": "search",
                "rank": rank,
                "score": hit["score"],
            }
        key = squash(label)
        same_name = [(self.release_kind(rid), rid) for rid in self._release_names.get(key, ()) if rid != own]
        # Records of the same kind first; a common name can be shared by many records.
        same_name.sort(key=lambda pair: (pair[0] != kind, pair[1]))
        for found_kind, record_id in same_name[:MAX_SAME_NAME]:
            entry = found.setdefault(
                record_id,
                {
                    "id": record_id,
                    "in": "release",
                    "kind": found_kind,
                    "label": self.kg.label(self.iri(record_id)),
                },
            )
            entry["match"] = "same name" if entry.get("match") is None else "same name, search"
        for record_id, other in sorted(self.records.items()):
            other_label = other.get(cfg.label_field) if cfg.label_field else None
            if record_id == own or not isinstance(other_label, str):
                continue
            if squash(other_label) == key:
                match = "same name"
            elif name_overlap(label, other_label) >= MIN_NAME_COVERAGE:
                match = "near name"
            else:
                continue
            found[record_id] = {
                "id": record_id,
                "in": "draft",
                "kind": other.get(cfg.type_field),
                "label": other_label,
                "match": match,
            }
        return list(found.values()), max(0, len(same_name) - MAX_SAME_NAME)

    def _duplicates(self, record: dict[str, Any]) -> dict[str, Any]:
        found, omitted = self.possible_duplicates(record)
        out: dict[str, Any] = {"possible_duplicates": found}
        if omitted:
            out["same_name_omitted"] = omitted
        return out

    def _record_id(self, iri: str) -> str | None:
        base = self.config.iri_base
        if not iri.startswith(base) or "/" in iri[len(base) :]:
            return None
        return iri[len(base) :]

    # Tools -------------------------------------------------------------------------------

    def upsert(self, record: Any) -> dict[str, Any]:
        cfg = self.config
        if not isinstance(record, dict):
            raise DraftError("record must be a JSON object")
        record_id = record.get(cfg.id_field)
        if not isinstance(record_id, str) or not record_id:
            raise DraftError(f"record must have a non-empty string {cfg.id_field!r}")
        if self._node(record_id) is None:
            raise DraftError(f"{cfg.id_field} {record_id!r} cannot be part of an IRI, so it cannot be an id")
        if record_id in self.base:
            raise DraftError(
                f"{record_id} is already in the store records. Change it with draft_change, which "
                "takes the base_sha256 of its line and the fields to change."
            )
        if self.in_release(record_id):
            raise DraftError(
                f"{record_id} is already in release {self.kg.bundle.version or ''}. draft_upsert only "
                "adds new records; change an existing one with draft_change"
                + ("." if self.base else ", which needs the store's lines (serve --draft-records).")
            )
        try:
            new_line = line(record, cfg)
        except ValueError as error:  # NaN or infinity
            raise DraftError(f"record is not valid JSON for export: {error}") from error
        previous = self.records.get(record_id)
        if previous is None:
            status = "added"
        elif line(previous, cfg) == new_line:
            status = "unchanged"
        else:
            status = "replaced"
        self.records[record_id] = copy.deepcopy(record)
        problems, warnings = self.check(record)
        return {
            "id": record_id,
            "iri": self.iri(record_id),
            "status": status,
            "problems": problems,
            "warnings": warnings,
            **self._duplicates(record),
            "drafts": self.count,
        }

    @property
    def count(self) -> int:
        return len(self.records) + len(self.changes)

    def _base_of(self, record_id: str) -> BaseRecord:
        cfg = self.config
        if not self.base:
            raise DraftError(
                "No store records were given, so existing records cannot be changed. Start the server "
                "with --draft-records naming the store's record files."
            )
        base = self.base.get(record_id)
        if base is None:
            raise DraftError(
                f"{record_id} is not in the store records given with --draft-records; "
                "draft_upsert adds a new record."
            )
        if base.kind not in cfg.draft_kinds:
            raise DraftError(
                f"{record_id} is a {base.kind} record; {base.kind} records cannot be drafted. "
                "Draftable kinds: " + ", ".join(cfg.draft_kinds)
            )
        return base

    def change(self, record_id: Any, changes: Any, base_sha256: Any) -> dict[str, Any]:
        """Change fields of an existing record. base_sha256 must be the digest of the record's
        committed line, so a change based on an older line is refused rather than applied over newer
        data. Calls on one record accumulate; a result equal to the base line is dropped."""
        cfg = self.config
        if not isinstance(record_id, str) or not record_id:
            raise DraftError("id must be a non-empty string")
        base = self._base_of(record_id)
        if base_sha256 != base.sha256:
            raise DraftError(
                f"base_sha256 {str(base_sha256)[:16]}... does not match the committed line of {record_id} "
                f"(sha256 {base.sha256}). The record changed since the base was read; read it again and "
                "redo the change against the current line."
            )
        parsed = json.loads(base.line)
        try:
            written = _json(parsed)
        except ValueError as error:
            raise DraftError(f"The committed line of {record_id} cannot be written again: {error}") from error
        if written != base.line:
            raise DraftError(
                f"The committed line of {record_id} is not in the form this server writes (JSON.stringify "
                "of the parsed record), so a change would also rewrite fields it does not touch."
            )
        current = self.changes.get(record_id) or parsed
        changed = keep_order(parsed, apply_changes(current, changes, (cfg.id_field, cfg.type_field)))
        try:
            new_line = _json(changed)
        except ValueError as error:  # NaN or infinity
            raise DraftError(f"the changed record is not valid JSON for export: {error}") from error
        problems, warnings = self._change_checks(parsed, changed)
        if new_line == base.line:
            self.changes.pop(record_id, None)
            status = "unchanged"
        else:
            self.changes[record_id] = changed
            status = "changed"
        return {
            "id": record_id,
            "iri": self.iri(record_id),
            "status": status,
            "base_sha256": base.sha256,
            "sha256": sha256_text(new_line),
            "fields": field_diff(parsed, changed),
            "problems": problems,
            "warnings": warnings,
            "drafts": self.count,
        }

    def _change_checks(self, before: dict[str, Any], after: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Problems and warnings the change brings in. Those the committed line already has are not
        the change's doing; they are listed as warnings so they cannot block it."""
        old_problems, old_warnings = self.check(before)
        problems, warnings = self.check(after)
        carried = [f"already in the committed line: {p}" for p in problems if p in old_problems]
        return [p for p in problems if p not in old_problems], [
            *carried,
            *(w for w in warnings if w not in old_warnings),
        ]

    def _changed_entry(self, record_id: str) -> dict[str, Any]:
        cfg = self.config
        base = self.base[record_id]
        before = json.loads(base.line)
        after = self.changes[record_id]
        problems, warnings = self._change_checks(before, after)
        return {
            "id": record_id,
            "iri": self.iri(record_id),
            "kind": after.get(cfg.type_field),
            "label": after.get(cfg.label_field) if cfg.label_field else None,
            "base_sha256": base.sha256,
            "sha256": sha256_text(_json(after)),
            "fields": field_diff(before, after),
            "problems": problems,
            "warnings": warnings,
        }

    def remove(self, record_id: str) -> dict[str, Any]:
        reverted = self.changes.pop(record_id, None) is not None
        removed = self.records.pop(record_id, None) is not None or reverted
        linked_from = sorted(rid for rid, r in self.records.items() if record_id in self._targets(r))
        result: dict[str, Any] = {"id": record_id, "removed": removed, "drafts": self.count}
        if reverted:
            result["note"] = "The change was dropped; the committed line stands."
        if removed and not reverted and linked_from:
            result["note"] = (
                "These drafts still point to the removed id and now carry a warning: "
                + ", ".join(linked_from)
            )
        return result

    def _targets(self, record: dict[str, Any]) -> set[str]:
        cfg = self.config
        out: set[str] = set()
        if cfg.links and isinstance(links := record.get(cfg.links.field), list):
            out |= {
                t for lk in links if isinstance(lk, dict) and isinstance(t := lk.get(cfg.links.target), str)
            }
        for field, _ in cfg.references:
            if isinstance(values := record.get(field), list):
                out |= {v for v in values if isinstance(v, str)}
        return out

    def diff(self) -> dict[str, Any]:
        """New records (added) and changes to store records (changed, with a field-level diff and
        the base digest). Checks are run again over the whole overlay, so a link to a draft added
        or removed later is current."""
        cfg = self.config
        added = []
        for record_id in sorted(self.records):
            record = self.records[record_id]
            problems, warnings = self.check(record)
            entry: dict[str, Any] = {
                "id": record_id,
                "iri": self.iri(record_id),
                "kind": record.get(cfg.type_field),
                "label": record.get(cfg.label_field) if cfg.label_field else None,
            }
            if cfg.links and isinstance(links := record.get(cfg.links.field), list):
                entry["links"] = normalise({cfg.links.field: links}, cfg)[cfg.links.field]
            for field, _ in cfg.references:
                if field in record:
                    entry[field] = record[field]
            entry["problems"] = problems
            entry["warnings"] = warnings
            entry.update(self._duplicates(record))
            added.append(entry)
        changed = [self._changed_entry(rid) for rid in sorted(self.changes)]
        entries = [*added, *changed]
        return {
            "dataset": self.kg.bundle.dataset,
            "release": self.kg.bundle.version,
            "manifest_sha256": self.kg.bundle.manifest_sha256,
            "added": added,
            "changed": changed,
            "problems": sum(len(e["problems"]) for e in entries),
            "warnings": sum(len(e["warnings"]) for e in entries),
        }

    def _remove_files(self, names: tuple[str, ...]) -> list[str]:
        removed = []
        if self.out_dir is not None:
            for name in names:
                path = self.out_dir / name
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
        return removed

    def export(self) -> dict[str, Any]:
        """batch.jsonl (one line per new record, sorted by id), changes.jsonl (one line per changed
        record, sorted by id; only when there are changes) and summary.json (the diff). Refused
        while any draft has problems. Written under --draft-dir only; returned inline too.

        A changes line is {"id", "base_sha256", "sha256", "record"}: the whole changed record,
        written in the key order of its committed line, the digest of that line, and the digest of
        the record as written, which is what the store's digest becomes."""
        summary = self.diff()
        if summary["problems"]:
            # Files from an earlier export would no longer match the overlay; leave none behind.
            entries = [*summary["added"], *summary["changed"]]
            return {
                "exported": False,
                "reason": "Some drafts have problems. Fix or remove them, then export again.",
                "problems": {e["id"]: e["problems"] for e in entries if e["problems"]},
                "removed": self._remove_files((BATCH_FILE, CHANGES_FILE, SUMMARY_FILE)),
            }
        batch = "".join(line(self.records[rid], self.config) + "\n" for rid in sorted(self.records))
        batch_bytes = batch.encode("utf-8")
        summary["batch_sha256"] = hashlib.sha256(batch_bytes).hexdigest()
        changes = "".join(
            _json(
                {
                    "id": rid,
                    "base_sha256": self.base[rid].sha256,
                    "sha256": sha256_text(_json(self.changes[rid])),
                    "record": self.changes[rid],
                }
            )
            + "\n"
            for rid in sorted(self.changes)
        )
        changes_bytes = changes.encode("utf-8")
        if changes:
            summary["changes_sha256"] = hashlib.sha256(changes_bytes).hexdigest()
        summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        written = []
        removed: list[str] = []
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            files = [(BATCH_FILE, batch_bytes)]
            if changes:
                files.append((CHANGES_FILE, changes_bytes))
            else:
                removed = self._remove_files((CHANGES_FILE,))
            files.append((SUMMARY_FILE, summary_text.encode("utf-8")))
            for name, data in files:
                _write_atomic(self.out_dir / name, data)
                written.append(str(self.out_dir / name))
        out: dict[str, Any] = {
            "exported": True,
            "records": len(self.records),
            "written": written,
            "batch_sha256": summary["batch_sha256"],
            "batch": batch,
        }
        if changes:
            out.update(changed=len(self.changes), changes_sha256=summary["changes_sha256"], changes=changes)
        if removed:
            out["removed"] = removed
        out["summary"] = summary
        return out


def name_overlap(a: str, b: str) -> float:
    """The larger share of either name's words found in the other, whole or as the start of a
    longer word (at least four letters). 1.0 when the squashed names are equal."""
    if squash(a) == squash(b):
        return 1.0
    wa, wb = set(words(a)), set(words(b))
    if not wa or not wb:
        return 0.0

    def found(word: str, others: set[str]) -> bool:
        return word in others or (len(word) >= 4 and any(o.startswith(word) for o in others))

    return max(
        sum(found(w, wb) for w in wa) / len(wa),
        sum(found(w, wa) for w in wb) / len(wb),
    )


def _number_problems(value: Any, path: str = "") -> list[str]:
    """Integers beyond 2^53 and non-finite numbers do not survive a round trip through JSON
    readers that use doubles; lone surrogates cannot be written as UTF-8."""
    where = path or "(record)"
    if isinstance(value, bool):
        return []
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        return [f"{where}: integer {value} is outside the range JSON numbers carry exactly (2^53)"]
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{where}: {value} is not a finite number"]
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return [f"{where}: text holds a lone surrogate"]
        return []
    if isinstance(value, dict):
        problems = []
        for k, v in value.items():
            problems += _number_problems(k, f"{path}/{k}" if path else k)
            problems += _number_problems(v, f"{path}/{k}" if path else k)
        return problems
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _number_problems(v, f"{path}[{i}]")]
    return []


def _short(message: str) -> str:
    """A validator message cut to a readable length (an enum of hundreds of values, say)."""
    return message if len(message) <= MAX_PROBLEM_TEXT else message[: MAX_PROBLEM_TEXT - 4] + " ..."


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


# mkstemp creates files readable by the owner only; exports get the mode a plain open() gives.
FILE_MODE = 0o666 & ~_umask()


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
