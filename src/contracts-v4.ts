/** Opt-in codemode data. Common native fields keep their v2/v3 validation. */
import {type Obj,type Json,type Capture,type ProfileV2,type Request,type Result,type Trace,bytes,canonical,choice,digest,exact,id,integer,list,object,refuse,sourcePin,text,uniqueStrings,validateResult,validateTrace} from './contracts.js';
import {type CaptureV3,validateProfileV2,validateCaptureV3} from './contracts-v2.js';

export interface ProfileV3 extends Omit<ProfileV2,'schema'|'mode'> {schema:'millforge-profile/v3';mode:'agent'}
export const codemodeOptions={models:false,mode:'on',inlineBudget:3000} as const;
export const codemodeConstants={script_timeout_default:null,script_timeout_max_ms:2147483647,vm_memory_limit_bytes:268435456,max_workers_env:1,worker_wait_ms:60000,vm_output_chars:16777216,vm_output_items:100000,store_value_chars:262144,store_total_chars:1048576,default_max_output_tokens:10000,nested_record_max_calls:256} as const;
export const codemodeLoader={extension_paths:['<inline:millforge-codemode>','<inline:millforge-guard>'],project_trusted:false,roots_empty_before:true,roots_empty_after:true} as const;
export interface CaptureV4 extends Omit<CaptureV3,'schema'> {schema:'millforge-capture/v4';declared_tools:{name:string;description_sha256:string}[];callable_tool_names:string[];capability:Obj}
export type ResultV4=Omit<Result,'schema'>&{schema:'millforge-result/v4'};
export type TraceV4=Omit<Trace,'schema'>&{schema:'millforge-trace/v4'};
const same=(a:unknown,b:unknown,where:string):void=>{if(canonical(a as Json)!==canonical(b as Json))refuse(`${where} mismatch`);};
/** Private validation projection only. It never changes the authored or saved object. */
export function nativeProfile(p:ProfileV3):ProfileV2{return {...p,schema:'millforge-profile/v2',capabilities:{extensions:[],packages:[]}};}
export function validateProfileV3(value:unknown):ProfileV3{
  const p=object(value);
  if(p.schema!=='millforge-profile/v3')refuse('unsupported schema version','UNSUPPORTED_VERSION');
  const caps=exact(p.capabilities,['extensions','packages']);
  if(canonical(caps)!==canonical({extensions:['pi-codemode/v1'],packages:[]}))refuse('unsupported v3 capability tuple','UNSUPPORTED_CAPABILITY');
  if(p.mode!=='agent'||!list(p.tools,7).length)refuse('codemode requires agent mode and native tools');
  validateProfileV2({...p,schema:'millforge-profile/v2',capabilities:{extensions:[],packages:[]}});
  return p as unknown as ProfileV3;
}
export function nativeCapture(c:CaptureV4,p:ProfileV3):CaptureV3{
  const {declared_tools:_declared,callable_tool_names:_callable,capability:_capability,...common}=c;
  return {...common,schema:'millforge-capture/v3',tool_names:p.tools,tool_schemas:p.tools.map(name=>c.tool_schemas.find(s=>s.name===name)!)};
}
export function validateCaptureV4(value:unknown,p:ProfileV3,r:Request,profileDigest:string):CaptureV4{
  if(object(value).schema!=='millforge-capture/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const c=exact(value,['schema','request_id','profile_digest','limits','cwd','resources','system_prompt','tool_names','tool_schemas','binding','runtime_guards','versions','external_tools','declared_tools','callable_tool_names','capability']);
  const names=uniqueStrings(c.tool_names,8,v=>choice(v,[...p.tools,'codemode']));
  same([...names].sort(),[...p.tools,'codemode'].sort(),'active tool set');
  const schemas=list(c.tool_schemas,8),declared=list(c.declared_tools,8);
  if(schemas.length!==names.length||declared.length!==names.length)refuse('declaration count mismatch');
  schemas.forEach((v,i)=>{const a=exact(v,['name','sha256']);if(a.name!==names[i])refuse('tool schema order mismatch');digest(a.sha256);});
  declared.forEach((v,i)=>{const a=exact(v,['name','description_sha256']);if(a.name!==names[i])refuse('declared tool order mismatch');digest(a.description_sha256);});
  const callable=uniqueStrings(c.callable_tool_names,7,v=>choice(v,p.tools));same([...callable].sort(),[...p.tools].sort(),'callable tool set');
  const cap=exact(c.capability,['id','factory','package','version','source_pin','fixed_options','exposure','default_active','declared_description_sha256','constrained_sampling_sha256','hidden_declarations','loader','pinned_constants']);
  for(const [key,v]of Object.entries({id:'pi-codemode/v1',factory:'createCodemodeExtension',package:'@earendil-works/pi-coding-agent',version:'1.4.0',source_pin:sourcePin,exposure:'model-only',default_active:false}))if(cap[key]!==v)refuse('capability identity mismatch');
  same(exact(cap.fixed_options,Object.keys(codemodeOptions)),codemodeOptions,'fixed options');
  same(exact(cap.loader,Object.keys(codemodeLoader)),codemodeLoader,'loader assertions');
  same(exact(cap.pinned_constants,Object.keys(codemodeConstants)),codemodeConstants,'pinned constants');
  same(cap.hidden_declarations,[],'hidden declarations');digest(cap.constrained_sampling_sha256);digest(cap.declared_description_sha256);
  if(cap.declared_description_sha256!==object(declared[names.indexOf('codemode')]).description_sha256)refuse('codemode description digest mismatch');
  validateCaptureV3(nativeCapture(c as unknown as CaptureV4,p),nativeProfile(p),r,profileDigest);
  return c as unknown as CaptureV4;
}
export function topLevelId(value:unknown,p:ProfileV3):string{return p.model.api==='openai-responses'?text(value,Number.MAX_SAFE_INTEGER,'native tool call ID',false):id(value);}
export function nestedId(value:unknown,parent:unknown,p:ProfileV3):string{
  const owner=topLevelId(parent,p),call=text(value,Number.MAX_SAFE_INTEGER,'nested tool call ID',false),suffix=call.slice(owner.length+1);
  if(!call.startsWith(owner+'/')||! /^[1-9]\d*$/.test(suffix))refuse('invalid nested tool identity');
  integer(Number(suffix),1);return call;
}
export function validateDeclaration(value:unknown):void{
  const d=exact(value,['format','grammar_sha256','schema_sha256']);choice(d.format,['grammar','json-schema']);
  if(d.format==='grammar'){digest(d.grammar_sha256);if(d.schema_sha256!==null)refuse('grammar declaration has schema digest');}
  else{digest(d.schema_sha256);if(d.grammar_sha256!==null)refuse('schema declaration has grammar digest');}
}
export function validateTraceV4(value:unknown,p:ProfileV3,r:Request,c:CaptureV4):TraceV4{
  const t=exact(value,['schema','request_id','sequence','elapsed_ms','kind','payload']);if(t.schema!=='millforge-trace/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const common={...t,schema:'millforge-trace/v3'},payload=object(t.payload),profile=nativeProfile(p),capture=nativeCapture(c,p);
  if(t.kind==='nested_join'){
    const j=exact(payload,['parent_call_id','started','ended','outstanding']);topLevelId(j.parent_call_id,p);integer(j.started);integer(j.ended,0,Number(j.started));integer(j.outstanding);
    if(j.outstanding!==Number(j.started)-Number(j.ended))refuse('join count mismatch');
    validateTrace({...common,kind:'capture',payload:{profile_digest:r.profile_digest}},profile,r,capture);
  }else if(t.kind==='tool_start'||t.kind==='tool_end'){
    const start=t.kind==='tool_start';exact(payload,start?['call_id','name','arguments','parent_call_id']:['call_id','status','text','truncated','parent_call_id']);
    if(payload.parent_call_id===null)topLevelId(payload.call_id,p);else nestedId(payload.call_id,payload.parent_call_id,p);
    if(start){if(typeof payload.name!=='string'||!(payload.parent_call_id===null?c.tool_names:c.callable_tool_names).includes(payload.name))refuse('unknown traced tool');if(bytes(canonical(object(payload.arguments)))>16384)refuse('tool arguments too large');}
    const {parent_call_id:_parent,...old}=payload;
    // Common value checks are unchanged; the old reader never sees a new ancestry shape.
    validateTrace({...common,payload:{...old,call_id:'validation',...(start?{name:p.tools[0]!}:{})}},profile,r,capture);
  }else if(t.kind==='provider_payload'){
    exact(payload,['attempt','source','api','provider_id','model_id','base_url','controls','codemode_declaration']);validateDeclaration(payload.codemode_declaration);
    if(p.model.api==='anthropic-messages'&&object(payload.codemode_declaration).format!=='json-schema')refuse('Anthropic requires schema declaration');
    const {codemode_declaration:_declaration,...old}=payload;validateTrace({...common,payload:old},profile,r,capture);
  }else validateTrace(common,profile,r,capture);
  return t as unknown as TraceV4;
}
export function validateResultV4(value:unknown,p:ProfileV3,r:Request,c:CaptureV4):ResultV4{
  const result=object(value);if(result.schema!=='millforge-result/v4')refuse('record/profile family mismatch','UNSUPPORTED_VERSION');
  const counts=exact(result.counts,['model_requests','tool_calls','nested_tool_calls','input_bytes','output_bytes','trace_bytes','usage','backend_requests','cost']);integer(counts.nested_tool_calls,0,integer(counts.tool_calls));
  const {nested_tool_calls:_nested,...old}=counts;
  validateResult({...result,schema:'millforge-result/v3',counts:old},nativeProfile(p),r,nativeCapture(c,p));return result as unknown as ResultV4;
}
