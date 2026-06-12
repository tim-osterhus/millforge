"""RuntimeArtifactWriter — produces the 7 standard runtime artifacts
with path safety, atomic writes, JSONL validation, and byte-determinism.

Path safety
-----------
Rejects absolute child paths, ``..`` components, symlink/reparse-point
escape via ``Path.resolve()`` TOCTOU check, and writes outside
``run_directory/millforge/``.

Atomic writes
-------------
Each artifact is written to a temporary file in the target directory,
flushed, fsynced, then atomically renamed to the final path via
``os.rename()``.

JSONL
-----
Every line is independently valid JSON followed by a single newline
(``\\n``).  A partial final line is impossible because the file is
produced entirely before being atomically renamed — readers either see
the complete file or nothing.

Byte determinism
----------------
All JSON output uses sorted keys, ``ensure_ascii=True``,
``allow_nan=False``, compact separators, and a trailing newline,
producing byte-identical output under identical inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from millforge.compiled_plan import SessionEvent, ToolTraceRecord
from millforge.contracts import (
    ArtifactManifestArtifact,
    ArtifactRef,
    DiagnosticArtifact,
    DiagnosticMetadata,
    ExecutionSummaryArtifact,
    MetricsArtifact,
    TerminalResultArtifact,
)
from millforge.exceptions import ArtifactWriteError

# ---------------------------------------------------------------------------
# Standard artifact identifiers
# ---------------------------------------------------------------------------

STANDARD_ARTIFACT_IDS: tuple[str, ...] = (
    "terminal_result",
    "execution_summary",
    "events",
    "tool_trace",
    "metrics",
    "artifact_manifest",
    "diagnostic",
)

STANDARD_ARTIFACT_FILENAMES: dict[str, str] = {
    "terminal_result": "terminal_result.json",
    "execution_summary": "execution_summary.json",
    "events": "events.jsonl",
    "tool_trace": "tool_trace.jsonl",
    "metrics": "metrics.json",
    "artifact_manifest": "artifact_manifest.json",
    "diagnostic": "diagnostic.json",
}

JSON_MEDIA_TYPE = "application/json"
NDJSON_MEDIA_TYPE = "application/x-ndjson"

# ---------------------------------------------------------------------------
# RuntimeArtifactWriter
# ---------------------------------------------------------------------------


class RuntimeArtifactWriter:
    """Writes the 7 standard runtime artifacts.

    Enforces path safety (no absolute child paths, no ``..`` traversal,
    no symlink escape, no writes outside ``run_directory/millforge/``).
    Uses atomic file replacement (write-temp, flush, fsync, rename).
    JSONL: each line independently valid JSON + newline.
    Tracks all writes for manifest enrichment.

    Parameters
    ----------
    run_directory : Path
        The run directory root.  All artifact writes must be within
        ``run_directory / "millforge"``.
    producer : str
        Producer identifier for the artifact manifest.
        Default ``"RuntimeArtifactWriter/v1"``.
    """

    def __init__(
        self,
        run_directory: Path,
        producer: str = "RuntimeArtifactWriter/v1",
    ) -> None:
        self._run_directory: Path = run_directory.resolve()
        self._millforge_dir: Path = self._run_directory / "millforge"
        self._millforge_dir.mkdir(parents=True, exist_ok=True)
        self._producer: str = producer
        # artifact_id -> {path, media_type, byte_size, sha256_hex, complete, producer}
        self._artifacts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _resolve_target(self, ref_path: Path) -> Path:
        """Validate and resolve *ref_path* to an absolute path under ``millforge/``.

        *ref_path* is interpreted as relative to the run directory.
        The resolved path must fall within ``run_directory/millforge/``.

        Raises
        ------
        ArtifactWriteError
            On any path safety violation.
        """
        # 1. Reject absolute paths — all artifact paths must be relative
        if ref_path.is_absolute():
            raise ArtifactWriteError(f"Absolute artifact path rejected: {ref_path!r}")

        # 2. Check for '..' components (path traversal)
        parts = ref_path.parts
        if ".." in parts:
            raise ArtifactWriteError(
                f"Path traversal rejected: {ref_path!r} contains '..'"
            )

        # 3. Build candidate by resolving the ref path against the run directory.
        #    This handles both bare filenames (e.g. "terminal_result.json") and
        #    prefixed paths (e.g. "millforge/terminal_result.json").
        resolved_base = self._millforge_dir.resolve()
        candidate = (self._run_directory / ref_path).resolve()

        # 4. Verify candidate is under the millforge directory
        try:
            candidate.relative_to(resolved_base)
        except ValueError:
            raise ArtifactWriteError(
                f"Path escape rejected: {ref_path!r} resolves outside "
                f"millforge/ (resolved to {candidate!r})"
            )

        return candidate

    # ------------------------------------------------------------------
    # Atomic write
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> dict[str, Any]:
        """Write *content* to *target* using atomic file replacement.

        Creates a temporary file in the same directory as *target*,
        writes *content*, flushes, fsyncs, then renames to *target*.

        Returns a dict with ``byte_size`` and ``sha256_hex``.
        """
        sha256_hex = hashlib.sha256(content).hexdigest()
        byte_size = len(content)

        fd: int | None = None
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f".{target.name}.tmp.",
            )
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, str(target))
            tmp_path = None  # successfully renamed — don't clean up
        except OSError as e:
            raise ArtifactWriteError(
                f"Failed to write artifact to {target!r}: {e}",
                cause=e,
            )
        finally:
            if tmp_path is not None:
                try:
                    if fd is not None:
                        os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return {
            "byte_size": byte_size,
            "sha256_hex": sha256_hex,
        }

    # ------------------------------------------------------------------
    # Canonical JSON / JSONL serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_json(obj: Any) -> str:
        """Serialize *obj* to canonical JSON (sorted keys, no whitespace).

        Uses the same conventions as ``canonical_json_serialize()`` in
        ``compiled_plan.py`` for consistency.
        """
        try:
            return (
                json.dumps(
                    obj,
                    sort_keys=True,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        except (ValueError, TypeError, OverflowError) as e:
            raise ArtifactWriteError(
                f"JSON serialization failed: {e}",
                cause=e,
            )

    @staticmethod
    def _serialize_json(data: Any) -> bytes:
        """Serialize *data* to canonical JSON bytes (UTF-8)."""
        return RuntimeArtifactWriter._canonical_json(data).encode("utf-8")

    @staticmethod
    def _serialize_jsonl(records: list[Any]) -> bytes:
        """Serialize *records* as newline-delimited JSON bytes (UTF-8).

        Each record is independently serialized as canonical JSON
        followed by a single ``\\n``.  Empty input returns empty bytes.
        Partial final lines are impossible because serialization is
        all-or-nothing.
        """
        if not records:
            return b""

        lines: list[bytes] = []
        for i, record in enumerate(records):
            try:
                text = (
                    json.dumps(
                        record,
                        sort_keys=True,
                        ensure_ascii=True,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                lines.append(text.encode("utf-8"))
            except (ValueError, TypeError, OverflowError) as e:
                raise ArtifactWriteError(
                    f"JSONL serialization failed at record {i}: {e}",
                    cause=e,
                )

        return b"".join(lines)

    # ------------------------------------------------------------------
    # Internal write helper
    # ------------------------------------------------------------------

    def _write_artifact(
        self,
        ref: ArtifactRef,
        content: bytes,
        media_type: str,
    ) -> None:
        """Validate path, write atomically, and track the artifact."""
        target = self._resolve_target(ref.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = self._atomic_write(target, content)

        self._artifacts[ref.artifact_id] = {
            "path": str(ref.path),
            "media_type": ref.content_type or media_type,
            "byte_size": meta["byte_size"],
            "sha256_hex": meta["sha256_hex"],
            "complete": True,
            "producer": self._producer,
        }

    @staticmethod
    def _validate_model(model: type[BaseModel], data: Any) -> dict[str, Any]:
        try:
            if isinstance(data, model):
                instance = data
            else:
                instance = model.model_validate(data)
            return instance.model_dump(mode="json")
        except Exception as e:
            raise ArtifactWriteError(
                f"{model.__name__} validation failed: {e}",
                cause=e,
            )

    @staticmethod
    def _json_object_no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise json.JSONDecodeError(f"Duplicate key {key!r}", "", 0)
            seen.add(key)
            result[key] = value
        return result

    # ------------------------------------------------------------------
    # Public protocol methods
    # ------------------------------------------------------------------

    async def write_terminal_result(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``terminal_result.json``.

        Written only for legal domain terminal or rejected results;
        not for infrastructure, cancellation, or timeout failures.
        """
        payload = self._validate_model(TerminalResultArtifact, data)
        content = self._serialize_json(payload)
        self._write_artifact(ref, content, JSON_MEDIA_TYPE)

    async def write_execution_summary(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``execution_summary.json``.

        Written for every return path.
        """
        payload = self._validate_model(ExecutionSummaryArtifact, data)
        content = self._serialize_json(payload)
        self._write_artifact(ref, content, JSON_MEDIA_TYPE)

    async def write_events(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``events.jsonl`` (JSONL format).

        *data* should be a list of JSON-serializable objects, each
        representing a ``SessionEvent`` record.  Each record becomes
        one line of valid JSON followed by ``\\n``.
        """
        if not isinstance(data, list | tuple):
            raise ArtifactWriteError(
                f"Expected a list for JSONL artifact {ref.artifact_id!r}, "
                f"got {type(data).__name__}"
            )
        records = [self._validate_model(SessionEvent, item) for item in data]
        content = self._serialize_jsonl(records)
        self._write_artifact(ref, content, NDJSON_MEDIA_TYPE)

    async def write_tool_trace(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``tool_trace.jsonl`` (JSONL format).

        *data* should be a list of JSON-serializable objects, each
        representing a ``ToolTraceRecord`` record.  Each record becomes
        one line of valid JSON followed by ``\\n``.
        """
        if not isinstance(data, list | tuple):
            raise ArtifactWriteError(
                f"Expected a list for JSONL artifact {ref.artifact_id!r}, "
                f"got {type(data).__name__}"
            )
        records = [self._validate_model(ToolTraceRecord, item) for item in data]
        content = self._serialize_jsonl(records)
        self._write_artifact(ref, content, NDJSON_MEDIA_TYPE)

    async def write_metrics(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``metrics.json``.

        Written for every return path.
        """
        payload = self._validate_model(MetricsArtifact, data)
        content = self._serialize_json(payload)
        self._write_artifact(ref, content, JSON_MEDIA_TYPE)

    async def write_artifact_manifest(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``artifact_manifest.json``.

        Enriches the provided *data* with computed ``sha256_hex``,
        ``byte_size``, ``complete``, and ``producer`` values for each
        artifact tracked by this writer.  The manifest does **not**
        include an entry for itself.

        *data* should be a dict with at least an ``"artifacts"`` key
        whose value is a list of dicts, each containing at minimum
        ``"artifact_id"``.  The writer injects the computed metadata
        into each dict.
        """
        target = self._resolve_target(ref.path)

        # Enrich the caller-provided data with computed values
        enriched = self._validate_model(
            ArtifactManifestArtifact, self._enrich_manifest_data(data)
        )

        content = self._serialize_json(enriched)
        meta = self._atomic_write(target, content)

        # The manifest is tracked as written but is NOT added to
        # _artifacts for manifest listing (it must not include itself).
        # Store only path-level info for later queries.
        self._manifest_path = str(ref.path)
        self._manifest_media_type = ref.content_type or JSON_MEDIA_TYPE
        self._manifest_byte_size = meta["byte_size"]
        self._manifest_sha256 = meta["sha256_hex"]

    async def write_diagnostic(self, ref: ArtifactRef, data: Any) -> None:
        """Write ``diagnostic.json``.

        Written only when a sanitized diagnostic is present.
        """
        if isinstance(data, DiagnosticMetadata):
            data = {"schema_version": "1.0", "diagnostic": data.model_dump(mode="json")}
        payload = self._validate_model(DiagnosticArtifact, data)
        content = self._serialize_json(payload)
        self._write_artifact(ref, content, JSON_MEDIA_TYPE)

    # ------------------------------------------------------------------
    # Manifest enrichment
    # ------------------------------------------------------------------

    def _enrich_manifest_data(self, data: Any) -> Any:
        """Inject computed artifact metadata into manifest data.

        If *data* is a dict containing an ``"artifacts"`` list of dicts,
        the list is replaced with the writer's tracked artifacts in
        deterministic artifact-id order. This guarantees manifest
        completeness for the files written by this writer and prevents
        caller-supplied stale metadata.

        Any artifact entries not tracked by this writer are left
        unchanged (they will lack computed fields).
        """
        if not isinstance(data, dict) or "artifacts" not in data:
            return data

        requested_ids = {
            item["artifact_id"]
            for item in data["artifacts"]
            if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
        }
        artifact_ids = sorted(set(self._artifacts) | requested_ids)
        missing = [
            artifact_id
            for artifact_id in artifact_ids
            if artifact_id not in self._artifacts
        ]
        if missing:
            raise ArtifactWriteError(
                "Manifest references unwritten artifact(s): " + ", ".join(missing)
            )

        enriched_artifacts: list[dict[str, Any]] = []
        for artifact_id in artifact_ids:
            tracked = self._artifacts[artifact_id]
            enriched_artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "path": tracked["path"],
                    "media_type": tracked["media_type"],
                    "byte_size": tracked["byte_size"],
                    "sha256_hex": tracked["sha256_hex"],
                    "complete": tracked["complete"],
                    "producer": tracked["producer"],
                }
            )

        result = dict(data)
        result.setdefault("schema_version", "1.0")
        result["artifacts"] = enriched_artifacts
        return result

    # ------------------------------------------------------------------
    # JSONL readers
    # ------------------------------------------------------------------

    def _read_jsonl_models(
        self, ref: ArtifactRef, model: type[BaseModel]
    ) -> tuple[BaseModel, ...]:
        target = self._resolve_target(ref.path)
        try:
            content = target.read_bytes()
        except OSError as e:
            raise ArtifactWriteError(
                f"Failed to read artifact from {target!r}: {e}",
                cause=e,
            )

        if content and not content.endswith(b"\n"):
            raise ArtifactWriteError(
                f"JSONL artifact {ref.artifact_id!r} has a partial final line"
            )

        records: list[BaseModel] = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line:
                raise ArtifactWriteError(
                    f"JSONL artifact {ref.artifact_id!r} has an empty line "
                    f"at {line_number}"
                )
            try:
                parsed = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=self._json_object_no_duplicate_keys,
                )
                records.append(model.model_validate(parsed))
            except Exception as e:
                raise ArtifactWriteError(
                    f"JSONL artifact {ref.artifact_id!r} invalid at line "
                    f"{line_number}: {e}",
                    cause=e,
                )
        return tuple(records)

    def read_events(self, ref: ArtifactRef) -> tuple[SessionEvent, ...]:
        """Read and validate an ``events.jsonl`` artifact."""
        records = self._read_jsonl_models(ref, SessionEvent)
        return tuple(record for record in records if isinstance(record, SessionEvent))

    def read_tool_trace(self, ref: ArtifactRef) -> tuple[ToolTraceRecord, ...]:
        """Read and validate a ``tool_trace.jsonl`` artifact."""
        records = self._read_jsonl_models(ref, ToolTraceRecord)
        return tuple(
            record for record in records if isinstance(record, ToolTraceRecord)
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def tracked_artifacts(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of artifacts tracked by this writer.

        Each value is a dict with keys ``path``, ``media_type``,
        ``byte_size``, ``sha256_hex``, ``complete``, ``producer``.
        """
        return dict(self._artifacts)

    @property
    def millforge_dir(self) -> Path:
        """Return the resolved millforge output directory."""
        return self._millforge_dir


__all__: list[str] = [
    "NDJSON_MEDIA_TYPE",
    "JSON_MEDIA_TYPE",
    "STANDARD_ARTIFACT_FILENAMES",
    "STANDARD_ARTIFACT_IDS",
    "RuntimeArtifactWriter",
]
