# Vendored Forge Update Policy

The private Forge subset may only be updated as an isolated, reviewable
change. The update record must identify the old and new upstream repository,
commit, package version, license, import date, and reviewed local patch set.

Required update steps:

1. Review upstream `CHANGELOG.md`, architecture docs, and decision records for
   changed guarded-loop behavior, dependency boundaries, and license notices.
2. Diff every path listed in `PROVENANCE.json` against the new upstream
   snapshot before copying files.
3. Recheck exclusions for provider clients, proxy/server/CLI modules, eval
   assets, dashboards, hardware discovery, `httpx`, provider SDKs, and
   transport implementation code.
4. Copy the accepted subset from the fixed upstream snapshot, recompute
   source SHA-256 values before namespace edits, and update destination paths,
   edit categories, patch reasons, and license files together.
5. Reapply private namespace, subset, and behavior patches deliberately,
   documenting the edit category and rationale for each patch in the manifest.
6. Run upstream-derived provenance tests and Millforge adapter tests,
   including the package build and wheel-content checks.
7. Commit the copy, manifest, license, policy, and tests as one isolated
   provenance update.
