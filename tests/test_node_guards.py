"""Every test that runs Node must be guarded, so a machine without Node skips instead of erroring.

Regression for a lost `@needs_node` (Phase 6.5 audit D2, issue #20). It is a static check on the
test modules, so it runs the same with or without Node installed and never starts Node itself.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _is_guard(node):
    """`@needs_node`, or the inline `@pytest.mark.skipif(NODE is None, ...)` used by dev-wiki tests."""
    if isinstance(node, ast.Name):
        return node.id == "needs_node"
    return isinstance(node, ast.Call) and "NODE" in _names(node) and "skipif" in ast.dump(node)


def _module_guarded(tree):
    return any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in n.targets)
               and any(_is_guard(c) for c in ast.walk(n.value) if isinstance(c, (ast.Name, ast.Call)))
               for n in tree.body)


def unguarded_node_tests():
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        # helpers that reach NODE directly or through another helper
        reach = {name for name, fn in funcs.items() if "NODE" in _names(fn)}
        grew = True
        while grew:
            grew = False
            for name, fn in funcs.items():
                if name not in reach and _names(fn) & reach:
                    reach.add(name)
                    grew = True
        if _module_guarded(tree):
            continue
        for name, fn in funcs.items():
            if name.startswith("test_") and _names(fn) & (reach | {"NODE"}):
                if not any(_is_guard(d) for d in fn.decorator_list):
                    found.append(f"{path.name}::{name}")
    return found


def test_every_test_that_runs_node_has_a_node_guard():
    assert unguarded_node_tests() == []


def test_the_scan_sees_a_missing_guard(tmp_path, monkeypatch):
    """The check itself must be able to fail: an unguarded Node test is reported."""
    (tmp_path / "test_sample.py").write_text(
        "import shutil\nNODE = shutil.which('node')\n"
        "def run():\n    return NODE\n"
        "def test_uses_node_without_a_guard():\n    run()\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "TESTS", tmp_path)
    assert unguarded_node_tests() == ["test_sample.py::test_uses_node_without_a_guard"]
