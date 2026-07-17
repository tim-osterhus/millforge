"""Installed-package typing verification for Millforge's public runner surface."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSUMER = ROOT / "tests" / "fixtures" / "typed_package_consumer.py"


def test_built_wheel_type_checks_external_public_consumer(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wheel = next(dist.glob("millforge-*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    assert (installed / "millforge" / "py.typed").is_file()

    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    environment = dict(os.environ)
    environment["MYPYPATH"] = str(installed)
    environment["PYTHONPATH"] = str(installed)
    subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(CONSUMER)],
        cwd=outside_checkout,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
