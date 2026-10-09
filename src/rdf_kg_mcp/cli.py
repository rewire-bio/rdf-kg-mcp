"""Command line: serve, verify, manifest and pack."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .bundle import BundleError, cache_root, locate, open_bundle, pack, verify, write_manifest
from .graph import KnowledgeGraph, load_store
from .query import QueryRunner

log = logging.getLogger("rdf_kg_mcp")


def _serve(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir) if args.cache_dir else cache_root()
    started = time.monotonic()
    bundle = open_bundle(args.bundle, cache_dir)
    store_dir = cache_dir if args.store == "disk" else None
    kg = KnowledgeGraph(bundle, load_store(bundle, store_dir))
    log.info(
        "Loaded %s %s: %d statements in %.1fs",
        bundle.dataset,
        bundle.version or "",
        len(kg.store),
        time.monotonic() - started,
    )
    runner = QueryRunner(bundle, store_dir, args.query_timeout) if args.query_timeout > 0 else None
    from .server import build_server  # imported late so verify/pack do not load the MCP stack

    server = build_server(kg, runner, max_rows=args.max_rows)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http", host=args.host, port=args.port)
    finally:
        if runner is not None:
            runner.close()
    return 0


def _verify(args: argparse.Namespace) -> int:
    root = locate(args.bundle, Path(args.cache_dir) if args.cache_dir else None)
    problems = verify(root, count=not args.quick)
    manifest = (
        json.loads((root / "manifest.json").read_text("utf-8")) if (root / "manifest.json").is_file() else {}
    )
    report = {
        "bundle": str(root),
        "dataset": manifest.get("dataset"),
        "version": manifest.get("version") or manifest.get("release_id"),
        "files": len(manifest.get("files") or {}),
        "valid": not problems,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    elif problems:
        print(f"INVALID: {root}", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
    else:
        print(f"OK: {report['dataset']} {report['version'] or ''} ({report['files']} files) at {root}")
    return 0 if not problems else 1


def _manifest(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    if args.dataset:
        manifest["dataset"] = args.dataset
    if args.version:
        manifest["version"] = args.version
    if "dataset" not in manifest:
        print("manifest.json has no dataset; pass --dataset", file=sys.stderr)
        return 2
    for item in args.graph or []:
        role, _, iri = item.partition("=")
        if not role or not iri:
            print(f"--graph expects ROLE=IRI, got {item!r}", file=sys.stderr)
            return 2
        manifest.setdefault("graphs", {})[role] = iri
    result = write_manifest(root, manifest, files=args.files or None)
    problems = verify(root)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"Wrote {path} ({len(result['files'])} files)")
    return 0 if not problems else 1


def _pack(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    problems = verify(root)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    digest = pack(root, Path(args.output))
    print(f"{args.output} sha256 {digest}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rdf-kg-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=f"rdf-kg-mcp {__version__}")
    p.add_argument("--cache-dir", help="Where downloads and disk stores go (default ~/.cache/rdf-kg-mcp)")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Serve a bundle over MCP")
    serve.add_argument("bundle", help="Bundle directory, manifest.json, .tar.gz, or URL of either")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--store",
        choices=["memory", "disk"],
        default="memory",
        help="disk keeps a store per bundle in the cache directory, so restarts are fast",
    )
    serve.add_argument(
        "--query-timeout",
        type=float,
        default=30.0,
        help="Seconds before a SPARQL query is stopped; 0 runs queries in-process with no timeout",
    )
    serve.add_argument("--max-rows", type=int, default=1000, help="Default row cap for sparql_query")
    serve.set_defaults(func=_serve)

    check = sub.add_parser("verify", help="Check a bundle's manifest, checksums and statement counts")
    check.add_argument("bundle")
    check.add_argument("--quick", action="store_true", help="Check checksums only, without parsing files")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_verify)

    manifest = sub.add_parser("manifest", help="Write or refresh manifest.json for a bundle directory")
    manifest.add_argument("directory")
    manifest.add_argument("--dataset")
    manifest.add_argument("--version", dest="version")
    manifest.add_argument("--graph", action="append", metavar="ROLE=IRI", help="Declare a graph role")
    manifest.add_argument("files", nargs="*", help="Files to list (default: every RDF file in the directory)")
    manifest.set_defaults(func=_manifest)

    archive = sub.add_parser("pack", help="Write a byte-reproducible .tar.gz of a verified bundle")
    archive.add_argument("directory")
    archive.add_argument("-o", "--output", required=True)
    archive.set_defaults(func=_pack)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except BundleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
