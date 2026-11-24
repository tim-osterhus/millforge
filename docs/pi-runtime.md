# Millforge: portable profiles and finite Pi runs

The Node source candidate includes native profile v2 and records v3, plus optional codemode profile v3 and records v4. Native reasoning choices and public Luna OAuth retain their existing contracts. M3 accepted the exact V7 Linux artifact on the selected Luna OAuth route. That receipt does not qualify changed artifact bytes, other routes, macOS, in-run refresh or release. M2 evidence applies only to its earlier accepted candidate. The separate `@millrace-ai/millforge-optimization` package handles Harness² campaigns.

## Install the candidate

The package requires Node 22.19.0 or newer and pins both public Pi packages to 1.4.0. Install the tarball in an explicit npm prefix and invoke its absolute binary path. The historical `millforge` Python package is distributed separately on PyPI.

```sh
npm install --prefix /absolute/millforge-prefix /absolute/millrace-ai-millforge-0.2.0.tgz
/absolute/millforge-prefix/node_modules/.bin/millforge profile validate /absolute/millforge-prefix/node_modules/@millrace-ai/millforge/profiles/generate
```

From a built source tree, use `node /absolute/millforge/dist/node/src/cli.js`. Pure commands do not load Pi. Owned run and auth-command children load public Pi only after their environments exist.

## Profile v2: reasoning and Luna OAuth

Use v2 for the amended runtime. The shipped `profiles/generate` and `profiles/agent` directories remain legacy v1 examples with their original fixed-off semantics. Profile v2 produces only coordinated v3 records; old saved records retain their meaning.

Save this manifest as `/absolute/luna-profile.json`, then use `profile create /absolute/luna --from /absolute/luna-profile.json`:

```json
{
  "schema": "millforge-profile/v2",
  "profile_id": "luna",
  "revision": 1,
  "mode": "generate",
  "runtime": {
    "pi_version": "1.4.0",
    "binding_version": 2
  },
  "model": {
    "provider": "openai",
    "api": "openai-responses",
    "id": "gpt-6-luna",
    "base_url": "https://api.openai.com/v1",
    "auth": {
      "kind": "oauth",
      "provider": "openai"
    },
    "context_tokens": 272000,
    "max_output_tokens": null,
    "metadata": {
      "source": "pinned-catalog",
      "overrides": {}
    },
    "compat": {}
  },
  "instructions": "Complete the supplied task and return its requested result.",
  "sampling": {
    "temperature": null,
    "top_p": null
  },
  "tools": [],
  "assets": [],
  "capabilities": {
    "extensions": [],
    "packages": []
  },
  "limits": {
    "model_requests": 1,
    "tool_calls": 0,
    "elapsed_seconds": 300,
    "model_timeout_seconds": 295,
    "tool_timeout_seconds": 30,
    "input_bytes": 1048576,
    "context_bytes": 16777216,
    "output_bytes": 1048576,
    "tool_output_bytes": 1048576,
    "trace_bytes": 16777216
  },
  "terminal": {
    "format": "text",
    "required_keys": [],
    "outcome_key": null,
    "outcomes": []
  }
}
```

These finite budgets are example operator choices. Omitted `reasoning` selects Millforge's pinned medium mirror of Pi 1.4.0, clamped through its public capability API. Direct Luna supports explicit `off`, `low`, `medium`, `high`, `xhigh` and `max`; explicit `minimal` refuses. Off is optional. Capture separates authored absence, resolved choice, mapped effort and later wire observations; none attests backend-effective effort. Native thinking/signatures/encrypted continuation stay private in memory, separate from saved public projections.

Null sampling and token ceiling are required on this sign-in route because the pinned adapter drops those fields. No server output-token cap is claimed. Actual count/time/byte guards remain. `context_tokens` is a reported-input stop threshold, unknown when native usage is absent. Omitted cache preference retains native short hints; explicit `none` suppresses supported hints. OAuth `long` refuses. No warming inference is added.

Prefix these commands with the absolute candidate Node CLI:

```text
auth login --provider openai --auth-home /absolute/millforge-auth
auth status --provider openai --auth-home /absolute/millforge-auth
auth refresh --provider openai --auth-home /absolute/millforge-auth --min-validity-seconds 600
run --profile /absolute/luna --request /absolute/request.json --output-dir /absolute/new-run --auth-home /absolute/millforge-auth
auth logout --provider openai --auth-home /absolute/millforge-auth
```

Bind the digest from `profile show` in ordinary request v1. Auth defaults to `$XDG_CONFIG_HOME/millforge/auth`, falling back to `$HOME/.config/millforge/auth`, resolved before the owned environment changes HOME. Foreign stores, symlinks and API-key entries refuse. There is no key/token literal, Codex credential extraction or Platform-billing fallback. Pure profile/inspect commands never open the home.

Login presents the public browser URL interactively and listens on exactly `127.0.0.1:1455`. Product receipts save no URL/state/callback/token values. A port conflict refuses. Remote sign-in needs an operator-arranged loopback SSH forward; the product adds no proxy or browser dependency. Keep same-home auth commands and runs sequential. Existing connections are not silently registered again; recovery needing a fresh registration requires the user's later choice.

First worker admission needs a stored lease covering the full original run plus Pi's five-minute refresh threshold. Check status/refresh deliberately first. The worker uses a selected read-only memory snapshot; automatic in-run refresh remains unqualified. Auth cancellation waits for a started SDK operation/persistence. The SDK's 15-second timeout bounds its refresh token exchange, not the login code exchange or whole command/persistence. Potential registration or unfinished persistence yields uncertainty, never false closure.

Auth receipts use a new exclusive 0600 file, default `auth-home/receipts/<UUID>.json`; `--receipt FILE` selects another new path. Status is completed/failed/cancelled/uncertain. Net snapshot changes count store mutations; unobservable refresh starts are null. Pre-capture capability/auth refusal exits 2 and leaves only owned Pi/temp directories plus `diagnostic.json`, without completed-run records.

## Profile v3: optional codemode

To select codemode, retain all required v2 manifest fields and use this capability-specific portion:

```json
{
  "schema": "millforge-profile/v3",
  "mode": "agent",
  "tools": ["read", "write"],
  "capabilities": {"extensions": ["pi-codemode/v1"], "packages": []}
}
```

The complete profile retains runtime `{ "pi_version":"1.4.0", "binding_version":2 }` and the existing native model/auth, sampling, reasoning, cache, assets, terminal and limits rules. Tools are a nonempty subset of the seven native names. Select codemode through capabilities. Do not add it to `tools`. Profile commands and run/inspect use their existing CLI forms.

Millforge calls only the public `createCodemodeExtension({models:false,mode:'on',inlineBudget:3000})`. Millforge fixes these options. Scripts have no `models` namespace, classifier or image-model access. Their callable tools contain exactly the selected native subset. Search and description helpers cannot restore removed tools. 

The host adds one handler-only guard. A public `DefaultResourceLoader` materializes those two named inline factories over empty owned discovery roots with `projectTrusted:false`.

The active registry contains selected natives plus codemode. Capture v4 hashes each actual post-loadout description and parameter schema separately. It also records callable order, codemode constrained-sampling metadata, fixed options, loader assertions and pinned VM/output/store constants. Provider observations save only the API-specific grammar or schema digest. They must agree with the serialized request. No raw declaration, message, credential or request body is saved.

Only one top-level tool phase runs at a time. Each codemode parent and each admitted nested native call consumes the same tool-call budget. Nested IDs append `/1`, `/2` and subsequent decimal integers to the exact parent ID. Responses parent IDs remain opaque.

Each recorded codemode parent end has one `nested_join` immediately before it. Recorded child counts and observed unrecorded calls drain separately.

Refused end records leave an honestly unfinished failed trace. Refused records do not keep physically ended calls in the drain sets. Join and parent-end budget admission uses their exact serialized bytes together, so a budget refusal cannot leave a dangling join. A later hook failure cannot replace the first diagnostic. Uncertain actual closure takes precedence.

The parent deadline is the earlier of the original run work deadline and its captured tool timeout. Worker acquisition and queued nested calls consume that deadline. Scripts may omit their timeout. A shorter script timeout can fail one tool while the session continues to a normal final response. Unawaited calls, cancellation and worker-thread failure use the same drain path. Effects that already occurred remain visible.

The host sets `PI_CODEMODE_MAX_WORKERS=1` before Pi import. Pi's pinned VM memory ceiling is 256 MiB. Its VM output ceilings are 16 Mi characters and 100,000 items. Its worker wait ceiling is 60 seconds. The original host deadline still applies.

The store permits 256 Ki characters per value and 1 Mi characters total. Capture records these pinned behavior facts. Profiles cannot change them.

Retained parent and child text, full truncated-output spills and image spills share `tool_output_bytes`. Codemode truncation follows its actual `details.fullOutputPath`. Image artifacts use `application/octet-stream`. Text spills retain their text metadata. 

Final spill accounting occurs after execution and does not bound peak disk use. Profiles do not enforce hard CPU or process-memory quotas. Native tools retain their host access.

Store/load works across scripts in the one in-memory Pi session. A fresh invocation starts empty. The launcher establishes worker/process-group exit before final publication and removes the owned Pi tree. Saved inspection never resumes a store or a worker.

### Codemode wire admission

Completions and Responses admit Pi's public grammar declaration or JSON-schema fallback. Messages admits its actual JSON-schema conversion. Each request must contain exactly one supported codemode declaration in top-level `tools`. Missing, duplicated or unsupported shapes refuse `INCOMPATIBLE_ROUTE` before HTTP.

For a v3 Responses profile, select the existing public compatibility values `supportsAdditionalTools:false` and `supportsToolSearch:false` to keep declarations in top-level `tools`. Choose `supportsOpenAIGrammarTools` explicitly when selecting grammar or fallback. Pi's catalog-default transcript/additional-tools transport is outside this packet and refuses. Native v2 compatibility defaults remain unchanged. These selections have only offline fake-peer coverage here.

Profile v1 keeps records v1/v2. Profile v2 keeps records v3. Profile v3 uses only capture/trace/result v4, with `parent_call_id`, `nested_join`, the provider declaration projection and `counts.nested_tool_calls`. Crossed families refuse `UNSUPPORTED_VERSION`. Older nonempty capability manifests still refuse. No saved records migrate.

Offline Linux tests use actual Pi and QuickJS with synthetic credentials or local fake peers. Codemode native providers, deployed containment and performance require separate qualification.

O3-C2 separately accepts the optional optimizer mapping for bounded offline Linux use with fake peers. The production optimizer CLI refuses Codemode `run`. Task-specific offline adoption produces a profile the base CLI can validate and run independently.

The remaining sections preserve legacy record details and M2 evidence. Canonical specification section 9 defines exact v2/v3 schemas and boundaries.

## Profile commands

Profiles are ordinary directories with `profile.json`, explicit text resources and optional `.history/<revision>/`. Commands are:

```text
profile create DIR --from MANIFEST.json
profile clone SOURCE DEST --id ID
profile select DIR
profile show DIR
profile validate DIR
profile edit DIR --from MANIFEST.json
profile diff LEFT RIGHT
profile export SOURCE DEST
profile import SOURCE DEST
profile restore DIR --revision N
inspect --run DIR
```

`show` emits `{profile,digest}` so the validated authored document and its digest are separate. `validate` emits `{valid:true,digest}`. Diff emits sorted `changes` entries with JSON pointer, before/after values and explicit presence flags, plus sorted `assets` entries with before/after hashes. Pure commands resolve no provider credentials and invoke no editor, shell, model, package installer or optional product.

Create, copy and import destinations must be new. Clone changes only the profile ID and revision (to 1). Import/export copy selected active resources only and exclude history and selection; active symlinks, special files and executable files refuse. Text assets use `.md`, `.txt` or `.json`, strict UTF-8 and no executable mode bits. Every selected skill support file must belong beneath a declared skill `SKILL.md`. The limit is 128 assets and 4 MiB total authored manifest/assets bytes. Unselected ordinary files are not adopted into copies or digests.

Selection saves only `{path:absolute_profile_directory}` in `$XDG_CONFIG_HOME/millforge/selection.json`, falling back to `$HOME/.config/millforge/selection.json`. Explicit profile resolution wins over selection; it does not read a selected path first. `run` resolves the explicit profile or saved selection once before capture.

Edit requires the supplied current revision and ID, validates replacement assets before publication, captures complete prior manifest/resources in `.history/<old_revision>/`, then publishes the next increasing revision. Restore validates a complete history snapshot and copies its resources into a new increasing revision. It never resets a revision or overwrites history. A short exclusive `.millforge-write.lock` reservation serializes supported writers; a preexisting reservation refuses and is never silently broken. Exact current manifest bytes are compared again before publication. This is cooperative local conflict detection, not protection from hostile same-user mutation. Restore preflights every destination before retaining history or changing assets: existing unselected files (even identical bytes), non-directory parents, symlinks, special files and executable targets refuse. An edit can select an existing ordinary asset after validating it; that does not authorize restore to overwrite unselected bytes. A complete byte-identical history snapshot may be reused after an interrupted publication; incomplete, different or extra history content refuses and stays preserved.

A handled failure before manifest replacement attempts to restore overwritten selected bytes, remove new asset files written by that call and remove its empty new directories. Rollback checks that each current asset still matches this call's write; a competing change is retained. An incomplete rollback reports a manual-recovery error with the complete prior snapshot retained in history. Manifest replacement is the commit point. This is bounded recovery for errors caught by the live process, not a transaction service, crash atomicity or power-loss durability. A crash can leave partial owned state or a reservation requiring manual recovery. History is never deleted to make a retry succeed.

The digest is SHA-256 of compact recursively key-sorted UTF-8 JSON:

```json
{"assets":[{"path":"assets/context.md","sha256":"<lowercase hex>","size":123}],"profile_sha256":"<SHA-256 of raw active profile.json>"}
```

Asset entries and object keys sort by JavaScript UTF-16 code-unit ordering, without locale collation or Unicode normalization. Strings, keys and numbers use JavaScript `JSON.stringify` escaping/serialization; object keys are recursively sorted, arrays preserve their order, and the compact digest input has no trailing newline. The resulting string is encoded as UTF-8 before SHA-256. Asset entries sort by path. Authored asset order still controls captured resource order. History, selection, timestamps and unselected files do not contribute. The request binds exact profile ID, digest and mode; its limits may only tighten the complete profile limits. Tightening `elapsed_seconds` must leave both call timeouts at most `elapsed_seconds-5`; tighten those timeouts too when necessary. No timeout is silently clipped by the data validator.

Profile base URL spellings are admitted exactly as authored. Raw ASCII whitespace/control characters, DEL, backslash and `@` refuse before URL parsing, as do non-HTTP(S), userinfo, query and fragment forms. Admitted spelling is retained rather than normalized. Asset first components matching `profile.json`, `.history` or `.millforge-write.lock` after case folding are reserved; this check does not qualify a platform or volume.

## Strict public record data

Profile/request/result/trace top-level and nested keys follow the runtime specification. Unsupported versions, duplicate JSON keys at any depth, unknown fields, invalid UTF-8, non-finite numbers, boolean numeric fields, unsafe paths and unsupported capabilities refuse. No missing bound is invented. RFC3339 timestamps use UTC `Z`; counts are nonnegative safe integers and unknown usage/backend work/cost stay null.

The source contract names capture facts without fixing all nested serialization. Both capture versions use these exact top-level keys:

- `schema`, `request_id`, `profile_digest`, `limits`, `cwd`, `resources`, `system_prompt`, `tool_names`, `tool_schemas`, `binding`, `credential_env`, `versions`, `external_tools`.
- Each resource is `{path,sha256,kind,size}` in authored order. Each tool schema is `{name,sha256}` in exact captured tool order.
- Legacy v1 `binding` is `{api,version,provider_id,base_url,source_pin,compat,options}`. Version is 1 and source pin is Pi `4a080252f06caf63a525d7873c5d94d3c23887e0`. Options are exactly `{temperature,top_p,thinking,max_retries}`, matching the profile with thinking `off` and retries 0.
- `versions` is `{millforge,pi,node,platform}`, with candidate Millforge `0.2.0` and Pi `1.4.0`.
- External executable entries are `{name,path,sha256,size,stat}`; stat is `{dev,ino,mode,mtime_ms}`. Entries match only selected find/fd and grep/rg; no filesystem probe occurs during inspect.

Compatibility data accepts only fields and nested types from the pinned public Pi OpenAICompletionsCompat or AnthropicMessagesCompat declarations. OpenAI authored controls remain required and must match. This validates explicitly supplied compatibility data; it does not resolve defaults. Pi's actual `getCompat`/`detectCompat` are private at this pin. That private resolved object cannot be observed through the pinned public seams. The selected v2 amendment below instead records authored intent, bounded public observations and explicit unknown internal resolution. M1 imports neither private Pi modules nor a copied compatibility resolver. Offline fixtures deliberately use fake provider/schema facts and are not runtime evidence.

Saved request/capture/result reads allow up to 32 MiB of encoded JSON, covering worst-case six-byte escaping of admitted text and bounded metadata. Decoded profile/request/context/output limits remain unchanged. This larger encoded bound applies only to structured request/capture/result records; profile resources, trace and retained tool artifacts keep their original limits, and retained extra artifacts plus traced tool output share the tool-output ceiling. Immutable publication links a fully written same-directory temporary file into a new final name without overwriting an existing record, then removes the temporary name.

Trace payload keys are fixed per kind in the specification. `tool_end.status` uses `completed|failed|cancelled|timeout`. Traces start with one capture event, use contiguous sequence numbers and monotonic elapsed values, pair sequential model attempts and tool calls, then end with terminal followed by closure. A completed result needs normal finish, no unfinished traced model/tool and closed owned resources with no observed detached effect. The existing record schema requires `cancelled` to carry `CANCELLED`, `timeout` to carry `TIMEOUT`, and completion to carry no diagnostic. A `failed` record can also carry `CANCELLED` or `TIMEOUT`. The structural trace validator permits `cancel` or `limit` before a completed terminal; this preserves existing saved records and is not permission for a worker to ignore a stop condition.

The M2 producer makes completion a single cutoff after a validated normal response, session settlement and required closure. A cancellation, deadline or enforced limit accepted before that cutoff stops admission and prevents completion. Signals or late callbacks after the final immutable result must not change it, reopen admission or produce a second terminal. The M1 supplied offline records remain data fixtures and prove no runtime timing or cutoff behavior.

Saved declarations are consistency evidence, not independent proof of actual execution, process closure, remote inference cessation or sandboxing.

## Producer and inspect boundary

`createCapture` accepts an explicitly validated loaded profile, request and supplied effective-data object. It exclusively creates an owned directory and snapshots profile/resources, request and capture; it launches nothing. `writeTrace` publishes one complete bounded trace. `artifactManifest` hashes only an explicit list of owned paths. `publishResult` validates all identities, counters, terminal shape, closure, trace consistency and artifacts before publishing one final immutable result. M2 splits exclusive tree preparation from immutable actual capture. Its bounded trace writer reserves 16 KiB for terminal and closure records.

`inspect` reads only saved profile/resources/request/capture/result/trace and the result's declared artifacts. It rejects missing, truncated, conflicting, identity-mismatched, hash-mismatched, symlink and unowned records. Directory enumeration rejects unowned bytes; it never adopts them. The result does not hash itself. Inspect does not resolve environment secrets, recheck current workspaces/executables, relaunch, repair or probe a provider. A later profile edit cannot change an earlier capture.

Pure command success exits 0; invalid/unsupported command data or file preflight exits 2. Runtime statuses map to exits completed 0, failed 3, timeout 124 and cancelled 130. A killed worker or pre-capture refusal can leave no final result and exits through a typed infrastructure/preflight diagnostic. No profile/tool setting supplies OS containment.

## Prior M2 source-fit disposition — 2026-11-04

This bounded review does not implement or qualify M2. At the pinned Pi 1.4.0 source, public payload/response hooks expose request parameters or the original model, not the provider's fully resolved internal compatibility object. The private `getCompat`/`detectCompat` helpers are not an admitted seam. The original contract required full effective compatibility capture. Tim subsequently selected the v2 public-evidence amendment below. Authored-only or inferred compatibility still must not be presented as observed effective settings. Independent Phase A approval precedes any bounded M2 implementation.

The pinned coding-agent root exports `createBashToolDefinition`, `createLocalBashOperations`, `BashOperations`, `BashToolOptions` and `BashSpawnHook`. `createAgentSession` accepts `customTools`, which can replace a same-name builtin. A later wrapper can reserve bytes before forwarding `onData` into Pi's accumulator and abort through a signal. It must set owned `TMPDIR`/`TMP`/`TEMP` before import, await spill closure and inventory all owned temp entries, including failure paths. Display truncation alone does not bound spill storage. Their existence is source-fit evidence, not runtime settlement, cancellation, output-limit or cleanup evidence. No private Pi imports, copied compatibility resolver, worker, provider call or optimizer is added here. Exact evidence and remaining decisions are in `lab/tasks/millforge-closeout-sourcefit/2026-11-04/source-fit/findings.md` and its hashed source manifest.


## Selected v2 public evidence — 2026-11-04

New runs use `millforge-capture/v2`, `millforge-result/v2` and `millforge-trace/v2` together. Existing complete v1 runs retain their reader rules. Mixed or unknown versions refuse. Profile/request schemas remain v1. Result v2 keeps the prior fields and requires `counts.backend_requests:null`: client attempts do not establish server-side work. A future nonnull backend count needs a separately selected trustworthy evidence contract.

Capture v2 binding is exactly `{api,version,provider_id,model_id,base_url,request_url,source_pin,compat,requested_options}`. Binding version mirrors `profile.runtime.binding_version` (currently 1); the outer capture/result/trace schemas version their record shapes independently. API, model ID and base URL equal the profile. Provider ID is explicitly selected. `request_url` is one intended endpoint, not an observed request. It obeys the existing raw HTTP(S) spelling rules, with one API-specific fixed-query exception: Anthropic requires exactly `?beta=true`; OpenAI permits no query. Extra, duplicate, changed or percent-encoded query variants refuse. Base URLs remain query-free. It has the base URL's parsed origin and the same path or a path below its slash boundary. Later host tests must prove the pinned public client's endpoint mapping.

`compat` is exactly `{authored_overrides:<profile.model.compat>,internal_resolution:"unknown"}`. Extra overrides, resolved values and observed-resolution labels refuse. `requested_options` contains the existing exact temperature/top_p/thinking/max_retries fields. These match profile sampling, thinking `off` and retries 0. This capture is immutable before dispatch. Its effective loaded resources/tool schemas remain separate from selected intent.

Trace v2 adds two exact payload shapes:

```text
provider_payload: {attempt,source,api,provider_id,model_id,base_url,controls}
http_attempt: {attempt,source,method,url}
controls: {model,stream,temperature,top_p,max_tokens,max_completion_tokens}
```

The payload source is `pi_onPayload`. Identity equals capture's selected dispatch identity. Model equals the selected ID; stream is true. Temperature/top-p match the request. The authored OpenAI token-field choice, or Anthropic max_tokens, carries the actual positive safe-integer ceiling, at most the profile max-output-token value. A lower observed ceiling is retained unchanged; zero, widening and invalid types refuse. The other token field and absent optional top-p use null as an absence sentinel. This sentinel is neither a transmitted JSON null nor an inferred default. No messages, tools, headers, raw body, auth or reasoning fields may be saved in these observations.

The HTTP source is `counted_fetch`, method is `POST`, and URL exactly equals captured `request_url`. Both events belong to the active numbered model attempt. At most one payload precedes at most one HTTP event. Normal/tool-stop model endings require both. Failed pre-network runs may retain a valid prefix without invented observations. Duplicate/orphan/reordered/post-terminal events refuse. Generate still permits only one attempt; all observation bytes consume the existing trace budget.

The later host must observe the final payload after supported public control setting. Missing requested controls refuse before HTTP. Public OpenAI sampling parameters can supply top_p; private Anthropic compatibility can omit temperature even with thinking off. Unsupported settings must refuse. Before HTTP, counted fetch must compare bounded actual outgoing body controls with the accepted hook observation and selected identity. It must also check URL, method, redirect policy, deadlines and remaining attempt budget before recording/counting dispatch. Do not save that body. An onPayload snapshot alone does not prove final wire contents.

These validators establish consistency of supplied records. Provenance labels do not independently attest runtime execution. Internal resolved compatibility remains unknown. No host implementation, private resolver, provider call or qualification is added by Phase A. The frozen specification and `lab/tasks/millforge-capture-m2/2026-11-04/` own the selection and independent approval gate. Existing finite limits, sanitization, native finish, tool settlement, completion cutoff and closure duties remain unchanged.


The fixed Anthropic query matches the pinned SDK 0.129.0 beta messages resource, whose POST target is `/v1/messages?beta=true`. Exact static evidence is `lab/tasks/millforge-capture-m2/2026-11-04/preflight/supplemental/beta-message-resource.json`. This finding proves the selected resource spelling, not an executed request or broader route qualification. Captured and observed request URL spellings must still match exactly.


## M2 worker and evidence scope

```text
node /absolute/millforge/dist/node/src/cli.js run --profile PROFILE --request REQUEST.json --output-dir NEWDIR
node /absolute/millforge/dist/node/src/cli.js inspect --run NEWDIR
```

The launcher validates explicit profile/request data and selected PATH executables before starting a private Node worker. It resolves a configured credential once from the named launch variable. The worker receives that value through private IPC. Its environment excludes provider credential variables.

Null credentials use a nonsecret SDK placeholder with authentication headers explicitly suppressed.

Before Pi import, the worker owns `PI_OFFLINE=1`, a new `pi/bin`, and `TMPDIR`/`TMP`/`TEMP` under the exclusive run tree. Selected fd/rg identities are rechecked before every admitted tool effect. ResourceLoader returns only captured context and skills, literal instructions and empty extension/prompt/theme lists. Literal authored instructions append to Pi's native base prompt, preserving native tool contributions. Pi advertises captured skills only when read or bash is selected. Without either tool, the captured skill file remains in the profile snapshot but Pi omits its prompt advertisement.

Session, settings and credential storage are in memory. Pi still constructs its static builtin model catalog. The selected wrapper fences every dispatched model/API/provider and exposes no fallback.

The actual session prompt, native tool schema hashes and selected binding intent are captured before effects. Agent tool execution is sequential. Native before/after hooks remain chained.

Prompt completion, `agent_settled`, idle state, no queued messages, matched tool IDs and native normal final finish are required for completion. The parent waits for worker exit and checks its owned process group before publishing `result.json`.

Bash retains the native Pi definition with owned process-group operations. Its operations admit output bytes before forwarding them to Pi.

Native spill files and duplicated retained tool text share the tool-output ceiling. Before forwarding Bash bytes, the worker conservatively reserves raw spill bytes, bounded decoded display text and path/status overhead. The reservation covers both byte and line truncation without predicting when Pi creates a spill. It can refuse output before the authored ceiling. This allowance does not widen any limit.

Native spill closure precedes manifest publication. Failed or uncertain closure never produces authoritative output.

One monotonic work deadline reserves five seconds for cleanup. Repeated signals share cleanup without resetting counters or deadlines. Stuck writes remain bounded by the outer worker deadline. Forced worker termination can leave only partial records. `inspect` refuses an incomplete tree rather than inferring success. Closing HTTP does not establish that remote inference stopped.

M2 evidence belongs to `lab/tasks/millforge-capture-m2/2026-11-04/final-acceptance.json`, with the additive OH review at that task's `m2/review/oh-second-opinion/receipt.json`. Its 274 executions apply to the exact accepted candidate hashes and finite localhost fake scenarios. Later qualification is scoped to its own artifact and receipt. No live provider, macOS, installed-prefix, minimum-Node, retained-history or release qualification follows from M2.

Deadlines are cooperative inside the worker. A synchronous native fd/rg version probe can block its event loop until the parent outer deadline kills it, potentially leaving no final result. Identity/bin checks also leave a same-user mutation window; they do not provide containment. Literal known-credential filtering does not promise removal of encoded or transformed secrets. The receipts retain unexecuted orphan/queued/missing-settled/blocked-filesystem injections and the source-only Anthropic strict-fallback reachability conclusion.

M3 acceptance and its exact artifact, journeys and limits are recorded in the Millrace-Dev workspace at `lab/tasks/millforge-m3/2026-11-05/final-acceptance.json`. The receipt covers selected Linux and Luna OAuth evidence, not general provider or platform qualification.

The optional optimizer has separate exact-route evidence. The November 15 closeout supports native operation, containment and accounting for one exact Windows Bonsai route with a Linux host. The original full run failed ordinary grading. Its unchanged report passed four checks under the corrected offline fallback evaluator, with eight regressions passing. The November 15 packet did not rerun the full Bonsai journey with the corrected grader.

The November 18 O2 receipt accepts one full journey for the exact Windows LFM3-1.7B Q4_K_M route and Linux workload. The seven-slot search found no observed improvement and retained the baseline. The retained profile passed four ordinary checks in a base-only run. Reconciled accounting records 21 target requests and three teacher requests. See `lab/tasks/millforge-lfm3-o2/2026-11-18/final-status.json` in the coordination workspace.

The historical codemode source-fit blocked its earlier worker-control contract. The source candidate now implements the bounded successor described above: original deadline abort, fixed Pi behavior and retained-output/spill accounting. Hard process and peak-disk quotas remain outside that implementation.
