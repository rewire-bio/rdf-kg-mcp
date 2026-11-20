"""A session overlay of new records, checked against the bundle and exported as a batch.

The overlay holds records as the JSON the dataset's store uses, never as RDF, so nothing is lost
in a round trip and the export is exactly what the caller wrote, apart from key order. It only
adds records: a record whose id is already in the release is refused. Release graphs are never
written, drafts are not projected into RDF, and the overlay is gone when the process exits.

Everything that depends on one dataset's record shape comes from the manifest's draft block (or a
file given with --draft-config): the IRI base, the id, kind and label fields, a JSON Schema for a
record, the kind of each class, the link field and its rules, and which fields to sort on export.

Every check is advisory. The dataset's own import (for Rewire, records add in a pull request) is
the gate. A link target that is not in the release is a warning rather than a problem, because
the store may have gained it since the release was cut.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import jsonschema
import pyoxigraph as ox

from .bundle import manifest_schema
from .graph import RDF_TYPE, GraphError, KnowledgeGraph
from .search import squash

# The largest integer every JSON implementation reads back exactly (RFC 7493, I-JSON).
MAX_SAFE_INTEGER = 2**53 - 1
DUPLICATE_SEARCH_HITS = 3
MAX_SAME_NAME = 10
BATCH_FILE = "batch.jsonl"
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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


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


# Overlay ---------------------------------------------------------------------------------


class DraftOverlay:
    def __init__(self, kg: KnowledgeGraph, config: DraftConfig, out_dir: Path | None = None):
        self.kg = kg
        self.config = config
        self.out_dir = out_dir
        self.records: dict[str, dict[str, Any]] = {}
        self._kind_of_class = {c: k for k, c in config.kinds.items()}
        asserted = kg.bundle.graphs.get("asserted")
        self._asserted = ox.NamedNode(asserted) if asserted else None
        self._validator = (
            jsonschema.Draft202012Validator(config.record_schema) if config.record_schema else None
        )

    # Release lookups -------------------------------------------------------------------

    def iri(self, record_id: str) -> str:
        return self.config.iri_base + record_id

    def in_release(self, record_id: str) -> bool:
        node = ox.NamedNode(self.iri(record_id))
        return next(iter(self.kg.store.quads_for_pattern(node, None, None)), None) is not None

    def release_kind(self, record_id: str) -> str | None:
        """The kind of a release record, from its asserted types; None if no single kind fits."""
        node = ox.NamedNode(self.iri(record_id))
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
                problems.append(f"{where}: {error.message}")
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

    def _target_kind(self, record_id: str) -> tuple[str | None, str | None]:
        """(kind, where) for an id: where is "draft", "release" or None if found in neither."""
        draft = self.records.get(record_id)
        if draft is not None:
            kind = draft.get(self.config.type_field)
            return (kind if isinstance(kind, str) else None), "draft"
        if self.in_release(record_id):
            return self.release_kind(record_id), "release"
        return None, None

    def _missing(self, where: str, record_id: str) -> str:
        return (
            f"{where}: {record_id} is not in release {self.kg.bundle.version or ''} or the drafts. "
            "This is fine only if the store gained it after this release."
        ).replace("  ", " ")

    def possible_duplicates(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """The top search hits of the same kind on the record's label, plus every release record
        and draft whose squashed label equals it. The caller rules on each one."""
        cfg = self.config
        label = record.get(cfg.label_field) if cfg.label_field else None
        if not isinstance(label, str) or not squash(label):
            return []
        own = record.get(cfg.id_field)
        kind = record.get(cfg.type_field)
        found: dict[str, dict[str, Any]] = {}
        cls_ = cfg.kinds.get(kind) if isinstance(kind, str) else None
        try:
            hits = self.kg.search(label, types=[cls_] if cls_ else None, limit=DUPLICATE_SEARCH_HITS + 1)
        except GraphError:
            hits = {"results": []}
        rank = 0
        for hit in hits["results"]:
            record_id = self._record_id(hit["iri"])
            if record_id is None or record_id == own:
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
        if len(same_name) > MAX_SAME_NAME:
            found["..."] = {
                "omitted": len(same_name) - MAX_SAME_NAME,
                "match": "same name",
                "note": "More release records share this name; search for them to see all.",
            }
        for record_id, other in sorted(self.records.items()):
            other_label = other.get(cfg.label_field) if cfg.label_field else None
            if record_id != own and isinstance(other_label, str) and squash(other_label) == key:
                found[record_id] = {
                    "id": record_id,
                    "in": "draft",
                    "kind": other.get(cfg.type_field),
                    "label": other_label,
                    "match": "same name",
                }
        return list(found.values())

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
        if self.in_release(record_id):
            raise DraftError(
                f"{record_id} is already in release {self.kg.bundle.version or ''}. The draft tools only "
                "add new records; change an existing record through the store itself."
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
            "possible_duplicates": self.possible_duplicates(record),
            "drafts": len(self.records),
        }

    def remove(self, record_id: str) -> dict[str, Any]:
        removed = self.records.pop(record_id, None) is not None
        linked_from = sorted(rid for rid, r in self.records.items() if record_id in self._targets(r))
        result: dict[str, Any] = {"id": record_id, "removed": removed, "drafts": len(self.records)}
        if removed and linked_from:
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
        """Every draft against the release: all are additions in this version. Checks are run
        again over the whole overlay, so a link to a draft added or removed later is current."""
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
            entry["possible_duplicates"] = self.possible_duplicates(record)
            added.append(entry)
        return {
            "dataset": self.kg.bundle.dataset,
            "release": self.kg.bundle.version,
            "manifest_sha256": self.kg.bundle.manifest_sha256,
            "added": added,
            "changed": [],
            "problems": sum(len(e["problems"]) for e in added),
            "warnings": sum(len(e["warnings"]) for e in added),
        }

    def export(self) -> dict[str, Any]:
        """batch.jsonl (one line per draft, sorted by id) and summary.json (the diff). Refused
        while any draft has problems. Written under --draft-dir only; returned inline too."""
        summary = self.diff()
        if summary["problems"]:
            return {
                "exported": False,
                "reason": "Some drafts have problems. Fix or remove them, then export again.",
                "problems": {e["id"]: e["problems"] for e in summary["added"] if e["problems"]},
            }
        batch = "".join(line(self.records[rid], self.config) + "\n" for rid in sorted(self.records))
        batch_bytes = batch.encode("utf-8")
        summary["batch_sha256"] = hashlib.sha256(batch_bytes).hexdigest()
        summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        written = []
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            for name, data in ((BATCH_FILE, batch_bytes), (SUMMARY_FILE, summary_text.encode("utf-8"))):
                _write_atomic(self.out_dir / name, data)
                written.append(str(self.out_dir / name))
        return {
            "exported": True,
            "records": len(self.records),
            "written": written,
            "batch_sha256": summary["batch_sha256"],
            "batch": batch,
            "summary": summary,
        }


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


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
