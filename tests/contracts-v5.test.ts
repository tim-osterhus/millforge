import {afterEach, describe, expect, it} from 'vitest';
import {mkdtemp, readFile, rm, stat, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {
  type CaptureV5, type Obj, type ProfileV4, type Trace,
  canonical, currentRuntimeGuards, object, parseJson, sourcePin, tightenLimits, validateCapture,
  validateProfile, validateRequest, validateResult, validateTrace,
} from '../src/contracts.js';
import {parsePythonArguments, pythonIdentity, pythonLockSha256, pythonToolSchema} from '../src/contracts-v5.js';
import {artifactManifest, createCapture, inspectRun, publishResult, requiredArtifacts, writeTrace} from '../src/artifacts.js';
import {createProfile, hash} from '../src/profile.js';

const sha = 'a'.repeat(64);
const roots: string[] = [];
afterEach(async () => {for (const root of roots.splice(0)) await rm(root, {recursive: true, force: true});});

async function records() {
  const input = object(parseJson(await readFile(new URL('./fixtures/codemode/profile.json', import.meta.url))));
  const p = validateProfile({...input, schema: 'millforge-profile/v4', profile_id: 'python-r1',
    tools: ['python_exec'], capabilities: {extensions: ['python-exec/v1'], packages: []}}) as ProfileV4;
  const request = validateRequest({schema: 'millforge-request/v1', request_id: 'r1', profile_id: p.profile_id,
    profile_digest: sha, expected_mode: 'agent', task: 'task', context: [], workspace: '/tmp', limits: {}, correlation: {}}, p, sha);
  const capture: CaptureV5 = {
    schema: 'millforge-capture/v5', request_id: 'r1', profile_digest: sha, limits: p.limits, cwd: request.workspace,
    resources: [], system_prompt: 'fixture', tool_names: ['python_exec'],
    tool_schemas: [{name: 'python_exec', sha256: hash(canonical(pythonToolSchema))}],
    runtime_guards: currentRuntimeGuards, versions: {millforge: '0.2.1', pi: '1.4.0', node: '26.7.0', platform: 'linux'},
    external_tools: [], python: {...pythonIdentity, admission_sha256: sha, environment_sha256: sha,
      lock_sha256: pythonLockSha256, interpreter_sha256: sha, bridge_sha256: sha, collector_sha256: sha},
    binding: {
      api: p.model.api, version: 2, provider_id: p.model.provider, model_id: p.model.id, base_url: p.model.base_url,
      request_url: `${p.model.base_url}/chat/completions`, source_pin: sourcePin,
      model_metadata: {source: {kind: 'operator', source_pin: sourcePin, catalog_sha256: null, profile_digest: sha},
        authored_overrides: p.model.metadata.overrides, selected: {...p.model.metadata.overrides, promptCache: null,
          inputLimits: null, samplingParams: null, samplingParamsByThinkingLevel: null}},
      compat: {catalog_values: {}, authored_overrides: p.model.compat, internal_resolution: 'unknown'},
      auth: {kind: 'none', provider_id: p.model.provider, reference: null, lease: null},
      requested_options: {...p.sampling, max_output_tokens: p.model.max_output_tokens,
        thinking: 'off', cache_retention: 'none', max_retries: 0},
      resolved_options: {thinking: 'off', thinking_source: 'profile', mapped_effort: null,
        cache_retention: 'none', cache_source: 'profile'},
    },
  };
  expect(validateCapture(capture, p, request, sha)).toEqual(capture);
  return {p, request, capture};
}

type Records = Awaited<ReturnType<typeof records>>;
const event = (kind: string, payload: Obj): Trace => ({schema: 'millforge-trace/v5', request_id: 'r1',
  sequence: 1, elapsed_ms: 0, kind, payload});
const closure = {worker: 'closed', tools: 'closed', writers: 'closed',
  inference: 'response_returned', detached_effects: 'none_observed'};

function events({p, capture}: Records) {
  function model(attempt: number, stop: 'normal' | 'tool') {
    return [event('model_start', {attempt}), event('provider_payload', {attempt, source: 'pi_onPayload',
      api: p.model.api, provider_id: p.model.provider, model_id: p.model.id, base_url: p.model.base_url,
      controls: {model: p.model.id, stream: true, store: null, temperature: null, top_p: null, max_tokens: 1000,
        max_completion_tokens: null, max_output_tokens: null, reasoning_effort: null, reasoning_summary: null,
        encrypted_context_requested: false, thinking: null, cache: {prompt_cache_key_present: false,
          prompt_cache_retention: null, prompt_cache_options: null, message_cache_control_present: false,
          tool_cache_control_present: false}}}),
    event('http_attempt', {attempt, source: 'counted_fetch', method: 'POST', url: capture.binding.request_url!}),
    event('model_end', {attempt, stop, usage: null, response_model: null,
      native_terminal: 'chat.finish_reason', native_status: stop === 'tool' ? 'tool_calls' : 'stop'})];
  }
  return [event('capture', {profile_digest: capture.profile_digest}), ...model(1, 'tool'),
    event('tool_start', {call_id: 'native', name: 'python_exec', arguments: {code: 'print("ok")'}}),
    event('tool_end', {call_id: 'native', status: 'completed', text: 'ok', truncated: false, dispatch: 'sent', cell_settled: true}),
    ...model(2, 'normal'), event('terminal', {status: 'completed', code: null}), event('closure', closure)];
}

async function saved(change?: (chain: Trace[]) => void, spill = false) {
  const root = await mkdtemp(join(tmpdir(), 'millforge-v5-'));
  roots.push(root);
  const {p, request, capture} = await records();
  const authored = join(root, 'authored.json');
  await writeFile(authored, JSON.stringify(p));
  const loaded = await createProfile(join(root, 'profile'), authored);
  request.profile_digest = capture.profile_digest = loaded.digest;
  request.workspace = capture.cwd = root;
  object(object(capture.binding.model_metadata).source).profile_digest = loaded.digest;
  const dir = join(root, 'run');
  await createCapture(dir, loaded, request, capture);
  const chain = events({p, request, capture});
  change?.(chain);
  chain.forEach((item, index) => {item.sequence = index + 1;});
  await writeTrace(dir, chain);
  if (spill) await writeFile(join(dir, 'output.txt'), 'surprise');
  const artifacts = await artifactManifest(dir, [...requiredArtifacts(loaded), ...(spill ? ['output.txt'] : [])]);
  await publishResult(dir, {schema: 'millforge-result/v5', request_id: 'r1', profile_id: p.profile_id,
    profile_digest: loaded.digest, mode: 'agent', status: 'completed', domain_outcome: null, output: 'done', diagnostic: null,
    counts: {model_requests: 2, tool_calls: 1, input_bytes: 4, output_bytes: 4, usage: null, backend_requests: null, cost: null,
      trace_bytes: (await stat(join(dir, 'trace.jsonl'))).size},
    timing: {started_at: '2026-11-23T00:00:00Z', ended_at: '2026-11-23T00:00:00Z', elapsed_ms: 0}, closure, artifacts});
  return inspectRun(dir);
}

describe('Python R1 finite records', () => {
  it('validates and inspects the v4/v5 family without Python or local admission', async () => {
    const run = await saved();
    expect(run.result.schema).toBe('millforge-result/v5');
    expect(run.result.counts.tool_calls).toBe(1);
    expect(run.capture.python?.lock_sha256).toBe(pythonLockSha256);
  });

  it('preserves v2 request limit tightening for the v4 profile', async () => {
    const {p} = await records();
    expect(tightenLimits(p, {tool_calls: 1}).tool_calls).toBe(1);
    expect(() => tightenLimits(p, {trace_bytes: 16384})).toThrow();
  });

  it('rejects mixed families and impossible capture facts', async () => {
    const {p, capture, request} = await records();
    const old = validateProfile({...p, schema: 'millforge-profile/v2', tools: [], capabilities: {extensions: [], packages: []}});
    expect(() => validateCapture(capture, old, request, sha)).toThrow('record/profile family mismatch');
    expect(() => validateCapture({...capture, schema: 'millforge-capture/v3'}, p, request, sha)).toThrow('record/profile family mismatch');
    expect(() => validateCapture({...capture, versions: {...capture.versions, millforge: '0.2.0'}}, p, request, sha)).toThrow();
    expect(() => validateCapture({...capture, python: {...capture.python, lock_sha256: sha}}, p, request, sha)).toThrow();
    expect(() => validateCapture({...capture, tool_schemas: [{name: 'python_exec', sha256: sha}]}, p, request, sha)).toThrow();
    expect(() => validateResult({schema: 'millforge-result/v3'}, p, request, capture)).toThrow('record/profile family mismatch');
    expect(() => validateTrace({...event('capture', {profile_digest: sha}), schema: 'millforge-trace/v3'}, p, request, capture))
      .toThrow('record/profile family mismatch');
  });

  it.each(['https://127.0.0.1:123/v1', 'http://localhost:123/v1', 'http://127.0.0.1/v1',
    'http://127.0.0.1:123/v1/', 'http://127.0.0.1:65536/v1'])('refuses unsupported route %s', async base_url => {
    const {p} = await records();
    expect(() => validateProfile({...p, model: {...p.model, base_url}})).toThrow();
  });

  it.each(['{"code":"a","code":"b"}', '{"code":"a"', '{"code":" \\t"}', '{"code":"a\\u0000"}',
    '{"code":"\\ud800"}', '{"code":"valid","other":true}', '{"code":"x"}' + ' '.repeat(16384)])
    ('refuses invalid original arguments', original => {expect(() => parsePythonArguments(original)).toThrow();});

  it('preserves the exact source and enforces the canonical byte budget', () => {
    const code = 'print("🧪")\n';
    expect(parsePythonArguments(JSON.stringify({code}))).toEqual({code});
    expect(() => parsePythonArguments(JSON.stringify({code: '\n'.repeat(10000)}))).toThrow();
  });

  it('refuses an orphan end, extra tool turn, uncertain completed dispatch and arbitrary artifacts', async () => {
    await expect(saved(chain => {chain.find(item => item.kind === 'tool_end')!.payload.call_id = 'orphan';})).rejects.toThrow();
    await expect(saved(chain => {chain.find(item => item.kind === 'tool_end')!.payload.dispatch = 'unknown';})).rejects.toThrow();
    await expect(saved(chain => {
      const end = chain.findIndex(item => item.kind === 'tool_end');
      chain.splice(end + 1, 0, event('tool_start', {call_id: 'another', name: 'python_exec', arguments: {code: 'x'}}));
    })).rejects.toThrow();
    await expect(saved(undefined, true)).rejects.toThrow('cannot adopt additional artifacts');
  });
});
