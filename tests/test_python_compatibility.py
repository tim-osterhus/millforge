"""Cross-version deterministic output coverage for supported Python runtimes."""

from __future__ import annotations

import datetime
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from millforge import (
    ArtifactManifestArtifact,
    ArtifactManifestEntry,
    ExecutionResultClass,
    StageIdentity,
    TerminalResultArtifact,
)
from millforge.base import composition
from millforge.base.identity import _build_invocation_evidence, describe_millforge_base
from millforge.base.options import MillforgeBaseOptions
from millforge.tools.pi_compat.process import PiCompatShellConfig
from tests.conftest import FakeCancellationResolver, make_canonical_builder_profile_a

FIXTURE = Path(__file__).parent / "fixtures" / "python_compatibility" / "v1.json"
ROOT = Path(__file__).parents[1]


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _compatibility_outputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    fixed_root = Path("/")
    monkeypatch.setattr(composition, "_require_supported_platform", lambda: None)
    monkeypatch.setattr(
        composition,
        "_resolved_absolute_path",
        lambda _path, _field_name: fixed_root,
    )
    monkeypatch.setattr(composition.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: PiCompatShellConfig(executable="/bin/sh", arguments=("-c",)),
    )
    monkeypatch.setattr(
        composition,
        "create_pi_compat_tool_executor",
        lambda *_args, **_kwargs: object(),
    )

    components = composition.create_millforge_base_components(
        model_profile=make_canonical_builder_profile_a(),
        cwd=fixed_root,
        home_directory=fixed_root,
        cancellation_resolver=FakeCancellationResolver(),
        options=MillforgeBaseOptions(load_context_files=False),
        prompt_date=datetime.date(2026, 7, 17),
    )
    descriptor = describe_millforge_base()
    evidence = _build_invocation_evidence(components, descriptor)

    terminal = TerminalResultArtifact(
        schema_version="1.0",
        request_id="request-python-compatibility-v1",
        run_id="run-python-compatibility-v1",
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        terminal_result="COMPLETE",
        result_class=ExecutionResultClass.DOMAIN_TERMINAL,
        summary_artifact_paths=("millforge/execution_summary.json",),
        compiled_harness_sha256=components.compiled_plan.compiled_sha256,
    )
    terminal_payload = terminal.model_dump(mode="json")
    terminal_bytes = _canonical_json_bytes(terminal_payload)
    manifest = ArtifactManifestArtifact(
        schema_version="1.0",
        request_id=terminal.request_id,
        run_id=terminal.run_id,
        artifacts=(
            ArtifactManifestEntry(
                artifact_id="terminal_result",
                path="millforge/terminal_result.json",
                media_type="application/json",
                byte_size=len(terminal_bytes),
                sha256_hex=hashlib.sha256(terminal_bytes).hexdigest(),
                complete=True,
                producer="millforge.runtime",
            ),
        ),
    )
    prompt_metadata = components.prompt.model_dump(
        mode="json", exclude={"system_instructions"}
    )
    context_metadata = components.context.model_dump(mode="json")

    return {
        "artifact_json_sha256": _sha256(manifest.model_dump(mode="json")),
        "base_context_metadata_sha256": _sha256(context_metadata),
        "base_prompt_metadata_sha256": _sha256(prompt_metadata),
        "canonical_compiled_plan_sha256": _sha256(
            components.compiled_plan.model_dump(mode="json")
        ),
        "compiled_plan_embedded_sha256": components.compiled_plan.compiled_sha256,
        "invocation_evidence_embedded_sha256": evidence.invocation_sha256,
        "invocation_evidence_sha256": _sha256(evidence.model_dump(mode="json")),
        "runner_descriptor_embedded_sha256": descriptor.descriptor_sha256,
        "runner_descriptor_sha256": _sha256(descriptor.model_dump(mode="json")),
        "terminal_json_sha256": hashlib.sha256(terminal_bytes).hexdigest(),
        "tool_catalog_sha256": descriptor.tool_catalog_sha256,
    }


def test_frozen_outputs_match_python_compatibility_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert _compatibility_outputs(monkeypatch) == expected


def test_python_support_metadata_and_lock_are_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert project["requires-python"] == ">=3.11"
    assert lock["requires-python"] == ">=3.11"
    assert {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    }.issubset(project["classifiers"])
    assert not any(
        "Windows" in classifier or "PyPy" in classifier
        for classifier in project["classifiers"]
    )
