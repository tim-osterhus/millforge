"""Packet-05 contracts for stable base identity and invocation evidence."""

from __future__ import annotations

import datetime
import getpass
import hashlib
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import millforge
import millforge.base.identity as identity
import millforge.base.runner as runner_module
from millforge import (
    CapabilityEnvelope,
    CapabilityGrant,
    MillforgeBaseBindingError,
    MillforgeBaseRunnerDescriptor,
    MillforgeBaseRuntimeServices,
    MillforgeInvocationEvidence,
    RuntimeArtifactWriterFactory,
    SecretRef,
    SelectedOutputRequirement,
    TerminalSelectedOutputRequirement,
    create_millforge_base_components,
    create_millforge_base_live_runner,
    create_millforge_base_runner,
    describe_millforge_base,
)
from millforge.base import composition
from millforge.base.harness import millforge_base_harness_source
from millforge.compiled_plan import finalize_compiled_plan_sha256
from millforge.model_backend import (
    AuthenticationPolicy,
    AuthenticationScheme,
    CapabilitySupport,
    ReasoningMode,
    ReasoningPolicy,
)
from millforge.testing import FakeModelClient
from millforge.tools.pi_compat.process import PiCompatShellConfig
from millforge.tools.pi_compat_catalog import (
    create_pi_compat_tool_registry,
    create_pi_compat_tool_snapshot,
)
from millforge.tools.pi_compat_runtime import create_pi_compat_tool_executor
from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    make_canonical_builder_profile_a,
)
from tests.test_base_runner import _request

ROOT = Path(__file__).resolve().parents[1]

DESCRIPTOR_FIELDS = (
    "schema_version",
    "package_name",
    "package_version",
    "runner_id",
    "runner_version",
    "harness_id",
    "harness_version",
    "tool_pack_id",
    "tool_pack_version",
    "required_model_capability_ids",
    "required_capability_ids",
    "legal_terminal_result_ids",
    "artifact_contract_version",
    "prompt_contract_version",
    "context_contract_version",
    "tool_catalog_sha256",
    "forge_provenance_sha256",
    "pi_provenance_sha256",
    "supported_platforms",
    "descriptor_sha256",
)
EVIDENCE_FIELDS = (
    "schema_version",
    "request_id",
    "run_id",
    "selected_output_requirements_sha256",
    "descriptor_sha256",
    "compiled_plan_sha256",
    "model_profile_id",
    "model_behavior_sha256",
    "capability_envelope_sha256",
    "effective_prompt_sha256",
    "context_sha256",
    "context_file_count",
    "context_truncated",
    "prompt_truncated",
    "cwd_sha256",
    "invocation_sha256",
)


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _components(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    legal_terminal_results: tuple[str, ...] = ("BLOCKED", "COMPLETE", "REJECTED"),
):
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: PiCompatShellConfig(executable="/test/bin/bash", arguments=("-c",)),
    )
    home = root / "home"
    home.mkdir(exist_ok=True)
    return composition.create_millforge_base_components(
        model_profile=make_canonical_builder_profile_a(),
        cwd=root.resolve(),
        cancellation_resolver=FakeCancellationResolver(),
        legal_terminal_results=legal_terminal_results,
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )


def _runner(monkeypatch: pytest.MonkeyPatch, root: Path):
    components = _components(monkeypatch, root)
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory,
                lambda _path: FakeArtifactWriter(),
            ),
        ),
    )
    return components, runner


def _replace_static_contract(
    monkeypatch: pytest.MonkeyPatch, index: int, value: Any
) -> None:
    original = identity._static_contract_for_terminal_results(
        ("BLOCKED", "COMPLETE", "REJECTED")
    )
    changed = (*original[:index], value, *original[index + 1 :])
    monkeypatch.setattr(
        identity,
        "_static_contract_for_terminal_results",
        lambda _legal_terminal_results: changed,
    )


def _replace_provenance_hash(
    monkeypatch: pytest.MonkeyPatch, resource_name: str
) -> None:
    original = identity._provenance_sha256

    def changed(*parts: str) -> str:
        if parts[-1] == "PROVENANCE.json" and parts[0] == resource_name:
            return "b" * 64
        return original(*parts)

    monkeypatch.setattr(identity, "_provenance_sha256", changed)


def _rehash_descriptor(payload: dict[str, Any]) -> MillforgeBaseRunnerDescriptor:
    payload["descriptor_sha256"] = hashlib.sha256(
        _canonical_bytes(
            {key: value for key, value in payload.items() if key != "descriptor_sha256"}
        )
    ).hexdigest()
    return MillforgeBaseRunnerDescriptor.model_validate(payload)


def test_base_provider_stage_identity_is_exact_and_provider_local() -> None:
    assert identity._MILLFORGE_BASE_STAGE_IDENTITY.model_dump(mode="json") == {
        "plane": "execution",
        "node_id": "millforge-base",
        "stage_kind_id": "millforge_base",
    }


def test_descriptor_has_exact_contract_and_canonical_self_hash() -> None:
    descriptor = describe_millforge_base()
    forge_provenance = (
        ROOT / "src" / "millforge" / "_forge" / "PROVENANCE.json"
    ).read_bytes()
    pi_provenance = (
        ROOT / "src" / "millforge" / "tools" / "pi_compat" / "PROVENANCE.json"
    ).read_bytes()
    expected = {
        "schema_version": "1.0",
        "package_name": "millforge",
        "package_version": millforge.__version__,
        "runner_id": "millforge-base",
        "runner_version": 2,
        "harness_id": "millforge.base.unrestricted_agent.v1",
        "harness_version": 1,
        "tool_pack_id": "millforge.toolpack.pi_compat.unrestricted.v1",
        "tool_pack_version": 1,
        "required_model_capability_ids": ["tool_calls"],
        "required_capability_ids": [
            "terminal.intent",
            "unrestricted.filesystem.read",
            "unrestricted.filesystem.write",
            "unrestricted.process.execute",
        ],
        "legal_terminal_result_ids": ["BLOCKED", "COMPLETE", "REJECTED"],
        "artifact_contract_version": "millforge.runtime-artifacts.v2",
        "prompt_contract_version": "millforge-base.prompt.v1",
        "context_contract_version": "millforge-base.context.v1",
        "tool_catalog_sha256": create_pi_compat_tool_snapshot().snapshot_sha256,
        "forge_provenance_sha256": identity._canonical_provenance_sha256(
            forge_provenance
        ),
        "pi_provenance_sha256": identity._canonical_provenance_sha256(pi_provenance),
        "supported_platforms": ["linux", "darwin"],
    }
    expected["descriptor_sha256"] = hashlib.sha256(
        _canonical_bytes(expected)
    ).hexdigest()

    assert tuple(type(descriptor).model_fields) == DESCRIPTOR_FIELDS
    assert descriptor.model_dump(mode="json") == expected


def test_descriptor_strict_json_round_trip_and_tamper_rejection() -> None:
    descriptor = describe_millforge_base()
    encoded = descriptor.model_dump_json()

    assert MillforgeBaseRunnerDescriptor.model_validate_json(encoded) == descriptor
    payload = json.loads(encoded)
    payload["runner_id"] = "tampered"
    with pytest.raises(ValidationError, match="descriptor_sha256"):
        MillforgeBaseRunnerDescriptor.model_validate(payload)
    payload = json.loads(encoded)
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _rehash_descriptor(payload)


def test_public_contracts_reject_missing_digests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor_payload = describe_millforge_base().model_dump(mode="json")
    descriptor_payload.pop("descriptor_sha256")
    with pytest.raises(ValidationError, match="descriptor_sha256"):
        MillforgeBaseRunnerDescriptor.model_validate(descriptor_payload)

    components, runner = _runner(monkeypatch, tmp_path)
    evidence_payload = runner.invocation_evidence_for(
        _request(components, tmp_path)
    ).model_dump(mode="json")
    evidence_payload.pop("invocation_sha256")
    with pytest.raises(ValidationError, match="invocation_sha256"):
        MillforgeInvocationEvidence.model_validate(evidence_payload)


def test_stale_model_instances_are_revalidated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor = describe_millforge_base()
    stale_descriptor = descriptor.model_copy(update={"runner_id": "stale"})
    with pytest.raises(ValidationError, match="descriptor_sha256"):
        MillforgeBaseRunnerDescriptor.model_validate(stale_descriptor)

    components, runner = _runner(monkeypatch, tmp_path)
    stale_evidence = runner.invocation_evidence_for(
        _request(components, tmp_path)
    ).model_copy(update={"cwd_sha256": "f" * 64})
    with pytest.raises(ValidationError, match="invocation_sha256"):
        MillforgeInvocationEvidence.model_validate(stale_evidence)


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    (
        ("package_name", object(), "package_name"),
        ("runner_version", {"invalid": True}, "runner_version"),
        ("unexpected", object(), "extra_forbidden"),
    ),
)
def test_descriptor_malformed_fields_raise_validation_errors(
    field_name: str, value: object, match: str
) -> None:
    payload = describe_millforge_base().model_dump(mode="json")
    payload[field_name] = value

    with pytest.raises(ValidationError, match=match):
        MillforgeBaseRunnerDescriptor.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    (
        ("model_profile_id", object(), "model_profile_id"),
        ("request_id", " ", "request_id"),
        ("run_id", " ", "run_id"),
        ("context_file_count", {"invalid": True}, "context_file_count"),
        ("unexpected", object(), "extra_forbidden"),
    ),
)
def test_evidence_malformed_fields_raise_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    value: object,
    match: str,
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    payload = runner.invocation_evidence_for(_request(components, tmp_path)).model_dump(
        mode="json"
    )
    payload[field_name] = value

    with pytest.raises(ValidationError, match=match):
        MillforgeInvocationEvidence.model_validate(payload)


def test_descriptor_schema_version_source_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity, "_DESCRIPTOR_SCHEMA_VERSION", "2.0")

    with pytest.raises(ValidationError, match="schema_version"):
        describe_millforge_base()


def test_descriptor_is_invariant_to_dynamic_process_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = describe_millforge_base().model_dump_json()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "different-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "different-profile"))
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "AGENTS.md").write_text("dynamic context", encoding="utf-8")

    assert describe_millforge_base().model_dump_json() == first


def test_descriptor_is_invariant_to_runtime_identity_and_model_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = describe_millforge_base().model_dump_json()

    class DifferentDate(datetime.date):
        @classmethod
        def today(cls) -> DifferentDate:
            return cls(2099, 12, 31)

    monkeypatch.setattr(datetime, "date", DifferentDate)
    monkeypatch.setattr(time, "time", lambda: 99_999_999.0)
    monkeypatch.setattr(time, "monotonic", lambda: 42.0)
    monkeypatch.setattr(os, "getpid", lambda: 999_999)
    monkeypatch.setattr(socket, "gethostname", lambda: "different-host")
    monkeypatch.setattr(getpass, "getuser", lambda: "different-user")
    monkeypatch.setenv("MILLFORGE_MODEL_PROFILE", "different-profile")
    monkeypatch.setenv("OPENAI_API_KEY", "prohibited-model-secret")

    assert describe_millforge_base().model_dump_json() == first


def test_fresh_process_import_and_descriptor_avoid_discovery_and_preparation() -> None:
    script = f"""
import os
import pathlib
import socket
import subprocess
import sys

sys.path.insert(0, {str(ROOT / "src")!r})

# Isolate Millforge import behavior from benign third-party import initialization.
import httpx
import pathspec
import pydantic

def prohibited(*args, **kwargs):
    raise AssertionError("prohibited discovery or preparation API")

os.getcwd = prohibited
pathlib.Path.cwd = prohibited
pathlib.Path.home = prohibited
socket.create_connection = prohibited
subprocess.Popen = prohibited
subprocess.run = prohibited
subprocess.check_call = prohibited
subprocess.check_output = prohibited

import millforge

descriptor = millforge.describe_millforge_base()
assert descriptor.runner_id == "millforge-base"
assert descriptor.supported_platforms == ("linux", "darwin")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_descriptor_inspection_avoids_preparation_and_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prohibited(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("prohibited descriptor side effect")

    monkeypatch.setattr(Path, "cwd", prohibited)
    monkeypatch.setattr(Path, "home", prohibited)
    monkeypatch.setattr(composition, "create_millforge_base_components", prohibited)
    monkeypatch.setattr(composition, "compile_harness_source_in_memory", prohibited)
    monkeypatch.setattr(composition, "_model_profile_catalog", prohibited)
    monkeypatch.setattr(composition, "resolve_pi_compat_shell", prohibited)
    monkeypatch.setattr(sys, "platform", "win32")

    assert describe_millforge_base().runner_id == "millforge-base"


@pytest.mark.parametrize(
    ("name", "mutate"),
    (
        (
            "package",
            lambda monkeypatch: monkeypatch.setattr(identity, "_PACKAGE_NAME", "other"),
        ),
        (
            "package_version",
            lambda monkeypatch: monkeypatch.setattr(
                identity.millforge, "__version__", "9.9.9"
            ),
        ),
        (
            "runner_id",
            lambda monkeypatch: monkeypatch.setattr(identity, "_RUNNER_ID", "other"),
        ),
        (
            "runner_version",
            lambda monkeypatch: monkeypatch.setattr(identity, "_RUNNER_VERSION", 3),
        ),
        (
            "harness_id",
            lambda monkeypatch: _replace_static_contract(
                monkeypatch, 0, "other.harness"
            ),
        ),
        (
            "harness_version",
            lambda monkeypatch: _replace_static_contract(monkeypatch, 1, 2),
        ),
        (
            "tool_pack_id",
            lambda monkeypatch: _replace_static_contract(
                monkeypatch, 2, "other.toolpack"
            ),
        ),
        (
            "tool_pack_version",
            lambda monkeypatch: monkeypatch.setattr(identity, "_TOOL_PACK_VERSION", 2),
        ),
        (
            "required_capabilities",
            lambda monkeypatch: _replace_static_contract(
                monkeypatch, 3, ("other.cap",)
            ),
        ),
        (
            "terminals",
            lambda monkeypatch: _replace_static_contract(monkeypatch, 4, ("OTHER",)),
        ),
        (
            "catalog",
            lambda monkeypatch: _replace_static_contract(monkeypatch, 5, "a" * 64),
        ),
        (
            "forge_provenance",
            lambda monkeypatch: _replace_provenance_hash(monkeypatch, "_forge"),
        ),
        (
            "pi_provenance",
            lambda monkeypatch: _replace_provenance_hash(monkeypatch, "tools"),
        ),
        (
            "model_capability",
            lambda monkeypatch: monkeypatch.setattr(
                identity, "_REQUIRED_MODEL_CAPABILITY_IDS", ("other",)
            ),
        ),
        (
            "artifact_contract",
            lambda monkeypatch: monkeypatch.setattr(
                identity, "_ARTIFACT_CONTRACT_VERSION", "other.artifact"
            ),
        ),
        (
            "prompt_contract",
            lambda monkeypatch: monkeypatch.setattr(
                identity, "_PROMPT_CONTRACT_VERSION", "other.prompt"
            ),
        ),
        (
            "context_contract",
            lambda monkeypatch: monkeypatch.setattr(
                identity, "_CONTEXT_CONTRACT_VERSION", "other.context"
            ),
        ),
        (
            "platforms",
            lambda monkeypatch: monkeypatch.setattr(
                identity, "_SUPPORTED_PLATFORMS", ("linux",)
            ),
        ),
    ),
)
def test_descriptor_is_sensitive_to_each_static_contract_source(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mutate: Any,
) -> None:
    del name
    original = describe_millforge_base()
    mutate(monkeypatch)
    changed = describe_millforge_base()

    assert changed.descriptor_sha256 != original.descriptor_sha256


@pytest.mark.parametrize(
    "parts",
    (("_forge", "PROVENANCE.json"), ("tools", "pi_compat", "PROVENANCE.json")),
)
def test_provenance_hashes_ignore_lf_and_crlf_line_endings(
    parts: tuple[str, ...],
) -> None:
    raw = (ROOT / "src" / "millforge" / Path(*parts)).read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")

    assert identity._canonical_provenance_sha256(lf) == (
        identity._canonical_provenance_sha256(crlf)
    )


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"[]",
        b'{"value":NaN}',
        b'{"duplicate":1,"duplicate":2}',
        b"\xff",
    ),
)
def test_provenance_hashing_fails_closed_for_invalid_records(raw: bytes) -> None:
    with pytest.raises(ValueError, match="provenance record"):
        identity._canonical_provenance_sha256(raw)


def _base_descriptor_source():
    return millforge_base_harness_source(
        model_profile_id="millforge-base.descriptor",
        system_instructions="millforge-base descriptor inspection",
    )


def _replace_descriptor_source(monkeypatch: pytest.MonkeyPatch, source: Any) -> None:
    monkeypatch.setattr(
        identity,
        "_millforge_base_harness_source_for_terminal_results",
        lambda **_kwargs: source,
    )


def test_required_capabilities_follow_only_harness_graph_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = describe_millforge_base()
    source = _base_descriptor_source()
    nodes = tuple(node for node in source.graph.nodes if node.node_id != "bash")
    changed_source = source.model_copy(
        update={"graph": source.graph.model_copy(update={"nodes": nodes})}
    )
    _replace_descriptor_source(monkeypatch, changed_source)

    changed = describe_millforge_base()

    assert "unrestricted.process.execute" in original.required_capability_ids
    assert "unrestricted.process.execute" not in changed.required_capability_ids
    assert changed.descriptor_sha256 != original.descriptor_sha256


@pytest.mark.parametrize("kind", ("duplicate", "missing", "unsupported"))
def test_harness_tool_reference_resolution_fails_closed(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    source = _base_descriptor_source()
    nodes = list(source.graph.nodes)
    if kind == "duplicate":
        nodes[1] = nodes[1].model_copy(update={"tool_ref": nodes[0].tool_ref})
        message = "duplicate exact tool reference"
    elif kind == "missing":
        nodes[0] = nodes[0].model_copy(
            update={"tool_ref": "builtin.pi_compat.missing@1"}
        )
        message = "not in the Pi catalog"
    else:
        nodes[0] = nodes[0].model_copy(update={"tool_ref": "unsupported"})
        message = "exact-version tool reference"
    changed_source = source.model_copy(
        update={"graph": source.graph.model_copy(update={"nodes": tuple(nodes)})}
    )
    _replace_descriptor_source(monkeypatch, changed_source)

    with pytest.raises(ValueError, match=message):
        describe_millforge_base()


def test_invocation_evidence_exact_contract_round_trip_and_self_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    request = _request(components, tmp_path)
    evidence = runner.invocation_evidence_for(request)
    payload = evidence.model_dump(mode="json")
    digest = payload.pop("invocation_sha256")

    assert tuple(type(evidence).model_fields) == EVIDENCE_FIELDS
    assert evidence.schema_version == "1.3"
    assert evidence.request_id == request.request_id
    assert evidence.run_id == request.run_id
    assert "selected_output_requirements_sha256" not in payload
    assert digest == hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    assert (
        MillforgeInvocationEvidence.model_validate_json(evidence.model_dump_json())
        == evidence
    )
    tampered = evidence.model_dump(mode="json")
    tampered["cwd_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="invocation_sha256"):
        MillforgeInvocationEvidence.model_validate(tampered)


def test_successor_identity_refuses_old_selected_output_contract_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    request = _request(components, tmp_path)
    evidence_payload = runner.invocation_evidence_for(request).model_dump(mode="json")

    old_schema = dict(evidence_payload)
    old_schema["schema_version"] = "1.2"
    old_schema["invocation_sha256"] = hashlib.sha256(
        _canonical_bytes(
            {
                key: value
                for key, value in old_schema.items()
                if key != "invocation_sha256"
            }
        )
    ).hexdigest()
    with pytest.raises(ValidationError, match="schema_version"):
        MillforgeInvocationEvidence.model_validate(old_schema)

    singular_shape = dict(evidence_payload)
    singular_shape["selected_output_schema_sha256"] = "a" * 64
    singular_shape["selected_output_required"] = True
    singular_shape["invocation_sha256"] = hashlib.sha256(
        _canonical_bytes(
            {
                key: value
                for key, value in singular_shape.items()
                if key != "invocation_sha256"
            }
        )
    ).hexdigest()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MillforgeInvocationEvidence.model_validate(singular_shape)

    old_descriptor_payload = runner.descriptor.model_dump(mode="json")
    old_descriptor_payload["runner_version"] = 1
    old_descriptor_payload["artifact_contract_version"] = (
        "millforge.runtime-artifacts.v1"
    )
    runner._descriptor = _rehash_descriptor(old_descriptor_payload)
    with pytest.raises(MillforgeBaseBindingError) as caught:
        runner.invocation_evidence_for(request)
    assert caught.value.reason == "descriptor_composition"


def test_invocation_evidence_changes_for_every_dynamic_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    descriptor = describe_millforge_base()
    original = identity._build_invocation_evidence(
        components,
        descriptor,
        request_id="request-evidence",
        run_id="run-evidence",
    )
    metadata_fields = (
        ("effective_prompt_sha256", "1" * 64),
        ("context_sha256", "2" * 64),
        ("cwd_sha256", "3" * 64),
        ("context_file_count", components.metadata.context_file_count + 1),
        ("context_truncated", not components.metadata.context_truncated),
        ("prompt_truncated", not components.metadata.prompt_truncated),
    )
    variants = [
        replace(
            components,
            compiled_plan=finalize_compiled_plan_sha256(
                components.compiled_plan.model_copy(update={"source_sha256": "4" * 64})
            ),
        ),
        replace(
            components,
            model_profile=components.model_profile.model_copy(
                update={"maximum_output_tokens": 1234}
            ),
        ),
        replace(
            components,
            capability_envelope=CapabilityEnvelope(
                grants=(CapabilityGrant(capability_id="different"),)
            ),
        ),
        *(
            replace(
                components,
                metadata=components.metadata.model_copy(update={name: value}),
            )
            for name, value in metadata_fields
        ),
    ]

    assert all(
        identity._build_invocation_evidence(
            variant,
            descriptor,
            request_id="request-evidence",
            run_id="run-evidence",
        ).invocation_sha256
        != original.invocation_sha256
        for variant in variants
    )
    assert (
        identity._build_invocation_evidence(
            components,
            descriptor,
            request_id="other-request-evidence",
            run_id="run-evidence",
        ).invocation_sha256
        != original.invocation_sha256
    )
    assert (
        identity._build_invocation_evidence(
            components,
            descriptor,
            request_id="request-evidence",
            run_id="other-run-evidence",
        ).invocation_sha256
        != original.invocation_sha256
    )
    selected = TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        ),
    )
    selected_evidence = identity._build_invocation_evidence(
        components,
        descriptor,
        request_id="request-evidence",
        run_id="run-evidence",
        selected_output_requirements=(selected,),
    )
    expected_selected_digest = hashlib.sha256(
        _canonical_bytes(
            [
                {
                    "required": True,
                    "schema_sha256": selected.selected_output.schema_sha256,
                    "terminal_result": "COMPLETE",
                }
            ]
        )
    ).hexdigest()
    assert (
        selected_evidence.selected_output_requirements_sha256
        == expected_selected_digest
    )
    assert selected_evidence.invocation_sha256 != original.invocation_sha256
    optional = selected.model_copy(
        update={
            "selected_output": selected.selected_output.model_copy(
                update={"required": False}
            )
        }
    )
    optional_evidence = identity._build_invocation_evidence(
        components,
        descriptor,
        request_id="request-evidence",
        run_id="run-evidence",
        selected_output_requirements=(optional,),
    )
    assert (
        optional_evidence.selected_output_requirements_sha256
        != selected_evidence.selected_output_requirements_sha256
    )
    assert optional_evidence.invocation_sha256 != selected_evidence.invocation_sha256
    assert describe_millforge_base() == descriptor


def test_runner_invocation_evidence_carries_request_local_selected_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    descriptor = describe_millforge_base()
    requirement = TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=SelectedOutputRequirement(
            required=False,
            json_schema={"type": "array", "items": {"type": "string"}},
        ),
    )
    request = _request(components, tmp_path).model_copy(
        update={"selected_output_requirements": (requirement,)}
    )

    evidence = runner.invocation_evidence_for(request)

    assert evidence.selected_output_requirements_sha256 is not None
    assert "json_schema" not in evidence.model_dump(mode="json")
    assert describe_millforge_base() == descriptor


def test_selected_output_requirement_collection_digest_is_canonical_and_sensitive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    components = _components(monkeypatch, tmp_path)
    descriptor = describe_millforge_base()
    alpha = TerminalSelectedOutputRequirement(
        terminal_result="BLOCKED",
        selected_output=SelectedOutputRequirement(
            required=False,
            json_schema={"enum": [None, "blocked"]},
        ),
    )
    beta = TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={"const": "complete"},
        ),
    )

    def evidence(
        requirements: tuple[TerminalSelectedOutputRequirement, ...],
    ) -> MillforgeInvocationEvidence:
        return identity._build_invocation_evidence(
            components,
            descriptor,
            request_id="request-evidence",
            run_id="run-evidence",
            selected_output_requirements=requirements,
        )

    ordered = evidence((alpha, beta))
    reordered = evidence((beta, alpha))
    expected = hashlib.sha256(
        _canonical_bytes(
            [
                {
                    "required": False,
                    "schema_sha256": alpha.selected_output.schema_sha256,
                    "terminal_result": "BLOCKED",
                },
                {
                    "required": True,
                    "schema_sha256": beta.selected_output.schema_sha256,
                    "terminal_result": "COMPLETE",
                },
            ]
        )
    ).hexdigest()

    assert ordered.selected_output_requirements_sha256 == expected
    assert reordered.selected_output_requirements_sha256 == expected
    assert reordered.invocation_sha256 == ordered.invocation_sha256
    variants = (
        (
            alpha.model_copy(update={"terminal_result": "REJECTED"}),
            beta,
        ),
        (
            alpha.model_copy(
                update={
                    "selected_output": SelectedOutputRequirement(
                        required=False,
                        json_schema={"const": "different"},
                    )
                }
            ),
            beta,
        ),
        (
            alpha.model_copy(
                update={
                    "selected_output": alpha.selected_output.model_copy(
                        update={"required": True}
                    )
                }
            ),
            beta,
        ),
    )
    assert all(
        evidence(variant).selected_output_requirements_sha256 != expected
        for variant in variants
    )


def test_model_behavior_excludes_secret_references_and_evidence_leaks_no_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    descriptor = describe_millforge_base()
    original = identity._build_invocation_evidence(
        components,
        descriptor,
        request_id="request-evidence",
        run_id="run-evidence",
    )
    secret = SecretRef(secret_id="raw-secret-reference", env_var="RAW_SECRET_ENV")
    authentication = components.model_profile.authentication.model_copy(
        update={"secret_ref": secret}
    )
    changed_secret = replace(
        components,
        model_profile=components.model_profile.model_copy(
            update={"authentication": authentication}
        ),
    )
    same_behavior = identity._build_invocation_evidence(
        changed_secret,
        descriptor,
        request_id="request-evidence",
        run_id="run-evidence",
    )
    canonical = _canonical_bytes(
        {
            "descriptor": descriptor.model_dump(mode="json"),
            "evidence": original.model_dump(mode="json"),
        }
    )

    assert same_behavior.model_behavior_sha256 == original.model_behavior_sha256
    for prohibited in (
        b"raw-secret-reference",
        b"RAW_SECRET_ENV",
        b"compat-a.test",
        str(tmp_path).encode("utf-8"),
        b"dynamic context",
        b"model output",
        b"tool output",
    ):
        assert prohibited not in canonical


@pytest.mark.parametrize(
    ("field_name", "mutate"),
    (
        (
            "provider_id",
            lambda profile: profile.model_copy(
                update={"provider_id": "other-provider"}
            ),
        ),
        (
            "model_id",
            lambda profile: profile.model_copy(update={"model_id": "other-model"}),
        ),
        (
            "transport_id",
            lambda profile: profile.model_copy(
                update={"transport_id": "other.transport"}
            ),
        ),
        (
            "endpoint",
            lambda profile: profile.model_copy(
                update={
                    "endpoint": profile.endpoint.model_copy(
                        update={"allow_missing_success_content_type": True}
                    )
                }
            ),
        ),
        (
            "authentication",
            lambda profile: profile.model_copy(
                update={
                    "authentication": AuthenticationPolicy(
                        scheme=AuthenticationScheme.HEADER,
                        secret_ref=profile.authentication.secret_ref,
                        header_name="X-API-Key",
                        allowed_custom_header_names=("x-api-key",),
                    )
                }
            ),
        ),
        (
            "configured_headers",
            lambda profile: profile.model_copy(
                update={
                    "configured_headers": profile.configured_headers.model_copy(
                        update={"values": {"X-Test": "changed"}}
                    )
                }
            ),
        ),
        (
            "timeout_seconds",
            lambda profile: profile.model_copy(update={"timeout_seconds": 61.0}),
        ),
        (
            "maximum_output_tokens",
            lambda profile: profile.model_copy(update={"maximum_output_tokens": 2048}),
        ),
        (
            "sampling",
            lambda profile: profile.model_copy(
                update={
                    "sampling": profile.sampling.model_copy(update={"temperature": 0.5})
                }
            ),
        ),
        (
            "reasoning",
            lambda profile: profile.model_copy(
                update={
                    "reasoning": profile.reasoning.model_copy(
                        update={"mode": ReasoningMode.ENABLED}
                    )
                }
            ),
        ),
        (
            "capabilities",
            lambda profile: profile.model_copy(
                update={
                    "capabilities": profile.capabilities.model_copy(
                        update={
                            "support": {
                                **profile.capabilities.support,
                                "usage_reporting": CapabilitySupport.UNSUPPORTED,
                            }
                        }
                    )
                }
            ),
        ),
        (
            "request_options",
            lambda profile: profile.model_copy(
                update={
                    "request_options": profile.request_options.model_copy(
                        update={"allowed_options": ("user",)}
                    )
                }
            ),
        ),
        (
            "error_mappings",
            lambda profile: profile.model_copy(
                update={
                    "error_mappings": profile.error_mappings.model_copy(
                        update={"code_paths": ("error.code",)}
                    )
                }
            ),
        ),
        (
            "transport",
            lambda profile: profile.model_copy(
                update={
                    "transport": profile.transport.model_copy(
                        update={"success_body_limit_bytes": 1024}
                    )
                }
            ),
        ),
    ),
)
def test_model_behavior_hash_covers_every_retained_behavior_field(
    field_name: str, mutate: Any
) -> None:
    del field_name
    profile = make_canonical_builder_profile_a()

    assert identity._model_behavior_sha256(mutate(profile)) != (
        identity._model_behavior_sha256(profile)
    )


def test_model_behavior_hash_ignores_source_diagnostics_and_secret_references() -> None:
    profile = make_canonical_builder_profile_a()
    original = identity._model_behavior_sha256(profile)
    changed_source = profile.model_copy(
        update={"source_name": "other-source", "source_digest": "other-digest"}
    )
    changed_secret = profile.model_copy(
        update={
            "authentication": profile.authentication.model_copy(
                update={
                    "secret_ref": SecretRef(
                        secret_id="other-secret", env_var="OTHER_SECRET"
                    )
                }
            )
        }
    )

    assert identity._model_behavior_sha256(changed_source) == original
    assert identity._model_behavior_sha256(changed_secret) == original


def test_replay_field_changes_behavior_hash_and_forge_provenance_changes_descriptor() -> (
    None
):
    profile = make_canonical_builder_profile_a()
    legacy_hash = identity._model_behavior_sha256(profile)
    replay_profile = profile.model_copy(
        update={
            "reasoning": ReasoningPolicy(
                mode=ReasoningMode.ENABLED,
                mode_field="thinking",
                mode_values={ReasoningMode.ENABLED: {"type": "enabled"}},
                tool_call_replay_field="reasoning_content",
            )
        }
    )

    assert legacy_hash == (
        "312ea21de480cb8221ea4329d169031e106118c546b365d912e6f5090fdbc0e7"
    )
    assert identity._model_behavior_sha256(replay_profile) != legacy_hash
    descriptor = describe_millforge_base()
    assert descriptor.forge_provenance_sha256 != (
        "239da2e99f843bd29a5ccc5fda8ffbfbb635b94c0e1dc077b67cd031ddd2dd48"
    )
    assert descriptor.descriptor_sha256 != (
        "a44cc37e4ea67208e21ed3333c9807e566c81cc0fc98452fa44f1fe0da2608fb"
    )
    assert descriptor.schema_version == "1.0"
    assert descriptor.runner_version == 2
    assert descriptor.context_contract_version == "millforge-base.context.v1"
    assert descriptor.artifact_contract_version == "millforge.runtime-artifacts.v2"


def test_runner_descriptor_is_read_only_and_request_evidence_is_stateless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    descriptor = runner.descriptor
    request = _request(components, tmp_path)
    first_evidence = runner.invocation_evidence_for(request)
    second_evidence = runner.invocation_evidence_for(request)

    assert runner.descriptor is descriptor
    assert first_evidence == second_evidence
    assert first_evidence is not second_evidence
    with pytest.raises(AttributeError):
        runner.descriptor = descriptor  # type: ignore[misc]
    with pytest.raises(AttributeError):
        runner.invocation_evidence_for = lambda _request: first_evidence  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("profile_id", "other-profile"),
        ("provider_id", "other-provider"),
        ("model_id", "other-model"),
        ("transport_id", "other.transport"),
    ),
)
def test_profile_binding_drift_is_rejected_before_evidence_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    value: str,
) -> None:
    components = _components(monkeypatch, tmp_path)
    tampered = replace(
        components,
        model_profile=components.model_profile.model_copy(update={field_name: value}),
    )
    evidence_calls: list[object] = []

    def prohibited_evidence(*args: object, **kwargs: object) -> None:
        evidence_calls.append((args, kwargs))
        raise AssertionError("evidence constructed before profile binding verification")

    monkeypatch.setattr(
        runner_module, "_build_invocation_evidence", prohibited_evidence
    )
    with pytest.raises(MillforgeBaseBindingError) as caught:
        create_millforge_base_runner(
            components=tampered,
            services=MillforgeBaseRuntimeServices(
                model_client=FakeModelClient(),
                clock=FakeClock(),
                cancellation_resolver=FakeCancellationResolver(),
            ),
        )

    assert caught.value.reason == "backend_composition"
    assert evidence_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("profile_id", "other-profile"),
        ("provider_id", "other-provider"),
        ("model_id", "other-model"),
        ("transport_id", "other.transport"),
    ),
)
async def test_profile_binding_tampering_is_rejected_before_execution_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
    value: str,
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    runner._components = replace(
        components,
        model_profile=components.model_profile.model_copy(update={field_name: value}),
    )
    monkeypatch.setattr(
        runner_module,
        "_load_invocation_executor",
        lambda: pytest.fail("executor loaded before profile binding verification"),
    )

    with pytest.raises(MillforgeBaseBindingError) as caught:
        await runner.execute(_request(components, tmp_path / "run"))

    assert caught.value.reason == "backend_composition"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("descriptor_digest", "descriptor"))
async def test_tampering_is_rejected_before_any_execution_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    components, runner = _runner(monkeypatch, tmp_path)
    if target == "descriptor_digest":
        runner._descriptor = runner.descriptor.model_copy(
            update={"descriptor_sha256": "f" * 64}
        )
        expected_reason = "descriptor_hash"
    elif target == "descriptor":
        payload = runner.descriptor.model_dump(mode="json")
        payload["runner_id"] = "other"
        runner._descriptor = _rehash_descriptor(payload)
        expected_reason = "descriptor_composition"
    monkeypatch.setattr(
        runner_module,
        "_load_invocation_executor",
        lambda: pytest.fail("executor loaded before binding verification"),
    )
    with pytest.raises(MillforgeBaseBindingError) as caught:
        await runner.execute(_request(components, tmp_path / "run"))
    assert caught.value.reason == expected_reason


def test_public_signatures_expose_only_public_millforge_types() -> None:
    assert tuple(inspect.signature(describe_millforge_base).parameters) == (
        "legal_terminal_results",
    )
    assert tuple(inspect.signature(create_millforge_base_runner).parameters) == (
        "components",
        "services",
    )
    for function in (describe_millforge_base, create_millforge_base_runner):
        signature = str(inspect.signature(function))
        assert "_forge" not in signature
        assert "Millrace" not in signature


def test_lower_level_public_signatures_remain_unchanged() -> None:
    assert tuple(inspect.signature(millforge_base_harness_source).parameters) == (
        "model_profile_id",
        "system_instructions",
    )
    assert tuple(inspect.signature(create_pi_compat_tool_registry).parameters) == ()
    assert tuple(inspect.signature(create_pi_compat_tool_snapshot).parameters) == ()
    executor_signature = inspect.signature(create_pi_compat_tool_executor)
    assert tuple(executor_signature.parameters) == (
        "plan",
        "cwd",
        "cancellation_resolver",
        "shell_config",
    )
    assert (
        executor_signature.parameters["plan"].kind
        is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert executor_signature.parameters["cwd"].kind is inspect.Parameter.KEYWORD_ONLY


def test_configured_terminal_vocabulary_has_one_canonical_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requested = ("REJECTED", "ESCALATED", "COMPLETE", "BLOCKED")
    expected = ("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")

    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=requested,
    )
    descriptor = describe_millforge_base(legal_terminal_results=requested)

    assert components.legal_terminal_results == expected
    assert descriptor.legal_terminal_result_ids == expected
    assert (
        tuple(
            sorted(
                node.terminal_result
                for node in components.harness_source.graph.nodes
                if node.terminal_result is not None
            )
        )
        == expected
    )
    assert (
        tuple(sorted(components.compiled_plan.terminal_result_map.values())) == expected
    )


@pytest.mark.parametrize(
    "legal_terminal_results",
    (
        (),
        ("COMPLETE", "COMPLETE"),
        ("invalid",),
        (1,),
        tuple(f"RESULT_{index}" for index in range(65)),
    ),
)
def test_configured_terminal_vocabulary_refuses_invalid_inputs(
    legal_terminal_results: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError):
        describe_millforge_base(
            legal_terminal_results=cast(tuple[str, ...], legal_terminal_results)
        )


def test_configured_terminal_vocabulary_public_signatures_are_keyword_only() -> None:
    default = ("BLOCKED", "COMPLETE", "REJECTED")
    for function in (
        describe_millforge_base,
        create_millforge_base_components,
        create_millforge_base_live_runner,
    ):
        parameter = inspect.signature(function).parameters["legal_terminal_results"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == default
    assert tuple(inspect.signature(create_millforge_base_runner).parameters) == (
        "components",
        "services",
    )


def test_default_terminal_contract_is_byte_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = ("BLOCKED", "COMPLETE", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=default,
    )
    descriptor = describe_millforge_base(legal_terminal_results=default)

    assert descriptor.descriptor_sha256 == (
        "dc67d572eb9c934e6acf0f073f27ff19eb3f3da6d46beab91a1cccce2640b981"
    )
    assert descriptor.tool_catalog_sha256 == (
        "5de78f0943c5ef169f971651fd3220308b2dee2fae9641919c262824cc92808a"
    )
    assert {
        (node.node_id, node.model_tool_name, node.terminal_result)
        for node in components.compiled_plan.nodes
        if node.terminal_result is not None
    } == {
        ("submit", "submit", "COMPLETE"),
        ("block", "block", "BLOCKED"),
        ("reject", "reject", "REJECTED"),
    }


def test_configured_terminal_catalog_plan_and_descriptor_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vocabulary = ("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=vocabulary,
    )
    descriptor = describe_millforge_base(legal_terminal_results=vocabulary)

    assert descriptor.tool_catalog_sha256 == components.tool_snapshot.snapshot_sha256
    assert tuple(sorted(components.compiled_plan.terminal_result_map.values())) == (
        "BLOCKED",
        "COMPLETE",
        "ESCALATED",
        "REJECTED",
    )
    assert tuple(
        node.model_tool_name
        for node in components.compiled_plan.nodes
        if node.terminal_result is not None
    ) == (
        "terminal_blocked",
        "terminal_complete",
        "terminal_escalated",
        "terminal_rejected",
    )


def test_configured_terminal_vocabulary_is_order_canonical_and_digest_bound() -> None:
    first = describe_millforge_base(
        legal_terminal_results=("REJECTED", "ESCALATED", "COMPLETE", "BLOCKED")
    )
    reordered = describe_millforge_base(
        legal_terminal_results=("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")
    )
    changed = describe_millforge_base(
        legal_terminal_results=("BLOCKED", "COMPLETE", "REJECTED", "REVIEW")
    )

    assert first == reordered
    assert first.tool_catalog_sha256 != changed.tool_catalog_sha256
    assert first.descriptor_sha256 != changed.descriptor_sha256
