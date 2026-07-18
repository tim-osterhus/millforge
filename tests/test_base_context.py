"""Bounded Pi-derived context and prompt cases for ``millforge-base``."""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from millforge.base.context import (
    MillforgeBaseContextFile,
    MillforgeBaseContextSnapshot,
    _CONTEXT_BYTE_LIMIT,
    load_millforge_base_context,
)
from millforge.base.options import MillforgeBaseOptions
from millforge.base.prompt import (
    MillforgeBasePromptBudgetError,
    _APPEND_PREFIX,
    _CONTEXT_PREFIX,
    _CONTEXT_SUFFIX,
    _DEFAULT_SYSTEM_PROMPT,
    _PROMPT_BYTE_LIMIT,
    build_millforge_base_system_prompt,
)

_DATE = datetime.date(2026, 7, 15)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    cwd = tmp_path / "workspace" / "nested"
    home.mkdir(parents=True)
    cwd.mkdir(parents=True)
    return home, cwd


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _empty_context(cwd: Path, home: Path) -> MillforgeBaseContextSnapshot:
    return load_millforge_base_context(cwd=cwd, home_directory=home, enabled=False)


def _context_file(path: Path, content: str) -> MillforgeBaseContextFile:
    return MillforgeBaseContextFile(
        scope="project",
        path=path.resolve(),
        content=content,
        raw_sha256="0" * 64,
        original_byte_count=len(content.encode("utf-8")),
        included_byte_count=len(content.encode("utf-8")),
        truncated=False,
    )


def _prompt(
    *,
    options: MillforgeBaseOptions,
    context: MillforgeBaseContextSnapshot,
    cwd: Path,
    home: Path,
):
    return build_millforge_base_system_prompt(
        options=options,
        context=context,
        cwd=cwd,
        home_directory=home,
        prompt_date=_DATE,
    )


def _footer(cwd: Path) -> str:
    return f"\nCurrent date: {_DATE.isoformat()}\nCurrent working directory: {cwd.resolve().as_posix()}"


def test_options_are_frozen_closed_and_utf8_bounded() -> None:
    options = MillforgeBaseOptions()
    assert options.model_config["frozen"] is True
    assert options.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        MillforgeBaseOptions(system_prompt=" ")
    with pytest.raises(ValidationError):
        MillforgeBaseOptions(append_system_prompt="x" * (_PROMPT_BYTE_LIMIT + 1))


def test_disabled_context_is_the_exact_empty_snapshot(tmp_path: Path) -> None:
    home, cwd = _workspace(tmp_path)
    snapshot = _empty_context(cwd, home)
    assert snapshot.files == ()
    assert snapshot.diagnostics == ()
    assert snapshot.context_sha256 == hashlib.sha256(b"[]").hexdigest()
    assert snapshot.truncated is False


def test_context_discovery_orders_global_then_root_to_cwd(
    tmp_path: Path,
) -> None:
    home, cwd = _workspace(tmp_path)
    root = tmp_path / "workspace"
    _write(home / ".millforge" / "AGENTS.md", "global")
    _write(root / "AGENTS.md", "root")
    _write(root / "nested" / "CLAUDE.md", "nested")

    snapshot = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)
    assert [file.content for file in snapshot.files] == ["global", "root", "nested"]
    assert [file.scope for file in snapshot.files] == ["global", "project", "project"]


def test_context_candidate_precedence_observes_host_case_semantics(
    tmp_path: Path,
) -> None:
    """Candidate order stays deterministic whether case variants alias or coexist."""

    home, cwd = _workspace(tmp_path)
    preferred = cwd / "CLAUDE.md"
    later = cwd / "CLAUDE.MD"
    _write(preferred, "preferred candidate")
    _write(later, "later candidate")

    aliases = preferred.samefile(later)
    snapshot = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)

    assert len(snapshot.files) == 1
    assert snapshot.files[0].content == (
        "later candidate" if aliases else "preferred candidate"
    )


def test_context_deduplicates_filesystem_aliases_with_global_precedence(
    tmp_path: Path,
) -> None:
    """Filesystem identity, including case aliases, must beat path spelling."""

    home, cwd = _workspace(tmp_path)
    global_context = home / ".millforge" / "AGENTS.md"
    project_alias = cwd / "AGENTS.md"
    _write(global_context, "global")
    project_alias.parent.mkdir(parents=True, exist_ok=True)
    os.link(global_context, project_alias)

    assert global_context.samefile(project_alias)
    assert global_context.resolve() != project_alias.resolve()

    snapshot = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)

    assert [(file.scope, file.path, file.content) for file in snapshot.files] == [
        ("global", global_context.resolve(), "global")
    ]


def test_context_global_pi_fallback_and_unreadable_winner_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = _workspace(tmp_path)
    _write(home / ".pi" / "agent" / "CLAUDE.md", "pi global")
    _write(cwd / "AGENTS.md", "unreadable")
    _write(cwd / "CLAUDE.md", "fallback")
    unreadable = (cwd / "AGENTS.md").resolve()
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == unreadable:
            raise OSError("access denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    snapshot = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)

    assert [file.content for file in snapshot.files] == ["pi global", "fallback"]
    assert len(snapshot.diagnostics) == 1
    assert snapshot.diagnostics[0].startswith("Could not read")
    assert len(snapshot.diagnostics[0].encode("utf-8")) <= 2_048


def test_context_raw_hash_replacement_decoding_and_allocation_boundaries(
    tmp_path: Path,
) -> None:
    home, cwd = _workspace(tmp_path)
    raw = b"\xff" + "e".encode("utf-8") * (_CONTEXT_BYTE_LIMIT + 1)
    _write(home / ".millforge" / "AGENTS.md", raw)
    _write(cwd / "AGENTS.md", "omitted")
    snapshot = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)

    assert len(snapshot.files) == 1
    first = snapshot.files[0]
    assert first.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.content.startswith("\ufffd")
    assert first.included_byte_count <= _CONTEXT_BYTE_LIMIT
    assert first.content.encode("utf-8").decode("utf-8") == first.content
    assert first.truncated is True
    assert snapshot.truncated is True

    _write(home / ".millforge" / "AGENTS.md", "x" * _CONTEXT_BYTE_LIMIT)
    exact = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)
    assert [file.content for file in exact.files] == ["x" * _CONTEXT_BYTE_LIMIT]
    assert exact.truncated is True

    _write(home / ".millforge" / "AGENTS.md", "")
    full = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)
    assert [file.content for file in full.files] == ["", "omitted"]
    assert full.truncated is False


def test_prompt_precedence_default_and_exact_framing(tmp_path: Path) -> None:
    home, cwd = _workspace(tmp_path)
    _write(cwd / ".millforge" / "SYSTEM.md", "project native")
    _write(cwd / ".pi" / "SYSTEM.md", "project pi")
    _write(home / ".millforge" / "SYSTEM.md", "global native")
    _write(home / ".pi" / "agent" / "SYSTEM.md", "global pi")
    _write(cwd / ".millforge" / "APPEND_SYSTEM.md", "project append")
    _write(cwd / ".pi" / "APPEND_SYSTEM.md", "project pi append")
    context_path = cwd / "AGENTS.md"
    _write(context_path, "context")
    context = load_millforge_base_context(cwd=cwd, home_directory=home, enabled=True)

    project_native = _prompt(
        options=MillforgeBaseOptions(), context=context, cwd=cwd, home=home
    )
    expected = (
        "project native\n\nproject append"
        f'{_CONTEXT_PREFIX}<project_instructions path="{context_path.resolve().as_posix()}">\n'
        "context\n</project_instructions>\n\n"
        f"{_CONTEXT_SUFFIX}{_footer(cwd)}"
    )
    assert project_native.system_instructions == expected

    (cwd / ".millforge" / "SYSTEM.md").unlink()
    (cwd / ".millforge" / "APPEND_SYSTEM.md").unlink()
    assert _prompt(
        options=MillforgeBaseOptions(),
        context=_empty_context(cwd, home),
        cwd=cwd,
        home=home,
    ).system_instructions.startswith("project pi\n\nproject pi append")

    (cwd / ".pi" / "SYSTEM.md").unlink()
    (cwd / ".pi" / "APPEND_SYSTEM.md").unlink()
    assert _prompt(
        options=MillforgeBaseOptions(),
        context=_empty_context(cwd, home),
        cwd=cwd,
        home=home,
    ).system_instructions.startswith("global native")

    (home / ".millforge" / "SYSTEM.md").unlink()
    assert _prompt(
        options=MillforgeBaseOptions(),
        context=_empty_context(cwd, home),
        cwd=cwd,
        home=home,
    ).system_instructions.startswith("global pi")

    direct = _prompt(
        options=MillforgeBaseOptions(
            system_prompt="direct", append_system_prompt="append"
        ),
        context=_empty_context(cwd, home),
        cwd=cwd,
        home=home,
    )
    assert direct.system_instructions == f"direct\n\nappend{_footer(cwd)}"

    for path in (
        home / ".pi" / "agent" / "SYSTEM.md",
        home / ".pi" / "agent" / "APPEND_SYSTEM.md",
    ):
        if path.exists():
            path.unlink()
    default = _prompt(
        options=MillforgeBaseOptions(),
        context=_empty_context(cwd, home),
        cwd=cwd,
        home=home,
    )
    assert default.system_instructions == _DEFAULT_SYSTEM_PROMPT + _footer(cwd)
    assert (
        default.effective_prompt_sha256
        == hashlib.sha256(default.system_instructions.encode("utf-8")).hexdigest()
    )


def test_prompt_allocation_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = _workspace(tmp_path)
    empty = _empty_context(cwd, home)
    footer = _footer(cwd)
    footer_bytes = len(footer.encode("utf-8"))

    with monkeypatch.context() as patch:
        patch.setattr("millforge.base.prompt._PROMPT_BYTE_LIMIT", footer_bytes - 1)
        with pytest.raises(MillforgeBasePromptBudgetError):
            _prompt(
                options=MillforgeBaseOptions(system_prompt="body"),
                context=empty,
                cwd=cwd,
                home=home,
            )

    exact_body = "x" * (_PROMPT_BYTE_LIMIT - footer_bytes)
    exact = _prompt(
        options=MillforgeBaseOptions(system_prompt=exact_body),
        context=empty,
        cwd=cwd,
        home=home,
    )
    assert exact.system_instructions == exact_body + footer
    assert exact.byte_count == _PROMPT_BYTE_LIMIT
    assert exact.truncated is False

    body_truncated = _prompt(
        options=MillforgeBaseOptions(system_prompt="x" * _PROMPT_BYTE_LIMIT),
        context=empty,
        cwd=cwd,
        home=home,
    )
    assert body_truncated.system_instructions == exact_body + footer
    assert body_truncated.truncated is True

    append_prefix_omitted = _prompt(
        options=MillforgeBaseOptions(
            system_prompt="x" * (_PROMPT_BYTE_LIMIT - footer_bytes - 1),
            append_system_prompt="append",
        ),
        context=empty,
        cwd=cwd,
        home=home,
    )
    assert append_prefix_omitted.system_instructions.endswith(footer)
    assert "append" not in append_prefix_omitted.system_instructions
    assert append_prefix_omitted.truncated is True

    append_content_truncated = _prompt(
        options=MillforgeBaseOptions(
            system_prompt="x"
            * (_PROMPT_BYTE_LIMIT - footer_bytes - len(_APPEND_PREFIX) - 1),
            append_system_prompt="append",
        ),
        context=empty,
        cwd=cwd,
        home=home,
    )
    assert append_content_truncated.system_instructions.endswith(
        f"{_APPEND_PREFIX}a{footer}"
    )
    assert append_content_truncated.truncated is True

    context_file = _context_file(cwd / "context.md", "content")
    context = MillforgeBaseContextSnapshot(
        files=(context_file,), diagnostics=(), context_sha256="0" * 64, truncated=False
    )
    context_prefix_omitted = _prompt(
        options=MillforgeBaseOptions(
            system_prompt="x"
            * (
                _PROMPT_BYTE_LIMIT
                - footer_bytes
                - len(_CONTEXT_PREFIX)
                - len(_CONTEXT_SUFFIX)
                + 1
            )
        ),
        context=context,
        cwd=cwd,
        home=home,
    )
    assert "<project_context>" not in context_prefix_omitted.system_instructions
    assert context_prefix_omitted.truncated is True

    record_prefix = f'<project_instructions path="{context_file.path.as_posix()}">\n'
    record_suffix = "\n</project_instructions>\n\n"
    record_capacity = (
        _PROMPT_BYTE_LIMIT
        - footer_bytes
        - 1
        - len(_CONTEXT_PREFIX + record_prefix + record_suffix + _CONTEXT_SUFFIX)
    )
    oversized_record = _context_file(cwd / "context.md", "x" * (record_capacity + 1))
    record_truncated = _prompt(
        options=MillforgeBaseOptions(system_prompt="b"),
        context=MillforgeBaseContextSnapshot(
            files=(oversized_record,),
            diagnostics=(),
            context_sha256="0" * 64,
            truncated=False,
        ),
        cwd=cwd,
        home=home,
    )
    assert record_truncated.system_instructions.endswith(
        f"{record_suffix}{_CONTEXT_SUFFIX}{footer}"
    )
    assert record_truncated.truncated is True

    first = _context_file(cwd / "first.md", "")
    second = _context_file(cwd / "second.md", "later")
    first_prefix = f'<project_instructions path="{first.path.as_posix()}">\n'
    body = "x" * (
        _PROMPT_BYTE_LIMIT
        - footer_bytes
        - len(_CONTEXT_PREFIX + first_prefix + record_suffix + _CONTEXT_SUFFIX)
    )
    next_record_omitted = _prompt(
        options=MillforgeBaseOptions(system_prompt=body),
        context=MillforgeBaseContextSnapshot(
            files=(first, second),
            diagnostics=(),
            context_sha256="0" * 64,
            truncated=False,
        ),
        cwd=cwd,
        home=home,
    )
    assert first_prefix in next_record_omitted.system_instructions
    assert second.path.as_posix() not in next_record_omitted.system_instructions
    assert next_record_omitted.truncated is True
