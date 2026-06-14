# Millforge Roadmap

Millforge is a component harness for Millrace. Its job is to make one
model-driven Millrace stage more reliable, constrained, observable, and
auditable. Millrace remains the workflow authority: it owns stage routing,
queues, approvals, capability grants, recovery, and final closure. Millforge
operates inside that boundary by compiling and executing a stage-local harness
that exposes only the tools, evidence paths, and terminal results approved for
that stage.

The long-term vision is simple:

> Millrace compiles governed workflows. Millforge compiles the behavior inside
> one model-backed stage.

This separation lets Millrace keep deterministic control over work while
Millforge specializes small or self-hosted models for focused tasks such as
planning, building, checking, and closure review.

## Why Millforge Exists

General-purpose agent prompts are too soft for governed workflow execution.
They can encourage useful behavior, but they do not reliably prevent premature
success claims, missing evidence, skipped prerequisites, unauthorized actions,
or malformed tool calls. Millrace needs stage workers that can operate under
explicit authority and produce structured evidence, especially when the model is
small, inexpensive, local, or otherwise less forgiving than a frontier model.

Millforge is designed to provide that stage-local discipline:

- declare which tools a stage may use
- require evidence-producing steps before terminal results
- enforce prerequisite relationships such as read-before-write
- validate model tool calls before execution
- route malformed or premature calls into bounded correction loops
- keep tool output and file contents contained as untrusted data
- write deterministic traces, metrics, artifacts, and terminal results
- fail closed when compiled hashes, capabilities, or bindings do not match

The goal is not to make the model omniscient. The goal is to make bounded,
governed model work dependable enough to become a reusable Millrace component.

## Architectural Boundary

Millforge is intentionally not a second workflow engine.

Millrace owns:

- graph and stage routing
- work-item state and durable queues
- capability grants and approval requirements
- stage retry policy, cancellation, timeout, and recovery
- legal terminal outcomes
- workflow closure and audit authority

Millforge owns:

- compilation of one selected stage harness
- model-facing tool exposure for that stage
- stage-local prerequisites and terminal gates
- guarded model invocation
- tool-call validation and correction nudges
- stage-local context management
- evidence, trace, metrics, and structured terminal output

Millforge does not decide the next Millrace stage, mutate Millrace queues,
grant itself new capabilities, approve its own use, or convert infrastructure
failure into domain success. A zero exit code or successful model call is never
enough by itself. Millrace accepts or rejects Millforge's structured result.

## Current Foundation

The first Millforge foundation is a Python package with Millforge-owned public
contracts and a private, provenance-tracked subset of Forge-inspired guardrail
runtime code. The public API is intentionally Millforge-owned rather than a
direct exposure of Forge types. That keeps the integration stable even if the
internal guardrail backend changes later.

The implemented runtime direction already includes:

- immutable compiled harness plan contracts
- capability envelopes and model profile references
- typed model request and response contracts
- provider-neutral model backend support
- an OpenAI-compatible Chat Completions transport subset
- compiled-plan hash verification before model or tool work
- guarded runtime execution through a private backend adapter
- terminal intent validation and structured terminal result artifacts
- runtime events, tool traces, metrics, manifests, and diagnostics
- cancellation, timeout, side-effect certainty, and redaction hardening
- offline deterministic tests for the default verification path

This foundation is deliberately smaller than the full roadmap. It proves the
runtime contract before expanding into a general harness compiler, production
tool registry, Millrace runner plugin, and evaluation suite.

## Roadmap

### 1. Runtime Contract And Guardrail Backend

The first milestone is the stage runtime: take a compiled harness plan plus a
Millrace-shaped execution request, run one guarded model/tool session, and
produce one structured terminal result with artifacts.

This layer must remain deterministic at its boundaries. It verifies compiled
plan identity and hash before invoking a model, checks that the requested stage
and model profile match the plan, intersects required capabilities with the
Millrace-supplied envelope, and writes bounded evidence for every outcome.

Key outcomes:

- one compiled Builder-style fixture can execute end to end
- premature terminal attempts are rejected and corrected
- missing required evidence cannot produce success
- invalid tool arguments do not mutate workspace state
- model, tool, backend, timeout, cancellation, and artifact failures stay
  distinguishable
- terminal results are written only at an explicit commit point

### 2. Harness Source Language And Compiler

Millforge harnesses should not run directly from raw YAML or JSON. Source files
are authoring inputs. The runtime consumes immutable compiled plans.

The compiler will accept a restricted, declarative source language for one
stage-local harness. Version 1 is intentionally closed and narrow. It supports
stage scope, one logical model profile, one prompt policy, bounded correction
budgets, a tiered context policy, exact tool references, required nodes,
conjunctive prerequisites, top-level argument equality, terminal mappings, and
artifact requirements.

It does not support arbitrary code, shell snippets, dynamic imports,
environment interpolation, latest-version tool references, disjunctive
branches, nested harnesses, parallel subgraphs, workflow routing, approvals, or
runtime mutation.

Key outcomes:

- YAML and JSON become syntax front ends for one source contract
- unsafe parser features are rejected before semantic compilation
- diagnostics are deterministic, bounded, location-aware, and redacted
- source and output paths are admitted through strict containment rules
- semantic-equivalent sources compile to byte-identical plans
- compiled output is content addressed and hash verified
- runtime can load emitted plans without source access

### 3. Trusted Built-In Tool Registry

Millforge needs a trusted tool registry between compiler-visible tool metadata
and executable runtime behavior. Harness source may select registered tools and
declare constraints around them, but it may not define executable code.

Initial built-in tool families should cover:

- request inspection
- workspace listing, reading, searching, and constrained patching
- named tests and static checks
- artifact reading and writing
- terminal submit, reject, and escalate actions

Every tool descriptor declares exact identity, version, schema, capabilities,
side-effect class, idempotency, timeout policy, output policy, implementation
identity, and descriptor hash. Runtime binds exact descriptors, rechecks
capabilities, validates inputs and outputs, and records side-effect certainty.

Key outcomes:

- a model cannot call an uncompiled tool
- schema-valid but unauthorized calls are denied
- workspace paths cannot escape their approved root
- general shell access is avoided in early milestones
- non-idempotent ambiguous failures are not retried automatically
- tool traces record capability and prerequisite decisions at decision time

### 4. Stage Harness Presets

Millforge should ship useful preset harnesses, but those presets are examples
of the harness compiler rather than hardcoded special cases.

The first presets target a compact Millrace evaluation workflow:

- Planner: produce one bounded implementation plan
- Builder: implement the plan and produce diff and test evidence
- Checker: independently review evidence without workspace write authority
- Arbiter: decide whether closure is sufficiently supported

Each preset has a distinct tool graph, evidence contract, prerequisite policy,
terminal mapping, and budget. This is the point of Millforge: a Builder,
Checker, Planner, and Arbiter should not share one generic harness. They may
share a model profile, but their behavioral contracts should differ.

Key outcomes:

- Planner cannot decompose work into new queued tasks
- Builder cannot submit before diff, tests, and summary artifacts exist
- Checker cannot approve before reading required evidence and rerunning checks
- Arbiter cannot close without an approved checker verdict and valid artifacts
- no preset contains provider credentials or unrestricted tool authority

### 5. Millrace Runner Integration

Millforge should be installable independently from Millrace and activated only
through explicit Millrace configuration. Installation alone must not make it a
runner.

The intended integration is an external Millrace stage runner named
`millforge`. During Millrace compilation, a graph node may select a Millforge
harness by identity and version. Millrace asks the runner plugin to compile or
resolve that harness, validates the returned binding, freezes the compiled
harness path and hash into the run plan, and supplies that frozen binding at
runtime.

Millrace validates:

- runner identity and package version
- compiled harness path containment
- compiled harness hash
- required capabilities against the stage envelope
- terminal results against the legal stage outcomes
- artifact declarations against stage contracts
- absence of secret-bearing configuration in the frozen plan

Key outcomes:

- built-in Millrace runners continue to work unchanged
- Millforge activation is explicit
- runtime never re-resolves mutable harness source for an active plan
- capability mismatch fails before model invocation
- harness hash mismatch fails before model, tool, or HTTP work

### 6. Controlled Evaluation

Millforge should earn reliability claims through Millrace-native evaluation,
not through anecdote or generic benchmark scores.

The first evaluation compares the same small or low-cost model running the same
Millrace workflow through two arms: a baseline runner and the Millforge runner.
Both arms use the same graph, task inputs, capability envelopes, legal terminal
results, retry limits, fixtures, deterministic validators, model identity, and
budget ceilings. Only runner-internal harness behavior differs.

Primary metrics should emphasize safety and governed-work reliability:

- end-to-end valid completion
- false closure
- false success
- required artifact completeness
- capability violations
- invalid or premature terminal actions

Secondary metrics include invalid tool-call rate, malformed arguments,
prerequisite violations, recovery after tool errors, model turns, token usage,
cost, latency, and runner overhead.

Key outcomes:

- false closure is reported separately from generic failure
- deterministic scoring decides primary success
- hidden checks remain hidden from runners
- trial records are append-only and reproducible
- invalid trials remain visible rather than silently excluded
- public claims stay bounded to the evaluated workflow and model

### 7. Connector Admission

External connectors can expand Millforge beyond built-in tools, but discovery
is not trust. A connector tool should become model-visible only after explicit
operator admission and compilation.

The first standard connector path should be MCP. Millforge should capture a
discovery snapshot, compare it with an operator-written admission manifest,
normalize schemas, map explicit capabilities, classify side effects, assign
approval policy, sanitize model-facing descriptions, and compile immutable
connector tool descriptors.

At runtime, the model never talks directly to a connector. Calls flow through
Millforge validation, prerequisite checks, Millrace capability checks, approval
checks, connector identity revalidation, broker invocation, output validation,
redaction, and trace recording.

Key outcomes:

- discovery alone exposes no tool
- every admitted connector tool has explicit capabilities and side effects
- connector identity or schema drift fails closed
- destructive tools require explicit approval or remain forbidden
- connector output is treated as untrusted data
- credentials never enter discovery snapshots or compiled descriptors

### 8. Local And On-Prem Model Deployment

Millforge's architecture is meant to support both cloud and local models. The
initial runtime uses provider-neutral model contracts and an OpenAI-compatible
transport subset because that is the fastest path to controlled evaluation.
Local backends can be added later behind the same model boundary.

The strategic target is not "any local model can do anything." It is narrower
and more useful: a pinned model, a strict harness, a trusted tool catalog, and
a governed Millrace workflow can make focused work reliable enough for private
or on-prem deployments.

Key outcomes:

- one logical model profile can be reused across multiple harnesses
- local backend support does not change harness compiler contracts
- model/provider credentials stay runtime-only
- context budgets and sampling policy are explicit
- evaluation results guide which models and backends are worth supporting

### 9. Vertical Harness Packs

Once the compiler, registry, runner integration, and evaluation story are
stable, Millforge can support vertical harness packs. A pack might include
stage harnesses, admitted connector descriptors, model profile requirements,
fixture tests, and scoring rules for a specific operational domain.

This is where Millforge can become product infrastructure: company-specific
workflows can be represented as governed Millrace graphs plus Millforge
harnesses that safely expose only the actions needed inside each stage.

Examples of future vertical packs may include:

- repository maintenance and code review
- evidence-backed document workflows
- internal support triage
- operational runbook execution
- compliance artifact preparation
- local knowledge-base maintenance

These packs should remain subordinate to Millrace authority. They customize
stage behavior; they do not define ungoverned workflow control.

## Non-Goals

Millforge is not trying to become:

- a general agent orchestrator
- a replacement for Millrace's graph compiler
- a queue manager or durable workflow store
- a plugin marketplace
- a dynamic code loader for model-authored tools
- a universal transaction or rollback system
- a promise that small models match frontier models on open-ended tasks

The project is strongest when it stays disciplined: compile restricted
stage-local behavior, execute it with strong guardrails, and hand structured
results back to Millrace.

## Public Compatibility Principles

As Millforge evolves, public surfaces should follow these principles:

- expose Millforge-owned contracts, not private backend types
- keep compiled plans immutable and hash-verifiable
- prefer exact versions and descriptor hashes over mutable "latest" references
- make credentials runtime-only and secret-reference based
- keep default tests offline and deterministic
- record provenance for vendored or derived guardrail code
- add abstractions only where they protect the Millrace/Millforge boundary
- fail closed on identity, capability, artifact, schema, or hash mismatch

## Maturity Expectations

Early Millforge releases should be treated as pre-alpha infrastructure. The
runtime and compiler contracts are intentionally strict because reliability is
the product. A feature is not ready merely because a model can complete a happy
path. It is ready when malformed inputs, missing evidence, unauthorized tool
use, provider failure, cancellation, timeout, output corruption, and secret
redaction all have deterministic behavior.

The roadmap favors evidence over breadth:

1. prove one compiled stage runtime
2. prove the compiler can produce immutable plans
3. prove trusted built-in tools are safe and observable
4. prove Planner, Builder, Checker, and Arbiter presets
5. prove Millrace runner integration without weakening governance
6. prove reliability improvements in controlled evaluation
7. expand into connectors, local backends, and vertical packs

That sequence keeps Millforge useful without letting it blur into a second
workflow engine.
