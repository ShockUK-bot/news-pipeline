"""Shared YAML config loading. PyYAML if installed; otherwise a tiny built-in
parser for the strict subset our config files use (nested maps by 2-space
indent, lists of scalars or single-level maps, scalar types, # comments).
Moved here from c1_ingestion.service in Phase 2 so all services share it.
"""
from __future__ import annotations

import os


def load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        return _tiny_yaml(path)


def config_path(filename: str) -> str:
    """Resolve a config file relative to the repo's config/ dir, overridable
    per-file via env: sources.yaml -> SOURCES_CONFIG, a1.yaml -> A1_CONFIG."""
    env_key = filename.split(".")[0].upper() + "_CONFIG"
    if os.environ.get(env_key):
        return os.environ[env_key]
    return os.path.join(os.path.dirname(__file__), "..", "..", "config", filename)


class _LazyNode(dict):
    """Starts as dict; converts semantics to list if children are list items."""
    def __init__(self):
        super().__init__()
        self._list: list | None = None

    def append(self, x):
        if self._list is None:
            self._list = []
        self._list.append(x)

    def resolved(self):
        return self._list if self._list is not None else dict(self)


def _tiny_yaml(path: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(-1, root)]
    with open(path) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip())
            content = stripped.strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if content.startswith("- "):
                item_src = content[2:].strip()
                if not hasattr(parent, "append"):
                    raise ValueError(f"list item outside list: {raw_line!r}")
                if ":" in item_src:
                    k, v = item_src.split(":", 1)
                    obj = {k.strip(): _scalar(v.strip())}
                    parent.append(obj)
                    stack.append((indent, obj))
                else:
                    parent.append(_scalar(item_src))
            elif content.endswith(":"):
                key = _scalar(content[:-1].strip())
                node = _LazyNode()
                parent[key] = node
                stack.append((indent, node))
            else:
                k, v = content.split(":", 1)
                parent[_scalar(k.strip())] = _inline_or_scalar(v.strip())
    return _resolve(root)


def _inline_or_scalar(v: str):
    """PyYAML-consistent handling of inline flow maps: 'low: {2: 1, 3: 1}'
    yields {2: 1, 3: 1} with typed (int) keys. One level deep — nested flow
    collections belong in real YAML, install PyYAML for those."""
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for pair in inner.split(","):
            pk, pv = pair.split(":", 1)
            out[_scalar(pk.strip())] = _scalar(pv.strip())
        return out
    return _scalar(v)


def _resolve(node):
    if isinstance(node, _LazyNode):
        node = node.resolved()
    if isinstance(node, dict):
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    return node


def _scalar(s: str):
    s = s.strip().strip('"').strip("'")
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s

