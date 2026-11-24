# Millforge

Millforge is a Node.js host for portable Pi profiles and finite agent invocations. It captures the effective configuration and public records so an operator can inspect a run. `@millrace-ai/millforge` requires Node 22.19.0 or newer and pins Pi packages to 1.4.0.

Install Millforge and validate the included generate profile without contacting a model endpoint.

```sh
prefix="$HOME/.local/millforge"
npm install --prefix "$prefix" @millrace-ai/millforge@0.2.1
"$prefix/node_modules/.bin/millforge" profile validate "$prefix/node_modules/@millrace-ai/millforge/profiles/generate"
```

See [Pi runtime](docs/pi-runtime.md) for installation, profile manifests, commands, authentication and record contracts.

## Profiles and runs

Profiles are directories with a manifest and selected text resources. The CLI supports create, select, clone, edit, diff, import, export and revision restore. A run binds a profile digest and records the request, captured configuration, result and trace. Inspect saved runs with `inspect --run DIR`.

`generate` sends one direct public Pi AI request without tools. `agent` uses one Pi `AgentSession`. Pi owns the agent loop. The default agent tools are Pi's read, bash, edit and write, with optional grep, find and ls. Profiles and tool selection do not sandbox host access. Selected external tools require admitted binaries. The runtime does not download them.

Profile v2 supports native reasoning choices and the public OpenAI Responses OAuth route. Authentication uses a Millforge-owned connection, not a billing fallback. The first worker admission requires a lease covering the run. Automatic in-run refresh is not qualified. See [Pi runtime](docs/pi-runtime.md) for the defaults, auth lifecycle and limits.

## Terminal UI

Run `millforge` in a terminal to open Pi with the selected profile, or use
`millforge tui --profile DIR`. The launcher supports Linux and macOS terminals.
It accepts ordinary v1 and v2 agent profiles with no credential or a named
environment credential. Use finite `millforge run` for OAuth or Codemode.

The terminal session keeps its history in memory. Use `/millforge` for profile and
saved-run commands or `/millforge harness2` for commands to run in a separate
shell. The installed PTY proof covers Linux with a scripted loopback endpoint.
The macOS terminal path has no installed PTY proof.
See [terminal use and limits](docs/pi-runtime.md#terminal-ui).

## Optional Python R1

A profile-v4 run can call `python_exec` in one stateful kernel. R1 requires a
separately admitted Linux x86-64 environment and restricted run actor. The
installed kernel proof used CPython 3.12.14 and a scripted loopback endpoint.
The sidecar ships as a separate GitHub release asset. Ordinary Millforge commands
require only the Node package. See the [Python R1 setup](docs/pi-runtime.md#profile-v4-optional-python-r1).

## Optional codemode

Profile v3 opts an agent into Pi 1.4.0 codemode with `capabilities: {"extensions":["pi-codemode/v1"],"packages":[]}`. Select at least one native tool. Pi runs JavaScript that can call only those native tools. Millforge records the effective declarations, nested calls, joins and shared budgets in capture, trace and result v4. Existing profiles keep their record families.

The host fixes `models:false`, mode `on`, `inlineBudget:3000` and one worker. The original parent/run deadline includes worker and nested-queue waiting. Retained text and all spill/image bytes share the tool-output ceiling. These controls do not impose hard CPU, process-memory or peak-disk quotas. Store/load lasts within one invocation. See [Pi runtime](docs/pi-runtime.md#profile-v3-optional-codemode) for exact selection and route limits.

Codemode has bounded offline Linux evidence through actual Pi and QuickJS.
The optimizer also passed a single protected scripted Codemode baseline in an
isolated Debian 13 Hyper-V guest. New deployments require their own protection
measurements and exact operator authority.

## Optional optimization

Millforge 0.2.1 includes the optimizer 0.1.1 archive as an inert file.
Install the archive explicitly to use `millforge optimize` for finite Harness²
campaigns. Campaigns require operator approval, and adoption requires separate
approval. See [optimizer installation and protected deployment](docs/pi-runtime.md#optional-optimizer).

## Status and boundaries

M3 accepted the exact V7 Linux artifact and selected Luna OAuth route, not all providers or platforms. The Plus-owned M4 adapter was accepted with offline Linux fake-endpoint evidence. Harness² O1 and O2 engineering were accepted offline with notes. The November 15 closeout supports native operation, containment and accounting for one exact Windows Bonsai route with a Linux host. The original full run failed ordinary grading. Its unchanged report passed four checks under the corrected offline fallback evaluator, with eight regressions passing. The November 15 packet did not rerun the full Bonsai journey with the corrected grader.

The November 18 O2 receipt accepts one native journey for the frozen Windows LFM3-1.7B Q4_K_M route with a Linux workload and an official GPT-6.1 Terra Low OAuth teacher. The seven-slot search found no observed improvement and retained the baseline. The retained profile passed all four ordinary checks in a base-only run. Reconciled accounting records 21 target requests and three teacher requests. Acceptance applies only to the exact route, workload and artifacts in that receipt.

Millforge is independent of Core, Plus and OS. Plus may invoke it through its own adapter. The historical runner and compiler ship separately as `millforge` on PyPI. See [the roadmap](https://github.com/tim-osterhus/millforge/blob/main/ROADMAP.md) for selected future capability work and [third-party notices](THIRD_PARTY_NOTICES.md) and [license](LICENSE) for attribution.
