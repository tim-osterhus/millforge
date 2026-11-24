/** Admission and finite ownership for the optional Python bridge. */
import {spawn, type ChildProcessByStdio} from 'node:child_process';
import {createHash} from 'node:crypto';
import {createReadStream} from 'node:fs';
import {access, constants, lstat, realpath} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import type {Readable, Writable} from 'node:stream';
import type {AssistantMessage} from '@earendil-works/pi-ai';
import {
  type PythonCapture, pythonArgumentsLimit, pythonIdentity, pythonLockSha256, pythonObservationLimit,
  pythonToolName, parsePythonArguments, validatePythonArguments,
} from './contracts-v5.js';
import {
  absolute, bytes, canonical, choice, currentRuntimeGuards, digest, exact, id, object,
  parseJson, refuse, statuses, text,
} from './contracts.js';
import {hash, regularFile} from './profile.js';

/** One observer per invocation. Only a confirmed native turn can authorize its one cell. */
export class PythonActionObserver {
  private turn = this.emptyTurn();
  private permit?: {id: string; code: string};
  private used = new Set<string>();

  private emptyTurn(): {original: string; size: number; id: string; name: string; finish: string; invalid: boolean} {
    return {original: '', size: 0, id: '', name: '', finish: '', invalid: false};
  }

  begin(): void {
    this.turn = this.emptyTurn();
    delete this.permit;
  }

  observe(value: unknown): void {
    if (this.turn.invalid) return;
    try {
      this.observeChunk(value);
    } catch {
      this.turn.invalid = true;
      this.turn.original = '';
    }
  }

  private observeChunk(value: unknown): void {
    const {choices} = object(value);
    if (!Array.isArray(choices) || choices.length > 1) refuse('ambiguous native tool choices');
    if (!choices.length) return;
    const choice = object(choices[0]);
    if (choice.index !== 0 || this.turn.finish) refuse('ambiguous native tool finish');
    const delta = object(choice.delta);
    if (delta.function_call !== undefined && delta.function_call !== null) refuse('legacy functions are not admitted');
    if (delta.tool_calls !== undefined && delta.tool_calls !== null) {
      if (!Array.isArray(delta.tool_calls) || delta.tool_calls.length !== 1) refuse('expected one native action');
      const call = object(delta.tool_calls[0]);
      if (call.index !== 0 || call.custom !== undefined || (call.type !== undefined && call.type !== 'function')) {
        refuse('unexpected native action shape');
      }
      if (call.id !== undefined) {
        const next = id(call.id);
        if (this.turn.id && this.turn.id !== next) refuse('native call ID changed');
        this.turn.id = next;
      }
      if (call.function !== undefined) {
        const fn = object(call.function);
        if (fn.name !== undefined) {
          if (fn.name !== pythonToolName) refuse('unexpected native tool name');
          this.turn.name = fn.name;
        }
        if (fn.arguments !== undefined) {
          const fragment = fn.arguments;
          if (typeof fragment !== 'string') refuse('malformed native argument delta');
          // JSON framing can split a surrogate pair across adjacent argument strings.
          const pair = /[\uD800-\uDBFF]$/.test(this.turn.original) && /^[\uDC00-\uDFFF]/.test(fragment);
          const size = this.turn.size + bytes(fragment) - (pair ? 2 : 0);
          if (size > pythonArgumentsLimit) refuse('original native arguments exceed the ceiling');
          this.turn.size = size;
          this.turn.original += fragment;
        }
      }
    }
    if (choice.finish_reason !== null && choice.finish_reason !== undefined) {
      if (typeof choice.finish_reason !== 'string' || !choice.finish_reason) refuse('malformed native finish');
      this.turn.finish = choice.finish_reason;
    }
  }

  complete(message: AssistantMessage): void {
    const turn = this.turn;
    this.begin();
    if (turn.invalid || turn.name !== pythonToolName || turn.finish !== 'tool_calls' ||
        message.stopReason !== 'toolUse' || message.rawStopReason !== 'tool_calls') return;
    const calls = message.content.filter(item => item.type === 'toolCall');
    const call = calls[0];
    if (calls.length !== 1 || !call || !turn.id || call.id !== turn.id || call.name !== turn.name) return;
    try {
      const original = parsePythonArguments(turn.original);
      if (canonical(original) === canonical(validatePythonArguments(call.arguments))) {
        this.permit = {id: call.id, code: original.code};
      }
    } catch {
      // Pi may normalize malformed arguments. A safe normalized value alone grants no permit.
    }
  }

  consume(message: AssistantMessage, call: {id: string; name: string; arguments: unknown}): {code: string} {
    const permit = this.permit;
    delete this.permit;
    const calls = message.content.filter(item => item.type === 'toolCall');
    const committed = calls[0];
    if (!permit || this.used.has(call.id) || call.id !== permit.id || call.name !== pythonToolName ||
        message.stopReason !== 'toolUse' || message.rawStopReason !== 'tool_calls' ||
        calls.length !== 1 || committed?.id !== call.id || committed.name !== call.name ||
        validatePythonArguments(committed.arguments).code !== permit.code ||
        validatePythonArguments(call.arguments).code !== permit.code) {
      refuse('original native Python action was not authorized', 'INVALID_FINISH');
    }
    this.used.add(call.id);
    return {code: permit.code};
  }
}

export interface PythonAdmission {
  manifest: string;
  interpreter: string;
  bridge: string;
  collector: string;
  capture: PythonCapture;
}

async function requireProtected(file: string, uid: number): Promise<void> {
  if (resolve(file) !== file || await realpath(file) !== file) refuse('noncanonical Python admission path');
  let filename = file;
  while (true) {
    const stat = await lstat(filename);
    if (stat.isSymbolicLink() || (filename === file ? !stat.isFile() : !stat.isDirectory()) || stat.uid === uid) {
      refuse('Python admission path is mutable by the run actor');
    }
    try {
      await access(filename, constants.W_OK);
      refuse('Python admission path is writable by the run actor');
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== 'EACCES' && code !== 'EROFS') throw error;
    }
    const parent = dirname(filename);
    if (parent === filename) return;
    filename = parent;
  }
}

export async function loadPythonAdmission(manifest: string | undefined): Promise<PythonAdmission> {
  if (!manifest) refuse('Python R1 requires an operator-admitted environment manifest', 'UNSUPPORTED_CAPABILITY');
  const uid = process.getuid?.();
  if (process.platform !== 'linux' || process.arch !== 'x64' || !uid || uid === 1000 || uid !== process.geteuid?.()) {
    refuse('Python R1 requires the selected restricted Linux x86-64 run actor', 'UNSUPPORTED_CAPABILITY');
  }
  const header = (process.report.getReport() as {header?: {glibcVersionRuntime?: string}}).header;
  const [major, minor] = (header?.glibcVersionRuntime ?? '').split('.').map(Number);
  if (!major || minor === undefined || !Number.isInteger(minor) || major < 2 || major === 2 && minor < 26) {
    refuse('Python R1 requires glibc 2.26 or later', 'UNSUPPORTED_CAPABILITY');
  }
  try {
    await requireProtected(absolute(manifest), uid);
    const raw = await regularFile(manifest, pythonArgumentsLimit);
    const fields = exact(parseJson(raw), ['schema', 'interpreter', 'bridge', 'collector', 'environment_sha256', 'lock_sha256']);
    if (fields.schema !== 'millforge-python-admission/v1' || fields.lock_sha256 !== pythonLockSha256) {
      refuse('Python admission schema or dependency lock changed');
    }
    const capture: PythonCapture = {...pythonIdentity, admission_sha256: hash(raw),
      environment_sha256: digest(fields.environment_sha256), lock_sha256: pythonLockSha256,
      interpreter_sha256: '', bridge_sha256: '', collector_sha256: ''};
    const paths = {interpreter: '', bridge: '', collector: ''};
    for (const role of ['interpreter', 'bridge', 'collector'] as const) {
      const identity = exact(fields[role], ['path', 'sha256']);
      const file = absolute(identity.path);
      await requireProtected(file, uid);
      const hasher = createHash('sha256');
      for await (const data of createReadStream(file)) hasher.update(data);
      const actual = hasher.digest('hex');
      if (actual !== digest(identity.sha256)) refuse('Python admission file changed');
      capture[`${role}_sha256`] = actual;
      paths[role] = file;
    }
    await access(paths.interpreter, constants.X_OK);
    await requireProtected(resolve(dirname(paths.interpreter), '..', 'pyvenv.cfg'), uid);
    return {manifest, ...paths, capture};
  } catch {
    return refuse('Python R1 admission files are unavailable, mutable or changed', 'UNSUPPORTED_CAPABILITY');
  }
}

const maxFrame = currentRuntimeGuards.encoded_record_bytes;
type Dispatch = 'not_sent' | 'sent' | 'unknown';
type BridgeProblem = 'CANCELLED' | 'TIMEOUT' | 'OUTPUT_LIMIT' | 'TOOL_ERROR';
interface CellResult {
  status: typeof statuses[number];
  text: string;
  truncated: boolean;
  dispatch: Dispatch;
  cell_settled: boolean;
  problem: BridgeProblem | null;
}
interface PendingCell {
  id: string;
  limit: number;
  dispatch: Dispatch;
  reply: (value: CellResult) => void;
}

/** Decodes the fixed-size header before allocating payload space. */
class BridgeFrames {
  private header = Buffer.alloc(4);
  private frame?: Buffer;
  private at = 0;

  push(chunk: Buffer, receive: (value: unknown) => void): void {
    let start = 0;
    while (start < chunk.length) {
      const target = this.frame ?? this.header;
      const count = Math.min(target.length - this.at, chunk.length - start);
      chunk.copy(target, this.at, start, start + count);
      this.at += count;
      start += count;
      if (this.at !== target.length) continue;
      this.at = 0;
      if (!this.frame) {
        const length = this.header.readUInt32BE();
        if (!length || length > maxFrame) refuse('Python bridge frame exceeds byte limit');
        this.frame = Buffer.allocUnsafe(length);
      } else {
        delete this.frame;
        receive(parseJson(target));
      }
    }
  }
}

const failedCell = (problem: BridgeProblem, dispatch: Dispatch): CellResult => ({
  status: problem === 'CANCELLED' ? 'cancelled' : problem === 'TIMEOUT' ? 'timeout' : 'failed',
  text: '', truncated: false, dispatch, cell_settled: false, problem,
});

export class PythonBridge {
  private child?: ChildProcessByStdio<Writable, Readable, null>;
  private joined?: Promise<boolean>;
  private cleanup?: Promise<boolean>;
  private pending?: PendingCell;
  private closed = false;

  constructor(private readonly admission: PythonAdmission, private readonly workspace: string,
              private readonly scratch: string, private readonly signal: AbortSignal) {}

  async run(callId: string, code: string, limit: number, deadline: number): Promise<CellResult> {
    const stopped = (dispatch: Dispatch): CellResult => failedCell(this.signal.aborted ? 'CANCELLED' : 'TIMEOUT', dispatch);
    if (this.signal.aborted || performance.now() >= deadline) return stopped('not_sent');
    if (this.closed || this.pending) return failedCell('TOOL_ERROR', 'not_sent');
    if (!this.child) {
      try {
        const verified = await loadPythonAdmission(this.admission.manifest);
        if (verified.capture.admission_sha256 !== this.admission.capture.admission_sha256) {
          return failedCell('TOOL_ERROR', 'not_sent');
        }
        if (this.closed || this.signal.aborted || performance.now() >= deadline) return stopped('not_sent');
        this.start();
      } catch {
        return failedCell('TOOL_ERROR', 'not_sent');
      }
    }
    const child = this.child!;
    const remaining = Math.ceil(deadline - performance.now());
    if (remaining <= 0 || this.signal.aborted) return stopped('not_sent');
    const body = Buffer.from(JSON.stringify({call_id: callId, code, limit, remaining_ms: remaining}));
    if (body.length > maxFrame) return failedCell('TOOL_ERROR', 'not_sent');
    const header = Buffer.alloc(4);
    header.writeUInt32BE(body.length);
    const abort = () => this.interrupt('CANCELLED');
    const timer = setTimeout(() => this.interrupt('TIMEOUT'), remaining);
    this.signal.addEventListener('abort', abort, {once: true});
    try {
      return await new Promise<CellResult>(reply => {
        this.pending = {id: callId, limit, dispatch: 'unknown', reply};
        child.stdin.write(Buffer.concat([header, body]), error => {
          if (error) this.interrupt('TOOL_ERROR');
        });
      });
    } finally {
      clearTimeout(timer);
      this.signal.removeEventListener('abort', abort);
    }
  }

  private start(): void {
    const paths = this.admission;
    const child = spawn(paths.interpreter, ['-I', '-B', paths.bridge, paths.collector, this.scratch], {
      cwd: this.workspace,
      env: {HOME: this.scratch, XDG_CONFIG_HOME: this.scratch, XDG_DATA_HOME: this.scratch,
        XDG_CACHE_HOME: this.scratch, JUPYTER_CONFIG_DIR: this.scratch, JUPYTER_RUNTIME_DIR: this.scratch,
        IPYTHONDIR: this.scratch, TMPDIR: this.scratch, LANG: 'C.UTF-8', PATH: '/usr/bin:/bin'},
      stdio: ['pipe', 'pipe', 'ignore'],
    });
    this.child = child;
    const frames = new BridgeFrames();
    child.stdout.on('data', (chunk: Buffer) => {
      if (this.closed) return;
      try { frames.push(chunk, value => this.receive(value)); } catch { this.interrupt('TOOL_ERROR'); }
    });
    child.on('error', () => this.interrupt('TOOL_ERROR'));
    child.stdin.on('error', () => this.interrupt('TOOL_ERROR'));
    this.joined = new Promise(done => child.once('close', (code, signal) => {
      this.failPending('TOOL_ERROR');
      done(code === 0 && signal === null);
    }));
  }

  private receive(value: unknown): void {
    const pending = this.pending;
    if (!pending) refuse('orphan Python bridge response');
    const message = object(value);
    if (message.call_id !== pending.id) refuse('Python bridge response ID mismatch');
    if (message.type === 'dispatch') {
      exact(message, ['type', 'call_id', 'dispatch']);
      if (message.dispatch !== 'sent' || pending.dispatch !== 'unknown') refuse('invalid Python bridge dispatch');
      pending.dispatch = 'sent';
      return;
    }
    if (message.type !== 'end') refuse('unknown Python bridge frame');
    exact(message, ['type', 'call_id', 'status', 'text', 'truncated', 'dispatch', 'cell_settled', 'problem']);
    const status = choice(message.status, statuses);
    const output = text(message.text, Math.min(pending.limit, pythonObservationLimit), 'Python result');
    const dispatch = choice(message.dispatch, ['not_sent', 'sent', 'unknown']);
    const problem = message.problem === null ? null : choice(message.problem, ['CANCELLED', 'TIMEOUT', 'OUTPUT_LIMIT', 'TOOL_ERROR']);
    if (typeof message.cell_settled !== 'boolean' || typeof message.truncated !== 'boolean' ||
        (dispatch === 'sent') !== (pending.dispatch === 'sent') ||
        message.cell_settled !== (problem === null) || message.cell_settled && dispatch !== 'sent' ||
        (status === 'completed' || status === 'failed' && problem === null) !== message.cell_settled ||
        problem !== null && (status !== failedCell(problem, dispatch).status || output || message.truncated)) {
      refuse('inconsistent Python bridge settlement');
    }
    delete this.pending;
    pending.reply({status, text: output, dispatch, truncated: message.truncated, cell_settled: message.cell_settled, problem});
  }

  private failPending(problem: BridgeProblem): void {
    const pending = this.pending;
    delete this.pending;
    pending?.reply(failedCell(problem, pending.dispatch));
  }

  private interrupt(problem: BridgeProblem): void {
    if (this.pending) this.child?.kill('SIGUSR1');
    this.failPending(problem);
    void this.close();
  }

  close(): Promise<boolean> {
    return this.cleanup ??= (async () => {
      this.closed = true;
      if (this.pending) this.child?.kill('SIGUSR1');
      this.failPending('CANCELLED');
      this.child?.stdin.end();
      if (!this.joined) return true;
      let timer: NodeJS.Timeout | undefined;
      try {
        return await Promise.race([this.joined, new Promise<boolean>(done => {timer = setTimeout(() => done(false), 1700);})]);
      } finally {
        if (timer) clearTimeout(timer);
      }
    })();
  }
}
