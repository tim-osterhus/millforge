"""Pi-derived parity and regression coverage for the search compatibility slice."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import sys

import pytest

from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatSideEffectState,
)
from millforge.tools.pi_compat.search import execute_find, execute_grep
import millforge.tools.pi_compat.search as search


_FIXTURE_TREE = (
    Path(__file__).parent / "fixtures" / "pi_compat" / "v0.79.6" / "search" / "tree"
)


@pytest.fixture
def search_tree(tmp_path: Path) -> Path:
    target = tmp_path / "tree"
    shutil.copytree(_FIXTURE_TREE, target)
    return target


def test_grep_file_context_limit_and_notice_match_pi_format(search_tree: Path) -> None:
    # Source: packages/coding-agent/test/tools.test.ts -
    # "should respect global limit and include context lines".
    result = execute_grep(
        cwd=search_tree,
        pattern="match",
        path="alpha.txt",
        context=1,
        limit=1,
    )

    assert result.model_text == (
        "alpha.txt-1- before\n"
        "alpha.txt:2: match one\n"
        "alpha.txt-3- after\n\n"
        "[1 matches limit reached. Use limit=2 for more, or refine pattern]"
    )
    assert result.truncated is True
    assert result.error_kind is None
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED


def test_grep_literal_ignore_case_binary_and_invalid_utf8(search_tree: Path) -> None:
    # Source: packages/coding-agent/src/core/tools/grep.ts - literal,
    # ignoreCase, binary skipping, and UTF-8 line rendering adaptations.
    (search_tree / "binary.txt").write_bytes(b"match\x00not searched\n")
    (search_tree / "invalid.txt").write_bytes(b"\xffMATCH\n")

    literal = execute_grep(
        cwd=search_tree,
        pattern="MATCH",
        path="invalid.txt",
        ignoreCase=True,
        literal=True,
    )
    binary = execute_grep(cwd=search_tree, pattern="match", path="binary.txt")

    assert literal.model_text == "invalid.txt:1: \ufffdMATCH"
    assert binary.model_text == "No matches found"


def test_grep_glob_uses_relative_posix_paths_and_skips_ignored_files(
    search_tree: Path,
) -> None:
    # Source: packages/coding-agent/src/core/tools/grep.ts - --glob handling;
    # Spec 11 section 7 supplies the deterministic Python traversal adaptation.
    result = execute_grep(
        cwd=search_tree,
        pattern="match",
        glob="nested/*.txt",
    )

    assert result.model_text == "nested/kept.txt:1: nested match"


def test_find_treats_a_leading_bang_as_a_literal_glob_character(
    search_tree: Path,
) -> None:
    # Pi 0.79.6: find.ts passes the pattern to fd --glob; it is not gitignore.
    (search_tree / "!literal.py").write_text("", encoding="utf-8")
    (search_tree / "ordinary.py").write_text("", encoding="utf-8")

    result = execute_find(cwd=search_tree, pattern="!*.py")

    assert result.model_text == "!literal.py"


def test_find_trailing_slash_patterns_do_not_invent_directory_semantics(
    search_tree: Path,
) -> None:
    # Pi 0.79.6: find.ts passes trailing slashes directly to fd --glob.
    (search_tree / "nested" / "folder").mkdir()
    (search_tree / "nested" / "folder" / "child.txt").write_text("", encoding="utf-8")

    result = execute_find(cwd=search_tree, pattern="nested/folder/")

    assert result.model_text == "No files found matching pattern"


def test_find_empty_pattern_matches_all_eligible_entries_in_order(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6: find.ts forwards an empty Type.String pattern to fd --glob.
    (tmp_path / "nested").mkdir()
    (tmp_path / "alpha.txt").write_text("", encoding="utf-8")
    (tmp_path / "nested" / "child.txt").write_text("", encoding="utf-8")

    result = execute_find(cwd=tmp_path, pattern="")

    assert result.model_text.splitlines() == [
        "alpha.txt",
        "nested/",
        "nested/child.txt",
    ]
    assert result.truncated is False


def test_grep_invalid_regex_is_an_invalid_arguments_result(search_tree: Path) -> None:
    # Source: packages/coding-agent/test/tools.test.ts - grep inputs are passed
    # to ripgrep as a regex; Spec 11 adapts the engine to Python re.
    result = execute_grep(cwd=search_tree, pattern="[")

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.model_text.startswith("Invalid regex pattern:")
    assert result.truncated is False


def test_find_includes_hidden_files_and_nested_gitignore_scope_in_order(
    search_tree: Path,
) -> None:
    # Source: packages/coding-agent/test/tools.test.ts -
    # "should include hidden files that are not gitignored" and
    # "should respect .gitignore".
    result = execute_find(cwd=search_tree, pattern="**/*.txt")

    assert result.model_text.splitlines() == [
        ".hidden/hidden.txt",
        "alpha.txt",
        "nested/kept.txt",
        "sibling/shadow.txt",
    ]
    assert result.truncated is False


def test_find_path_globs_match_full_relative_paths(search_tree: Path) -> None:
    # Source: packages/coding-agent/test/suite/regressions/3302-find-path-glob.test.ts -
    # "src/**/*.spec.ts matches nested spec file".
    (search_tree / "src" / "foo" / "bar").mkdir(parents=True)
    (search_tree / "src" / "foo" / "bar" / "example.spec.ts").write_text(
        "", encoding="utf-8"
    )

    result = execute_find(cwd=search_tree, pattern="src/**/*.spec.ts")

    assert result.model_text == "src/foo/bar/example.spec.ts"


def test_find_nested_gitignore_does_not_leak_to_siblings(tmp_path: Path) -> None:
    # Source: packages/coding-agent/test/suite/regressions/3303-find-nested-gitignore.test.ts -
    # "applies a/.gitignore only inside a/ and leaves b/ untouched".
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    for directory in ("a", "b"):
        (tmp_path / directory / "ignored.txt").write_text("", encoding="utf-8")
        (tmp_path / directory / "kept.txt").write_text("", encoding="utf-8")

    result = execute_find(cwd=tmp_path, pattern="**/*.txt")

    assert result.model_text.splitlines() == [
        "a/kept.txt",
        "b/ignored.txt",
        "b/kept.txt",
    ]


def test_find_nested_gitignore_negation_reincludes_one_tmp_sibling(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6 find.ts relies on fd's hierarchical gitignore precedence.
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (nested / ".gitignore").write_text("!kept.tmp\n", encoding="utf-8")
    (nested / "kept.tmp").write_text("", encoding="utf-8")
    (nested / "ignored.tmp").write_text("", encoding="utf-8")

    result = execute_find(cwd=tmp_path, pattern="*.tmp")

    assert result.model_text == "nested/kept.tmp"


def test_find_includes_symlinks_without_descending_into_directory_symlinks(
    tmp_path: Path,
) -> None:
    # Source: packages/coding-agent/src/core/tools/find.ts - fd result paths;
    # Spec 11 section 7 explicitly ports symlink inclusion without recursion.
    target_directory = tmp_path / "target-directory"
    target_directory.mkdir()
    (target_directory / "nested.txt").write_text("nested", encoding="utf-8")
    target_file = tmp_path / "target-file.txt"
    target_file.write_text("file", encoding="utf-8")
    try:
        (tmp_path / "link-directory").symlink_to(
            target_directory, target_is_directory=True
        )
        (tmp_path / "link-file").symlink_to(target_file)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable on this host: {exc}")

    result = execute_find(cwd=tmp_path, pattern="link-*")

    assert result.model_text.splitlines() == ["link-directory", "link-file"]


def test_find_marks_result_limit_and_byte_limit_in_specified_notice_order(
    tmp_path: Path,
) -> None:
    # Source: packages/coding-agent/src/core/tools/find.ts - result-limit and
    # truncateHead notices; Spec 11 section 7 fixes their ordering and bounds.
    for index in range(1_000):
        (tmp_path / f"result-{index:04d}-{'x' * 60}.txt").write_text(
            "", encoding="utf-8"
        )

    result = execute_find(cwd=tmp_path, pattern="result-*.txt", limit=1_000)

    assert result.truncated is True
    assert (
        "[1000 results limit reached. Use limit=2000 for more, or refine pattern. 50.0KB limit reached]"
        in result.model_text
    )
    raw_output = result.model_text.split("\n\n[", maxsplit=1)[0]
    assert len(raw_output.encode("utf-8")) <= 50 * 1024
    assert raw_output.endswith(".txt")


def test_grep_long_lines_and_unreadable_descendants_set_truncation(
    search_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Source: packages/coding-agent/src/core/tools/grep.ts - 500-character
    # line truncation; Spec 11 section 7 adds deterministic unreadable notices.
    (search_tree / "long.txt").write_text(f"match {'x' * 510}\n", encoding="utf-8")
    denied_path = search_tree / "z-denied.txt"
    denied_path.write_text("match denied\n", encoding="utf-8")
    original_read = search._read_search_file

    def fail_for_one_descendant(path: Path) -> bytes | None:
        if path == denied_path:
            raise PermissionError("denied")
        return original_read(path)

    monkeypatch.setattr(search, "_read_search_file", fail_for_one_descendant)
    result = execute_grep(cwd=search_tree, pattern="match", glob="*.txt")

    assert "long.txt:1: match " in result.model_text
    assert (
        "Some lines truncated to 500 chars. Use read tool to see full lines"
        in result.model_text
    )
    assert result.model_text.endswith("Skipped 1 unreadable path(s)]")
    assert result.truncated is True


def test_grep_uses_utf16_character_limits_without_surrogate_splitting(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6: truncate.ts truncateLine slices JavaScript UTF-16 units.
    (tmp_path / "astral.txt").write_text(
        "match " + "\U0001f642" * 251 + "\n", encoding="utf-8"
    )
    (tmp_path / "boundary.txt").write_text("x" * 499 + "\U0001f642\n", encoding="utf-8")

    astral = execute_grep(cwd=tmp_path, pattern="match", path="astral.txt")
    boundary = execute_grep(cwd=tmp_path, pattern="x", path="boundary.txt")

    assert astral.model_text == (
        "astral.txt:1: match " + "\U0001f642" * 247 + "... [truncated]\n\n"
        "[Some lines truncated to 500 chars. Use read tool to see full lines]"
    )
    assert boundary.model_text.startswith("boundary.txt:1: " + "x" * 499)
    assert "\U0001f642" not in boundary.model_text
    assert boundary.model_text.endswith(
        "[Some lines truncated to 500 chars. Use read tool to see full lines]"
    )


def test_grep_and_find_numeric_edge_cases_are_closed_and_source_compatible(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6: grep.ts uses Math.max; find.ts sends limit to fd --max-results.
    (tmp_path / "matches.txt").write_text(
        "before\nmatch one\nafter\nmatch two\n", encoding="utf-8"
    )
    (tmp_path / "one.txt").write_text("", encoding="utf-8")
    (tmp_path / "two.txt").write_text("", encoding="utf-8")

    negative_context = execute_grep(
        cwd=tmp_path, pattern="match one", path="matches.txt", context=-1
    )
    fractional_context = execute_grep(
        cwd=tmp_path, pattern="match one", path="matches.txt", context=1.5
    )
    fractional_limit = execute_grep(cwd=tmp_path, pattern="match", limit=1.5)
    zero_find_limit = execute_find(cwd=tmp_path, pattern="*.txt", limit=0)
    whole_float_find_limit = execute_find(cwd=tmp_path, pattern="*.txt", limit=1.0)

    assert negative_context.model_text == "matches.txt:2: match one"
    assert fractional_context.model_text == (
        "matches.txt-1- before\nmatches.txt:2: match one\nmatches.txt-3- after"
    )
    assert fractional_limit.model_text.endswith(
        "[1.5 matches limit reached. Use limit=3 for more, or refine pattern]"
    )
    assert fractional_limit.model_text.count(": match") == 2
    assert zero_find_limit.model_text == (
        "matches.txt\none.txt\ntwo.txt\n\n"
        "[0 results limit reached. Use limit=0 for more, or refine pattern]"
    )
    assert zero_find_limit.truncated is True
    assert whole_float_find_limit.model_text.endswith(
        "[1 results limit reached. Use limit=2 for more, or refine pattern]"
    )

    for value in (-1, 1.5, float("inf"), float("nan"), sys.maxsize + 1):
        result = execute_find(cwd=tmp_path, pattern="*.txt", limit=value)
        assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
        assert result.model_text == "find limit must be a non-negative integer" or (
            isinstance(value, float)
            and result.model_text == "find limit must be a number"
        )

    for value in (float("inf"), float("nan")):
        result = execute_grep(cwd=tmp_path, pattern="match", context=value)
        assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
        assert result.model_text == "grep context must be a number"


def test_search_closes_nul_paths(tmp_path: Path) -> None:
    for result in (
        execute_grep(cwd=tmp_path, pattern="match", path="bad\x00.txt"),
        execute_find(cwd=tmp_path, pattern="*", path="bad\x00"),
    ):
        assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
        assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
        assert "NUL" in result.model_text


def test_search_renders_posix_byte_filenames(tmp_path: Path) -> None:
    # Millforge 11A QA: search results stay UTF-8 display-safe.
    if os.name == "nt":
        pytest.skip("POSIX byte filenames are not available on Windows")
    filename = b"byte-name-\xff.txt"
    try:
        descriptor = os.open(
            os.fsencode(tmp_path) + b"/" + filename,
            os.O_WRONLY | os.O_CREAT,
            0o600,
        )
    except OSError as error:
        if error.errno == errno.EILSEQ:
            pytest.skip("host filesystem rejects non-UTF-8 filename bytes")
        raise
    try:
        os.write(descriptor, b"match\n")
    finally:
        os.close(descriptor)

    result = execute_grep(cwd=tmp_path, pattern="match")
    assert result.model_text == "byte-name-\ufffd.txt:1: match"
    assert all(
        not 0xD800 <= ord(character) <= 0xDFFF for character in result.model_text
    )


def test_search_root_failures_are_closed_operation_errors(search_tree: Path) -> None:
    # Source: packages/coding-agent/src/core/tools/grep.ts and find.ts -
    # missing roots; Spec 11 section 7 defines closed error results.
    missing = execute_grep(cwd=search_tree, pattern="match", path="does-not-exist")
    invalid_find_root = execute_find(cwd=search_tree, pattern="*", path="alpha.txt")
    invalid_glob = execute_find(cwd=search_tree, pattern="[")

    assert missing.error_kind is PiCompatErrorKind.NOT_FOUND
    assert invalid_find_root.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert invalid_glob.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX file modes do not reliably deny the owner on Windows"
)
def test_unreadable_explicit_grep_file_is_a_permission_error(tmp_path: Path) -> None:
    # Source: packages/coding-agent/src/core/tools/grep.ts - explicit file
    # roots; Spec 11 section 7 distinguishes them from skipped descendants.
    restricted = tmp_path / "restricted.txt"
    restricted.write_text("match\n", encoding="utf-8")
    restricted.chmod(0)
    try:
        result = execute_grep(cwd=tmp_path, pattern="match", path="restricted.txt")
    finally:
        restricted.chmod(0o600)

    assert result.error_kind is PiCompatErrorKind.PERMISSION_DENIED
