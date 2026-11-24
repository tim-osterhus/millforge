import {describe,expect,it} from 'vitest';
import * as fs from 'node:fs/promises';
import {type ProfileV1,type Json,type Limits,type Obj,limitKeys,object,parseJson,sourcePin,tightenLimits,validateCapture,validateLimits,validateProfile as validateAnyProfile,validateRequest,validateTrace} from '../src/contracts.js';
const validateProfile=(value:unknown):ProfileV1=>validateAnyProfile(value) as ProfileV1;
async function profile(){return validateProfile(parseJson(await fs.readFile(new URL('../profiles/agent/profile.json',import.meta.url))));}
describe('finite exact limit contracts',()=>{
  it.each(limitKeys)('refuses bool, unsafe, fractional and negative %s',async(key)=>{const p=await profile();for(const value of [true,Number.MAX_SAFE_INTEGER+1,1.5,-1])expect(()=>validateLimits({...p.limits,[key]:value},p.mode)).toThrow();});
  it.each(limitKeys)('refuses widened request %s',async(key)=>{const p=await profile();expect(()=>tightenLimits(p,{[key]:p.limits[key]+1})).toThrow();});
  it.each([['model_requests',65],['tool_calls',257],['elapsed_seconds',1801],['input_bytes',1048577],['context_bytes',2097153],['output_bytes',262145],['tool_output_bytes',262145],['trace_bytes',8388609]] as const)('refuses v1 ceiling %s',async(key,value)=>{const p=await profile();expect(()=>validateLimits({...p.limits,[key]:value},p.mode)).toThrow();});
  it('enforces cleanup reserve, missing/unknown bounds and zero semantics',async()=>{const p=await profile();expect(()=>validateLimits({...p.limits,model_timeout_seconds:176},p.mode)).toThrow();expect(()=>validateLimits({...p.limits,tool_timeout_seconds:176},p.mode)).toThrow();expect(()=>validateLimits({...p.limits,extra:1},p.mode)).toThrow();const value={...p.limits} as Partial<Limits>;delete value.trace_bytes;expect(()=>validateLimits(value,p.mode)).toThrow();expect(()=>tightenLimits(p,{tool_calls:0})).not.toThrow();expect(()=>tightenLimits(p,{model_requests:0})).toThrow();expect(()=>tightenLimits(p,{elapsed_seconds:10})).toThrow();expect(tightenLimits(p,{elapsed_seconds:40,model_timeout_seconds:35,tool_timeout_seconds:30}).elapsed_seconds).toBe(40);});
  it('admits only request identity/mode/digest and tighter bounds',async()=>{const p=await profile();const r:Obj={schema:'millforge-request/v1',request_id:'request',profile_id:p.profile_id,profile_digest:'a'.repeat(64),expected_mode:'agent',task:'task',context:[],workspace:'/explicit/workspace',limits:{},correlation:{parent:'opaque'}};expect(validateRequest(r,p,'a'.repeat(64)).request_id).toBe('request');for(const [key,value]of [['profile_id','other'],['profile_digest','b'.repeat(64)],['expected_mode','generate'],['model',{id:'other'}],['limits',{input_bytes:2}],['workspace',null],['correlation',{'bad/key':'value'}]] as [string,Json][]){expect(()=>validateRequest({...r,[key]:value},p,'a'.repeat(64))).toThrow();}});
});

describe('bounded v2 public observation shapes',()=>{
  async function boundary(){
    const p=await profile();const digest='a'.repeat(64);const request=validateRequest({schema:'millforge-request/v1',request_id:'bounded',profile_id:p.profile_id,profile_digest:digest,expected_mode:p.mode,task:'task',context:[],workspace:'/explicit',limits:{},correlation:{}},p,digest);
    const value={schema:'millforge-capture/v2',request_id:request.request_id,profile_digest:digest,limits:p.limits,cwd:request.workspace,resources:[],system_prompt:p.instructions,tool_names:p.tools,tool_schemas:p.tools.map(name=>({name,sha256:'b'.repeat(64)})),binding:{api:p.model.api,version:p.runtime.binding_version,provider_id:'explicit-provider',model_id:p.model.id,base_url:p.model.base_url,request_url:p.model.base_url+'/chat/completions',source_pin:sourcePin,compat:{authored_overrides:p.model.compat,internal_resolution:'unknown'},requested_options:{...p.sampling,thinking:'off',max_retries:0}},credential_env:p.model.credential_env,versions:{millforge:'0.2.0',pi:'1.4.0',node:'22.19.0',platform:'linux'},external_tools:[]};
    return {p,request,value,capture:validateCapture(value,p,request,digest),digest};
  }
  it('retains existing provider/URL field ceilings and exact selected request scope',async()=>{
    const f=await boundary();for(const binding of [{...f.value.binding,provider_id:'p'.repeat(257)},{...f.value.binding,request_url:f.p.model.base_url+'/'+'x'.repeat(4096)},{...f.value.binding,request_url:f.p.model.base_url.replace('/v1','/v10')+'/chat/completions'}])expect(()=>validateCapture({...f.value,binding},f.p,f.request,f.digest)).toThrow();
    expect(f.capture.limits.trace_bytes).toBe(f.p.limits.trace_bytes);expect(f.capture.limits.context_bytes).toBe(f.p.limits.context_bytes);
  });
  it('bounds attempt ordinals and rejects unallowlisted transport/credential fields',async()=>{
    const f=await boundary();const value={schema:'millforge-trace/v2',request_id:f.request.request_id,sequence:1,elapsed_ms:0,kind:'http_attempt',payload:{attempt:1,source:'counted_fetch',method:'POST',url:f.value.binding.request_url}};
    expect(()=>validateTrace(value,f.p,f.request,f.capture)).not.toThrow();
    for(const attempt of [true,0,1.5,f.p.limits.model_requests+1])expect(()=>validateTrace({...value,payload:{...value.payload,attempt}},f.p,f.request,f.capture)).toThrow();
    for(const extra of [{headers:{authorization:'secret'}},{body:'raw prompt'},{reasoning:'private'}])expect(()=>validateTrace({...value,payload:{...value.payload,...extra}},f.p,f.request,f.capture)).toThrow();
  });
});
