/** Installed Pi admission proof. The fixture tool returns text and never starts Python. */
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdir, mkdtemp, readFile, rm, stat, writeFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {promisify} from 'node:util';

const packageDir = resolve(process.argv[2] ?? '');
assert(process.argv[2], 'Provide the absolute installed Millforge package directory');
const source = name => import(pathToFileURL(join(packageDir, 'dist', 'node', 'src', `${name}.js`)));
const {createBinding} = await source('bindings');
const {PythonActionObserver} = await source('python-r1-host');
const {pythonToolName, pythonToolSchema} = await source('contracts-v5');
const {memoryCredentials} = await source('pi-host');
const piName = '@earendil-works/pi-coding-agent';
const piDir = resolve(packageDir, '../..', piName);
const piPackage = JSON.parse(await readFile(join(piDir, 'package.json'), 'utf8'));
assert.equal(piPackage.name, piName);
assert.equal(piPackage.version, '1.4.0');
assert.equal(piPackage.exports['.'].import, './dist/index.js');
const pi = await import(pathToFileURL(join(piDir, piPackage.exports['.'].import)));
const workspace = await mkdtemp(join(tmpdir(), 'millforge-r1-native-'));
const code = 'print("fixture")\n';
const args = JSON.stringify({code});
const exec = promisify(execFile);

function profile(base_url) {
  return {
    schema: 'millforge-profile/v4', profile_id: 'python-r1-native-fixture', mode: 'agent', revision: 1,
    runtime: {pi_version: '1.4.0', binding_version: 2},
    model: {
      provider: 'millforge-loopback', api: 'openai-completions', id: 'python-r1-fixture', base_url,
      auth: {kind: 'none'}, context_tokens: 8192, max_output_tokens: 512,
      metadata: {source: 'operator', overrides: {
        name: 'Python R1 loopback', input: ['text'], cost: null, reasoning: false,
        thinkingLevelMap: {}, contextWindow: 8192, maxTokens: 512,
      }},
      compat: {supportsFinishReason: true, maxTokensField: 'max_tokens'},
    },
    instructions: 'Complete the local test.', sampling: {temperature: null, top_p: null},
    tools: [pythonToolName], assets: [], capabilities: {extensions: ['python-exec/v1'], packages: []},
    limits: {
      model_requests: 3, tool_calls: 2, elapsed_seconds: 15, model_timeout_seconds: 5, tool_timeout_seconds: 3,
      input_bytes: 8192, context_bytes: 65536, output_bytes: 8192, tool_output_bytes: 65536, trace_bytes: 1048576,
    },
    terminal: {format: 'text', required_keys: [], outcome_key: null, outcomes: []},
    reasoning: 'off', cache: {retention: 'none'},
  };
}

async function proveCliRefusal() {
  let requests = 0;
  const server = createServer((_req, res) => {requests++; res.writeHead(400).end();});
  await new Promise((done, error) => {server.once('error', error); server.listen(0, '127.0.0.1', done);});
  try {
    const dir = join(workspace, 'cli-profile');
    const request = join(workspace, 'cli-request.json');
    const selected = profile(`http://127.0.0.1:${server.address().port}/v1`);
    await mkdir(dir);
    await writeFile(join(dir, 'profile.json'), JSON.stringify(selected));
    const bin = join(packageDir, 'dist', 'node', 'src', 'cli.js');
    const run = (args, env = {}) => exec(process.execPath, [bin, ...args], {env, timeout: 5000});
    const report = JSON.parse((await run(['profile', 'validate', dir])).stdout);
    assert.equal(report.valid, true);
    await writeFile(request, JSON.stringify({schema: 'millforge-request/v1', request_id: 'r1-cli',
      profile_id: selected.profile_id, profile_digest: report.digest, expected_mode: 'agent', task: 'start no effects',
      context: [], workspace, limits: {}, correlation: {}}));
    const cases = [{name: 'missing manifest argument', env: {},
      message: 'Python R1 requires an operator-admitted environment manifest'}];
    const uid = process.getuid?.();
    const sharedActor = process.platform === 'linux' && process.arch === 'x64' && uid === 1000;
    if (process.platform !== 'linux' || process.arch !== 'x64' || !uid || uid === 1000 || uid !== process.geteuid?.()) {
      cases.push({name: sharedActor ? 'Linux x64 UID 1000 actor policy with a supplied path' : 'actor or platform policy with a supplied path',
        env: {MILLFORGE_PYTHON_R1_MANIFEST: join(workspace, 'absent-manifest')},
        message: 'Python R1 requires the selected restricted Linux x86-64 run actor'});
    }
    for (const [index, test] of cases.entries()) {
      const output = join(workspace, `run-${index}`);
      await assert.rejects(run(['run', '--profile', dir, '--request', request, '--output-dir', output], test.env), error => {
        assert.equal(error.code, 2);
        assert.deepEqual(JSON.parse(error.stderr), {code: 'UNSUPPORTED_CAPABILITY', message: test.message});
        return true;
      });
      await assert.rejects(stat(output), {code: 'ENOENT'});
    }
    assert.equal(requests, 0);
    console.log(JSON.stringify({name: 'installed CLI preflight refusals', requests, refusals: cases.map(test => test.name)}));
  } finally {
    server.closeAllConnections();
    await new Promise(done => server.close(done));
  }
}

async function prove(name, steps, expected) {
  let requests = 0, dispatched = 0, authorized, peerError;
  const committed = [], refused = [];
  const server = createServer(async (req, res) => {
    try {
      assert.equal(req.url, '/v1/chat/completions');
      assert.equal(req.method, 'POST');
      assert.equal(req.headers.authorization, undefined);
      assert.equal(req.headers['x-api-key'], undefined);
      const chunks = [];
      for await (const part of req) chunks.push(part);
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      assert.deepEqual(body.tools.map(tool => tool.function.name), [pythonToolName]);
      const spec = steps[requests++];
      assert(spec, 'unexpected continuation');
      res.writeHead(200, {'content-type': 'text/event-stream'});
      const frame = (delta, finish_reason = null) => {
        const chunk = {id: `chat-${requests}`, object: 'chat.completion.chunk', created: 1, model: body.model,
          choices: [{index: 0, delta, finish_reason}]};
        res.write(`data: ${JSON.stringify(chunk)}\n\n`);
      };
      if (spec.final) {
        frame({role: 'assistant', content: 'normal final'});
        frame({}, 'stop');
      } else {
        const id = spec.id ?? 'native-call';
        const original = spec.original ?? args;
        frame({role: 'assistant', tool_calls: [{index: 0, id, type: 'function',
          function: {name: pythonToolName, arguments: original.slice(0, 8)}}]});
        frame({tool_calls: [{index: 0, ...(spec.changedId ? {id: 'inconsistent-id'} : {}),
          function: {arguments: original.slice(8)}}]});
        if (!spec.missingFinish) frame({}, 'tool_calls');
      }
      res.end('data: [DONE]\n\n');
    } catch (error) {
      peerError = error;
      res.destroy();
    }
  });
  await new Promise((done, error) => {server.once('error', error); server.listen(0, '127.0.0.1', done);});
  const observer = new PythonActionObserver();
  const selected = profile(`http://127.0.0.1:${server.address().port}/v1`);
  const controller = new AbortController();
  const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(12_000)]);
  let session;
  try {
    const binding = await createBinding({profile: selected, limits: selected.limits, signal,
      deadline: performance.now() + 10_000, profileDigest: 'f'.repeat(64), onTrace() {}, pythonObserver: observer});
    const runtime = await pi.ModelRuntime.create({credentials: memoryCredentials(), modelsPath: null,
      refreshOnCreate: false, allowModelNetwork: false, signal});
    runtime.registerNativeProvider(binding.provider);
    await runtime.refresh({allowNetwork: false, signal});
    const resources = {getExtensions: () => ({extensions: [], errors: [], runtime: pi.createExtensionRuntime()}),
      getSkills: () => ({skills: [], diagnostics: []}), getPrompts: () => ({prompts: [], diagnostics: []}),
      getThemes: () => ({themes: [], diagnostics: []}), getAgentsFiles: () => ({agentsFiles: []}),
      getSystemPrompt: () => undefined, getSystemPromptSource: () => undefined,
      getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
      extendResources() {throw Error('resource discovery disabled');}, async reload() {}};
    const tool = {name: pythonToolName, label: 'Fixture marker', description: 'Returns an in-memory marker',
      parameters: pythonToolSchema, async execute(id, value) {
        assert.deepEqual(authorized, {id, code: value.code});
        authorized = undefined;
        dispatched++;
        return {content: [{type: 'text', text: 'marker'}], details: {}};
      }};
    ({session} = await pi.createAgentSession({cwd: workspace, agentDir: workspace, modelRuntime: runtime,
      model: binding.model, thinkingLevel: 'off', tools: [pythonToolName], customTools: [tool], resourceLoader: resources,
      sessionManager: pi.SessionManager.inMemory(workspace), settingsManager: pi.SettingsManager.inMemory({
        retry: {enabled: false, maxRetries: 0, provider: {maxRetries: 0}}, compaction: {enabled: false},
        cacheWarming: 'off',
      }, {projectTrusted: false})}));
    session.agent.toolExecution = 'sequential';
    assert.deepEqual(session.getActiveToolNames(), [pythonToolName]);
    controller.signal.addEventListener('abort', () => {void session.abort();}, {once: true});
    const nativeBefore = session.agent.beforeToolCall;
    session.agent.beforeToolCall = async (ctx, signal) => {
      committed.push(ctx.toolCall.arguments.code);
      try {
        const result = await nativeBefore?.(ctx, signal);
        if (result?.block) return result;
        authorized = {id: ctx.toolCall.id, ...observer.consume(ctx.assistantMessage, ctx.toolCall)};
      } catch (error) {
        refused.push(error.code);
        controller.abort();
        throw error;
      }
    };
    try {await session.prompt('Use only the test peer plan.', {expandPromptTemplates: false});}
    catch (error) {if (!controller.signal.aborted) throw error;}
    await session.waitForIdle();
    console.log(JSON.stringify({name, dispatched, refused, requests, committed: committed.length,
      providerAttempts: binding.httpAttempts(), committedMatches: committed.every(value => value === code)}));
    assert.equal(peerError, undefined);
    assert.equal(authorized, undefined);
    assert.equal(dispatched, expected.dispatched, name);
    assert.equal(refused.length, expected.refused, name);
    assert.deepEqual(committed, expected.committed, name);
    assert.equal(requests, expected.requests, name);
    assert.equal(binding.httpAttempts(), requests);
    if (expected.final) {
      const last = session.agent.state.messages.findLast(m => m.role === 'assistant');
      binding.assertNormal(last);
      assert.equal(binding.finalText(last), 'normal final');
    }
  } finally {
    session?.dispose();
    server.closeAllConnections();
    await new Promise(done => server.close(done));
  }
}

try {
  await proveCliRefusal();
  await prove('valid native commit', [{}, {final: true}], {requests: 2, committed: [code], dispatched: 1, refused: 0, final: true});
  await prove('Pi-repaired native action', [{original: args.replace('\\n', '\n')}], {requests: 1, committed: [code], dispatched: 0, refused: 1});
  await prove('duplicate code key', [{original: `{"code":"wrong",${args.slice(1)}`}], {requests: 1, committed: [code], dispatched: 0, refused: 1});
  await prove('changed native ID', [{changedId: true}], {requests: 1, committed: [code], dispatched: 0, refused: 1});
  await prove('missing native finish', [{missingFinish: true}], {requests: 1, committed: [], dispatched: 0, refused: 0});
  await prove('repeated native ID', [{}, {}], {requests: 2, committed: [code, code], dispatched: 1, refused: 1});
} finally {
  await rm(workspace, {recursive: true, force: true});
}
