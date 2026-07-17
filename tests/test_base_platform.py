"""POSIX-only platform contract for the operational base runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import get_type_hints

import pytest

import millforge
import millforge.base.composition as composition
import millforge.base.runner as runner_module
from millforge import (
    HarnessExecutionRequest,
    UnsupportedPlatformError,
    describe_millforge_base,
)
from millforge.base.platform import SUPPORTED_PLATFORMS, _require_supported_platform
from millforge.model_backend import ResolvedModelProfile
from tests.conftest import FakeCancellationResolver

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("platform_id", ("linux", "darwin"))
def test_supported_posix_platforms_are_admitted(
    monkeypatch: pytest.MonkeyPatch, platform_id: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform_id)

    _require_supported_platform()


def test_wsl_uses_linux_admission_without_a_special_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    _require_supported_platform()

    assert SUPPORTED_PLATFORMS == ("linux", "darwin")
    assert "wsl" not in SUPPORTED_PLATFORMS


@pytest.mark.parametrize("platform_id", ("win32", "plan9"))
def test_unsupported_platform_error_has_stable_bounded_contract(
    monkeypatch: pytest.MonkeyPatch, platform_id: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform_id)

    with pytest.raises(UnsupportedPlatformError) as caught:
        _require_supported_platform()

    error = caught.value
    assert error.platform_id == platform_id
    assert error.supported_platforms == ("linux", "darwin")
    assert str(error) == (
        "millforge-base requires Linux, macOS, or WSL; "
        f"native platform {platform_id} is unsupported"
    )
    hints = get_type_hints(UnsupportedPlatformError)
    assert set(hints) == {"platform_id", "supported_platforms"}
    assert len(str(error).encode("utf-8")) <= 256


def test_descriptor_inspection_remains_static_on_native_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        composition,
        "_load_millforge_base_context_resolved",
        lambda **_kwargs: pytest.fail("descriptor inspection discovered context"),
    )

    descriptor = describe_millforge_base()

    assert descriptor.supported_platforms == ("linux", "darwin")


def test_descriptor_digest_covers_ordered_supported_platforms() -> None:
    descriptor = describe_millforge_base()
    payload = descriptor.model_dump(mode="json")
    digest = payload.pop("descriptor_sha256")

    assert payload["supported_platforms"] == ["linux", "darwin"]
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


def test_composition_refuses_before_discovery_or_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        composition,
        "_resolved_absolute_path",
        lambda *_args: pytest.fail("composition resolved a path"),
    )
    monkeypatch.setattr(
        composition,
        "_load_millforge_base_context_resolved",
        lambda **_kwargs: pytest.fail("composition discovered context"),
    )
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: pytest.fail("composition resolved a shell"),
    )

    with pytest.raises(UnsupportedPlatformError):
        composition.create_millforge_base_components(
            model_profile=object.__new__(ResolvedModelProfile),
            cwd=Path("relative-must-not-be-read"),
            cancellation_resolver=FakeCancellationResolver(),
        )


def test_runner_creation_refuses_before_descriptor_or_component_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        runner_module,
        "describe_millforge_base",
        lambda: pytest.fail("runner creation inspected the descriptor"),
    )

    with pytest.raises(UnsupportedPlatformError):
        runner_module.create_millforge_base_runner(
            components=object.__new__(composition.MillforgeBaseComponents),
            services=object.__new__(runner_module.MillforgeBaseRuntimeServices),
        )


def test_direct_runner_construction_refuses_before_component_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        runner_module,
        "describe_millforge_base",
        lambda: pytest.fail("direct construction inspected the descriptor"),
    )

    with pytest.raises(UnsupportedPlatformError):
        runner_module.MillforgeBaseRunner(
            components=object.__new__(composition.MillforgeBaseComponents),
            services=object.__new__(runner_module.MillforgeBaseRuntimeServices),
        )


def test_execute_refuses_before_binding_artifacts_model_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    runner = object.__new__(runner_module.MillforgeBaseRunner)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        runner_module,
        "_load_invocation_executor",
        lambda: pytest.fail("execute loaded its side-effecting backend"),
    )

    with pytest.raises(UnsupportedPlatformError):
        asyncio.run(runner.execute(object.__new__(HarnessExecutionRequest)))


def test_docs_and_classifiers_match_runtime_platform_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    classifiers = set(project["classifiers"])

    assert SUPPORTED_PLATFORMS == ("linux", "darwin")
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Operating System :: MacOS" in classifiers
    assert "Operating System :: OS Independent" not in classifiers
    assert all(term in readme for term in ("Linux", "macOS", "WSL", "Native Windows"))
    assert all(
        term in roadmap
        for term in (
            "drive/UNC",
            "command quoting",
            "process-group/tree cancellation",
            "reparse-point",
            "fixture line endings",
            "native Windows CI",
        )
    )


def test_unsupported_platform_error_is_public() -> None:
    assert millforge.UnsupportedPlatformError is UnsupportedPlatformError
    assert "UnsupportedPlatformError" in millforge.__all__
