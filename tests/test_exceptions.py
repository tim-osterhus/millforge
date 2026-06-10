"""Tests for the millforge exception hierarchy."""

from __future__ import annotations

import pytest

from millforge import (
    ArtifactWriteError,
    BackendTranslationError,
    DeadlineExceededError,
    HarnessMismatchError,
    MillforgeConfigError,
    MillforgeError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)

# ---------------------------------------------------------------------------
# Every exception is exported in __all__
# ---------------------------------------------------------------------------


def test_all_exceptions_exported() -> None:
    from millforge import __all__ as exported

    expected = {
        "ArtifactWriteError",
        "BackendTranslationError",
        "DeadlineExceededError",
        "HarnessMismatchError",
        "MillforgeConfigError",
        "MillforgeError",
        "ModelTransportError",
        "OperationCancelledError",
        "ToolInvokeError",
    }
    exported_set = set(exported)
    assert expected.issubset(exported_set), f"Missing: {expected - exported_set}"


# ---------------------------------------------------------------------------
# All concrete exceptions are MillforgeError subclasses
# ---------------------------------------------------------------------------

CONCRETE_EXCEPTIONS = [
    MillforgeConfigError,
    HarnessMismatchError,
    BackendTranslationError,
    ModelTransportError,
    ToolInvokeError,
    DeadlineExceededError,
    OperationCancelledError,
    ArtifactWriteError,
]


class TestHierarchy:
    """Verify every concrete exception is a MillforgeError subclass."""

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_is_millforge_error(self, exc_cls: type[MillforgeError]) -> None:
        assert issubclass(exc_cls, MillforgeError)
        assert issubclass(exc_cls, Exception)

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_can_be_raised_and_caught_as_millforge(
        self, exc_cls: type[MillforgeError]
    ) -> None:
        """Each exception can be raised and caught as MillforgeError."""
        with pytest.raises(MillforgeError) as exc_info:
            raise exc_cls("test message")
        assert isinstance(exc_info.value, exc_cls)

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_can_be_raised_and_caught_independently(
        self, exc_cls: type[MillforgeError]
    ) -> None:
        """Each exception can be raised and caught by its own type."""
        with pytest.raises(exc_cls) as exc_info:
            raise exc_cls("independent catch")
        assert isinstance(exc_info.value, exc_cls)


# ---------------------------------------------------------------------------
# Message propagation
# ---------------------------------------------------------------------------


class TestMessagePropagation:
    """Verify message strings are propagated correctly."""

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_message_stored(self, exc_cls: type[MillforgeError]) -> None:
        msg = f"testing {exc_cls.__name__}"
        exc = exc_cls(msg)
        assert str(exc) == msg
        assert exc.args[0] == msg

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_multiple_args_not_expected(self, exc_cls: type[MillforgeError]) -> None:
        """Our exceptions accept a single message string."""
        msg = "single string message"
        exc = exc_cls(msg)
        assert str(exc) == msg


# ---------------------------------------------------------------------------
# Cause chaining
# ---------------------------------------------------------------------------


class TestCauseChaining:
    """Verify __cause__ chaining works correctly."""

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_cause_passed(self, exc_cls: type[MillforgeError]) -> None:
        cause = ValueError("root cause")
        exc = exc_cls("wrapping message", cause=cause)
        assert exc._cause is cause
        assert exc.__cause__ is cause

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_cause_omitted_by_default(self, exc_cls: type[MillforgeError]) -> None:
        exc = exc_cls("no cause")
        assert exc._cause is None
        assert exc.__cause__ is None

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_implicit_chaining_not_overwritten(
        self, exc_cls: type[MillforgeError]
    ) -> None:
        """Python's implicit __cause__ (from 'raise X from Y') still works."""
        try:
            raise ValueError("original error")
        except ValueError as cause:
            with pytest.raises(exc_cls) as exc_info:
                raise exc_cls("wrapped", cause=cause)
            assert exc_info.value.__cause__ is cause


# ---------------------------------------------------------------------------
# __str__ representation
# ---------------------------------------------------------------------------


class TestStrRepresentation:
    """Verify __str__ includes message and optionally the cause."""

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_str_without_cause(self, exc_cls: type[MillforgeError]) -> None:
        msg = f"str test {exc_cls.__name__}"
        exc = exc_cls(msg)
        assert str(exc) == msg

    @pytest.mark.parametrize("exc_cls", CONCRETE_EXCEPTIONS)
    def test_str_with_cause(self, exc_cls: type[MillforgeError]) -> None:
        msg = f"str test {exc_cls.__name__}"
        cause = RuntimeError("inner")
        exc = exc_cls(msg, cause=cause)
        s = str(exc)
        assert msg in s
        assert "caused by:" in s
        assert "inner" in s


# ---------------------------------------------------------------------------
# Root exception MillforgeError
# ---------------------------------------------------------------------------


class TestMillforgeError:
    """MillforgeError itself is a plain Exception subclass."""

    def test_root_is_exception_subclass(self) -> None:
        assert issubclass(MillforgeError, Exception)

    def test_root_can_be_raised_directly(self) -> None:
        with pytest.raises(MillforgeError):
            raise MillforgeError("direct raise")

    def test_base_stores_message(self) -> None:
        exc = MillforgeError("base message")
        assert str(exc) == "base message"

    def test_base_accepts_cause(self) -> None:
        cause = TypeError("cause")
        exc = MillforgeError("base with cause", cause=cause)
        assert exc._cause is cause
        assert exc.__cause__ is cause
