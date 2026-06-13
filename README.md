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
subset import safety. Runtime adapter behavior also classifies exhausted
prerequisite correction budgets as `budget_exhausted` with diagnostic code
`prerequisite_budget_exhausted`.

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

### Opt-In Live Model Backend Smoke

Normal test runs are offline, deterministic, and do not require provider
credentials. The live OpenAI-compatible backend smoke is marked
`live_model_backend` and is skipped unless explicitly enabled:

```bash
MILLFORGE_LIVE_MODEL_BACKEND_SMOKE=1 \
MILLFORGE_LIVE_MODEL_PROFILE_ID=<profile-id> \
MILLFORGE_LIVE_MODEL_PROVIDER_ID=<provider-id> \
MILLFORGE_LIVE_MODEL_ID=<model-id> \
MILLFORGE_LIVE_MODEL_BASE_URL=<openai-compatible-base-url> \
MILLFORGE_LIVE_MODEL_SECRET_ID=<secret-ref-id> \
MILLFORGE_LIVE_MODEL_SECRET_ENV_VAR=<credential-env-var-name> \
<credential-env-var-name>=<credential> \
python -m pytest -m live_model_backend tests/test_model_backend.py
```

Optional variables: `MILLFORGE_LIVE_MODEL_AUTH_SCHEME` (`bearer` or `header`),
`MILLFORGE_LIVE_MODEL_AUTH_HEADER` for custom header authentication,
`MILLFORGE_LIVE_MODEL_TIMEOUT_SECONDS`, and
`MILLFORGE_LIVE_MODEL_MAX_OUTPUT_TOKENS`. The smoke records only sanitized
provider, model, latency, finish reason, and usage metadata.
