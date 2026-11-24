#!/usr/bin/env node
/** Optional foreground command boundary; protocol performs no discovery or execution. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {type Obj,OptimizationError,absolute,canonical,fail,json} from './contracts.js';
import {productionDependencies} from './lifecycle.js';
import {candidateCommand,inspectCapturedCampaign,loadCampaign,prepareCampaign,verifyCodemodeAssessmentScope} from './store.js';
import {adopt,assessCommand} from './evaluation.js';
import {runCampaign} from './method.js';
import {protocol} from './versions.js';
import {deploymentPaths} from './protected-deployment.js';
export {protocol} from './versions.js';
export async function main(args:string[],write:(s:string)=>void=s=>process.stdout.write(s),writeError:(s:string)=>void=s=>process.stderr.write(s)):Promise<number>{
 if(args.length===1&&args[0]==='protocol'){write(JSON.stringify(protocol)+'\n');return 0;}
 const controller=new AbortController(),stop=():void=>controller.abort();process.on('SIGINT',stop);process.on('SIGTERM',stop);
 try{const [command,...rest]=args;const commands:Record<string,string[]>={prepare:['--campaign','--output'],run:['--campaign','--approval'],candidate:['--campaign','--proposal','--output'],assess:['--campaign','--evidence','--output'],adopt:['--campaign','--report','--approval','--destination'],inspect:['--campaign']};const required=commands[command??''];if(!required)fail('expected prepare/run/candidate/assess/adopt/inspect or protocol');const options:Record<string,string>=Object.create(null);for(let i=0;i<rest.length;i++){const key=rest[i]!;if(![...required,'--base-bin'].includes(key)||options[key]!==undefined)fail('unknown/repeated option');const value=rest[++i];if(!value||value.startsWith('--'))fail('option value required');options[key]=value;}for(const key of [...required,'--base-bin'])if(!options[key])fail('missing '+key);const base=await fs.realpath(absolute(options['--base-bin'])),deps=productionDependencies(controller.signal);let result:unknown;
  const deployment=deploymentPaths(await fs.realpath(fileURLToPath(import.meta.url)));
  if(deployment)deps.codemodeDeployment=deployment;
  if(command==='prepare')result=await prepareCampaign(options['--campaign']!,options['--output']!,base,deps);
  else{if(command==='run'){const captured=await json(path.join(options['--campaign']!,'campaign.json'));if(captured.base.cli_path!==base)fail('base CLI mismatch');const run=await runCampaign(options['--campaign']!,options['--approval']!,deps);write(canonical(run)+'\n');return run.exit;}if(command==='inspect')result=await inspectCapturedCampaign(options['--campaign']!,base,deps);
   else{
    const readback=command==='assess'
     ? (dir:string,c:Obj)=>verifyCodemodeAssessmentScope(dir,c,options['--evidence']!,deployment)
     : async(dir:string)=>{if(dir===deployment?.campaign)fail('protected Codemode admits only baseline validation and readback');};
    const loaded=await loadCampaign(options['--campaign']!,base,deps,undefined,readback);
    switch(command){
     case'candidate':result=await candidateCommand(loaded,options['--proposal']!,options['--output']!,deps);break;
     case'assess':result=await assessCommand(loaded,options['--evidence']!,options['--output']!,deps);break;
     case'adopt':result=await adopt(loaded,options['--report']!,options['--approval']!,options['--destination']!,deps);break;
    }
   }
  }
  if(command==='prepare'){const loaded=result as Awaited<ReturnType<typeof prepareCampaign>>;result={campaign_id:loaded.c.campaign_id,campaign_digest:loaded.digest,path:loaded.dir};}write(canonical(result)+'\n');return 0;
 }catch(e){const error=e instanceof OptimizationError?e:new OptimizationError('IO_ERROR',(e as NodeJS.ErrnoException).code==='EEXIST'?'destination/reservation already exists':'file/process operation failed');writeError(JSON.stringify({code:error.code,message:error.message})+'\n');return error.code==='CANCELLED'?130:2;}finally{process.off('SIGINT',stop);process.off('SIGTERM',stop);}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(await fs.realpath(process.argv[1])).href)process.exitCode=await main(process.argv.slice(2));
