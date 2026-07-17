from __future__ import annotations

import ast
import email.policy
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

PROJECT_LICENSE = "Apache-2.0"
PROJECT_LICENSE_CLASSIFIER = "License :: OSI Approved :: Apache Software License"
EXPECTED_PERSON = {"name": "Tim Osterhus", "email": "tim@millrace.ai"}
EXPECTED_URLS = {
    "Homepage": "https://github.com/tim-osterhus/millforge",
    "Repository": "https://github.com/tim-osterhus/millforge",
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
