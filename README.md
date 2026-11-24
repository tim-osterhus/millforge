# Millforge

Millforge is a Node.js host for portable Pi profiles and finite agent invocations. It captures the effective configuration and public records so an operator can inspect a run. `@millrace-ai/millforge` 0.2.0 requires Node 22.19.0 or newer and pins Pi packages to 1.4.0.

See [Pi runtime](docs/pi-runtime.md) for isolated installation, profile manifests, commands, authentication and record contracts.

## Profiles and runs

Profiles are directories with a manifest and selected text resources. The CLI supports create, select, clone, edit, diff, import, export and revision restore. A run binds a profile digest and records the request, captured configuration, result and trace. Inspect saved runs with `inspect --run DIR`.

`generate` sends one direct public Pi AI request without tools. `agent` uses one Pi `AgentSession`. Pi owns the agent loop. The default agent tools are Pi's read, bash, edit and write, with optional grep, find and ls. Profiles and tool selection do not sandbox host access. Selected external tools require admitted binaries. The runtime does not download them.

Profile v2 supports native reasoning choices and the public OpenAI Responses OAuth route. Authentication uses a Millforge-owned connection, not a billing fallback. The first worker admission requires a lease covering the run. Automatic in-run refresh is not qualified. See [Pi runtime](docs/pi-runtime.md) for the defaults, auth lifecycle and limits.

## Optional codemode

Profile v3 opts an agent into Pi 1.4.0 codemode with `capabilities: {"extensions":["pi-codemode/v1"],"packages":[]}`. Select at least one native tool. Pi runs JavaScript that can call only those native tools. Millforge records the effective declarations, nested calls, joins and shared budgets in capture, trace and result v4. Existing profiles keep their record families.

The host fixes `models:false`, mode `on`, `inlineBudget:3000` and one worker. The original parent/run deadline includes worker and nested-queue waiting. Retained text and all spill/image bytes share the tool-output ceiling. These controls do not impose hard CPU, process-memory or peak-disk quotas. Store/load lasts within one invocation. See [Pi runtime](docs/pi-runtime.md#profile-v3-optional-codemode) for exact selection and route limits.

Codemode has bounded offline Linux acceptance through actual Pi and QuickJS. The optional optimizer has separate offline acceptance for the Codemode target, including task-specific adoption. Its production CLI refuses Codemode `run`. Native routes, deployed containment and performance require separate qualification for Codemode.

## Optional optimization

The separate `@millrace-ai/millforge-optimization` package provides finite Harness² campaigns via `millforge optimize`. The base package does not depend on it. Ordinary runs and saved profiles need neither the optimizer nor a teacher. Campaigns require explicit selection and adoption. Production native-target runs require protection qualified for the exact deployment. The optimizer does not replace Pi's agent loop.

## Status and boundaries

M3 accepted the exact V7 Linux artifact and selected Luna OAuth route, not all providers or platforms. The Plus-owned M4 adapter was accepted with offline Linux fake-endpoint evidence. Harness² O1 and O2 engineering were accepted offline with notes. The November 15 closeout supports native operation, containment and accounting for one exact Windows Bonsai route with a Linux host. The original full run failed ordinary grading. Its unchanged report passed four checks under the corrected offline fallback evaluator, with eight regressions passing. The November 15 packet did not rerun the full Bonsai journey with the corrected grader.

The November 18 O2 receipt accepts one native journey for the frozen Windows LFM3-1.7B Q4_K_M route with a Linux workload and an official GPT-6.1 Terra Low OAuth teacher. The seven-slot search found no observed improvement and retained the baseline. The retained profile passed all four ordinary checks in a base-only run. Reconciled accounting records 21 target requests and three teacher requests. Acceptance applies only to the exact route, workload and artifacts in that receipt.

Millforge is independent of Core, Plus and OS. Plus may invoke it through its own adapter. The historical Python runner and compiler source remain in this repository as history, not the architecture of this Node candidate. See [the roadmap](https://github.com/tim-osterhus/millforge/blob/main/ROADMAP.md) for selected future capability work and [third-party notices](THIRD_PARTY_NOTICES.md) and [license](LICENSE) for attribution.
