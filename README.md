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

## Millforge 03A Closure Evidence

Closure evidence for 03A was refreshed on `2026-06-14T09:11:34Z` for
`task-03a-r2-04-closure-evidence-and-gates`.

Contract coverage:

- Source contract field table: `HarnessSource`, `StageScopeSource`,
  `PromptSource`, `BudgetSource`, `ContextPolicySource`, `HarnessGraphSource`,
  `HarnessNodeSource`, `PrerequisiteSource`, `ArgumentMatchSource`,
  `ArtifactPolicySource`, and terminal artifact policies are implemented in
  `src/millforge/compiler/source.py` with strict Pydantic v2 models,
  `extra="forbid"`, frozen contracts, explicit defaults, identifier bounds,
  collection bounds, tuple-backed snapshots, and mapping-to-record conversion.
- Canonical YAML and equivalent JSON examples are covered by
  `tests/compiler/test_parsing.py`; both front ends validate into the same
  `HarnessSource` model.
- Parser threat and limit matrix coverage includes duplicate keys, decoded key
  equivalence, unsafe YAML aliases/anchors/merge keys/tags, non-string keys,
  multiple documents, non-finite numbers, controls, invalid UTF-8, source size,
  nesting depth, entry count, scalar size, numeric lexeme limits, JSON leading
  whitespace, trailing JSON content, and top-level object requirements.
- Diagnostic trigger evidence covers request, parse, schema, cross-field,
  source-secret, ordering, truncation, and redaction cases in
  `tests/compiler/test_diagnostics.py`, `tests/compiler/test_parsing.py`, and
  `tests/compiler/test_requests.py`, including public unsupported-format parser
  coverage for `MF-S005`, exact schema trigger coverage for `MF-S021`
  unknown fields, `MF-S022` identifiers, `MF-S023` unversioned tool
  references, `MF-S024` budgets, and `MF-S025` context policy values, plus
  request-admission precedence for `MF-S018`.
- Source-location examples cover parser and schema diagnostics with RFC 6901
  field paths and one-based line/column locations.
- Request and result serialized examples, result invariant matrix, raw request
  admission examples, path-containment evidence, output-directory evidence,
  source hash examples, replacement-race evidence, deep-snapshot proof, and
  no-I/O/deferred-boundary proof are covered by `tests/compiler/test_requests.py`,
  `tests/compiler/test_source.py`, and
  `tests/compiler/test_frontend_boundaries.py`.
- Schema-phase failure results preserve `source_document_sha256` and parsed
  `harness_id` after a successful source parse, while request-phase and
  parse-phase failures continue to omit parsed identity; `tests/compiler/test_requests.py`
  covers the expected-harness mismatch and adjacent cross-field failure
  boundaries.
- Secret-helper evidence is covered without recording suspected source scalar
  values; diagnostics and serialized outputs use redacted fields only.
- Dependency and deferred-boundary audit evidence confirms compiler modules do
  not import `_forge`, runtime execution, catalog resolution, HTTP/network,
  subprocess, or model/tool invocation boundaries.

Verification commands and results:

```text
python -m pytest
732 passed, 1 skipped in 8.89s

python -m ruff check .
All checks passed!

python -m ruff format --check .
58 files already formatted

python -m mypy .
Success: no issues found in 35 source files

python -m build
Successfully built millforge-0.1.0.tar.gz and millforge-0.1.0-py3-none-any.whl
```

Source-control evidence:

```text
git diff --stat acd4491b905e635d6d3f9e9878206042a74692eb
 README.md                                  |   90 ++
 src/millforge/compiler/__init__.py         |  103 ++
 src/millforge/compiler/diagnostics.py      |  400 ++++++++
 src/millforge/compiler/parsing.py          | 1424 ++++++++++++++++++++++++++++
 src/millforge/compiler/requests.py         | 1152 ++++++++++++++++++++++
 src/millforge/compiler/source.py           |  375 ++++++++
 src/millforge/compiler/validators.py       |  171 ++++
 tests/compiler/test_diagnostics.py         |  159 ++++
 tests/compiler/test_frontend_boundaries.py |   71 ++
 tests/compiler/test_parsing.py             |  579 +++++++++++
 tests/compiler/test_requests.py            |  955 +++++++++++++++++++
 tests/compiler/test_source.py              |  203 ++++
 12 files changed, 5682 insertions(+)

git status --short --branch
## main...origin/main [ahead 1]
```

## Millforge 03B Semantic Compiler Boundary

03B adds a private semantic compiler layer under `src/millforge/compiler/`.
It consumes an accepted 03A `CompileInvocation` and `HarnessSource` plus one
tool catalog snapshot and one model-profile snapshot. It resolves exact tool
bindings and the compiled model profile, validates graph legality, top-level
argument matches, required capability grants, and terminal-required artifacts,
then produces a private immutable `ResolvedHarness` for later lowering.

The resolved IR is not a public API and is not a compiled plan. 03B performs no
source or output path I/O, source-file rereads, catalog refresh, plugin
discovery, network calls, subprocess calls, runtime execution, output writes,
compiled-plan hashing, or lowering. Those responsibilities are orchestrated by
the 03C default compiler service in `src/millforge/compiler/service.py`, which
delegates lowering to `src/millforge/compiler/lowering.py` and compiler output
publication in `src/millforge/compiler/output.py`; runtime execution and
registry/runtime work remain deferred.

Remediated 03B closure evidence was refreshed on `2026-06-14T18:35:03Z` for
`task-03b-r1-05-exact-closure-evidence-and-offline-gates`. The closure target is
exact semantic parity with the canonical 03B root-source contract, not a new
public compiled-plan or runtime result family. The evidence expects exact
code-to-trigger coverage for catalog resolution, schema normalization, graph and
argument validation, capability aggregation, artifact satisfiability, deferred
scope, source-control state, and full offline gates.

Representative 03B fixtures live under `tests/compiler/`:

- `test_catalogs.py` covers catalog metadata capture, closed lookup outcomes,
  `resolve_exact` protocol shape, immutable descriptor admission, model-profile
  admission, lookup exception redaction, and reusable static snapshot fixtures.
- `test_schema_validation.py` covers the compiler-owned JSON Schema subset,
  scalar `const` replacement for `type`, `null` const values, declared enum
  order, numeric scalar duplicate identity, deterministic normalization,
  compatibility bytes, and checked-in golden parity vectors without importing
  private Forge modules.
- `test_graph.py`, `test_capabilities.py`, and
  `test_artifact_validation.py` cover graph reachability and legality,
  terminal-prerequisite separation from argument-match failures, exact
  capability aggregation, artifact satisfiability, duplicate artifact IDs,
  terminal-gated producer evidence, determinism, and no-cascade behavior.
- `test_semantic.py` covers successful semantic compilation into the private
  immutable IR, 03A failure pass-through without catalog access, catalog
  resolution failures including `MF-R009` internal failures, duplicate bindings,
  duplicate model tool names, capability and artifact failures, and
  unresolved-node suppression.
- `test_frontend_boundaries.py` audits compiler modules for deferred imports
  and forbidden I/O/runtime invocation calls.

## Millforge 03C Compiler Service, Lowering, and Output Boundary

03C's default validated compiler service in `src/millforge/compiler/service.py`
orchestrates admitted compile requests through semantic validation, lowering,
compiled-hash verification, and atomic output publication. The service exposes
the public typed `HarnessCompiler` boundary, reuses the parsed source carried
by request admission, preserves front-end-before-catalog ordering, and returns
immutable compile results with deterministic diagnostics.

`tests/compiler/test_service.py` covers successful commits,
front-end-before-catalog precedence, semantic failure normalization, and
output-directory admission failure handling. `tests/compiler/test_lowering.py`
covers accepted field shape, nested required fields, deterministic ordering,
compiled-hash verification, and forbidden-data exclusion.
`tests/compiler/test_output.py` covers request identity hashing, path
confinement, admitted-directory revalidation, no-clobber reuse/conflict,
durability failure handling, and diagnostics persistence.
The compiler test area also includes checked-in YAML/JSON golden harness
fixtures under `tests/compiler/fixtures/`, exact semantic and compiled-plan
byte/hash assertions, output failure injection, and same-destination
concurrent publication coverage. The representative golden fixtures cover
three fixture pairs, including a rich case with two legal terminal results
(`BLOCKED` and `BUILDER_COMPLETE`), a terminal-required artifact, all accepted
budget fields, context phase thresholds, and multi-capability aggregation.
Across all three cases, the representative fixture set pins exact diagnostics
report shape and semantic-change hash movement without importing production
Spec 07 preset ownership.
Output diagnostics use the 03C root-source meanings: `MF-O001` for invalid
output paths, `MF-O002` for diagnostics write failures, `MF-O003` for plan
write failures, `MF-O004` for existing content-addressed output integrity
failures, and `MF-O005` for temporary output cleanup failures.
Lowering and internal diagnostics use the R2 root-source meanings:
`MF-L001` for lowering invariant failures, `MF-L002` for accepted compiled-plan
validation failures, `MF-L003` for source semantic hash failures, `MF-L004` for
compiled hash verification failures, and `MF-I001` for bounded compiler
internal errors.

03C compiler output is the accepted `CompiledHarnessPlan` contract, not a
compiler-specific plan shape. `source_document_sha256` remains compile-result
evidence over the admitted normalized source document bytes, while
`source_sha256` is stored in the emitted plan as the SHA-256 of the canonical
validated semantic payload. The compiled hash is calculated by removing only
the top-level `compiled_sha256` field from a complete JSON-mode plan payload,
serializing with the shared canonical JSON encoder, hashing the UTF-8 bytes,
reconstructing the final plan, and passing the shared
`verify_compiled_plan_sha256()` verifier before output commit.
The compiler service uses the shared
`src/millforge/compiled_plan.py::calculate_compiled_plan_sha256()` helper for
that pre-verifier check, preserving Arbiter criterion 7's single owned
compiled-hash algorithm boundary.

Emitted plan bytes are canonical compact UTF-8 JSON with one trailing newline
and are published under the admitted output directory as relative
`<url-escaped-harness-id>@<harness-version>.<compiled-sha256>.compiled.json`
paths, for example
`compiled/millforge.test.golden.compiler.v1@1.1d65583fe8bd8379d95f889fe0e889d9ee28ada85d912db9188191eb73bddc52.compiled.json`.
Diagnostics remain request-addressed and use request-only,
source-document-hash plus request-hash, or compiled-digest plus request-hash
path forms such as
`compiled/<harness-id>@<harness-version>.<compiled-sha256>.request-<request-identity-sha256>.diagnostics.json`.
Diagnostics reports move from prepared to committed evidence only after plan
publication is confirmed, and serialized diagnostics redact secret-looking
messages and scalar fields before persistence. The
`FileCompiledHarnessLoader` loads those emitted bytes through the same
parse/hash/identity verifier used by runtime preflight. The focused
compatibility test in `tests/compiler/test_service.py` removes the original
harness source tree, loads only the emitted compiled bytes, and proves
`DefaultHarnessRuntime` reaches the deterministic fake backend after preflight.

Representative 03C coverage remains offline and deterministic. It covers the
compiler-output boundary, hash stability, output-state guarantees, runtime
loader/preflight compatibility, Forge adapter field compatibility, and package
content inspection without live provider calls or production Spec 07 preset
ownership. Production Spec 07 preset curation and registry ownership remain
deferred to their owning workstream.

03C R2 closure evidence maps the latest Arbiter gaps to completed work:
canonical lowering/internal diagnostic meanings, three representative YAML/JSON
fixture pairs with exact semantic and compiled hashes plus diagnostics report
shape assertions, and a complete deterministic failure-injection matrix across
source semantic hashing, accepted-plan validation, compiled-hash verification,
diagnostics persistence, plan output, publication, directory fsync, and
temporary cleanup boundaries. The evidence continues to preserve the typed
`HarnessCompiler` protocol, accepted `CompiledHarnessPlan` lowering, output
addressing, same-plan concurrency, different-request diagnostics non-collision,
and runtime compatibility through emitted compiled bytes loaded without the
original source tree.

## Millforge 03D Compiler Hardening and Spec 03 Closure

03D closes the implemented Spec 03 compiler packet while preserving the
accepted 03C baseline at commit
`ebfa3ed205758780fef431674cf525e50f1559a5`. The final closure evidence is
retained in the run-local conformance matrix and refreshed closure report for
`task-03d-r4-03-closure-evidence-gate-refresh`, which preserved the exact
offline gate set after the matrix parity pass and redacted the broad
secret-pattern scan outputs.

03D hardening adds or retains exact evidence for bounded parser adversarial
coverage, source-contract property checks, graph oracle tests, catalog drift,
descriptor-change hash behavior, capability/artifact validation, service
failure precedence, deterministic compile variants, diagnostics sentinel
redaction, filesystem/output failure injection, runtime loading from emitted
compiled bytes, package/dependency boundaries, archive contents, Forge
provenance, and unchanged `ref-forge/` state.

Default verification remains offline and deterministic: the compiler suite,
full pytest suite, Ruff lint, Ruff format check, MyPy, package build, wheel
listing (`python -m zipfile -l dist/*.whl`), sdist listing
(`tar -tzf dist/*.tar.gz`), baseline diff stat (`git diff --stat
ebfa3ed205758780fef431674cf525e50f1559a5`), and source-control status
(`git status --short --branch --untracked-files=all`) are the closure gates.
The live OpenAI-compatible backend smoke remains opt-in and is not part of
default closure.

03D does not implement or claim Millrace runner binding, production built-in
tool registry behavior, admitted connector compilation, small-model Millrace
workflow behavior, production Spec 07 preset compilation, comparative
evaluation workflow behavior, or live provider/model/tool execution. Those
remain deferred to Spec 01, Spec 04, Spec 05, Spec 06, Spec 07, and Spec 08
respectively.

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
