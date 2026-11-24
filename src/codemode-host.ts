/** Fixed public Pi materializer and the one handler-only nested-call guard. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import type {AgentSession,ExtensionFactory,LoadExtensionsResult,ToolCallEvent,ToolResultEvent} from '@earendil-works/pi-coding-agent';
import {type Json,type Obj,type Limits,type ProfileV3,MillforgeError,bytes,canonical,codemodeConstants,codemodeLoader,codemodeOptions,object,refuse,sourcePin} from './contracts.js';
import {nestedId,topLevelId} from './contracts-v4.js';
import {hash,publicError} from './profile.js';

export async function assertCodemodeRoots(home:string):Promise<void>{
  const root=await fs.lstat(home);
  if(!root.isDirectory()||root.isSymbolicLink()||canonical((await fs.readdir(home)).sort())!==canonical(['bin','codemode-loader']))refuse('owned codemode HOME changed','UNSUPPORTED_CAPABILITY');
  for(const name of ['bin','codemode-loader']){const dir=path.join(home,name),st=await fs.lstat(dir);if(!st.isDirectory()||st.isSymbolicLink()||(await fs.readdir(dir)).length)refuse('owned codemode discovery root is not empty','UNSUPPORTED_CAPABILITY');}
}
export function assertCodemodeExtensions(result:LoadExtensionsResult):LoadExtensionsResult{
  if(result.errors.length||result.warnings?.length||canonical(result.extensions.map(e=>e.path))!==canonical([...codemodeLoader.extension_paths]))refuse('unadmitted extension materialization','UNSUPPORTED_CAPABILITY');
  const [codemode,guard]=result.extensions;
  if(!codemode||!guard||codemode.resolvedPath!==codemode.path||guard.resolvedPath!==guard.path||codemode.hidden||guard.hidden||codemode.replaceable||guard.replaceable)refuse('inline extension identity changed','UNSUPPORTED_CAPABILITY');
  if(canonical([...codemode.tools.keys()])!==canonical(['codemode'])||guard.tools.size||canonical([...guard.handlers.keys()])!==canonical(['tool_call','tool_result']))refuse('inline extension registrations changed','UNSUPPORTED_CAPABILITY');
  for(const extension of result.extensions)if(extension.commands.size||extension.flags.size||extension.shortcuts.size||extension.messageRenderers.size||extension.toolRenderers?.length||extension.entryRenderers?.size||extension.markdownTransformer)refuse('unexpected inline registrations','UNSUPPORTED_CAPABILITY');
  const definition=codemode.tools.get('codemode')!.definition;
  if(definition.defaultActive!==false||definition.exposure!=='model-only')refuse('codemode exposure changed','UNSUPPORTED_CAPABILITY');
  if(result.runtime.pendingProviderRegistrations.length||result.runtime.pendingNativeProviderRegistrations.length||result.runtime.pendingVirtualModelRegistrations.length)refuse('extension model registration refused','UNSUPPORTED_CAPABILITY');
  return result;
}
export async function materializeCodemode(pi:typeof import('@earendil-works/pi-coding-agent'),home:string,guard:CodemodeGuard){
  await assertCodemodeRoots(home);
  const root=path.join(home,'codemode-loader'),settings=pi.SettingsManager.inMemory({}, {projectTrusted:false});
  if(settings.isProjectTrusted())refuse('codemode materializer requires untrusted project','UNSUPPORTED_CAPABILITY');
  const handlerOnly:ExtensionFactory=extension=>{extension.on('tool_call',event=>guard.before(event));extension.on('tool_result',event=>guard.after(event));};
  const loader=new pi.DefaultResourceLoader({cwd:root,agentDir:root,settingsManager:settings,noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,additionalExtensionPaths:[],extensionFactories:[{name:'millforge-codemode',factory:pi.createCodemodeExtension(codemodeOptions)},{name:'millforge-guard',factory:handlerOnly}],extensionsOverride:assertCodemodeExtensions});
  await loader.reload();if(settings.isProjectTrusted())refuse('codemode project trust changed','UNSUPPORTED_CAPABILITY');assertCodemodeExtensions(loader.getExtensions());await assertCodemodeRoots(home);return loader;
}
export function codemodeDeclarations(session:AgentSession,profile:ProfileV3):Obj{
  const active=session.agent.state.tools,names=active.map(t=>t.name),all=session.getAllTools(),callable=session.getCallableToolNames(),wanted=[...profile.tools,'codemode'].sort();
  if(new Set(names).size!==names.length||canonical([...names].sort())!==canonical(wanted)||canonical(all.map(t=>t.name).sort())!==canonical(wanted)||new Set(callable).size!==callable.length||canonical([...callable].sort())!==canonical([...profile.tools].sort()))refuse('codemode loadout changed','UNSUPPORTED_CAPABILITY');
  const tool=active.find(t=>t.name==='codemode')!,definition=session.getToolDefinition('codemode');
  if(!definition||definition.defaultActive!==false||definition.exposure!=='model-only'||!tool.constrainedSampling||typeof tool.constrainedSampling!=='object')refuse('codemode grammar metadata is unavailable','UNSUPPORTED_CAPABILITY');
  if(all.some(t=>t.name!=='codemode'&&t.exposure!=='direct'))refuse('native callable exposure changed','UNSUPPORTED_CAPABILITY');
  const declared=active.map(t=>({name:t.name,description_sha256:hash(t.description)}));
  return {tool_names:names,tool_schemas:active.map(t=>({name:t.name,sha256:hash(canonical(t.parameters as Json))})),declared_tools:declared,callable_tool_names:callable,capability:{id:'pi-codemode/v1',factory:'createCodemodeExtension',package:'@earendil-works/pi-coding-agent',version:'1.4.0',source_pin:sourcePin,fixed_options:{...codemodeOptions},exposure:'model-only',default_active:false,declared_description_sha256:declared.find(t=>t.name==='codemode')!.description_sha256,constrained_sampling_sha256:hash(canonical(tool.constrainedSampling as unknown as Json)),hidden_declarations:[],loader:{...codemodeLoader,extension_paths:[...codemodeLoader.extension_paths]},pinned_constants:{...codemodeConstants}}};
}
type Start={type:'tool_execution_start';toolCallId:string;toolName:string;args:unknown;parentToolCallId?:string};
type End={type:'tool_execution_end';toolCallId:string;toolName:string;result:unknown;isError:boolean;parentToolCallId?:string};
interface Parent {id:string;name:string;deadline:number;timer:NodeJS.Timeout;started:number;ended:number;next:number}
interface GuardOptions {profile:ProfileV3;limits:Limits;deadline:number;signal:AbortSignal;emit:(kind:string,payload:Obj)=>void;emitEnd:(payload:Obj,join?:Obj)=>void;problem:()=>MillforgeError|undefined;stop:(error:MillforgeError)=>void;verify:()=>Promise<void>;truncated:(name:string,result:unknown)=>boolean}
/** Recorded calls and observed unrecorded calls have separate drain sets. */
export class CodemodeGuard {
  private parent:Parent|undefined;private children=new Map<string,string>();private observed=new Set<string>();private seen=new Set<string>();private endedParents=new Set<string>();private waiters=new Set<()=>void>();
  private calls=0;private nested=0;private uncertain=false;private refusedEnd=false;
  constructor(private readonly options:GuardOptions){}
  get toolCalls():number{return this.calls;}
  get nestedCalls():number{return this.nested;}
  get closed():boolean{return !this.uncertain&&!this.parent&&!this.children.size&&!this.observed.size;}
  get recordsClosed():boolean{return !this.refusedEnd&&this.closed;}
  private stop(error:unknown):void{const problem=publicError(error);if(problem.code==='CLOSURE_UNCERTAIN')this.uncertain=true;this.options.stop(problem);}
  private wake():void{for(const resolve of this.waiters)resolve();this.waiters.clear();}
  private check():void{if(this.options.problem())throw this.options.problem();if(this.options.signal.aborted)refuse('tool admission aborted','CANCELLED');if(performance.now()>=Math.min(this.options.deadline,this.parent?.deadline??Infinity))refuse('original tool deadline expired','TIMEOUT');}
  event(event:Start|End):void{
    try{if(event.type==='tool_execution_start')this.start(event);else this.end(event);}catch(error){this.stop(error);}
  }
  private start(event:Start):void{
    const nested=event.parentToolCallId!==undefined;
    // All observed starts must drain, even if no trace slot or argument budget remains.
    if(this.seen.has(event.toolCallId)){this.stop(new MillforgeError('CLOSURE_UNCERTAIN','duplicate observed tool identity'));return;}
    this.seen.add(event.toolCallId);this.observed.add(event.toolCallId);
    if(nested){const parent=this.parent;if(!parent||parent.name!=='codemode'||event.parentToolCallId!==parent.id)refuse('orphan or deeper nested call','TOOL_ERROR');nestedId(event.toolCallId,parent.id,this.options.profile);if(event.toolCallId!==parent.id+'/'+parent.next++)refuse('nonconsecutive nested call identity','TOOL_ERROR');if(!this.options.profile.tools.includes(event.toolName))refuse('unapproved nested tool','TOOL_ERROR');}
    else{topLevelId(event.toolCallId,this.options.profile);if(this.parent||this.children.size||this.observed.size!==1)refuse('parallel top-level call','TOOL_ERROR');if(![...this.options.profile.tools,'codemode'].includes(event.toolName))refuse('unapproved top-level tool','TOOL_ERROR');}
    this.check();const args=object(event.args);if(bytes(canonical(args))>16384)refuse('tool arguments exceed captured bound','OUTPUT_LIMIT');
    if(this.calls>=this.options.limits.tool_calls)refuse('shared tool call ceiling reached','LIMIT_EXHAUSTED');
    this.options.emit('tool_start',{call_id:event.toolCallId,name:event.toolName,arguments:args,parent_call_id:event.parentToolCallId??null});
    this.calls++;this.observed.delete(event.toolCallId);
    if(nested){this.children.set(event.toolCallId,event.toolName);this.parent!.started++;this.nested++;}
    else{const deadline=Math.min(this.options.deadline,performance.now()+this.options.limits.tool_timeout_seconds*1000),id=event.toolCallId;this.parent={id,name:event.toolName,deadline,started:0,ended:0,next:1,timer:setTimeout(()=>{if(this.parent?.id===id)this.stop(new MillforgeError('TIMEOUT','original tool deadline expired'));},Math.max(1,deadline-performance.now()))};}
  }
  async before(event:ToolCallEvent):Promise<void>{
    try{
      this.check();const parent=this.parent;
      if(!parent)refuse('unrecorded tool admission refused','TOOL_ERROR');
      const admitted=event.parentToolCallId===undefined?event.toolCallId===parent.id&&event.toolName===parent.name:event.parentToolCallId===parent.id&&this.children.get(event.toolCallId)===event.toolName;
      if(!admitted)refuse('unrecorded tool admission refused','TOOL_ERROR');
      if(bytes(canonical(object(event.input)))>16384)refuse('tool arguments exceed captured bound','OUTPUT_LIMIT');
      await this.options.verify();this.check();
    }catch(error){this.stop(error);throw this.options.problem()??publicError(error);}
  }
  async after(event:ToolResultEvent):Promise<void>{
    const parent=this.parent;if(event.parentToolCallId!==undefined||event.toolName!=='codemode'||!parent||parent.id!==event.toolCallId)return;
    const until=Math.min(parent.deadline+1500,this.options.deadline+1500);
    while(this.children.size||this.observed.size){
      const remaining=until-performance.now();if(remaining<=0){this.stop(new MillforgeError('CLOSURE_UNCERTAIN','nested calls did not drain before close cutoff'));return;}
      await new Promise<void>(resolve=>{let timer:NodeJS.Timeout;const done=():void=>{clearTimeout(timer);this.waiters.delete(done);resolve();};this.waiters.add(done);timer=setTimeout(done,remaining);});
    }
  }
  private end(event:End):void{
    if(this.observed.delete(event.toolCallId)){this.wake();return;}
    const parent=this.parent;
    if(!parent||this.endedParents.has(event.parentToolCallId??event.toolCallId))refuse('late or orphan tool end','CLOSURE_UNCERTAIN');
    const child=event.parentToolCallId!==undefined;
    if(child?event.parentToolCallId!==parent.id||this.children.get(event.toolCallId)!==event.toolName:event.toolCallId!==parent.id||event.toolName!==parent.name)refuse('tool end identity mismatch','CLOSURE_UNCERTAIN');
    let recorded=false;
    try{
      if(!child&&performance.now()>=parent.deadline)this.stop(new MillforgeError('TIMEOUT','original tool deadline expired'));
      const result=event.result as {content?:{type:string;text?:string}[];details?:{fullOutputPath?:unknown}};
      const text=(result?.content??[]).filter(c=>c.type==='text').map(c=>c.text??'').join('\n');
      if(!child&&(this.children.size||this.observed.size))this.stop(new MillforgeError('CLOSURE_UNCERTAIN','parent returned before nested calls drained'));
      const join=!child&&parent.name==='codemode'?{parent_call_id:parent.id,started:parent.started,ended:parent.ended,outstanding:parent.started-parent.ended}:undefined;
      const payload:Obj={call_id:event.toolCallId,status:this.options.problem()?.code==='TIMEOUT'?'timeout':event.isError||!child&&(this.children.size>0||this.observed.size>0||parent.started!==parent.ended)?'failed':'completed',text,truncated:event.toolName==='codemode'?typeof result?.details?.fullOutputPath==='string':this.options.truncated(event.toolName,event.result),parent_call_id:event.parentToolCallId??null};
      this.options.emitEnd(payload,join);recorded=true;
    }finally{
      // A refused record does not leave physically ended work in the drain sets.
      if(!recorded)this.refusedEnd=true;
      if(child){this.children.delete(event.toolCallId);if(recorded)parent.ended++;}else{clearTimeout(parent.timer);this.endedParents.add(parent.id);this.parent=undefined;}
      this.wake();
    }
  }
  dispose():void{if(this.parent)clearTimeout(this.parent.timer);this.wake();}
}
