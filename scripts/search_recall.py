"""Measure how often search puts the expected entity in its top k, on a real bundle.

    uv run python scripts/search_recall.py BUNDLE --gold GOLD [--k 3 5] [--json report.json]

GOLD is either a SPARQL SELECT file run against the bundle, or a JSON Lines file. Each row (or
line) gives:

    bucket    a name for the kind of pair, reported separately
    query     the text passed to search
    expected  the IRI search should return
    type      optional: the class passed as search's types filter
    self      optional: an IRI dropped from the results before ranking (the query's own record,
              when it is not the answer)

The script also reports load time, search-index build time and peak memory, so the cost of
serving the bundle is measured on the same run. Nothing here is specific to one dataset: the
gold pairs come from the caller.

--search-config overrides the manifest's search block (as JSON) for this run only, so the
effect of declaring text predicates can be measured before a bundle is rebuilt.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyoxigraph as ox

from rdf_kg_mcp.bundle import open_bundle
from rdf_kg_mcp.graph import GraphError, KnowledgeGraph, load_store

FIELDS = ("bucket", "query", "expected", "type", "self")


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return peak / (1024 * 1024) if platform.system() == "Darwin" else peak / 1024


def gold_pairs(kg: KnowledgeGraph, path: Path) -> list[dict[str, str]]:
    if path.suffix == ".rq":
        result = kg.store.query(path.read_text("utf-8"), use_default_graph_as_union=True)
        assert isinstance(result, ox.QuerySolutions)
        rows = []
        for solution in result:
            row = {}
            for name in FIELDS:
                value = solution[name]
                if value is not None:
                    row[name] = value.value
            rows.append(row)
    else:
        rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    pairs = []
    seen = set()
    for row in rows:
        if not all(row.get(k) for k in ("bucket", "query", "expected")):
            continue
        key = tuple(row.get(k) for k in FIELDS)
        if key not in seen:
            seen.add(key)
            pairs.append(row)
    return pairs


def rank_of(kg: KnowledgeGraph, pair: dict[str, str], depth: int) -> int | None:
    types = [pair["type"]] if pair.get("type") else None
    try:
        found = kg.search(pair["query"], types=types, limit=depth + 1)
    except GraphError:
        return None
    iris = [r["iri"] for r in found["results"] if r["iri"] != pair.get("self")]
    return iris.index(pair["expected"]) + 1 if pair["expected"] in iris[:depth] else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle")
    p.add_argument("--gold", required=True, help="SPARQL SELECT (.rq) or JSON Lines file of gold pairs")
    p.add_argument("--k", type=int, nargs="+", default=[3, 5])
    p.add_argument("--cache-dir", help="Where a downloaded bundle is unpacked")
    p.add_argument("--search-config", help="JSON for the manifest's search block, for this run only")
    p.add_argument("--misses", type=int, default=15, help="Misses listed per bucket")
    p.add_argument("--json", help="Also write the report here")
    args = p.parse_args(argv)

    started = time.monotonic()
    bundle = open_bundle(args.bundle, Path(args.cache_dir) if args.cache_dir else None)
    if args.search_config:
        bundle.manifest["search"] = json.loads(args.search_config)
    kg = KnowledgeGraph(bundle, load_store(bundle))
    loaded = time.monotonic()
    kg.search_index  # noqa: B018
    indexed = time.monotonic()
    pairs = gold_pairs(kg, Path(args.gold))
    depth = max(args.k)

    hits: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    misses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        bucket = pair["bucket"]
        totals[bucket] += 1
        rank = rank_of(kg, pair, depth)
        for k in args.k:
            if rank is not None and rank <= k:
                hits[bucket][k] += 1
        missed = rank is None or rank > min(args.k)
        if missed and len(misses[bucket]) < args.misses:
            misses[bucket].append(
                {"query": pair["query"], "expected": kg.compact(pair["expected"]), "rank": rank}
            )
    searched = time.monotonic()

    all_pairs = sum(totals.values())
    report = {
        "dataset": bundle.dataset,
        "version": bundle.version,
        "statements": len(kg.store),
        "indexed_entities": len(kg.search_index),
        "load_seconds": round(loaded - started, 1),
        "index_seconds": round(indexed - loaded, 1),
        "search_seconds": round(searched - indexed, 1),
        "peak_rss_mb": round(peak_rss_mb()),
        "pairs": all_pairs,
        "recall": {
            f"at_{k}": round(sum(h[k] for h in hits.values()) / all_pairs, 3) if all_pairs else None
            for k in args.k
        },
        "buckets": {
            bucket: {
                "pairs": totals[bucket],
                **{f"at_{k}": round(hits[bucket][k] / totals[bucket], 3) for k in args.k},
                "misses": misses[bucket],
            }
            for bucket in sorted(totals)
        },
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
