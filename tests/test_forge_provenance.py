from __future__ import annotations

import hashlib
import json
import token
import tokenize
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_FORGE = REPO_ROOT / "src" / "millforge" / "_forge"
REF_FORGE = REPO_ROOT / "ref-forge"
PROVENANCE = PRIVATE_FORGE / "PROVENANCE.json"

EXPECTED_ATTRIBUTION_SHA256 = {
    "LICENSE": "6e47a0c234be433f58471c7e6ba6e24715798e54c94031a553f5f01ee72a5268",
    "PROVENANCE.json": "5a706684673d8fcc0a094149c2dfff7e5e3bdd57501e03b1fdceb25ba4747e9d",
    "UPDATE_POLICY.md": "d095284118b7f6b6c2bf266061c19f0e87877d4e8b88114ce982f6c2a75be8c5",
}
EXPECTED_UPSTREAM_SOURCE_SHA256 = {
    "src/forge/errors.py": "9a0b5409b0f81588cbd4e410f19781db42472eb4e2e7df06dde6ab015f272d40",
    "src/forge/clients/base.py": "db4bdf33da769acde686093e42bb62e3f586910ba23eb6632698b09e85f40de5",
    "src/forge/context/manager.py": "934173f7912eda253cad788528cc0409ce851da825ca1753b9e9e65c5e7cd15d",
    "src/forge/context/strategies.py": "c9b618ae7ac3f66ac34febf24b943ed441645f1b703cac51cbe80f569576f526",
    "src/forge/core/workflow.py": "e5baad6aac152a2625a9bdccf164d32e476ce5a02b9e47be2e4640f6c1ad4862",
    "src/forge/core/messages.py": "41ce4e4cd9d1776ed13491158ca64bb6e6dcb633faf686a8ed6e002602ae5a3e",
    "src/forge/core/steps.py": "aeeef79fa1d78770203346c163786715fc5f08b7e223794ce70031d0656c9d4c",
    "src/forge/core/inference.py": "d719a32f4bcf63e60a0c808d596e30999668d25b529339f7aad523b45d21b6d4",
    "src/forge/core/runner.py": "cbf0f751f0a797646c5e4e325f473ccf348cafcc21f804c40ccf5a22b6a4bafc",
    "src/forge/guardrails/error_tracker.py": "989658eb8fe14c71eaa131ff5ad1767b35cdc1ce0d0315924e7665165f6cfe8a",
    "src/forge/guardrails/guardrails.py": "4ed4beae5988abe9b9c146e898bfb6cecc9c5133a3075747f91f20da23407823",
    "src/forge/guardrails/nudge.py": "35a1ff122f9c8b1d12c51051f7453af4abfb0acc605e46b3bfbbf3fc62863c1c",
    "src/forge/guardrails/response_validator.py": "215d6310e7f1b5194ea39b54d5ea6ac25d137b8169ddc600673e60d8e6de1363",
    "src/forge/guardrails/step_enforcer.py": "5a0b50f0f90ba20708fcae6ff13908175d2cb15540e415e13f0dc16b583883c3",
    "src/forge/prompts/nudges.py": "ccd6a53de5bf3b5577dc19305a15a5041881c04cca364dc7bca4153e64414cef",
    "src/forge/prompts/templates.py": "aff74a8edcf8afc032e9fb1f60e9f47e8a97596363fe50a2861daafb9608e84d",
}


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


def test_vendored_forge_manifest_preserves_pinned_upstream_source_hashes() -> None:
    copied_files: list[dict[str, str]] = _manifest()["copied_files"]
    recorded_hashes = {
        entry["upstream_path"]: entry["sha256_before_namespace_edits"]
        for entry in copied_files
    }

    assert recorded_hashes == EXPECTED_UPSTREAM_SOURCE_SHA256
    for entry in copied_files:
        destination = REPO_ROOT / str(entry["private_destination"])
        assert destination.is_file()

        if REF_FORGE.exists():
            upstream_path = REF_FORGE / str(entry["upstream_path"])
            assert upstream_path.is_file()
            digest = hashlib.sha256(upstream_path.read_bytes()).hexdigest()
            assert digest == entry["sha256_before_namespace_edits"]


def test_vendored_forge_attribution_bytes_match_pinned_package_records() -> None:
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            PRIVATE_FORGE / "LICENSE",
            PRIVATE_FORGE / "PROVENANCE.json",
            PRIVATE_FORGE / "UPDATE_POLICY.md",
        )
    } == EXPECTED_ATTRIBUTION_SHA256


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
