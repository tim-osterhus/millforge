"""Validated options for the unrestricted Millforge base preset."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, field_validator

__all__ = ["MillforgeBaseOptions"]

_MAX_PROMPT_INPUT_BYTES = 65_536


class MillforgeBaseOptions(BaseModel):
    """The complete base-specific option boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    config_id: Literal["millforge-base.v1"] = "millforge-base.v1"
    load_context_files: StrictBool = True
    system_prompt: StrictStr | None = None
    append_system_prompt: StrictStr | None = None

    @field_validator("system_prompt", "append_system_prompt")
    @classmethod
    def _prompt_is_nonblank_and_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("prompt values must be nonblank when set")
        if len(value.encode("utf-8")) > _MAX_PROMPT_INPUT_BYTES:
            raise ValueError("prompt values may contain at most 65536 UTF-8 bytes")
        return value
