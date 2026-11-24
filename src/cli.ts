#!/usr/bin/env node
/** Pure command boundary; the finite worker is loaded only for run. */
import {realpathSync} from 'node:fs';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {type SelectionEnv,copyProfile,createProfile,diffProfiles,editProfile,loadProfile,publicError,restoreProfile,selectProfile} from './profile.js';
import {integer,refuse} from './contracts.js';
import {inspectRun} from './artifacts.js';
interface Arguments {positional:string[];options:Record<string,string>}
function argumentsFor(args:string[],allowed:string[]):Arguments{
  const positional:string[]=[],options:Record<string,string>=Object.create(null) as Record<string,string>;
  for(let i=0;i<args.length;i++){const value=args[i]!;if(value.startsWith('--')){if(!allowed.includes(value)||options[value]!==undefined)refuse('unknown or repeated option');const next=args[++i];if(next===undefined||next.startsWith('--'))refuse('option value required');options[value]=next;}else positional.push(value);}return {positional,options};
}
function arity(values:string[],length:number):void{if(values.length!==length)refuse('wrong number of command arguments');}
function required(options:Record<string,string>,key:string):string{return options[key]??refuse(`required option ${key}`);}
/** Return a CLI exit code; supplied streams/environment make pure command tests explicit. */
export async function main(args:string[],env:SelectionEnv,write:(value:string)=>void,writeError:(value:string)=>void):Promise<number>{
  try{
    const [command,subcommand,...rest]=args;let output:unknown;
    if(command==='profile'){
      if(!subcommand)refuse('profile command required');
      const allowed:Record<string,string[]>={create:['--from'],clone:['--id'],select:[],show:[],validate:[],edit:['--from'],diff:[],export:[],import:[],restore:['--revision']};
      const flags=allowed[subcommand];if(!flags)refuse('unknown profile command');const {positional:p,options:o}=argumentsFor(rest,flags);
      switch(subcommand){
        case'create':arity(p,1);output={digest:(await createProfile(p[0]!,required(o,'--from'))).digest};break;
        case'clone':arity(p,2);output={digest:(await copyProfile(p[0]!,p[1]!,required(o,'--id'))).digest};break;
        case'select':arity(p,1);output={path:await selectProfile(p[0]!,env)};break;
        case'show':{arity(p,1);const loaded=await loadProfile(p[0]!);output={profile:loaded.profile,digest:loaded.digest};break;}
        case'validate':arity(p,1);output={valid:true,digest:(await loadProfile(p[0]!)).digest};break;
        case'edit':arity(p,1);output={digest:(await editProfile(p[0]!,required(o,'--from'))).digest};break;
        case'diff':arity(p,2);output=await diffProfiles(p[0]!,p[1]!);break;
        case'export':case'import':arity(p,2);output={digest:(await copyProfile(p[0]!,p[1]!)).digest};break;
        case'restore':{arity(p,1);const value=required(o,'--revision');if(!/^[1-9]\d*$/.test(value))refuse('revision must be a positive integer');output={digest:(await restoreProfile(p[0]!,integer(Number(value),1))).digest};break;}
      }
    }else if(command==='tui'||(command===undefined&&process.stdin.isTTY&&process.stdout.isTTY)){
      const {positional,options}=argumentsFor(args.slice(1),['--profile']);arity(positional,0);
      const {launchInteractive}=await import('./interactive-launcher.js');
      return await launchInteractive(options['--profile'],env,writeError);
    }else if(command==='inspect'){
      const {positional,options}=argumentsFor(args.slice(1),['--run']);arity(positional,0);output=await inspectRun(required(options,'--run'));
    }else if(command==='run'){
      const {positional,options}=argumentsFor(args.slice(1),['--profile','--request','--output-dir','--auth-home']);arity(positional,0);const {runInvocation}=await import('./lifecycle.js');const result=await runInvocation({...(options['--profile']?{profile:options['--profile']}:{}),...(options['--auth-home']?{authHome:options['--auth-home']}:{}),request:required(options,'--request'),outputDir:required(options,'--output-dir'),env:{...process.env,...env}});write(JSON.stringify(result,null,2)+'\n');return result.status==='completed'?0:result.status==='timeout'?124:result.status==='cancelled'?130:3;
    }else if(command==='auth'){
      if(!subcommand||!['login','status','refresh','logout'].includes(subcommand))refuse('unknown auth command');
      const {positional,options}=argumentsFor(rest,['--provider','--auth-home','--receipt',...(subcommand==='refresh'?['--min-validity-seconds']:[])]);arity(positional,0);
      const lease=options['--min-validity-seconds'];if(lease!==undefined&&!/^[1-9]\d*$/.test(lease))refuse('validity must be a positive integer');
      const {runAuthCommand}=await import('./auth.js');return await runAuthCommand({command:subcommand as 'login'|'status'|'refresh'|'logout',provider:required(options,'--provider'),env:{...process.env,...env},...(options['--auth-home']?{authHome:options['--auth-home']}:{}),...(options['--receipt']?{receipt:options['--receipt']}:{}),...(lease===undefined?{}:{minValiditySeconds:integer(Number(lease),1,Math.floor(Number.MAX_SAFE_INTEGER/1000))})});
    }else if(command==='optimize'){
      const {dispatchOptimization}=await import('./optimization-dispatch.js');
      return await dispatchOptimization(args.slice(1),fileURLToPath(import.meta.url),process.env,write,writeError);
    }else refuse('expected profile, inspect or run command');
    write(JSON.stringify(output,null,2)+'\n');return 0;
  }catch(error){const diagnostic=publicError(error);writeError(JSON.stringify({code:diagnostic.code,message:diagnostic.message})+'\n');return 2;}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(realpathSync(process.argv[1])).href){
  process.exitCode=await main(process.argv.slice(2),{...(process.env.HOME?{HOME:process.env.HOME}:{}),...(process.env.XDG_CONFIG_HOME?{XDG_CONFIG_HOME:process.env.XDG_CONFIG_HOME}:{})},value=>process.stdout.write(value),value=>process.stderr.write(value));
}
