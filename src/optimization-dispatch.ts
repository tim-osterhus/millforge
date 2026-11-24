/** Optional executable delegation. No campaign, runtime, auth or provider imports. */
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {access,readFile,realpath,stat} from 'node:fs/promises';
import {delimiter,isAbsolute,join} from 'node:path';
import {StringDecoder} from 'node:string_decoder';

import {optimizerIdentity} from './versions.js';
interface Identity {path:string;sha256:string}
async function identity(file:string,executable=true):Promise<Identity>{
  const resolved=await realpath(file);if(executable)await access(resolved,constants.X_OK);
  if(!(await stat(resolved)).isFile())throw new Error('not an executable file');
  return {path:resolved,sha256:createHash('sha256').update(await readFile(resolved)).digest('hex')};
}
async function resolveOptimizer(explicit:string|undefined,pathValue:string|undefined):Promise<Identity>{
  if(explicit!==undefined)return await identity(explicit);
  for(const directory of (pathValue??'').split(delimiter)){
    try{return await identity(join(directory||'.','millforge-optimize'));}catch{/* Search PATH only for this optional command. */}
  }
  throw new Error('optional executable missing');
}
function compatible(stdout:Buffer):boolean{
  try{
    const text=new TextDecoder('utf-8',{fatal:true}).decode(stdout);
    if(!text.endsWith('\n'))return false;
    const parsed:unknown=JSON.parse(text);
    if(parsed===null||Array.isArray(parsed)||typeof parsed!=='object')return false;
    const record=parsed as Record<string,unknown>;
    // All identity values are strings, so this also detects duplicate/escaped keys.
    const keys=[...text.matchAll(/("(?:[^"\\]|\\.)*")\s*:/g)].map(match=>JSON.parse(match[1]!));
    return keys.length===3&&new Set(keys).size===3&&Object.keys(record).length===3&&Object.entries(optimizerIdentity).every(([key,value])=>record[key]===value);
  }catch{return false;}
}
function groupExists(pid:number):boolean{
  try{process.kill(-pid,0);return true;}catch(error){if((error as NodeJS.ErrnoException).code==='ESRCH')return false;throw error;}
}
function killGroup(pid:number,signal:NodeJS.Signals):void{
  try{process.kill(-pid,signal);}catch(error){if((error as NodeJS.ErrnoException).code!=='ESRCH')throw error;}
}
const delay=(ms:number)=>new Promise<void>(resolve=>setTimeout(resolve,ms));
interface Outcome {code:number|null;signal:NodeJS.Signals|null;stdout:Buffer;failed:boolean;uncertain:boolean}

/** Wait for exactly one owned process group; cancellation never starts another child. */
export async function dispatchOptimization(args:string[],baseFile:string,env:NodeJS.ProcessEnv,write:(text:string)=>void,writeError:(text:string)=>void):Promise<number>{
  const diagnostic=(code:string,message:string,exit=2):number=>{writeError(JSON.stringify({code,message})+'\n');return exit;};
  const forwarded:string[]=[];let explicit:string|undefined;
  for(let i=0;i<args.length;i++){
    const value=args[i]!;
    if(value==='--base-bin'||value.startsWith('--base-bin='))return diagnostic('OPTIMIZER_INCOMPATIBLE','--base-bin is owned by millforge');
    if(value==='--optimizer-bin'){
      const next=args[++i];
      if(explicit!==undefined||next===undefined||next.startsWith('--')||!isAbsolute(next))return diagnostic('OPTIMIZER_INCOMPATIBLE','--optimizer-bin requires one absolute executable path');
      explicit=next;
    }else if(value.startsWith('--optimizer-bin='))return diagnostic('OPTIMIZER_INCOMPATIBLE','use --optimizer-bin ABS');
    else forwarded.push(value);
  }
  let optimizer:Identity,base:Identity;
  try{optimizer=await resolveOptimizer(explicit,env.PATH);base=await identity(baseFile,false);}catch{return diagnostic('OPTIMIZER_UNAVAILABLE','optional optimizer or current base executable is unavailable');}
  let cancelled:NodeJS.Signals|undefined,stopActive:(()=>Promise<void>)|undefined;
  const cancel=(signal:NodeJS.Signals):void=>{if(cancelled)return;cancelled=signal;void stopActive?.();};
  const onInt=()=>cancel('SIGINT'),onTerm=()=>cancel('SIGTERM');
  process.on('SIGINT',onInt);process.on('SIGTERM',onTerm);
  const run=async(argv:string[],probe:boolean):Promise<Outcome>=>{
    const child=spawn(optimizer.path,argv,{env,detached:true,stdio:[probe?'ignore':'inherit','pipe','pipe']});
    // detached creates an owned POSIX group; the CLI remains foreground and awaits closure.
    let stdout=Buffer.alloc(0),failed=false,uncertain=false,cleanup:Promise<void>|undefined;
    const outputDecoder=new StringDecoder('utf8'),errorDecoder=new StringDecoder('utf8');
    const stop=():Promise<void>=>cleanup??=(async()=>{
      if(!child.pid)return;
      try{
        killGroup(child.pid,cancelled??'SIGTERM');
        // Only negotiation has a bounded kill escalation. Delegated work owns its cleanup.
        if(probe){await delay(250);if(groupExists(child.pid))killGroup(child.pid,'SIGKILL');}
      }catch{uncertain=true;}
    })();
    stopActive=stop;
    if(cancelled)void stop();
    const timer=probe?setTimeout(()=>{failed=true;void stop();},2000):undefined;
    child.stdout.on('data',(data:Buffer)=>{if(probe){if(stdout.length+data.length>4096){failed=true;void stop();}else stdout=Buffer.concat([stdout,data]);}else write(outputDecoder.write(data));});
    child.stderr.on('data',(data:Buffer)=>{if(probe){failed=true;void stop();}else writeError(errorDecoder.write(data));});
    const result=await new Promise<{code:number|null;signal:NodeJS.Signals|null}>(resolve=>{
      child.once('error',()=>{failed=true;resolve({code:null,signal:null});});
      child.once('exit',(code,signal)=>resolve({code,signal}));
    });
    if(timer)clearTimeout(timer);
    if(cleanup)await cleanup;
    if(child.pid){
      try{
        if(groupExists(child.pid)){
          // The leader has exited; sweep only leftover members of its exact owned group.
          killGroup(child.pid,'SIGKILL');
          for(let i=0;i<10&&groupExists(child.pid);i++)await delay(50);
          if(groupExists(child.pid))uncertain=true;
        }
      }catch{uncertain=true;}
    }
    // Drain the exited leader's pipes, then release any pipes inherited by descendants.
    await new Promise<void>(resolve=>{if(child.stdout.destroyed&&child.stderr.destroyed)resolve();else{const timer=setTimeout(resolve,100);child.once('close',()=>{clearTimeout(timer);resolve();});}});
    child.stdout.destroy();child.stderr.destroy();
    if(!probe){write(outputDecoder.end());writeError(errorDecoder.end());}
    stopActive=undefined;
    return {...result,stdout,failed,uncertain};
  };
  try{
    const negotiated=await run(['protocol'],true);
    if(negotiated.uncertain)return diagnostic('OPTIMIZER_CLOSURE_UNCERTAIN','optimizer process-group closure is unproven',3);
    if(cancelled)return cancelled==='SIGINT'?130:143;
    if(negotiated.failed||negotiated.code!==0||!compatible(negotiated.stdout))return diagnostic('OPTIMIZER_INCOMPATIBLE','optional optimizer protocol is incompatible');
    // Refuse a replacement between discovery/protocol and dispatch, for both executables.
    const currentOptimizer=await identity(optimizer.path),currentBase=await identity(base.path,false);
    if(currentOptimizer.sha256!==optimizer.sha256||currentBase.sha256!==base.sha256)return diagnostic('OPTIMIZER_INCOMPATIBLE','executable identity changed during negotiation');
    if(cancelled)return cancelled==='SIGINT'?130:143;
    const result=await run([...forwarded,'--base-bin',base.path],false);
    if(result.uncertain)return diagnostic('OPTIMIZER_CLOSURE_UNCERTAIN','optimizer process-group closure is unproven',3);
    if(result.failed)return diagnostic('OPTIMIZER_UNAVAILABLE','optional optimizer could not start');
    return result.code??(result.signal==='SIGINT'?130:result.signal==='SIGTERM'?143:3);
  }catch{return diagnostic('OPTIMIZER_INCOMPATIBLE','optional optimizer identity or I/O validation failed');}
  finally{process.off('SIGINT',onInt);process.off('SIGTERM',onTerm);}
}
