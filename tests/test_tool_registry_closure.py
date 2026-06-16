from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "tests" / "fixtures" / "spec04_conformance_matrix.json"
README_PATH = ROOT / "README.md"
ROADMAP_PATH = ROOT / "ROADMAP.md"

REQUIRED_FIELDS = {
    "requirement_id",
    "requirement_summary",
    "source_specs",
    "implemented_by_files",
    "covered_by_tests",
    "evidence_commands",
    "status",
    "notes",
}

REQUIRED_TOPICS = {
    "descriptor immutability and hashing": {"immutab", "hash"},
    "registry freeze and lookup": {"freeze", "lookup"},
    "built-in descriptor projection": {"built-in", "projection"},
    "schema and hash determinism": {"schema", "determin"},
    "capability aggregation": {"capability", "aggreg"},
    "side-effect and idempotency classification": {"side-effect", "idempotency"},
    "exact binding identity": {"binding", "identity"},
    "input validation": {"input", "validation"},
    "output validation": {"output", "validation"},
    "redaction and sanitized hashing": {"redact", "hash"},
    "workspace containment": {"workspace", "contain"},
    "artifact containment": {"artifact", "contain"},
    "shell profile restriction": {"shell", "profile"},
    "terminal intent boundary": {"terminal", "intent"},
    "trace completeness": {"trace", "completeness"},
    "ambiguous failure retry policy": {"ambiguous", "retry"},
    "package and public API boundary": {"package", "public api"},
    "connector readiness boundary": {"connector", "readiness"},
}

FUTURE_SPECS = {"01", "05", "06", "07", "08"}
FUTURE_TERMS = {
    "spec 01": ("millrace runner", "queue/status", "daemon"),
    "spec 05": ("connector admission", "mcp", "broker", "custom tool"),
    "spec 06": ("small-model", "small model"),
    "spec 07": ("production preset", "planner preset", "builder preset"),
    "spec 08": ("eval", "comparative evaluation"),
}

OFFLINE_FORBIDDEN_TOKENS = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "pip install",
    "uv add",
    "openai",
    "deepseek",
    "mcp",
    "connector",
    "daemon",
    "tmux",
    "release",
}
ARCHIVE_FORBIDDEN_PARTS = {
    "ref-forge",
    "millrace-agents",
    "ideas",
    "tests",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "dist",
    "build",
    "scratch",
    "logs",
}
PUBLIC_API_REQUIRED_EXPORTS = {
    "ToolDescriptor",
    "ToolOutputPolicy",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistryErrorCode",
    "ToolTimeoutPolicy",
    "create_builtin_tool_executor",
    "descriptor_hash_payload",
}
TOOLS_API_REQUIRED_EXPORTS = {
    "BUILTIN_CAPABILITY_IDS",
    "BUILTIN_TOOL_DESCRIPTORS",
    "BUILTIN_TOOL_VERSION",
    "CompiledToolBindingExecutor",
    "RuntimeToolRegistry",
    "ToolBindingDenialCode",
    "ToolExecutionErrorCode",
    "ToolDescriptor",
    "ToolOutputPolicy",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistryErrorCode",
    "ToolTimeoutPolicy",
    "create_builtin_runtime_registry",
    "create_builtin_tool_executor",
    "create_builtin_tool_registry",
    "create_builtin_tool_snapshot",
    "create_tool_executor",
    "descriptor_hash_payload",
    "iter_builtin_tool_descriptors",
}
PUBLIC_API_FORBIDDEN_EXPORTS = {
    "AdmissionManifest",
    "Broker",
    "ConnectorAdmission",
    "ConnectorBroker",
    "ConnectorDiscovery",
    "Daemon",
    "DefaultProductionPreset",
    "DefaultToolRegistry",
    "EvalHarness",
    "LocalWorkspaceDefault",
    "MillraceRunner",
    "ProductionBuilderPreset",
    "ProductionCheckerPreset",
    "ProductionPlannerPreset",
    "ProductionToolPreset",
    "QueueControl",
    "RunnerPlugin",
    "StatusControl",
    "ToolDispatchMap",
}
DOCS_FORBIDDEN_IMPLEMENTED_CLAIMS = {
    "connector admission is implemented",
    "connector broker is implemented",
    "custom tool compiler is implemented",
    "eval suite is implemented",
    "live provider execution is implemented",
    "millrace runner integration is implemented",
    "production stage presets are implemented",
}


def test_spec04_conformance_matrix_has_required_shape_and_real_paths() -> None:
    rows = _load_matrix()
    assert rows
    assert len({row["requirement_id"] for row in rows}) == len(rows)

    for row in rows:
        assert set(row) == REQUIRED_FIELDS
        assert isinstance(row["requirement_id"], str) and row["requirement_id"]
        assert (
            isinstance(row["requirement_summary"], str) and row["requirement_summary"]
        )
        assert row["status"] in {"implemented", "deferred", "not_applicable"}
        assert isinstance(row["notes"], str) and row["notes"]
        _assert_non_empty_string_list(row, "source_specs")
        _assert_non_empty_string_list(row, "implemented_by_files")
        _assert_non_empty_string_list(row, "covered_by_tests")
        _assert_non_empty_string_list(row, "evidence_commands")
        for relative_path in row["implemented_by_files"]:
            assert (ROOT / relative_path).exists(), relative_path
        for relative_path in row["covered_by_tests"]:
            test_path = ROOT / relative_path
            assert test_path.exists(), relative_path
            assert test_path.name.startswith("test_") or relative_path.endswith(
                "tests/compiler"
            ), relative_path


def test_spec04_conformance_matrix_commands_are_offline_and_deterministic() -> None:
    rows = _load_matrix()
    for row in rows:
        for command in row["evidence_commands"]:
            assert command.strip() == command
            assert "\n" not in command
            assert command.startswith(("python -m ", "git ", "tar -tzf ")), command
            lowered = command.casefold()
            assert not any(token in lowered for token in OFFLINE_FORBIDDEN_TOKENS), (
                command
            )
            shlex.split(command)


def test_spec04_conformance_matrix_covers_required_closure_topics() -> None:
    corpus = "\n".join(
        " ".join(
            (
                row["requirement_id"],
                row["requirement_summary"],
                " ".join(row["source_specs"]),
                " ".join(row["implemented_by_files"]),
                " ".join(row["covered_by_tests"]),
                " ".join(row["evidence_commands"]),
                row["status"],
                row["notes"],
            )
        ).casefold()
        for row in _load_matrix()
    )

    missing = {
        topic: sorted(tokens - {token for token in tokens if token in corpus})
        for topic, tokens in REQUIRED_TOPICS.items()
        if not tokens <= {token for token in tokens if token in corpus}
    }
    assert missing == {}


def test_spec04_conformance_matrix_does_not_overclaim_future_specs() -> None:
    rows = _load_matrix()
    implemented_rows = [row for row in rows if row["status"] == "implemented"]
    for row in implemented_rows:
        assert FUTURE_SPECS.isdisjoint(set(row["source_specs"])), row
        text = f"{row['requirement_summary']} {row['notes']}".casefold()
        for owner, terms in FUTURE_TERMS.items():
            if any(term in text for term in terms):
                conservative = (
                    "not claim",
                    "does not",
                    "without",
                    "avoid",
                    "reject",
                    "outside",
                    "deferred",
                )
                assert any(marker in text for marker in conservative), (owner, row)

    deferred_sources = {
        source
        for row in rows
        if row["status"] == "deferred"
        for source in row["source_specs"]
    }
    assert FUTURE_SPECS <= deferred_sources


def test_spec04_conformance_matrix_avoids_citation_only_pass_rows() -> None:
    rows = _load_matrix()
    for row in rows:
        if row["status"] != "implemented":
            continue
        assert len(row["implemented_by_files"]) >= 1
        assert len(row["covered_by_tests"]) >= 1
        assert len(row["evidence_commands"]) >= 1
        assert all(
            not path.startswith("millrace-agents/specs/")
            for path in row["implemented_by_files"]
        ), row["requirement_id"]


def test_public_package_exports_only_spec04_accepted_tool_surface() -> None:
    import millforge
    import millforge.tools as public_tools

    millforge_exports = set(millforge.__all__)
    tool_exports = set(public_tools.__all__)

    assert PUBLIC_API_REQUIRED_EXPORTS <= millforge_exports
    assert TOOLS_API_REQUIRED_EXPORTS <= tool_exports
    assert not (PUBLIC_API_FORBIDDEN_EXPORTS & millforge_exports)
    assert not (PUBLIC_API_FORBIDDEN_EXPORTS & tool_exports)
    assert not any(name.startswith("_") for name in millforge_exports)
    assert not any(name.startswith("_") for name in tool_exports)
    assert "create_builtin_tool_executor" in tool_exports
    assert "ForgeGuardrailBackend" not in millforge_exports


def test_package_build_policy_excludes_private_state_and_test_trees() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/millforge"
    ]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"] == [
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "src/millforge",
    ]


def test_built_archives_exclude_private_generated_cache_test_and_log_segments(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out_dir)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    wheel_path = next(out_dir.glob("millforge-*.whl"))
    sdist_path = next(out_dir.glob("millforge-*.tar.gz"))

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = wheel.namelist()
    with tarfile.open(sdist_path) as sdist:
        sdist_names = ["/".join(Path(name).parts[1:]) for name in sdist.getnames()]

    _assert_archive_names_exclude_forbidden_segments(wheel_names)
    _assert_archive_names_exclude_forbidden_segments(sdist_names)


def test_private_state_roots_are_ignored_and_not_tracked_or_dirty() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--", "millrace-agents", "ideas", "ref-forge"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", "millrace-agents", "ideas", "ref-forge"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    private_status = subprocess.run(
        [
            "git",
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            "millrace-agents",
            "ideas",
            "ref-forge",
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert tracked.stdout == ""
    assert private_status.stdout == ""
    assert "millrace-agents" in ignored.stdout
    assert "ideas" in ignored.stdout
    assert "ref-forge" in ignored.stdout


def test_readme_and_roadmap_keep_spec04_claims_conservative() -> None:
    docs = {
        "README.md": README_PATH.read_text(encoding="utf-8"),
        "ROADMAP.md": ROADMAP_PATH.read_text(encoding="utf-8"),
    }

    for name, text in docs.items():
        lower = text.casefold()
        assert not any(claim in lower for claim in DOCS_FORBIDDEN_IMPLEMENTED_CLAIMS), (
            name
        )

    readme_spec04 = _section_between(
        docs["README.md"], "## Millforge 04A Tool Registry Core", None
    ).casefold()
    assert "04d" in readme_spec04
    assert "conformance matrix" in readme_spec04
    assert "closure evidence" in readme_spec04
    for required in (
        "04a",
        "04b",
        "04c",
        "connector admission",
        "millrace runner integration",
        "live provider/model/tool execution",
    ):
        assert required in readme_spec04

    roadmap_foundation = _section_between(
        docs["ROADMAP.md"], "## Current Foundation", "## Roadmap"
    ).casefold()
    assert "trusted built-in tool registry" in roadmap_foundation
    assert "default verification path" in roadmap_foundation
    assert "production tool registry" not in roadmap_foundation


def _load_matrix() -> list[dict[str, Any]]:
    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert all(isinstance(row, dict) for row in raw)
    return raw


def _assert_non_empty_string_list(row: dict[str, Any], field: str) -> None:
    value = row[field]
    assert isinstance(value, list) and value, (row["requirement_id"], field)
    assert all(isinstance(item, str) and item for item in value), (
        row["requirement_id"],
        field,
    )


def _assert_archive_names_exclude_forbidden_segments(names: list[str]) -> None:
    for name in names:
        parts = set(Path(name).parts)
        assert not (parts & ARCHIVE_FORBIDDEN_PARTS), name
        assert not name.endswith(".pyc"), name


def _section_between(text: str, start_heading: str, end_heading: str | None) -> str:
    start = text.index(start_heading)
    if end_heading is None:
        return text[start:]
    end = text.index(end_heading, start)
    return text[start:end]
