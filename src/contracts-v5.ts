/** Optional finite Python profile and offline record family. */
import {createHash} from 'node:crypto';
import {
  type Obj, type Request, type Result, type Trace,
  bytes, canonical, choice, digest, exact, list, object, parseJson, refuse, text,
  validateResult, validateTrace,
} from './contracts.js';
import {type CaptureV3, type ProfileV2, validateCaptureV3, validateProfileV2} from './contracts-v2.js';
import {millforgeVersion} from './versions.js';

export const pythonToolName = 'python_exec';
export const pythonToolSchema = {
  type: 'object',
  properties: {code: {type: 'string'}},
  required: ['code'],
  additionalProperties: false,
};
export const pythonArgumentsLimit = 16_384;
export const pythonObservationLimit = 51_200;
export const pythonLockSha256 = '97164d2432cf902dfa7a6b3fb6394d5729ca2cb1d9ddf204b59e12f5b9fc3750';
export const pythonIdentity = {
  action_protocol: 'pi-native-tool/v1',
  platform: 'linux-x64',
  python_version: '3.12.14',
} as const;
const pythonDigestFields = [
  'admission_sha256', 'environment_sha256', 'lock_sha256', 'interpreter_sha256',
  'bridge_sha256', 'collector_sha256',
] as const;

export type PythonCapture = typeof pythonIdentity & Record<typeof pythonDigestFields[number], string>;
export interface ProfileV4 extends Omit<ProfileV2, 'schema' | 'mode'> {
  schema: 'millforge-profile/v4';
  mode: 'agent';
}
export interface CaptureV5 extends Omit<CaptureV3, 'schema' | 'python'> {
  schema: 'millforge-capture/v5';
  python: PythonCapture;
}
export type ResultV5 = Omit<Result, 'schema'> & {schema: 'millforge-result/v5'};
export type TraceV5 = Omit<Trace, 'schema'> & {schema: 'millforge-trace/v5'};

function nativeProfile(profile: ProfileV4): ProfileV2 {
  return {...profile, schema: 'millforge-profile/v2', tools: [], capabilities: {extensions: [], packages: []}};
}

function nativeCapture(capture: CaptureV5): CaptureV3 {
  const {python: _python, ...common} = capture;
  return {...common, schema: 'millforge-capture/v3', tool_names: [], tool_schemas: []};
}

export function validateProfileV4(value: unknown): ProfileV4 {
  const p = object(value);
  if (p.schema !== 'millforge-profile/v4') refuse('unsupported schema version', 'UNSUPPORTED_VERSION');
  const capabilities = exact(p.capabilities, ['extensions', 'packages']);
  if (canonical(capabilities) !== canonical({extensions: ['python-exec/v1'], packages: []})) {
    refuse('unsupported v4 capability tuple', 'UNSUPPORTED_CAPABILITY');
  }
  if (p.mode !== 'agent' || !Array.isArray(p.tools) || p.tools.length !== 1 || p.tools[0] !== pythonToolName) {
    refuse('Python R1 requires agent mode and exactly python_exec', 'UNSUPPORTED_CAPABILITY');
  }
  const profile = p as unknown as ProfileV4;
  const model = validateProfileV2(nativeProfile(profile)).model;
  const port = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/v1$/.exec(model.base_url)?.[1];
  if (model.api !== 'openai-completions' || model.auth.kind !== 'none' || model.metadata.source !== 'operator' ||
      port === undefined || Number(port) > 65535) {
    refuse('Python R1 requires the selected keyless loopback route with operator metadata', 'UNSUPPORTED_BINDING');
  }
  return profile;
}

export function validatePythonArguments(value: unknown): {code: string} {
  const args = exact(value, ['code'], [], 'Python arguments');
  const code = text(args.code, pythonArgumentsLimit, 'Python source', false);
  if (!code.trim() || code.includes('\0') || bytes(canonical({code})) > pythonArgumentsLimit) {
    refuse('Python source must be nonblank, NUL-free and within the argument limit');
  }
  return {code};
}

export function parsePythonArguments(original: string): {code: string} {
  text(original, pythonArgumentsLimit, 'original Python arguments', false);
  return validatePythonArguments(parseJson(original));
}

export function validateCaptureV5(value: unknown, profile: ProfileV4, request: Request, profileDigest: string): CaptureV5 {
  if (object(value).schema !== 'millforge-capture/v5') refuse('record/profile family mismatch', 'UNSUPPORTED_VERSION');
  const capture = exact(value, [
    'schema', 'request_id', 'profile_digest', 'limits', 'cwd', 'resources', 'system_prompt', 'tool_names',
    'tool_schemas', 'binding', 'runtime_guards', 'versions', 'external_tools', 'python',
  ]);
  if (canonical(capture.tool_names!) !== canonical([pythonToolName])) {
    refuse('Python capture has the wrong active tool set');
  }
  list(capture.external_tools, 0);
  const schemas = list(capture.tool_schemas, 1);
  const schema = exact(schemas[0], ['name', 'sha256']);
  const schemaHash = createHash('sha256').update(canonical(pythonToolSchema)).digest('hex');
  if (schema.name !== pythonToolName || schema.sha256 !== schemaHash) refuse('Python tool schema mismatch');
  const python = exact(capture.python, [...Object.keys(pythonIdentity), ...pythonDigestFields]);
  for (const [key, expected] of Object.entries(pythonIdentity)) {
    if (python[key] !== expected) refuse('Python runtime identity mismatch');
  }
  for (const key of pythonDigestFields) digest(python[key]);
  if (python.lock_sha256 !== pythonLockSha256) refuse('Python dependency lock mismatch');
  const versions = object(capture.versions);
  if (versions.millforge !== millforgeVersion || versions.platform !== 'linux') refuse('unsupported Python capture version or platform', 'UNSUPPORTED_VERSION');
  const candidate = capture as unknown as CaptureV5;
  validateCaptureV3(nativeCapture(candidate), nativeProfile(profile), request, profileDigest);
  return candidate;
}

export function validateTraceV5(value: unknown, profile: ProfileV4, request: Request, capture: CaptureV5): TraceV5 {
  const trace = exact(value, ['schema', 'request_id', 'sequence', 'elapsed_ms', 'kind', 'payload']);
  if (trace.schema !== 'millforge-trace/v5') refuse('record/profile family mismatch', 'UNSUPPORTED_VERSION');
  const native = {...nativeProfile(profile), tools: [pythonToolName]};
  const payload = object(trace.payload);
  let common: Obj = payload;
  if (trace.kind === 'tool_start') validatePythonArguments(payload.arguments);
  if (trace.kind === 'tool_end') {
    exact(payload, ['call_id', 'status', 'text', 'truncated', 'dispatch', 'cell_settled']);
    text(payload.text, Math.min(pythonObservationLimit, profile.limits.tool_output_bytes), 'Python observation');
    const dispatch = choice(payload.dispatch, ['not_sent', 'sent', 'unknown']);
    if (typeof payload.cell_settled !== 'boolean' || payload.cell_settled && dispatch !== 'sent' ||
        payload.status === 'completed' && !payload.cell_settled) {
      refuse('Python cell settlement requires confirmed dispatch and correlated completion');
    }
    const {dispatch: _dispatch, cell_settled: _settled, ...shared} = payload;
    common = shared;
  }
  if (trace.kind === 'model_end' && payload.stop === 'tool' && payload.native_status !== 'tool_calls') {
    refuse('Python tool turn requires a native tool_calls finish');
  }
  validateTrace({...trace, schema: 'millforge-trace/v3', payload: common}, native, request, nativeCapture(capture));
  return trace as unknown as TraceV5;
}

export function validateResultV5(value: unknown, profile: ProfileV4, request: Request, capture: CaptureV5): ResultV5 {
  const result = object(value);
  if (result.schema !== 'millforge-result/v5') refuse('record/profile family mismatch', 'UNSUPPORTED_VERSION');
  validateResult({...result, schema: 'millforge-result/v3'}, nativeProfile(profile), request, nativeCapture(capture));
  return result as unknown as ResultV5;
}
