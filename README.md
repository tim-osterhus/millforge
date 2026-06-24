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
content inspection without live provider calls. The public offline Spec 07
preset registry and readiness report now live in `millforge.eval_presets`,
the public offline 08B eval-trial contract boundary, including
caller-selected append-only campaign-store APIs, now lives in
`millforge.eval_trials`, while live Spec 07 execution and admission remain
deferred to the owning workstream.

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

## Millforge 04A Tool Registry Core

04A adds the public `millforge.tools` registry core. It provides immutable
`ToolDescriptor`, `ToolTimeoutPolicy`, and `ToolOutputPolicy` contracts,
registry-computed deterministic descriptor hashes, explicit in-process
`ToolRegistry` registration, immutable exact-version
`FrozenToolRegistrySnapshot` lookup, and projection into the existing compiler
catalog and `ToolBindingRef` path.

The package exposes registry constants, descriptor hash records, typed registry
errors, and descriptor hash payload inspection helpers for deterministic
evidence. Descriptor and snapshot hashing remains generic and registry-owned;
04A does not ship production `builtin.*` descriptors, a default production
registry, production presets, tool execution or dispatch, connector admission,
live provider dependencies, or Millrace runner integration.

## Millforge 04B Built-In Tool Descriptor Data

04B adds the descriptor-only production `builtin.*` catalog data under
`src/millforge/tools/`. It now spans 26 version-1 built-in descriptors for
request inspection, workspace listing/reading/searching/writing/patching,
named test execution, static checks, artifact read/write, terminal
submit/reject/escalate actions, and the fixed artifact bridge readers and
split verdict writers, all using the accepted 04A registry contracts.

The built-in catalog stays import-safe and side-effect-free. It exposes the
immutable descriptor set, deterministic registry construction helper, and
frozen exact-version snapshot helper, but it still does not add tool execution,
dispatch maps, connector admission, custom tools, production presets, queue
policy, implementation registration objects, or Millrace runner integration.

### Spec 07 Capability Projection Boundary

Spec 07 compile cases validate concrete tool-level compiler/catalog grants,
including `request.read`, `artifact.read`, `artifact.write`, `workspace.read`,
`workspace.write`, `workspace.diff.read`, `process.test`,
`process.static_check`, and `terminal.intent`. These grants are the catalog
capability vocabulary used by built-in descriptors and semantic compilation.

That vocabulary remains separate from the broader 06B eval-stage capability
envelope concepts. 06B envelopes include `evidence.emit`, `runner.invoke`,
`shell.run`, and broad workspace, package, network, git, and runtime-control
envelopes. Spec 07 projects eval-stage needs into concrete compiler/catalog
tool grants; it does not rename or replace the 06B capability names.

The boundary is pinned by
`tests/test_eval_presets.py::test_checker_compile_case_needs_tool_level_grants_not_only_06b_eval_envelope`
and
`tests/test_builtin_tool_catalog.py::test_builtin_descriptors_feed_capability_validation`.

## Millforge 04C Tool Execution Boundary

04C adds the compiled-plan-scoped runtime execution boundary under
`src/millforge/tools/`. It exposes `create_builtin_tool_executor(...)` from
`millforge.tools` and admits calls only through exact `ToolBindingRef`
matches from the frozen compiled plan plus the accepted 04B built-in snapshot.

Runtime implementations stay explicit and source-owned. Accepted built-in
`implementation_id` values are registered to callables in source rather than
descriptor import strings, dotted paths, plugin loading, connector lookup, or
other dynamic dispatch.

Binding denial is fail-closed and typed, with deterministic `not_found`,
`conflict`, and `binding_mismatch` categories for uncompiled names, ambiguous
model-visible names, runtime-implementation gaps, and projection mismatches.

`DefaultHarnessRuntime` threads a runtime-owned `ToolExecutionContext` through
the guarded session and into the tool-executor bridge, so tool dispatch uses
trusted request, stage, run, workspace, artifact, capability, deadline, and
cancellation data supplied by the runtime rather than model-authored
substitutes.

### Execution Validation, Results, And Traces

Model tool calls are converted into closed `ValidatedToolCall` objects before
implementation entry, so extra fields are rejected by schema rather than
flowing into the runtime.

The runtime rechecks prerequisites, required capabilities, deadlines, and
cancellation before dispatch. Built-in calls also pass through an
executor-owned, non-effectful pre-entry policy gate before implementation
entry, covering workspace logical-path containment, artifact declarations and
availability, shell profile/selector/timeout admission, and terminal artifact
requirements. The runtime validates implementation output against descriptor
output schemas after completion and fails closed on invalid output before
model-visible return, then redacts and bounds accepted results before return.

Every attempted or denied call emits a `ToolTraceRecord`. Pre-entry denials
persist `side_effect_certainty=not_attempted`, and resolved, ambiguous, and
uncompiled binding states are preserved through `binding_resolution_status`.
Connector traces also carry structured approval, drift, request/response,
retry, and redacted-evidence fields so failures stay auditable without prose
summaries.

## Millforge 04D Tool Registry Closure Evidence

04D closes the accepted Spec 04 tool-registry packet with retained offline
evidence rather than new production authority. The closure surface is the
machine-checkable conformance matrix at
`tests/fixtures/spec04_conformance_matrix.json`, validated by
`tests/test_tool_registry_closure.py`, plus focused readiness tests for the
04A registry contracts, 04B built-in descriptor catalog, and 04C built-in
execution boundary.

The package and documentation audits keep the public claim narrow:
implemented Spec 04 behavior covers registry descriptors, built-in catalog
data, exact compiled-plan tool binding, built-in execution policy gates,
trace/result evidence. Offline 05A through 05D closure work adds connector
admission, runtime-boundary admission snapshots, compile-only custom-tool
descriptors, and mixed registry/catalog/compiler evidence through the generic
tool path. Deferred live connector transport, marketplace installation,
production stage presets, Millrace runner integration, eval-suite execution,
live connector execution, and live provider/model/tool execution remain
deferred.

Default Spec 04 closure verification is offline and deterministic. The retained
gate set includes the focused registry closure suite, the full pytest suite,
Ruff lint and format checks, MyPy, `pip check`, package build, wheel and sdist
archive listings, the accepted 04C baseline diff, source-control status, and
private-state checks for `millrace-agents/`, `ideas/`, and `ref-forge/`.

## Millforge 05A Connector Descriptor Admission

05A adds the public `millforge.connectors` package for deterministic offline
connector descriptor admission. It exposes frozen connector identity, discovery
snapshot, admission manifest, admission policy, admission result, admission
record, and diagnostic contracts, plus `admit_connector_tools(...)` for
lowering explicitly admitted discovered tools into existing immutable
`ToolDescriptor` objects.

Discovery snapshots are evidence only: they are not tool catalogs, do not
implement `ToolCatalogSnapshot`, and are rejected before semantic compilation
can resolve model-visible tools. Admitted connector descriptors continue through
the generic `ToolRegistry`, `FrozenToolRegistrySnapshot`, and compiler catalog
path rather than a connector-specific runtime catalog.

Connector input and output schemas are normalized through the accepted
compiler-owned JSON Schema subset. Unsupported bound keywords such as
`maxLength`, `maxItems`, `minimum`, and `maximum` are rejected at admission
with explicit schema-error diagnostics because runtime does not enforce those
bounds.

05A remains offline and descriptor-only. It does not implement real MCP stdio
or HTTP transport, connector process launching, sockets, live connector
discovery, live connector invocation, credential use, a runtime connector
broker, production connector presets, Millrace runner integration, or
Millrace approval token handling.

## Millforge 05B Connector Runtime Admission Snapshot

05B adds the runtime-owned `ConnectorAdmissionSnapshot` and
`ConnectorAdmissionBinding` contracts, plus the connector-scoped
`ConnectorBroker`, `ConnectorInvocationRequest`, and `ConnectorBrokerOutcome`
runtime boundary. The snapshot deep-freezes accepted 05A
`ConnectorAdmissionRecord` evidence into exact bindings keyed by `tool_id`,
`tool_version`, and `descriptor_sha256`, exposes a deterministic
`snapshot_sha256`, and preserves `connector_id`, `provider_tool_name`,
`connector_identity_sha256`, `discovery_snapshot_sha256`, `raw_tool_sha256`,
`input_schema_sha256`, `output_schema_sha256`, `provider_description_sha256`,
`required_capabilities`, `side_effect_class`, `idempotency`,
`timeout_policy`, `output_policy`, optional `idempotency_key_policy`,
`approval_policy`, and `admission_record_sha256`. `CompiledToolBindingExecutor`
and `create_tool_executor(...)` require that the snapshot and broker be
provided for compiled connector descriptors while built-in-only plans continue
to construct and execute unchanged.

Before broker entry, the executor revalidates broker-exposed provider evidence
against the admitted connector identity, discovery snapshot, raw tool, and
schema or description hashes when those fields are available. It also
compares preserved admission `required_capabilities` against the compiled
connector descriptor before broker entry; capability drift fails closed with a
`binding_mismatch` denial that stays distinct from ordinary runtime
capability-grant failures.

Broker requests are keyed by `connector_id` and `provider_tool_name`, carry
validated JSON object arguments plus `tool_id`, `tool_version`,
`descriptor_sha256`, and runtime provenance, and stay narrow enough for the
offline `DeterministicFakeConnectorBroker` to keep connector boundary tests
deterministic.

Snapshot construction fails closed for missing, duplicate, stale,
descriptor-inconsistent, or non-connector admission records, and later source
mutation cannot change the frozen runtime bindings.

## Millforge 05C Custom-Tool Mini-Compiler

05C adds the public `millforge.custom_tools` package for deterministic offline
custom-tool contracts and diagnostics. It exposes frozen source manifest,
declaration, compiler policy, compilation record, compilation result, and
diagnostic contracts, plus `compile_custom_tools(...)`, deterministic hash
helpers, and validation helpers for raw contract inputs.

Accepted `runtime_kind=contract_only` declarations lower into immutable hashed
`ToolDescriptor` data and one immutable `CustomToolCompilationRecord` per
descriptor while remaining compatible with the existing `ToolRegistry`,
`FrozenToolRegistrySnapshot`, and compiler catalog path. Accepted results
normalize descriptor and record ordering by package, tool, version,
model-facing identity, implementation, descriptor hash, and record hash, while
rejected diagnostics are sorted canonically with evidence-aware tie-breakers.
The contracts keep explicit UTC provenance, closed approval/runtime enums, and
redacted bounded diagnostics for malformed source.

05C stays compile-only. It does not register executable custom-tool runtime
implementations, expose a custom tool registry or catalog, launch tools, broker
connectors, run sandboxed code, integrate with a runner, or claim executable
runtime support.

## Millforge 05D Mixed Connector And Custom-Tool Closure

05D adds deterministic offline closure evidence that combines built-in,
admitted connector, and compiled contract-only custom-tool descriptors through
the existing `ToolRegistry`, `FrozenToolRegistrySnapshot`, compiler catalog,
semantic validation, lowering, and compiler service path. The machine-readable
conformance matrix at `tests/fixtures/spec05_conformance_matrix.json` and the
focused smoke in `tests/test_connector_custom_tool_closure.py` back the mixed
registry, mixed catalog, harness selection, and illegal tool-denial rows.

The closure fixtures cover connector discovery, admission manifest, admission
policy, custom-tool source manifest, expected hashes, and a mixed harness
fixture under `tests/fixtures/spec05_mixed_harness/`. 05D remains offline and
does not add live connector transport, marketplace installation, automatic
discovery or admission, custom runtime execution, runner integration, eval
workflows, or live model/backend validation.

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
