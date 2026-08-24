"""Every HTTP entrypoint must prove row-level security can actually engage.

RLS is silently inert for a superuser or a BYPASSRLS role: the policies exist, the
tests pass, and the database still hands every tenant everyone else's rows.
`check_rls_posture()` exists to catch that at boot, and `RGNR8_REQUIRE_RLS=1` turns
it into a refusal to start.

`deploy/entry.py` had that gate. `deploy/operator_entry.py` -- the staff console,
which reads and writes ACROSS every client's books and is therefore the surface
that can leak the most -- did not, because nothing checked. Both are `pragma: no
cover` (they build a live app at import), so no runtime test would ever have
noticed, and a third entrypoint added tomorrow would inherit the same blind spot.

So this asserts on the SOURCE, and DISCOVERS the entrypoints rather than listing
them: any module under deploy/ that exposes a module-level `application` and opens
the production database must run the posture check and exit on a fatal verdict.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"


def _calls(tree: ast.AST) -> set[str]:
    """Every plain function name called anywhere in the module."""
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _exposes_application(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "application" for t in node.targets)
        for node in ast.walk(tree)
    )


def _http_entrypoints() -> list[tuple[str, ast.AST]]:
    found = []
    for path in sorted(DEPLOY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _exposes_application(tree) and "open_connection" in _calls(tree):
            found.append((path.name, tree))
    return found


def test_discovery_finds_the_known_entrypoints() -> None:
    """Guard the guard: if this stops finding files, every assertion below is vacuous."""
    names = {name for name, _ in _http_entrypoints()}
    assert {"entry.py", "operator_entry.py"} <= names, names


@pytest.mark.parametrize("name,tree", _http_entrypoints(), ids=lambda v: v if isinstance(v, str) else "")
def test_entrypoint_checks_rls_posture(name: str, tree: ast.AST) -> None:
    assert "check_rls_posture" in _calls(tree), (
        f"deploy/{name} opens the production database and serves HTTP but never calls "
        "check_rls_posture() -- on a superuser or BYPASSRLS role its RLS policies are "
        "inert and it would boot anyway."
    )


@pytest.mark.parametrize("name,tree", _http_entrypoints(), ids=lambda v: v if isinstance(v, str) else "")
def test_entrypoint_refuses_to_boot_on_a_fatal_verdict(name: str, tree: ast.AST) -> None:
    raises_exit = any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SystemExit"
        for node in ast.walk(tree)
    )
    assert raises_exit, (
        f"deploy/{name} calls check_rls_posture() but never raises SystemExit -- a "
        "fatal verdict would print a warning and serve traffic anyway, which is the "
        "whole failure mode RGNR8_REQUIRE_RLS exists to prevent."
    )
