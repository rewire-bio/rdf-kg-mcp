"""Knowledge-graph bundles: locate, download, verify and pack them.

A bundle is a directory with a ``manifest.json`` and the RDF files it lists. It can be
given as a local directory, a local or remote ``.tar.gz``, or the URL of a remote
``manifest.json`` whose files sit next to it. See docs/bundle-format.md.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema
import pyoxigraph as ox

MANIFEST = "manifest.json"
FORMATS: dict[str, ox.RdfFormat] = {
    ".nq": ox.RdfFormat.N_QUADS,
    ".trig": ox.RdfFormat.TRIG,
    ".nt": ox.RdfFormat.N_TRIPLES,
    ".ttl": ox.RdfFormat.TURTLE,
    ".rdf": ox.RdfFormat.RDF_XML,
    ".owl": ox.RdfFormat.RDF_XML,
}
QUAD_SUFFIXES = {".nq", ".trig"}
DEFAULT_LABEL_PREDICATES = [
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://www.w3.org/2004/02/skos/core#altLabel",
    "https://schema.org/name",
    "http://purl.org/dc/terms/title",
]
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")


class BundleError(Exception):
    """The bundle cannot be found, downloaded or trusted."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    graph: str | None
    quads: int | None
    triples: int | None

    @property
    def format(self) -> ox.RdfFormat:
        return FORMATS[PurePosixPath(self.path).suffix]

    @property
    def has_quads(self) -> bool:
        return PurePosixPath(self.path).suffix in QUAD_SUFFIXES


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict[str, Any]
    manifest_sha256: str

    @property
    def dataset(self) -> str:
        return self.manifest["dataset"]

    @property
    def version(self) -> str | None:
        return self.manifest.get("version") or self.manifest.get("release_id")

    @property
    def files(self) -> list[FileEntry]:
        return [
            FileEntry(path, f["sha256"], f.get("graph"), f.get("quads"), f.get("triples"))
            for path, f in sorted(self.manifest["files"].items())
        ]

    @property
    def graphs(self) -> dict[str, str]:
        return dict(self.manifest.get("graphs") or {})

    @property
    def prefixes(self) -> dict[str, str]:
        return dict(self.manifest.get("prefixes") or {})

    @property
    def label_predicates(self) -> list[str]:
        given = self.manifest.get("label_predicates") or DEFAULT_LABEL_PREDICATES
        return [expand_curie(p, self.prefixes) for p in given]

    @property
    def example_queries(self) -> list[dict[str, str]]:
        return list(self.manifest.get("example_queries") or [])

    def role_of(self, graph: str | None) -> str:
        """Role of a named graph ("asserted", "inferred", ...); "default" for the default graph."""
        if graph is None:
            return "default"
        for role, iri in self.graphs.items():
            if iri == graph:
                return role
        return "other"


def expand_curie(term: str, prefixes: dict[str, str]) -> str:
    term = term.strip()
    if term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    prefix, sep, local = term.partition(":")
    if sep and prefix in prefixes and not local.startswith("//"):
        return prefixes[prefix] + local
    return term


@cache
def manifest_schema() -> dict[str, Any]:
    return json.loads(resources.files("rdf_kg_mcp").joinpath("manifest.schema.json").read_text("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def count_statements(path: Path, fmt: ox.RdfFormat) -> int:
    return sum(1 for _ in ox.parse(path=str(path), format=fmt))


def cache_root() -> Path:
    configured = os.environ.get("RDF_KG_MCP_CACHE")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "rdf-kg-mcp"


def is_url(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme in ("http", "https")


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "rdf-kg-mcp"})
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        raise BundleError(f"Could not download {url}: {error}") from error
    tmp.replace(dest)


def _extract(archive: Path, dest: Path) -> None:
    """Extract a bundle archive, refusing links and paths outside the destination."""
    tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=dest.parent))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                    raise BundleError(f"Unsafe entry in bundle archive: {member.name}")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:  # Python < 3.10.12 / 3.11.4: members were checked above
                tar.extractall(tmp)
        entries = list(tmp.iterdir())
        # Accept archives that wrap the bundle in a single top-level directory.
        inner = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
        if not (inner / MANIFEST).is_file():
            raise BundleError(f"No {MANIFEST} in {archive}")
        if dest.exists():
            shutil.rmtree(dest)
        inner.replace(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def locate(source: str, cache_dir: Path | None = None) -> Path:
    """Return a local directory holding the bundle, downloading or extracting it if needed."""
    cache_dir = cache_dir or cache_root()
    if is_url(source):
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(source.encode()).hexdigest()[:16]
        if source.endswith(ARCHIVE_SUFFIXES):
            archive = cache_dir / f"{key}.tar.gz"
            _download(source, archive)
            dest = cache_dir / f"bundle-{sha256_file(archive)[:16]}"
            if not (dest / MANIFEST).is_file():
                _extract(archive, dest)
            archive.unlink()
            return dest
        manifest_url = source if source.endswith(".json") else source.rstrip("/") + "/" + MANIFEST
        staging = cache_dir / f"remote-{hashlib.sha256(manifest_url.encode()).hexdigest()[:16]}"
        staging.mkdir(parents=True, exist_ok=True)
        _download(manifest_url, staging / MANIFEST)
        manifest = _read_manifest(staging)
        for path, entry in manifest["files"].items():
            target = staging / path
            if target.is_file() and sha256_file(target) == entry["sha256"]:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _download(urllib.parse.urljoin(manifest_url, path), target)
        return staging
    path = Path(source).expanduser()
    if path.is_dir():
        return path
    if path.is_file() and path.name == MANIFEST:
        return path.parent
    if path.is_file() and path.name.endswith(ARCHIVE_SUFFIXES):
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / f"bundle-{sha256_file(path)[:16]}"
        if not (dest / MANIFEST).is_file():
            _extract(path, dest)
        return dest
    raise BundleError(f"Not a bundle directory, archive, manifest or URL: {source}")


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST
    if not path.is_file():
        raise BundleError(f"No {MANIFEST} in {root}")
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as error:
        raise BundleError(f"{path} is not valid JSON: {error}") from error
    errors = sorted(jsonschema.Draft202012Validator(manifest_schema()).iter_errors(manifest), key=str)
    if errors:
        details = "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors)
        raise BundleError(f"Invalid manifest: {details}")
    return manifest


def verify(root: Path, *, count: bool = False) -> list[str]:
    """Check the manifest and every file's checksum. With count, also parse each file and
    compare its statement count. Returns a list of problems; empty means the bundle is valid."""
    try:
        manifest = _read_manifest(root)
    except BundleError as error:
        return [str(error)]
    problems: list[str] = []
    resolved_root = root.resolve()
    for path, entry in sorted(manifest["files"].items()):
        file = (root / path).resolve()
        if resolved_root not in file.parents:
            problems.append(f"{path}: outside the bundle")
            continue
        if not file.is_file():
            problems.append(f"{path}: missing")
            continue
        actual = sha256_file(file)
        if actual != entry["sha256"]:
            problems.append(f"{path}: sha256 is {actual}, manifest says {entry['sha256']}")
            continue
        if PurePosixPath(path).suffix not in QUAD_SUFFIXES and "quads" in entry:
            problems.append(f"{path}: a triples file cannot declare quads")
        expected = entry.get("quads", entry.get("triples"))
        if count and expected is not None:
            try:
                found = count_statements(file, FORMATS[PurePosixPath(path).suffix])
            except (SyntaxError, OSError) as error:
                problems.append(f"{path}: does not parse: {error}")
                continue
            if found != expected:
                problems.append(f"{path}: {found} statements, manifest says {expected}")
    return problems


def open_bundle(source: str, cache_dir: Path | None = None) -> Bundle:
    """Locate a bundle and verify every file against the manifest before it is used."""
    root = locate(source, cache_dir)
    problems = verify(root)
    if problems:
        raise BundleError("Bundle failed verification:\n  " + "\n  ".join(problems))
    raw = (root / MANIFEST).read_bytes()
    return Bundle(root=root, manifest=json.loads(raw), manifest_sha256=hashlib.sha256(raw).hexdigest())


def write_manifest(
    root: Path, manifest: dict[str, Any], files: Iterable[str] | None = None
) -> dict[str, Any]:
    """Fill in sha256 and statement counts for the bundle's RDF files and write manifest.json.

    Files already in the manifest keep their other fields (such as graph). With files=None,
    every RDF file under root is listed."""
    entries: dict[str, Any] = dict(manifest.get("files") or {})
    paths = (
        sorted(files)
        if files is not None
        else sorted(
            p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.suffix in FORMATS
        )
    )
    for path in paths:
        file = root / path
        suffix = PurePosixPath(path).suffix
        entry = {k: v for k, v in entries.get(path, {}).items() if k not in ("sha256", "quads", "triples")}
        entry["sha256"] = sha256_file(file)
        entry["quads" if suffix in QUAD_SUFFIXES else "triples"] = count_statements(file, FORMATS[suffix])
        entries[path] = dict(sorted(entry.items()))
    result = {"format_version": "1.0", **manifest, "files": dict(sorted(entries.items()))}
    (root / MANIFEST).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return result


def pack(root: Path, output: Path) -> str:
    """Write a byte-reproducible .tar.gz of the manifest and the files it lists. Returns its sha256."""
    manifest = _read_manifest(root)
    names = [MANIFEST, *sorted(manifest["files"])]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in names:
            data = (root / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        gz.write(buffer.getvalue())
    return sha256_file(output)
