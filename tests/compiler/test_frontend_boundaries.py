"""Dependency and no-I/O audits for compiler front-end modules."""

from __future__ import annotations

import ast
from pathlib import Path


COMPILER_ROOT = Path("src/millforge/compiler")
FORBIDDEN_IMPORT_PREFIXES = (
    "millforge._forge",
    "millforge.runtime",
    "millforge.catalog",
    "requests",
    "urllib",
    "http",
    "subprocess",
)
FORBIDDEN_VALIDATOR_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
}
FORBIDDEN_VALIDATOR_ATTRS = {
    ("os", "environ"),
    ("Path", "open"),
    ("Path", "read_text"),
    ("Path", "read_bytes"),
}


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_compiler_front_end_does_not_import_deferred_runtime_boundaries() -> None:
    imports: dict[str, set[str]] = {}
    for path in COMPILER_ROOT.glob("*.py"):
        imports[path.as_posix()] = _imported_module_names(_module_tree(path))

    flattened = {module for module_names in imports.values() for module in module_names}
    assert "millforge.compiled_plan" not in flattened
    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in flattened
        for forbidden in FORBIDDEN_IMPORT_PREFIXES
    )


def test_compiler_validators_do_not_perform_io_or_runtime_invocation() -> None:
    tree = _module_tree(COMPILER_ROOT / "validators.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                assert target.id not in FORBIDDEN_VALIDATOR_CALLS
            elif isinstance(target, ast.Attribute):
                owner = target.value
                if isinstance(owner, ast.Name):
                    assert (owner.id, target.attr) not in FORBIDDEN_VALIDATOR_ATTRS
