from __future__ import annotations

from pathlib import Path
import threading

from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatSideEffectState,
)
from millforge.tools.pi_compat.editing import execute_edit
from millforge.tools.pi_compat.mutations import _path_locks, file_mutation_lock


def test_edit_replaces_multiple_disjoint_original_regions(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should replace multiple disjoint regions in one call"
    target = tmp_path / "edit-multi.txt"
    target.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8", newline="")

    result = execute_edit(
        cwd=tmp_path,
        path="edit-multi.txt",
        edits=[
            {"oldText": "alpha\n", "newText": "ALPHA\n"},
            {"oldText": "gamma\n", "newText": "GAMMA\n"},
        ],
    )

    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\ndelta\n"
    assert result.model_text == "Successfully replaced 2 block(s) in edit-multi.txt."
    assert result.changed_path == target
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE


def test_edit_matches_all_replacements_against_the_original_file(
    tmp_path: Path,
) -> None:
    # Pi source: test/tools.test.ts - "should match edits against the original file, not incrementally"
    target = tmp_path / "edit-original.txt"
    target.write_text("foo\nbar\nbaz\n", encoding="utf-8", newline="")

    result = execute_edit(
        cwd=tmp_path,
        path="edit-original.txt",
        edits=[
            {"oldText": "foo\n", "newText": "foo bar\n"},
            {"oldText": "bar\n", "newText": "BAR\n"},
        ],
    )

    assert result.error_kind is None
    assert target.read_text(encoding="utf-8") == "foo bar\nBAR\nbaz\n"


def test_edit_rejects_duplicate_or_overlapping_matches_without_writing(
    tmp_path: Path,
) -> None:
    # Pi source: test/tools.test.ts - "should fail if text appears multiple times"
    duplicate_target = tmp_path / "duplicate.txt"
    duplicate_target.write_text("foo foo foo", encoding="utf-8", newline="")
    duplicate_result = execute_edit(
        cwd=tmp_path,
        path="duplicate.txt",
        edits=[{"oldText": "foo", "newText": "bar"}],
    )
    assert duplicate_result.error_kind is PiCompatErrorKind.CONFLICT
    assert "Found 3 occurrences" in duplicate_result.model_text
    assert duplicate_target.read_text(encoding="utf-8") == "foo foo foo"

    # Pi source: test/tools.test.ts - "should fail when multi-edit regions overlap"
    overlap_target = tmp_path / "overlap.txt"
    overlap_target.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")
    overlap_result = execute_edit(
        cwd=tmp_path,
        path="overlap.txt",
        edits=[
            {"oldText": "one\ntwo\n", "newText": "ONE\nTWO\n"},
            {"oldText": "two\nthree\n", "newText": "TWO\nTHREE\n"},
        ],
    )
    assert overlap_result.error_kind is PiCompatErrorKind.CONFLICT
    assert "overlap" in overlap_result.model_text
    assert overlap_target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_edit_is_all_or_nothing_when_one_original_match_is_missing(
    tmp_path: Path,
) -> None:
    # Pi source: test/tools.test.ts - "should not partially apply edits when one edit fails"
    target = tmp_path / "no-partial.txt"
    original = "alpha\nbeta\ngamma\n"
    target.write_text(original, encoding="utf-8", newline="")

    result = execute_edit(
        cwd=tmp_path,
        path="no-partial.txt",
        edits=[
            {"oldText": "alpha\n", "newText": "ALPHA\n"},
            {"oldText": "missing\n", "newText": "MISSING\n"},
        ],
    )

    assert result.error_kind is PiCompatErrorKind.CONFLICT
    assert "Could not find edits[1]" in result.model_text
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
    assert target.read_text(encoding="utf-8") == original


def test_edit_uses_pi_fuzzy_unicode_and_whitespace_normalization(
    tmp_path: Path,
) -> None:
    # Pi source: test/tools.test.ts - "should match text with trailing whitespace stripped"
    target = tmp_path / "fuzzy.txt"
    target.write_text(
        "console.log(\u2018hello\u2019);\nhello\u00a0world\n",
        encoding="utf-8",
        newline="",
    )

    result = execute_edit(
        cwd=tmp_path,
        path="fuzzy.txt",
        edits=[
            {
                "oldText": "console.log('hello');\n",
                "newText": "console.log('world');\n",
            },
            {"oldText": "hello world\n", "newText": "hello universe\n"},
        ],
    )

    assert result.error_kind is None
    assert target.read_text(encoding="utf-8") == (
        "console.log('world');\nhello universe\n"
    )


def test_edit_preserves_bom_and_crlf_in_multi_edit_mode(tmp_path: Path) -> None:
    # Pi source: test/tools.test.ts - "should preserve CRLF line endings and BOM in multi-edit mode"
    target = tmp_path / "bom-crlf.txt"
    target.write_bytes("\ufefffirst\r\nsecond\r\nthird\r\nfourth\r\n".encode("utf-8"))

    result = execute_edit(
        cwd=tmp_path,
        path="bom-crlf.txt",
        edits=[
            {"oldText": "second\n", "newText": "SECOND\n"},
            {"oldText": "fourth\n", "newText": "FOURTH\n"},
        ],
    )

    assert result.error_kind is None
    assert target.read_bytes() == "\ufefffirst\r\nSECOND\r\nthird\r\nFOURTH\r\n".encode(
        "utf-8"
    )


def test_mutation_queue_serializes_one_path_and_releases_idle_entries(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6: file-mutation-queue.ts serializes a path then removes its queue.
    target = tmp_path / "shared.txt"
    entered = threading.Event()
    release = threading.Event()
    second_completed = threading.Event()

    def hold_first() -> None:
        with file_mutation_lock(target):
            entered.set()
            assert release.wait(timeout=5)

    def wait_second() -> None:
        assert entered.wait(timeout=5)
        with file_mutation_lock(target):
            second_completed.set()

    first = threading.Thread(target=hold_first)
    second = threading.Thread(target=wait_second)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    assert not second_completed.wait(timeout=0.1)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_completed.is_set()
    assert _path_locks == {}


def test_mutation_queue_allows_independent_paths_to_enter_together(
    tmp_path: Path,
) -> None:
    # Pi 0.79.6: file-mutation-queue.ts keeps distinct path operations parallel.
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def hold(path: Path, entered: threading.Event) -> None:
        with file_mutation_lock(path):
            entered.set()
            assert release.wait(timeout=5)

    first = threading.Thread(target=hold, args=(tmp_path / "one.txt", first_entered))
    second = threading.Thread(target=hold, args=(tmp_path / "two.txt", second_entered))
    first.start()
    second.start()
    assert first_entered.wait(timeout=5)
    assert second_entered.wait(timeout=5)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert _path_locks == {}
