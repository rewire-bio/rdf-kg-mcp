"""Serve a versioned, pre-reasoned RDF knowledge-graph bundle read-only over MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rdf-kg-mcp")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"
