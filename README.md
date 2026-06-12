# Millforge

A reliability layer for self-hosted LLM tool-calling, inspired by the
principles of [Forge guardrails](https://github.com/antoinezambelli/forge).

## Forge Provenance

| Field    | Value                                                                       |
|----------|-----------------------------------------------------------------------------|
| Name     | forge-guardrails                                                            |
| Version  | 0.7.4                                                                       |
| License  | MIT                                                                         |
| Upstream | [https://github.com/antoinezambelli/forge](https://github.com/antoinezambelli/forge) |
| Commit   | `bd99f4df0a7aab2fd4db2e6dae7f810a32617d76`                                  |

Millforge is an independent project. It does **not** vendor, copy, rename, or
import Forge source code. Forge's guardrails concepts informed Millforge's
architecture and design, but all implementation is original.

## Snapshot Comparison

Millforge's reference snapshot in `ref-forge/` is based on upstream
[forge-guardrails](https://github.com/antoinezambelli/forge) tag
[`v0.7.4`](https://github.com/antoinezambelli/forge/releases/tag/v0.7.4).
The snapshot was taken at commit
`bd99f4df0a7aab2fd4db2e6dae7f810a32617d76`.

**Note:** The `ref-forge/` directory is a plain-file snapshot and does **not**
contain `.git` metadata. It is provided for reference and comparison purposes
only.

## Development

- Python 3.12+
- Install for development: `pip install -e ".[dev]"`
- Run tests: `pytest`
- Lint: `ruff check .`
- Format: `ruff format --check .`
