import {afterEach,describe,expect,it} from 'vitest';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {tmpdir} from 'node:os';
import {type Json,type Obj,type Trace,type ProfileV1,canonical,object,parseJson,sourcePin,tightenLimits,validateCapture,validateProfile,validateRequest,validateTrace} from '../src/contracts.js';
import {artifactManifest,createCapture,inspectRun,publishResult,requiredArtifacts,writeTrace} from '../src/artifacts.js';
import {hash,loadProfile} from '../src/profile.js';
import {main} from '../src/cli.js';
const roots:string[]=[];afterEach(async()=>{for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});});
// These records describe a deliberately fake, offline producer. They do not attest Pi execution.
async function fixture(agent=false,options:{task?:string;systemPrompt?:string;contextBytes?:number;v2?:boolean;anthropic?:boolean;traceBytes?:number;topP?:number}={}){
  const root=await fs.mkdtemp(path.join(tmpdir(),'millforge-record-'));roots.push(root);const dir=path.join(root,'authored');await fs.mkdir(dir);const p=validateProfile(parseJson(await fs.readFile(new URL(`../profiles/${agent?'agent':'generate'}/profile.json`,import.meta.url)))) as ProfileV1;
  if(options.anthropic){p.model.api='anthropic-messages';p.model.base_url='http://127.0.0.1:8080';p.model.compat={};p.sampling.top_p=null;}
  if(options.topP!==undefined)p.sampling.top_p=options.topP;
  if(options.traceBytes!==undefined)p.limits.trace_bytes=options.traceBytes;
  if(options.contextBytes!==undefined)p.limits.context_bytes=options.contextBytes;await fs.writeFile(path.join(dir,'profile.json'),JSON.stringify(p));const loaded=await loadProfile(dir);const request=validateRequest({schema:'millforge-request/v1',request_id:'fake-request',profile_id:p.profile_id,profile_digest:loaded.digest,expected_mode:p.mode,task:options.task??'fixed supplied task',context:[],workspace:agent?root:null,limits:{},correlation:{}},p,loaded.digest);
  const capture=validateCapture({schema:options.v2?'millforge-capture/v2':'millforge-capture/v1',request_id:request.request_id,profile_digest:loaded.digest,limits:tightenLimits(p,{}),cwd:request.workspace,resources:loaded.resources,system_prompt:options.systemPrompt??p.instructions,tool_names:p.tools,tool_schemas:p.tools.map(name=>({name,sha256:hash('fake-schema-'+name)})),binding:options.v2?{api:p.model.api,version:p.runtime.binding_version,provider_id:'fake-offline-data',model_id:p.model.id,base_url:p.model.base_url,request_url:p.model.base_url.replace(/\/$/,'')+(options.anthropic?'/v1/messages?beta=true':'/chat/completions'),source_pin:sourcePin,compat:{authored_overrides:p.model.compat,internal_resolution:'unknown'},requested_options:{...p.sampling,thinking:'off',max_retries:0}}:{api:p.model.api,version:1,provider_id:'fake-offline-data',base_url:p.model.base_url,source_pin:sourcePin,compat:{...p.model.compat,supportsStore:false,thinkingFormat:'openai'},options:{...p.sampling,thinking:'off',max_retries:0}},credential_env:p.model.credential_env,versions:{millforge:'0.2.0',pi:'1.4.0',node:'22.19.0',platform:'linux'},external_tools:[]},p,request,loaded.digest);
  const run=path.join(root,'saved');await createCapture(run,loaded,request,capture);
  return {root,dir,p,loaded,request,capture,run};
}
type Fixture=Awaited<ReturnType<typeof fixture>>;
function event(f:Fixture,sequence:number,kind:string,payload:Obj):Trace{return {schema:f.capture.schema==='millforge-capture/v2'?'millforge-trace/v2':'millforge-trace/v1',request_id:f.request.request_id,sequence,elapsed_ms:sequence,kind,payload};}
const closed:Obj={worker:'closed',tools:'closed',writers:'closed',inference:'response_returned',detached_effects:'none_observed'};
function observations(f:Fixture,attempt:number):[string,Obj][]{
  const b=f.capture.binding;const field=f.p.model.api==='openai-completions'?f.p.model.compat.maxTokensField:'max_tokens';
  return [['provider_payload',{attempt,source:'pi_onPayload',api:b.api!,provider_id:b.provider_id!,model_id:b.model_id!,base_url:b.base_url!,controls:{model:f.p.model.id,stream:true,temperature:f.p.sampling.temperature,top_p:f.p.sampling.top_p,max_tokens:field==='max_tokens'?f.p.model.max_output_tokens:null,max_completion_tokens:field==='max_completion_tokens'?f.p.model.max_output_tokens:null}}],['http_attempt',{attempt,source:'counted_fetch',method:'POST',url:b.request_url!}]];
}
async function resultData(f:Fixture,agent=false){
  const events:[string,Obj][]=[['capture',{profile_digest:f.loaded.digest}],['model_start',{attempt:1}],['model_end',{attempt:1,stop:agent?'tool':'normal',usage:null}]];
  if(agent)events.push(['tool_start',{call_id:'call-1',name:'read',arguments:{path:'input.txt'}}],['tool_end',{call_id:'call-1',status:'completed',text:'read data',truncated:false}],['model_start',{attempt:2}],['model_end',{attempt:2,stop:'normal',usage:null}]);
  if(f.capture.schema==='millforge-capture/v2')for(let i=events.length-1;i>=0;i--)if(events[i]![0]==='model_start'){const attempt=Number(events[i]![1].attempt);events.splice(i+1,0,...observations(f,attempt));}
  events.push(['output_partial',{text:'done'}],['terminal',{status:'completed',code:null}],['closure',closed]);const trace=events.map(([kind,payload],i)=>event(f,i+1,kind,payload));await writeTrace(f.run,trace);const traceBytes=(await fs.stat(path.join(f.run,'trace.jsonl'))).size;
  const result:Obj={schema:f.capture.schema==='millforge-capture/v2'?'millforge-result/v2':'millforge-result/v1',request_id:f.request.request_id,profile_id:f.p.profile_id,profile_digest:f.loaded.digest,mode:f.p.mode,status:'completed',domain_outcome:null,output:'done',diagnostic:null,counts:{model_requests:agent?2:1,tool_calls:agent?1:0,input_bytes:Buffer.byteLength(f.request.task),output_bytes:4,trace_bytes:traceBytes,usage:null,backend_requests:null,cost:null},timing:{started_at:'2026-11-03T00:00:00.000Z',ended_at:'2026-11-03T00:00:01.000Z',elapsed_ms:1000},closure:closed,artifacts:await artifactManifest(f.run,requiredArtifacts(f.loaded)) as unknown as Json};return {result,trace};
}
async function save(f:Fixture,agent=false){const data=await resultData(f,agent);await publishResult(f.run,data.result);return data;}
async function rewriteTrace(f:Fixture,result:Obj,trace:Trace[]){await fs.writeFile(path.join(f.run,'trace.jsonl'),trace.map(t=>JSON.stringify(t)+'\n').join(''));object(result.counts).trace_bytes=(await fs.stat(path.join(f.run,'trace.jsonl'))).size;result.artifacts=await artifactManifest(f.run,requiredArtifacts(f.loaded)) as unknown as Json;await fs.writeFile(path.join(f.run,'result.json'),JSON.stringify(result));}
describe('immutable capture and pure inspect',()=>{
  it('round-trips saved data and stays independent of later authored changes',async()=>{const f=await fixture();await save(f);const inspected=await inspectRun(f.run);expect(inspected.result.status).toBe('completed');expect(inspected.result.counts.usage).toBeNull();f.p.instructions='later authored change';await fs.writeFile(path.join(f.dir,'profile.json'),JSON.stringify(f.p));expect((await inspectRun(f.run)).capture.system_prompt).toBe('Follow the supplied task.');await expect(createCapture(f.run,f.loaded,f.request,f.capture)).rejects.toThrow();let output='';expect(await main(['inspect','--run',f.run],{},s=>output+=s,()=>{})).toBe(0);expect(JSON.parse(output).result.request_id).toBe(f.request.request_id);});
  it.each([{task:'\0'.repeat(262144)},{systemPrompt:'\0'.repeat(2097152),contextBytes:2097152}])('round-trips maximum escaped admitted input/capture text',async(options)=>{const f=await fixture(false,options);await save(f);expect((await inspectRun(f.run)).result.status).toBe('completed');});
  it('never replaces an existing final result',async()=>{const f=await fixture();const {result}=await save(f);const before=await fs.readFile(path.join(f.run,'result.json'));await expect(publishResult(f.run,result)).rejects.toThrow();expect(await fs.readFile(path.join(f.run,'result.json'))).toEqual(before);});
  it('admits a negative completed domain result with strict terminal keys',async()=>{const f=await fixture();const p=f.p;p.terminal={format:'json-object',required_keys:['outcome'],outcome_key:'outcome',outcomes:['accept','reject']};await fs.writeFile(path.join(f.run,'profile/profile.json'),JSON.stringify(p));const current=await loadProfile(path.join(f.run,'profile'));const request={...f.request,profile_digest:current.digest};const capture={...f.capture,profile_digest:current.digest};await fs.writeFile(path.join(f.run,'request.json'),JSON.stringify(request));await fs.writeFile(path.join(f.run,'capture.json'),JSON.stringify(capture));const modified={...f,p,loaded:current,request,capture};const {result}=await resultData(modified);result.output={outcome:'reject'};result.domain_outcome='reject';object(result.counts).output_bytes=20;await publishResult(f.run,result);expect((await inspectRun(f.run)).result.domain_outcome).toBe('reject');});
  it('preserves null failed output and uncertain closure without treating partial text as success',async()=>{const f=await fixture();const {result,trace}=await resultData(f);result.status='failed';result.output=null;result.domain_outcome=null;result.diagnostic={code:'CLOSURE_UNCERTAIN',message:'owned closure not proven'};result.closure={...closed,writers:'may_live',inference:'disconnected_unknown',detached_effects:'unknown'};trace.at(-2)!.payload={status:'failed',code:'CLOSURE_UNCERTAIN'};trace.at(-1)!.payload=object(result.closure);await fs.writeFile(path.join(f.run,'trace.jsonl'),trace.map(t=>JSON.stringify(t)+'\n').join(''));object(result.counts).trace_bytes=(await fs.stat(path.join(f.run,'trace.jsonl'))).size;result.artifacts=await artifactManifest(f.run,requiredArtifacts(f.loaded)) as unknown as Json;await publishResult(f.run,result);expect((await inspectRun(f.run)).result.output).toBeNull();});
  it.each(['identity','unknown','closure','diagnostic','count','timing','output'])('rejects result %s corruption',async(kind)=>{const f=await fixture();const {result}=await save(f);if(kind==='identity')result.request_id='other';if(kind==='unknown')result.extra=true;if(kind==='closure')result.closure={...closed,tools:'may_live'};if(kind==='diagnostic')result.diagnostic={code:'PROVIDER_ERROR',message:'false completion'};if(kind==='count')object(result.counts).model_requests=true;if(kind==='timing')object(result.timing).ended_at='2026-11-02T23:00:00Z';if(kind==='output')result.output=null;await fs.writeFile(path.join(f.run,'result.json'),JSON.stringify(result));await expect(inspectRun(f.run)).rejects.toThrow();});
  it.each(['missing','truncated','hash','unowned','symlink','selfhash'])('rejects saved bytes %s corruption',async(kind)=>{const f=await fixture();const {result}=await save(f);if(kind==='missing')await fs.unlink(path.join(f.run,'result.json'));if(kind==='truncated')await fs.writeFile(path.join(f.run,'trace.jsonl'),'{}');if(kind==='hash')await fs.appendFile(path.join(f.run,'capture.json'),' ');if(kind==='unowned')await fs.writeFile(path.join(f.run,'raw-secret.log'),'must not be adopted');if(kind==='symlink'){await fs.unlink(path.join(f.run,'request.json'));await fs.symlink(path.join(f.run,'capture.json'),path.join(f.run,'request.json'));}if(kind==='selfhash'){(result.artifacts as Json[]).push({path:'result.json',size:1,sha256:'a'.repeat(64),media_type:'application/json'});await fs.writeFile(path.join(f.run,'result.json'),JSON.stringify(result));}await expect(inspectRun(f.run)).rejects.toThrow();});
  it.each(['sequence','terminal','finish','pending','duplicate','orphan','nonstring','inside-model','after-normal'])('rejects semantic trace %s after recomputed artifact hashes',async(kind)=>{const f=await fixture(true);const {result,trace}=await save(f,true);if(kind==='sequence')trace[2]!.sequence=20;if(kind==='terminal')trace.at(-2)!.payload.status='failed';if(kind==='finish')trace[6]!.payload.stop='length';if(kind==='pending')trace.splice(6,1);if(kind==='duplicate')trace.splice(5,0,{...trace[3]!});if(kind==='orphan')trace[4]!.payload.call_id='different';if(kind==='nonstring')trace[3]!.payload.name=['read'];if(kind==='inside-model'){const ended=trace.splice(2,1)[0]!;trace.splice(4,0,ended);}if(kind==='after-normal')trace[2]!.payload.stop='normal';trace.forEach((t,i)=>{if(kind!=='sequence')t.sequence=i+1;});await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();});
  it('strictly validates supplied public compatibility fields without resolving defaults',async()=>{const f=await fixture();for(const bad of [{secret:'value'},{supportsStore:'yes'},{openRouterRouting:{only:[false]}},{chatTemplateArgs:{x:{$var:'other'}}}]){const c={...f.capture,binding:{...f.capture.binding,compat:{...f.p.model.compat,...bad}}};expect(()=>validateCapture(c,f.p,f.request,f.loaded.digest)).toThrow();}expect(()=>validateCapture({...f.capture,binding:{...f.capture.binding,compat:{...f.p.model.compat,supportsStore:false,chatTemplateKwargs:{enable:false,thinking:{$var:'thinking.enabled',omitWhenOff:true}},openRouterRouting:{allow_fallbacks:false,sort:{by:'price',partition:null}}}}},f.p,f.request,f.loaded.digest)).not.toThrow();});
  it('requires a selected string tool identity rather than coercing arrays',async()=>{const f=await fixture(true);expect(()=>validateTrace(event(f,1,'tool_start',{call_id:'call',name:['read'],arguments:{}}),f.p,f.request)).toThrow();});
});

describe('versioned public observation records',()=>{
  it.each([false,true])('round-trips v2 with authored intent separate from actual public observations (Anthropic=%s)',async(anthropic)=>{
    const f=await fixture(false,{v2:true,anthropic});const before=await fs.readFile(path.join(f.run,'capture.json'));await save(f);
    const inspected=await inspectRun(f.run);expect(inspected.result.schema).toBe('millforge-result/v2');expect(object(inspected.capture.binding.compat).internal_resolution).toBe('unknown');
    expect(await fs.readFile(path.join(f.run,'capture.json'))).toEqual(before);
    expect(inspected.capture.binding).not.toHaveProperty('observations');expect(inspected.trace_records).toBe(8);
  });
  it('round-trips an explicit OpenAI top-p control without treating it as a default',async()=>{const f=await fixture(false,{v2:true,topP:0.75});await save(f);expect((await inspectRun(f.run)).capture.binding.requested_options).toMatchObject({top_p:0.75});});
  it('retains sequential agent/tool rules with an observation pair for each attempt',async()=>{const f=await fixture(true,{v2:true});await save(f,true);expect((await inspectRun(f.run)).result.counts.model_requests).toBe(2);});
  it.each(['resolved','extra-overrides','observed-label','wrong-model','wrong-base','outside-origin','outside-path','unknown-option','wrong-version'])('refuses v2 capture %s corruption before creating a destination',async(kind)=>{
    const f=await fixture(false,{v2:true});const c=structuredClone(f.capture);const b=c.binding;
    if(kind==='resolved')object(b.compat).internal_resolution={value:{supportsStore:false}};
    if(kind==='extra-overrides')object(object(b.compat).authored_overrides).supportsStore=false;
    if(kind==='observed-label')object(b.compat).observed=true;
    if(kind==='wrong-model')b.model_id='other';if(kind==='wrong-base')b.base_url='https://other.test';
    if(kind==='outside-origin')b.request_url='https://other.test/chat/completions';if(kind==='outside-path')b.request_url=new URL('/outside',String(b.base_url)).href;
    if(kind==='unknown-option')object(b.requested_options).raw_payload='secret';if(kind==='wrong-version')b.version=2;
    const dest=path.join(f.root,'refused');await expect(createCapture(dest,f.loaded,f.request,c)).rejects.toThrow();await expect(fs.access(dest)).rejects.toThrow();
  });
  it.each(['capture-version','result-version','trace-version','unknown-trace-version'])('refuses mixed or unsupported %s at the first producer/inspect boundary',async(kind)=>{
    const f=await fixture(false,{v2:true});const {result,trace}=await save(f);
    if(kind==='capture-version'){const capture={...f.capture,schema:'millforge-capture/v1'};await fs.writeFile(path.join(f.run,'capture.json'),JSON.stringify(capture));}
    if(kind==='result-version')result.schema='millforge-result/v1';
    if(kind==='trace-version')trace[0]!.schema='millforge-trace/v1';if(kind==='unknown-trace-version')Object.assign(trace[0]!,{schema:'millforge-trace/v99'});
    if(kind==='capture-version')await expect(rewriteTrace(f,result,trace)).rejects.toThrow();else await rewriteTrace(f,result,trace);
    await expect(inspectRun(f.run)).rejects.toThrow();
  });
  it.each(['provenance','payload-identity','attempt','url','method','duplicate-payload','duplicate-http','http-first','missing-payload','missing-http','after-terminal','extra-control','missing-control','bool-control','temperature','token-field','stream'])('rejects v2 observation %s corruption after hashes are recomputed',async(kind)=>{
    const f=await fixture(false,{v2:true});const {result,trace}=await save(f);const payload=trace.find(t=>t.kind==='provider_payload')!,http=trace.find(t=>t.kind==='http_attempt')!,controls=object(payload.payload.controls);
    if(kind==='provenance')payload.payload.source='private_resolver';if(kind==='payload-identity')payload.payload.provider_id='other';if(kind==='attempt')http.payload.attempt=2;
    if(kind==='url')http.payload.url=String(f.capture.binding.request_url)+'/other';if(kind==='method')http.payload.method='GET';
    if(kind==='duplicate-payload')trace.splice(3,0,structuredClone(payload));if(kind==='duplicate-http')trace.splice(4,0,structuredClone(http));
    if(kind==='http-first'){const i=trace.indexOf(payload);trace[i]=http;trace[i+1]=payload;}
    if(kind==='missing-payload')trace.splice(trace.indexOf(payload),1);if(kind==='missing-http')trace.splice(trace.indexOf(http),1);
    if(kind==='after-terminal'){trace.splice(trace.indexOf(http),1);trace.splice(trace.length-1,0,http);}
    if(kind==='extra-control')controls.messages=['secret prompt'];if(kind==='missing-control')delete controls.stream;if(kind==='bool-control')controls.temperature=true;if(kind==='temperature')controls.temperature=0.99;
    if(kind==='token-field'){controls.max_tokens=null;controls.max_completion_tokens=null;}if(kind==='stream')controls.stream=false;
    trace.forEach((t,i)=>{t.sequence=i+1;t.elapsed_ms=i+1;});await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();
  });
  it('accepts the pinned Anthropic beta resource endpoint without changing the query-free base URL',async()=>{
    const f=await fixture(false,{v2:true,anthropic:true});expect(f.capture.binding.request_url).toBe(f.p.model.base_url+'/v1/messages?beta=true');expect(f.p.model.base_url).not.toContain('?');await save(f);expect((await inspectRun(f.run)).capture.binding.request_url).toBe(f.p.model.base_url+'/v1/messages?beta=true');
  });
  it.each(['','?beta=false','?beta=True','?beta=true&key=secret','?beta=true&beta=true','?%62eta=true','?beta=%74rue','?beta=true#fragment'])('refuses changed, extra, duplicate or encoded Anthropic request query %s',async(query)=>{
    const f=await fixture(false,{v2:true,anthropic:true});const capture=structuredClone(f.capture);capture.binding.request_url=f.p.model.base_url+'/v1/messages'+query;expect(()=>validateCapture(capture,f.p,f.request,f.loaded.digest)).toThrow();
  });
  it('rejects an observed Anthropic query different from the captured fixed endpoint',async()=>{
    const f=await fixture(false,{v2:true,anthropic:true});const {result,trace}=await save(f);trace.find(t=>t.kind==='http_attempt')!.payload.url=f.p.model.base_url+'/v1/messages?beta=false';await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();
  });
  it('keeps base URLs and OpenAI request URLs query-free',async()=>{
    const f=await fixture(false,{v2:true});const capture=structuredClone(f.capture);capture.binding.request_url=String(capture.binding.request_url)+'?beta=true';expect(()=>validateCapture(capture,f.p,f.request,f.loaded.digest)).toThrow();
    const authored=structuredClone(f.p);authored.model.base_url+='?beta=true';expect(()=>validateProfile(authored)).toThrow();
  });
  it.each([false,true])('preserves a lower positive observed token ceiling (Anthropic=%s)',async(anthropic)=>{
    const f=await fixture(false,{v2:true,anthropic});const {result,trace}=await resultData(f);const payload=trace.find(t=>t.kind==='provider_payload')!;const field=f.p.model.api==='openai-completions'?String(f.p.model.compat.maxTokensField):'max_tokens';
    object(payload.payload.controls)[field]=128;await rewriteTrace(f,result,trace);await fs.unlink(path.join(f.run,'result.json'));await publishResult(f.run,result);
    expect((await inspectRun(f.run)).result.status).toBe('completed');const saved=(await fs.readFile(path.join(f.run,'trace.jsonl'),'utf8')).trim().split('\n').map(line=>object(parseJson(line)));expect(object(object(saved.find(t=>t.kind==='provider_payload')!.payload).controls)[field]).toBe(128);
  });
  it.each([0,-1,1.5,true,'128','widen'])('refuses invalid or widened observed token ceiling %s',async(value)=>{
    const f=await fixture(false,{v2:true});const {result,trace}=await save(f);const payload=trace.find(t=>t.kind==='provider_payload')!;const field=String(f.p.model.compat.maxTokensField);object(payload.payload.controls)[field]=value==='widen'?f.p.model.max_output_tokens+1:value;
    await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();
  });
  it.each(['none','payload','http'])('accepts a failed prefix without inventing observations: %s',async(prefix)=>{
    const f=await fixture(false,{v2:true});const {result}=await resultData(f);const trace=[event(f,1,'capture',{profile_digest:f.loaded.digest}),event(f,2,'model_start',{attempt:1}),event(f,3,'model_end',{attempt:1,stop:'error',usage:null}),event(f,4,'terminal',{status:'failed',code:'INCOMPATIBLE_ROUTE'}),event(f,5,'closure',closed)];
    if(prefix!=='none'){const observationsForAttempt=observations(f,1);trace.splice(2,0,...observationsForAttempt.slice(0,prefix==='payload'?1:2).map(([kind,payload],i)=>event(f,i+3,kind,payload)));trace.forEach((t,i)=>{t.sequence=i+1;t.elapsed_ms=i+1;});}
    result.status='failed';result.output=null;result.diagnostic={code:'INCOMPATIBLE_ROUTE',message:'public binding observation failed'};object(result.counts).output_bytes=0;object(result.counts).backend_requests=null;
    await rewriteTrace(f,result,trace);await fs.unlink(path.join(f.run,'result.json'));await publishResult(f.run,result);expect((await inspectRun(f.run)).result.status).toBe('failed');
  });
  it('refuses a second generate HTTP attempt and any nonnull backend count',async()=>{
    const f=await fixture(false,{v2:true});const {result,trace}=await save(f);for(const count of [0,1,2]){object(result.counts).backend_requests=count;await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();}
    object(result.counts).backend_requests=null;trace.splice(4,0,structuredClone(trace[3]!));trace.forEach((t,i)=>{t.sequence=i+1;t.elapsed_ms=i+1;});await rewriteTrace(f,result,trace);await expect(inspectRun(f.run)).rejects.toThrow();
  });
  it('counts observation bytes against the unchanged trace ceiling',async()=>{
    const f=await fixture(false,{v2:true,traceBytes:1});const events=[event(f,1,'capture',{profile_digest:f.loaded.digest}),event(f,2,'model_start',{attempt:1}),...observations(f,1).map(([kind,payload],i)=>event(f,i+3,kind,payload))];
    await expect(writeTrace(f.run,events)).rejects.toThrow(/trace limit/);await expect(fs.access(path.join(f.run,'trace.jsonl'))).rejects.toThrow();
  });
});

describe('incremental bounded capture production',()=>{
  it('preserves the terminal reserve and closes its writer once',async()=>{const {TraceWriter}=await import('../src/artifacts.js');const f=await fixture(false,{v2:true,traceBytes:18000});const writer=new TraceWriter(f.run,{profile:f.loaded,request:f.request,capture:f.capture},performance.now());writer.append('capture',{profile_digest:f.loaded.digest});expect(()=>writer.append('output_partial',{text:'x'.repeat(2000)})).toThrow();writer.append('terminal',{status:'failed',code:'TRACE_LIMIT'});writer.append('closure',closed);writer.close();writer.close();expect(()=>writer.append('cancel',{signal:'internal'})).toThrow();const raw=await fs.readFile(path.join(f.run,'trace.jsonl'),'utf8');expect(raw).not.toContain('x'.repeat(2000));expect(raw.trim().split('\n').map(line=>JSON.parse(line).kind)).toEqual(['capture','terminal','closure']);expect(Buffer.byteLength(raw)).toBe(writer.size);});
});
