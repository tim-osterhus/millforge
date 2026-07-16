from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

from millforge.tools import pi_compat
from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatSideEffectState,
)
from millforge.tools.pi_compat.operations import (
    execute_ls,
    execute_read,
    execute_write,
)
from millforge.tools.pi_compat.paths import expand_path, resolve_read_path
from millforge.tools.pi_compat.truncation import format_size


REPO_ROOT = Path(__file__).resolve().parents[1]
PI_COMPAT = REPO_ROOT / "src" / "millforge" / "tools" / "pi_compat"

_EXPECTED_ATTRIBUTION_SHA256 = {
    "PI_LICENSE": "0457f5bcec3b3b211605dfb5d1a49042fd638f3686a410fe099c24a25af13c48",
    "PROVENANCE.json": "590e01669f89f052bb7fdabe0aedc8d2df4b01c8d0bcdcb6745c6331475323ed",
    "UPDATE_POLICY.md": "bff600af05515e69244c074779bd5ed5a0b8b1c1aeeee0b8d22bf1ba65e7ed7f",
}

_EXPECTED_ADAPTATIONS = [
    "Python filesystem and process APIs replace Node APIs.",
    "`pathspec` and the exact Section 7 ignore rules replace `fd`/`rg` discovery.",
    "Python regex/glob, binary detection, decoding, traversal, and error rules are the exact Section 7 search adaptation.",
    "supported image reads return the exact successful text-only result in Spec 11 Section 6 instead of multimodal image content; non-image bytes retain Pi's UTF-8 replacement-decoding behavior.",
    "the poll/wait cancellation protocol replaces Pi's abort signal.",
    "Windows uses COMSPEC/cmd.exe while POSIX preserves Pi's Bash-first order.",
    "the inherited environment omits Pi's private bin-directory PATH mutation.",
    "the 11A-owned async timer enforces the caller-supplied strictly positive finite timeout; expiry after spawn performs the specified process-tree cleanup and returns `process_timeout` with `completion_unknown`.",
]

_EXPECTED_CLASSIFICATIONS = {
    "LICENSE": "ported",
    "packages/coding-agent/package.json": "excluded",
    "packages/coding-agent/src/core/tools/index.ts": "adapted",
    "packages/coding-agent/src/core/tools/read.ts": "adapted",
    "packages/coding-agent/src/core/tools/ls.ts": "ported",
    "packages/coding-agent/src/core/tools/grep.ts": "adapted",
    "packages/coding-agent/src/core/tools/find.ts": "adapted",
    "packages/coding-agent/src/core/tools/write.ts": "ported",
    "packages/coding-agent/src/core/tools/edit.ts": "ported",
    "packages/coding-agent/src/core/tools/edit-diff.ts": "ported",
    "packages/coding-agent/src/core/tools/bash.ts": "adapted",
    "packages/coding-agent/src/core/tools/truncate.ts": "ported",
    "packages/coding-agent/src/core/tools/output-accumulator.ts": "adapted",
    "packages/coding-agent/src/core/tools/path-utils.ts": "ported",
    "packages/coding-agent/src/core/tools/file-mutation-queue.ts": "adapted",
    "packages/coding-agent/src/core/tools/render-utils.ts": "excluded",
    "packages/coding-agent/src/utils/shell.ts": "adapted",
    "packages/coding-agent/src/utils/child-process.ts": "adapted",
    "packages/coding-agent/src/utils/mime.ts": "ported",
    "packages/coding-agent/src/utils/paths.ts": "ported",
    "packages/coding-agent/test/tools.test.ts": "test-derived",
    "packages/coding-agent/test/path-utils.test.ts": "test-derived",
    "packages/coding-agent/test/bash-close-hang-windows.test.ts": "test-derived",
    "packages/coding-agent/test/suite/regressions/3302-find-path-glob.test.ts": "test-derived",
    "packages/coding-agent/test/suite/regressions/3303-find-nested-gitignore.test.ts": "test-derived",
    "packages/coding-agent/test/suite/regressions/5208-late-bash-output.test.ts": "test-derived",
    "packages/coding-agent/test/suite/regressions/5303-bash-output-truncation.test.ts": "test-derived",
    "packages/coding-agent/src/core/system-prompt.ts": "adapted",
    "packages/coding-agent/src/core/resource-loader.ts": "adapted",
    "packages/coding-agent/test/system-prompt.test.ts": "test-derived",
    "packages/coding-agent/test/resource-loader.test.ts": "test-derived",
}


def test_package_exports_only_the_11a_internal_operation_surface() -> None:
    assert pi_compat.__all__ == [
        "PiCompatCancellation",
        "PiCompatErrorKind",
        "PiCompatOperationResult",
        "PiCompatShellConfig",
        "PiCompatShellResolutionError",
        "PiCompatSideEffectState",
        "execute_bash",
        "execute_edit",
        "execute_find",
        "execute_grep",
        "execute_ls",
        "execute_read",
        "execute_write",
        "resolve_pi_compat_shell",
    ]


def test_operation_contracts_expose_the_spec_11_closed_values() -> None:
    assert {error.value for error in PiCompatErrorKind} == {
        "invalid_arguments",
        "not_found",
        "permission_denied",
        "conflict",
        "io_error",
        "process_exit_nonzero",
        "process_timeout",
        "cancelled",
        "process_launch_error",
    }


def test_sync_operations_reject_a_relative_cwd_before_any_operation_work() -> None:
    relative_cwd = Path("relative")
    results = (
        pi_compat.execute_read(cwd=relative_cwd, path="missing.txt"),
        pi_compat.execute_ls(cwd=relative_cwd),
        pi_compat.execute_grep(cwd=relative_cwd, pattern="match"),
        pi_compat.execute_find(cwd=relative_cwd, pattern="*"),
        pi_compat.execute_write(cwd=relative_cwd, path="new.txt", content="new"),
        pi_compat.execute_edit(
            cwd=relative_cwd,
            path="existing.txt",
            edits=[{"oldText": "old", "newText": "new"}],
        ),
    )

    for result in results:
        assert result.model_text == "cwd must be an absolute path"
        assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
        assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
    assert {state.value for state in PiCompatSideEffectState} == {
        "not_attempted",
        "confirmed_absent",
        "confirmed_complete",
        "rolled_back",
        "completion_unknown",
    }


def test_read_truncates_lines_with_pi_continuation_text(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should truncate files exceeding line limit"
    target = tmp_path / "large.txt"
    target.write_text(
        "\n".join(f"Line {index}" for index in range(1, 2_501)),
        encoding="utf-8",
        newline="",
    )

    result = execute_read(cwd=tmp_path, path="large.txt")

    assert result.error_kind is None
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
    assert result.truncated is True
    assert "Line 1" in result.model_text
    assert "Line 2000" in result.model_text
    assert "Line 2001" not in result.model_text
    assert (
        "[Showing lines 1-2000 of 2500. Use offset=2001 to continue.]"
        in result.model_text
    )


def test_read_offset_limit_and_replacement_decoding(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should handle offset + limit together"
    target = tmp_path / "offset-limit.txt"
    target.write_text(
        "\n".join(f"Line {index}" for index in range(1, 101)),
        encoding="utf-8",
        newline="",
    )

    result = execute_read(cwd=tmp_path, path="offset-limit.txt", offset=41, limit=20)

    assert "Line 41" in result.model_text
    assert "Line 60" in result.model_text
    assert "Line 61" not in result.model_text
    assert "[40 more lines in file. Use offset=61 to continue.]" in result.model_text

    # Pi source: test/tools.test.ts - text files use Buffer.toString("utf-8")
    invalid_utf8 = tmp_path / "invalid.txt"
    invalid_utf8.write_bytes(b"prefix \xff suffix")
    replacement_result = execute_read(cwd=tmp_path, path="invalid.txt")
    assert replacement_result.model_text == "prefix \ufffd suffix"


def test_read_returns_the_specified_text_only_image_adaptation(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should detect image MIME type from file magic (not extension)"
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4"
        "DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
    )
    target = tmp_path / "image.txt"
    target.write_bytes(png_data)

    result = execute_read(cwd=tmp_path, path="image.txt")

    assert result.model_text == (
        "Read image file [image/png]\n"
        "[Image attachment omitted: Millforge's tool-result contract is text-only. "
        f"File size: {len(png_data)} bytes.]"
    )
    assert result.truncated is False
    assert result.error_kind is None


def test_read_rejects_an_offset_beyond_the_file(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should show error when offset is beyond file length"
    target = tmp_path / "short.txt"
    target.write_text("Line 1\nLine 2\nLine 3", encoding="utf-8", newline="")

    result = execute_read(cwd=tmp_path, path="short.txt", offset=100)

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.model_text == "Offset 100 is beyond end of file (3 lines total)"
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED


def test_write_creates_parent_directories_and_reports_confirmed_completion(
    tmp_path: Path,
) -> None:
    # Pi source: test/tools.test.ts - "should create parent directories"
    result = execute_write(
        cwd=tmp_path,
        path="nested/dir/test.txt",
        content="Nested content",
    )

    target = tmp_path / "nested" / "dir" / "test.txt"
    assert target.read_text(encoding="utf-8") == "Nested content"
    assert result.model_text == "Successfully wrote 14 bytes to nested/dir/test.txt"
    assert result.changed_path == target
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE


def test_ls_includes_dotfiles_directories_and_limit_notice(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should list dotfiles and directories"
    (tmp_path / ".hidden-file").write_text("secret", encoding="utf-8")
    (tmp_path / ".hidden-dir").mkdir()
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")

    full_result = execute_ls(cwd=tmp_path)
    limited_result = execute_ls(cwd=tmp_path, limit=2)

    assert full_result.model_text.splitlines() == [
        ".hidden-dir/",
        ".hidden-file",
        "visible.txt",
    ]
    assert limited_result.model_text == (
        ".hidden-dir/\n.hidden-file\n\n[2 entries limit reached. Use limit=4 for more]"
    )
    assert limited_result.truncated is True


def test_path_expansion_and_unicode_filename_recovery(tmp_path: Path) -> None:
    # Pi source: test/path-utils.test.ts - "should normalize Unicode spaces"
    assert expand_path("@file\u00a0name.txt") == "file name.txt"

    # Pi source: test/path-utils.test.ts - "should handle curly quotes vs straight quotes (macOS filenames)"
    target = tmp_path / "Capture d\u2019cran.txt"
    target.write_text("content", encoding="utf-8")
    assert resolve_read_path("Capture d'cran.txt", tmp_path) == target

    # Pi source: test/path-utils.test.ts - "should handle macOS screenshot AM/PM variant with narrow no-break space"
    screenshot = tmp_path / "Screenshot 2024-01-01 at 10.00.00\u202fAM.png"
    screenshot.write_text("content", encoding="utf-8")
    assert (
        resolve_read_path("Screenshot 2024-01-01 at 10.00.00 AM.png", tmp_path)
        == screenshot
    )


def test_path_operations_close_embedded_nul_arguments_before_filesystem_work(
    tmp_path: Path,
) -> None:
    # Millforge 11A QA: JSON strings containing NUL must not escape an operation.
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    results = (
        execute_read(cwd=tmp_path, path="bad\x00.txt"),
        execute_ls(cwd=tmp_path, path="bad\x00"),
        execute_write(cwd=tmp_path, path="bad\x00.txt", content="new"),
        pi_compat.execute_edit(
            cwd=tmp_path,
            path="bad\x00.txt",
            edits=[{"oldText": "before", "newText": "after"}],
        ),
    )

    for result in results:
        assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
        assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
        assert "NUL" in result.model_text
    assert target.read_text(encoding="utf-8") == "before"


def test_file_url_decoded_nul_and_unencodable_paths_close_all_operations(
    tmp_path: Path,
) -> None:
    # Millforge 11A QA: validate paths after file:// percent decoding.
    for bad_path in (f"{tmp_path.as_uri()}%00", "\ud800"):
        results = (
            execute_read(cwd=tmp_path, path=bad_path),
            execute_ls(cwd=tmp_path, path=bad_path),
            execute_write(cwd=tmp_path, path=bad_path, content="new"),
            pi_compat.execute_edit(
                cwd=tmp_path,
                path=bad_path,
                edits=[{"oldText": "before", "newText": "after"}],
            ),
            pi_compat.execute_grep(cwd=tmp_path, pattern="before", path=bad_path),
            pi_compat.execute_find(cwd=tmp_path, pattern="*", path=bad_path),
        )

        for result in results:
            assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
            assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
            assert "\x00" not in result.model_text


def test_write_closes_unencodable_content_after_mutation_entry(tmp_path: Path) -> None:
    # Millforge 11A QA: a Python-only lone surrogate cannot raise from write().
    result = execute_write(cwd=tmp_path, path="surrogate.txt", content="\ud800")

    assert result.error_kind is PiCompatErrorKind.IO_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert all(
        not 0xD800 <= ord(character) <= 0xDFFF for character in result.model_text
    )


def test_posix_surrogateescape_filename_is_recoverable_and_display_safe(
    tmp_path: Path,
) -> None:
    # Millforge 11A QA: POSIX byte filenames must not fail UTF-8 result formatting.
    if os.name == "nt":
        return

    filename = b"undecodable-\xff.txt"
    byte_path = os.fsencode(tmp_path) + b"/" + filename
    descriptor = os.open(byte_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, b"contents")
    finally:
        os.close(descriptor)

    surrogate_path = os.fsdecode(filename)
    read_result = execute_read(cwd=tmp_path, path=surrogate_path)
    list_result = execute_ls(cwd=tmp_path)

    assert read_result.model_text == "contents"
    assert "undecodable-\ufffd.txt" in list_result.model_text
    assert all(
        not 0xD800 <= ord(character) <= 0xDFFF for character in list_result.model_text
    )


def test_format_size_uses_javascript_to_fixed_half_up_ties() -> None:
    # Pi 0.79.6: packages/coding-agent/src/core/tools/truncate.ts formatSize.
    assert format_size(1_280) == "1.3KB"
    assert format_size(2_304) == "2.3KB"
    assert format_size(1_310_720) == "1.3MB"


def test_pi_license_and_provenance_match_the_pinned_11a_packet() -> None:
    # Millforge 11A packet: retain exact attribution and pinned source records.
    manifest = json.loads((PI_COMPAT / "PROVENANCE.json").read_text(encoding="utf-8"))

    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            PI_COMPAT / "PI_LICENSE",
            PI_COMPAT / "PROVENANCE.json",
            PI_COMPAT / "UPDATE_POLICY.md",
        )
    } == _EXPECTED_ATTRIBUTION_SHA256
    assert manifest["upstream"] == {
        "repository": "https://github.com/earendil-works/pi",
        "package": "@earendil-works/pi-coding-agent",
        "version": "0.79.6",
        "license": "MIT",
        "copyright": "Copyright (c) 2025 Mario Zechner",
    }
    assert manifest["source_snapshot"] == "reference/pi/"
    assert manifest["source_history"] == (
        "The approved reference copy is history-free. "
        "No upstream commit hash is recorded or inferred."
    )
    assert manifest["adaptations"] == _EXPECTED_ADAPTATIONS
    pinned_paths = manifest["pinned_paths"]
    assert len(pinned_paths) == 31
    assert len({entry["path"] for entry in pinned_paths}) == len(pinned_paths)
    assert all(
        len(entry["sha256"]) == 64
        and entry["sha256"] == entry["sha256"].lower()
        and all(character in "0123456789abcdef" for character in entry["sha256"])
        for entry in pinned_paths
    )
    assert {
        entry["path"]: entry["classification"] for entry in pinned_paths
    } == _EXPECTED_CLASSIFICATIONS
    assert {entry["classification"] for entry in pinned_paths} == {
        "ported",
        "adapted",
        "excluded",
        "test-derived",
    }
    assert (
        next(entry for entry in pinned_paths if entry["path"] == "LICENSE")["sha256"]
        == _EXPECTED_ATTRIBUTION_SHA256["PI_LICENSE"]
    )


def test_pi_compat_license_and_provenance_ship_in_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    # Millforge 11A packet: package the attribution files in both build artifacts.
    output_directory = tmp_path / "dist"
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output_directory)],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    wheel_path = next(output_directory.glob("millforge-*.whl"))
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = set(wheel.namelist())
    assert {
        "millforge/tools/pi_compat/PI_LICENSE",
        "millforge/tools/pi_compat/PROVENANCE.json",
        "millforge/tools/pi_compat/UPDATE_POLICY.md",
    } <= wheel_names

    sdist_path = next(output_directory.glob("millforge-*.tar.gz"))
    with tarfile.open(sdist_path) as sdist:
        sdist_names = {
            "/".join(Path(member.name).parts[1:]) for member in sdist.getmembers()
        }
    assert {
        "src/millforge/tools/pi_compat/PI_LICENSE",
        "src/millforge/tools/pi_compat/PROVENANCE.json",
        "src/millforge/tools/pi_compat/UPDATE_POLICY.md",
    } <= sdist_names
