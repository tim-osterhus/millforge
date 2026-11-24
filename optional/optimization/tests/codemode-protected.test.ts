/** Synthetic process observations test admission and offline binding. Linux host protection is measured separately. */
import {afterEach,describe,expect,it,vi} from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import {homedir} from 'node:os';
import * as contracts from '../src/contracts.js';
import {assessCommand} from '../src/evaluation.js';
import {runCampaign} from '../src/method.js';
import {deploymentPaths} from '../src/protected-deployment.js';
import {inspectCapturedCampaign,loadCampaign,prepareCampaign,verifyCodemodeAssessmentScope} from '../src/store.js';
import {makeHarness} from './fixtures/fake-process.mjs';

const {canonical,hash,json,read,write}=contracts;
const roots:string[]=[];
afterEach(async()=>{
 vi.restoreAllMocks();
 for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});
});

async function replace(file:string,value:unknown){
 await fs.writeFile(file,canonical(value)+'\n');
}

async function setup(scope='deployed',validation=true){
 const root=await fs.mkdtemp(path.join(await fs.realpath(homedir()),'mfopt-protected-test-'));
 roots.push(root);
 const f=await makeHarness(root,{...contracts,prepareCampaign},{
  codemode:true,validation,nativeTools:['read','write'],noEditableAssets:true,envAuth:true,equalOutcome:true,
  qualificationPatch:{scope}
 });
 const paths=deploymentPaths(path.join(root,'optimizer/node_modules/@millrace-ai/millforge-optimization/dist/src/cli.js'))!;
 await write(paths.entry,'// fixture entry identity\n');
 const baseline=path.join(f.campaign.baseline.profile_path,'profile.json');
 await replace(baseline,{...await json(baseline),assets:[]});
 f.campaign.baseline.profile_digest=(await f.load(path.dirname(baseline))).digest;
 f.campaign.mutable_fields=[];
 if(validation)f.campaign.budget={max_model_requests:3,max_tool_calls:5,max_work_items:1,elapsed_seconds:90,
  executor_seconds:30,proposal_seconds:null,evaluation_seconds:10};
 await replace(f.author,f.campaign);
 const loaded=await f.prepare('campaign');
 const expires=Date.now()+3600000;
 const authority={...await json(path.join(loaded.dir,'approval-template.json')),expires_at:new Date(expires).toISOString()};
 await write(paths.authority,authority);
 const deps={...f.deps,scope:'deployed' as const,codemodeDeployment:paths};
 f.calls.length=0;
 return {f,paths,loaded,deps,authority,expires};
}

describe.skipIf(process.platform!=='linux'||process.getuid?.()===0)('protected Codemode baseline',()=>{
 it.each(['unregistered','wrong-path','missing','expired','changed','runtime','test-scope','search','write-boundary'])(
  'refuses %s before base, broker or run write',async reason=>{
   const {f,paths,loaded,deps,authority}=await setup(reason==='test-scope'?'test':'deployed',reason!=='search');
   let approval=paths.authority,dir=loaded.dir;
   if(reason==='unregistered'){
    dir=path.join(f.root,'other-campaign');
    await fs.rename(loaded.dir,dir);
    await replace(approval,{...authority,execution_root:dir});
   }
   if(reason==='wrong-path'){
    approval=path.join(f.root,'copied-approval.json');
    await write(approval,await read(paths.authority));
   }
   if(reason==='missing')await fs.unlink(approval);
   if(reason==='expired')await replace(approval,{...authority,expires_at:'2000-01-01T00:00:00Z'});
   if(reason==='changed')await replace(approval,{...authority,campaign_digest:hash('substitute')});
   if(reason==='runtime')await fs.appendFile(loaded.c.execution.runtime_path,'changed');
   if(reason==='write-boundary')await fs.chmod(path.dirname(approval),0o770);
   const broker=vi.fn(deps.broker);
   await expect(runCampaign(dir,approval,{...deps,broker})).rejects.toThrow();
   expect(f.calls).toHaveLength(0);
   expect(broker).not.toHaveBeenCalled();
   expect(f.claims.size).toBe(0);
   await expect(fs.lstat(path.join(dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
  }
 );

 it('detects drift after trusted reads before creating a run or claim',async()=>{
  const {f,paths,loaded,deps,authority}=await setup();
  const broker={...f.broker,async identity(){
   await replace(paths.authority,{...authority,expires_at:'2000-01-01T00:00:00Z'});
   return f.broker.identity();
  }};
  await expect(runCampaign(loaded.dir,paths.authority,{...deps,broker:()=>broker})).rejects.toThrow('protected deployment input changed');
  expect(f.calls.map((call:contracts.Obj)=>call.args[0])).toEqual([f.baseBin,f.baseBin,'version','info','network','image','image']);
  expect(f.claims.size).toBe(0);
  await expect(fs.lstat(path.join(loaded.dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
 });

 it('admits one baseline and supports fixed offline reads after authority expiry',async()=>{
  const {f,paths,loaded,deps,expires}=await setup();
  const run=await runCampaign(loaded.dir,paths.authority,deps);
  expect(run).toMatchObject({exit:0,report:{protection_scope:'deployed',recommendation:'baseline'}});
  expect(f.claims.size).toBe(1);
  expect(await read(path.join(loaded.dir,'receipts/authority.json'))).toEqual(await read(paths.authority));
  expect(await read(path.join(loaded.dir,'receipts/qualification.json'))).toEqual(await read(loaded.c.execution.qualification.path));
  f.calls.length=0;
  await expect(runCampaign(loaded.dir,paths.authority,deps)).rejects.toThrow('campaign already started');
  expect(f.calls).toHaveLength(0);
  await fs.unlink(paths.authority);
  await fs.unlink(loaded.c.execution.qualification.path);
  vi.spyOn(Date,'now').mockReturnValue(expires+1);
  const broker=vi.fn(deps.broker),offline={...deps,broker};
  const evidence=path.join(loaded.dir,'evidence.json');
  expect(await inspectCapturedCampaign(loaded.dir,f.baseBin,offline)).toMatchObject({status:'completed',protection_scope:'deployed'});
  const assessed=await loadCampaign(loaded.dir,f.baseBin,offline,undefined,
   (dir,c)=>verifyCodemodeAssessmentScope(dir,c,evidence,paths));
  expect(await assessCommand(assessed,evidence,path.join(f.root,'assessment'),offline)).toMatchObject({protection_scope:'deployed',recommendation:'baseline'});
  await fs.unlink(path.join(loaded.dir,'report.json'));
  expect(await inspectCapturedCampaign(loaded.dir,f.baseBin,offline)).toMatchObject({status:'completed',protection_scope:'deployed'});
  expect(f.calls.every((call:contracts.Obj)=>call.args[0]===f.baseBin)).toBe(true);
  expect(broker).not.toHaveBeenCalled();
 });

 it.each(['authority','qualification','evidence','closure','other-root','other-evidence'])(
  'refuses changed %s before offline base reads',async reason=>{
   const {f,paths,loaded,deps}=await setup();
   expect((await runCampaign(loaded.dir,paths.authority,deps)).exit).toBe(0);
   const evidence=path.join(loaded.dir,'evidence.json');
   const receipt=(name:string)=>path.join(loaded.dir,'receipts',name+'.json');
   const after=await json(evidence);
   let dir=loaded.dir,suppliedEvidence=evidence;
   if(reason==='authority')await fs.appendFile(receipt('authority'),' ');
   if(reason==='qualification')await replace(receipt('qualification'),{...await json(receipt('qualification')),scope:'test'});
   if(reason==='evidence')await replace(evidence,{...after,protection_scope:'test'});
   if(reason==='closure')await replace(receipt('baseline-closure'),{...await json(receipt('baseline-closure')),scope:'test'});
   if(reason==='other-evidence'){
    suppliedEvidence=path.join(f.root,'other-evidence.json');
    await write(suppliedEvidence,await read(evidence));
   }
   if(reason==='other-root'){
    dir=path.join(f.root,'other-campaign');
    await fs.rename(loaded.dir,dir);
    suppliedEvidence=path.join(dir,'evidence.json');
   }
   f.calls.length=0;
   if(reason!=='other-evidence')await expect(inspectCapturedCampaign(dir,f.baseBin,deps)).rejects.toThrow();
   await expect(loadCampaign(dir,f.baseBin,deps,undefined,
    (root,c)=>verifyCodemodeAssessmentScope(root,c,suppliedEvidence,paths))).rejects.toThrow();
   expect(f.calls).toHaveLength(0);
   expect(f.claims.size).toBe(1);
  }
 );
});
