"""Changes to existing records through the draft overlay (draft_change), on the pets fixture.

The committed lines are written with keys in an order no sort would give, so a test that the
untouched keys keep their place cannot pass by accident."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from rdf_kg_mcp.cli import main
from rdf_kg_mcp.drafts import DraftConfig, DraftError, DraftOverlay, field_diff, load_records
from rdf_kg_mcp.graph import KnowledgeGraph
from rdf_kg_mcp.server import build_server

from .test_drafts import DRAFT_BLOCK, dog

# Committed lines as a JavaScript store writes them: props keys unsorted, 0.00001 not 1e-05.
REX = (
    '{"key":"rex","species":"dog","title":"Rex","mood":"calm","props":{"zeta":1,"alpha":{"y":2,"x":1},'
    '"mid":0.00001,"colour":"brown"},"related":[{"rel":"owned_by","to":"alice"}],"seen_in":["finding1"]}'
)
ALICE = '{"key":"alice","species":"person","title":"Alice Smith","props":{},"related":[],"seen_in":[]}'
FINDING = '{"key":"finding1","species":"finding","title":"F1","props":{},"related":[],"seen_in":[]}'


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    folder = tmp_path / "store"
    folder.mkdir()
    (folder / "dogs.jsonl").write_text(REX + "\n")
    (folder / "people.jsonl").write_text(ALICE + "\n")
    with gzip.open(folder / "findings.jsonl.gz", "wt") as handle:
        handle.write(FINDING + "\n")
    (folder / "notes.txt").write_text("not records")
    return folder


@pytest.fixture
def overlay(kg: KnowledgeGraph, tmp_path: Path, store: Path) -> DraftOverlay:
    config = DraftConfig.load(DRAFT_BLOCK, kg)
    return DraftOverlay(kg, config, tmp_path / "out", load_records([store], config))


def test_records_load_from_files_and_directories(kg: KnowledgeGraph, store: Path) -> None:
    config = DraftConfig.load(DRAFT_BLOCK, kg)
    base = load_records([store], config)
    assert sorted(base) == ["alice", "finding1", "rex"]
    assert base["rex"].line == REX and base["rex"].sha256 == sha(REX) and base["rex"].kind == "dog"
    assert sorted(load_records([store / "dogs.jsonl"], config)) == ["rex"]
    with pytest.raises(DraftError, match="rex is in"):
        load_records([store, store / "dogs.jsonl"], config)
    (store / "bad.jsonl").write_text('{"species":"dog"}\n')
    with pytest.raises(DraftError, match="has no string 'key'"):
        load_records([store], config)


def test_a_change_keeps_every_untouched_key_in_place(overlay: DraftOverlay) -> None:
    result = overlay.change(
        "rex",
        [
            {"path": "/props/mid", "value": 0.5},
            {"path": "/props/new", "value": {"b": 1, "a": 2}},
            {"path": "/mood", "remove": True},
            {"path": "/seen_in", "value": ["finding1", "finding2"]},
        ],
        sha(REX),
    )
    assert result["status"] == "changed" and result["problems"] == []
    exported = overlay.export()
    line = json.loads(exported["changes"])
    record = line["record"]
    # The changed value stays where it was; the new key goes last; nothing is sorted, at any depth.
    assert list(record) == ["key", "species", "title", "props", "related", "seen_in"]
    assert list(record["props"]) == ["zeta", "alpha", "mid", "colour", "new"]
    assert list(record["props"]["alpha"]) == ["y", "x"]
    assert list(record["props"]["new"]) == ["b", "a"]
    # Byte for byte, the line is the committed line with only the changed values different.
    expected = (
        REX.replace('"mood":"calm",', "")
        .replace('"mid":0.00001', '"mid":0.5')
        .replace('"colour":"brown"}', '"colour":"brown","new":{"b":1,"a":2}}')
        .replace('["finding1"]', '["finding1","finding2"]')
    )
    assert exported["changes"] == (
        f'{{"id":"rex","base_sha256":"{sha(REX)}","sha256":"{sha(expected)}","record":{expected}}}\n'
    )
    assert line["sha256"] == sha(expected) and line["base_sha256"] == sha(REX)
    assert exported["batch"] == "" and exported["summary"]["added"] == []


def test_a_stale_base_is_refused(overlay: DraftOverlay) -> None:
    older = REX.replace('"Rex"', '"Rex the First"')
    with pytest.raises(DraftError, match="does not match the committed line of rex"):
        overlay.change("rex", [{"path": "/title", "value": "Rex II"}], sha(older))
    with pytest.raises(DraftError, match="does not match"):
        overlay.change("rex", [{"path": "/title", "value": "Rex II"}], None)
    assert overlay.changes == {}
    # A second change accumulates, but only against the same committed line.
    overlay.change("rex", [{"path": "/title", "value": "Rex II"}], sha(REX))
    overlay.change("rex", [{"path": "/props/colour", "value": "black"}], sha(REX))
    paths = [f["path"] for f in overlay.diff()["changed"][0]["fields"]]
    assert paths == ["/title", "/props/colour"]
    with pytest.raises(DraftError, match="does not match"):
        overlay.change("rex", [{"path": "/title", "value": "Rex III"}], sha(older))
    assert overlay.changes["rex"]["title"] == "Rex II"


def test_what_cannot_be_changed(overlay: DraftOverlay, kg: KnowledgeGraph) -> None:
    good = sha(REX)
    cases: list[tuple[str, Any, str]] = [
        ("rex", [{"path": "/key", "value": "rex2"}], "'key' cannot be changed"),
        ("rex", [{"path": "/species", "value": "person"}], "'species' cannot be changed"),
        ("rex", [{"path": "title", "value": "x"}], "must be a JSON Pointer"),
        ("rex", [{"path": "/related/0/to", "value": "x"}], "/related is not an object; replace it whole"),
        ("rex", [{"path": "/props/nope", "remove": True}], "/props/nope does not exist"),
        ("rex", [{"path": "/title"}], "needs either value or remove"),
        ("rex", [{"path": "/title", "value": 1, "extra": 1}], "must be {path, value}"),
        ("rex", [], "non-empty list"),
        ("rex", [{"path": "/props/", "value": 1}], "has an empty key"),
        ("ghost", [{"path": "/title", "value": "x"}], "not in the store records"),
        ("finding1", [{"path": "/title", "value": "x"}], "finding records cannot be drafted"),
    ]
    for record_id, changes, message in cases:
        with pytest.raises(DraftError, match=message):
            overlay.change(record_id, changes, good if record_id == "rex" else "x")
    # Without --draft-records there is nothing to change against.
    bare = DraftOverlay(kg, overlay.config)
    with pytest.raises(DraftError, match="--draft-records"):
        bare.change("rex", [{"path": "/title", "value": "x"}], good)


def test_a_store_line_not_in_the_written_form_is_refused(kg: KnowledgeGraph, tmp_path: Path) -> None:
    (tmp_path / "s.jsonl").write_text(REX.replace("0.00001", "1e-05") + "\n")
    config = DraftConfig.load(DRAFT_BLOCK, kg)
    overlay = DraftOverlay(kg, config, None, load_records([tmp_path / "s.jsonl"], config))
    line = REX.replace("0.00001", "1e-05")
    with pytest.raises(DraftError, match="not in the form this server writes"):
        overlay.change("rex", [{"path": "/title", "value": "x"}], sha(line))


def test_a_change_back_to_the_committed_line_is_unchanged(overlay: DraftOverlay, tmp_path: Path) -> None:
    overlay.change("rex", [{"path": "/title", "value": "Rex II"}], sha(REX))
    overlay.export()
    assert (tmp_path / "out" / "changes.jsonl").is_file()
    back = overlay.change("rex", [{"path": "/title", "value": "Rex"}], sha(REX))
    assert back["status"] == "unchanged" and back["fields"] == []
    assert overlay.diff()["changed"] == []
    # The next export leaves no stale changes file behind.
    result = overlay.export()
    assert "changes" not in result and result["removed"] == [str(tmp_path / "out" / "changes.jsonl")]
    assert not (tmp_path / "out" / "changes.jsonl").exists()


def test_draft_remove_drops_a_change(overlay: DraftOverlay) -> None:
    overlay.change("rex", [{"path": "/title", "value": "Rex II"}], sha(REX))
    removed = overlay.remove("rex")
    assert removed["removed"] is True and "committed line stands" in removed["note"]
    assert overlay.changes == {}


def test_problems_the_change_brings_in_block_the_export(overlay: DraftOverlay) -> None:
    result = overlay.change(
        "rex", [{"path": "/related", "value": [{"rel": "owned_by", "to": "rex"}]}], sha(REX)
    )
    assert result["problems"] == ["related[0]: a record cannot link to itself"]
    refused = overlay.export()
    assert refused["exported"] is False and list(refused["problems"]) == ["rex"]
    # A link to a store record that the release lacks is fine: its kind comes from the store line.
    ok = overlay.change(
        "rex", [{"path": "/related", "value": [{"rel": "owned_by", "to": "alice"}]}], sha(REX)
    )
    assert ok["status"] == "unchanged"


def test_problems_already_in_the_committed_line_do_not_block(kg: KnowledgeGraph, tmp_path: Path) -> None:
    bad = REX.replace('"mood":"calm"', '"mood":"sleepy"')
    (tmp_path / "s.jsonl").write_text(bad + "\n")
    config = DraftConfig.load(DRAFT_BLOCK, kg)
    overlay = DraftOverlay(kg, config, None, load_records([tmp_path / "s.jsonl"], config))
    result = overlay.change("rex", [{"path": "/title", "value": "Rex II"}], sha(bad))
    assert result["problems"] == []
    assert any(w.startswith("already in the committed line: mood") for w in result["warnings"])
    assert overlay.export()["exported"] is True


def test_store_ids_cannot_be_added_again(overlay: DraftOverlay) -> None:
    with pytest.raises(DraftError, match="already in the store records"):
        overlay.upsert(dog("alice", species="person"))
    # A new record may link to a store record the release does not hold.
    added = overlay.upsert(dog("fido", related=[{"rel": "friend_of", "to": "rex"}], seen_in=["finding1"]))
    assert added["problems"] == [] and added["warnings"] == []


def test_export_with_adds_and_changes_is_deterministic(
    overlay: DraftOverlay, kg: KnowledgeGraph, store: Path, tmp_path: Path
) -> None:
    overlay.upsert(dog("fido"))
    overlay.change("rex", [{"path": "/props/colour", "value": "black"}], sha(REX))
    overlay.change("alice", [{"path": "/title", "value": "Alice Jones"}], sha(ALICE))
    first = overlay.export()
    out = tmp_path / "out"
    assert first["batch"] == (out / "batch.jsonl").read_text()
    assert [json.loads(x)["id"] for x in first["changes"].splitlines()] == ["alice", "rex"]
    # A second overlay given the same calls in another order writes the same bytes.
    again = DraftOverlay(kg, overlay.config, tmp_path / "again", load_records([store], overlay.config))
    again.change("alice", [{"path": "/title", "value": "Alice Jones"}], sha(ALICE))
    again.change("rex", [{"path": "/props/colour", "value": "black"}], sha(REX))
    again.upsert(dog("fido"))
    second = again.export()
    for name in ("batch.jsonl", "changes.jsonl", "summary.json"):
        assert (out / name).read_bytes() == (tmp_path / "again" / name).read_bytes(), name
    assert overlay.export()["changes_sha256"] == first["changes_sha256"] == second["changes_sha256"]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["changes_sha256"] == hashlib.sha256((out / "changes.jsonl").read_bytes()).hexdigest()
    assert summary["changed"][1]["fields"] == [
        {"path": "/props/colour", "change": "changed", "before": "brown", "after": "black"}
    ]
    # Adding a record does not change the batch bytes a changes-free overlay would write.
    only_add = DraftOverlay(kg, overlay.config, None)
    only_add.upsert(dog("fido"))
    assert only_add.export()["batch"] == first["batch"]


def test_field_diff() -> None:
    before = {"a": 1, "b": {"x": [1, 2], "y": "k"}, "c": True}
    after = {"b": {"y": "k", "x": [2, 1], "z": 0}, "a": 1.0, "d": None}
    assert field_diff(before, after) == [
        {"path": "/b/x", "change": "changed", "before": [1, 2], "after": [2, 1]},
        {"path": "/b/z", "change": "added", "after": 0},
        {"path": "/c", "change": "removed", "before": True},
        {"path": "/d", "change": "added", "after": None},
    ]
    assert field_diff({"a/b": 1}, {"a/b": 2})[0]["path"] == "/a~1b"


@pytest.mark.anyio
async def test_draft_change_over_mcp(kg: KnowledgeGraph, store: Path, tmp_path: Path) -> None:
    config = DraftConfig.load(DRAFT_BLOCK, kg)
    drafts = DraftOverlay(kg, config, tmp_path / "out", load_records([store], config))
    async with Client(build_server(kg, None, max_rows=20, drafts=drafts)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "3 loaded" in (tools["draft_change"].description or "")
        stale = await client.call_tool(
            "draft_change",
            {"id": "rex", "changes": [{"path": "/title", "value": "R"}], "base_sha256": "0" * 64},
        )
        assert stale.is_error and "does not match" in stale.content[0].text  # type: ignore[union-attr]
        result = await client.call_tool(
            "draft_change",
            {"id": "rex", "changes": [{"path": "/title", "value": "R"}], "base_sha256": sha(REX)},
        )
        assert result.structured_content and result.structured_content["status"] == "changed"
        diff = await client.call_tool("draft_diff", {})
        assert diff.structured_content and diff.structured_content["changed"][0]["id"] == "rex"
        exported = await client.call_tool("draft_export", {})
        assert exported.structured_content and exported.structured_content["changed"] == 1
    assert (tmp_path / "out" / "changes.jsonl").is_file()


def test_draft_records_need_drafts_and_must_exist(
    bundle_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["serve", str(bundle_dir), "--draft-records", "x"]) == 2
    assert "need --drafts" in capsys.readouterr().err
    assert main(["serve", str(bundle_dir), "--drafts", "--draft-records", "/no/such/file"]) == 2
    assert "not found: /no/such/file" in capsys.readouterr().err


def test_a_key_removed_and_set_again_keeps_its_place(overlay: DraftOverlay) -> None:
    # The same value back: nothing changed, whatever order the changes came in.
    same = overlay.change(
        "rex", [{"path": "/props/zeta", "remove": True}, {"path": "/props/zeta", "value": 1}], sha(REX)
    )
    assert same["status"] == "unchanged" and same["fields"] == []
    # A new value through remove and set: the key stays where the committed line has it.
    moved = overlay.change(
        "rex",
        [
            {"path": "/props/zeta", "remove": True},
            {"path": "/props/zeta", "value": 2},
            {"path": "/title", "remove": True},
            {"path": "/title", "value": "Rex"},
        ],
        sha(REX),
    )
    assert moved["status"] == "changed"
    assert [f["path"] for f in moved["fields"]] == ["/props/zeta"]
    record = json.loads(overlay.export()["changes"])["record"]
    assert list(record) == ["key", "species", "title", "mood", "props", "related", "seen_in"]
    assert list(record["props"]) == ["zeta", "alpha", "mid", "colour"]


def test_a_lone_surrogate_is_refused_before_anything_is_stored(overlay: DraftOverlay) -> None:
    # JSON.stringify would escape it, but UTF-8 cannot hold it, so the line could not be hashed.
    for changes in (
        [{"path": "/props/colour", "value": "\ud800"}],
        [{"path": "/props/\udc00", "value": "x"}],
        [{"path": "/props/colour", "value": {"nested": ["ok", "\ud800"]}}],
    ):
        with pytest.raises(DraftError, match="lone surrogate"):
            overlay.change("rex", changes, sha(REX))
        assert overlay.changes == {}
    overlay.change("rex", [{"path": "/props/colour", "value": "black"}], sha(REX))
    assert overlay.export()["changes"].count("\n") == 1
    with pytest.raises(DraftError, match="lone surrogate"):
        overlay.upsert(dog("pip", title="Pip \ud800"))
    assert "pip" not in overlay.records


def test_new_keys_follow_the_order_they_were_set_in(overlay: DraftOverlay) -> None:
    overlay.change("rex", [{"path": "/props/n1", "value": 1}, {"path": "/props/n2", "value": 2}], sha(REX))
    assert list(overlay.changes["rex"]["props"])[-2:] == ["n1", "n2"]
    overlay.remove("rex")
    overlay.change("rex", [{"path": "/props/n2", "value": 2}, {"path": "/props/n1", "value": 1}], sha(REX))
    assert list(overlay.changes["rex"]["props"])[-2:] == ["n2", "n1"]
