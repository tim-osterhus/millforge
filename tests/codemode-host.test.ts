import {afterEach,describe,expect,it,vi} from 'vitest';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {tmpdir} from 'node:os';
import {Worker} from 'node:worker_threads';
import {type Obj,type ProfileV3,type Trace,MillforgeError,canonical,codemodeConstants,codemodeLoader,codemodeOptions,parseJson,validateProfile,object} from '../src/contracts.js';
import {CodemodeGuard,assertCodemodeExtensions,assertCodemodeRoots,codemodeDeclarations,materializeCodemode} from '../src/codemode-host.js';
import {executeWorker,nativeTruncated,memoryCredentials} from '../src/pi-host.js';
import {artifactManifest,inspectRun,publishResult,requiredArtifacts} from '../src/artifacts.js';
import {codemodeDeclaration,createBinding} from '../src/bindings.js';
import {hash,loadProfile} from '../src/profile.js';
import {ownedAuthHome} from '../src/auth.js';
import {executableIdentity} from '../src/lifecycle.js';
const roots:string[]=[],tempBase=tmpdir();
afterEach(async()=>{vi.restoreAllMocks();vi.unstubAllEnvs();for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});});
async function root(){const dir=await fs.mkdtemp(path.join(tempBase,'millforge-codemode-'));roots.push(dir);return dir;}
async function profile():Promise<ProfileV3>{return validateProfile(parseJson(await fs.readFile(new URL('./fixtures/codemode/profile.json',import.meta.url)))) as ProfileV3;}
type Step={code?:string;args?:unknown;name?:string;id?:string;text?:string};
function sse(api:string,step:Step,index:number,grammar=false):Response{
  const call=step.code!==undefined||step.args!==undefined,tool=step.name??'codemode',id=step.id??'call_'+index;
  const args=step.args??{code:step.code},events:unknown[]=[];
  if(api==='openai-completions'){
    events.push({id:'c',model:'codemode-fixture',choices:[{index:0,delta:call?{tool_calls:[grammar&&step.code!==undefined?{index:0,id,type:'custom',custom:{name:tool,input:step.code}}:{index:0,id,type:'function',function:{name:tool,arguments:JSON.stringify(args)}}]}:{role:'assistant',content:step.text??'done'},finish_reason:null}]},{id:'c',model:'codemode-fixture',choices:[{index:0,delta:{},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:20,completion_tokens:5}});
  }else if(api==='anthropic-messages'){
    events.push({type:'message_start',message:{id:'a',type:'message',role:'assistant',model:'codemode-fixture',content:[],stop_reason:null,usage:{input_tokens:20,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0}}});
    if(call)events.push({type:'content_block_start',index:0,content_block:{type:'tool_use',id,name:tool,input:{}}},{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(args)}});
    else events.push({type:'content_block_start',index:0,content_block:{type:'text',text:''}},{type:'content_block_delta',index:0,delta:{type:'text_delta',text:step.text??'done'}});
    events.push({type:'content_block_stop',index:0},{type:'message_delta',delta:{stop_reason:call?'tool_use':'end_turn',stop_sequence:null},usage:{output_tokens:5}},{type:'message_stop'});
  }else{
    const item=call?(grammar&&step.code!==undefined?{type:'custom_tool_call',id:'ctc_'+index,call_id:id,name:tool,input:step.code,status:'completed'}:{type:'function_call',id:'fc_'+index,call_id:id,name:tool,arguments:JSON.stringify(args),status:'completed'}):{type:'message',id:'msg_'+index,role:'assistant',phase:'final_answer',status:'completed',content:[{type:'output_text',text:step.text??'done',annotations:[]}]};
    events.push({type:'response.created',response:{id:'r_'+index}},{type:'response.output_item.added',output_index:0,item},{type:'response.output_item.done',output_index:0,item},{type:'response.completed',response:{id:'r_'+index,status:'completed',model:'gpt-6-luna',output:[item],usage:{input_tokens:20,output_tokens:5}}});
  }
  return new Response(events.map(e=>{const type=(e as {type?:string}).type;return (type?'event: '+type+'\n':'')+'data: '+JSON.stringify(e)+'\n\n';}).join('')+(api==='openai-completions'?'data: [DONE]\n\n':''),{headers:{'content-type':'text/event-stream'}});
}
async function run(steps:Step[],options:{api?:'openai-completions'|'openai-responses'|'anthropic-messages';grammar?:boolean;change?:(p:ProfileV3)=>void;atRequest?:(index:number,body:Obj,runDir:string)=>Promise<void>|void;signal?:AbortSignal;deadlineMs?:number;external?:'fd'|'rg';save?:boolean;wireDrift?:boolean}={}){
  const dir=await root(),authored=path.join(dir,'profile'),out=path.join(dir,'run');await fs.mkdir(authored);await fs.writeFile(path.join(dir,'input.txt'),'native contents');
  const p=await profile();p.model.api=options.api??'openai-completions';p.model.compat=p.model.api==='anthropic-messages'?{}:{supportsOpenAIGrammarTools:options.grammar??true,...(p.model.api==='openai-completions'?{supportsFinishReason:true,maxTokensField:'max_tokens'}:{})};
  let authHome:string|undefined;
  if(p.model.api==='openai-responses'){
    p.model.compat={...p.model.compat,supportsAdditionalTools:false,supportsToolSearch:false};p.model.provider='openai';p.model.id='gpt-6-luna';p.model.base_url='https://api.openai.com/v1';p.model.auth={kind:'oauth',provider:'openai'};p.model.metadata={source:'pinned-catalog',overrides:{}};p.model.context_tokens=272000;p.model.max_output_tokens=null;
    authHome=path.join(dir,'auth');await ownedAuthHome(authHome,true);await fs.writeFile(path.join(authHome,'auth.json'),JSON.stringify({openai:{type:'oauth',access:'CODEMODE_SYNTHETIC_ACCESS',refresh:'CODEMODE_SYNTHETIC_REFRESH',expires:Date.now()+3600000,clientId:'fixture',scopes:['chatgpt.tokens.use.direct']}}),{mode:0o600});
  }
  const externalTools=[];if(options.external){const bin=path.join(dir,'bin');await fs.mkdir(bin);await fs.writeFile(path.join(bin,options.external),'#!/bin/sh\nprintf original\n',{mode:0o700});vi.stubEnv('PATH',bin+path.delimiter+(process.env.PATH??''));externalTools.push(await executableIdentity(options.external,process.env.PATH!));p.tools=['write',options.external==='fd'?'find':'grep'];}
  options.change?.(p);await fs.writeFile(path.join(authored,'profile.json'),JSON.stringify(p));const loaded=await loadProfile(authored);
  for(const [key,value]of Object.entries({PI_OFFLINE:'1',PI_CODEMODE_MAX_WORKERS:'1',PI_CODING_AGENT_DIR:path.join(out,'pi'),HOME:path.join(out,'pi'),TMPDIR:path.join(out,'tmp'),TMP:path.join(out,'tmp'),TEMP:path.join(out,'tmp')}))vi.stubEnv(key,value);
  const bodies:Obj[]=[],wire:Obj[]=[];let requests=0;
  vi.spyOn(globalThis,'fetch').mockImplementation(async(_input,init)=>{const body=parseJson(String(init?.body)) as Obj;requests++;bodies.push(body);wire.push(codemodeDeclaration(body,p.model.api));await options.atRequest?.(requests,body,out);return sse(p.model.api,steps[requests-1]??{text:'done'},requests,wire.at(-1)!.format==='grammar');});
  if(options.wireDrift){const stringify=JSON.stringify;vi.spyOn(JSON,'stringify').mockImplementation((value,replacer,space)=>{if(value&&typeof value==='object'&&value.model===p.model.id&&value.stream===true&&Array.isArray(value.tools)){value={...value,tools:value.tools.map((tool:Obj)=>{const body=p.model.api==='openai-completions'?object(tool.custom??tool.function):tool;if(body.name!=='codemode')return tool;const altered=body.format?{...body,format:{...object(body.format),...(p.model.api==='openai-completions'?{grammar:{...object(object(body.format).grammar),definition:'changed'}}:{definition:'changed'})}}:{...body,[p.model.api==='anthropic-messages'?'input_schema':'parameters']:{type:'object',properties:{changed:{type:'string'}}}};return p.model.api==='openai-completions'?{...tool,[tool.type==='custom'?'custom':'function']:altered}:altered;})};}return stringify(value,replacer,space);});}
  const reply=await executeWorker({dir:out,profileDir:authored,request:{schema:'millforge-request/v1',request_id:'codemode',profile_id:p.profile_id,profile_digest:loaded.digest,expected_mode:'agent',task:'finite task',context:[],workspace:dir,limits:{},correlation:{}},tools:externalTools,...(authHome?{authHome}:{}),deadlineNs:(process.hrtime.bigint()+BigInt((options.deadlineMs??7000)*1e6)).toString(),startedAt:new Date().toISOString()},options.signal??new AbortController().signal);
  const trace=(await fs.readFile(path.join(out,'trace.jsonl'),'utf8')).trim().split('\n').map(s=>JSON.parse(s) as Trace),capture=JSON.parse(await fs.readFile(path.join(out,'capture.json'),'utf8'));
  // Offline reader proof after the in-process session has closed. Launcher proof is separate.
  if(options.save!==false){await fs.rm(path.join(out,'pi'),{recursive:true});if(!reply.spills.length)await fs.rmdir(path.join(out,'tmp'));
  reply.draft.artifacts=await artifactManifest(out,[...requiredArtifacts(loaded),...reply.spills]);reply.draft.timing.elapsed_ms=10000;
  await publishResult(out,reply.draft);await inspectRun(out);}return {dir,out,p,reply,trace,capture,bodies,wire,requests};
}
function textResults(f:Awaited<ReturnType<typeof run>>):string{return f.trace.filter(t=>t.kind==='tool_end').map(t=>t.payload.text).join('\n');}
describe('codemode actual public Pi and QuickJS',()=>{
  it.each([['openai-completions',true],['openai-completions',false],['anthropic-messages',false],['openai-responses',true],['openai-responses',false]] as const)('captures effective declarations and exact %s grammar=%s wire member',async(api,grammar)=>{
    const f=await run([{code:'text(await tools.read({path:"input.txt"}));'},{text:'done'}],{api,grammar});expect(f.reply.draft,JSON.stringify({diagnostic:f.reply.draft.diagnostic,ends:f.trace.filter(t=>t.kind==='model_end'),requests:f.requests})).toMatchObject({schema:'millforge-result/v4',status:'completed',counts:{model_requests:2,tool_calls:2,nested_tool_calls:1}});
    expect(f.capture.capability).toMatchObject({fixed_options:codemodeOptions,pinned_constants:codemodeConstants,loader:codemodeLoader,hidden_declarations:[]});expect(f.capture.tool_names).toEqual(f.capture.declared_tools.map((t:{name:string})=>t.name));expect(f.capture.callable_tool_names.sort()).toEqual(['bash','read','write']);
    const pi=await import('@earendil-works/pi-coding-agent');expect(f.capture.declared_tools.find((t:{name:string})=>t.name==='read').description_sha256).not.toBe(hash(pi.createReadTool(f.dir).description));
    const projections=f.trace.filter(t=>t.kind==='provider_payload').map(t=>t.payload.codemode_declaration);expect(projections).toEqual(f.wire);expect(projections[0]).toMatchObject({format:api==='anthropic-messages'||!grammar?'json-schema':'grammar'});
    const declaration=(f.bodies[0]!.tools as Obj[]).find(t=>(api==='openai-completions'?((t.custom??t.function) as Obj).name:t.name)==='codemode')!;
    const member=api==='anthropic-messages'?declaration.input_schema:api==='openai-completions'?grammar?((declaration.custom as Obj).format as Obj).grammar:(declaration.function as Obj).parameters:grammar?declaration.format:declaration.parameters;
    expect((projections[0] as Obj)[api!=='anthropic-messages'&&grammar?'grammar_sha256':'schema_sha256']).toBe(hash(canonical(member!)));
    expect(textResults(f)).toContain('native contents');expect(f.trace.filter(t=>t.kind==='nested_join').map(t=>t.payload)).toEqual([{parent_call_id:api==='openai-responses'?'call_1|'+(grammar?'ctc_1':'fc_1'):'call_1',started:1,ended:1,outstanding:0}]);
    expect(JSON.stringify(f.trace)).not.toMatch(/CODEMODE_SYNTHETIC|"input_schema"|"definition"/);
  });
  it('refuses Responses transcript-only declarations before HTTP without changing native defaults',async()=>{const f=await run([{code:'text(1);'}],{api:'openai-responses',change:p=>{p.model.compat={supportsOpenAIGrammarTools:true};}});expect(f.reply.draft.diagnostic?.code).toBe('INCOMPATIBLE_ROUTE');expect(f.requests).toBe(0);expect(f.trace.some(t=>t.kind==='http_attempt')).toBe(false);});
  it.each([['openai-completions',true],['openai-completions',false],['anthropic-messages',false],['openai-responses',true],['openai-responses',false]] as const)('refuses serialized declaration drift for %s grammar=%s before HTTP',async(api,grammar)=>{const f=await run([{code:'text(1);'}],{api,grammar,wireDrift:true});expect(f.reply.draft.diagnostic?.code).toBe('INCOMPATIBLE_ROUTE');expect(f.requests).toBe(0);expect(f.trace.filter(t=>t.kind==='provider_payload')).toHaveLength(1);expect(f.trace.some(t=>t.kind==='http_attempt')).toBe(false);});
  it('keeps models, absent tools, evaluator and nested codemode out of every script discovery surface',async()=>{
    const f=await run([{code:'text(typeof models); text(ALL_TOOLS); text(await searchTools("write")); for (const n of ["write","tool_search","codemode","evaluate"]) { try { text(await describeTool(n)); } catch (e) {text(String(e));} } text(typeof tools.write); text(typeof tools.codemode);'}],{change:p=>{p.tools=['read'];}});
    expect(f.reply.draft.status).toBe('completed');expect(f.requests).toBe(2);expect(f.capture.tool_names).toEqual(['read','codemode']);expect(textResults(f)).toContain('undefined');expect(f.trace.filter(t=>t.kind==='tool_start')).toHaveLength(1);await expect(fs.lstat(path.join(f.dir,'effect'))).rejects.toThrow();
  });
  it('allows several queued starts, executes effects sequentially, and charges wrapper and children once',async()=>{
    const f=await run([{code:'await Promise.all([tools.bash({command:"sleep 0.05; printf first > order"}),tools.bash({command:"cat order; printf second >> order"}),tools.read({path:"order"})]);'}]);
    expect(f.reply.draft.counts).toMatchObject({tool_calls:4,nested_tool_calls:3});expect(await fs.readFile(path.join(f.dir,'order'),'utf8')).toBe('firstsecond');const starts=f.trace.filter(t=>t.kind==='tool_start');expect(starts.map(t=>t.payload.call_id)).toEqual(['call_1','call_1/1','call_1/2','call_1/3']);expect(f.trace.findIndex(t=>t.payload.call_id==='call_1/3'&&t.kind==='tool_start')).toBeLessThan(f.trace.findIndex(t=>t.payload.call_id==='call_1/1'&&t.kind==='tool_end'));
  });
  it('drains observed over-limit children without tracing or executing them and preserves the first cause',async()=>{
    const f=await run([{code:'await Promise.allSettled([tools.write({path:"allowed",content:"yes"}),tools.write({path:"forbidden",content:"no"})]);'}],{change:p=>{p.limits.tool_calls=2;}});
    expect(f.reply.draft.diagnostic?.code).toBe('LIMIT_EXHAUSTED');expect(f.reply.draft.counts).toMatchObject({tool_calls:2,nested_tool_calls:1});expect(f.trace.filter(t=>t.kind==='nested_join').map(t=>t.payload)).toEqual([{parent_call_id:'call_1',started:1,ended:1,outstanding:0}]);expect(f.trace.some(t=>t.payload.call_id==='call_1/2')).toBe(false);await expect(fs.lstat(path.join(f.dir,'forbidden'))).rejects.toThrow();
  });
  it.each([{code:1},{code:null},{}])('records one zero-child join when argument validation skips execution: %j',async args=>{const f=await run([{args}],{grammar:false});expect(f.reply.draft.status,JSON.stringify(f.reply.draft.diagnostic)).toBe('completed');expect(f.trace.filter(t=>t.kind==='nested_join').map(t=>t.payload)).toEqual([{parent_call_id:'call_1',started:0,ended:0,outstanding:0}]);});
  it('emits a zero-child join when cancellation arrives after the public parent start and before execution',async()=>{const abort=new AbortController(),event=CodemodeGuard.prototype.event;vi.spyOn(CodemodeGuard.prototype,'event').mockImplementation(function(this:CodemodeGuard,value){event.call(this,value);if(value.type==='tool_execution_start'&&value.parentToolCallId===undefined)abort.abort();});const f=await run([{code:'await tools.write({path:"forbidden",content:"no"});'}],{signal:abort.signal});expect(f.reply.draft.diagnostic?.code).toBe('CANCELLED');expect(f.trace.find(t=>t.kind==='nested_join')?.payload).toMatchObject({started:0,ended:0,outstanding:0});await expect(fs.lstat(path.join(f.dir,'forbidden'))).rejects.toThrow();});
  it('rejects oversized script arguments before admission and drains the actual end without a fabricated start',async()=>{const f=await run([{code:'text("'+ 'x'.repeat(17000)+'");'}]);expect(f.reply.draft.diagnostic?.code).toBe('OUTPUT_LIMIT');expect(f.reply.draft.counts).toMatchObject({tool_calls:0,nested_tool_calls:0});expect(f.trace.some(t=>t.kind==='tool_start'||t.kind==='tool_end')).toBe(false);});
  it('drains oversized nested arguments without executing or tracing the child',async()=>{const f=await run([{code:'await tools.write({path:"forbidden",content:"x".repeat(17000)});'}]);expect(f.reply.draft.diagnostic?.code).toBe('OUTPUT_LIMIT');expect(f.reply.draft.counts).toMatchObject({tool_calls:1,nested_tool_calls:0});expect(f.trace.find(t=>t.kind==='nested_join')?.payload).toMatchObject({started:0,ended:0,outstanding:0});await expect(fs.lstat(path.join(f.dir,'forbidden'))).rejects.toThrow();});
  it('admits missing script timeout and lets a shorter script timeout fail one tool before a normal finish',async()=>{
    const f=await run([{code:'text("no timeout");'},{code:'// @options: {"timeout_ms":20}\nwhile(true){}'},{text:'done'}]);expect(f.reply.draft.status).toBe('completed');expect(textResults(f)).toContain('Script timed out');expect(f.trace.filter(t=>t.kind==='tool_end').map(t=>t.payload.status)).toEqual(['completed','failed']);
  });
  it.each(['while(true){}','await Promise.all([tools.bash({command:"sleep 20"}),tools.write({path:"late",content:"no"})]);'])('aborts original parent deadline including nested queue time: %s',async code=>{
    const f=await run([{code}],{change:p=>{p.limits.tool_timeout_seconds=1;}});expect(f.reply.draft.diagnostic?.code).toBe('TIMEOUT');expect(f.reply.draft.closure.tools).toBe('closed');expect(f.trace.filter(t=>t.kind==='nested_join').every(t=>t.payload.outstanding===0)).toBe(true);await expect(fs.lstat(path.join(f.dir,'late'))).rejects.toThrow();
  },10000);
  it('aborts an actual script waiting for the one worker at its original parent deadline',async()=>{
    const dir=await root();await fs.mkdir(path.join(dir,'bin'));await fs.mkdir(path.join(dir,'codemode-loader'));vi.stubEnv('PI_CODEMODE_MAX_WORKERS','1');
    const pi=await import('@earendil-works/pi-coding-agent'),f=guardFixture(),loader=await materializeCodemode(pi,dir,f.guard),p=await profile();
    const binding=await createBinding({profile:p,profileDigest:'a'.repeat(64),limits:p.limits,signal:new AbortController().signal,deadline:performance.now()+7000,onTrace:()=>{}}),runtime=await pi.ModelRuntime.create({credentials:memoryCredentials(),modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});runtime.registerNativeProvider(binding.provider);
    const {session}=await pi.createAgentSession({cwd:dir,agentDir:dir,modelRuntime:runtime,model:binding.model,tools:[...p.tools,'codemode'],resourceLoader:loader,sessionManager:pi.SessionManager.inMemory(dir),settingsManager:pi.SettingsManager.inMemory({retry:{enabled:false},compaction:{enabled:false},cacheWarming:'off'},{projectTrusted:false})});
    const definition=session.agent.state.tools.find(t=>t.name==='codemode')!;
    const controller=new AbortController();let ready!:()=>void;const started=new Promise<void>(resolve=>{ready=resolve;});
    const holder=definition.execute('holder',{code:'while(true){}'},controller.signal,()=>ready());
    await started;
    try{const candidate=await run([{code:'text("must not execute");'}],{change:p=>{p.limits.tool_timeout_seconds=1;}});expect(candidate.reply.draft.diagnostic?.code).toBe('TIMEOUT');expect(textResults(candidate)).toContain('The script did not start.');expect(textResults(candidate)).not.toContain('must not execute');expect(candidate.trace.find(t=>t.kind==='nested_join')?.payload.outstanding).toBe(0);}finally{controller.abort();await holder;session.dispose();f.guard.dispose();}
  },10000);
  it('joins cancellation and preserves partial effects without a later queued effect',async()=>{
    const abort=new AbortController();const timer=setTimeout(()=>abort.abort(),700);
    try{const f=await run([{code:'await tools.write({path:"partial",content:"kept"}); await tools.bash({command:"sleep 20"});'}],{signal:abort.signal});expect(f.reply.draft.diagnostic?.code).toBe('CANCELLED');expect(f.reply.draft.closure.tools).toBe('closed');expect(f.trace.find(t=>t.kind==='nested_join')?.payload.outstanding).toBe(0);expect(await fs.readFile(path.join(f.dir,'partial'),'utf8')).toBe('kept');}finally{clearTimeout(timer);}
  },10000);
  it('joins actual worker-thread exit as a failed tool and allows the session to finish normally',async()=>{
    const emit=Worker.prototype.emit;let terminated=0;vi.spyOn(Worker.prototype,'emit').mockImplementation(function(this:Worker,event:string|symbol,...args:unknown[]){const result=emit.call(this,event,...args);if(event==='online'){terminated++;void this.terminate();}return result;});
    const f=await run([{code:'while(true){}'}]);expect(terminated).toBe(1);expect(f.reply.draft.status).toBe('completed');expect(f.trace.find(t=>t.kind==='tool_end')?.payload.status).toBe('failed');expect(f.trace.find(t=>t.kind==='nested_join')?.payload.outstanding).toBe(0);expect(textResults(f)).toContain('Script');
  });
  it('joins an unawaited nested effect before parent end and retains its actual status',async()=>{
    const f=await run([{code:'tools.bash({command:"sleep 0.1; printf partial > partial"}); text("returning");'}]);expect(f.reply.draft.status).toBe('completed');const join=f.trace.findIndex(t=>t.kind==='nested_join');expect(f.trace[join+1]?.kind).toBe('tool_end');expect(f.trace[join]?.payload.outstanding).toBe(0);expect(f.trace.slice(join+1).some(t=>t.payload.parent_call_id==='call_1')).toBe(false);
  });
  it('retains partial effects after script failure and starts each new invocation with an empty store',async()=>{
    const one=await run([{code:'store("key",{value:42}); text("stored");'},{code:'text(load("key")); await tools.write({path:"partial",content:"kept"}); throw new Error("after effect");'},{text:'done'}]);expect(one.reply.draft.status).toBe('completed');expect(textResults(one)).toContain('42');expect(await fs.readFile(path.join(one.dir,'partial'),'utf8')).toBe('kept');
    const two=await run([{code:'text(load("key"));'}]);expect(textResults(two)).toContain('undefined');expect(await fs.readdir(two.out)).not.toContain('codemode-store');
  });
  it('captures actual fullOutputPath truncation and charges its full spill plus retained parent text',async()=>{
    const f=await run([{code:'// @options: {"max_output_tokens":100}\ntext("x".repeat(10000));'}]);expect(f.reply.draft.status).toBe('completed');expect(f.reply.spills).toHaveLength(1);expect(f.trace.find(t=>t.kind==='tool_end')?.payload.truncated).toBe(true);expect((await fs.readFile(path.join(f.out,f.reply.spills[0]!))).length).toBe(10000);expect(f.reply.draft.artifacts.find(a=>a.path===f.reply.spills[0])?.media_type).toBe('text/plain');
  });
  it('accounts image spills as octet streams alongside retained text',async()=>{
    const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l4kAAAAASUVORK5CYII=';
    const f=await run([{code:`image("data:image/png;base64,${png}");`}]);expect(f.reply.draft.status).toBe('completed');expect(f.reply.spills).toHaveLength(1);expect(f.reply.draft.artifacts.find(a=>a.path===f.reply.spills[0])?.media_type).toBe('application/octet-stream');expect((await fs.readFile(path.join(f.out,f.reply.spills[0]!))).length).toBe(Buffer.from(png,'base64').length);
  });
  it('publishes a prompt failed v4 record when parent retained text exceeds the shared output bound',async()=>{
    const started=performance.now(),f=await run([{code:'text("x".repeat(6000));'}],{change:p=>{p.limits.tool_output_bytes=4096;p.limits.tool_timeout_seconds=5;}});
    expect(performance.now()-started).toBeLessThan(4000);expect(f.reply.draft).toMatchObject({status:'failed',diagnostic:{code:'OUTPUT_LIMIT'},closure:{tools:'may_live',detached_effects:'none_observed'}});expect(f.trace.some(t=>t.kind==='nested_join'||t.kind==='tool_end')).toBe(false);expect(f.reply.draft.counts).toMatchObject({model_requests:1,tool_calls:1,nested_tool_calls:0});
  });
  it('drains a physically ended child promptly when its retained text is refused',async()=>{
    const started=performance.now(),f=await run([{code:'await tools.read({path:"big.txt"}); text("ok");'}],{change:p=>{p.limits.tool_output_bytes=4096;p.limits.tool_timeout_seconds=5;},atRequest:async(index,_body,dir)=>{if(index===1)await fs.writeFile(path.join(path.dirname(dir),'big.txt'),'y'.repeat(6000));}});
    expect(performance.now()-started).toBeLessThan(4000);expect(f.reply.draft).toMatchObject({status:'failed',diagnostic:{code:'OUTPUT_LIMIT'},closure:{tools:'may_live',detached_effects:'none_observed'}});expect(f.trace.find(t=>t.kind==='nested_join')?.payload).toMatchObject({started:1,ended:0,outstanding:1});expect(f.trace.filter(t=>t.kind==='tool_end').map(t=>t.payload)).toEqual([expect.objectContaining({call_id:'call_1',status:'failed'})]);
  });
  it('refuses a parent join and end together when their exact trace bytes exceed the bound',async()=>{
    const f=await run([{code:'text("x".repeat(6000));'}],{change:p=>{p.limits.trace_bytes=22000;}});expect(f.reply.draft).toMatchObject({status:'failed',diagnostic:{code:'TRACE_LIMIT'},closure:{tools:'may_live',detached_effects:'none_observed'}});expect(f.trace.some(t=>t.kind==='nested_join'||t.kind==='tool_end')).toBe(false);expect(f.reply.draft.counts.trace_bytes).toBeLessThanOrEqual(22000);
  });
  it('fails closed when post-run spills exceed the shared output bound',async()=>{
    const f=await run([{code:'// @options: {"max_output_tokens":10}\ntext("x".repeat(10000));'}],{change:p=>{p.limits.tool_output_bytes=2000;},save:false});expect(f.reply.draft.diagnostic?.code).toBe('OUTPUT_LIMIT');await expect(fs.lstat(path.join(f.out,'result.json'))).rejects.toThrow();
  });
  it.each(['fd','rg'] as const)('rechecks captured nested %s identity after an earlier admitted write',async external=>{
    const f=await run([{code:`await tools.write({path:"bin/${external}",content:"#!/bin/sh\\nprintf changed\\n"}); await tools.${external==='fd'?'find':'grep'}(${external==='fd'?'{pattern:"*.txt"}':'{pattern:"native",path:"input.txt"}'});`}],{external});expect(f.reply.draft.diagnostic?.code).toBe('UNSUPPORTED_CAPABILITY');expect(f.reply.draft.counts).toMatchObject({tool_calls:3,nested_tool_calls:2});expect(f.trace.find(t=>t.kind==='nested_join')?.payload).toMatchObject({started:2,ended:2,outstanding:0});
  });
  it('includes all setup and execution in the original run deadline',async()=>{const f=await run([{code:'while(true){}'}],{deadlineMs:1000});expect(f.reply.draft.diagnostic?.code).toBe('TIMEOUT');expect(f.trace.find(t=>t.kind==='tool_end')?.elapsed_ms).toBeLessThan(2000);});
  it('refuses unowned temporary directories without publishing a result',async()=>{const f=await run([{code:'await tools.bash({command:"mkdir \\"$TMPDIR/unowned\\""});'}],{save:false});expect(f.reply.draft.diagnostic?.code).toBe('CLOSURE_UNCERTAIN');await expect(fs.lstat(path.join(f.out,'result.json'))).rejects.toThrow();});
  it('blocks a populated owned bin in the public hook before a native effect',async()=>{
    const f=await run([{code:'await tools.write({path:"forbidden",content:"no"});'}],{atRequest:async(index,_body,dir)=>{if(index===1)await fs.writeFile(path.join(dir,'pi','bin','unexpected'),'changed');}});expect(f.reply.draft.diagnostic?.code).toBe('UNSUPPORTED_CAPABILITY');await expect(fs.lstat(path.join(f.dir,'forbidden'))).rejects.toThrow();expect(f.trace.filter(t=>t.kind==='nested_join').map(t=>t.payload.outstanding)).toEqual([0]);
  });
});
function guardFixture(bound=8){
  const events:{kind:string;payload:Obj}[]=[],abort=new AbortController();let problem:MillforgeError|undefined;let verifies=0;
  const p={schema:'millforge-profile/v3',model:{api:'openai-completions'},tools:['read','write'],limits:{}} as ProfileV3;
  const guard=new CodemodeGuard({profile:p,limits:{tool_calls:bound,tool_timeout_seconds:2} as ProfileV3['limits'],deadline:performance.now()+3000,signal:abort.signal,emit:(kind,payload)=>events.push({kind,payload}),emitEnd:(payload,join)=>{if(join)events.push({kind:'nested_join',payload:join});events.push({kind:'tool_end',payload});},problem:()=>problem,stop:error=>{if(error.code==='CLOSURE_UNCERTAIN')problem=error;else problem??=error;abort.abort();},verify:async()=>{verifies++;},truncated:nativeTruncated});
  const start=(id='parent',name='codemode',parent?:string,args:unknown={code:'text(1)'}):void=>guard.event({type:'tool_execution_start',toolCallId:id,toolName:name,args,...(parent?{parentToolCallId:parent}:{})});
  const end=(id='parent',name='codemode',parent?:string):void=>guard.event({type:'tool_execution_end',toolCallId:id,toolName:name,result:{content:[]},isError:false,...(parent?{parentToolCallId:parent}:{})});
  return {guard,events,start,end,problem:()=>problem,verifies:()=>verifies};
}
describe('codemode guard adversarial public observations',()=>{
  it.each(['orphan','depth2','suffix','name','duplicate'])('refuses %s identity without admitting an effect',async kind=>{
    const f=guardFixture();if(kind!=='orphan')f.start();if(kind==='duplicate')f.start('parent');else f.start(kind==='suffix'?'parent/01':kind==='depth2'?'parent/1/1':'parent/1',kind==='name'?'codemode':'read',kind==='orphan'?'missing':kind==='depth2'?'parent/1':'parent');expect(f.problem()).toBeDefined();await expect(f.guard.before({type:'tool_call',toolName:'read',toolCallId:'parent/1',parentToolCallId:'parent',input:{}})).rejects.toThrow();expect(f.verifies()).toBe(0);f.guard.dispose();
  });
  it.each(['problem','oversized'])('drains observed unrecorded %s children separately',kind=>{
    const f=guardFixture(kind==='problem'?1:8);f.start();f.start('parent/1','read','parent',kind==='oversized'?{path:'x'.repeat(17000)}:{});const first=f.problem()?.code;f.end('parent/1','read','parent');f.end();expect(f.guard.closed).toBe(true);expect(f.problem()?.code).toBe(first);expect(f.events.map(e=>e.kind)).toEqual(['tool_start','nested_join','tool_end']);expect(f.events[1]?.payload).toMatchObject({started:0,ended:0,outstanding:0});f.guard.dispose();
  });
  it('records actual outstanding counts and closure uncertainty when a parent skips a needed drain',()=>{const f=guardFixture();f.start();f.start('parent/1','read','parent',{});f.end();f.end('parent/1','read','parent');expect(f.problem()?.code).toBe('CLOSURE_UNCERTAIN');expect(f.events.find(e=>e.kind==='nested_join')?.payload).toMatchObject({started:1,ended:0,outstanding:1});expect(f.events.filter(e=>e.kind==='tool_end')).toHaveLength(1);expect(f.events.find(e=>e.kind==='tool_end')?.payload.status).toBe('failed');f.guard.dispose();});
});
describe('owned empty materializer roots',()=>{
  it.each(['file','symlink','loader-child','home-child','.agents'])('refuses %s discovery before loading',async kind=>{const dir=await root();await fs.mkdir(path.join(dir,'bin'));await fs.mkdir(path.join(dir,'codemode-loader'));if(kind==='file')await fs.writeFile(path.join(dir,'codemode-loader','x'),'x');if(kind==='symlink'){await fs.rmdir(path.join(dir,'bin'));await fs.symlink(path.join(dir,'codemode-loader'),path.join(dir,'bin'));}if(kind==='loader-child')await fs.mkdir(path.join(dir,'codemode-loader','child'));if(kind==='home-child'||kind==='.agents')await fs.mkdir(path.join(dir,kind==='.agents'?'.agents':'extensions'));await expect(assertCodemodeRoots(dir)).rejects.toMatchObject({code:'UNSUPPORTED_CAPABILITY'});});
  it('refuses populated discovery roots after actual reload',async()=>{const dir=await root();await fs.mkdir(path.join(dir,'bin'));await fs.mkdir(path.join(dir,'codemode-loader'));const f=guardFixture(),pi=await import('@earendil-works/pi-coding-agent'),reload=pi.DefaultResourceLoader.prototype.reload;vi.spyOn(pi.DefaultResourceLoader.prototype,'reload').mockImplementation(async function(this:import('@earendil-works/pi-coding-agent').DefaultResourceLoader,options){await reload.call(this,options);await fs.writeFile(path.join(dir,'codemode-loader','late'),'foreign');});await expect(materializeCodemode(pi,dir,f.guard)).rejects.toMatchObject({code:'UNSUPPORTED_CAPABILITY'});f.guard.dispose();});
  it('loads exactly two fixed factories and rejects added discovery output or diagnostics',async()=>{const dir=await root();await fs.mkdir(path.join(dir,'bin'));await fs.mkdir(path.join(dir,'codemode-loader'));const f=guardFixture(),pi=await import('@earendil-works/pi-coding-agent');const loader=await materializeCodemode(pi,dir,f.guard),extensions=loader.getExtensions();expect(extensions.extensions.map(e=>e.path)).toEqual(codemodeLoader.extension_paths);for(const drift of ['path','package','warning','error']){const changed={...extensions,extensions:[...extensions.extensions]};if(drift==='path'||drift==='package')changed.extensions.push({...extensions.extensions[0]!,path:drift==='path'?'/tmp/foreign-extension':'npm:foreign'});if(drift==='warning')changed.warnings=[{path:'x',warning:'x'}];if(drift==='error')changed.errors=[{path:'x',error:'x'}];expect(()=>assertCodemodeExtensions(changed)).toThrow();}await assertCodemodeRoots(dir);f.guard.dispose();});
});
