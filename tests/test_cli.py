from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from rdf_kg_mcp.cli import main


def test_verify_ok_and_json(bundle_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", str(bundle_dir)]) == 0
    assert capsys.readouterr().out.startswith("OK: pets 2026-01-01 (4 files)")
    assert main(["verify", "--json", str(bundle_dir)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True and report["problems"] == []


def test_verify_reports_problems(fresh_bundle_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fresh_bundle_dir / "asserted.nq").write_text("")
    assert main(["verify", str(fresh_bundle_dir)]) == 1
    assert "asserted.nq: sha256" in capsys.readouterr().err


def test_manifest_command_writes_hashes_and_roles(bundle_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "new"
    target.mkdir()
    shutil.copy(bundle_dir / "asserted.nq", target / "asserted.nq")
    code = main(
        [
            "manifest",
            str(target),
            "--dataset",
            "pets",
            "--version",
            "1",
            "--graph",
            "asserted=https://e.org/g",
        ]
    )
    assert code == 0
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["graphs"] == {"asserted": "https://e.org/g"}
    assert manifest["files"]["asserted.nq"]["quads"] == 73


def test_manifest_requires_dataset(tmp_path: Path) -> None:
    assert main(["manifest", str(tmp_path)]) == 2


def test_pack_command(bundle_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack", str(bundle_dir), "-o", str(tmp_path / "pets.tar.gz")]) == 0
    assert "sha256" in capsys.readouterr().out
    assert main(["verify", str(tmp_path / "pets.tar.gz")]) == 0


def test_bad_bundle_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().err.startswith("error: Not a bundle")


@pytest.mark.anyio
async def test_serve_over_stdio(bundle_dir: Path, tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rdf_kg_mcp", "serve", str(bundle_dir), "--store", "disk", "--query-timeout", "20"],
        env={**os.environ, "RDF_KG_MCP_CACHE": str(tmp_path / "cache")},
    )
    async with Client(params) as client:
        result = await client.call_tool("sparql_query", {"query": "SELECT ?o WHERE { id:alice ex:owns ?o }"})
        assert result.structured_content == {
            "type": "select",
            "variables": ["o"],
            "rows": [{"o": "id:rex"}],
            "truncated": False,
        }
    assert list((tmp_path / "cache").glob("store-*"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
