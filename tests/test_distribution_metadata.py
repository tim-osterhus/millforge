from __future__ import annotations

import ast
import email.policy
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest

import millforge
from millforge import describe_millforge_base


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
VERSION_MODULE = ROOT / "src" / "millforge" / "_version.py"
PACKAGE_INIT = ROOT / "src" / "millforge" / "__init__.py"
INSTALLED_SMOKE = ROOT / "scripts" / "installed_package_smoke.py"
READINESS_REPORT = (
    ROOT / "tests" / "fixtures" / "default_runner_readiness_closure_report.md"
)
READINESS_BASELINE = "96d0e61514788d43635e390c39e14bb52a44387c"

PROJECT_LICENSE = "Apache-2.0"
PROJECT_LICENSE_CLASSIFIER = "License :: OSI Approved :: Apache Software License"
EXPECTED_PERSON = {"name": "Tim Osterhus", "email": "tim@millrace.ai"}
EXPECTED_URLS = {
    "Homepage": "https://github.com/tim-osterhus/millforge",
    "Repository": "https://github.com/tim-osterhus/millforge",
}
EXPECTED_DESCRIPTOR_SHA256 = (
    "dc67d572eb9c934e6acf0f073f27ff19eb3f3da6d46beab91a1cccce2640b981"
)
EXPECTED_SELECTED_OUTPUT_REQUIREMENTS_SHA256 = (
    "9b99f70aa6f8e99930fe00865da605001a0207592b0b44d98239ab999e8009b0"
)
EXPECTED_SELECTED_OUTPUT_SCHEMA_SHA256 = {
    "BLOCKED": "b7162fa77b72a2b6243eefede444a2dac2cecb9fa3676fd403fe149bf8a23559",
    "COMPLETE": "3247892e7214a387e48649af88525522c650614ef6fc9bb8d675a43f4899bb7a",
}
UPSTREAM_NOTICE_PATHS = (
    "millforge/_forge/LICENSE",
    "millforge/_forge/PROVENANCE.json",
    "millforge/_forge/UPDATE_POLICY.md",
    "millforge/tools/pi_compat/PI_LICENSE",
    "millforge/tools/pi_compat/PROVENANCE.json",
    "millforge/tools/pi_compat/UPDATE_POLICY.md",
)
FORBIDDEN_ARCHIVE_PARTS = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "credentials",
    "dist",
    "eval-evidence",
    "eval-results",
    "eval_evidence",
    "eval_results",
    "evidence",
    "ideas",
    "lab",
    "millrace-agents",
    "ref-forge",
    "reference",
    "runtime-state",
    "runtime_state",
    "secrets",
    "scripts",
    "tests",
}
SECRET_SHAPED_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
)


@dataclass(frozen=True)
class BuiltDistributions:
    wheel: Path
    sdist: Path
    wheel_metadata: Message
    sdist_metadata: Message
    wheel_names: tuple[str, ...]
    sdist_names: tuple[str, ...]


def _project_config() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _metadata_from_wheel(path: Path) -> tuple[Message, tuple[str, ...]]:
    with zipfile.ZipFile(path) as archive:
        names = tuple(archive.namelist())
        metadata_path = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=email.policy.default).parsebytes(
            archive.read(metadata_path)
        )
    return metadata, names


def _metadata_from_sdist(path: Path) -> tuple[Message, tuple[str, ...]]:
    with tarfile.open(path, "r:gz") as archive:
        members = tuple(archive.getmembers())
        metadata_member = next(
            member for member in members if member.name.endswith("/PKG-INFO")
        )
        extracted = archive.extractfile(metadata_member)
        if extracted is None:
            raise AssertionError("sdist PKG-INFO is not readable")
        metadata = BytesParser(policy=email.policy.default).parsebytes(extracted.read())
        names = tuple("/".join(Path(member.name).parts[1:]) for member in members)
    return metadata, names


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> BuiltDistributions:
    output = tmp_path_factory.mktemp("distribution-metadata")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wheel = next(output.glob("millforge-*.whl"))
    sdist = next(output.glob("millforge-*.tar.gz"))
    wheel_metadata, wheel_names = _metadata_from_wheel(wheel)
    sdist_metadata, sdist_names = _metadata_from_sdist(sdist)
    return BuiltDistributions(
        wheel=wheel,
        sdist=sdist,
        wheel_metadata=wheel_metadata,
        sdist_metadata=sdist_metadata,
        wheel_names=wheel_names,
        sdist_names=sdist_names,
    )


@pytest.fixture(scope="module")
def installed_release_smoke_evidence(
    built_distributions: BuiltDistributions,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, Any]]:
    root = tmp_path_factory.mktemp("installed-release-smoke")
    evidence: dict[str, dict[str, Any]] = {}
    for artifact_kind, package in (
        ("wheel", built_distributions.wheel),
        ("sdist", built_distributions.sdist),
    ):
        environment = root / f"{artifact_kind}-env"
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        python = environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            [str(python), "-m", "pip", "install", str(package)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_environment = dict(os.environ)
        process_environment.pop("PYTHONHOME", None)
        process_environment.pop("PYTHONPATH", None)
        process_environment["PYTHONNOUSERSITE"] = "1"
        completed = subprocess.run(
            [
                str(python),
                "-I",
                str(INSTALLED_SMOKE),
                "0.1.0",
                ">=3.11",
                "--release-evidence",
            ],
            cwd=root,
            env=process_environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        evidence[artifact_kind] = json.loads(completed.stdout)
    return evidence


def test_project_metadata_declares_supported_distribution_contract() -> None:
    config = _project_config()
    project = config["project"]
    assert isinstance(project, dict)

    assert project["name"] == "millforge"
    assert project["dynamic"] == ["version"]
    assert "version" not in project
    assert project["license"] == PROJECT_LICENSE
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [EXPECTED_PERSON]
    assert project["maintainers"] == [EXPECTED_PERSON]
    assert project["requires-python"] == ">=3.11"
    assert project["urls"] == EXPECTED_URLS
    assert project["description"] == (
        "A typed Python runner and harness compiler for guarded LLM tool execution."
    )

    classifiers = project["classifiers"]
    assert PROJECT_LICENSE_CLASSIFIER in classifiers
    assert "License :: OSI Approved :: MIT License" not in classifiers
    assert "Development Status :: 2 - Pre-Alpha" in classifiers
    assert "Operating System :: MacOS" in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Programming Language :: Python :: 3.11" in classifiers

    runtime_dependency_names = {
        re.split(r"[\s<>=!~;\[]", item, maxsplit=1)[0].lower()
        for item in project["dependencies"]
    }
    assert runtime_dependency_names == {"httpx", "pathspec", "pydantic"}
    assert "scripts" not in project
    assert "gui-scripts" not in project
    assert "entry-points" not in project
    assert set(project["optional-dependencies"]) == {"dev"}


def test_installed_smoke_uses_only_the_root_millforge_consumer_surface() -> None:
    tree = ast.parse(INSTALLED_SMOKE.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)

    millforge_imports = tuple(
        module for module in imported_modules if module.startswith("millforge")
    )
    assert millforge_imports == ("millforge", "millforge")
    assert not any(
        module.startswith(
            (
                "millforge._forge",
                "millforge.model_backend",
                "millforge.testing",
                "tests",
            )
        )
        for module in imported_modules
    )


def test_default_runner_readiness_report_is_tracked_and_self_contained() -> None:
    report = READINESS_REPORT.read_text(encoding="utf-8")
    required_sections = (
        "## Source Identity",
        "## Public API And Compatibility Changes",
        "## Descriptor Identity",
        "## Selected Output Contract",
        "## Lifecycle, Ownership, And Timeouts",
        "## Local Verification Evidence",
        "## Hosted CI Evidence",
        "## Package Inspection",
        "## Repository Status",
        "## Deferred Work",
        "## Readiness Boundary",
    )
    required_commands = (
        "uv sync --frozen --extra dev",
        'uv run python -m pytest -m "not live_model_backend"',
        "uv run python -m compileall -q src",
        "uv run mypy .",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv build",
        "uv run python scripts/ci_package_smoke.py dist",
        "git ls-files --error-unmatch "
        "tests/fixtures/default_runner_readiness_closure_report.md",
        "git diff --check",
        f"git diff --stat {READINESS_BASELINE}",
        "git status --short --branch",
    )
    deferred_terms = (
        "external adapter implementation",
        "runner selection/defaulting",
        "caller dispatch echo",
        "workflow terminal mapping",
        "retries",
        "durable orchestration",
        "live paid evaluation",
        "native Windows",
        "release tagging",
        "GitHub release creation",
        "PyPI publication",
    )

    assert report.startswith("# Default Runner Readiness Closure Report\n")
    assert READINESS_BASELINE in report
    assert all(section in report for section in required_sections)
    assert all(f"`{command}`" in report for command in required_commands)
    assert all(term in report for term in deferred_terms)
    assert "Millrace runtime artifacts are not evidence for this report" in report
    assert re.search(r"Resulting closure commit: (?:`[0-9a-f]{40}`|PENDING)", report)

    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--error-unmatch",
            str(READINESS_REPORT.relative_to(ROOT)),
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert tracked.returncode == 0, tracked.stderr


def test_version_module_is_the_only_project_version_source() -> None:
    config = _project_config()
    hatch_version = config["tool"]["hatch"]["version"]
    assert hatch_version == {"path": "src/millforge/_version.py"}

    version_tree = ast.parse(VERSION_MODULE.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in version_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.Constant)
    assert assignments[0].value.value == millforge.__version__

    init_tree = ast.parse(PACKAGE_INIT.read_text(encoding="utf-8"))
    init_assignments = [
        node
        for node in init_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    ]
    assert len(init_assignments) == 1
    init_value = init_assignments[0].value
    assert isinstance(init_value, ast.Attribute)
    assert isinstance(init_value.value, ast.Name)
    assert (init_value.value.id, init_value.attr) == ("_version", "__version__")
    assert describe_millforge_base().package_version == millforge.__version__


def test_readme_and_license_files_state_exact_license_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    root_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    project = _project_config()["project"]

    assert readme.splitlines()[2] == project["description"]
    assert "Millforge is currently pre-alpha" in readme
    assert "Tim Osterhus <tim@millrace.ai>" in readme
    assert "https://github.com/tim-osterhus/millforge" in readme
    assert "Apache License" in root_license
    assert "Version 2.0, January 2004" in root_license
    assert "Millforge is licensed under Apache-2.0" in readme
    assert "millforge/_forge/{LICENSE,PROVENANCE.json}" in readme
    assert "millforge/tools/pi_compat/{PI_LICENSE,PROVENANCE.json}" in readme
    assert (
        (ROOT / "src" / "millforge" / "_forge" / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("MIT License\n")
    )
    assert (
        (ROOT / "src" / "millforge" / "tools" / "pi_compat" / "PI_LICENSE")
        .read_text(encoding="utf-8")
        .startswith("MIT License\n")
    )
    assert not (ROOT / "THIRD_PARTY_NOTICES.md").exists()


def test_built_metadata_agrees_with_project_contract(
    built_distributions: BuiltDistributions,
) -> None:
    for artifact, metadata in (
        (built_distributions.wheel, built_distributions.wheel_metadata),
        (built_distributions.sdist, built_distributions.sdist_metadata),
    ):
        assert metadata["Name"] == "millforge"
        assert metadata["Version"] == millforge.__version__
        assert f"-{millforge.__version__}" in artifact.name
        assert metadata["License-Expression"] == PROJECT_LICENSE
        assert metadata["Requires-Python"] == ">=3.11"
        assert metadata["Author-email"] == "Tim Osterhus <tim@millrace.ai>"
        assert metadata["Maintainer-email"] == "Tim Osterhus <tim@millrace.ai>"
        assert metadata["Summary"] == (
            "A typed Python runner and harness compiler for guarded LLM tool execution."
        )
        assert metadata["Description-Content-Type"] == "text/markdown"
        assert metadata.get_all("License-File") == ["LICENSE"]
        classifiers = metadata.get_all("Classifier", failobj=[])
        assert PROJECT_LICENSE_CLASSIFIER in classifiers
        assert "License :: OSI Approved :: MIT License" not in classifiers
        project_urls = set(metadata.get_all("Project-URL", failobj=[]))
        assert project_urls == {f"{name}, {url}" for name, url in EXPECTED_URLS.items()}


def test_built_archives_contain_only_intended_license_and_package_surfaces(
    built_distributions: BuiltDistributions,
) -> None:
    wheel_names = built_distributions.wheel_names
    sdist_names = built_distributions.sdist_names

    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
    assert "LICENSE" in sdist_names
    for path in UPSTREAM_NOTICE_PATHS:
        assert path in wheel_names
        assert f"src/{path}" in sdist_names
    assert "millforge/py.typed" in wheel_names
    assert "src/millforge/py.typed" in sdist_names

    for names in (wheel_names, sdist_names):
        assert not any(name.endswith("THIRD_PARTY_NOTICES.md") for name in names)
        for name in names:
            parts = {part.lower() for part in Path(name).parts}
            assert not (parts & FORBIDDEN_ARCHIVE_PARTS), name
            assert not name.endswith((".log", ".pyc", ".pyo")), name
            assert "__pycache__" not in parts, name


def test_distribution_metadata_contains_no_secret_shaped_values(
    built_distributions: BuiltDistributions,
) -> None:
    for metadata in (
        built_distributions.wheel_metadata,
        built_distributions.sdist_metadata,
    ):
        serialized = metadata.as_string(policy=email.policy.default)
        assert not any(pattern.search(serialized) for pattern in SECRET_SHAPED_PATTERNS)


def test_wheel_and_sdist_installed_smoke_preserves_frozen_release_identity(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        identity = evidence["release_evidence"]["identity"]
        assert evidence["version"] == "0.1.0"
        assert evidence["requires_python"] == ">=3.11"
        assert identity == {
            "context_contract_version": "millforge-base.context.v1",
            "descriptor_sha256": EXPECTED_DESCRIPTOR_SHA256,
            "distribution": "millforge",
            "legal_terminal_results": ["BLOCKED", "COMPLETE", "REJECTED"],
            "prompt_contract_version": "millforge-base.prompt.v1",
            "runner_id": "millforge-base",
            "runner_version": 2,
            "version": "0.1.0",
        }


def test_wheel_and_sdist_installed_smoke_preserves_distinct_terminal_result_selected_output_schemas(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        outputs = evidence["release_evidence"]["selected_outputs"]
        assert outputs["COMPLETE"] == {
            "schema_sha256": EXPECTED_SELECTED_OUTPUT_SCHEMA_SHA256["COMPLETE"],
            "selected_output": {"present": True, "value": {"answer": 42}},
        }
        assert outputs["BLOCKED"] == {
            "schema_sha256": EXPECTED_SELECTED_OUTPUT_SCHEMA_SHA256["BLOCKED"],
            "selected_output": {"present": True, "value": ["operator", "input"]},
        }


def test_wheel_and_sdist_installed_smoke_preserves_schema_less_terminal_result(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        schema_less = evidence["release_evidence"]["schema_less_terminal_result"]
        assert schema_less == {
            "execution_status": "completed",
            "selected_output": None,
            "selected_output_schema_sha256": None,
            "terminal_result": "REJECTED",
        }


def test_wheel_and_sdist_installed_smoke_refuses_crossed_terminal_result_selected_output(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        refusal = evidence["release_evidence"]["crossed_result_refusal"]
        assert refusal == {
            "correction_observed": True,
            "rejected_call_id": "call-crossed",
            "terminal_result": "COMPLETE",
        }


def test_wheel_and_sdist_installed_smoke_preserves_selected_output_evidence_digest(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        assert (
            evidence["release_evidence"]["selected_output_requirements_sha256"]
            == EXPECTED_SELECTED_OUTPUT_REQUIREMENTS_SHA256
        )


def test_wheel_and_sdist_installed_smoke_preserves_required_reasoning_continuation(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        continuation = evidence["release_evidence"]["reasoning_continuation"]
        assert continuation == {
            "provider_tool_call_id": "call-reasoning-read",
            "replayed": True,
        }


def test_wheel_and_sdist_installed_smoke_allows_correction_after_nonreplayable_soft_failure(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        correction = evidence["release_evidence"]["soft_failure_correction"]
        assert correction == {
            "corrective_call_id": "call-corrective-read",
            "failed_execution_trace_records": 1,
            "failed_call_id": "call-missing-read",
            "failed_call_replayed": False,
            "subsequent_request_history": [
                {"assistant_call_records": 1, "tool_result_records": 1},
                {"assistant_call_records": 1, "tool_result_records": 1},
            ],
            "terminal_result": "COMPLETE",
        }


def test_wheel_and_sdist_installed_smoke_executes_multiple_tool_calls_in_order_with_parallel_disabled(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        serial = evidence["release_evidence"]["serial_tool_calls"]
        assert serial == {
            "parallel_tool_calls": False,
            "provider_call_order": ["call-serial-first", "call-serial-second"],
            "tool_result_order": ["call-serial-first", "call-serial-second"],
        }


def test_wheel_and_sdist_installed_smoke_normalizes_blank_content_for_valid_tool_calls(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        assert evidence["release_evidence"]["blank_content_with_tool_calls"] == {
            "normalized_to_absent": True,
        }


def test_wheel_and_sdist_installed_smoke_refuses_blank_content_without_tool_calls(
    installed_release_smoke_evidence: dict[str, dict[str, Any]],
) -> None:
    for evidence in installed_release_smoke_evidence.values():
        assert evidence["release_evidence"]["blank_content_without_tool_calls"] == {
            "refused": True,
        }
