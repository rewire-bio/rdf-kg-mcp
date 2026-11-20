"""The draft overlay, on the pets fixture with a record shape unlike any real dataset's.

Field names (key, species, title, props, related, seen_in) are chosen so that nothing here
could pass by accident on code that assumes one dataset's record shape.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from rdf_kg_mcp.bundle import open_bundle, verify, write_manifest
from rdf_kg_mcp.cli import main
from rdf_kg_mcp.drafts import DraftConfig, DraftError, DraftOverlay, _umask, js_number, line
from rdf_kg_mcp.graph import KnowledgeGraph
from rdf_kg_mcp.server import build_server

from .conftest import ID

DRAFT_BLOCK: dict[str, Any] = {
    "iri_base": ID,
    "id_field": "key",
    "type_field": "species",
    "label_field": "title",
    "record_schema": {
        "type": "object",
        "required": ["key", "species", "title", "props", "related", "seen_in"],
        "properties": {
            "key": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
            "species": {"type": "string"},
            "title": {"type": "string", "minLength": 1},
            "mood": {"enum": ["calm", "lively"]},
            "props": {"type": "object"},
            "related": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["rel", "to"],
                    "properties": {"rel": {"type": "string"}, "to": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "seen_in": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    "kinds": {"dog": "ex:Dog", "person": "ex:Person", "finding": "ex:Finding"},
    "draft_kinds": ["dog", "person"],
    "links": {"field": "related", "relation": "rel", "target": "to"},
    "link_rules": {
        "owned_by": {"from": ["dog"], "to": ["person"]},
        "friend_of": {"from": ["dog", "person"], "to": ["dog", "person"]},
    },
    "references": [{"field": "seen_in", "kinds": ["finding"]}],
    "sort_within": ["props"],
    "sort_arrays": ["related"],
}


def dog(key: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "key": key,
        "species": "dog",
        "title": key.title(),
        "props": {},
        "related": [],
        "seen_in": [],
    }
    record.update(fields)
    return record


@pytest.fixture
def overlay(kg: KnowledgeGraph, tmp_path: Path) -> DraftOverlay:
    return DraftOverlay(kg, DraftConfig.load(DRAFT_BLOCK, kg), tmp_path / "out")


def test_new_record_is_added_with_its_iri(overlay: DraftOverlay) -> None:
    result = overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "alice"}]))
    assert result["status"] == "added"
    assert result["iri"] == ID + "fido"
    assert result["problems"] == [] and result["warnings"] == []
    assert overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "alice"}]))["status"] == "unchanged"
    assert overlay.upsert(dog("fido"))["status"] == "replaced"


def test_release_ids_are_refused(overlay: DraftOverlay) -> None:
    with pytest.raises(DraftError, match="already in release"):
        overlay.upsert(dog("rex"))
    with pytest.raises(DraftError, match="non-empty string 'key'"):
        overlay.upsert({"species": "dog"})
    with pytest.raises(DraftError, match="JSON object"):
        overlay.upsert(["fido"])


def test_shape_problems_come_from_the_record_schema(overlay: DraftOverlay) -> None:
    problems = overlay.upsert(dog("Bad--id", colour="brown", mood="sleepy"))["problems"]
    text = "\n".join(problems)
    assert "key: 'Bad--id' does not match" in text
    assert "Additional properties are not allowed ('colour' was unexpected)" in text
    assert "mood: 'sleepy' is not one of ['calm', 'lively']" in text
    link = overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "alice", "why": "x"}]))
    assert any("related/0" in p and "'why' was unexpected" in p for p in link["problems"])


def test_kinds_must_be_known_and_draftable(overlay: DraftOverlay) -> None:
    assert "species: 'cat' is not a kind" in overlay.upsert(dog("tom", species="cat"))["problems"][0]
    finding = overlay.upsert(dog("f9", species="finding"))["problems"]
    assert finding == ["species: finding records cannot be drafted; draftable kinds: dog, person"]


def test_link_rules_check_both_kinds(overlay: DraftOverlay) -> None:
    ok = overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "alice"}]))
    assert ok["problems"] == []
    wrong_target = overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "dog01"}]))["problems"]
    assert wrong_target == ["related[0]: owned_by cannot point to dog01, a dog; it points to person"]
    person = {**dog("bob", species="person"), "related": [{"rel": "owned_by", "to": "alice"}]}
    assert overlay.upsert(person)["problems"] == [
        "related[0]: owned_by cannot start from a person; it starts from dog"
    ]
    unknown = overlay.upsert(dog("fido", related=[{"rel": "likes", "to": "alice"}]))["problems"]
    assert unknown == ["related[0]: unknown relation 'likes'"]
    twice = [{"rel": "friend_of", "to": "rex"}, {"rel": "friend_of", "to": "rex"}]
    assert overlay.upsert(dog("fido", related=twice))["problems"] == [
        "related[1]: friend_of to rex is listed twice"
    ]
    selfish = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "fido"}]))["problems"]
    assert selfish == ["related[0]: a record cannot link to itself"]


def test_missing_targets_are_warnings_because_the_release_lags(overlay: DraftOverlay) -> None:
    result = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "newer"}]))
    assert result["problems"] == []
    assert result["warnings"][0].startswith("related[0]: newer is not in release 2026-01-01 or the drafts.")
    # A target with no kind in the release (leeds has no rdf:type) is reported, not refused.
    untyped = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "leeds"}]))
    assert untyped["warnings"] == ["related[0]: the kind of release record leeds could not be determined"]


def test_links_between_drafts_are_checked_again_on_diff(overlay: DraftOverlay) -> None:
    overlay.upsert(dog("pup", related=[{"rel": "owned_by", "to": "carol"}]))
    assert overlay.diff()["added"][0]["warnings"]  # carol is not drafted yet
    overlay.upsert({**dog("carol", species="person"), "title": "Carol"})
    diff = overlay.diff()
    assert [e["id"] for e in diff["added"]] == ["carol", "pup"]
    assert diff["problems"] == 0 and diff["warnings"] == 0
    assert diff["changed"] == []
    removed = overlay.remove("carol")
    assert removed["removed"] is True and "pup" in removed["note"]
    assert overlay.diff()["warnings"] == 1
    assert overlay.remove("carol")["removed"] is False


def test_references_must_name_the_right_kind(overlay: DraftOverlay) -> None:
    # Findings are typed only in the derived graph; kinds come from asserted types alone.
    found = overlay.upsert(dog("fido", seen_in=["finding1"]))
    assert found["problems"] == []
    assert found["warnings"] == ["seen_in[0]: the kind of release record finding1 could not be determined"]
    wrong = overlay.upsert(dog("fido", seen_in=["alice", "finding1", "finding1"]))["problems"]
    assert wrong == [
        "seen_in[0]: alice is a person; this field takes finding",
        "seen_in[2]: finding1 is listed twice",
    ]


def test_numbers_that_json_readers_cannot_carry_are_refused(overlay: DraftOverlay) -> None:
    big = overlay.upsert(dog("fido", props={"n": 2**53, "ok": 2**53 - 1, "f": 1e-05}))["problems"]
    assert big == ["props/n: integer 9007199254740992 is outside the range JSON numbers carry exactly (2^53)"]
    with pytest.raises(DraftError, match="not valid JSON"):
        overlay.upsert(dog("fido", props={"x": float("nan")}))


def test_true_and_one_are_different_values(overlay: DraftOverlay) -> None:
    overlay.upsert(dog("fido", props={"x": 1}))
    assert overlay.upsert(dog("fido", props={"x": True}))["status"] == "replaced"
    # 1 and 1.0 are the same JSON number and export as the same bytes.
    overlay.upsert(dog("fido", props={"x": 1}))
    assert overlay.upsert(dog("fido", props={"x": 1.0}))["status"] == "unchanged"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (2e-05, "0.00002"),
        (1e-05, "0.00001"),
        (1e-06, "0.000001"),
        (1e-07, "1e-7"),
        (1.5e-07, "1.5e-7"),
        (1.0, "1"),
        (-0.0, "0"),
        (0.1, "0.1"),
        (-2.5, "-2.5"),
        (123456.789, "123456.789"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.2345678901234568e20, "123456789012345680000"),
        (2.5e25, "2.5e+25"),
    ],
)
def test_numbers_are_written_as_javascript_writes_them(value: float, text: str) -> None:
    assert js_number(value) == text


def test_export_lines_are_store_lines(overlay: DraftOverlay) -> None:
    record = dog("fido", props={"learning_rate": 2e-05, "epochs": 3, "ratio": 1.0, "big": 1e21})
    overlay.upsert(record)
    batch_line = overlay.export()["batch"].strip()
    assert '"big":1e+21' in batch_line and '"epochs":3' in batch_line
    assert '"learning_rate":0.00002' in batch_line and '"ratio":1' in batch_line
    # Read back and written again (what a JavaScript import does), the bytes do not change.
    assert line(json.loads(batch_line), overlay.config) == batch_line


def test_ids_that_cannot_be_records_are_problems_not_crashes(overlay: DraftOverlay) -> None:
    bad = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "esm 2"}], seen_in=["a b"]))
    assert bad["status"] == "added"
    assert bad["problems"] == [
        "related[0]: 'esm 2' cannot be part of an IRI",
        "seen_in[0]: 'a b' cannot be part of an IRI",
    ]
    # A sub-resource of a record is an IRI, but not a record id.
    sub = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "rex/missing/owner"}]))
    assert sub["problems"] == [
        "related[0]: 'rex/missing/owner' is not a valid key: 'rex/missing/owner' does not match "
        "'^[a-z0-9]+(?:-[a-z0-9]+)*$'"
    ]
    # The overlay stays usable: diff and export report the problem instead of failing.
    assert overlay.diff()["problems"] == 1
    assert overlay.export()["exported"] is False
    with pytest.raises(DraftError, match="cannot be part of an IRI"):
        overlay.upsert(dog("a b>c"))
    assert "a b>c" not in overlay.records


def test_long_validator_messages_are_cut(overlay: DraftOverlay) -> None:
    config = DraftConfig.load(
        {
            **DRAFT_BLOCK,
            "record_schema": {
                **DRAFT_BLOCK["record_schema"],
                "properties": {
                    **DRAFT_BLOCK["record_schema"]["properties"],
                    "mood": {"enum": [f"mood-{i}" for i in range(500)]},
                },
            },
        },
        overlay.kg,
    )
    problems = DraftOverlay(overlay.kg, config).upsert(dog("fido", mood="x"))["problems"]
    assert len(problems) == 1 and len(problems[0]) < 320 and problems[0].endswith(" ...")


def test_possible_duplicates(overlay: DraftOverlay) -> None:
    found = overlay.upsert(dog("rexy", title="REX"))["possible_duplicates"]
    rex = next(d for d in found if d["id"] == "rex")
    assert rex["in"] == "release" and rex["kind"] == "dog" and rex["match"] == "same name, search"
    assert len([d for d in found if d["match"].endswith("search")]) <= 3
    overlay.upsert({**dog("bob", species="person"), "title": "Dog-01"})
    other = overlay.upsert(dog("pupper", title="dog 01"))["possible_duplicates"]
    assert {"id": "bob", "in": "draft", "kind": "person", "label": "Dog-01", "match": "same name"} in other
    assert any(d["id"] == "dog01" and d["match"] == "same name, search" for d in other)


def test_unrelated_search_hits_are_not_duplicates(overlay: DraftOverlay) -> None:
    # Search finds rex on its description ("Good dog"), which says nothing about its name.
    assert overlay.kg.search("Good small pup", types=["ex:Dog"])["results"][0]["iri"] == ID + "rex"
    assert overlay.upsert(dog("pup", title="Good small pup"))["possible_duplicates"] == []
    found = overlay.upsert(dog("zz", title="Fakeformer RV330 small"))["possible_duplicates"]
    assert found == []
    near = overlay.upsert(dog("zz2", title="Fakeformer RV330"))["possible_duplicates"]
    assert near == [
        {"id": "zz", "in": "draft", "kind": "dog", "label": "Fakeformer RV330 small", "match": "near name"}
    ]
    assert not overlay.upsert(dog("zz3", title="Bulldog"))["possible_duplicates"]


def test_export_bytes_are_deterministic(overlay: DraftOverlay, kg: KnowledgeGraph, tmp_path: Path) -> None:
    links = [{"rel": "owned_by", "to": "alice"}, {"rel": "friend_of", "to": "rex"}]
    props = {"b": {"z": 1, "a": [{"y": 2, "x": 1}]}, "a": 0.00001, "￿": 1, "\U0001f600": 2}
    overlay.upsert(dog("fido", props=props, related=links, seen_in=["finding2", "finding1"]))
    overlay.upsert({**dog("carol", species="person"), "title": "Carol"})
    first = overlay.export()
    assert first["exported"] is True and first["records"] == 2
    batch = (tmp_path / "out" / "batch.jsonl").read_bytes()
    assert batch.decode() == first["batch"]
    fido = json.loads(batch.decode().splitlines()[1])
    assert list(fido) == ["key", "species", "title", "props", "related", "seen_in"]
    # Keys sorted at every depth inside props, in UTF-16 order (the emoji's surrogates sort first).
    assert list(fido["props"]) == ["a", "b", "\U0001f600", "￿"]
    assert list(fido["props"]["b"]["a"][0]) == ["x", "y"]
    assert batch.decode().splitlines()[1].count('"a":0.00001') == 1
    # Links sorted by relation then target; other lists keep the caller's order.
    assert fido["related"] == [{"rel": "friend_of", "to": "rex"}, {"rel": "owned_by", "to": "alice"}]
    assert fido["seen_in"] == ["finding2", "finding1"]

    # The same content in another key and link order, in a new overlay, gives the same bytes.
    again = DraftOverlay(kg, overlay.config, tmp_path / "again")
    reordered = {
        "seen_in": ["finding2", "finding1"],
        "related": list(reversed(links)),
        "props": dict(reversed(list(props.items()))),
        "title": "Fido",
        "species": "dog",
        "key": "fido",
    }
    again.upsert(
        {"title": "Carol", "key": "carol", "species": "person", "props": {}, "related": [], "seen_in": []}
    )
    again.upsert(reordered)
    assert again.export()["batch"] == first["batch"]
    assert (tmp_path / "again" / "batch.jsonl").read_bytes() == batch
    assert overlay.export()["batch_sha256"] == first["batch_sha256"]
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["batch_sha256"] == first["batch_sha256"]
    assert [e["id"] for e in summary["added"]] == ["carol", "fido"]


def test_export_is_refused_while_drafts_have_problems(overlay: DraftOverlay, tmp_path: Path) -> None:
    overlay.upsert(dog("fido", related=[{"rel": "owned_by", "to": "dog01"}]))
    result = overlay.export()
    assert result["exported"] is False and list(result["problems"]) == ["fido"]
    assert not (tmp_path / "out").exists()


def test_a_refused_export_leaves_no_earlier_batch(overlay: DraftOverlay, tmp_path: Path) -> None:
    overlay.upsert(dog("fido"))
    assert overlay.export()["exported"] is True
    batch = tmp_path / "out" / "batch.jsonl"
    # Readable by others as a plain open() would leave it, not the owner-only mode of mkstemp.
    assert batch.stat().st_mode & 0o777 == 0o666 & ~_umask()
    overlay.upsert(dog("Bad_ID"))
    refused = overlay.export()
    assert refused["exported"] is False
    assert not batch.exists() and not (tmp_path / "out" / "summary.json").exists()
    assert sorted(refused["removed"]) == sorted([str(batch), str(tmp_path / "out" / "summary.json")])


def test_without_a_draft_dir_nothing_is_written(kg: KnowledgeGraph) -> None:
    inline = DraftOverlay(kg, DraftConfig.load(DRAFT_BLOCK, kg))
    inline.upsert(dog("fido"))
    result = inline.export()
    assert result["written"] == [] and result["batch"] == line(dog("fido"), inline.config) + "\n"


def test_bad_draft_blocks_are_refused(kg: KnowledgeGraph) -> None:
    with pytest.raises(DraftError, match="kinds not listed in kinds"):
        DraftConfig.load({**DRAFT_BLOCK, "draft_kinds": ["cat"]}, kg)
    with pytest.raises(DraftError, match="more than one kind"):
        DraftConfig.load(
            {**DRAFT_BLOCK, "kinds": {"dog": "ex:Dog", "puppy": "ex:Dog"}, "draft_kinds": []}, kg
        )
    with pytest.raises(DraftError, match="Invalid draft configuration"):
        DraftConfig.load({**DRAFT_BLOCK, "surprise": 1}, kg)


def test_manifest_can_carry_the_draft_block(fresh_bundle_dir: Path) -> None:
    manifest = json.loads((fresh_bundle_dir / "manifest.json").read_text())
    manifest["draft"] = DRAFT_BLOCK
    write_manifest(fresh_bundle_dir, manifest)
    assert verify(fresh_bundle_dir) == []
    manifest["draft"] = {**DRAFT_BLOCK, "links": {"field": "related"}}
    write_manifest(fresh_bundle_dir, manifest)
    assert any("draft/links" in p for p in verify(fresh_bundle_dir))


# Server and command line ---------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def draft_client(kg: KnowledgeGraph, tmp_path: Path) -> AsyncIterator[Client]:
    drafts = DraftOverlay(kg, DraftConfig.load(DRAFT_BLOCK, kg), tmp_path / "out")
    async with Client(build_server(kg, None, max_rows=20, drafts=drafts)) as c:
        yield c


@pytest.mark.anyio
async def test_draft_tools_over_mcp(draft_client: Client, tmp_path: Path) -> None:
    tools = {t.name: t for t in (await draft_client.list_tools()).tools}
    assert {"draft_upsert", "draft_remove", "draft_diff", "draft_export"} <= set(tools)
    assert tools["draft_upsert"].annotations and tools["draft_upsert"].annotations.read_only_hint is False
    assert "species" in (tools["draft_upsert"].description or "")
    result = await draft_client.call_tool("draft_upsert", {"record": dog("fido")})
    assert result.structured_content and result.structured_content["status"] == "added"
    refused = await draft_client.call_tool("draft_upsert", {"record": dog("rex")})
    assert refused.is_error and "already in release" in refused.content[0].text  # type: ignore[union-attr]
    bad_id = await draft_client.call_tool("draft_upsert", {"record": dog("a b>c")})
    assert bad_id.is_error and "cannot be part of an IRI" in bad_id.content[0].text  # type: ignore[union-attr]
    bad_target = await draft_client.call_tool(
        "draft_upsert", {"record": dog("spot", related=[{"rel": "friend_of", "to": "esm 2"}])}
    )
    assert bad_target.structured_content and bad_target.structured_content["problems"]
    await draft_client.call_tool("draft_remove", {"id": "spot"})
    diff = await draft_client.call_tool("draft_diff", {})
    assert diff.structured_content and [e["id"] for e in diff.structured_content["added"]] == ["fido"]
    exported = await draft_client.call_tool("draft_export", {})
    assert exported.structured_content and exported.structured_content["exported"] is True
    assert (tmp_path / "out" / "batch.jsonl").is_file()
    # Drafts are not in the release graphs.
    search = await draft_client.call_tool("find_entity", {"text": "Fido"})
    assert search.structured_content and search.structured_content["total"] == 0


@pytest.mark.anyio
async def test_draft_tools_are_absent_by_default(kg: KnowledgeGraph) -> None:
    async with Client(build_server(kg, None)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert not any(n.startswith("draft_") for n in names)


def test_drafts_are_refused_over_http(bundle_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(bundle_dir), "--drafts", "--transport", "streamable-http"]) == 2
    assert "only allowed with --transport stdio" in capsys.readouterr().err
    assert main(["serve", str(bundle_dir), "--draft-dir", "x"]) == 2


def test_drafts_need_a_draft_block(
    bundle_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_load(*args: Any) -> None:
        raise AssertionError("the store was loaded before the draft block was checked")

    monkeypatch.setattr("rdf_kg_mcp.cli.load_store", no_load)
    assert main(["serve", str(bundle_dir), "--drafts"]) == 2
    assert "no draft block" in capsys.readouterr().err


@pytest.mark.anyio
async def test_serve_with_drafts_over_stdio(bundle_dir: Path, tmp_path: Path) -> None:
    config = tmp_path / "draft.json"
    config.write_text(json.dumps(DRAFT_BLOCK))
    out = tmp_path / "batch"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "rdf_kg_mcp",
            "serve",
            str(bundle_dir),
            "--store",
            "disk",
            "--drafts",
            "--draft-config",
            str(config),
            "--draft-dir",
            str(out),
        ],
        env={**os.environ, "RDF_KG_MCP_CACHE": str(tmp_path / "cache")},
    )
    async with Client(params) as client:
        await client.call_tool("draft_upsert", {"record": dog("fido")})
        result = await client.call_tool("draft_export", {})
        assert result.structured_content and result.structured_content["exported"] is True
    assert (out / "batch.jsonl").read_text() == line(
        dog("fido"), DraftConfig.load(DRAFT_BLOCK, _kg(bundle_dir))
    ) + "\n"
    shutil.rmtree(tmp_path / "cache")


def _kg(bundle_dir: Path) -> KnowledgeGraph:
    from rdf_kg_mcp.graph import load_store

    bundle = open_bundle(str(bundle_dir))
    return KnowledgeGraph(bundle, load_store(bundle))


def test_many_records_with_the_same_name_are_capped(overlay: DraftOverlay) -> None:
    found = overlay.upsert(dog("pup", title="Dog"))["possible_duplicates"]
    assert all(d.get("label") != "Dog" or d["in"] != "release" for d in found)
    for i in range(12):
        overlay.upsert({**dog(f"p{i}", species="person"), "title": "Rex"})
    # Release matches are capped; draft matches are always listed.
    dups = overlay.upsert(dog("rexy", title="Rex"))["possible_duplicates"]
    assert sum(d.get("in") == "draft" for d in dups) == 12
    assert next(d for d in dups if d["id"] == "rex")["label"] == "Rex"


def test_same_name_release_matches_beyond_the_cap_are_counted(
    overlay: DraftOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rdf_kg_mcp.drafts.MAX_SAME_NAME", 0)
    result = overlay.upsert(dog("rexy", title="Rex"))
    assert result["same_name_omitted"] == 1
    assert all("id" in d for d in result["possible_duplicates"])
    assert overlay.diff()["added"][0]["same_name_omitted"] == 1
    monkeypatch.setattr("rdf_kg_mcp.drafts.MAX_SAME_NAME", 10)
    assert "same_name_omitted" not in overlay.upsert(dog("rexy", title="Rex"))
