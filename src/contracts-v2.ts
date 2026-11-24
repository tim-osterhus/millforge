/** Pure successor schemas. No Pi, model catalog, credentials or host discovery. */
import {type Obj,type ProfileV1,type Capture,type Request,type Limits,type Mode,object,exact,text,integer,number,choice,refuse,validateProfile,validateCompatCapture,canonical,limitKeys,sourcePin,digest,absolute,list,uniqueStrings,tools,validateCaptureVersions} from './contracts.js';
export const thinkingLevels=['off','minimal','low','medium','high','xhigh','max'] as const;
export type ThinkingLevel=typeof thinkingLevels[number];
export const currentRuntimeGuards={encoded_request_bytes:33554432,encoded_response_bytes:33554432,encoded_record_bytes:33554432} as const;
export interface ProfileV2 extends Omit<ProfileV1,'schema'|'runtime'|'model'|'sampling'> {
  schema:'millforge-profile/v2';runtime:{pi_version:'1.4.0';binding_version:2};
  model:{provider:string;api:'openai-completions'|'anthropic-messages'|'openai-responses';id:string;base_url:string;auth:{kind:'none'}|{kind:'env';name:string}|{kind:'oauth';provider:string};context_tokens:number;max_output_tokens:number|null;metadata:{source:'pinned-catalog'|'operator';overrides:Obj};compat:Obj};
  sampling:{temperature:number|null;top_p:number|null};reasoning?:ThinkingLevel;cache?:{retention:'none'|'short'|'long'};
}
function boolean(v:unknown):void{if(typeof v!=='boolean')refuse('boolean required');}
const fields=['name','input','cost','reasoning','thinkingLevelMap','contextWindow','maxTokens'];
export function validateMetadataFields(value:unknown,required=false,projection=false):Obj{
  const m=exact(value,required?fields:[],required?[]:fields);
  if(m.name!==undefined)text(m.name,256,'model name',false);
  if(m.input!==undefined)uniqueStrings(m.input,2,v=>choice(v,['text','image']));
  if(m.reasoning!==undefined)boolean(m.reasoning);
  for(const k of ['contextWindow','maxTokens'])if(m[k]!==undefined)integer(m[k],1);
  if(m.thinkingLevelMap===null&&!projection)refuse('thinking map must be typed');
  if(m.thinkingLevelMap!==undefined&&m.thinkingLevelMap!==null){const map=exact(m.thinkingLevelMap,[],thinkingLevels);for(const v of Object.values(map))if(v!==null)text(v,256,'mapped effort',false);}
  if(m.cost!==undefined&&m.cost!==null){const c=exact(m.cost,['input','output','cacheRead','cacheWrite'],['tiers']);for(const k of ['input','output','cacheRead','cacheWrite'])number(c[k],0,Number.MAX_VALUE);if(c.tiers!==undefined)for(const t of list(c.tiers,128)){const tier=exact(t,['inputTokensAbove','input','output','cacheRead','cacheWrite']);for(const n of Object.values(tier))number(n,0,Number.MAX_VALUE);}}
  return m;
}
export function validateLimitsV2(value:unknown,mode:Mode):Limits{
  const l=exact(value,limitKeys);for(const k of limitKeys)integer(l[k],k==='tool_calls'?0:1);
  integer(l.elapsed_seconds,6,2147483);for(const k of ['model_timeout_seconds','tool_timeout_seconds'])integer(l[k],1,Number(l.elapsed_seconds)-5);
  if(Number(l.trace_bytes)<=16384)refuse('trace limit must exceed closure reserve');
  if(mode==='generate'&&(l.model_requests!==1||l.tool_calls!==0))refuse('generate requires one request and no tools');
  return l as unknown as Limits;
}
export function validateProfileV2(value:unknown):ProfileV2{
  const p=exact(value,['schema','profile_id','revision','mode','runtime','model','instructions','sampling','tools','assets','capabilities','limits','terminal'],['description','reasoning','cache']);
  const runtime=exact(p.runtime,['pi_version','binding_version']);if(runtime.pi_version!=='1.4.0'||runtime.binding_version!==2)refuse('unsupported runtime version','UNSUPPORTED_VERSION');
  const m=exact(p.model,['provider','api','id','base_url','auth','context_tokens','max_output_tokens','metadata','compat']);text(m.provider,256,'provider',false);text(m.id,256,'model ID',false);
  choice(m.api,['openai-completions','anthropic-messages','openai-responses']);integer(m.context_tokens,1);if(m.max_output_tokens!==null)integer(m.max_output_tokens,1);
  const a=object(m.auth);choice(a.kind,['none','env','oauth']);exact(a,a.kind==='none'?['kind']:a.kind==='env'?['kind','name']:['kind','provider']);
  if(a.kind==='env'&&!/^[A-Za-z_][A-Za-z0-9_]*$/.test(text(a.name,256,'environment name',false)))refuse('invalid auth environment reference');
  if(a.kind==='oauth'&&(a.provider!==m.provider||m.provider!=='openai'||m.api!=='openai-responses'||m.base_url!=='https://api.openai.com/v1'))refuse('unsupported OAuth binding','UNSUPPORTED_BINDING');
  if(m.api==='openai-responses'&&a.kind!=='oauth')refuse('Responses requires selected OAuth binding','UNSUPPORTED_BINDING');
  validateCompatV2(m.compat,m.api as string);
  const metadata=exact(m.metadata,['source','overrides']);choice(metadata.source,['pinned-catalog','operator']);validateMetadataFields(metadata.overrides,metadata.source==='operator');
  if(p.reasoning!==undefined)choice(p.reasoning,thinkingLevels);if(p.cache!==undefined)choice(exact(p.cache,['retention']).retention,['none','short','long']);
  const sampling=exact(p.sampling,['temperature','top_p']);
  if(sampling.temperature!==null)number(sampling.temperature,0,m.api==='anthropic-messages'?1:Number.MAX_VALUE);
  if(sampling.top_p!==null&&number(sampling.top_p,0,1)===0)refuse('top_p must be positive');
  if(m.api==='anthropic-messages'&&sampling.top_p!==null)refuse('Anthropic top_p must be null');
  if(a.kind==='oauth'&&(sampling.temperature!==null||sampling.top_p!==null||m.max_output_tokens!==null||object(p.cache??{}).retention==='long'))refuse('OAuth route drops requested control');
  // Reuse unchanged common profile rules using a private legacy-shaped validation value.
  const mode=choice(p.mode,['generate','agent']);validateLimitsV2(p.limits,mode);
  const {reasoning:_reasoning,cache:_cache,...common}=p;
  validateProfile({...common,schema:'millforge-profile/v1',runtime:{pi_version:'1.4.0',binding_version:1},model:{api:'anthropic-messages',id:m.id,base_url:m.base_url,credential_env:null,context_tokens:1,max_output_tokens:1,compat:{}},sampling:{temperature:0,top_p:null},limits:{model_requests:mode==='generate'?1:64,tool_calls:mode==='generate'?0:256,elapsed_seconds:60,model_timeout_seconds:55,tool_timeout_seconds:55,input_bytes:1,context_bytes:1,output_bytes:1,tool_output_bytes:1,trace_bytes:16385}} as unknown);
  return p as unknown as ProfileV2;
}
export interface RuntimeGuards {encoded_request_bytes:number;encoded_response_bytes:number;encoded_record_bytes:number}
export interface CaptureV3 extends Omit<Capture,'schema'|'credential_env'> {schema:'millforge-capture/v3';runtime_guards:Obj}
export interface UsageV3 {input_tokens:number|null;output_tokens:number|null;cache_read_tokens:number|null;cache_write_tokens:number|null;reasoning_tokens:number|null}
function same(a:unknown,b:unknown,where:string):void{if(canonical(a as Obj)!==canonical(b as Obj))refuse(`${where} mismatch`);}
function utc(v:unknown):void{if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(text(v,64))||!Number.isFinite(Date.parse(String(v))))refuse('UTC timestamp required');}
export function validateCaptureV3(value:unknown,p:ProfileV2,r:Request,profileDigest:string):CaptureV3{
  const c=exact(value,['schema','request_id','profile_digest','limits','cwd','resources','system_prompt','tool_names','tool_schemas','binding','runtime_guards','versions','external_tools']);
  if(c.schema!=='millforge-capture/v3')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  if(c.request_id!==r.request_id||c.profile_digest!==profileDigest||c.cwd!==r.workspace)refuse('capture identity mismatch');
  same(c.limits,{...p.limits,...r.limits},'capture limits');validateLimitsV2(c.limits,p.mode);
  const guards=exact(c.runtime_guards,Object.keys(currentRuntimeGuards));for(const [k,v]of Object.entries(guards))integer(v,1,currentRuntimeGuards[k as keyof RuntimeGuards]);
  text(c.system_prompt,Number(guards.encoded_record_bytes),'system prompt');
  const resources=list(c.resources,128);if(resources.length!==p.assets.length)refuse('resource mismatch');resources.forEach((v,i)=>{const a=exact(v,['path','sha256','kind','size']);same({path:a.path,sha256:a.sha256,kind:a.kind},p.assets[i],'resource');integer(a.size,0);});
  same(uniqueStrings(c.tool_names,7,v=>choice(v,tools)),p.tools,'tool order');const schemas=list(c.tool_schemas,7);if(schemas.length!==p.tools.length)refuse('tool schemas mismatch');schemas.forEach((v,i)=>{const a=exact(v,['name','sha256']);if(a.name!==p.tools[i])refuse('tool schema order mismatch');digest(a.sha256);});
  const b=exact(c.binding,['api','version','provider_id','model_id','base_url','request_url','source_pin','model_metadata','compat','auth','requested_options','resolved_options']);
  if(b.api!==p.model.api||b.version!==2||b.provider_id!==p.model.provider||b.model_id!==p.model.id||b.base_url!==p.model.base_url||b.source_pin!==sourcePin)refuse('binding identity mismatch');
  const suffix=p.model.api==='openai-responses'?'/responses':p.model.api==='openai-completions'?'/chat/completions':'/v1/messages?beta=true';
  if(b.request_url!==p.model.base_url.replace(/\/$/,'')+suffix)refuse('request URL mismatch');
  const mm=exact(b.model_metadata,['source','authored_overrides','selected']),source=exact(mm.source,['kind','source_pin','catalog_sha256','profile_digest']);
  if(source.kind!==p.model.metadata.source||source.source_pin!==sourcePin||source.profile_digest!==profileDigest)refuse('metadata provenance mismatch');
  if(source.kind==='pinned-catalog')digest(source.catalog_sha256);else if(source.catalog_sha256!==null)refuse('operator catalog hash must be null');same(mm.authored_overrides,p.model.metadata.overrides,'metadata overrides');
  const selected=exact(mm.selected,[...fields,'promptCache','inputLimits','samplingParams','samplingParamsByThinkingLevel']);
  validateMetadataFields(Object.fromEntries(fields.map(k=>[k,selected[k]])),true,true);
  for(const [k,v]of Object.entries(p.model.metadata.overrides))same(v,selected[k],'selected metadata override');
  validateSelectedExtras(selected);
  const compat=exact(b.compat,['catalog_values','authored_overrides','internal_resolution']);if(compat.internal_resolution!=='unknown')refuse('compat resolution must remain unknown');same(compat.authored_overrides,p.model.compat,'compat overrides');validateCompatV2(compat.catalog_values,p.model.api);
  const auth=exact(b.auth,['kind','provider_id','reference','lease']);if(auth.kind!==p.model.auth.kind||auth.provider_id!==p.model.provider)refuse('auth mismatch');
  if(p.model.auth.kind==='none'){if(auth.reference!==null||auth.lease!==null)refuse('unexpected auth reference');}
  else if(p.model.auth.kind==='env'){if(auth.reference!==p.model.auth.name||auth.lease!==null)refuse('env reference mismatch');}
  else{absolute(auth.reference);const lease=exact(auth.lease,['resolved_at','sdk_expires_at','minimum_validity_ms','refresh_in_run']);utc(lease.resolved_at);integer(lease.sdk_expires_at,1);integer(lease.minimum_validity_ms,1);if(lease.refresh_in_run!==false)refuse('in-run refresh unadmitted');if(Number(lease.minimum_validity_ms)<Number(object(c.limits).elapsed_seconds)*1000+300000||Number(lease.sdk_expires_at)<Date.parse(String(lease.resolved_at))+Number(lease.minimum_validity_ms))refuse('insufficient captured OAuth lease');}
  same(exact(b.requested_options,['temperature','top_p','max_output_tokens','thinking','cache_retention','max_retries']),{...p.sampling,max_output_tokens:p.model.max_output_tokens,thinking:p.reasoning??null,cache_retention:p.cache?.retention??null,max_retries:0},'requested options');
  const resolved=exact(b.resolved_options,['thinking','thinking_source','mapped_effort','cache_retention','cache_source']);choice(resolved.thinking,thinkingLevels);if(resolved.thinking_source!==(p.reasoning===undefined?'pi_default':'profile')||p.reasoning!==undefined&&resolved.thinking!==p.reasoning)refuse('reasoning source mismatch');
  if(resolved.mapped_effort!==null)text(resolved.mapped_effort,256,'mapped effort',false);
  const level=String(resolved.thinking),map=selected.thinkingLevelMap===null?{}:object(selected.thinkingLevelMap),mapped=map[level];
  if(mapped===null||(!selected.reasoning&&level!=='off')||mapped===undefined&&['xhigh','max'].includes(level))refuse('unsupported captured reasoning');
  const expected=mapped??(level==='off'?null:level);if(resolved.mapped_effort!==expected)refuse('mapped effort mismatch');
  if(p.model.context_tokens>Number(selected.contextWindow)||p.model.max_output_tokens!==null&&p.model.max_output_tokens>Number(selected.maxTokens))refuse('captured model capacity mismatch');choice(resolved.cache_retention,['none','short','long']);if(resolved.cache_source!==(p.cache===undefined?'pi_default':'profile')||p.cache!==undefined&&resolved.cache_retention!==p.cache.retention)refuse('cache source mismatch');
  validateCaptureVersions(c.versions);
  const bins=list(c.external_tools,2),wanted=p.tools.filter(n=>n==='find'||n==='grep').map(n=>n==='find'?'fd':'rg').sort();const actual:string[]=[];for(const v of bins){const e=exact(v,['name','path','sha256','size','stat']);actual.push(choice(e.name,['fd','rg']));absolute(e.path);digest(e.sha256);integer(e.size);const st=exact(e.stat,['dev','ino','mode','mtime_ms']);for(const x of Object.values(st))number(x,0,Number.MAX_SAFE_INTEGER);}same(actual.sort(),wanted,'external tools');
  return c as unknown as CaptureV3;
}
export function validateUsageV3(value:unknown):void{
  if(value===null)return;const u=exact(value,['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens']);for(const v of Object.values(u))if(v!==null)integer(v);
  if(u.input_tokens!==null){let cache=0;for(const k of ['cache_read_tokens','cache_write_tokens'])if(u[k]!==null){cache+=Number(u[k]);if(!Number.isSafeInteger(cache)||cache>Number(u.input_tokens))refuse('cache usage exceeds total input');}}
}
export function validateModelEndV3(p:Obj,profile:ProfileV2):void{
  validateUsageV3(p.usage);for(const k of ['response_model','native_status','native_terminal'])if(p[k]!==null)text(p[k],256,k,false);
  if(profile.model.api!=='openai-responses'&&['normal','tool'].includes(String(p.stop))&&p.native_terminal!==(profile.model.api==='openai-completions'?'chat.finish_reason':'anthropic.message_delta'))refuse('native terminal provenance mismatch');
  if(profile.model.api==='openai-responses'&&['normal','tool'].includes(String(p.stop))&&(p.native_status!=='completed'||p.native_terminal!=='response.completed'))refuse('Responses success requires native completion');
}
export function validateObservationV3(kind:string,p:Obj,profile:ProfileV2,capture:Capture):void{
  integer(p.attempt,1,capture.limits.model_requests);const b=capture.binding;
  if(kind==='http_attempt'){if(p.source!=='counted_fetch'||p.method!=='POST'||p.url!==b.request_url)refuse('HTTP observation mismatch');return;}
  if(p.source!=='pi_onPayload'||p.api!==b.api||p.provider_id!==b.provider_id||p.model_id!==b.model_id||p.base_url!==b.base_url)refuse('payload identity mismatch');
  const c=exact(p.controls,['model','stream','store','temperature','top_p','max_tokens','max_completion_tokens','max_output_tokens','reasoning_effort','reasoning_summary','encrypted_context_requested','thinking','cache']);
  if(c.model!==b.model_id||c.stream!==true)refuse('payload model mismatch');if(c.store!==null)boolean(c.store);
  for(const k of ['temperature','top_p'])if(c[k]!==null)number(c[k],0,k==='top_p'?1:Number.MAX_VALUE);
  for(const k of ['temperature','top_p'])if(profile.sampling[k as 'temperature'|'top_p']!==null&&c[k]!==profile.sampling[k as 'temperature'|'top_p'])refuse('sampling omission or drift');
  const tokenFields=['max_tokens','max_completion_tokens','max_output_tokens'];let seen=0;for(const k of tokenFields)if(c[k]!==null){integer(c[k],1,profile.model.max_output_tokens??Number.MAX_SAFE_INTEGER);seen++;}if(seen>1||profile.model.max_output_tokens!==null&&seen!==1)refuse('token ceiling omission or drift');
  for(const k of ['reasoning_effort','reasoning_summary'])if(c[k]!==null)text(c[k],256,k,false);boolean(c.encrypted_context_requested);
  if(c.thinking!==null){const t=exact(c.thinking,['type','budget_tokens']);choice(t.type,['disabled','enabled','adaptive']);if(t.budget_tokens!==null)integer(t.budget_tokens,1);}
  const cache=exact(c.cache,['prompt_cache_key_present','prompt_cache_retention','prompt_cache_options','message_cache_control_present','tool_cache_control_present']);for(const k of ['prompt_cache_key_present','message_cache_control_present','tool_cache_control_present'])boolean(cache[k]);if(cache.prompt_cache_retention!==null)text(cache.prompt_cache_retention,256);if(cache.prompt_cache_options!==null){const o=exact(cache.prompt_cache_options,['mode','ttl']);for(const v of Object.values(o))if(v!==null)text(v,256);}
  if(profile.model.api==='openai-responses'){const resolved=object(b.resolved_options);if(c.reasoning_effort!==resolved.mapped_effort)refuse('observed reasoning effort drift');if(resolved.thinking!=='off'&&(c.reasoning_summary===null||c.encrypted_context_requested!==true))refuse('missing native reasoning continuation controls');}
  if(profile.model.api==='openai-responses'&&(c.store!==false||c.temperature!==null||c.top_p!==null||seen!==0||cache.prompt_cache_retention!==null||cache.prompt_cache_options!==null))refuse('OAuth dropped control observed');
  if(profile.cache?.retention==='none'&&(cache.prompt_cache_key_present||cache.message_cache_control_present||cache.tool_cache_control_present))refuse('cache none drift');
}

/** Typed public compatibility only; no private resolution or provider defaults. */
function validateCompatV2(value:unknown,api:string):void{
  if(api==='openai-responses'){const c=exact(value,[],['supportsDeveloperRole','supportsMidConvoSystemMessages','sessionAffinityFormat','supportsLongCacheRetention','supportsStrictMode','supportsOpenAIGrammarTools','supportsAdditionalTools','supportsToolSearch','supportsExplicitPromptCacheMode','supportsMaxOutputTokens','supportsMidConvoEffort']);for(const [k,v]of Object.entries(c))if(k==='sessionAffinityFormat')choice(v,['openai','openai-nosession','openrouter']);else boolean(v);return;}
  validateCompatCapture(value,{model:{api,compat:{}}} as unknown as ProfileV1,false);
}

function validateSelectedExtras(s:Obj):void{
  if(s.promptCache!==null){const p=exact(s.promptCache,[],['short','long']);for(const n of Object.values(p)){if(number(n,0,Number.MAX_VALUE)===0)refuse('cache lifetime must be positive');}}
  if(s.inputLimits!==null){const i=exact(s.inputLimits,[],['maxRequestBytes','images']);if(i.maxRequestBytes!==undefined)integer(i.maxRequestBytes,1);if(i.images!==undefined){const images=exact(i.images,[],['resize','maxPerMessage','maxPerRequest']);for(const k of ['maxPerMessage','maxPerRequest'])if(images[k]!==undefined)integer(images[k],1);if(images.resize!==undefined){const resize=exact(images.resize,[],['maxWidth','maxHeight','maxBytes','jpegQuality']);for(const [k,v]of Object.entries(resize))integer(v,1,k==='jpegQuality'?100:Number.MAX_SAFE_INTEGER);}}}
  if(s.samplingParams!==null)object(s.samplingParams);if(s.samplingParamsByThinkingLevel!==null){const map=exact(s.samplingParamsByThinkingLevel,[],thinkingLevels);for(const v of Object.values(map))object(v);}
}
