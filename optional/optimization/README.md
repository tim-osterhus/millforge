# Millforge optimization 0.1.1

Optional finite profile optimization for Millforge 0.2.1 and Pi 1.4.0. Base
commands, builds and retained profiles work without this package.

The fixed method runs baseline, general proposal, domain proposal, general probe,
domain probe, composition proposal and composed probe, sequentially. Assessment
is deterministic. Adoption requires separate exact owner approval. There is no
resume, automatic adoption, hidden retry, provisioning or broker server.

```text
millforge-optimize protocol
millforge optimize prepare --campaign campaign.json --output NEWDIR --optimizer-bin ABS
millforge optimize run --campaign NEWDIR --approval approval.json --optimizer-bin ABS
millforge optimize candidate --campaign NEWDIR --proposal proposal.json --output CANDIDATE
millforge optimize assess --campaign NEWDIR --evidence evidence.json --output ASSESSMENT
millforge optimize adopt --campaign NEWDIR --report report.json --approval adoption.json --destination DEST
millforge optimize inspect --campaign NEWDIR
```

Direct add-on commands require `--base-bin ABS` except protocol. The base wrapper
supplies its own exact CLI path. Neither executable installs the other package.
All destinations are new owned directories. Existing starts/claims are never
reset; a new attempt needs a new campaign ID and explicit approval.

Only instructions, approved plain context/skills and an order-preserving subset
of baseline tools may change. Model, route, reasoning/cache, sampling, auth,
limits, runtime, permissions and evaluator authority remain protected. Omitted
reasoning stays omitted. SDK usage preserves nulls and reported provenance;
OAuth billing remains unknown. Search expense and ordinary execution are
separate. Library promotion requires fresh paired validation and scoped evidence.

New `prepare` and `run` require campaign v2 and base 0.2.1. Historical
campaigns retain their original base and optimizer provenance. For readback of a
0.2.0 campaign, invoke `millforge-optimize` directly with `--base-bin` set to the
original captured base CLI. The CLI and installation tree must match the
campaign digests. Historical v1 inspect, candidate, assess and adopt keep their
original meaning for the native target. Campaign v2 selects a complete `target`
tuple for the baseline and probes. Both tuples use `millforge-request/v1`,
binding version 2 and Pi 1.4.0.

| Target | Profile | Capture | Trace | Result |
| --- | --- | --- | --- | --- |
| Native | `millforge-profile/v2` | `millforge-capture/v3` | `millforge-trace/v3` | `millforge-result/v3` |
| Codemode | `millforge-profile/v3` | `millforge-capture/v4` | `millforge-trace/v4` | `millforge-result/v4` |

Proposals always use the native tuple and direct `generate` mode. A v2 proposer
runs either brokered in Podman or as a captured `host_generate` public base
`run` with one request and no tools. Host slots use `auth:none` or the official
OpenAI OAuth route with a captured auth home. Host slots account only for
base-observed client HTTP attempts and receive no broker claim or receipt.
Target OAuth still refuses before claims.

Codemode candidates retain the fixed `pi-codemode/v1` capability and at least
one approved native tool. The base inspector validates each run's active and
callable tool sets. Approved subsets can change the native tool declarations.
Assessment compares the entire captured capability, including the wrapper
description and constrained-sampling digests. The reported tool total includes
wrapper and nested starts once each.

O3-C2 has bounded offline Linux acceptance with a loopback fake model and
injected protection peers. Codemode dispatch requires test-scope dependencies
and a test-scope qualification receipt. The production CLI refuses Codemode
`run` before worker, broker or claim effects. Protected deployment remains
unqualified. Public `prepare`, `candidate`, `inspect`, `assess` and owner-approved
task-specific `adopt` support offline artifacts. Assessment and adoption require
test-scope evidence. Set the campaign's `library` to `null` and leave reuse
evidence empty.
The ordinary base CLI can validate and run the adopted profile directly.

If execution stopped after writing the start record and before retaining
qualification, `inspect` verifies the start and work-map identities and reports
`uncertain`. A prepared campaign reports `prepared`. Assessment requires
retained qualification and refuses an interrupted start with missing
qualification. These reads do not resume the campaign.

For v2, the original public `run` stdout Result is retained outside the worker's
writable mounts as `receipts/<work>-base-result.json`. Run, assessment and audit
reads require exact equality between that Result and the inspected saved chain.
This detects saved-chain replacement; it does not sign records or attest the
producer.

Ordinary verification selects exactly one retained profile once: the adopted
recommended candidate, or the baseline when a determinate search keeps it or the
owner declines adoption. It is one target-only ordinary reservation and run, not
a second baseline beside an adopted candidate. A failed or inconclusive search
cannot pass merely because the baseline runs, and the ordinary record populates
no matched `ordinary_execution` or amortization claim.

Offline tests inject process/broker peers internally. No production flag or env
bypasses containment. Production run requires deployed qualification of exact
Podman/images/network/broker/base identities. The fake method does not qualify
native usefulness, deployed containment, provider behavior, billing or release
readiness.

The November 15 closeout supports native operation, containment and accounting for one exact Windows Bonsai route with a Linux host. The original full run failed ordinary grading. Its unchanged report passed four checks under the corrected offline fallback evaluator, with eight regressions passing. The November 15 packet did not rerun the full Bonsai journey with the corrected grader.

The November 18 O2 receipt accepts one full native journey for the frozen Windows
LFM3-1.7B Q4_K_M route, Linux workload and official GPT-6.1 Terra Low OAuth
teacher. All 16 protection actions completed. The seven-slot search found no
observed improvement and retained the baseline. The retained profile passed all
four ordinary checks using the base CLI. Reconciled counts are 21 target requests
and three teacher requests. The receipt covers the exact native target, route,
workload and artifacts. Codemode retains its separate offline acceptance.

The canonical specification and plan are in the Millrace workspace under
`lab/specs/review/2026-10-24-millforge-harness2-optimization-*.md`. Their current
O1 supplement and focused test plan live under
`lab/tasks/millforge-harness2-o1/2026-11-08/`.
