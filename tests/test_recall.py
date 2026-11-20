from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from .conftest import EX, ID

SCRIPT = Path(__file__).parent.parent / "scripts" / "search_recall.py"


def load_script():
    spec = importlib.util.spec_from_file_location("search_recall", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recall_from_json_lines(bundle_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gold = tmp_path / "gold.jsonl"
    pairs = [
        {"bucket": "name", "query": "Rex", "expected": ID + "rex", "type": EX + "Dog"},
        {"bucket": "name", "query": "Alice", "expected": ID + "alice"},
        {"bucket": "spelling", "query": "Dog-07", "expected": ID + "dog07", "type": EX + "Dog"},
        {"bucket": "spelling", "query": "Dog 08", "expected": ID + "dog08", "self": ID + "dog08"},
    ]
    gold.write_text("".join(json.dumps(p) + "\n" for p in pairs))
    out = tmp_path / "report.json"
    assert (
        load_script().main([str(bundle_dir), "--gold", str(gold), "--k", "1", "3", "--json", str(out)]) == 0
    )
    report = json.loads(out.read_text())
    assert report["pairs"] == 4
    assert report["buckets"]["name"]["at_1"] == 1.0
    # The last pair drops its own answer, so it misses by construction.
    assert report["buckets"]["spelling"]["at_3"] == 0.5
    assert report["buckets"]["spelling"]["misses"] == [
        {"query": "Dog 08", "expected": "id:dog08", "rank": None}
    ]
    assert report["peak_rss_mb"] > 0 and "load_seconds" in report


def test_recall_from_sparql(bundle_dir: Path, tmp_path: Path) -> None:
    gold = tmp_path / "gold.rq"
    label = "http://www.w3.org/2000/01/rdf-schema#label"
    gold.write_text(
        f"SELECT ?bucket ?query ?expected WHERE {{ ?expected a <{EX}Person> ; <{label}> ?query "
        'BIND("p" AS ?bucket) }'
    )
    out = tmp_path / "report.json"
    assert load_script().main([str(bundle_dir), "--gold", str(gold), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["buckets"]["p"]["at_3"] == 1.0
