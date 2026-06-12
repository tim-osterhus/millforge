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

Millforge is an independent project. Its public API is Millforge-owned, while
`src/millforge/_forge/` contains a private vendored subset of the reviewed
Forge v0.7.4 guarded-loop implementation. That subset is limited to
transport-free protocol helpers, workflow/message/step/inference/runner
modules, guardrails, prompt helpers, fixed context management, the private
Millforge plan-translation adapter layer, and private model/tool/terminal
bridge adapters, plus `ForgeGuardrailBackend` runtime integration.

The vendored subset intentionally excludes provider clients, proxy/server/CLI
modules, eval assets, dashboards, hardware discovery, `httpx`, provider SDKs,
and transport implementation code. Runtime package code does not import or
depend on `ref-forge/`.

Machine-readable provenance lives in
`src/millforge/_forge/PROVENANCE.json`. The upstream MIT license is retained in
`src/millforge/_forge/LICENSE`, and update rules are documented in
`src/millforge/_forge/UPDATE_POLICY.md`.

Private behavioral patches are recorded in the manifest's
`private_behavior_patches` section. They currently cover configurable guarded
loop violation budgets, non-retryable tool outcomes, strict supported-subset
JSON Schema conversion, mapped prerequisite argument enforcement, and private
subset import safety.

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
