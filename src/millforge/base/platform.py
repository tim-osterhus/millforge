"""Supported-platform preflight for operational ``millforge-base`` APIs."""

from __future__ import annotations

import sys
from typing import Literal, TypeAlias

from millforge.exceptions import UnsupportedPlatformError

SupportedPlatform: TypeAlias = Literal["linux", "darwin"]
SUPPORTED_PLATFORMS: tuple[SupportedPlatform, ...] = ("linux", "darwin")


def _require_supported_platform() -> None:
    platform_id = sys.platform
    if platform_id not in SUPPORTED_PLATFORMS:
        raise UnsupportedPlatformError(platform_id)
