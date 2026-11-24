/** Private finite worker. No Pi runtime import occurs until the owned environment exists. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {spawn,type ChildProcess} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import type {AgentSession,ResourceLoader,BashOperations,ToolDefinition} from '@earendil-works/pi-coding-agent';
import type {AssistantMessage,Credential,CredentialStore} from '@earendil-works/pi-ai';
import {type Closure,type Json,type Obj,type Result,type Code,currentRuntimeGuards,MillforgeError,bytes,canonical,object,parseJson,refuse,tightenLimits,validateTerminal} from './contracts.js';
import {finishCapture,prepareCapture,publishPreparedInputs,TraceWriter} from './artifacts.js';
import {hash,loadProfile,publicError,regularFile} from './profile.js';
import {type WorkerInput,type WorkerReply,verifyExecutables} from './lifecycle.js';
import {createBinding} from './bindings.js';
import {CodemodeGuard,assertCodemodeRoots,codemodeDeclarations,materializeCodemode} from './codemode-host.js';
const delay=(ms:number):Promise<void>=>new Promise(resolve=>setTimeout(resolve,ms));
function alive(pid:number):boolean{try{process.kill(-pid,0);return true;}catch(error){if((error as NodeJS.ErrnoException).code==='ESRCH')return false;throw error;}}
function signalGroup(child:ChildProcess,signal:NodeJS.Signals):void{if(child.pid)try{process.kill(-child.pid,signal);}catch(error){if((error as NodeJS.ErrnoException).code!=='ESRCH')throw error;}}
/** Retain group handles even after the main shell exits, so detached descendants prevent success. */
export class OwnedProcesses {
  private children=new Set<ChildProcess>();private pending=new Set<Promise<unknown>>();private retainedText=0;private reservedRaw=0;private stopped=false;private cleanup?:Promise<boolean>;
  constructor(private readonly signal:AbortSignal,private readonly deadline:number,private readonly outputLimit:number,private readonly toolTimeout:number,private readonly env:NodeJS.ProcessEnv,private readonly onStop?:(error:MillforgeError)=>void,private readonly displayBytes=50*1024,private readonly credential?:string|readonly string[]){}
  retainText(total:number):void{this.retainedText=total;}
  operations():BashOperations{return {exec:(command,cwd,options)=>{
    if(this.stopped||this.signal.aborted||performance.now()>=this.deadline)return Promise.reject(new MillforgeError('TIMEOUT','tool admission closed'));
    const job=new Promise<{exitCode:number|null}>((resolve,reject)=>{
      const child=spawn('/bin/bash',['--noprofile','--norc','-c',command],{cwd,env:this.env,detached:true,stdio:['ignore','pipe','pipe']});this.children.add(child);let problem:MillforgeError|undefined,accept=true,received=0,rawReceived=0,held=Buffer.alloc(0),killTimer:NodeJS.Timeout|undefined;
      const abort=():void=>{accept=false;signalGroup(child,'SIGTERM');killTimer??=setTimeout(()=>signalGroup(child,'SIGKILL'),1000);};
      this.signal.addEventListener('abort',abort,{once:true});options.signal?.addEventListener('abort',abort,{once:true});
      const timeout=Math.max(1,Math.min(this.deadline-performance.now(),this.toolTimeout*1000,(options.timeout??this.toolTimeout)*1000));
      const timer=setTimeout(()=>{problem=new MillforgeError('TIMEOUT','tool deadline expired');this.onStop?.(problem);abort();},timeout);
      const secrets=[...new Set(typeof this.credential==='string'?[this.credential]:this.credential??[])].filter(Boolean).sort((a,b)=>b.length-a.length).map(value=>Buffer.from(value)),suffix=Math.max(0,...secrets.map(value=>value.length-1)),replacement=Buffer.from('[redacted]');
      const stopOutput=(message:string):void=>{problem=new MillforgeError('OUTPUT_LIMIT',message);this.onStop?.(problem);abort();};
      const forward=(chunk:Buffer):void=>{if(!chunk.length)return;if(this.retainedText+this.reservedRaw+received+chunk.length+Math.min(3*(received+chunk.length),this.displayBytes)+bytes(this.env.TMPDIR??'')+1024>this.outputLimit){stopOutput('tool output byte ceiling reached');return;}received+=chunk.length;try{options.onData(chunk);}catch{problem=new MillforgeError('TOOL_ERROR','tool output consumer failed');this.onStop?.(problem);abort();}};
      // Keep the longest selected literal's suffix, bounded by the same raw byte ceiling.
      // Only scrubbed, fully charged bytes reach Pi's accumulator or native spill stream.
      const filtered=(chunk:Buffer,final=false):void=>{if(!secrets.length){forward(chunk);return;}const joined=Buffer.concat([held,chunk]),parts:Buffer[]=[];const safeEnd=final?joined.length:Math.max(0,joined.length-suffix);let at=0;while(at<safeEnd){let match=-1,secret:Buffer|undefined;for(const value of secrets){const found=joined.indexOf(value,at);if(found!==-1&&(match===-1||found<match)){match=found;secret=value;}}if(!secret||match>=safeEnd){parts.push(joined.subarray(at,safeEnd));at=safeEnd;break;}parts.push(joined.subarray(at,match),replacement);at=match+secret.length;}held=Buffer.from(joined.subarray(at));forward(Buffer.concat(parts));};
      const data=(chunk:Buffer):void=>{if(!accept)return;if(rawReceived+chunk.length>this.outputLimit){stopOutput('raw tool output byte ceiling reached');return;}rawReceived+=chunk.length;filtered(chunk);};
      child.stdout.on('data',data);child.stderr.on('data',data);child.once('error',()=>{problem=new MillforgeError('TOOL_ERROR','tool process failed');});
      child.once('close',(code)=>{if(accept)filtered(Buffer.alloc(0),true);held=Buffer.alloc(0);this.reservedRaw+=received;clearTimeout(timer);if(killTimer)clearTimeout(killTimer);accept=false;this.signal.removeEventListener('abort',abort);options.signal?.removeEventListener('abort',abort);if(problem)reject(problem);else if(this.signal.aborted||options.signal?.aborted)reject(new MillforgeError('CANCELLED','tool cancelled'));else resolve({exitCode:code});});
    });this.pending.add(job);void job.then(()=>this.pending.delete(job),()=>this.pending.delete(job));return job;
  }};}
  close():Promise<boolean>{return this.cleanup??=(async()=>{this.stopped=true;for(const child of this.children)signalGroup(child,'SIGTERM');const until=performance.now()+1000;while(performance.now()<until&&[...this.children].some(c=>c.pid&&alive(c.pid)))await delay(20);for(const child of this.children)if(child.pid&&alive(child.pid))signalGroup(child,'SIGKILL');await Promise.race([Promise.allSettled([...this.pending]),delay(400)]);return this.pending.size===0&&[...this.children].every(c=>!c.pid||!alive(c.pid));})();}
}
/** Public native tool details describe both byte/line truncation and explicit result ceilings. */
export function nativeTruncated(name:string,result:unknown):boolean{
  const value=result as {details?:{truncation?:{truncated?:unknown};resultLimitReached?:unknown;matchLimitReached?:unknown;entryLimitReached?:unknown;linesTruncated?:unknown}};
  const details=value?.details;if(!details)return false;
  if(details.truncation?.truncated===true)return true;
  const positive=(n:unknown):boolean=>typeof n==='number'&&Number.isFinite(n)&&n>0;
  return name==='find'&&positive(details.resultLimitReached)||name==='grep'&&(positive(details.matchLimitReached)||details.linesTruncated===true)||name==='ls'&&positive(details.entryLimitReached);
}
async function emptyOwnedBin(dir:string):Promise<void>{
  for(const member of [dir,path.join(dir,'bin')]){const st=await fs.lstat(member);if(!st.isDirectory()||st.isSymbolicLink())refuse('owned Pi tool directory changed','UNSUPPORTED_CAPABILITY');}
  if((await fs.readdir(path.join(dir,'bin'))).length)refuse('owned Pi tool directory is populated','UNSUPPORTED_CAPABILITY');
}
/** In-memory public credential store with serialized per-provider modifications. */
export function memoryCredentials():CredentialStore{
  const values=new Map<string,Credential>(),queues=new Map<string,Promise<unknown>>();
  const serialized=<T>(provider:string,signal:AbortSignal|undefined,fn:()=>Promise<T>):Promise<T>=>{const next=(queues.get(provider)??Promise.resolve()).catch(()=>{}).then(async()=>{if(signal?.aborted)refuse('credential operation cancelled','CANCELLED');return fn();});queues.set(provider,next);return next;};
  const check=(signal?:AbortSignal):void=>{if(signal?.aborted)refuse('credential operation cancelled','CANCELLED');};
  return {async read(provider,options){check(options?.signal);return values.get(provider);},async list(options){check(options?.signal);return [...values].map(([providerId,value])=>({providerId,type:value.type}));},async modify(provider,fn,options){return serialized(provider,options?.signal,async()=>{const value=await fn(values.get(provider));check(options?.signal);if(value)values.set(provider,value);return values.get(provider);});},async delete(provider,options){await serialized(provider,options?.signal,async()=>{check(options?.signal);values.delete(provider);});}};
}
async function temporaryFiles(root:string):Promise<string[]>{
  const result:string[]=[];for(const name of await fs.readdir(root)){const file=path.join(root,name),st=await fs.lstat(file);if(!st.isFile()||st.isSymbolicLink())refuse('unowned temporary entry','CLOSURE_UNCERTAIN');result.push('tmp/'+name);}return result.sort();
}
export async function executeWorker(input:WorkerInput,signal:AbortSignal,getCredential?:()=>Promise<string|undefined>):Promise<WorkerReply>{
  const started=performance.now(),deadline=started+Number(BigInt(input.deadlineNs)-process.hrtime.bigint())/1e6,loaded=await loadProfile(input.profileDir),limits=tightenLimits(loaded.profile,input.request.limits);
  const codemode=loaded.profile.schema==='millforge-profile/v3',successor=loaded.profile.schema!=='millforge-profile/v1';let run=await prepareCapture(input.dir,loaded,input.request,successor);await fs.mkdir(path.join(input.dir,'pi','bin'),{recursive:true,mode:0o700});await fs.mkdir(path.join(input.dir,'tmp'),{mode:0o700});if(codemode)await fs.mkdir(path.join(input.dir,'pi','codemode-loader'),{mode:0o700});
  if(process.env.PI_OFFLINE!=='1'||process.env.PI_CODING_AGENT_DIR!==path.join(input.dir,'pi')||process.env.TMPDIR!==path.join(input.dir,'tmp'))refuse('worker environment was not prepared','UNSUPPORTED_CAPABILITY');
  if(codemode&&(process.env.PI_CODEMODE_MAX_WORKERS!=='1'||process.env.HOME!==path.join(input.dir,'pi')||process.env.TMP!==process.env.TMPDIR||process.env.TEMP!==process.env.TMPDIR))refuse('codemode worker environment was not prepared','UNSUPPORTED_CAPABILITY');
  await verifyExecutables(input.tools,process.env.PATH??'');if(signal.aborted)refuse('cancelled before Pi load','CANCELLED');
  const controller=new AbortController();const cancel=():void=>{if(codemode)problem??=new MillforgeError('CANCELLED','invocation cancelled');controller.abort();};signal.addEventListener('abort',cancel,{once:true});
  const deadlineTimer=setTimeout(()=>{if(codemode)problem??=new MillforgeError('TIMEOUT','work deadline expired');controller.abort();},Math.max(0,deadline-performance.now()));
  let trace:TraceWriter|undefined,session:AgentSession|undefined,processes:OwnedProcesses|undefined,problem:MillforgeError|undefined,output:Json=null,domain:string|null=null,tools=0,outputBytes=0,toolBytes=0,settled=false,idle=true,toolClosed=true;
  let guard:CodemodeGuard|undefined;let declarations:Obj={};
  const pending=new Set<string>(),seen=new Set<string>(),toolTimers=new Map<string,{deadline:number;timer:NodeJS.Timeout}>();let cleanup:Promise<void>|undefined;
  const clean=():Promise<void>=>cleanup??=(async()=>{for(const call of toolTimers.values())clearTimeout(call.timer);toolTimers.clear();if(session){idle=false;await Promise.race([session.abort().then(()=>session!.waitForIdle()).then(()=>{idle=true;}),delay(1500)]);session.dispose();}if(processes)toolClosed=await processes.close();guard?.dispose();})();
  let binding:Awaited<ReturnType<typeof createBinding>>|undefined;
  const redact=(value:Json):Json=>{if(typeof value==='string'){for(const secret of [binding?.oauthLease?.credential.access,binding?.oauthLease?.credential.refresh,input.credential].filter((x):x is string=>!!x).sort((a,b)=>b.length-a.length))value=value.split(secret).join('[redacted]');return value;}return Array.isArray(value)?value.map(redact):value!==null&&typeof value==='object'?Object.fromEntries(Object.entries(value).map(([key,v])=>[key,redact(v)])):value;};
  const emit=(kind:string,payload:Obj):void=>{payload=redact(payload) as Obj;if(!trace)refuse('effect before capture','IO_ERROR');if(problem&&!['model_end','tool_end',...(codemode?['nested_join']:[])].includes(kind))throw problem;if(kind==='tool_end'){const size=bytes(String(payload.text));if(toolBytes+size>limits.tool_output_bytes)refuse('retained tool output ceiling reached','OUTPUT_LIMIT');toolBytes+=size;processes?.retainText(toolBytes);}if(kind==='output_partial'){const size=bytes(String(payload.text));if(outputBytes+size>limits.output_bytes)refuse('assistant output exceeds ceiling','OUTPUT_LIMIT');outputBytes+=size;}trace.append(kind,payload);};
  const emitEnd=(payload:Obj,join?:Obj):void=>{
    payload=redact(payload) as Obj;if(join)join=redact(join) as Obj;if(!trace)refuse('effect before capture','IO_ERROR');
    const size=bytes(String(payload.text));if(toolBytes+size>limits.tool_output_bytes)refuse('retained tool output ceiling reached','OUTPUT_LIMIT');
    trace.appendToolEnd(payload,join);toolBytes+=size;processes?.retainText(toolBytes);
  };
  try{
    binding=await createBinding({profile:loaded.profile,limits,signal:controller.signal,deadline,onTrace:emit,profileDigest:loaded.digest,...(getCredential?{getCredential:async()=>{const value=await getCredential();if(value===undefined)delete input.credential;else input.credential=value;return value;}}:{}),...(input.authHome?{authHome:input.authHome}:{}),...(input.credential===undefined?{}:{credential:input.credential})});
    if(successor)run=await publishPreparedInputs(run);
    let prompt=loaded.profile.instructions,schemas:{name:string;sha256:string}[]=[],names:string[]=[];
    if(loaded.profile.mode==='agent'){
      const pi=await import('@earendil-works/pi-coding-agent');
      const runtime=await pi.ModelRuntime.create({credentials:binding.oauthLease?.store??memoryCredentials(),modelsPath:null,refreshOnCreate:false,allowModelNetwork:false,signal:controller.signal});runtime.registerNativeProvider(binding.provider);
      if(!successor)await runtime.setRuntimeApiKey(binding.model.provider,input.credential??'millforge-no-auth');
      await runtime.refresh({allowNetwork:false,signal:controller.signal});
      if(successor&&canonical(runtime.getModel(binding.model.provider,binding.model.id) as unknown as Obj)!==canonical(binding.model as unknown as Obj))refuse('runtime model differs from admitted native model','UNSUPPORTED_CAPABILITY');
      const {skills}=pi.loadSkills({cwd:run.profile.dir,agentDir:path.join(input.dir,'pi'),skillPaths:loaded.profile.assets.filter(a=>a.kind==='skill'&&path.posix.basename(a.path)==='SKILL.md').map(a=>path.join(run.profile.dir,a.path)),includeDefaults:false});
      const resources:ResourceLoader={getExtensions:()=>({extensions:[],errors:[],runtime:pi.createExtensionRuntime()}),getSkills:()=>({skills,diagnostics:[]}),getPrompts:()=>({prompts:[],diagnostics:[]}),getThemes:()=>({themes:[],diagnostics:[]}),getAgentsFiles:()=>({agentsFiles:loaded.profile.assets.filter(a=>a.kind==='context').map(a=>({path:path.join(run.profile.dir,a.path),content:loaded.assets.get(a.path)!.toString('utf8')}))}),getSystemPrompt:()=>undefined,getSystemPromptSource:()=>undefined,getAppendSystemPrompt:()=>[loaded.profile.instructions],getAppendSystemPromptSources:()=>[],extendResources:()=>refuse('resource discovery disabled','UNSUPPORTED_CAPABILITY'),reload:async()=>{}};
      processes=new OwnedProcesses(controller.signal,deadline,limits.tool_output_bytes,limits.tool_timeout_seconds,{PATH:process.env.PATH??'',HOME:process.env.HOME??path.join(input.dir,'pi'),TMPDIR:process.env.TMPDIR!,LANG:process.env.LANG??'C.UTF-8'},error=>{problem??=error;controller.abort();},pi.DEFAULT_MAX_BYTES,binding.oauthLease?[binding.oauthLease.credential.access,binding.oauthLease.credential.refresh]:input.credential);
      if(loaded.profile.schema==='millforge-profile/v3'){
        guard=new CodemodeGuard({profile:loaded.profile,limits,deadline,signal:controller.signal,emit,emitEnd,problem:()=>problem,stop:error=>{if(error.code==='CLOSURE_UNCERTAIN')problem=error;else problem??=error;controller.abort();},verify:async()=>{await verifyExecutables(input.tools,process.env.PATH??'');await emptyOwnedBin(path.join(input.dir,'pi'));},truncated:nativeTruncated});
        const materializer=await materializeCodemode(pi,path.join(input.dir,'pi'),guard);resources.getExtensions=()=>materializer.getExtensions();resources.reload=async()=>{await assertCodemodeRoots(path.join(input.dir,'pi'));await materializer.reload();await assertCodemodeRoots(path.join(input.dir,'pi'));};
      }
      const customTools=loaded.profile.tools.includes('bash')?[pi.createBashToolDefinition(input.request.workspace!,{operations:processes.operations(),exposeSessionEnvironment:false}) as unknown as ToolDefinition]:[]; // SDK customTools erases the native schema generic; native definition/data remain unchanged.
      ({session}=await pi.createAgentSession({cwd:input.request.workspace!,agentDir:path.join(input.dir,'pi'),modelRuntime:runtime,model:binding.model,thinkingLevel:binding.thinkingLevel??'off',scopedModels:[{model:binding.model,thinkingLevel:binding.thinkingLevel??'off'}],tools:codemode?[...loaded.profile.tools,'codemode']:loaded.profile.tools,customTools,resourceLoader:resources,sessionManager:pi.SessionManager.inMemory(input.request.workspace!),settingsManager:pi.SettingsManager.inMemory({retry:{enabled:false,maxRetries:0,provider:{maxRetries:0}},compaction:{enabled:false},cacheWarming:'off'},codemode?{projectTrusted:false}:{})}));
      const scoped=session.scopedModels[0];if(successor&&(session.thinkingLevel!==binding.thinkingLevel||session.scopedModels.length!==1||!scoped||scoped.thinkingLevel!==binding.thinkingLevel||canonical(scoped.model as unknown as Obj)!==canonical(binding.model as unknown as Obj)))refuse('native session reasoning/model differs from admission','UNSUPPORTED_CAPABILITY');
      session.agent.toolExecution='sequential';names=session.getActiveToolNames();schemas=names.map(name=>({name,sha256:hash(canonical(session!.getToolDefinition(name)!.parameters as Json))}));prompt=session.systemPrompt;if(loaded.profile.schema==='millforge-profile/v3')declarations=codemodeDeclarations(session,loaded.profile);
      const nativeBefore=session.agent.beforeToolCall,nativeAfter=session.agent.afterToolCall;
      if(!codemode)session.agent.beforeToolCall=async(ctx,s)=>{try{if(problem)throw problem;if(controller.signal.aborted||performance.now()>=deadline)refuse('tool deadline reached','TIMEOUT');await verifyExecutables(input.tools,process.env.PATH??'');if(ctx.toolCall.name==='find'||ctx.toolCall.name==='grep')await emptyOwnedBin(path.join(input.dir,'pi'));const result=await nativeBefore?.(ctx,s);const call=toolTimers.get(ctx.toolCall.id);if(call&&performance.now()>=call.deadline)refuse('native tool deadline expired','TIMEOUT');return result;}catch(error){problem=publicError(error);controller.abort();throw problem;}};
      session.agent.afterToolCall=async(ctx,s)=>nativeAfter?.(ctx,s);
      session.subscribe(event=>{try{
        if(event.type==='agent_settled')settled=true;
        if(codemode&&(event.type==='tool_execution_start'||event.type==='tool_execution_end')){guard!.event(event);return;}
        if(event.type==='tool_execution_start'){
          if(pending.size||seen.has(event.toolCallId))refuse('duplicate or parallel tool identity','TOOL_ERROR');if(tools>=limits.tool_calls)refuse('tool call ceiling reached','LIMIT_EXHAUSTED');
          emit('tool_start',{call_id:event.toolCallId,name:event.toolName,arguments:object(event.args)});seen.add(event.toolCallId);pending.add(event.toolCallId);tools++;const callId=event.toolCallId,callDeadline=Math.min(deadline,performance.now()+limits.tool_timeout_seconds*1000);toolTimers.set(callId,{deadline:callDeadline,timer:setTimeout(()=>{if(pending.has(callId)){problem??=new MillforgeError('TIMEOUT','native tool deadline expired');controller.abort();}},Math.max(1,callDeadline-performance.now()))});
        }
        if(event.type==='tool_execution_end'){
          if(!pending.has(event.toolCallId))refuse('orphan tool result','TOOL_ERROR');const call=toolTimers.get(event.toolCallId);if(call)clearTimeout(call.timer);toolTimers.delete(event.toolCallId);if(call&&performance.now()>=call.deadline){problem??=new MillforgeError('TIMEOUT','native tool deadline expired');controller.abort();}
          const content=(event.result as {content?:{type:string;text?:string}[]}).content??[];const text=content.filter(c=>c.type==='text').map(c=>c.text??'').join('\n');
          emit('tool_end',{call_id:event.toolCallId,status:problem?.code==='TIMEOUT'?'timeout':event.isError?'failed':'completed',text,truncated:nativeTruncated(event.toolName,event.result)});pending.delete(event.toolCallId);
        }
      }catch(error){if(codemode)problem??=publicError(error);else problem=publicError(error);controller.abort();}});
    }else{prompt=[prompt,...loaded.profile.assets.map(a=>loaded.assets.get(a.path)!.toString('utf8'))].filter(Boolean).join('\n\n');}
    const boundary=await finishCapture(run,{schema:codemode?'millforge-capture/v4':successor?'millforge-capture/v3':'millforge-capture/v2',request_id:input.request.request_id,profile_digest:loaded.digest,limits,cwd:input.request.workspace,resources:loaded.resources,system_prompt:prompt,tool_names:names,tool_schemas:schemas,...declarations,binding:binding.binding,...(loaded.profile.schema==='millforge-profile/v1'?{credential_env:loaded.profile.model.credential_env}:{runtime_guards:currentRuntimeGuards}),versions:{millforge:'0.2.0',pi:'1.4.0',node:process.versions.node,platform:process.platform},external_tools:input.tools});
    trace=new TraceWriter(input.dir,boundary,started);emit('capture',{profile_digest:loaded.digest});
    const abortCleanup=():void=>{void clean();};controller.signal.addEventListener('abort',abortCleanup,{once:true});
    if(controller.signal.aborted||performance.now()>=deadline)refuse(signal.aborted?'invocation cancelled':'work deadline expired',signal.aborted?'CANCELLED':'TIMEOUT');
    const task=[...input.request.context,input.request.task].join('\n\n');let finalText:string;
    if(session){
      await session.prompt(task,{expandPromptTemplates:false});await session.waitForIdle();idle=session.isIdle;
      if(problem)throw problem;if(!settled||!idle||session.pendingMessageCount||pending.size||guard&&!guard.recordsClosed||session.agent.state.pendingToolCalls.size)refuse('session did not settle','CLOSURE_UNCERTAIN');
      const final=[...session.agent.state.messages].reverse().find(message=>message.role==='assistant') as AssistantMessage|undefined;if(!final)refuse('missing final assistant','INVALID_FINISH');binding.assertNormal(final);finalText=binding.finalText?binding.finalText(final):redact(final.content.filter(c=>c.type==='text').map(c=>c.text).join('')) as string;
    }else finalText=(await binding.generate({systemPrompt:prompt,messages:[{role:'user',content:task,timestamp:Date.now()}]})).text;
    if(problem)throw problem;if(controller.signal.aborted)refuse(signal.aborted?'invocation cancelled':'work deadline expired',signal.aborted?'CANCELLED':'TIMEOUT');
    if(!finalText.trim()||bytes(finalText)>limits.output_bytes)refuse('invalid terminal text','INVALID_TERMINAL');outputBytes=Math.max(outputBytes,bytes(finalText));
    try{output=loaded.profile.terminal.format==='text'?finalText:parseJson(finalText);domain=validateTerminal(output,{...loaded.profile,limits});}catch{refuse('terminal shape did not match','INVALID_TERMINAL');}
  }catch(error){problem=problem??publicError(error);if(signal.aborted&&!codemode)problem=new MillforgeError('CANCELLED','invocation cancelled');else if(!codemode&&performance.now()>=deadline&&['IO_ERROR','CANCELLED','TIMEOUT','PROVIDER_ERROR'].includes(problem.code))problem=new MillforgeError('TIMEOUT','work deadline expired');controller.abort();}
  finally{clearTimeout(deadlineTimer);signal.removeEventListener('abort',cancel);await clean();}
  if(!trace||!binding){const failure=problem??new MillforgeError('IO_ERROR','capture was not established');if(successor){await fs.rm(path.join(input.dir,'profile'),{recursive:true,force:true});await fs.rm(path.join(input.dir,'request.json'),{force:true});await fs.writeFile(path.join(input.dir,'diagnostic.json'),JSON.stringify({schema:'millforge-admission-diagnostic/v1',request_id:input.request.request_id,code:failure.code,message:failure.message})+'\n',{flag:'wx',mode:0o600});}throw failure;}
  let spills:string[]=[];try{spills=await temporaryFiles(path.join(input.dir,'tmp'));let retained=toolBytes;for(const rel of spills){const file=path.join(input.dir,rel);if(codemode&&retained+(await fs.lstat(file)).size>limits.tool_output_bytes)refuse('retained spills exceed aggregate ceiling','OUTPUT_LIMIT');retained+=(await regularFile(file,limits.tool_output_bytes)).length;}if(retained>limits.tool_output_bytes)refuse('retained spills exceed aggregate ceiling','OUTPUT_LIMIT');}catch(error){if(codemode){problem??=publicError(error);if(publicError(error).code==='CLOSURE_UNCERTAIN')toolClosed=false;}else{problem=publicError(error);toolClosed=false;}}
  if(guard&&!guard.closed)toolClosed=false;
  const closure:Closure={worker:'closed',tools:toolClosed&&pending.size===0&&(!guard||guard.recordsClosed)?'closed':'may_live',writers:'closed',inference:binding.inferenceReturned()?'response_returned':'disconnected_unknown',detached_effects:toolClosed?'none_observed':'unknown'};
  if(!idle||!toolClosed||pending.size){problem=new MillforgeError('CLOSURE_UNCERTAIN','owned session or tool closure is uncertain');}
  const status=problem?(problem.code==='CANCELLED'?'cancelled':problem.code==='TIMEOUT'?'timeout':'failed'):'completed';
  try{trace.append('terminal',{status,code:problem?.code??null});trace.append('closure',closure as unknown as Obj);}finally{trace.close();}
  const draft:Result={schema:codemode?'millforge-result/v4':successor?'millforge-result/v3':'millforge-result/v2',request_id:input.request.request_id,profile_id:loaded.profile.profile_id,profile_digest:loaded.digest,mode:loaded.profile.mode,status,domain_outcome:problem?null:domain,output:problem?null:output,diagnostic:problem?{code:problem.code as Exclude<Code,'OPTIMIZER_UNAVAILABLE'>,message:problem.message}:null,counts:{model_requests:trace.modelStarts,tool_calls:guard?.toolCalls??tools,...(guard?{nested_tool_calls:guard.nestedCalls}:{}),input_bytes:bytes(input.request.task)+input.request.context.reduce((sum,v)=>sum+bytes(v),0),output_bytes:outputBytes,trace_bytes:trace.size,usage:binding.usage() as unknown as Json,backend_requests:null,cost:null},timing:{started_at:input.startedAt,ended_at:new Date().toISOString(),elapsed_ms:performance.now()-started},closure,artifacts:[]};
  return {draft,spills};
}
if(process.env.MILLFORGE_WORKER==='1'&&process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const abort=new AbortController();let running=false,credentialReply:((value:string|undefined)=>void)|undefined;process.on('SIGTERM',()=>abort.abort());process.on('SIGINT',()=>abort.abort());
  process.on('message',(value:WorkerInput|{cancel:true}|{credentialReply:true;credential?:string})=>{if('cancel'in value){abort.abort();return;}if('credentialReply'in value){credentialReply?.(value.credential);credentialReply=undefined;return;}if(running)return;running=true;void executeWorker(value,abort.signal,()=>new Promise(resolve=>{credentialReply=resolve;process.send?.({credentialRequest:true});})).then(reply=>{process.send?.({reply},undefined,{},()=>{process.exit(0);});},error=>{process.send?.({error:{code:publicError(error).code}},undefined,{},()=>{process.exit(2);});});});process.send?.({ready:true});
}
