from __future__ import annotations

import hashlib
import json
import token
import tokenize
from typing import Any
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_FORGE = REPO_ROOT / "src" / "millforge" / "_forge"
REF_FORGE = REPO_ROOT / "ref-forge"
PROVENANCE = PRIVATE_FORGE / "PROVENANCE.json"


def _manifest() -> dict[str, Any]:
    return json.loads(PROVENANCE.read_text(encoding="utf-8"))


def test_vendored_forge_manifest_records_fixed_upstream_identity() -> None:
    manifest = _manifest()

    assert manifest["upstream"] == {
        "name": "forge-guardrails",
        "url": "https://github.com/antoinezambelli/forge",
        "commit": "bd99f4df0a7aab2fd4db2e6dae7f810a32617d76",
        "package_version": "0.7.4",
        "license": "MIT",
        "copyright": "Copyright (c) 2025-2026 Antoine Zambelli",
    }
    assert manifest["import_date"] == "2026-06-12"
    assert manifest["source_snapshot"] == "ref-forge/"


def test_vendored_forge_manifest_hashes_match_ref_snapshot_when_present() -> None:
    if not REF_FORGE.exists():
        pytest.skip(
            "ref-forge/ is absent; provenance hash check requires local snapshot"
        )

    copied_files: list[dict[str, str]] = _manifest()["copied_files"]
    for entry in copied_files:
        upstream_path = REF_FORGE / str(entry["upstream_path"])
        destination = REPO_ROOT / str(entry["private_destination"])

        assert upstream_path.is_file()
        assert destination.is_file()
        digest = hashlib.sha256(upstream_path.read_bytes()).hexdigest()
        assert digest == entry["sha256_before_namespace_edits"]


def test_vendored_forge_subset_excludes_transport_provider_and_hardware_modules() -> (
    None
):
    forbidden_paths = {
        PRIVATE_FORGE / "clients" / "anthropic.py",
        PRIVATE_FORGE / "clients" / "llamafile.py",
        PRIVATE_FORGE / "clients" / "ollama.py",
        PRIVATE_FORGE / "clients" / "openai_compat.py",
        PRIVATE_FORGE / "clients" / "sampling_defaults.py",
        PRIVATE_FORGE / "clients" / "vllm.py",
        PRIVATE_FORGE / "context" / "hardware.py",
        PRIVATE_FORGE / "core" / "slot_worker.py",
        PRIVATE_FORGE / "server.py",
    }

    for path in forbidden_paths:
        assert not path.exists()

    for directory_name in ("proxy", "tools"):
        assert not (PRIVATE_FORGE / directory_name).exists()


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    tokens = list(tokenize.generate_tokens(path.open(encoding="utf-8").readline))
    index = 0
    while index < len(tokens):
        current = tokens[index]
        if current.type == token.NAME and current.string == "import":
            index += 1
            while index < len(tokens) and tokens[index].type != token.NEWLINE:
                if tokens[index].type == token.NAME:
                    modules.add(tokens[index].string)
                    while (
                        index + 1 < len(tokens)
                        and tokens[index + 1].type == token.OP
                        and tokens[index + 1].string == "."
                    ):
                        index += 2
                    index += 1
                    continue
                index += 1
            continue
        if current.type == token.NAME and current.string == "from":
            index += 1
            while index < len(tokens) and tokens[index].type != token.NEWLINE:
                if tokens[index].type == token.NAME:
                    modules.add(tokens[index].string)
                    break
                index += 1
        index += 1
    return modules


def test_vendored_forge_python_imports_stay_private_and_transport_free() -> None:
    forbidden_modules = {
        "forge",
        "httpx",
        "anthropic",
        "openai",
        "ollama",
        "llamafile",
        "vllm",
    }

    for path in PRIVATE_FORGE.rglob("*.py"):
        for imported in _imported_modules(path):
            assert imported not in forbidden_modules, (path, imported)


def test_vendored_forge_notices_are_packaged() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    required = {
        "src/millforge/_forge/LICENSE",
        "src/millforge/_forge/PROVENANCE.json",
        "src/millforge/_forge/UPDATE_POLICY.md",
    }
    for path in required:
        assert f'"{path}" = "millforge/_forge/{Path(path).name}"' in pyproject
        assert f'"{path}" = "{path}"' in pyproject


def test_private_behavior_patches_are_recorded_in_manifest() -> None:
    patches: list[dict[str, str]] = _manifest()["private_behavior_patches"]

    expected_destinations = {
        "src/millforge/_forge/core/runner.py",
        "src/millforge/_forge/errors.py",
        "src/millforge/_forge/core/workflow.py",
        "src/millforge/_forge/core/__init__.py",
    }
    assert expected_destinations <= {
        str(entry["private_destination"]) for entry in patches
    }
    for entry in patches:
        assert entry["edit_category"] in {
            "private_behavior_patch",
            "private_subset_safety_patch",
        }
        assert entry["patch_reason"]
