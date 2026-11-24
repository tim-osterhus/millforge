/** Pure launch boundary. Pi is loaded only inside the owned child after preadmission. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {fork,type ChildProcess} from 'node:child_process';
import {type Closure,type Obj,type Request,type Result,MillforgeError,codes,choice,parseJson,refuse,tightenLimits,validateRequest} from './contracts.js';
import {loadProfile,regularFile,resolveProfile,hash,publicError,type SelectionEnv} from './profile.js';
import {artifactManifest,publishResult,requiredArtifacts} from './artifacts.js';
import {authHome,ownedEnvironment} from './auth.js';
export interface ExternalTool extends Obj {name:string;path:string;sha256:string;size:number;stat:Obj}
export interface RunOptions {profile?:string;request:string;outputDir:string;env:NodeJS.ProcessEnv;signal?:AbortSignal;authHome?:string}
export interface WorkerInput {dir:string;profileDir:string;request:Request;credential?:string;authHome?:string;tools:ExternalTool[];deadlineNs:string;startedAt:string}
export interface WorkerReply {draft:Result;spills:string[]}
export async function executableIdentity(name:string,searchPath:string):Promise<ExternalTool>{
  for(const location of searchPath.split(path.delimiter).filter(Boolean)){
    const file=path.resolve(location,name);try{await fs.access(file,fs.constants.X_OK);const real=await fs.realpath(file),st=await fs.stat(real);if(!st.isFile())continue;const data=await regularFile(real,32*1024*1024);return {name,path:real,sha256:hash(data),size:data.length,stat:{dev:st.dev,ino:st.ino,mode:st.mode,mtime_ms:st.mtimeMs}};}catch{/* Continue explicit PATH only. */}
  }return refuse(`required executable ${name} is unavailable`,'UNSUPPORTED_CAPABILITY');
}
export async function verifyExecutables(tools:ExternalTool[],searchPath?:string):Promise<void>{
  for(const expected of tools){try{const st=await fs.stat(expected.path),data=await regularFile(expected.path,32*1024*1024);const current:ExternalTool={name:expected.name,path:expected.path,sha256:hash(data),size:data.length,stat:{dev:st.dev,ino:st.ino,mode:st.mode,mtime_ms:st.mtimeMs}};if(JSON.stringify(current)!==JSON.stringify(expected)||searchPath!==undefined&&JSON.stringify(await executableIdentity(expected.name,searchPath))!==JSON.stringify(expected))refuse('captured executable identity changed','UNSUPPORTED_CAPABILITY');}catch{refuse('captured executable is unavailable or changed','UNSUPPORTED_CAPABILITY');}}
}
function groupExists(pid:number):boolean{try{process.kill(-pid,0);return true;}catch(error){if((error as NodeJS.ErrnoException).code==='ESRCH')return false;throw error;}}
function killGroup(child:ChildProcess,signal:NodeJS.Signals):void{if(child.pid)try{process.kill(-child.pid,signal);}catch(error){if((error as NodeJS.ErrnoException).code!=='ESRCH')throw error;}}
/** One invocation, one worker, one monotonic deadline; no resumable execution state. */
export async function runInvocation(options:RunOptions):Promise<Result>{
  if(options.signal?.aborted)refuse('invocation cancelled','CANCELLED');
  const started=performance.now(),startedNs=process.hrtime.bigint(),startedAt=new Date().toISOString();
  const profileDir=await resolveProfile(options.profile,options.env as SelectionEnv),loaded=await loadProfile(profileDir);
  const request=validateRequest(parseJson(await regularFile(options.request,32*1024*1024)),loaded.profile,loaded.digest),limits=tightenLimits(loaded.profile,request.limits);
  if(request.workspace!==null&&!((await fs.stat(request.workspace)).isDirectory()))refuse('workspace must be a directory');
  const tools:ExternalTool[]=[];for(const name of [...new Set(loaded.profile.tools.filter(n=>n==='find'||n==='grep').map(n=>n==='find'?'fd':'rg'))].sort())tools.push(await executableIdentity(name,options.env.PATH??''));
  const credentialName=loaded.profile.schema==='millforge-profile/v1'?loaded.profile.model.credential_env:loaded.profile.model.auth.kind==='env'?loaded.profile.model.auth.name:null,credential=loaded.profile.schema==='millforge-profile/v1'&&credentialName!==null?options.env[credentialName]:undefined;if(loaded.profile.schema==='millforge-profile/v1'&&credentialName!==null&&!credential)refuse('selected credential is unavailable','AUTH_UNAVAILABLE');
  const selectedAuthHome=loaded.profile.schema!=='millforge-profile/v1'&&loaded.profile.model.auth.kind==='oauth'?authHome(options.authHome,options.env):undefined;
  // Worker loads the original once more and must match the admitted request digest before creating effects.
  const dir=path.resolve(options.outputDir);try{await fs.lstat(dir);return refuse('output directory must be new');}catch(error){if(error instanceof MillforgeError)throw error;if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
  const env:NodeJS.ProcessEnv={...ownedEnvironment(dir,options.env),MILLFORGE_WORKER:'1',...(loaded.profile.schema==='millforge-profile/v3'?{PI_CODEMODE_MAX_WORKERS:'1'}:{})};
  const child=fork(new URL('./pi-host.js',import.meta.url),[],{env,detached:true,stdio:['ignore','ignore','ignore','ipc'],serialization:'json',execArgv:[]});
  let reply:WorkerReply|undefined,failure:MillforgeError|undefined,requested=false,termTimer:NodeJS.Timeout|undefined,killTimer:NodeJS.Timeout|undefined;
  const stop=():void=>{if(requested)return;requested=true;if(child.connected)child.send({cancel:true},()=>{});termTimer=setTimeout(()=>killGroup(child,'SIGTERM'),1500);killTimer=setTimeout(()=>killGroup(child,'SIGKILL'),3500);};
  const signalStop=():void=>stop();process.on('SIGINT',signalStop);process.on('SIGTERM',signalStop);options.signal?.addEventListener('abort',signalStop,{once:true});if(options.signal?.aborted)stop();
  const outer=setTimeout(()=>{failure=new MillforgeError('TIMEOUT','outer invocation deadline expired');killGroup(child,'SIGKILL');},Math.max(1,limits.elapsed_seconds*1000-(performance.now()-started)));
  try{
    await new Promise<void>((resolve,reject)=>{
      child.on('message',(message:unknown)=>{const m=message as {ready?:boolean;credentialRequest?:boolean;reply?:WorkerReply;error?:{code:string}};if(m.ready){const input:WorkerInput={dir,profileDir,request,tools,deadlineNs:(startedNs+BigInt((limits.elapsed_seconds-5)*1e9)).toString(),startedAt,...(credential===undefined?{}:{credential}),...(selectedAuthHome?{authHome:selectedAuthHome}:{})};child.send(input);}else if(m.credentialRequest){const selected=requested||credentialName===null?undefined:options.env[credentialName];child.send({credentialReply:true,...(selected===undefined?{}:{credential:selected})});}else if(m.reply)reply=m.reply;else if(m.error)failure=new MillforgeError(choice(m.error.code,codes),'worker refused before capture');});
      child.once('error',reject);child.once('exit',(code)=>{if(code!==0&&!failure)failure=new MillforgeError('CLOSURE_UNCERTAIN','worker exited without normal closure');resolve();});
    });
  }finally{clearTimeout(outer);if(termTimer)clearTimeout(termTimer);if(killTimer)clearTimeout(killTimer);process.off('SIGINT',signalStop);process.off('SIGTERM',signalStop);options.signal?.removeEventListener('abort',signalStop);}
  const groupLeft=child.pid!==undefined&&groupExists(child.pid);if(groupLeft){killGroup(child,'SIGKILL');failure=new MillforgeError('CLOSURE_UNCERTAIN','worker left owned processes');}
  if(!reply)throw failure??new MillforgeError(requested?'CANCELLED':'IO_ERROR','worker did not publish a closed candidate');
  if(failure||groupLeft)throw failure;if(requested&&reply.draft.status==='completed')throw new MillforgeError('CANCELLED','cancelled before publication cutoff');
  const draft=reply.draft;const closure:Closure={...draft.closure,worker:'closed'};draft.closure=closure;
  // All writers/processes are gone before final publication. Never adopt arbitrary paths from a scan.
  await fs.rm(path.join(dir,'pi'),{recursive:true,force:true});
  if(reply.spills.length===0)await fs.rmdir(path.join(dir,'tmp'));
  draft.artifacts=await artifactManifest(dir,[...requiredArtifacts(loaded),...reply.spills]);
  draft.timing.ended_at=new Date().toISOString();draft.timing.elapsed_ms=performance.now()-started;
  return publishResult(dir,draft);
}
