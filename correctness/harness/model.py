from __future__ import annotations

import collections
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .contracts import CATALOG_NAMESPACES, SEMANTIC_CATALOG_NAMESPACES

GRAPH_PATH = Path(__file__).resolve().parent.parent / "correctness.yaml"

def _repository_root_for_graph_path(path: Path) -> Path:
    """Resolve repository root for canonical and isolated-test graph layouts."""
    return path.parent.parent if path.parent.name == "correctness" else path.parent

def _plain_data(value: Any) -> Any:
    """Convert round-trip YAML containers into plain Python containers."""
    if isinstance(value, dict):
        return {str(k): _plain_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_data(v) for v in value]
    return value


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate YAML mapping keys."""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: str  # claim | assumption
    data: dict[str, Any]

    @property
    def dependencies(self) -> list[str]:
        value = self.data.get("depends_on", [])
        return list(value) if isinstance(value, list) else []


class Graph:
    def __init__(self, doc: dict[str, Any], *, repository_root: Path | None = None):
        self.doc = doc
        self.repository_root = repository_root
        assumptions = doc.get("assumptions", {})
        claims = doc.get("claims", {})
        roots = doc.get("roots", [])
        self.assumptions: dict[str, dict[str, Any]] = assumptions if isinstance(assumptions, dict) else {}
        self.claims: dict[str, dict[str, Any]] = claims if isinstance(claims, dict) else {}
        self.roots: list[str] = roots if isinstance(roots, list) else []
        self.nodes: dict[str, Node] = {}
        for node_id, data in self.assumptions.items():
            self.nodes[node_id] = Node(node_id, "assumption", data)
        for node_id, data in self.claims.items():
            self.nodes[node_id] = Node(node_id, "claim", data)

    @classmethod
    def load(cls, path: Path = GRAPH_PATH) -> "Graph":
        try:
            with path.open("r", encoding="utf-8") as f:
                doc = yaml.load(f, Loader=UniqueKeyLoader)
        except FileNotFoundError as exc:
            raise SystemExit(f"Graph file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise SystemExit(f"Invalid YAML in {path}:\n{exc}") from exc
        if not isinstance(doc, dict):
            raise SystemExit(f"Top-level YAML document must be a mapping: {path}")
        return cls(doc, repository_root=_repository_root_for_graph_path(path))

    def require_node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise SystemExit(f"Unknown node: {node_id}") from exc

    def reverse_dependencies(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep in rev:
                    rev[dep].append(node.node_id)
        for values in rev.values():
            values.sort()
        return rev

    def dependency_closure(self, target: str, stop_at: set[str] | None = None) -> set[str]:
        self.require_node(target)
        stop_at = stop_at or set()
        closure: set[str] = set()
        stack = [target]
        while stack:
            node_id = stack.pop()
            if node_id in closure:
                continue
            closure.add(node_id)
            if node_id in stop_at and node_id != target:
                continue
            node = self.require_node(node_id)
            stack.extend(reversed(node.dependencies))
        return closure

    def dependency_depths(self, target: str, stop_at: set[str] | None = None) -> dict[str, int]:
        self.require_node(target)
        stop_at = stop_at or set()
        depths = {target: 0}
        queue = collections.deque([target])
        while queue:
            node_id = queue.popleft()
            if node_id in stop_at and node_id != target:
                continue
            for dep in self.require_node(node_id).dependencies:
                if dep not in self.nodes:
                    continue
                candidate = depths[node_id] + 1
                if dep not in depths or candidate < depths[dep]:
                    depths[dep] = candidate
                    queue.append(dep)
        return depths

    def affected_by(self, changed: Iterable[str]) -> tuple[dict[str, int], dict[str, str | None]]:
        changed = list(changed)
        for node_id in changed:
            self.require_node(node_id)
        rev = self.reverse_dependencies()
        distance: dict[str, int] = {}
        via: dict[str, str | None] = {}
        queue: collections.deque[str] = collections.deque()
        for node_id in changed:
            distance[node_id] = 0
            via[node_id] = None
            queue.append(node_id)
        while queue:
            node_id = queue.popleft()
            for dependent in rev[node_id]:
                candidate = distance[node_id] + 1
                if dependent not in distance or candidate < distance[dependent]:
                    distance[dependent] = candidate
                    via[dependent] = node_id
                    queue.append(dependent)
        return distance, via


def _as_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _as_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _catalog(graph: Graph) -> dict[str, Any]:
    value = graph.doc.get("catalog", {})
    return value if isinstance(value, dict) else {}


def _catalog_namespace(graph: Graph, namespace: str) -> dict[str, Any]:
    value = _catalog(graph).get(namespace, {})
    return value if isinstance(value, dict) else {}


def _catalog_entry_payload(entry: Any) -> Any:
    if isinstance(entry, dict):
        return _plain_data(entry)
    return entry


def _semantic_catalog_refs_for_node(graph: Graph, node: Node) -> dict[str, list[str]]:
    """Resolve semantic catalog entries actually used by one proposition.

    Explicit mechanism refs are authoritative. Term/state refs are inferred only from exact
    catalog identifiers appearing in the proposition/formal intent, which keeps underscore-style
    protocol vocabulary both defined and locally injectable without NLP guessing.
    """
    texts = [str(node.data.get("statement", "")), str(node.data.get("formal_intent", ""))]
    combined = "\n".join(texts)
    refs: dict[str, list[str]] = {ns: [] for ns in SEMANTIC_CATALOG_NAMESPACES}
    for namespace in ("terms", "state"):
        for key in _catalog_namespace(graph, namespace):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", combined, re.IGNORECASE):
                refs[namespace].append(key)
    mechanisms = node.data.get("mechanisms", [])
    if isinstance(mechanisms, list):
        refs["mechanisms"] = [x for x in mechanisms if isinstance(x, str)]
    semantic_contracts = node.data.get("semantic_contracts", [])
    if isinstance(semantic_contracts, list):
        refs["semantic_contracts"] = [x for x in semantic_contracts if isinstance(x, str)]
    for namespace in refs:
        refs[namespace] = sorted(set(refs[namespace]))
    return refs


def _slice_catalog_context(graph: Graph, node_ids: Iterable[str]) -> dict[str, Any]:
    refs: dict[str, set[str]] = {ns: set() for ns in SEMANTIC_CATALOG_NAMESPACES}
    for node_id in node_ids:
        if node_id not in graph.nodes:
            continue
        local = _semantic_catalog_refs_for_node(graph, graph.nodes[node_id])
        for namespace, keys in local.items():
            refs[namespace].update(keys)
    result: dict[str, Any] = {}
    for namespace in SEMANTIC_CATALOG_NAMESPACES:
        entries = _catalog_namespace(graph, namespace)
        selected = {key: entries[key] for key in sorted(refs[namespace]) if key in entries}
        if selected:
            result[namespace] = selected
    # The failure model is a global proof boundary, so every local audit receives it in full.
    result["failure_events"] = _catalog_namespace(graph, "failure_events")
    return result


