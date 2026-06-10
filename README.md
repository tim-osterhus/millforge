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
| Commit   | `ae7c6d9f1e5349cbc120fcad63440f7057d84482`                                  |

Millforge is an independent project. It does **not** vendor, copy, rename, or
import Forge source code. Forge's guardrails concepts informed Millforge's
architecture and design, but all implementation is original.

## Development

- Python 3.12+
- Install for development: `pip install -e ".[dev]"`
- Run tests: `pytest`
- Lint: `ruff check .`
- Format: `ruff format --check .`
