"""Audit and exercise built wheel and sdist artifacts outside the checkout."""

from __future__ import annotations

import email.parser
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple


_REQUIRED_PACKAGE_FILES = (
    "millforge/_forge/LICENSE",
    "millforge/_forge/PROVENANCE.json",
    "millforge/_forge/UPDATE_POLICY.md",
    "millforge/tools/pi_compat/PI_LICENSE",
    "millforge/tools/pi_compat/PROVENANCE.json",
    "millforge/tools/pi_compat/UPDATE_POLICY.md",
)
_FORBIDDEN_ARCHIVE_PARTS = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "credentials",
    "dist",
    "ideas",
    "logs",
    "millrace-agents",
    "ref-forge",
    "reference",
    "runtime-state",
    "runtime_state",
    "scripts",
    "secrets",
    "specs",
    "tests",
}
_FORBIDDEN_EVIDENCE_TEXT = (
    "package-smoke-secret-value",
    "Read the package note.",
    "Installed package traversal complete.",
    "millrace-agents",
    "runtime_snapshot",
)
_FORBIDDEN_ARCHIVE_NAME_TERMS = (
    "credential",
    "mailbox",
    "runtime_snapshot",
    "generated_state",
    "secret",
    "token",
)


class _ArchiveMetadata(NamedTuple):
    version: str
    requires_python: str


def _completed(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {args[1:3]!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _archive_metadata(package: Path) -> _ArchiveMetadata:
    if package.suffix == ".whl":
        with zipfile.ZipFile(package) as archive:
            name = next(
                item
                for item in archive.namelist()
                if item.endswith(".dist-info/METADATA")
            )
            raw = archive.read(name)
    else:
        with tarfile.open(package, "r:gz") as archive:
            member = next(
                item for item in archive.getmembers() if item.name.endswith("/PKG-INFO")
            )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("sdist PKG-INFO is not readable")
            raw = extracted.read()

    metadata = email.parser.BytesParser().parsebytes(raw)
    assert metadata["Name"] == "millforge"
    assert metadata["License-Expression"] == "Apache-2.0"
    assert "Operating System :: MacOS" in metadata.get_all("Classifier", failobj=[])
    assert "Operating System :: POSIX :: Linux" in metadata.get_all(
        "Classifier", failobj=[]
    )
    dependencies = tuple(metadata.get_all("Requires-Dist", failobj=[]))
    assert not any("millrace" in dependency.lower() for dependency in dependencies)
    return _ArchiveMetadata(
        version=str(metadata["Version"]),
        requires_python=str(metadata["Requires-Python"]),
    )


def _archive_names(package: Path) -> set[str]:
    if package.suffix == ".whl":
        with zipfile.ZipFile(package) as archive:
            return set(archive.namelist())
    with tarfile.open(package, "r:gz") as archive:
        return {
            "/".join(Path(member.name).parts[1:])
            for member in archive.getmembers()
            if Path(member.name).parts[1:]
        }


def _audit_archive(package: Path) -> _ArchiveMetadata:
    metadata = _archive_metadata(package)
    names = _archive_names(package)
    prefix = "" if package.suffix == ".whl" else "src/"
    for required in _REQUIRED_PACKAGE_FILES:
        assert f"{prefix}{required}" in names, (package.name, required)

    for name in names:
        lowered_parts = {part.lower() for part in Path(name).parts}
        assert not (lowered_parts & _FORBIDDEN_ARCHIVE_PARTS), (package.name, name)
        assert "__pycache__" not in lowered_parts, (package.name, name)
        assert not name.endswith((".log", ".pyc", ".pyo")), (package.name, name)
        assert not any(
            term in Path(name).name.lower() for term in _FORBIDDEN_ARCHIVE_NAME_TERMS
        ), (package.name, name)
    return metadata


def _install_and_smoke(
    package: Path,
    environment: Path,
    metadata: _ArchiveMetadata,
) -> dict[str, object]:
    if metadata.requires_python != ">=3.11":
        raise RuntimeError(f"{package.name} does not require Python >=3.11")

    _completed(sys.executable, "-m", "venv", str(environment))
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    _completed(str(python), "-m", "pip", "install", str(package))

    smoke_script = environment.parent / "installed_package_smoke.py"
    shutil.copyfile(
        Path(__file__).with_name("installed_package_smoke.py"), smoke_script
    )
    process_environment = dict(os.environ)
    process_environment.pop("PYTHONHOME", None)
    process_environment.pop("PYTHONPATH", None)
    process_environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [
            str(python),
            "-I",
            str(smoke_script),
            metadata.version,
            metadata.requires_python,
        ],
        cwd=environment.parent,
        env=process_environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"installed artifact smoke failed for {package.name}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )

    retained_output = completed.stdout.strip()
    assert completed.stderr == ""
    assert retained_output
    assert str(environment.parent) not in retained_output
    assert str(Path(__file__).resolve().parents[1]) not in retained_output
    assert not any(value in retained_output for value in _FORBIDDEN_EVIDENCE_TEXT)
    evidence = json.loads(retained_output)
    assert evidence == {
        "construction_surface": "millforge.create_millforge_base_live_runner",
        "fake_transport_calls": 2,
        "network_probe_events": 0,
        "provider_local_result": "COMPLETE",
        "requires_python": ">=3.11",
        "version": metadata.version,
    }
    return evidence


def main() -> None:
    dist = Path(sys.argv[1]).resolve()
    wheel = next(dist.glob("millforge-*.whl"))
    sdist = next(dist.glob("millforge-*.tar.gz"))
    wheel_metadata = _audit_archive(wheel)
    sdist_metadata = _audit_archive(sdist)
    assert wheel_metadata == sdist_metadata

    with tempfile.TemporaryDirectory() as raw_temp:
        temp = Path(raw_temp)
        wheel_evidence = _install_and_smoke(
            wheel,
            temp / "wheel-env",
            wheel_metadata,
        )
        sdist_evidence = _install_and_smoke(
            sdist,
            temp / "sdist-env",
            sdist_metadata,
        )

    retained = {
        "archive_audit": "passed",
        "sdist": sdist_evidence,
        "wheel": wheel_evidence,
    }
    serialized = json.dumps(retained, sort_keys=True, separators=(",", ":"))
    assert not any(value in serialized for value in _FORBIDDEN_EVIDENCE_TEXT)
    print(serialized)


if __name__ == "__main__":
    main()
