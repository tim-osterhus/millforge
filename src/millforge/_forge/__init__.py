"""Private vendored Forge guarded-loop subset.

This package is not part of Millforge's public API. It contains the reviewed
transport-free Forge v0.7.4 subset used by later private adapter stages.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("forge-guardrails")
except PackageNotFoundError:
    __version__ = "0.7.4+vendored"

__all__ = ["__version__"]
