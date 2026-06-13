from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tomllib  # type: ignore[import-not-found]
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import millforge
from millforge._forge.adapter import (
    ForgeEventTranslator,
    ForgeModelBridge,
    ForgeSessionInputBuilder,
)
from millforge.compiled_plan import SessionEventType
from millforge.contracts import (
    AssistantMessage,
    ModelCompletionResponse,
    ModelToolCall,
    ParsedToolArguments,
)
from millforge.testing import FakeModelClient
from tests.conftest import (
    FakeClock,
    make_test_compiled_plan,
    make_test_guarded_session_request,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_MILLFORGE = REPO_ROOT / "src" / "millforge"
PRIVATE_FORGE = SRC_MILLFORGE / "_forge"

FORBIDDEN_PUBLIC_IMPORTS = {
    "millforge._forge",
    "forge",
    "anthropic",
    "openai",
    "ollama",
    "llamafile",
    "vllm",
}
FORBIDDEN_RUNTIME_DEPENDENCIES = {
    "forge-guardrails",
    "anthropic",
    "openai",
    "ollama",
    "llamafile",
    "vllm",
    "litellm",
    "fastapi",
    "uvicorn",
    "typer",
    "rich",
    "gradio",
    "streamlit",
    "torch",
    "transformers",
}
FORBIDDEN_PROVIDER_IMPORT_ROOTS = {
    "anthropic",
    "httpx",
    "litellm",
    "openai",
    "requests",
}
FORBIDDEN_WHEEL_PATH_PARTS = {
    "anthropic.py",
    "llamafile.py",
    "ollama.py",
    "openai_compat.py",
    "sampling_defaults.py",
    "vllm.py",
    "hardware.py",
    "slot_worker.py",
    "server.py",
}
FORBIDDEN_WHEEL_DIR_PARTS = {
    "forge",
    "proxy",
    "cli",
    "eval",
    "dashboard",
    "dashboards",
    "tools",
}


def _public_python_files() -> Iterator[Path]:
    for path in SRC_MILLFORGE.rglob("*.py"):
        if PRIVATE_FORGE in path.parents:
            continue
        yield path


def _source_python_files() -> Iterator[Path]:
    yield from SRC_MILLFORGE.rglob("*.py")


def _annotation_text(annotation: ast.AST | None) -> str:
    if annotation is None:
        return ""
    return ast.unparse(annotation)


def _assert_no_private_forge_annotation(path: Path, tree: ast.AST) -> None:
    for node in ast.walk(tree):
        annotation = ""
        if isinstance(node, ast.AnnAssign):
            annotation = _annotation_text(node.annotation)
        elif isinstance(node, ast.arg):
            annotation = _annotation_text(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotation = _annotation_text(node.returns)
        if not annotation:
            continue
        assert "millforge._forge" not in annotation, (path, annotation)
        assert "Forge" not in annotation, (path, annotation)


def test_public_modules_have_no_private_forge_imports_or_annotations() -> None:
    for path in _public_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    assert imported not in FORBIDDEN_PUBLIC_IMPORTS
                    assert not imported.startswith("millforge._forge.")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = node.module
                assert imported not in FORBIDDEN_PUBLIC_IMPORTS
                assert not imported.startswith("millforge._forge.")
        _assert_no_private_forge_annotation(path, tree)


def test_public_package_exports_no_private_forge_symbols() -> None:
    exports = tuple(millforge.__all__)

    assert "_forge" not in exports
    assert not any(name.startswith("Forge") for name in exports)
    assert not any(name.startswith("_") for name in exports)
    assert not hasattr(millforge, "ForgeGuardrailBackend")


def test_runtime_dependencies_exclude_forge_transport_and_provider_packages() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    dependencies = pyproject["project"].get("dependencies", ())
    normalized = {
        re.split(r"[\s<>=!~;\[]", item, maxsplit=1)[0].lower() for item in dependencies
    }

    assert "httpx" in normalized
    assert "pydantic" in normalized
    assert not (normalized & FORBIDDEN_RUNTIME_DEPENDENCIES)


def test_http_transport_imports_are_isolated_to_private_model_backend() -> None:
    allowed = {SRC_MILLFORGE / "model_backend.py"}
    for path in _source_python_files():
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_root = alias.name.split(".", maxsplit=1)[0]
                    assert imported_root not in FORBIDDEN_PROVIDER_IMPORT_ROOTS, (
                        path,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_root = node.module.split(".", maxsplit=1)[0]
                assert imported_root not in FORBIDDEN_PROVIDER_IMPORT_ROOTS, (
                    path,
                    node.module,
                )


def test_wheel_content_exposes_only_millforge_private_forge_subset(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wheel_path = next(out_dir.glob("millforge-*.whl"))

    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        path_parts = {part for name in names for part in Path(name).parts}
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = wheel.read(metadata_name).decode("utf-8")

    assert "forge/__init__.py" not in names
    assert not any(name.startswith("forge/") for name in names)
    assert "millforge/_forge/LICENSE" in names
    assert "millforge/_forge/PROVENANCE.json" in names
    assert "millforge/_forge/UPDATE_POLICY.md" in names
    assert not any(name.endswith(".pyc") for name in names)
    assert not (path_parts & FORBIDDEN_WHEEL_DIR_PARTS)
    assert not any(Path(name).name in FORBIDDEN_WHEEL_PATH_PARTS for name in names)
    metadata_lower = metadata.lower()
    for dependency in FORBIDDEN_RUNTIME_DEPENDENCIES:
        assert f"requires-dist: {dependency}" not in metadata_lower


def test_sdist_content_excludes_tests_runtime_artifacts_and_ref_forge(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sdist_path = next(out_dir.glob("millforge-*.tar.gz"))

    with tarfile.open(sdist_path) as sdist:
        names = set(sdist.getnames())

    stripped = {"/".join(Path(name).parts[1:]) for name in names}
    assert "src/millforge/_forge/LICENSE" in stripped
    assert "src/millforge/_forge/PROVENANCE.json" in stripped
    assert "src/millforge/_forge/UPDATE_POLICY.md" in stripped
    assert not any(name.startswith("tests/") for name in stripped)
    assert not any(name.startswith("ref-forge/") for name in stripped)
    assert not any(name.startswith("millrace-agents/") for name in stripped)
    assert not any("__pycache__" in Path(name).parts for name in stripped)
    assert not any(name.endswith(".pyc") for name in stripped)
    assert not any("secret" in Path(name).name.lower() for name in stripped)


def test_prompt_assembly_excludes_secrets_and_artifact_contents_and_preserves_braces() -> (
    None
):
    plan = make_test_compiled_plan().model_copy(
        update={
            "prompt_policy": make_test_compiled_plan().prompt_policy.model_copy(
                update={
                    "system_instructions": "Keep literal braces: {request_id}",
                    "include_request_context": False,
                }
            )
        }
    )
    session_request = make_test_guarded_session_request()

    messages = ForgeSessionInputBuilder().build(plan, session_request)
    payload = json.loads(messages[1].content)

    assert messages[0].content == "Keep literal braces: {request_id}"
    assert payload == {
        "kind": "millforge_stage_request",
        "request_id": "req-test-001",
        "run_id": "run-test-001",
        "schema_version": "1.0",
        "stage": {
            "node_id": "builder",
            "plane": "execution",
            "stage_kind_id": "builder",
        },
    }
    assert "work_item_id" not in payload
    assert "input_artifacts" not in payload
    assert "prompt_policy" not in payload
    assert "context_policy" not in payload
    assert "compiled_harness" not in payload
    assert "run_directory" not in payload
    assert "timeout" not in payload
    assert "cancellation" not in payload
    assert "secret_refs" not in payload
    assert "DATABASE_PASSWORD" not in messages[1].content
    assert '{"schema_version":"test"}' not in messages[1].content


async def _send_one_model_request(
    *,
    prompt_body: str,
    exception: Exception | None = None,
) -> tuple[ForgeEventTranslator, str]:
    session_request = make_test_guarded_session_request()
    translator = ForgeEventTranslator(
        session_request=session_request, clock=FakeClock()
    )
    responses = []
    exceptions = []
    if exception is None:
        responses.append(
            ModelCompletionResponse(
                provider_request_id="provider-call-001",
                model_id="deepseek_flash_high",
                message=AssistantMessage(
                    content="ok",
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-001",
                            name="prepare",
                            arguments=ParsedToolArguments(value={"path": "input.txt"}),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
    else:
        exceptions.append(exception)
    bridge = ForgeModelBridge(
        model_client=FakeModelClient(responses=responses, exceptions=exceptions),
        model="deepseek_flash_high",
        event_translator=translator,
    )
    try:
        await bridge.send([{"role": "user", "content": prompt_body}])
    except Exception:
        if exception is None:
            raise
    return translator, json.dumps(
        [event.model_dump(mode="json") for event in translator.events],
        sort_keys=True,
    )


async def test_model_prompt_events_record_only_role_size_and_sha256() -> None:
    prompt_body = "raw private prompt with credential sk-test-secret"
    translator, serialized = await _send_one_model_request(prompt_body=prompt_body)

    start_event = translator.events[0]
    fields: dict[str, Any] = {field.key: field.value for field in start_event.fields}
    assert start_event.event_type == SessionEventType.MODEL_REQUEST_STARTED
    assert fields["prompt_0_role"] == "user"
    assert fields["prompt_0_byte_size"] == len(prompt_body.encode("utf-8"))
    assert (
        fields["prompt_0_sha256"]
        == hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()
    )
    assert prompt_body not in serialized
    assert "sk-test-secret" not in serialized


async def test_model_failure_events_exclude_raw_exception_body() -> None:
    prompt_body = "raw private prompt"
    _translator, serialized = await _send_one_model_request(
        prompt_body=prompt_body,
        exception=RuntimeError("raw provider body with token sk-provider-secret"),
    )

    assert prompt_body not in serialized
    assert "raw provider body" not in serialized
    assert "sk-provider-secret" not in serialized
