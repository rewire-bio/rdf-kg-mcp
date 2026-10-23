from __future__ import annotations

import functools
import http.server
import json
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from rdf_kg_mcp.bundle import BundleError, locate, open_bundle, pack, sha256_file, verify, write_manifest

from .conftest import ONTOLOGY


def test_valid_bundle_verifies_with_counts(bundle_dir: Path) -> None:
    assert verify(bundle_dir, count=True) == []


def test_manifest_lists_hashes_counts_and_graphs(bundle_dir: Path) -> None:
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["format_version"] == "1.0"
    assert manifest["files"]["asserted.nq"]["quads"] == 73
    assert manifest["files"]["ontology/pets.ttl"]["graph"] == ONTOLOGY
    assert "triples" in manifest["files"]["ontology/pets.ttl"]
    assert manifest["files"]["inferred.nq"]["sha256"] == sha256_file(bundle_dir / "inferred.nq")


def test_tampered_file_is_refused(fresh_bundle_dir: Path) -> None:
    with (fresh_bundle_dir / "inferred.nq").open("a") as f:
        f.write('<https://example.org/id/x> <https://example.org/vocab#p> "evil" .\n')
    with pytest.raises(BundleError, match=r"inferred\.nq: sha256"):
        open_bundle(str(fresh_bundle_dir))


def test_missing_file_is_reported(fresh_bundle_dir: Path) -> None:
    (fresh_bundle_dir / "ontology" / "pets.ttl").unlink()
    assert verify(fresh_bundle_dir) == ["ontology/pets.ttl: missing"]


def test_wrong_count_is_reported_only_when_counting(fresh_bundle_dir: Path) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    manifest["files"]["asserted.nq"]["quads"] = 1
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    assert verify(fresh_bundle_dir) == []
    assert verify(fresh_bundle_dir, count=True) == ["asserted.nq: 73 statements, manifest says 1"]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m.pop("dataset"), "'dataset' is a required property"),
        (lambda m: m.update(format_version="2.0"), "format_version"),
        (lambda m: m["files"].update({"../escape.nq": {"sha256": "0" * 64}}), "files"),
        (lambda m: m["files"].update({"/abs.nq": {"sha256": "0" * 64}}), "files"),
        (lambda m: m["files"].update({"notes.txt": {"sha256": "0" * 64}}), "files"),
    ],
)
def test_invalid_manifests_are_rejected(fresh_bundle_dir: Path, change, message: str) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    change(manifest)
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    problems = verify(fresh_bundle_dir)
    assert len(problems) == 1 and problems[0].startswith("Invalid manifest") and message in problems[0]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m["queries"][0]["params"][0].update(type="date"), "queries/0/params/0/type"),
        (lambda m: m["queries"][0].update(name="has space"), "queries/0/name"),
        (lambda m: m["queries"][0].pop("sparql"), "'sparql' is a required property"),
    ],
)
def test_invalid_queries_are_rejected_by_the_schema(fresh_bundle_dir: Path, change, message: str) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    change(manifest)
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    problems = verify(fresh_bundle_dir)
    assert len(problems) == 1 and problems[0].startswith("Invalid manifest") and message in problems[0]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m["queries"].append(dict(m["queries"][0])), "queries/owner_of: duplicate query name"),
        (lambda m: m["queries"][0].update(sparql="DELETE WHERE { $pet ?p ?o }"), "read-only"),
        (
            lambda m: m["queries"][0].update(sparql="SELECT ?o WHERE { ?pet ex:ownedBy ?o }"),
            "parameter pet is not used as $pet",
        ),
        (
            lambda m: m["queries"][1]["params"][1].update(default="3"),
            "default for limit is not of type integer",
        ),
    ],
)
def test_query_problems_are_reported(fresh_bundle_dir: Path, change, message: str) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    change(manifest)
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    problems = verify(fresh_bundle_dir)
    assert len(problems) == 1 and message in problems[0], problems


def test_manifest_without_new_fields_still_opens(fresh_bundle_dir: Path) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    for field in ("queries", "graph_descriptions"):
        manifest.pop(field)
    manifest["graphs"].pop("derived")
    (fresh_bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    bundle = open_bundle(str(fresh_bundle_dir))
    assert bundle.queries == []
    assert set(bundle.graph_descriptions) == {"asserted", "inferred", "ontology"}


def test_write_manifest_keeps_other_fields(fresh_bundle_dir: Path) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    result = write_manifest(fresh_bundle_dir, manifest)
    assert result == manifest


def test_pack_is_reproducible_and_opens(bundle_dir: Path, tmp_path: Path) -> None:
    first = pack(bundle_dir, tmp_path / "a.tar.gz")
    second = pack(bundle_dir, tmp_path / "b.tar.gz")
    assert first == second
    with tarfile.open(tmp_path / "a.tar.gz") as tar:
        assert tar.getnames() == [
            "manifest.json",
            "asserted.nq",
            "derived.nq",
            "inferred.nq",
            "ontology/pets.ttl",
        ]
    opened = open_bundle(str(tmp_path / "a.tar.gz"), tmp_path / "cache")
    assert opened.dataset == "pets" and opened.root.parent == tmp_path / "cache"


def test_archive_with_link_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(BundleError, match="Unsafe entry"):
        locate(str(archive), tmp_path / "cache")


def test_manifest_path_is_accepted(bundle_dir: Path) -> None:
    assert locate(str(bundle_dir / "manifest.json")) == bundle_dir


def test_unknown_source_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="Not a bundle"):
        locate(str(tmp_path / "nothing-here"))


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_root(bundle_dir: Path, tmp_path: Path) -> Iterator[str]:
    pack(bundle_dir, bundle_dir.parent / "pets.tar.gz")
    handler = functools.partial(QuietHandler, directory=str(bundle_dir.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_remote_manifest_downloads_listed_files(http_root: str, bundle_dir: Path, tmp_path: Path) -> None:
    opened = open_bundle(f"{http_root}/{bundle_dir.name}/manifest.json", tmp_path / "cache")
    assert opened.dataset == "pets"
    assert (opened.root / "ontology" / "pets.ttl").is_file()
    # A second open reuses files whose checksums still match.
    assert open_bundle(f"{http_root}/{bundle_dir.name}/", tmp_path / "cache").root == opened.root


def test_remote_archive(http_root: str, tmp_path: Path) -> None:
    opened = open_bundle(f"{http_root}/pets.tar.gz", tmp_path / "cache")
    assert opened.version == "2026-01-01"


def test_download_failure_is_a_bundle_error(http_root: str, tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="Could not download"):
        open_bundle(f"{http_root}/missing/manifest.json", tmp_path / "cache")
