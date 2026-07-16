from __future__ import annotations

import json
from pathlib import Path

import millforge
import millforge.base as millforge_base
from millforge.compiler import (
    InMemoryHarnessCompileError,
    compile_harness_source_in_memory,
)


ROOT = Path(__file__).resolve().parents[1]

BASE_EXPORTS = (
    "MillforgeBaseOptions",
    "MillforgeBaseContextFile",
    "MillforgeBaseContextSnapshot",
    "MillforgeBasePromptSnapshot",
    "MillforgeBasePromptBudgetError",
    "MillforgeBaseMetadata",
    "MillforgeBaseComponents",
    "load_millforge_base_context",
    "build_millforge_base_system_prompt",
    "millforge_base_harness_source",
    "create_millforge_base_components",
)
PUBLIC_EXPORTS = BASE_EXPORTS + (
    "compile_harness_source_in_memory",
    "InMemoryHarnessCompileError",
)
PINNED_PROMPT_RECORDS = [
    {
        "path": "packages/coding-agent/src/core/system-prompt.ts",
        "sha256": "49cd7166a7f1eb8d088b7d8b8f2c38642de827ac2ff7a327ec4b7549b543d8d1",
        "classification": "adapted",
    },
    {
        "path": "packages/coding-agent/src/core/resource-loader.ts",
        "sha256": "9e339467f3c5997ec363ad96ff18e1850cff71e2ab37dfeca7b4e8782926a2ff",
        "classification": "adapted",
    },
    {
        "path": "packages/coding-agent/test/system-prompt.test.ts",
        "sha256": "6443dbc77ee1c39ca14c89652079e0907f47a4efae2bf9ce08323442983e2981",
        "classification": "test-derived",
    },
    {
        "path": "packages/coding-agent/test/resource-loader.test.ts",
        "sha256": "98b343646e389f87743ef548f9c23e329ccf1eb0bb6945a66016884ec515b45e",
        "classification": "test-derived",
    },
]


def _read_base_readme_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Millforge Base")
    end = readme.find("\n## ", start + 1)
    return readme[start:] if end == -1 else readme[start:end]


def test_base_public_exports_are_deliberate_and_free_of_private_forge_symbols() -> None:
    assert tuple(millforge_base.__all__) == BASE_EXPORTS
    assert (
        tuple(name for name in millforge.__all__ if name in PUBLIC_EXPORTS)
        == PUBLIC_EXPORTS
    )
    assert all(
        getattr(millforge, name) is getattr(millforge_base, name)
        for name in BASE_EXPORTS
    )
    assert (
        millforge.compile_harness_source_in_memory is compile_harness_source_in_memory
    )
    assert millforge.InMemoryHarnessCompileError is InMemoryHarnessCompileError
    assert not any(name.startswith("Forge") for name in millforge.__all__)
    assert "ForgeGuardrailBackend" not in millforge.__all__


def test_base_prompt_and_context_provenance_records_are_appended_exactly() -> None:
    provenance = json.loads(
        (ROOT / "src/millforge/tools/pi_compat/PROVENANCE.json").read_text(
            encoding="utf-8"
        )
    )

    assert provenance["pinned_paths"][-4:] == PINNED_PROMPT_RECORDS


def test_base_docs_state_the_compatible_unrestricted_surface_and_deferrals() -> None:
    section = _read_base_readme_section()

    assert "@earendil-works/pi-coding-agent` 0.79.6" in section
    assert (
        "A Python behavioral port of Pi 0.79.6's complete built-in coding tool pack, "
        "adapted to Millforge's compiler and runtime contracts."
    ) in section
    assert all(
        f"`{tool}`" in section
        for tool in ("read", "bash", "edit", "write", "grep", "find", "ls")
    )
    assert all(f"`{tool}`" in section for tool in ("submit", "block", "reject"))
    assert "unrestricted and unsandboxed" in section
    assert (
        "millforge-base runs with the permissions of the Millforge process. It can read, "
        "write, delete, execute commands, access the network, and access credentials "
        "available to that process. Use only in trusted environments."
    ) in section
    assert "Deliberate adaptations" in section
    assert "create_millforge_base_components" in section
    assert "ordinary harness DSL graph" in section
    assert "Millrace default selection and live efficacy evaluation remain" in section
