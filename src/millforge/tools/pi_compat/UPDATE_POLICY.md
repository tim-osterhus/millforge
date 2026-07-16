# Pi Compatibility Update Policy

This source-attributed behavioral port may be updated only as an isolated,
reviewable change against a new approved Pi snapshot.

1. Record the upstream package name, version, license, repository URL, and
   approved snapshot location. Do not invent a commit hash when the snapshot
   has no history.
2. Recompute SHA-256 values for every path in PROVENANCE.json before copying
   or translating behavior. Stop the update when a pinned source differs until
   the source version and manifest are explicitly reconciled.
3. Review Pi's tool sources, utility sources, and source-derived tests together.
   Preserve model-visible text unless a documented Spec 11 adaptation requires
   a divergence.
4. Reassess the complete adaptation list, including Python filesystem/process
   APIs, text-only image results, search implementation, mutation locking, and
   Windows shell behavior. Document any newly required divergence in the
   accepted specification, provenance, tests, and public documentation.
5. Copy the upstream MIT notice unmodified, update every path hash and
   classification, and keep the package-data rules in sync.
6. Run the source-derived focused tests, the full relevant Millforge tests,
   Ruff, MyPy, dependency checks, and package-content verification before
   accepting the update.

No Pi runtime, Node package, downloaded search binary, or unreviewed source
snapshot is an acceptable substitute for this procedure.

## Current Platform Validation

The POSIX shell path, timeout, cancellation, process-group cleanup, output
ordering, and persistence behavior are covered by the bundled Python 3.12
tests. The Windows `COMSPEC`/`cmd.exe` and `taskkill /F /T` implementation is
source-reviewed and has platform-gated contract tests, but native Windows
process-tree stress behavior was not locally verified for the 0.79.6 port.
Run those gated tests in a controlled Windows CI environment before claiming
native Windows process-tree parity.
