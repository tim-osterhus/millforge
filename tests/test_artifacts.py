"""Artifact output tests for the real ``RuntimeArtifactWriter``.

Tests:
- Complete 7-artifact tree produced with correct content.
- Partial tree on early failure (only execution_summary, metrics,
  manifest written; no terminal_result).
- Atomic replace — write failure does not corrupt original file.
- Path-traversal attacks rejected (absolute, ``..``, symlink escape).
- JSONL validation — each line independently valid JSON, correct
  newline separators, partial final line detection.
- Manifest verification — all entries present, correct sizes/hashes,
  no self-reference.
- Byte-determinism — two runs under identical fixed clock produce
  byte-identical files.

All tests use deterministic implementations (no real backend, model,
tool, or network). No Forge, provider SDK, or network imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from millforge.artifacts import (
    STANDARD_ARTIFACT_FILENAMES,
    STANDARD_ARTIFACT_IDS,
    RuntimeArtifactWriter,
)
from millforge.contracts import (
    ArtifactRef,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    StageIdentity,
)
from millforge.exceptions import ArtifactWriteError
from tests.conftest import (
    SHA_B,
    make_test_session_event,
    make_test_tool_trace_record,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from collections.abc import Generator


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for artifact tests."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _make_ref(artifact_id: str, path: str | None = None) -> ArtifactRef:
    """Build an ArtifactRef for the given artifact_id."""
    if path is not None:
        return ArtifactRef(artifact_id=artifact_id, path=Path(path))
    filename = STANDARD_ARTIFACT_FILENAMES[artifact_id]
    return ArtifactRef(
        artifact_id=artifact_id,
        path=Path(f"millforge/{filename}"),
    )


def _make_terminal_result_data() -> dict[str, Any]:
    """Generate terminal result data for testing."""
    return {
        "schema_version": "1.0",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "stage": StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ).model_dump(mode="json"),
        "terminal_result": "success",
        "result_class": ExecutionResultClass.DOMAIN_TERMINAL.value,
        "summary_artifact_paths": ("millforge/execution_summary.json",),
        "compiled_harness_sha256": SHA_B,
    }


def _make_execution_summary_data() -> dict[str, Any]:
    """Generate execution summary data for testing."""
    return {
        "schema_version": "1.0",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "stage": StageIdentity(
            plane="execution", node_id="builder", stage_kind_id="builder"
        ).model_dump(mode="json"),
        "status": ExecutionStatus.COMPLETED.value,
        "result_class": ExecutionResultClass.DOMAIN_TERMINAL.value,
        "diagnostic_error_code": None,
    }


def _make_metrics_data() -> dict[str, Any]:
    """Generate metrics data for testing."""
    return {
        "schema_version": "1.0",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "session_id": "sess-test-001",
        "status": "terminal",
        "usage": {
            "model_calls": 5,
            "token_usage": {
                "input_tokens": 150,
                "output_tokens": 42,
                "provider_reported": True,
                "total_tokens": 192,
            },
            "tool_calls": 3,
        },
    }


def _make_events_data() -> list[dict[str, Any]]:
    """Generate events data for testing."""
    return [
        make_test_session_event(sequence=1).model_dump(mode="json"),
        make_test_session_event(sequence=2).model_dump(mode="json"),
    ]


def _make_tool_trace_data() -> list[dict[str, Any]]:
    """Generate tool trace data for testing."""
    return [
        make_test_tool_trace_record().model_dump(mode="json"),
    ]


def _make_manifest_data(
    artifact_entries: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Generate manifest data for testing."""
    if artifact_entries is None:
        artifact_entries = [
            {"artifact_id": "terminal_result"},
            {"artifact_id": "execution_summary"},
            {"artifact_id": "events"},
            {"artifact_id": "tool_trace"},
            {"artifact_id": "metrics"},
        ]
    return {
        "schema_version": "1.0",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "artifacts": artifact_entries,
    }


def _make_diagnostic_data() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "diagnostic": DiagnosticMetadata(
            error_code="test",
            category="internal",
            message="Diagnostic message",
            retryable=False,
            origin="test",
        ).model_dump(mode="json"),
    }


def _read_file(path: Path) -> bytes:
    """Read a file as bytes."""
    return path.read_bytes()


def _read_text(path: Path) -> str:
    """Read a file as text."""
    return path.read_text(encoding="utf-8")


def _sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content).hexdigest()


# ======================================================================
# Complete 7-artifact tree
# ======================================================================


@pytest.mark.asyncio
async def test_complete_7_artifact_tree(temp_dir: Path) -> None:
    """All 7 standard artifacts are produced with correct content."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    # Write all 7 artifacts
    await writer.write_terminal_result(
        _make_ref("terminal_result"), _make_terminal_result_data()
    )
    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )
    await writer.write_events(_make_ref("events"), _make_events_data())
    await writer.write_tool_trace(_make_ref("tool_trace"), _make_tool_trace_data())
    await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())
    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"), _make_manifest_data()
    )
    await writer.write_diagnostic(
        _make_ref("diagnostic"),
        _make_diagnostic_data(),
    )

    # Verify all files exist
    millforge_dir = temp_dir / "millforge"
    assert (millforge_dir / "terminal_result.json").exists()
    assert (millforge_dir / "execution_summary.json").exists()
    assert (millforge_dir / "events.jsonl").exists()
    assert (millforge_dir / "tool_trace.jsonl").exists()
    assert (millforge_dir / "metrics.json").exists()
    assert (millforge_dir / "artifact_manifest.json").exists()
    assert (millforge_dir / "diagnostic.json").exists()

    # Verify content of terminal_result.json
    terminal = json.loads(_read_text(millforge_dir / "terminal_result.json"))
    assert terminal["request_id"] == "req-test-001"
    assert terminal["terminal_result"] == "success"

    # Verify content of execution_summary.json
    summary = json.loads(_read_text(millforge_dir / "execution_summary.json"))
    assert summary["status"] == "completed"

    # Verify events.jsonl (each line independently valid JSON)
    events_text = _read_text(millforge_dir / "events.jsonl")
    events_lines = events_text.strip().split("\n")
    assert len(events_lines) == 2
    for line in events_lines:
        record = json.loads(line)
        assert "schema_version" in record
    assert len(writer.read_events(_make_ref("events"))) == 2
    # Each line must end with newline
    assert events_text.endswith("\n")

    # Verify tool_trace.jsonl
    trace_text = _read_text(millforge_dir / "tool_trace.jsonl")
    trace_lines = trace_text.strip().split("\n")
    assert len(trace_lines) == 1
    trace_record = json.loads(trace_lines[0])
    assert trace_record["model_tool_name"] == "get_weather"
    assert len(writer.read_tool_trace(_make_ref("tool_trace"))) == 1

    # Verify metrics.json
    metrics = json.loads(_read_text(millforge_dir / "metrics.json"))
    assert set(metrics["usage"]) == {"model_calls", "token_usage", "tool_calls"}
    assert metrics["usage"]["token_usage"]["input_tokens"] == 150

    # Verify diagnostic.json
    diagnostic = json.loads(_read_text(millforge_dir / "diagnostic.json"))
    assert diagnostic["diagnostic"]["error_code"] == "test"

    # Verify manifest.json
    manifest = json.loads(_read_text(millforge_dir / "artifact_manifest.json"))
    assert "artifacts" in manifest
    assert manifest["request_id"] == "req-test-001"
    assert all(entry["failure_code"] is None for entry in manifest["artifacts"])

    # Verify tracked artifacts
    tracked = writer.tracked_artifacts
    assert len(tracked) == 6  # diagnostic tracked but manifest not tracking itself
    assert "terminal_result" in tracked
    assert "execution_summary" in tracked
    assert "diagnostic" in tracked
    assert tracked["diagnostic"]["failure_code"] is None


# ======================================================================
# Partial tree on early failure
# ======================================================================


@pytest.mark.asyncio
async def test_partial_tree_on_early_failure(temp_dir: Path) -> None:
    """Simulate early failure: only execution_summary, metrics, manifest
    written; no terminal_result."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    # Only write artifacts that are written for every return path
    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )
    await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())
    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"),
        _make_manifest_data(
            [{"artifact_id": "execution_summary"}, {"artifact_id": "metrics"}]
        ),
    )

    millforge_dir = temp_dir / "millforge"
    assert (millforge_dir / "execution_summary.json").exists()
    assert (millforge_dir / "metrics.json").exists()
    assert (millforge_dir / "artifact_manifest.json").exists()

    # terminal_result should NOT exist (early failure path)
    assert not (millforge_dir / "terminal_result.json").exists()
    # events and tool_trace should not exist either
    assert not (millforge_dir / "events.jsonl").exists()
    assert not (millforge_dir / "tool_trace.jsonl").exists()

    # Verify tracked artifacts
    tracked = writer.tracked_artifacts
    assert "execution_summary" in tracked
    assert "metrics" in tracked
    assert "terminal_result" not in tracked


# ======================================================================
# Atomic replace — write failure does not corrupt original file
# ======================================================================


def test_atomic_replace_write_failure_does_not_corrupt_original(temp_dir: Path) -> None:
    """If the atomic write fails mid-way, the original file is preserved."""
    millforge_dir = temp_dir / "millforge"
    millforge_dir.mkdir(parents=True, exist_ok=True)
    target = millforge_dir / "metrics.json"

    # Write original content
    original_content = json.dumps(
        {"status": "original"}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    target.write_bytes(original_content)
    original_hash = _sha256(original_content)

    # Create a writer
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    # The atomic write uses a temp file in the same directory then renames.
    # To simulate a failure, we make the millforge dir non-writable
    # _after_ creating the writer.
    try:
        # Make millforge dir read-only (on Unix)
        os.chmod(str(millforge_dir), 0o444)

        with pytest.raises(ArtifactWriteError):
            writer._atomic_write(target, b"new content that should fail")

        # Restore permissions so we can read the original
        os.chmod(str(millforge_dir), 0o755)

        # Original file must be unchanged
        assert target.read_bytes() == original_content
        assert _sha256(target.read_bytes()) == original_hash
    finally:
        try:
            os.chmod(str(millforge_dir), 0o755)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_atomic_replace_write_method_preserves_original_on_failure(
    temp_dir: Path,
) -> None:
    """write_metrics (which uses _atomic_write internally) preserves
    original file on write failure."""
    millforge_dir = temp_dir / "millforge"
    millforge_dir.mkdir(parents=True, exist_ok=True)
    target = millforge_dir / "metrics.json"

    original_content = json.dumps(
        {"status": "original"}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    target.write_bytes(original_content)
    original_hash = _sha256(original_content)

    # Use the artifact writer
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    try:
        os.chmod(str(millforge_dir), 0o444)

        with pytest.raises(ArtifactWriteError):
            await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())

        # Restore permissions so we can read the original
        os.chmod(str(millforge_dir), 0o755)

        assert target.read_bytes() == original_content
        assert _sha256(target.read_bytes()) == original_hash
    finally:
        try:
            os.chmod(str(millforge_dir), 0o755)
        except OSError:
            pass


# ======================================================================
# Path-traversal rejection
# ======================================================================


def test_path_traversal_rejects_absolute_path(temp_dir: Path) -> None:
    """Absolute artifact paths are rejected."""
    writer = RuntimeArtifactWriter(temp_dir)

    with pytest.raises(ArtifactWriteError, match="Absolute.*rejected"):
        writer._resolve_target(Path("/etc/passwd"))


def test_path_traversal_rejects_dot_dot(temp_dir: Path) -> None:
    """'..' traversal in artifact paths is rejected."""
    writer = RuntimeArtifactWriter(temp_dir)

    with pytest.raises(ArtifactWriteError, match="Path traversal.*rejected"):
        writer._resolve_target(Path("millforge/../../../etc/passwd"))


def test_path_traversal_rejects_dot_dot_simple(temp_dir: Path) -> None:
    """Simple '..' in path is rejected."""
    writer = RuntimeArtifactWriter(temp_dir)

    with pytest.raises(ArtifactWriteError, match="Path traversal.*rejected"):
        writer._resolve_target(Path("millforge/../../secrets.json"))


def test_path_traversal_rejects_escape_via_resolve(temp_dir: Path) -> None:
    """Path that resolves outside millforge/ is rejected.

    We create a symlink inside millforge/ that points outside, then verify
    _resolve_target catches it.
    """
    millforge_dir = temp_dir / "millforge"
    millforge_dir.mkdir(parents=True, exist_ok=True)

    # Create a symlink in millforge/ that points to /tmp
    link_path = millforge_dir / "escape_link"
    link_path.symlink_to(temp_dir.parent, target_is_directory=True)

    writer = RuntimeArtifactWriter(temp_dir)

    # Use the symlink ref path: the resolved path will be outside millforge/
    with pytest.raises(ArtifactWriteError, match="Path escape.*rejected"):
        writer._resolve_target(Path("millforge/escape_link/secrets.json"))


@pytest.mark.asyncio
async def test_path_traversal_via_write_method(temp_dir: Path) -> None:
    """Writing to a path outside millforge/ via any write method raises."""
    writer = RuntimeArtifactWriter(temp_dir)

    with pytest.raises(
        ArtifactWriteError,
        match="Path escape.*rejected|Absolute.*rejected|Path traversal.*rejected",
    ):
        await writer.write_metrics(
            ArtifactRef(
                artifact_id="metrics",
                path=Path("../../../etc/passwd"),
            ),
            _make_metrics_data(),
        )


# ======================================================================
# JSONL validation
# ======================================================================


def test_jsonl_each_line_independently_valid(temp_dir: Path) -> None:
    """_serialize_jsonl produces independently valid JSON per line."""
    writer = RuntimeArtifactWriter(temp_dir)

    records = [
        {"a": 1, "b": 2},
        {"x": "hello", "y": [1, 2, 3]},
        {"nested": {"key": "value"}},
    ]

    content = writer._serialize_jsonl(records)
    text = content.decode("utf-8")

    # Each line must be independently valid JSON followed by \n
    lines = text.split("\n")
    # Last element is empty (trailing newline)
    assert lines[-1] == "", f"Expected trailing empty string, got {lines[-1]!r}"
    non_empty = lines[:-1]
    assert len(non_empty) == len(records)

    for i, line in enumerate(non_empty):
        parsed = json.loads(line)
        assert parsed == records[i], f"Line {i} content mismatch"
        # The line must not have leading spaces
        assert line == line.strip(), f"Line {i} has leading/trailing whitespace"


def test_jsonl_empty_input_returns_empty(temp_dir: Path) -> None:
    """Empty input to _serialize_jsonl returns empty bytes."""
    writer = RuntimeArtifactWriter(temp_dir)
    content = writer._serialize_jsonl([])
    assert content == b""


def test_jsonl_trailing_newline(temp_dir: Path) -> None:
    """_serialize_jsonl output ends with a single newline."""
    writer = RuntimeArtifactWriter(temp_dir)
    content = writer._serialize_jsonl([{"key": "value"}])
    text = content.decode("utf-8")
    assert text.endswith("\n"), "JSONL must end with newline"
    assert text.count("\n") == 1, "Expected exactly one newline"


def test_jsonl_partial_final_line_impossible(temp_dir: Path) -> None:
    """Because serialization is all-or-nothing, partial final line
    cannot occur. We verify the output is consistent."""
    writer = RuntimeArtifactWriter(temp_dir)
    content = writer._serialize_jsonl([{"a": 1}, {"b": 2}])
    text = content.decode("utf-8")

    # Split and verify each line is complete
    lines = text.split("\n")
    non_empty = [ln for ln in lines if ln]
    assert len(non_empty) == 2
    json.loads(non_empty[0])  # must not raise
    json.loads(non_empty[1])  # must not raise


def test_read_events_rejects_partial_final_line(temp_dir: Path) -> None:
    """JSONL readers reject files without a final newline."""
    writer = RuntimeArtifactWriter(temp_dir)
    target = temp_dir / "millforge" / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(make_test_session_event().model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactWriteError, match="partial final line"):
        writer.read_events(_make_ref("events"))


def test_read_events_rejects_duplicate_diagnostic_keys(temp_dir: Path) -> None:
    """Duplicate DiagnosticField keys are rejected through SessionEvent validation."""
    writer = RuntimeArtifactWriter(temp_dir)
    target = temp_dir / "millforge" / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = make_test_session_event().model_dump(mode="json")
    payload["fields"] = [
        {"key": "reason", "value": "first"},
        {"key": "reason", "value": "second"},
    ]
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ArtifactWriteError, match="diagnostic field keys"):
        writer.read_events(_make_ref("events"))


def test_read_tool_trace_rejects_off_contract_shape(temp_dir: Path) -> None:
    """Legacy or off-contract trace records are rejected."""
    writer = RuntimeArtifactWriter(temp_dir)
    target = temp_dir / "millforge" / "tool_trace.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"trace_id": "old", "tool_name": "legacy"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactWriteError, match="invalid at line"):
        writer.read_tool_trace(_make_ref("tool_trace"))


# ======================================================================
# Manifest verification
# ======================================================================


@pytest.mark.asyncio
async def test_manifest_lists_all_artifacts_except_self(temp_dir: Path) -> None:
    """Manifest lists all artifacts except manifest.json itself."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    await writer.write_terminal_result(
        _make_ref("terminal_result"), _make_terminal_result_data()
    )
    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )
    await writer.write_events(_make_ref("events"), _make_events_data())
    await writer.write_tool_trace(_make_ref("tool_trace"), _make_tool_trace_data())
    await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())

    # Write manifest
    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"),
        _make_manifest_data(
            [
                {"artifact_id": "terminal_result"},
                {"artifact_id": "execution_summary"},
                {"artifact_id": "events"},
                {"artifact_id": "tool_trace"},
                {"artifact_id": "metrics"},
            ]
        ),
    )

    millforge_dir = temp_dir / "millforge"
    manifest_path = millforge_dir / "artifact_manifest.json"
    manifest = json.loads(_read_text(manifest_path))

    # Verifiy each artifact entry has required fields
    assert "artifacts" in manifest
    for entry in manifest["artifacts"]:
        assert "artifact_id" in entry
        assert "byte_size" in entry, f"Missing byte_size for {entry['artifact_id']}"
        assert "sha256_hex" in entry, f"Missing sha256_hex for {entry['artifact_id']}"
        assert "complete" in entry, f"Missing complete for {entry['artifact_id']}"
        assert "producer" in entry, f"Missing producer for {entry['artifact_id']}"
        assert entry["complete"] is True
        assert entry["producer"] == "test/v1"
        assert isinstance(entry["byte_size"], int)
        assert entry["byte_size"] > 0
        assert isinstance(entry["sha256_hex"], str)
        assert len(entry["sha256_hex"]) == 64

    # No self-reference: manifest.json should not appear in artifacts list
    artifact_ids = [a["artifact_id"] for a in manifest["artifacts"]]
    assert "artifact_manifest" not in artifact_ids, "Manifest must not reference itself"


@pytest.mark.asyncio
async def test_manifest_byte_size_and_sha256_are_correct(temp_dir: Path) -> None:
    """Each manifest entry has correct byte_size and sha256_hex."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    await writer.write_terminal_result(
        _make_ref("terminal_result"), _make_terminal_result_data()
    )
    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )
    await writer.write_events(_make_ref("events"), _make_events_data())
    await writer.write_tool_trace(_make_ref("tool_trace"), _make_tool_trace_data())
    await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())

    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"),
        _make_manifest_data(
            [
                {"artifact_id": "terminal_result"},
                {"artifact_id": "execution_summary"},
                {"artifact_id": "events"},
                {"artifact_id": "tool_trace"},
                {"artifact_id": "metrics"},
            ]
        ),
    )

    millforge_dir = temp_dir / "millforge"
    manifest = json.loads(_read_text(millforge_dir / "artifact_manifest.json"))

    # Verify each entry's byte_size and sha256_hex matches actual file
    for entry in manifest["artifacts"]:
        aid = entry["artifact_id"]
        filename = STANDARD_ARTIFACT_FILENAMES[aid]
        file_path = millforge_dir / filename
        assert file_path.exists(), f"File {filename} does not exist"

        actual_bytes = file_path.read_bytes()
        actual_size = len(actual_bytes)
        actual_hash = hashlib.sha256(actual_bytes).hexdigest()

        assert entry["byte_size"] == actual_size, (
            f"byte_size mismatch for {aid}: "
            f"manifest={entry['byte_size']}, actual={actual_size}"
        )
        assert entry["sha256_hex"] == actual_hash, (
            f"sha256_hex mismatch for {aid}: "
            f"manifest={entry['sha256_hex']}, actual={actual_hash}"
        )


@pytest.mark.asyncio
async def test_manifest_no_self_reference(temp_dir: Path) -> None:
    """Manifest explicitly does not include itself in artifacts list."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    # Write one artifact
    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )

    # Write manifest with only execution_summary
    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"),
        _make_manifest_data([{"artifact_id": "execution_summary"}]),
    )

    millforge_dir = temp_dir / "millforge"
    manifest = json.loads(_read_text(millforge_dir / "artifact_manifest.json"))

    artifact_ids = [a["artifact_id"] for a in manifest["artifacts"]]
    assert "artifact_manifest" not in artifact_ids

    # Verify tracked_artifacts also doesn't contain manifest
    tracked = writer.tracked_artifacts
    assert "artifact_manifest" not in tracked


@pytest.mark.asyncio
async def test_failure_path_manifest_lists_only_written_non_terminal_artifacts(
    temp_dir: Path,
) -> None:
    """Failure-path manifest excludes terminal_result and manifest itself."""
    writer = RuntimeArtifactWriter(temp_dir, producer="test/v1")

    await writer.write_execution_summary(
        _make_ref("execution_summary"), _make_execution_summary_data()
    )
    await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())
    await writer.write_diagnostic(_make_ref("diagnostic"), _make_diagnostic_data())
    await writer.write_artifact_manifest(
        _make_ref("artifact_manifest"),
        _make_manifest_data(
            [
                {"artifact_id": "execution_summary"},
                {"artifact_id": "metrics"},
                {"artifact_id": "diagnostic"},
            ]
        ),
    )

    millforge_dir = temp_dir / "millforge"
    assert not (millforge_dir / "terminal_result.json").exists()

    manifest = json.loads(_read_text(millforge_dir / "artifact_manifest.json"))
    artifact_ids = {entry["artifact_id"] for entry in manifest["artifacts"]}
    assert artifact_ids == {"execution_summary", "metrics", "diagnostic"}

    for entry in manifest["artifacts"]:
        artifact_id = entry["artifact_id"]
        artifact_path = millforge_dir / STANDARD_ARTIFACT_FILENAMES[artifact_id]
        artifact_bytes = artifact_path.read_bytes()

        assert entry["path"] == f"millforge/{STANDARD_ARTIFACT_FILENAMES[artifact_id]}"
        assert entry["media_type"] == "application/json"
        assert entry["byte_size"] == len(artifact_bytes)
        assert entry["sha256_hex"] == hashlib.sha256(artifact_bytes).hexdigest()
        assert entry["complete"] is True
        assert entry["producer"] == "test/v1"


# ======================================================================
# Byte-determinism
# ======================================================================


def _write_full_artifact_set(writer: RuntimeArtifactWriter) -> None:
    """Write a full set of artifacts using the given writer."""
    # We need async handling here — use asyncio.run for deterministic setup
    import asyncio

    async def _write() -> None:
        await writer.write_terminal_result(
            _make_ref("terminal_result"), _make_terminal_result_data()
        )
        await writer.write_execution_summary(
            _make_ref("execution_summary"), _make_execution_summary_data()
        )
        await writer.write_events(_make_ref("events"), _make_events_data())
        await writer.write_tool_trace(_make_ref("tool_trace"), _make_tool_trace_data())
        await writer.write_metrics(_make_ref("metrics"), _make_metrics_data())
        await writer.write_artifact_manifest(
            _make_ref("artifact_manifest"),
            _make_manifest_data(
                [
                    {"artifact_id": "terminal_result"},
                    {"artifact_id": "execution_summary"},
                    {"artifact_id": "events"},
                    {"artifact_id": "tool_trace"},
                    {"artifact_id": "metrics"},
                ]
            ),
        )

    asyncio.run(_write())


def _collect_file_hashes(directory: Path) -> dict[str, str]:
    """Collect SHA-256 hashes of all files in directory (recursive)."""
    hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rel = path.relative_to(directory)
            hashes[str(rel)] = _sha256(path.read_bytes())
    return hashes


def test_byte_determinism_two_runs_identical(temp_dir: Path) -> None:
    """Two runs under the same fixed inputs produce byte-identical files."""
    # Run 1
    dir1 = temp_dir / "run1"
    dir1.mkdir(parents=True, exist_ok=True)
    writer1 = RuntimeArtifactWriter(dir1, producer="test/v1")
    _write_full_artifact_set(writer1)
    hashes1 = _collect_file_hashes(dir1 / "millforge")

    # Run 2
    dir2 = temp_dir / "run2"
    dir2.mkdir(parents=True, exist_ok=True)
    writer2 = RuntimeArtifactWriter(dir2, producer="test/v1")
    _write_full_artifact_set(writer2)
    hashes2 = _collect_file_hashes(dir2 / "millforge")

    # Both runs must produce the same set of files
    assert set(hashes1.keys()) == set(hashes2.keys()), (
        f"File set differs: +{set(hashes1.keys()) - set(hashes2.keys())}, "
        f"-{set(hashes2.keys()) - set(hashes1.keys())}"
    )

    # Every file must have the same hash
    for filename, hash1 in hashes1.items():
        hash2 = hashes2[filename]
        assert hash1 == hash2, (
            f"Byte mismatch in {filename}: run1={hash1}, run2={hash2}"
        )


def test_byte_determinism_same_content_different_writers(temp_dir: Path) -> None:
    """Two different RuntimeArtifactWriter instances with same input
    produce byte-identical output files."""
    dir_a = temp_dir / "a"
    dir_b = temp_dir / "b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    writer_a = RuntimeArtifactWriter(dir_a, producer="test/v1")
    writer_b = RuntimeArtifactWriter(dir_b, producer="test/v1")

    _write_full_artifact_set(writer_a)
    _write_full_artifact_set(writer_b)

    hashes_a = _collect_file_hashes(dir_a / "millforge")
    hashes_b = _collect_file_hashes(dir_b / "millforge")

    assert set(hashes_a.keys()) == set(hashes_b.keys())
    for fname in hashes_a:
        assert hashes_a[fname] == hashes_b[fname], f"Byte mismatch in {fname}"


# ======================================================================
# Instrumentation / safety checks
# ======================================================================


def test_no_forge_imports() -> None:
    """No Forge or provider SDK imports in the test file."""
    import ast
    import inspect

    import tests.test_artifacts as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    _FORBIDDEN_MODULES = {"forge", "httpx"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                top = node.module.split(".")[0]
                assert top not in _FORBIDDEN_MODULES, (
                    f"Forbidden import from: {node.module}"
                )


def test_standard_artifact_ids_complete() -> None:
    """STANDARD_ARTIFACT_IDS contains exactly 7 entries."""
    assert len(STANDARD_ARTIFACT_IDS) == 7
    expected = {
        "terminal_result",
        "execution_summary",
        "events",
        "tool_trace",
        "metrics",
        "artifact_manifest",
        "diagnostic",
    }
    assert set(STANDARD_ARTIFACT_IDS) == expected


def test_standard_artifact_filenames_complete() -> None:
    """STANDARD_ARTIFACT_FILENAMES covers all 7 IDs."""
    assert len(STANDARD_ARTIFACT_FILENAMES) == 7
    for aid in STANDARD_ARTIFACT_IDS:
        assert aid in STANDARD_ARTIFACT_FILENAMES, f"Missing filename for {aid}"
