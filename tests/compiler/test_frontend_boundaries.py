"""Dependency and no-I/O audits for compiler front-end modules."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import millforge
import millforge.compiler as compiler


COMPILER_ROOT = Path("src/millforge/compiler")
RUNTIME_MODULE = Path("src/millforge/runtime.py")
FORGE_ROOT = Path("src/millforge/_forge")
PYPROJECT = Path("pyproject.toml")
LOWERING_MODULE = "src/millforge/compiler/lowering.py"
OUTPUT_MODULE = "src/millforge/compiler/output.py"
SERVICE_MODULE = "src/millforge/compiler/service.py"
COMPILER_IO_BOUNDARY_MODULES = {OUTPUT_MODULE}
COMPILED_PLAN_CONSUMER_MODULES = {LOWERING_MODULE, OUTPUT_MODULE, SERVICE_MODULE}
FORBIDDEN_IMPORT_PREFIXES = (
    "millforge._forge",
    "millforge.runtime",
    "millforge.catalog",
    "millforge.connectors",
    "millforge.providers",
    "millforge.registry",
    "millrace",
    "anthropic",
    "openai",
    "requests",
    "urllib",
    "http",
    "subprocess",
)
FORBIDDEN_RUNTIME_IMPORT_PREFIXES = (
    "millforge.compiler",
    "yaml",
)
PRIVATE_COMPILER_EXPORT_REASON_BY_NAME = {
    "ArtifactProducerEvidence": "03B/03D compiler artifact diagnostics tests inspect producer-evidence records.",
    "ArtifactValidationResult": "03B/03D compiler artifact diagnostics tests inspect validation results.",
    "CapabilityValidationResult": "03B/03D compiler capability diagnostics tests inspect validation results.",
    "GraphValidationResult": "03B/03D graph oracle tests inspect validation diagnostics and node reachability.",
    "ResolvedHarness": "03B/03C compiler lowering tests inspect immutable semantic IR before lowering.",
    "ResolvedNodeDescriptor": "03B graph/artifact tests inspect resolved node descriptors.",
    "ResolvedToolBinding": "03B/03C semantic and lowering tests inspect resolved tool bindings.",
    "ResolvedToolBindingRef": "03B/03C semantic and lowering tests inspect resolved tool binding references.",
    "SemanticCompileResult": "03B/03C semantic tests inspect internal phase handoff results.",
    "HarnessSourceParser": "03A/03D parser adversarial tests exercise the parser implementation directly.",
    "ParsedHarnessSource": "03A/03D parser tests assert parser result metadata.",
    "SourceDocument": "03A/03D parser and canonicalization tests construct source documents directly.",
    "compiler_identity": "03C lowering tests assert compiled-plan compiler identity stability.",
    "lower_resolved_harness": "03C lowering and 03D hash tests exercise the lowering seam directly.",
    "compiled_plan_output_path": "03C/03D output tests assert content-addressed output naming.",
    "diagnostics_output_path": "03C/03D output tests assert request-addressed diagnostics naming.",
    "persist_compile_outputs": "03C/03D output failure-injection tests exercise publication directly.",
    "request_identity_sha256": "03C/03D service and output tests assert diagnostics identity derivation.",
}
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


def test_runtime_and_forge_adapter_do_not_import_harness_source_or_yaml() -> None:
    imports: dict[str, set[str]] = {}
    paths = [RUNTIME_MODULE, *(FORGE_ROOT.rglob("*.py"))]
    for path in paths:
        imports[path.as_posix()] = _imported_module_names(_module_tree(path))

    for path_name, module_names in imports.items():
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for module in module_names
            for forbidden in FORBIDDEN_RUNTIME_IMPORT_PREFIXES
        ), path_name


def test_top_level_public_api_excludes_compiler_and_fixture_internals() -> None:
    exports = set(millforge.__all__)
    forbidden_exports = (
        "HarnessSourceParser",
        "ParsedHarnessSource",
        "SourceDocument",
        "ResolvedHarness",
        "ResolvedToolBinding",
        "GraphValidationResult",
        "lower_resolved_harness",
        "persist_compile_outputs",
        "compiled_plan_output_path",
        "diagnostics_output_path",
        "FakeModelClient",
        "FakeToolExecutor",
        "make_test_compiled_plan",
    )

    assert not exports.intersection(forbidden_exports)
    assert not any(name.startswith("_") for name in exports)
    assert "testing" not in exports


def test_private_compiler_exports_have_recorded_compatibility_reasons() -> None:
    compiler_exports = set(compiler.__all__)
    private_exports = set(PRIVATE_COMPILER_EXPORT_REASON_BY_NAME)

    assert private_exports <= compiler_exports
    assert all(PRIVATE_COMPILER_EXPORT_REASON_BY_NAME[name] for name in private_exports)


def test_package_build_policy_excludes_repository_runtime_and_fixture_trees() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert wheel["packages"] == ["src/millforge"]
    assert set(sdist["only-include"]) == {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "src/millforge",
    }

    force_included = set(wheel["force-include"]) | set(sdist["force-include"])
    assert "src/millforge/_forge/LICENSE" in force_included
    assert "src/millforge/_forge/PROVENANCE.json" in force_included
    assert "src/millforge/_forge/UPDATE_POLICY.md" in force_included
    assert "ref-forge" not in force_included
    assert "millrace-agents" not in force_included


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
