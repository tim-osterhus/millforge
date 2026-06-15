"""Dependency and no-I/O audits for compiler front-end modules."""

from __future__ import annotations

import ast
from pathlib import Path


COMPILER_ROOT = Path("src/millforge/compiler")
LOWERING_MODULE = "src/millforge/compiler/lowering.py"
OUTPUT_MODULE = "src/millforge/compiler/output.py"
SERVICE_MODULE = "src/millforge/compiler/service.py"
COMPILER_IO_BOUNDARY_MODULES = {OUTPUT_MODULE}
COMPILED_PLAN_CONSUMER_MODULES = {LOWERING_MODULE, OUTPUT_MODULE, SERVICE_MODULE}
FORBIDDEN_IMPORT_PREFIXES = (
    "millforge._forge",
    "millforge.runtime",
    "millforge.catalog",
    "requests",
    "urllib",
    "http",
    "subprocess",
)
FORBIDDEN_COMPILER_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
}
FORBIDDEN_COMPILER_ATTRS = {
    ("os", "environ"),
    ("Path", "open"),
    ("Path", "read_text"),
    ("Path", "read_bytes"),
    ("Path", "write_text"),
    ("Path", "write_bytes"),
    ("Path", "mkdir"),
    ("Path", "glob"),
    ("Path", "rglob"),
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
        if path.as_posix() in COMPILED_PLAN_CONSUMER_MODULES:
            continue
        imports[path.as_posix()] = _imported_module_names(_module_tree(path))

    flattened = {module for module_names in imports.values() for module in module_names}
    assert "millforge.compiled_plan" not in flattened
    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in flattened
        for forbidden in FORBIDDEN_IMPORT_PREFIXES
    )


def test_compiler_modules_do_not_perform_io_or_runtime_invocation() -> None:
    for path in COMPILER_ROOT.glob("*.py"):
        if path.as_posix() in COMPILER_IO_BOUNDARY_MODULES:
            continue
        tree = _module_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    assert target.id not in FORBIDDEN_COMPILER_CALLS, path.as_posix()
                elif isinstance(target, ast.Attribute):
                    owner = target.value
                    if isinstance(owner, ast.Name):
                        assert (
                            owner.id,
                            target.attr,
                        ) not in FORBIDDEN_COMPILER_ATTRS, path.as_posix()
