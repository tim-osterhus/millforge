# Default Runner Readiness Closure Report

Report prepared for `task-13-07-readiness-closure-evidence` on 2026-07-18.
This is public-source evidence for Arbiter review. Millrace runtime artifacts are not evidence for this report.

## Source Identity

- Audited baseline commit: `96d0e61514788d43635e390c39e14bb52a44387c`.
- Baseline subject: `Complete runner release readiness`.
- Current branch HEAD during the audit: the same baseline commit.
- Resulting closure commit: PENDING — the prerequisite implementation and this
  report are still working-tree changes. A commit hash and CI run for that exact
  hash do not yet exist and are not fabricated here.
- The source delta is measured directly with
  `git diff --stat 96d0e61514788d43635e390c39e14bb52a44387c` and the complete
  repository state is retained below. The report is eligible for source
  control through
  `git ls-files --error-unmatch tests/fixtures/default_runner_readiness_closure_report.md`.

## Public API And Compatibility Changes

- `HarnessExecutionRequest.stage` is provider-local and the base runner admits
  exactly `StageIdentity(plane="execution", node_id="millforge-base",
  stage_kind_id="millforge_base")` before model or tool work. Opaque caller
  `request_id` and `run_id` values are validated and echoed, not interpreted as
  workflow authority.
- The package root now exposes `create_millforge_base_live_runner()`,
  `MillforgeBaseLiveRunner`, `AsyncHttpTransport`, public OpenAI-compatible
  profile/secret/timeout contracts, and typed construction/close errors. The
  documented consumer path uses imports from `millforge` only.
- The package root now exposes `SelectedOutputRequirement`,
  `SelectedOutputAbsent`, `SelectedOutputPresent`, selected-output canonical
  JSON/digest helpers, and the six named public bounds recorded below.
- `MillforgeInvocationEvidence.schema_version` changes from `1.0` to `1.2` and
  now carries request/run correlations plus the optional selected-schema digest
  and required flag. No permissive pre-release shim accepts the earlier shape.
- The package version remains `0.1.0`; request/result models gain the deliberate
  invocation-local selected-output fields without a release tag or publication.
- Compatibility snapshots deliberately add selected request, result,
  absent/null, schema, and required/optional evidence digests. The baseline
  invocation evidence digests
  `e6f4b1a91b991ed28078fe753d1132ea5298ae6e191ee77f388d91e7a9a1490e`
  and `083f025a476a9386c5625de8f33727304d693c6a32b04c87088d1bede83d5914`
  become
  `3da389b8a9d6aa594c3b6f0ed744bf5024b05e990984c83111ead91fe640b7bb`
  and `4e84f18ae5dab47ae4b78b56cb77c1ad7d79cfab617b7c6cf695177b78bb4548`.

## Descriptor Identity

The installed static descriptor is intentionally unchanged before and after
the closure work:

- embedded `descriptor_sha256`:
  `ce4f77c4644ed22b01751abffe5960fe270ba5cbebe0d0c179c55454c347b530`
- canonical descriptor JSON fixture SHA-256:
  `135cc8e4308fcf6b1b1410af355ad961201267d0657b451a1392ac88d83e60b0`
- schema/runner/package versions: `1.0` / `1` / `0.1.0`
- tool catalog SHA-256:
  `5de78f0943c5ef169f971651fd3220308b2dee2fae9641919c262824cc92808a`
- legal provider results: `BLOCKED`, `COMPLETE`, and `REJECTED`
- supported descriptor platforms: `linux` and `darwin`

Selected-output authority remains request-local, so it changes invocation
evidence and terminal schemas without mutating this static identity.

## Selected Output Contract

The admitted closed JSON subset is `object`, `array`, `string`, `integer`,
finite `number`, `boolean`, and `null`. Supported keywords are limited by type:
objects use `properties`, `required`, and closed `additionalProperties`; arrays
use `items`, `minItems`, and `maxItems`; strings use `minLength` and
`maxLength`. Unsupported keywords, duplicate keys, non-string object keys,
non-finite numbers, invalid bounds, and unknown fields fail closed.

Public global bounds are exact:

| Bound | Value |
| --- | ---: |
| `MAX_SELECTED_OUTPUT_SCHEMA_BYTES` | 65,536 bytes |
| `MAX_SELECTED_OUTPUT_PAYLOAD_BYTES` | 1,048,576 bytes |
| `MAX_SELECTED_OUTPUT_NESTING_DEPTH` | 16 |
| `MAX_SELECTED_OUTPUT_OBJECT_PROPERTIES` | 64 |
| `MAX_SELECTED_OUTPUT_ARRAY_ITEMS` | 1,024 |
| `MAX_SELECTED_OUTPUT_STRING_LENGTH` | 65,536 characters |

Required and optional authority is explicit. Omission is represented by
`SelectedOutputAbsent` and remains distinct from
`SelectedOutputPresent(value=None)`. The terminal candidate is validated through
the existing bounded correction path and is returned separately from summary,
diagnostics, artifacts, stdout, logs, and workspace files.

## Lifecycle, Ownership, And Timeouts

- Construction is asynchronous only to support cleanup after partial local
  construction; it performs no provider or network probe.
- `async with` and explicit `aclose()` are supported; close is idempotent,
  factory-owned model/HTTP clients close once, and use after close raises
  `MillforgeBaseClosedError`.
- The caller retains ownership of the secret resolver, clock, cancellation
  resolver, artifact-writer factory, and any injected `AsyncHttpTransport`.
  The live runner does not close an injected transport.
- Connect, read, write, pool, and local-total timeout bounds must be positive
  and finite. The effective model-call timeout is the minimum of the admitted
  request deadline, resolved profile `timeout_seconds`, and explicit
  `local_total_seconds`; named HTTP phases narrow that result further and never
  widen caller authority.

## Local Verification Evidence

Host: Linux, CPython 3.13.13. These results apply to the working-tree closure
source described by `git status --short --branch`, not to a nonexistent result
commit.

| Command | Exact result |
| --- | --- |
| `uv sync --frozen --extra dev` | PASS — `Checked 25 packages in 122ms` |
| `uv run python -m pytest -q tests/test_python_compatibility.py tests/test_distribution_metadata.py` | PASS — `10 passed in 8.64s` |
| `uv run python -m pytest -m "not live_model_backend"` | PASS — `1892 passed, 2 skipped, 1 deselected, 5 warnings in 253.97s` |
| `uv run python -m compileall -q src` | PASS — exit 0, no output |
| `uv run mypy .` | PASS — `Success: no issues found in 143 source files` |
| `uv run ruff check .` | PASS — `All checks passed!` |
| `uv run ruff format --check .` | PASS — `169 files already formatted` |
| `uv build` | PASS — built `dist/millforge-0.1.0.tar.gz` and `dist/millforge-0.1.0-py3-none-any.whl` |
| `uv run python scripts/ci_package_smoke.py dist` | PASS — archive audit passed; wheel and sdist each completed two fake transport calls with zero network probe events and provider-local result `COMPLETE` |
| `git ls-files --error-unmatch tests/fixtures/default_runner_readiness_closure_report.md` | PASS — printed the exact report path and exited 0 |
| `git diff --check` | PASS — exit 0, no output |
| `git diff --stat 96d0e61514788d43635e390c39e14bb52a44387c` | PASS — exact output retained below |
| `git status --short --branch` | PASS — exact output retained below; source is intentionally not described as clean or committed |

The earlier pre-report pass completed with `1891 passed, 2 skipped, 1
deselected`; the retained final count includes the new source-controlled report
test.

## Hosted CI Evidence

The only authoritative hosted run currently attached to the audited repository
is baseline run
[`29614464121`](https://github.com/tim-osterhus/millforge/actions/runs/29614464121)
for commit `96d0e61514788d43635e390c39e14bb52a44387c`:

| Platform | Baseline result and exact link | Closure result |
| --- | --- | --- |
| Ubuntu Python 3.11 | [PASS](https://github.com/tim-osterhus/millforge/actions/runs/29614464121/job/87996393918) | PENDING result commit and CI |
| Ubuntu Python 3.12 | [PASS](https://github.com/tim-osterhus/millforge/actions/runs/29614464121/job/87996393912) | PENDING result commit and CI |
| Ubuntu Python 3.13 | [PASS](https://github.com/tim-osterhus/millforge/actions/runs/29614464121/job/87996393864) | PENDING result commit and CI |
| macOS Python 3.11 | [FAIL — old bounded compatibility gate](https://github.com/tim-osterhus/millforge/actions/runs/29614464121/job/87996393873) | PENDING result commit and full-suite CI |
| macOS Python 3.12 | [FAIL — old bounded compatibility gate](https://github.com/tim-osterhus/millforge/actions/runs/29614464121/job/87996393898) | PENDING result commit and full-suite CI |

The current `.github/workflows/ci.yml` runs
`uv run python -m pytest -m "not live_model_backend"` for all five jobs and
adds build/installed-package smoke on both macOS versions. Those current jobs
cannot have links or results until this source is committed and CI runs. The
baseline macOS failures are affirmative historical evidence, not evidence that
the uncommitted portability repairs passed on macOS.

## Package Inspection

`python -m zipfile -l dist/*.whl` and `tar -tzf dist/*.tar.gz` were inspected
after the successful build. Both archives retain the required Forge
`LICENSE`, `PROVENANCE.json`, and `UPDATE_POLICY.md`, and the Pi-compatible
`PI_LICENSE`, `PROVENANCE.json`, and `UPDATE_POLICY.md`.

The package smoke independently rejects archive entries containing Millrace
state, `ideas`, `ref-forge`, `reference`, local specs, tests, scripts, build
scratch, logs, credentials/secrets/tokens, caches, bytecode, or generated
runtime/development state. It also compares wheel/sdist metadata and proves
Apache-2.0, Python `>=3.11`, Linux/macOS classifiers, and no Millrace runtime
dependency.

Sanitized installed-artifact construction output was exactly:

```json
{"archive_audit":"passed","sdist":{"construction_surface":"millforge.create_millforge_base_live_runner","fake_transport_calls":2,"network_probe_events":0,"provider_local_result":"COMPLETE","requires_python":">=3.11","version":"0.1.0"},"wheel":{"construction_surface":"millforge.create_millforge_base_live_runner","fake_transport_calls":2,"network_probe_events":0,"provider_local_result":"COMPLETE","requires_python":">=3.11","version":"0.1.0"}}
```

The retained JSON contains no raw secret, checkout/temp path, transcript,
credential, or Millrace runtime state.

## Repository Status

Final `git diff --stat 96d0e61514788d43635e390c39e14bb52a44387c`:

```text
 .github/workflows/ci.yml                           |  18 -
 README.md                                          | 111 ++-
 scripts/ci_package_smoke.py                        | 194 +++-
 scripts/installed_package_smoke.py                 | 319 ++++---
 src/millforge/__init__.py                          |  74 ++
 src/millforge/_forge/adapter.py                    | 171 +++-
 src/millforge/_forge/core/inference.py             |   2 +-
 src/millforge/base/__init__.py                     |  44 +
 src/millforge/base/composition.py                  |  20 +-
 src/millforge/base/context.py                      |  20 +-
 src/millforge/base/identity.py                     |  52 +-
 src/millforge/base/runner.py                       | 316 ++++++-
 src/millforge/compiled_plan.py                     |  15 +-
 src/millforge/contracts.py                         | 727 ++++++++++++++-
 src/millforge/exceptions.py                        |   7 +
 src/millforge/model_backend.py                     | 195 +++-
 src/millforge/protocols.py                         |  18 +
 src/millforge/runtime.py                           |  53 ++
 .../default_runner_readiness_closure_report.md     | 286 ++++++
 tests/fixtures/python_compatibility/v1.json        |  17 +-
 tests/test_base_composition.py                     |  10 +-
 tests/test_base_context.py                         |  51 +-
 tests/test_base_identity.py                        | 162 +++-
 tests/test_base_public_api.py                      |  76 +-
 tests/test_base_runner.py                          | 983 ++++++++++++++++++++-
 tests/test_contracts.py                            | 366 +++++++-
 tests/test_distribution_metadata.py                | 109 +++
 tests/test_model_backend.py                        |  87 +-
 tests/test_pi_compat_operations.py                 |  13 +-
 tests/test_pi_compat_process.py                    |  22 +-
 tests/test_python_compatibility.py                 | 167 +++-
 31 files changed, 4410 insertions(+), 295 deletions(-)
```

Final `git status --short --branch`:

```text
## main...origin/main
 M .github/workflows/ci.yml
 M README.md
 M scripts/ci_package_smoke.py
 M scripts/installed_package_smoke.py
 M src/millforge/__init__.py
 M src/millforge/_forge/adapter.py
 M src/millforge/_forge/core/inference.py
 M src/millforge/base/__init__.py
 M src/millforge/base/composition.py
 M src/millforge/base/context.py
 M src/millforge/base/identity.py
 M src/millforge/base/runner.py
 M src/millforge/compiled_plan.py
 M src/millforge/contracts.py
 M src/millforge/exceptions.py
 M src/millforge/model_backend.py
 M src/millforge/protocols.py
 M src/millforge/runtime.py
 A tests/fixtures/default_runner_readiness_closure_report.md
 M tests/fixtures/python_compatibility/v1.json
 M tests/test_base_composition.py
 M tests/test_base_context.py
 M tests/test_base_identity.py
 M tests/test_base_public_api.py
 M tests/test_base_runner.py
 M tests/test_contracts.py
 M tests/test_distribution_metadata.py
 M tests/test_model_backend.py
 M tests/test_pi_compat_operations.py
 M tests/test_pi_compat_process.py
 M tests/test_python_compatibility.py
```

All divergence is source, test, CI, documentation, package-smoke, compatibility,
or closure-report work for this root spec. Ignored/generated Millrace state is
not used to establish readiness.

## Deferred Work

The closure deliberately does not implement or claim:

- external adapter implementation
- runner selection/defaulting
- caller dispatch echo
- workflow terminal mapping
- retries
- durable orchestration
- live paid evaluation
- native Windows
- release tagging
- GitHub release creation
- PyPI publication

It also introduces no Millrace dependency, workflow authority branch, provider
registry, plugin discovery, credential store, live-provider transcript, or
release action.

## Readiness Boundary

The local Linux source, compatibility, static, package, and isolated installed
artifact evidence is green. Default-runner release readiness is not yet a
supported final claim because the resulting closure commit and hosted Linux
3.11/3.12/3.13 plus macOS 3.11/3.12 CI results are pending. Arbiter should bind
any final readiness verdict to the eventual exact commit and its five hosted
job results, not to this dirty working tree or ignored runtime state.
