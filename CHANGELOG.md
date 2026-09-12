# Changelog

## 0.1.1 - 2026-09-11

- Add the `millforge-base.cooperative-local-io.v1` native pause profile for
  Core integration, with actual invocation checkpoints, stock local I/O and
  loopback model-effect reservations, and durable invalidation before
  finalization writes.
- Add deterministic gate, path-scope, and live integration coverage for the
  native control profile.
- Preserve provider-supplied empty and whitespace-only reasoning continuations
  through response normalization and exact tool-call replay.
- Retain the vendored Forge guardrail subset at version 0.7.4.
