"""Verify built wheel and sdist installation in fresh Python environments."""

from __future__ import annotations

import email.parser
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple


def _run(*args: str) -> None:
    subprocess.run(args, check=True)


class _ArchiveMetadata(NamedTuple):
    version: str
    requires_python: str


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
    return _ArchiveMetadata(
        version=str(metadata["Version"]),
        requires_python=str(metadata["Requires-Python"]),
    )


def _install_and_smoke(package: Path, environment: Path) -> None:
    archive_metadata = _archive_metadata(package)
    if archive_metadata.requires_python != ">=3.11":
        raise RuntimeError(f"{package.name} does not require Python >=3.11")

    _run(sys.executable, "-m", "venv", str(environment))
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    _run(str(python), "-m", "pip", "install", str(package))
    _run(
        str(python),
        str(Path(__file__).with_name("installed_package_smoke.py")),
        archive_metadata.version,
        archive_metadata.requires_python,
    )


def main() -> None:
    dist = Path(sys.argv[1]).resolve()
    wheel = next(dist.glob("millforge-*.whl"))
    sdist = next(dist.glob("millforge-*.tar.gz"))

    with tempfile.TemporaryDirectory() as raw_temp:
        temp = Path(raw_temp)
        _install_and_smoke(wheel, temp / "wheel-env")
        _install_and_smoke(sdist, temp / "sdist-env")


if __name__ == "__main__":
    main()
