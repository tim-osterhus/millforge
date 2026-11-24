import {millforgeVersion,piVersion} from './versions.js';
import {validateProfileV3,validateCaptureV4,validateTraceV4,validateResultV4,type ProfileV3,type CaptureV4} from './contracts-v4.js';
export {type ProfileV3,type CaptureV4,type TraceV4,type ResultV4,codemodeConstants,codemodeLoader,codemodeOptions} from './contracts-v4.js';
import {validateProfileV2,validateCaptureV3,validateObservationV3,validateUsageV3,validateModelEndV3,validateLimitsV2,type ProfileV2} from './contracts-v2.js';
export {type ProfileV2,type ThinkingLevel,type CaptureV3,type RuntimeGuards,type UsageV3,currentRuntimeGuards} from './contracts-v2.js';
import {validateProfileV4,validateCaptureV5,validateTraceV5,validateResultV5,type ProfileV4,type CaptureV5,type PythonCapture} from './contracts-v5.js';
export {type ProfileV4,type CaptureV5,type TraceV5,type ResultV5} from './contracts-v5.js';
/** Pure versioned data contracts. This module never loads Pi, credentials or host resources. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Obj = { [key: string]: Json };
export type Mode = 'generate' | 'agent';
export const codes = ['INVALID_INPUT','UNSUPPORTED_VERSION','UNSUPPORTED_CAPABILITY','UNSUPPORTED_BINDING','PROFILE_CHANGED','AUTH_UNAVAILABLE','PROVIDER_ERROR','INVALID_FINISH','INVALID_TERMINAL','TOOL_ERROR','CONTEXT_LIMIT','OUTPUT_LIMIT','TRACE_LIMIT','LIMIT_EXHAUSTED','CANCELLED','TIMEOUT','CLOSURE_UNCERTAIN','IO_ERROR','INCOMPATIBLE_ROUTE'] as const;
export type Code = typeof codes[number] | 'OPTIMIZER_UNAVAILABLE';
export class MillforgeError extends Error {
  constructor(public readonly code: Code, message: string) { super(message); this.name = 'MillforgeError'; }
}
export function refuse(message: string, code: Code = 'INVALID_INPUT'): never { throw new MillforgeError(code, message); }
export const bytes = (value: string): number => Buffer.byteLength(value, 'utf8');
export function object(value: unknown, where = 'object'): Obj {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) refuse(`${where} must be an object`);
  return value as Obj;
}
export function exact(value: unknown, keys: readonly string[], optional: readonly string[] = [], where = 'object'): Obj {
  const obj = object(value, where);
  if (keys.some(k => !Object.hasOwn(obj,k)) || Object.keys(obj).some(k => !keys.includes(k) && !optional.includes(k))) refuse(`${where} has missing or unknown fields`);
  return obj;
}
export function text(value: unknown, max: number, where = 'text', empty = true): string {
  if (typeof value !== 'string' || (!empty && !value.length) || bytes(value) > max || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)) refuse(`${where} must be bounded UTF-8 text`);
  return value;
}
export function integer(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER, where = 'integer'): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) refuse(`${where} is outside its integer bounds`);
  return value;
}
export function number(value: unknown, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) refuse('number outside bounds');
  return value;
}
export function choice<T extends string>(value: unknown, allowed: readonly T[], where = 'value'): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) refuse(`${where} is unsupported`);
  return value as T;
}
export function id(value: unknown): string { const v = text(value,64,'ID',false); if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(v)) refuse('invalid ID'); return v; }
export function digest(value: unknown): string { if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) refuse('invalid SHA-256 digest'); return value; }
export function relative(value: unknown): string {
  const v = text(value,4096,'relative path',false);
  if (v.includes('\\') || v.includes('\0') || v.startsWith('/') || /^[A-Za-z]:/.test(v) || v.split('/').some(p => !p || p === '.' || p === '..')) refuse('unsafe relative path');
  return v;
}
export function absolute(value: unknown): string { const v = text(value,4096,'absolute path',false); if (!v.startsWith('/') || v.includes('\0')) refuse('absolute path required'); return v; }
export function list(value: unknown, max: number, where = 'list'): Json[] { if (!Array.isArray(value) || value.length > max) refuse(`${where} must be a bounded list`); return value as Json[]; }
export function uniqueStrings(value: unknown, max: number, validate: (v: unknown) => string = id): string[] {
  const values = list(value,max).map(validate); if (new Set(values).size !== values.length) refuse('duplicate list entry'); return values;
}
function bool(value: unknown): boolean { if (typeof value !== 'boolean') refuse('boolean required'); return value; }
function version(value: Obj, schema: string): void { if (value.schema !== schema) refuse('unsupported schema version','UNSUPPORTED_VERSION'); }
export function canonical(value: Json): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort().map(k => JSON.stringify(k)+':'+canonical(value[k]!)).join(',') + '}';
}
/** Parse strict JSON without allowing JSON.parse's duplicate-key overwrite behavior. */
export function parseJson(input: Uint8Array | string): Json {
  let source: string;
  try { source = typeof input === 'string' ? input : new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(input); } catch { return refuse('invalid UTF-8 JSON'); }
  let at = 0;
  const ws = (): void => { while (/[\x20\t\r\n]/.test(source[at] ?? '\x00')) at++; };
  const string = (): string => {
    const start=at++; while (at < source.length) { const ch=source[at++]; if (ch === '\\') { at++; continue; } if (ch === '"') { try { return text(JSON.parse(source.slice(start,at)),Number.MAX_SAFE_INTEGER); } catch { return refuse('invalid JSON string'); } } }
    return refuse('unterminated JSON string');
  };
  const value = (depth: number): Json => {
    if (depth > 64) refuse('JSON nesting exceeds 64'); ws(); const ch=source[at];
    if (ch === '"') return string();
    if (ch === '{') {
      at++; ws(); const out: Obj=Object.create(null) as Obj;
      if (source[at] === '}') { at++; return out; }
      while (true) { ws(); if (source[at] !== '"') refuse('JSON object key required'); const key=string(); if (Object.hasOwn(out,key)) refuse('duplicate JSON key'); ws(); if (source[at++] !== ':') refuse('JSON colon required'); out[key]=value(depth+1); ws(); const separator=source[at++]; if (separator === '}') return out; if (separator !== ',') refuse('invalid JSON object'); }
    }
    if (ch === '[') { at++; ws(); const out: Json[]=[]; if (source[at] === ']') { at++; return out; } while (true) { out.push(value(depth+1)); ws(); const separator=source[at++]; if (separator === ']') return out; if (separator !== ',') refuse('invalid JSON list'); } }
    for (const [token,result] of [['true',true],['false',false],['null',null]] as const) if (source.startsWith(token,at)) { at+=token.length; return result; }
    const match=/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(source.slice(at));
    if (!match) return refuse('invalid JSON value'); at+=match[0].length; const n=Number(match[0]); if (!Number.isFinite(n)) refuse('non-finite JSON number'); return n;
  };
  const result=value(0); ws(); if (at !== source.length) refuse('trailing JSON data'); return result;
}
export const limitKeys = ['model_requests','tool_calls','elapsed_seconds','model_timeout_seconds','tool_timeout_seconds','input_bytes','context_bytes','output_bytes','tool_output_bytes','trace_bytes'] as const;
export type Limits = { [K in typeof limitKeys[number]]: number };
export const tools = ['read','bash','edit','write','grep','find','ls'] as const;
export type Asset = { path: string; sha256: string; kind: 'context' | 'skill' };
export interface ProfileV1 {
  schema: 'millforge-profile/v1'; profile_id: string; revision: number; mode: Mode;
  runtime: { pi_version: '1.4.0'; binding_version: 1 };
  model: { api: 'openai-completions'|'anthropic-messages'; id: string; base_url: string; credential_env: string|null; context_tokens: number; max_output_tokens: number; compat: Obj };
  instructions: string; sampling: {temperature:number;top_p:number|null}; tools: string[]; assets: Asset[];
  capabilities: {extensions:Json[];packages:Json[]}; limits: Limits;
  terminal: {format:'text'|'json-object';required_keys:string[];outcome_key:string|null;outcomes:string[]}; description?: string;
}
export type Profile = ProfileV1 | ProfileV2 | ProfileV3 | ProfileV4;
export function validateLimits(value: unknown, mode: Mode, schema?:string): Limits {
  if(schema==='millforge-profile/v2'||schema==='millforge-profile/v3'||schema==='millforge-profile/v4')return validateLimitsV2(value,mode);
  const obj=exact(value,limitKeys,[],'limits');
  const max: Limits={model_requests:mode==='generate'?1:64,tool_calls:mode==='generate'?0:256,elapsed_seconds:1800,model_timeout_seconds:1800,tool_timeout_seconds:1800,input_bytes:1048576,context_bytes:2097152,output_bytes:262144,tool_output_bytes:262144,trace_bytes:8388608};
  for (const key of limitKeys) integer(obj[key],key==='tool_calls'?0:1,max[key],key);
  const result=obj as unknown as Limits;
  if (mode==='generate' && result.model_requests!==1) refuse('generate requires one model request');
  if (result.model_timeout_seconds>result.elapsed_seconds-5 || result.tool_timeout_seconds>result.elapsed_seconds-5) refuse('call timeout must reserve five seconds for cleanup');
  return result;
}
function endpoint(value: unknown): string {
  const v=text(value,4096,'base_url',false); if(/[\x00-\x20\x7f\\@]/.test(v))refuse('unsafe raw endpoint spelling'); let url: URL; try { url=new URL(v); } catch { return refuse('invalid explicit HTTP(S) URL'); }
  if (!['http:','https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || v.includes('?') || v.includes('#')) refuse('invalid explicit HTTP(S) URL');
  return v;
}
/** Only the pinned Anthropic beta resource's fixed query is admitted. Base URLs stay query-free. */
function requestEndpoint(value:unknown,api:string):string{
  const v=text(value,4096,'request_url',false);
  if(api==='anthropic-messages'){
    const query='?beta=true';if(!v.endsWith(query))refuse('expected fixed Anthropic beta query');
    endpoint(v.slice(0,-query.length));return v;
  }
  return endpoint(v);
}
function credential(value: unknown): string|null { if (value===null) return null; const v=text(value,256,'credential environment name',false); if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(v)) refuse('credential must be an environment variable reference'); return v; }
export function validateProfile(value: unknown): Profile {
  if(object(value).schema==='millforge-profile/v4')return validateProfileV4(value);
  if(object(value).schema==='millforge-profile/v3')return validateProfileV3(value);
  if(object(value).schema==='millforge-profile/v2')return validateProfileV2(value);
  const p=exact(value,['schema','profile_id','revision','mode','runtime','model','instructions','sampling','tools','assets','capabilities','limits','terminal'],['description'],'profile'); version(p,'millforge-profile/v1'); id(p.profile_id); integer(p.revision,1); const mode=choice(p.mode,['generate','agent']);
  const runtime=exact(p.runtime,['pi_version','binding_version']); if (runtime.pi_version!=='1.4.0' || runtime.binding_version!==1) refuse('unsupported runtime version','UNSUPPORTED_VERSION');
  const model=exact(p.model,['api','id','base_url','credential_env','context_tokens','max_output_tokens','compat']);
  if (typeof model.api!=='string' || !['openai-completions','anthropic-messages'].includes(model.api)) refuse('unsupported binding','UNSUPPORTED_BINDING');
  text(model.id,256,'model ID',false); endpoint(model.base_url); credential(model.credential_env); integer(model.context_tokens,1); integer(model.max_output_tokens,1);
  if (model.api==='openai-completions') { const c=exact(model.compat,['supportsDeveloperRole','supportsReasoningEffort','supportsFinishReason','maxTokensField']); bool(c.supportsDeveloperRole); bool(c.supportsReasoningEffort); if (c.supportsFinishReason!==true) refuse('native finish metadata required'); choice(c.maxTokensField,['max_tokens','max_completion_tokens']); } else exact(model.compat,[]);
  text(p.instructions,65536,'instructions'); const sampling=exact(p.sampling,['temperature','top_p']); number(sampling.temperature,0,1); if (sampling.top_p!==null && number(sampling.top_p,0,1)===0) refuse('top_p must be positive'); if (model.api==='anthropic-messages' && sampling.top_p!==null) refuse('Anthropic top_p must be null');
  const selected=uniqueStrings(p.tools,7,v=>choice(v,tools)); if (mode==='generate' && selected.length) refuse('generate cannot expose tools');
  const seen=new Set<string>(); for (const entry of list(p.assets,128)) { const a=exact(entry,['path','sha256','kind']); const path=relative(a.path); if (!/\.(md|txt|json)$/.test(path) || ['profile.json','.history','.millforge-write.lock'].includes(path.split('/')[0]!.toLowerCase()) || seen.has(path)) refuse('invalid or duplicate asset path'); seen.add(path); digest(a.sha256); choice(a.kind,['context','skill']); }
  const skillAssets=(p.assets as unknown as Asset[]).filter(a=>a.kind==='skill');const skillRoots=skillAssets.filter(a=>a.path.split('/').at(-1)==='SKILL.md').map(a=>a.path.includes('/')?a.path.slice(0,a.path.lastIndexOf('/')):'.');for(const a of skillAssets)if(!skillRoots.some(root=>root==='.'||a.path.startsWith(root+'/')))refuse('skill support file lacks declared SKILL.md');
  const caps=exact(p.capabilities,['extensions','packages']); if (list(caps.extensions,128).length || list(caps.packages,128).length) refuse('v1 capabilities must be empty','UNSUPPORTED_CAPABILITY'); validateLimits(p.limits,mode);
  const term=exact(p.terminal,['format','required_keys','outcome_key','outcomes']); const format=choice(term.format,['text','json-object']); const required=uniqueStrings(term.required_keys,16); const outcomes=uniqueStrings(term.outcomes,16);
  if (term.outcome_key!==null) { if (!required.includes(id(term.outcome_key)) || !outcomes.length) refuse('outcome key must be required with declared outcomes'); } else if (outcomes.length) refuse('outcomes require an outcome key');
  if (format==='text' && (required.length || term.outcome_key!==null)) refuse('text terminal cannot have object keys');
  if (p.description!==undefined) text(p.description,2048,'description'); return p as unknown as Profile;
}
export interface Request {
  schema:'millforge-request/v1';request_id:string;profile_id:string;profile_digest:string;expected_mode:Mode;task:string;context:string[];workspace:string|null;limits:Partial<Limits>;correlation:Record<string,string>;
}
export function validateRequest(value: unknown, profile?: Profile, profileDigest?: string): Request {
  const r=exact(value,['schema','request_id','profile_id','profile_digest','expected_mode','task','context','workspace','limits','correlation']); version(r,'millforge-request/v1'); id(r.request_id); id(r.profile_id); digest(r.profile_digest); const mode=choice(r.expected_mode,['generate','agent']); text(r.task,1048576,'task'); const context=list(r.context,1048576).map(v=>text(v,1048576,'context'));
  if (mode==='agent') absolute(r.workspace); else if (r.workspace!==null) refuse('generate workspace must be null');
  const overrides=object(r.limits); if (Object.keys(overrides).some(k=>!limitKeys.includes(k as typeof limitKeys[number]))) refuse('unknown limit'); for (const [key,v] of Object.entries(overrides)) integer(v,key==='tool_calls'?0:1);
  const correlation=object(r.correlation); if (Object.keys(correlation).length>16) refuse('too many correlation keys'); for (const [k,v] of Object.entries(correlation)) { id(k); text(v,256,'correlation'); }
  if (profile) {
    if (r.profile_id!==profile.profile_id || mode!==profile.mode || (profileDigest!==undefined && r.profile_digest!==profileDigest)) refuse('request/profile identity mismatch','PROFILE_CHANGED');
    const limits=tightenLimits(profile,overrides); if (bytes(String(r.task))+context.reduce((n,s)=>n+bytes(s),0)>limits.input_bytes) refuse('request input exceeds limit');
  }
  return r as unknown as Request;
}
export function tightenLimits(profile: Profile, value: unknown): Limits {
  const overrides=object(value); if (Object.keys(overrides).some(k=>!limitKeys.includes(k as typeof limitKeys[number]))) refuse('unknown request limit');
  for (const [k,v] of Object.entries(overrides)) if (integer(v,k==='tool_calls'?0:1)>profile.limits[k as keyof Limits]) refuse('request widens profile limits');
  return profile.schema!=='millforge-profile/v1'?validateLimitsV2({...profile.limits,...overrides},profile.mode):validateLimits({...profile.limits,...overrides},profile.mode);
}
export interface Resource extends Asset {size:number}
export interface Capture {
  schema:'millforge-capture/v1'|'millforge-capture/v2'|'millforge-capture/v3'|'millforge-capture/v4'|'millforge-capture/v5';request_id:string;profile_digest:string;limits:Limits;cwd:string|null;resources:Resource[];system_prompt:string;tool_names:string[];tool_schemas:{name:string;sha256:string}[];binding:Obj;credential_env?:string|null;runtime_guards?:Obj;declared_tools?:{name:string;description_sha256:string}[];callable_tool_names?:string[];capability?:Obj;python?:PythonCapture;versions:{millforge:string;pi:string;node:string;platform:string};external_tools:Obj[];
}
export const sourcePin='4a080252f06caf63a525d7873c5d94d3c23887e0';
export function validateCapture(value: unknown, profile: Profile, request: Request, profileDigest: string): Capture {
  if(profile.schema==='millforge-profile/v4')return validateCaptureV5(value,profile,request,profileDigest);
  if(object(value).schema==='millforge-capture/v5')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if(profile.schema==='millforge-profile/v3')return validateCaptureV4(value,profile,request,profileDigest);
  if(object(value).schema==='millforge-capture/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if(profile.schema==='millforge-profile/v2')return validateCaptureV3(value,profile,request,profileDigest);
  if(object(value).schema==='millforge-capture/v3')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const c=exact(value,['schema','request_id','profile_digest','limits','cwd','resources','system_prompt','tool_names','tool_schemas','binding','credential_env','versions','external_tools']); const v2=c.schema==='millforge-capture/v2';if(!v2)version(c,'millforge-capture/v1');
  if (c.request_id!==request.request_id || c.profile_digest!==profileDigest || c.cwd!==request.workspace || c.credential_env!==profile.model.credential_env) refuse('capture identity mismatch');
  if (canonical(object(c.limits))!==canonical(tightenLimits(profile,request.limits) as unknown as Obj)) refuse('capture limit mismatch');
  const resources=list(c.resources,128); if (resources.length!==profile.assets.length) refuse('capture resource mismatch');
  for (let i=0;i<resources.length;i++) { const r=exact(resources[i],['path','sha256','kind','size']); const a=profile.assets[i]!; if (r.path!==a.path || r.sha256!==a.sha256 || r.kind!==a.kind) refuse('capture resource mismatch'); integer(r.size,0,4194304); }
  text(c.system_prompt,2097152,'effective system prompt'); const names=uniqueStrings(c.tool_names,7,v=>choice(v,tools)); if (canonical(names)!==canonical(profile.tools)) refuse('captured tool order mismatch');
  const schemas=list(c.tool_schemas,7); if (schemas.length!==names.length) refuse('captured schema mismatch'); schemas.forEach((v,i)=>{const s=exact(v,['name','sha256']);if(s.name!==names[i])refuse('captured schema order mismatch');digest(s.sha256);});
  const b=exact(c.binding,v2?['api','version','provider_id','model_id','base_url','request_url','source_pin','compat','requested_options']:['api','version','provider_id','base_url','source_pin','compat','options']); if (b.api!==profile.model.api || b.version!==profile.runtime.binding_version || b.base_url!==profile.model.base_url || b.source_pin!==sourcePin) refuse('binding capture mismatch'); text(b.provider_id,256,'provider ID',false);
  if(v2){
    if(b.model_id!==profile.model.id)refuse('selected model mismatch');
    const base=new URL(endpoint(b.base_url)),target=new URL(requestEndpoint(b.request_url,profile.model.api));
    if(target.origin!==base.origin||(target.pathname!==base.pathname&&!target.pathname.startsWith(base.pathname.replace(/\/?$/,'/'))))refuse('selected request URL outside base endpoint');
    const compat=exact(b.compat,['authored_overrides','internal_resolution']);
    if(compat.internal_resolution!=='unknown'||canonical(object(compat.authored_overrides))!==canonical(profile.model.compat))refuse('authored compatibility or unknown marker mismatch');
  }else validateCompatCapture(b.compat,profile);
  const options=exact(v2?b.requested_options:b.options,['temperature','top_p','thinking','max_retries']); if (options.temperature!==profile.sampling.temperature || options.top_p!==profile.sampling.top_p || options.thinking!=='off' || options.max_retries!==0) refuse(v2?'requested binding options mismatch':'effective binding options mismatch');
  validateCaptureVersions(c.versions);
  const binaries=list(c.external_tools,2); const wanted=names.filter(n=>n==='find'||n==='grep').map(n=>n==='find'?'fd':'rg').sort(); const actual:string[]=[];
  for (const entry of binaries) { const e=exact(entry,['name','path','sha256','size','stat']);actual.push(choice(e.name,['fd','rg']));absolute(e.path);digest(e.sha256);integer(e.size);const st=exact(e.stat,['dev','ino','mode','mtime_ms']);integer(st.dev);integer(st.ino);integer(st.mode,0,0o177777);number(st.mtime_ms,0,Number.MAX_SAFE_INTEGER); }
  if (canonical(actual.sort())!==canonical(wanted)) refuse('external tool capture mismatch'); return c as unknown as Capture;
}
export function validateCaptureVersions(value: unknown): void {
  const versions = exact(value, ['millforge', 'pi', 'node', 'platform']);
  if ((versions.millforge !== '0.2.0' && versions.millforge !== millforgeVersion) || versions.pi !== piVersion) {
    refuse('unsupported captured version', 'UNSUPPORTED_VERSION');
  }
  text(versions.node, 128, 'Node version', false);
  text(versions.platform, 128, 'platform', false);
}
export function validateCompatCapture(value: unknown, profile: Profile,requireFinish=true): void {
  // Public Pi 1.4.0 compatibility declaration shapes, never provider defaults.
  const openBooleans=['supportsStore','supportsDeveloperRole','supportsReasoningEffort','supportsUsageInStreaming','supportsFinishReason','requiresToolResultName','requiresAssistantAfterToolResult','requiresThinkingAsText','requiresReasoningContentOnAssistantMessages','zaiToolStream','supportsThinkingTokenBudget','supportsOpenAIGrammarTools','supportsMidConvoSystemMessages','supportsMidConvoToolAdditions','supportsStrictMode','sendSessionAffinityHeaders','supportsLongCacheRetention'];
  const anthropicBooleans=['supportsEagerToolInputStreaming','supportsLongCacheRetention','sendSessionAffinityHeaders','supportsCacheControlOnTools','supportsTemperature','forceAdaptiveThinking','allowEmptySignature','supportsStrictTools','supportsMidConvoEffort','supportsMidConvoSystemMessages','supportsMidConvoToolChanges'];
  const open=profile.model.api==='openai-completions';
  const bools=open?openBooleans:anthropicBooleans;
  const extras=open?['maxTokensField','thinkingFormat','chatTemplateKwargs','chatTemplateArgs','openRouterRouting','vercelGatewayRouting','thinkingTokenBudgetField','cacheControlFormat','sessionAffinityFormat','vllmPriority']:['sessionAffinityFormat','allowedFallbackModels'];
  const c=exact(value,open?Object.keys(profile.model.compat):[],[...bools,...extras]);
  for(const k of bools)if(c[k]!==undefined)bool(c[k]);
  for(const [k,v]of Object.entries(profile.model.compat))if(canonical(c[k]!)!==canonical(v))refuse('authored compatibility mismatch');
  if(open&&requireFinish&&c.supportsFinishReason!==true)refuse('native finish metadata required');
  if(c.maxTokensField!==undefined)choice(c.maxTokensField,['max_tokens','max_completion_tokens']);
  if(c.thinkingFormat!==undefined)choice(c.thinkingFormat,['openai','openrouter','deepseek','together','baseten','zai','qwen','chat-template','qwen-chat-template','string-thinking','ant-ling']);
  if(c.thinkingTokenBudgetField!==undefined)choice(c.thinkingTokenBudgetField,['thinking_token_budget','thinking_budget','thinking_budget_tokens']);
  if(c.cacheControlFormat!==undefined)choice(c.cacheControlFormat,['anthropic']);
  if(c.sessionAffinityFormat!==undefined)choice(c.sessionAffinityFormat,open?['openai','openai-nosession','openrouter']:['openrouter']);
  if(c.vllmPriority!==undefined)number(c.vllmPriority,-Number.MAX_VALUE,Number.MAX_VALUE);
  for(const key of ['chatTemplateKwargs','chatTemplateArgs'])if(c[key]!==undefined){const values=object(c[key]);if(Object.keys(values).length>128)refuse('too many template compatibility keys');for(const [k,v]of Object.entries(values)){text(k,256);if(v!==null&&typeof v==='object'){const variable=exact(v,['$var'],['omitWhenOff']);choice(variable.$var,['thinking.enabled','thinking.effort','thinking.budget']);if(variable.omitWhenOff!==undefined)bool(variable.omitWhenOff);}else if(typeof v==='string')text(v,4096);else if(typeof v==='number')number(v,-Number.MAX_VALUE,Number.MAX_VALUE);}}
  if(c.openRouterRouting!==undefined){const r=exact(c.openRouterRouting,[],['allow_fallbacks','require_parameters','data_collection','zdr','enforce_distillable_text','order','only','ignore','quantizations','sort','max_price','preferred_min_throughput','preferred_max_latency']);for(const k of ['allow_fallbacks','require_parameters','zdr','enforce_distillable_text'])if(r[k]!==undefined)bool(r[k]);if(r.data_collection!==undefined)choice(r.data_collection,['deny','allow']);for(const k of ['order','only','ignore','quantizations'])if(r[k]!==undefined)list(r[k],128).forEach(v=>text(v,256));if(r.sort!==undefined){if(typeof r.sort==='string')text(r.sort,256);else{const sort=exact(r.sort,[],['by','partition']);if(sort.by!==undefined)text(sort.by,256);if(sort.partition!==undefined&&sort.partition!==null)text(sort.partition,256);}}if(r.max_price!==undefined){const prices=exact(r.max_price,[],['prompt','completion','image','audio','request']);for(const v of Object.values(prices))if(typeof v==='string')text(v,256);else number(v,-Number.MAX_VALUE,Number.MAX_VALUE);}for(const k of ['preferred_min_throughput','preferred_max_latency'])if(r[k]!==undefined){if(typeof r[k]==='number')number(r[k],-Number.MAX_VALUE,Number.MAX_VALUE);else{const quantiles=exact(r[k],[],['p50','p75','p90','p99']);for(const v of Object.values(quantiles))number(v,-Number.MAX_VALUE,Number.MAX_VALUE);}}}
  if(c.vercelGatewayRouting!==undefined){const routing=exact(c.vercelGatewayRouting,[],['only','order']);for(const v of Object.values(routing))list(v,128).forEach(item=>text(item,256));}
  if(c.allowedFallbackModels!==undefined)for(const v of list(c.allowedFallbackModels,128)){const model=exact(v,['provider','model','cost']);text(model.provider,256);text(model.model,256);const cost=exact(model.cost,['input','output','cacheRead','cacheWrite'],['tiers']);for(const k of ['input','output','cacheRead','cacheWrite'])number(cost[k],0,Number.MAX_VALUE);if(cost.tiers!==undefined)for(const tier of list(cost.tiers,128)){const t=exact(tier,['input','output','cacheRead','cacheWrite','inputTokensAbove']);for(const n of Object.values(t))number(n,0,Number.MAX_VALUE);}}
  if(bytes(canonical(c))>65536)refuse('compatibility capture exceeds bound');
}
export interface Usage {input_tokens:number|null;output_tokens:number|null;cache_read_tokens:number|null;cache_write_tokens:number|null}
export function validateUsage(value: unknown): void { if (value===null) return; const u=exact(value,['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens']);for(const v of Object.values(u))if(v!==null)integer(v); }
export const statuses=['completed','failed','cancelled','timeout'] as const;
export type Status=typeof statuses[number];
export interface Closure {worker:'closed'|'may_live';tools:'closed'|'may_live';writers:'closed'|'may_live';inference:'response_returned'|'disconnected_unknown';detached_effects:'none_observed'|'unknown'}
export function validateClosure(value: unknown, completed=false): Closure {
  const c=exact(value,['worker','tools','writers','inference','detached_effects']); for (const k of ['worker','tools','writers']) choice(c[k],['closed','may_live']);choice(c.inference,['response_returned','disconnected_unknown']);choice(c.detached_effects,['none_observed','unknown']);
  if (completed && (c.worker!=='closed'||c.tools!=='closed'||c.writers!=='closed'||c.detached_effects!=='none_observed'||c.inference!=='response_returned')) refuse('completed result requires closure'); return c as unknown as Closure;
}
export interface Artifact {path:string;size:number;sha256:string;media_type:string}
export interface Result {
  schema:'millforge-result/v1'|'millforge-result/v2'|'millforge-result/v3'|'millforge-result/v4'|'millforge-result/v5';request_id:string;profile_id:string;profile_digest:string;mode:Mode;status:Status;domain_outcome:string|null;output:Json;diagnostic:{code:typeof codes[number];message:string}|null;counts:Obj;timing:Obj;closure:Closure;artifacts:Artifact[];
}
export function validateTerminal(value: Json, profile: Profile): string|null {
  const t=profile.terminal;
  if (t.format==='text') { text(value,profile.limits.output_bytes,'terminal text',false); return null; }
  const out=object(value,'terminal output'); if (bytes(canonical(out))>profile.limits.output_bytes || t.required_keys.some(k=>!Object.hasOwn(out,k))) refuse('invalid terminal output','INVALID_TERMINAL');
  if (t.outcome_key!==null) return choice(out[t.outcome_key],t.outcomes,'domain outcome'); return null;
}
function timestamp(value: unknown): number { const v=text(value,64,'timestamp',false);if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(v)||!Number.isFinite(Date.parse(v)))refuse('RFC3339 UTC timestamp required');return Date.parse(v); }
export function validateResult(value: unknown, profile: Profile, request: Request, capture: Capture): Result {
  if(profile.schema==='millforge-profile/v4'){
    if(capture.schema!=='millforge-capture/v5')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
    return validateResultV5(value,profile,request,capture as CaptureV5);
  }
  if(capture.schema==='millforge-capture/v5'||object(value).schema==='millforge-result/v5')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if(profile.schema==='millforge-profile/v3'){if(capture.schema!=='millforge-capture/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');return validateResultV4(value,profile,request,capture as CaptureV4);}
  if(capture.schema==='millforge-capture/v4'||object(value).schema==='millforge-result/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if((profile.schema==='millforge-profile/v2')!==(capture.schema==='millforge-capture/v3'))refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const r=exact(value,['schema','request_id','profile_id','profile_digest','mode','status','domain_outcome','output','diagnostic','counts','timing','closure','artifacts']);version(r,capture.schema==='millforge-capture/v3'?'millforge-result/v3':capture.schema==='millforge-capture/v2'?'millforge-result/v2':'millforge-result/v1');
  if(r.request_id!==request.request_id||r.profile_id!==profile.profile_id||r.profile_digest!==request.profile_digest||r.mode!==profile.mode)refuse('result identity mismatch'); const status=choice(r.status,statuses);
  if(status==='completed'){if(r.diagnostic!==null)refuse('completed diagnostic must be null');if(validateTerminal(r.output!,{...profile,limits:capture.limits})!==r.domain_outcome)refuse('domain outcome mismatch');}
  else{if(r.output!==null||r.domain_outcome!==null)refuse('failed output must be null');const d=exact(r.diagnostic,['code','message']);choice(d.code,codes);text(d.message,2048,'diagnostic');if(status==='cancelled'&&d.code!=='CANCELLED'||status==='timeout'&&d.code!=='TIMEOUT')refuse('status diagnostic mismatch');}
  const counts=exact(r.counts,['model_requests','tool_calls','input_bytes','output_bytes','trace_bytes','usage','backend_requests','cost']);for(const k of ['model_requests','tool_calls','input_bytes','output_bytes','trace_bytes'])integer(counts[k],0,capture.limits[k as keyof Limits]);if(capture.schema==='millforge-capture/v3')validateUsageV3(counts.usage);else validateUsage(counts.usage);if(counts.backend_requests!==null){if(capture.schema!=='millforge-capture/v1')refuse('backend work is unknown');integer(counts.backend_requests);}
  if(counts.input_bytes!==bytes(request.task)+request.context.reduce((n,s)=>n+bytes(s),0))refuse('input count mismatch');if(counts.cost!==null){const c=exact(counts.cost,['amount','currency','source']);number(c.amount,0,Number.MAX_VALUE);text(c.currency,16,'currency',false);choice(c.source,['reported','estimated']);}
  if(profile.schema==='millforge-profile/v2'&&profile.model.auth.kind==='oauth'&&counts.cost!==null)refuse('OAuth monetary cost is unknown');
  const timing=exact(r.timing,['started_at','ended_at','elapsed_ms']);if(timestamp(timing.ended_at)<timestamp(timing.started_at))refuse('timing order invalid');number(timing.elapsed_ms,0,Number.MAX_SAFE_INTEGER);validateClosure(r.closure,status==='completed');
  const artifacts=list(r.artifacts,1024);let previous='';for(const entry of artifacts){const a=exact(entry,['path','size','sha256','media_type']);const path=relative(a.path);if(path==='result.json'||path<=previous)refuse('artifact list must be unique, sorted and exclude result');previous=path;integer(a.size);digest(a.sha256);if(!/^[a-zA-Z0-9!#$&^_.+-]+\/[a-zA-Z0-9!#$&^_.+-]+$/.test(text(a.media_type,128)))refuse('invalid media type');}
  return r as unknown as Result;
}
export interface Trace {schema:'millforge-trace/v1'|'millforge-trace/v2'|'millforge-trace/v3'|'millforge-trace/v4'|'millforge-trace/v5';request_id:string;sequence:number;elapsed_ms:number;kind:string;payload:Obj}
export function validateTrace(value: unknown, profile: Profile, request: Request, capture?:Capture): Trace {
  if(profile.schema==='millforge-profile/v4'){
    if(capture?.schema!=='millforge-capture/v5')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
    return validateTraceV5(value,profile,request,capture as CaptureV5);
  }
  if(capture?.schema==='millforge-capture/v5'||object(value).schema==='millforge-trace/v5')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if(profile.schema==='millforge-profile/v3'){if(capture?.schema!=='millforge-capture/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');return validateTraceV4(value,profile,request,capture as CaptureV4);}
  if(capture?.schema==='millforge-capture/v4'||object(value).schema==='millforge-trace/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if((profile.schema==='millforge-profile/v2')!==(capture?.schema==='millforge-capture/v3'))refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const t=exact(value,['schema','request_id','sequence','elapsed_ms','kind','payload']);const v3=capture?.schema==='millforge-capture/v3',v2=v3||capture?.schema==='millforge-capture/v2';version(t,v3?'millforge-trace/v3':v2?'millforge-trace/v2':'millforge-trace/v1');if(t.request_id!==request.request_id)refuse('trace identity mismatch');integer(t.sequence,1);number(t.elapsed_ms,0,Number.MAX_SAFE_INTEGER);const kind=choice(t.kind,['capture','model_start','model_end','tool_start','tool_end','output_partial','limit','cancel','terminal','closure',...(v2?['provider_payload','http_attempt']:[])]);
  const shapes:Record<string,string[]>={capture:['profile_digest'],model_start:['attempt'],model_end:['attempt','stop','usage'],tool_start:['call_id','name','arguments'],tool_end:['call_id','status','text','truncated'],output_partial:['text'],limit:['name','value'],cancel:['signal'],terminal:['status','code'],closure:['worker','tools','writers','inference','detached_effects']};shapes.provider_payload=['attempt','source','api','provider_id','model_id','base_url','controls'];shapes.http_attempt=['attempt','source','method','url'];if(v3)shapes.model_end=['attempt','stop','usage','response_model','native_status','native_terminal'];const p=exact(t.payload,shapes[kind]!);
  if(kind==='provider_payload'||kind==='http_attempt'){if(v3)validateObservationV3(kind,p,profile as ProfileV2,capture!);else validateObservation(kind,p,profile,capture!);}if(v3&&kind==='model_end')validateModelEndV3(p,profile as ProfileV2);
  // Pi Responses preserves its native call and item IDs as one opaque continuation identity.
  const toolCallId=(value:unknown):void=>{if(v3&&profile.model.api==='openai-responses')text(value,Number.MAX_SAFE_INTEGER,'native tool call ID',false);else id(value);};
  switch(kind){case'capture':if(p.profile_digest!==request.profile_digest)refuse('trace digest mismatch');break;case'model_start':integer(p.attempt,1,profile.limits.model_requests);break;case'model_end':integer(p.attempt,1,profile.limits.model_requests);choice(p.stop,['normal','length','tool','error','aborted','unknown','pending','deferred']);if(!v3)validateUsage(p.usage);break;case'tool_start':toolCallId(p.call_id);if(typeof p.name!=='string'||!profile.tools.includes(p.name))refuse('unknown traced tool');if(bytes(canonical(object(p.arguments)))>16384)refuse('tool arguments too large');break;case'tool_end':toolCallId(p.call_id);choice(p.status,statuses);text(p.text,profile.limits.tool_output_bytes);bool(p.truncated);break;case'output_partial':text(p.text,profile.limits.output_bytes);break;case'limit':choice(p.name,limitKeys);integer(p.value);break;case'cancel':choice(p.signal,['SIGINT','SIGTERM','deadline','internal']);break;case'terminal':{const status=choice(p.status,statuses);if(status==='completed'){if(p.code!==null)refuse('completed terminal code must be null');}else choice(p.code,codes);break;}case'closure':validateClosure(p);break;}
  return t as unknown as Trace;
}

/** Validate only selected public observations; never resolve or infer Pi compatibility. */
function validateObservation(kind:string,p:Obj,profile:Profile,capture:Capture):void{
  integer(p.attempt,1,capture.limits.model_requests);const b=capture.binding;
  if(kind==='http_attempt'){
    if(p.source!=='counted_fetch'||p.method!=='POST'||requestEndpoint(p.url,profile.model.api)!==b.request_url)refuse('HTTP observation mismatch');
    return;
  }
  if(p.source!=='pi_onPayload'||p.api!==b.api||p.provider_id!==b.provider_id||p.model_id!==b.model_id||p.base_url!==b.base_url)refuse('payload observation identity mismatch');
  const controls=exact(p.controls,['model','stream','temperature','top_p','max_tokens','max_completion_tokens']);
  if(controls.model!==b.model_id||controls.stream!==true||controls.temperature!==profile.sampling.temperature||controls.top_p!==profile.sampling.top_p)refuse('observed payload control mismatch');
  const field=profile.model.api==='openai-completions'?profile.model.compat.maxTokensField:'max_tokens';
  integer(controls[field as string],1,profile.model.max_output_tokens??Number.MAX_SAFE_INTEGER);
  if(controls[field==='max_tokens'?'max_completion_tokens':'max_tokens']!==null)refuse('observed token control mismatch');
}
