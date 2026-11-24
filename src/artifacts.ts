/** Offline capture/result producers and inspection. Supplied facts are data, not execution proof. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {openSync,writeSync,closeSync} from 'node:fs';
import {type Artifact,type Capture,type Json,type Obj,type Profile,type Request,type Result,type Trace,bytes,canonical,currentRuntimeGuards,parseJson,refuse,relative,tightenLimits,validateCapture,validateRequest,validateResult,validateTrace} from './contracts.js';
import {type LoadedProfile,atomicNew,checkedPath,hash,loadProfile,regularFile,writeExclusive} from './profile.js';
// Worst-case six-byte JSON escaping plus bounded lists/metadata fits 32 MiB.
const MAX_RECORD_BYTES=32*1024*1024;
function artifactReadLimit(rel:string,b?:Boundary):number{if(rel==='profile/profile.json'||rel.startsWith('profile/'))return 4194304;if(b?.capture.runtime_guards){if(rel==='trace.jsonl')return b.capture.limits.trace_bytes;if(rel==='request.json'||rel==='capture.json')return Number(b.capture.runtime_guards.encoded_record_bytes);return b.capture.limits.tool_output_bytes;}if(rel==='request.json'||rel==='capture.json')return MAX_RECORD_BYTES;if(rel==='trace.jsonl')return 8388608;return 262144;}
export interface Boundary {profile:LoadedProfile;request:Request;capture:Capture}
export interface PreparedRun {dir:string;profile:LoadedProfile;request:Request}
/** Create the exclusive owned tree before any Pi import. No existing tree is adopted. */
export async function prepareCapture(dir:string,loaded:LoadedProfile,requestValue:unknown,deferRecords=false):Promise<PreparedRun>{
  const request=validateRequest(requestValue,loaded.profile,loaded.digest);
  if(request.workspace!==null){const st=await fs.stat(request.workspace);if(!st.isDirectory())refuse('workspace must be an existing directory');}
  await fs.mkdir(dir,{mode:0o700});const run={dir,profile:loaded,request};return deferRecords?run:publishPreparedInputs(run);
}
/** After capability/auth admission, freeze the input records in the already owned run tree. */
export async function publishPreparedInputs(run:PreparedRun):Promise<PreparedRun>{
  const {dir,profile:loaded,request}=run;const profileDir=path.join(dir,'profile');await fs.mkdir(profileDir,{mode:0o700});
  for(const [rel,data]of loaded.assets){const file=path.join(profileDir,rel);await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});await writeExclusive(file,data);}
  const rawRequest=JSON.stringify(request)+'\n';if(loaded.profile.schema!=='millforge-profile/v1'&&bytes(rawRequest)>currentRuntimeGuards.encoded_record_bytes)refuse('encoded request record exceeds host guard','CONTEXT_LIMIT');
  await writeExclusive(path.join(profileDir,'profile.json'),loaded.raw);await writeExclusive(path.join(dir,'request.json'),rawRequest);
  return {dir,profile:{...loaded,dir:profileDir},request};
}
/** Publish immutable actual session facts before the first model/tool effect. */
export async function finishCapture(run:PreparedRun,captureValue:unknown):Promise<Boundary>{
  const loaded=run.profile,request=run.request,capture=validateCapture(captureValue,loaded.profile,request,loaded.digest);
  if(capture.resources.some((r,i)=>r.size!==loaded.resources[i]!.size))refuse('captured resource size mismatch');
  if(loaded.resources.reduce((n,r)=>n+r.size,0)+bytes(capture.system_prompt)>capture.limits.context_bytes)refuse('captured context exceeds limit','CONTEXT_LIMIT');
  const encoded=JSON.stringify(capture)+'\n';if(capture.runtime_guards&&bytes(encoded)>Number(capture.runtime_guards.encoded_record_bytes))refuse('encoded capture exceeds host guard','CONTEXT_LIMIT');
  await writeExclusive(path.join(run.dir,'capture.json'),encoded);return {profile:loaded,request,capture};
}
/** Offline compatibility producer; supplied facts do not prove execution. */
export async function createCapture(dir:string,loaded:LoadedProfile,requestValue:unknown,captureValue:unknown):Promise<Boundary>{
  validateCapture(captureValue,loaded.profile,validateRequest(requestValue,loaded.profile,loaded.digest),loaded.digest);
  return finishCapture(await prepareCapture(dir,loaded,requestValue),captureValue);
}
/** Synchronous owned writes are drained before return; the outer worker deadline bounds a stuck OS write. */
export class TraceWriter {
  private fd:number;private sequence=0;private retained=0;private last=0;private closed=false;private models=0;
  constructor(dir:string,private readonly boundary:Boundary,private readonly started:number){this.fd=openSync(path.join(dir,'trace.jsonl'),'wx',0o600);}
  append(kind:string,payload:Obj,extraReserve=0):void{
    if(this.closed)refuse('trace writer closed','IO_ERROR');
    const elapsed=Math.max(this.last,performance.now()-this.started);
    const capture=this.boundary.capture;
    const schema=capture.schema==='millforge-capture/v5'?'millforge-trace/v5':capture.schema==='millforge-capture/v4'?'millforge-trace/v4':capture.schema==='millforge-capture/v3'?'millforge-trace/v3':'millforge-trace/v2';
    const value=validateTrace({schema,request_id:this.boundary.request.request_id,sequence:this.sequence+1,elapsed_ms:elapsed,kind,payload},{...this.boundary.profile.profile,limits:capture.limits},this.boundary.request,capture);
    const data=Buffer.from(JSON.stringify(value)+'\n'),reserve=(['terminal','closure','cancel','limit'].includes(kind)?0:16384)+extraReserve;
    if(capture.runtime_guards&&data.length>Number(capture.runtime_guards.encoded_record_bytes))refuse('encoded trace record exceeds host guard','TRACE_LIMIT');
    if(this.retained+data.length>this.boundary.capture.limits.trace_bytes-reserve)refuse('trace byte ceiling reached','TRACE_LIMIT');
    let written=0;while(written<data.length)written+=writeSync(this.fd,data,written,data.length-written);
    this.retained+=data.length;this.sequence++;this.last=elapsed;if(kind==='model_start')this.models++;
  }
  /** Admit the exact serialized pair before either record is written. */
  appendToolEnd(payload:Obj,join?:Obj):void{
    if(!join){this.append('tool_end',payload);return;}
    if(this.closed)refuse('trace writer closed','IO_ERROR');
    if(this.boundary.capture.schema!=='millforge-capture/v4')refuse('nested join requires v4','UNSUPPORTED_VERSION');
    const elapsed=Math.max(this.last,performance.now()-this.started);
    const records=[['nested_join',join],['tool_end',payload]] as const;
    const data=Buffer.concat(records.map(([kind,value],index)=>{
      const record=validateTrace({schema:'millforge-trace/v4',request_id:this.boundary.request.request_id,sequence:this.sequence+index+1,elapsed_ms:elapsed,kind,payload:value},{...this.boundary.profile.profile,limits:this.boundary.capture.limits},this.boundary.request,this.boundary.capture);
      const encoded=Buffer.from(JSON.stringify(record)+'\n');
      if(encoded.length>Number(this.boundary.capture.runtime_guards!.encoded_record_bytes))refuse('encoded trace record exceeds host guard','TRACE_LIMIT');return encoded;
    }));
    if(this.retained+data.length>this.boundary.capture.limits.trace_bytes-16384)refuse('trace byte ceiling reached','TRACE_LIMIT');
    let written=0;while(written<data.length)written+=writeSync(this.fd,data,written,data.length-written);
    this.retained+=data.length;this.sequence+=2;this.last=elapsed;
  }
  get modelStarts():number{return this.models;}
  get size():number{return this.retained;}
  close():void{if(!this.closed){closeSync(this.fd);this.closed=true;}}
}
async function boundary(dir:string):Promise<Boundary>{
  const st=await fs.lstat(dir);if(!st.isDirectory()||st.isSymbolicLink())refuse('run must be a real directory');const profile=await loadProfile(await checkedPath(dir,'profile'));
  const request=validateRequest(parseJson(await regularFile(await checkedPath(dir,'request.json'),MAX_RECORD_BYTES)),profile.profile,profile.digest);
  const capture=validateCapture(parseJson(await regularFile(await checkedPath(dir,'capture.json'),MAX_RECORD_BYTES)),profile.profile,request,profile.digest);
  if(capture.resources.some((r,i)=>r.size!==profile.resources[i]!.size))refuse('captured resource size mismatch');
  if(profile.resources.reduce((n,r)=>n+r.size,0)+bytes(capture.system_prompt)>capture.limits.context_bytes)refuse('captured context exceeds limit');return {profile,request,capture};
}
export async function writeTrace(dir:string,values:unknown[]):Promise<void>{
  const b=await boundary(dir);const profile={...b.profile.profile,limits:b.capture.limits};const trace=values.map(v=>validateTrace(v,profile,b.request,b.capture));const data=trace.map(v=>JSON.stringify(v)+'\n').join('');if(bytes(data)>b.capture.limits.trace_bytes)refuse('trace limit exceeded');await atomicNew(path.join(dir,'trace.jsonl'),data);
}
export async function artifactManifest(dir:string,ownedPaths:string[]):Promise<Artifact[]>{
  const b=await boundary(dir);const result:Artifact[]=[];for(const rel of [...ownedPaths].sort()){relative(rel);if(rel==='result.json'||result.some(a=>a.path===rel))refuse('invalid owned artifact declaration');const data=await regularFile(await checkedPath(dir,rel),artifactReadLimit(rel,b));result.push({path:rel,size:data.length,sha256:hash(data),media_type:b.capture.schema==='millforge-capture/v4'&&/^tmp\/pi-codemode-.*\.(png|jpg|gif|webp)$/.test(rel)?'application/octet-stream':rel.endsWith('.json')?'application/json':rel.endsWith('.jsonl')?'application/x-ndjson':'text/plain'});}return result;
}
export function requiredArtifacts(loaded:LoadedProfile):string[]{return ['capture.json','request.json','trace.jsonl','profile/profile.json',...loaded.resources.map(r=>'profile/'+r.path)].sort();}
async function validateSaved(dir:string,b:Boundary,result:Result):Promise<Trace[]>{
  const v5=b.capture.schema==='millforge-capture/v5';
  const declared=new Set(result.artifacts.map(a=>a.path));const required=new Set(requiredArtifacts(b.profile));const spillBytes=result.artifacts.filter(a=>!required.has(a.path)).reduce((sum,a)=>sum+a.size,0);if(spillBytes>b.capture.limits.tool_output_bytes)refuse('retained tool artifacts exceed output limit');for(const rel of required)if(!declared.has(rel))refuse('missing required artifact declaration');
  if(v5&&declared.size!==required.size)refuse('Python runs cannot adopt additional artifacts');
  const allowedDirs=new Set<string>();for(const rel of declared){let parent=path.posix.dirname(rel);while(parent!=='.'){allowedDirs.add(parent);parent=path.posix.dirname(parent);}}
  const visit=async(root:string,rel=''):Promise<void>=>{for(const name of await fs.readdir(root)){const child=rel?rel+'/'+name:name;const file=path.join(root,name);const st=await fs.lstat(file);if(st.isSymbolicLink())refuse('saved symlink refused');if(st.isDirectory()){if(!allowedDirs.has(child))refuse('unowned saved directory');await visit(file,child);}else if(!st.isFile()||(child!=='result.json'&&!declared.has(child)))refuse('unowned or partial saved record');}};
  await visit(dir);
  for(const a of result.artifacts){const data=await regularFile(await checkedPath(dir,a.path),artifactReadLimit(a.path,b));if(data.length!==a.size||hash(data)!==a.sha256)refuse('artifact hash or size mismatch');}
  const raw=await regularFile(await checkedPath(dir,'trace.jsonl'),b.capture.limits.trace_bytes);if(raw.length!==result.counts.trace_bytes||!raw.length||raw[raw.length-1]!==10)refuse('truncated or miscounted trace');let content:string;try{content=new TextDecoder('utf-8',{fatal:true}).decode(raw);}catch{return refuse('invalid trace UTF-8');}
  const profile={...b.profile.profile,limits:b.capture.limits};const trace=content.slice(0,-1).split('\n').map(line=>{
    if(v5&&bytes(line)+1>Number(b.capture.runtime_guards!.encoded_record_bytes))refuse('encoded trace record exceeds host guard');
    return validateTrace(parseJson(line),profile,b.request,b.capture);
  });
  const v4=b.capture.schema==='millforge-capture/v4',v2=b.capture.schema!=='millforge-capture/v1';let payloadSeen=false,httpSeen=false,httpAttempts=0;
  let elapsed=-1,models=0,tools=0,activeModel:number|null=null,activeTool:string|null=null,toolPhase=false,toolBytes=0,partialBytes=0;let lastStop:string|null=null;const seenTools=new Set<string>();let terminal:Trace|undefined;let closure:Trace|undefined;
  let activeName:string|null=null,nested=0,childStarts=0,childEnds=0,nextChild=1,joined=false,unclosedJoin=false,unsettledPython=false;const children=new Set<string>();
  trace.forEach((t,i)=>{if(t.sequence!==i+1||t.elapsed_ms<elapsed||t.elapsed_ms>Number(result.timing.elapsed_ms))refuse('trace sequence or timing mismatch');elapsed=t.elapsed_ms;
    if(i===0&&t.kind!=='capture'||i>0&&t.kind==='capture')refuse('trace must start with one capture');
    if(closure)refuse('trace event after closure');if(terminal&&t.kind!=='closure')refuse('trace event after terminal');
    if(t.kind==='model_start'){if(activeModel!==null||activeTool!==null||v4&&children.size>0||v5&&(toolPhase||unsettledPython)||t.payload.attempt!==models+1)refuse('invalid model attempt sequence');models++;activeModel=models;toolPhase=false;payloadSeen=false;httpSeen=false;}
    if(t.kind==='provider_payload'){if(t.payload.attempt!==activeModel||payloadSeen||httpSeen)refuse('orphan or duplicate payload observation');payloadSeen=true;}
    if(t.kind==='http_attempt'){if(t.payload.attempt!==activeModel||!payloadSeen||httpSeen)refuse('orphan or duplicate HTTP attempt');httpSeen=true;httpAttempts++;if(httpAttempts>b.capture.limits.model_requests||profile.mode==='generate'&&httpAttempts>1)refuse('HTTP attempt ceiling exceeded');}
    if(t.kind==='model_end'){if(t.payload.attempt!==activeModel)refuse('orphan model end');if(v2&&['normal','tool'].includes(String(t.payload.stop))&&(!payloadSeen||!httpSeen))refuse('successful model end lacks public observations');activeModel=null;lastStop=String(t.payload.stop);toolPhase=lastStop==='tool';}
    if(t.kind==='tool_start'){
      const call=String(t.payload.call_id),child=v4&&t.payload.parent_call_id!==null;
      if(seenTools.has(call)||activeModel!==null||!toolPhase)refuse('duplicate or out-of-phase tool call');
      if(child){if(activeName!=='codemode'||t.payload.parent_call_id!==activeTool||joined||call!==activeTool+'/'+nextChild++)refuse('invalid nested ancestry or order');children.add(call);childStarts++;nested++;}
      else{if(activeTool!==null||v4&&children.size)refuse('duplicate or parallel tool call');activeTool=call;activeName=String(t.payload.name);childStarts=0;childEnds=0;nextChild=1;joined=false;}
      seenTools.add(call);tools++;if(v5)toolPhase=false;
    }
    if(t.kind==='nested_join'){
      if(!v4||activeName!=='codemode'||t.payload.parent_call_id!==activeTool||joined||t.payload.started!==childStarts||t.payload.ended!==childEnds||t.payload.outstanding!==children.size)refuse('invalid nested join');joined=true;if(children.size)unclosedJoin=true;
      if(trace[i+1]?.kind!=='tool_end'||trace[i+1]?.payload.call_id!==activeTool||trace[i+1]?.payload.parent_call_id!==null)refuse('join must immediately precede parent end');
    }
    if(t.kind==='tool_end'){
      if(v4&&t.payload.parent_call_id!==null){const call=String(t.payload.call_id);if(t.payload.parent_call_id!==activeTool||activeName!=='codemode'||joined||!children.delete(call))refuse('orphan or late nested tool end');childEnds++;}
      else{if(t.payload.call_id!==activeTool)refuse('orphan tool end');if(v4&&activeName==='codemode'){if(!joined)refuse('missing nested join');if(t.payload.status==='completed'&&children.size)refuse('completed parent has outstanding children');}activeTool=null;activeName=null;}
      if(v5&&t.payload.cell_settled!==true)unsettledPython=true;
      toolBytes+=bytes(String(t.payload.text));
    }
    if(t.kind==='output_partial')partialBytes+=bytes(String(t.payload.text));if(t.kind==='terminal')terminal=t;if(t.kind==='closure')closure=t;
  });
  if(!terminal||!closure||terminal.payload.status!==result.status||terminal.payload.code!==(result.diagnostic?.code??null)||canonical(closure.payload)!==canonical(result.closure as unknown as Obj))refuse('terminal or closure record mismatch');
  if(v4&&nested!==result.counts.nested_tool_calls)refuse('nested result count mismatch');
  if((v4||v5)&&(activeTool!==null||children.size)&&result.closure.tools==='closed')refuse('pending tools require nonclosed closure');
  if(models!==result.counts.model_requests||tools!==result.counts.tool_calls||toolBytes+spillBytes>b.capture.limits.tool_output_bytes||partialBytes>b.capture.limits.output_bytes||partialBytes>Number(result.counts.output_bytes))refuse('trace count or aggregate output mismatch');
  if(result.status==='completed'&&(activeModel!==null||activeTool!==null||v4&&(children.size>0||unclosedJoin)||unsettledPython||lastStop!=='normal'||models<1))refuse('completed trace has unsettled work or invalid finish');
  const outputBytes=result.output===null?0:bytes(typeof result.output==='string'?result.output:canonical(result.output));if(outputBytes>Number(result.counts.output_bytes))refuse('output byte count is too small');return trace;
}
/** Publish one final record after complete offline consistency validation. */
export async function publishResult(dir:string,value:unknown):Promise<Result>{
  const b=await boundary(dir);const result=validateResult(value,b.profile.profile,b.request,b.capture);await validateSaved(dir,b,result);const encoded=JSON.stringify(result)+'\n';if(b.capture.runtime_guards&&bytes(encoded)>Number(b.capture.runtime_guards.encoded_record_bytes))refuse('encoded result exceeds host guard','OUTPUT_LIMIT');await atomicNew(path.join(dir,'result.json'),encoded);return result;
}
/** Inspect only saved owned bytes. It never relaunches, probes or repairs a run. */
export async function inspectRun(dir:string):Promise<{result:Result;capture:Capture;trace_records:number}>{
  const b=await boundary(dir);const limit=b.capture.schema==='millforge-capture/v5'?Number(b.capture.runtime_guards!.encoded_record_bytes):MAX_RECORD_BYTES;
  const result=validateResult(parseJson(await regularFile(await checkedPath(dir,'result.json'),limit)),b.profile.profile,b.request,b.capture);const trace=await validateSaved(dir,b,result);return {result,capture:b.capture,trace_records:trace.length};
}
