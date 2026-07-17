"""Focused regression tests for the public compact-eval artifact surface."""

from __future__ import annotations

import millforge
import millforge.eval_artifacts as eval_artifacts
import pytest
from pydantic import ValidationError

from millforge.eval_artifacts import (
    EVAL_ARTIFACT_MEDIA_TYPE_JSON,
    EVAL_ARTIFACT_MEDIA_TYPE_JSONL,
    EVAL_PUBLIC_ARTIFACT_IDS,
    EvalArtifactId,
    EvalArtifactManifestArtifact,
    EvalArtifactManifestEntry,
    calculate_eval_artifact_manifest_sha256,
    canonical_eval_artifact_layout,
    canonical_eval_artifact_manifest_bytes,
    eval_artifact_layout_entry,
)


def test_eval_artifacts_public_contracts_are_root_exports() -> None:
    for public_name in eval_artifacts.__all__:
        assert public_name in millforge.__all__
        assert getattr(millforge, public_name) is getattr(eval_artifacts, public_name)


def test_eval_artifact_layout_is_complete_path_free_and_stable() -> None:
    layout = canonical_eval_artifact_layout()

    assert (
        tuple(artifact.value for artifact in EvalArtifactId) == EVAL_PUBLIC_ARTIFACT_IDS
    )
    assert tuple(layout) == tuple(EvalArtifactId)
    assert layout is canonical_eval_artifact_layout()
    assert layout[EvalArtifactId.EVENT_LOG].media_type == EVAL_ARTIFACT_MEDIA_TYPE_JSONL

    for artifact_id, entry in layout.items():
        assert entry is eval_artifact_layout_entry(artifact_id)
        assert entry.layout_path.startswith("trial/")
        assert ".." not in entry.layout_path
        assert "\\" not in entry.layout_path
        assert entry.layout_path == f"{entry.section.value}/{entry.canonical_filename}"
        if artifact_id is not EvalArtifactId.EVENT_LOG:
            assert entry.media_type == EVAL_ARTIFACT_MEDIA_TYPE_JSON


def test_eval_artifact_manifest_bytes_and_hash_are_deterministic() -> None:
    manifest = _manifest()
    reversed_manifest = _manifest(reversed_entries=True)

    assert tuple(entry.artifact_id for entry in manifest.entries) == (
        EvalArtifactId.TASK,
        EvalArtifactId.PLAN,
    )
    assert (
        canonical_eval_artifact_manifest_bytes(manifest).decode("ascii").endswith("\n")
    )
    assert canonical_eval_artifact_manifest_bytes(manifest) == (
        canonical_eval_artifact_manifest_bytes(reversed_manifest)
    )
    assert calculate_eval_artifact_manifest_sha256(manifest) == (
        calculate_eval_artifact_manifest_sha256(reversed_manifest)
    )


def test_eval_artifact_manifest_rejects_self_reference_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="must not reference itself"):
        _manifest(artifact_ids=(EvalArtifactId.ARTIFACT_MANIFEST,))

    with pytest.raises(ValidationError, match="entries must be unique"):
        _manifest(artifact_ids=(EvalArtifactId.TASK, EvalArtifactId.TASK))


def _manifest(
    *,
    artifact_ids: tuple[EvalArtifactId, ...] = (
        EvalArtifactId.PLAN,
        EvalArtifactId.TASK,
    ),
    reversed_entries: bool = False,
) -> EvalArtifactManifestArtifact:
    entries = tuple(_manifest_entry(artifact_id) for artifact_id in artifact_ids)
    if reversed_entries:
        entries = tuple(reversed(entries))
    return EvalArtifactManifestArtifact(
        trial_id="trial-public-artifacts",
        created_by="millforge.eval_artifacts",
        summary="Public eval artifact manifest",
        entries=entries,
    )


def _manifest_entry(artifact_id: EvalArtifactId) -> EvalArtifactManifestEntry:
    layout = eval_artifact_layout_entry(artifact_id)
    return EvalArtifactManifestEntry(
        artifact_id=artifact_id,
        layout_path=layout.layout_path,
        media_type=layout.media_type,
        schema_id=layout.schema_id,
        byte_size=2,
        sha256="0" * 64,
        producer="millforge.eval_artifacts",
        model_visible=layout.model_visible,
    )
