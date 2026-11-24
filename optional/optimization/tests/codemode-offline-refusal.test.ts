/** Offline deployed-Codemode refusal and interrupted retained-state readback. No deployed admission. */
import {afterEach,describe,expect,it,vi} from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';
import {main} from '../src/cli.js';
import * as lifecycle from '../src/lifecycle.js';
import {runCampaign} from '../src/method.js';
import {prepareCampaign} from '../src/store.js';
import {protocol} from '../src/versions.js';
import {makeHarness} from './fixtures/fake-process.mjs';

const roots:string[]=[];
const {canonical,hash,json,read,write}=contracts;

afterEach(async()=>{
 vi.restoreAllMocks();
 for(const dir of roots.splice(0))await fs.rm(dir,{recursive:true,force:true});
});

async function setup(options:contracts.Obj={}){
 const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-codemode-refusal-'));
 roots.push(root);
 const f=await makeHarness(root,{...contracts,prepareCampaign},{codemode:true,...options});
 return {...f,deps:{...f.deps,scope:'test' as const}};
}

async function execute(f:Awaited<ReturnType<typeof setup>>){
 const loaded=await f.prepare();
 const run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
 return {loaded,run};
}

async function replace(file:string,value:unknown){
 await fs.writeFile(file,canonical(value)+'\n');
}

/** Public CLI with fixture dependencies at the production scope. */
function fixtureCli(f:Awaited<ReturnType<typeof setup>>,dir:string){
 vi.spyOn(lifecycle,'productionDependencies').mockReturnValue({...f.deps,scope:'deployed'});
 return async(command:string,extra:string[]=[])=>{
  let stdout='',stderr='';
  const exit=await main([command,'--campaign',dir,'--base-bin',f.baseBin,...extra],s=>stdout+=s,s=>stderr+=s);
  return {exit,stdout:stdout?JSON.parse(stdout):null,stderr:stderr?JSON.parse(stderr):null};
 };
}

const scopeRefusal={exit:2,stdout:null,stderr:{code:'INVALID_INPUT',message:'Codemode optimization requires test scope'}};

describe('Codemode run refusal before effects',()=>{
 it.each([['test','deployed'],['deployed','test'],['deployed','deployed']] as const)(
  'refuses receipt scope %s and dispatch scope %s before any process or broker call',async(receiptScope,dispatchScope)=>{
   const f=await setup({qualificationPatch:{scope:receiptScope}}),loaded=await f.prepare();
   f.calls.length=0;
   const deps={...f.deps,scope:dispatchScope,broker:()=>{throw Error('broker called');}};
   await expect(runCampaign(loaded.dir,await f.approve(loaded),deps)).rejects.toThrow('requires test scope');
   expect(f.calls).toHaveLength(0);
   expect(f.claims.size).toBe(0);
   await expect(fs.stat(path.join(loaded.dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
  }
 );

 it.each(['valid','missing','expired','wrong-root','changed-digest','test-qualification'] as const)(
  'refuses deployed validation through the public CLI before any spawn with %s input',async scenario=>{
   const f=await setup({validation:true,nativeTools:['read','write'],noEditableAssets:true,qualificationPatch:{scope:scenario==='test-qualification'?'test':'deployed'}});
   f.campaign.mutable_fields=[];
   f.campaign.budget={max_model_requests:3,max_tool_calls:5,max_work_items:1,elapsed_seconds:90,executor_seconds:30,proposal_seconds:null,evaluation_seconds:10};
   await replace(f.author,f.campaign);
   const captured=await f.prepare(),approval=await f.approve(captured);
   expect(captured.c.base.package_version).toBe('0.2.1');
   const authority=await json(approval);
   if(scenario==='missing')await fs.unlink(approval);
   if(scenario==='expired')await replace(approval,{...authority,expires_at:'2000-01-01T00:00:00Z'});
   if(scenario==='wrong-root')await replace(approval,{...authority,execution_root:f.root});
   if(scenario==='changed-digest')await replace(approval,{...authority,campaign_digest:hash('changed')});
   const before=(await contracts.tree(captured.dir)).digest,cli=fixtureCli(f,captured.dir);
   f.calls.length=0;
   expect(await cli('run',['--approval',approval])).toMatchObject(scopeRefusal);
   expect(f.calls).toHaveLength(0);
   expect(f.claims.size).toBe(0);
   expect((await contracts.tree(captured.dir)).digest).toBe(before);
  }
 );
});

describe('Codemode retained-state readback',()=>{
 it('permits completed test-scope inspect and assessment with only retained receipts',async()=>{
  const f=await setup({validation:true}),{loaded,run}=await execute(f);
  expect(run.exit).toBe(0);
  await fs.unlink(loaded.c.execution.qualification.path);
  await fs.unlink(path.join(f.root,'approval.json'));
  vi.spyOn(Date,'now').mockReturnValue(Date.now()+7200000);
  const cli=fixtureCli(f,loaded.dir),evidence=path.join(loaded.dir,'evidence.json');
  f.calls.length=0;
  expect(await cli('inspect')).toMatchObject({exit:0,stdout:{status:'completed',protection_scope:'test'}});
  expect(await cli('assess',['--evidence',evidence,'--output',path.join(f.root,'offline')])).toMatchObject({
   exit:0,stdout:{recommendation:'baseline',protection_scope:'test'}
  });
  expect(f.calls.length).toBeGreaterThan(0);
  expect(f.calls.every((spec:contracts.Obj)=>spec.args[0]===f.baseBin)).toBe(true);
  expect(f.claims.size).toBe(1);
 });

 it('reports an interrupted start as uncertain without a retained qualification or child process',async()=>{
  const f=await setup({validation:true}),loaded=await f.prepare(),approval=await f.approve(loaded);
  const file=path.join(loaded.dir,'run.json');
  const run={
   schema:'millforge-optimization-run/v1',campaign_id:loaded.c.campaign_id,campaign_digest:loaded.digest,
   execution_root:loaded.dir,authority_sha256:hash(await read(approval)),started_at:new Date().toISOString(),
   elapsed_seconds:loaded.c.budget.elapsed_seconds,
   optimizer:{path:f.baseBin,sha256:hash('fixture'),...protocol}
  };
  await write(file,run);
  const cli=fixtureCli(f,loaded.dir);
  f.calls.length=0;
  expect(await cli('inspect')).toEqual({
   exit:0,stderr:null,stdout:{campaign_id:loaded.c.campaign_id,status:'uncertain',dispatch_status:'uncertain',
    recommendation:null,protection_scope:null,exit_code:3,exit_code_source:'saved_disposition',inventory:null}
  });
  const output=path.join(f.root,'refused-output');
  expect(await cli('assess',['--evidence',path.join(loaded.dir,'evidence.json'),'--output',output])).toMatchObject({
   exit:2,stdout:null,stderr:{code:'INVALID_INPUT',message:'retained qualification missing'}
  });
  await expect(fs.stat(output)).rejects.toMatchObject({code:'ENOENT'});
  await replace(file,{...run,execution_root:f.root});
  expect(await cli('inspect')).toMatchObject({exit:2,stderr:{message:'run identity mismatch'}});
  await replace(file,{...run,elapsed_seconds:loaded.c.budget.elapsed_seconds+1});
  expect(await cli('inspect')).toMatchObject({exit:2,stderr:{message:'integer outside limits'}});
  await fs.writeFile(file,'{"schema":');
  expect((await cli('inspect')).exit).toBe(2);
  await replace(file,run);
  await replace(path.join(loaded.dir,'work-map.json'),{...loaded.map,campaign_digest:hash('other campaign')});
  expect(await cli('inspect')).toMatchObject({exit:2,stderr:{message:'work map identity mismatch'}});
  expect(f.calls).toHaveLength(0);
  expect(f.claims.size).toBe(0);
 });

 it.each(['qualification','evidence','closure','supplied-evidence'] as const)(
  'refuses changed %s scope before loading the base without a saved report',async receipt=>{
   const f=await setup({validation:true}),{loaded,run}=await execute(f);
   expect(run.exit).toBe(0);
   const evidence=path.join(loaded.dir,'evidence.json');
   await fs.unlink(path.join(loaded.dir,'report.json'));
   let assessEvidence=evidence;
   if(receipt==='qualification'){
    const file=path.join(loaded.dir,'receipts/qualification.json');
    await replace(file,{...await json(file),scope:'deployed'});
    const changed={...loaded.c,execution:{...loaded.c.execution,qualification:{
     ...loaded.c.execution.qualification,sha256:hash(await read(file))
    }}};
    await replace(path.join(loaded.dir,'campaign.json'),changed);
    await replace(path.join(loaded.dir,'work-map.json'),{...loaded.map,campaign_digest:hash(canonical(changed))});
   }
   if(receipt==='evidence')await replace(evidence,{...await json(evidence),protection_scope:'deployed'});
   if(receipt==='closure'){
    const file=path.join(loaded.dir,'receipts/baseline-closure.json');
    await replace(file,{...await json(file),scope:'deployed'});
   }
   if(receipt==='supplied-evidence'){
    assessEvidence=path.join(f.root,'altered-evidence.json');
    await write(assessEvidence,{...await json(evidence),protection_scope:'deployed'});
   }
   const cli=fixtureCli(f,loaded.dir);
   f.calls.length=0;
   if(receipt!=='supplied-evidence')expect(await cli('inspect')).toMatchObject(scopeRefusal);
   const output=path.join(f.root,'refused-output');
   expect(await cli('assess',['--evidence',assessEvidence,'--output',output])).toMatchObject(scopeRefusal);
   await expect(fs.stat(output)).rejects.toMatchObject({code:'ENOENT'});
   expect(f.calls).toHaveLength(0);
   expect(f.claims.size).toBe(1);
  }
 );

 it('refuses a retained closure or inventory whose qualification is missing',async()=>{
  const f=await setup({validation:true}),{loaded,run}=await execute(f);
  expect(run.exit).toBe(0);
  await fs.unlink(path.join(loaded.dir,'report.json'));
  await fs.unlink(path.join(loaded.dir,'evidence.json'));
  await fs.unlink(path.join(loaded.dir,'receipts/qualification.json'));
  const cli=fixtureCli(f,loaded.dir);
  f.calls.length=0;
  expect(await cli('inspect')).toMatchObject({exit:2,stdout:null,stderr:{code:'INVALID_INPUT',message:'retained qualification missing'}});
  expect(f.calls).toHaveLength(0);
 });
});

describe('Ordinary inspection is unaffected',()=>{
 it('inspects a prepared ordinary campaign through the existing base-loading path',async()=>{
  const f=await setup({codemode:false}),loaded=await f.prepare();
  expect(contracts.codemode(loaded.c)).toBe(false);
  const cli=fixtureCli(f,loaded.dir);
  f.calls.length=0;
  expect(await cli('inspect')).toMatchObject({exit:0,stdout:{status:'prepared',protection_scope:null,exit_code:null}});
  expect(f.calls.length).toBeGreaterThan(0);
 });
});
