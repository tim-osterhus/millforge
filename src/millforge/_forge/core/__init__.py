"""Private Forge core guarded-loop subset."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from millforge._forge.core.runner import WorkflowRunner

__all__ = ["WorkflowRunner"]


def __getattr__(name: str) -> object:
    if name == "WorkflowRunner":
        from millforge._forge.core.runner import WorkflowRunner

        return WorkflowRunner
    raise AttributeError(name)
