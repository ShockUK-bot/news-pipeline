"""v0.31.2 (2026-11-10): the gate's main news branch defines item_source,
cclass and lane before it uses them. Checked per function with the AST, the
way the v0.27.0 / v0.29.0 substring tests should have."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _assigned_names(fn: ast.AsyncFunctionDef) -> set[str]:
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _used_names(fn: ast.AsyncFunctionDef) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def test_gate_handle_defines_what_it_uses():
    tree = ast.parse((ROOT / "src" / "c3_gate" / "service.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Service"))
    fns = {n.name: n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("handle", "_handle_eh_shadow"):
        fn = fns[name]
        used, assigned = _used_names(fn), _assigned_names(fn)
        params = {a.arg for a in fn.args.args}
        for var in ("lane", "item_source", "cclass"):
            if var in used:
                assert var in assigned or var in params, f"{name} uses {var} without assigning it"
    # and the main branch really does use them (the lanes depend on it)
    h = fns["handle"]
    assert {"lane", "item_source", "cclass"} <= _used_names(h)
    assert {"lane", "item_source", "cclass"} <= _assigned_names(h)
