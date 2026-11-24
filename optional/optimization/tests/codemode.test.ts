import {afterEach,describe,expect,it} from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';
import {adopt,assess,fileDigest} from '../src/evaluation.js';
import {stdoutResult} from '../src/lifecycle.js';
import {runCampaign} from '../src/method.js';
import {candidateCommand,loadCampaign,prepareCampaign,validateSubject} from '../src/store.js';
import {makeHarness} from './fixtures/fake-process.mjs';

const roots:string[]=[];
const {canonical,codemodeTarget,hash,json,read,same,target,write}=contracts;

afterEach(async()=>{
 for(const dir of roots.splice(0))await fs.rm(dir,{recursive:true,force:true});
});

async function setup(options:contracts.Obj={}){
 const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-codemode-'));
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

describe('Codemode campaign and profile roles',()=>{
 it('matches the complete target tuple and retains the native v1 reader',async()=>{
  const f=await setup(),authored=f.campaign;
  expect(contracts.campaign(authored).target).toEqual(codemodeTarget);
  const v1={...authored,schema:'millforge-optimization-campaign/v1'};
  expect(contracts.campaign({...v1,target}).target).toEqual(target);
  expect(()=>contracts.campaign(v1)).toThrow('unsupported optimizer target');
  for(const field of ['profile_schema','result_schema','capture_schema','trace_schema'] as const){
   expect(()=>contracts.campaign({...authored,target:{...codemodeTarget,[field]:target[field]}})).toThrow('unsupported optimizer target');
  }
  for(const extra of [{request_schema:'millforge-request/v2'},{binding_version:1},{pi_version:'1.4.1'},{other:true}]){
   expect(()=>contracts.campaign({...authored,target:{...codemodeTarget,...extra}})).toThrow('unsupported optimizer target');
  }
  expect(()=>contracts.campaign({...authored,library:{path:'library',digest:hash('library')}})).toThrow('null library');
  expect(stdoutResult('{"schema":"millforge-result/v3"}').schema).toBe(target.result_schema);
  expect(()=>stdoutResult('{"schema":"millforge-result/v4"}')).toThrow('stdout family mismatch');
 });

 it('prepares and reloads the exact executor and tool-free teacher roles',async()=>{
  const f=await setup(),loaded=await f.prepare();
  const reloaded=await loadCampaign(loaded.dir,f.baseBin,f.deps);
  expect(reloaded.digest).toBe(loaded.digest);
  expect(reloaded.baseline.profile.schema).toBe(codemodeTarget.profile_schema);
  expect(reloaded.proposer?.profile).toMatchObject({
   schema:target.profile_schema,mode:'generate',tools:[],capabilities:{extensions:[],packages:[]}
  });
  expect(reloaded.map.slots.filter((s:contracts.Obj)=>s.kind==='proposal').map((s:contracts.Obj)=>[s.limits.model_requests,s.limits.tool_calls])).toEqual([[1,0],[1,0],[1,0]]);
  await replace(f.author,{...f.campaign,baseline:f.campaign.proposal_profile});
  await expect(f.prepare('crossed-baseline')).rejects.toThrow('unsupported optimizer profile');
  await replace(path.join(loaded.dir,'campaign.json'),{...loaded.c,proposal_profile:loaded.c.baseline});
  await expect(loadCampaign(loaded.dir,f.baseBin,f.deps)).rejects.toThrow('unsupported optimizer profile');
 });

 it('materializes only an approved ordered native subset and preserves protected profile and assets',async()=>{
  const f=await setup({protectedAsset:true,nativeTools:['read','ls','write']});
  f.campaign.allowed_tools=['read','ls'];
  await replace(f.author,f.campaign);
  const loaded=await f.prepare(),original=loaded.baseline.profile;
  const draft={
   schema:'millforge-optimization-proposal/v1',campaign_id:loaded.c.campaign_id,kind:'general',
   baseline_digest:loaded.baseline.digest,parent_candidates:[],instruction_text:original.instructions,
   tools:['read'],assets:[],rationale:'Retain the approved read tool.'
  };
  const file=path.join(f.root,'proposal.json');
  await write(file,draft);
  const candidate=await candidateCommand(loaded,file,path.join(f.root,'subset'),f.deps);
  const next=await f.load(candidate.path);
  expect(next.profile).toMatchObject({
   schema:codemodeTarget.profile_schema,tools:['read'],capabilities:original.capabilities,
   model:original.model,limits:original.limits,runtime:original.runtime
  });
  const protectedAsset=path.join('context','protected.txt');
  expect(await read(path.join(candidate.path,protectedAsset))).toEqual(loaded.baseline.assets.get(protectedAsset));
  for(const [i,tools]of [['ls','read'],['read','bash'],['write'],['codemode'],[]].entries()){
   await replace(file,{...draft,tools});
   await expect(candidateCommand(loaded,file,path.join(f.root,'bad-'+i),f.deps)).rejects.toThrow(/tool subset\/order|one native tool/);
  }
  await replace(file,{...draft,capabilities:{extensions:[],packages:[]}});
  await expect(candidateCommand(loaded,file,path.join(f.root,'bad-capability'),f.deps)).rejects.toThrow('unknown fields');
  const assets=new Map([[protectedAsset,await read(path.join(candidate.path,protectedAsset))]]);
  expect(()=>validateSubject(loaded,{assets,profile:{...next.profile,capabilities:{extensions:[],packages:[]}}})).toThrow('protected profile changed');
  expect(()=>validateSubject(loaded,{assets,profile:{...next.profile,model:{...next.profile.model,id:'other'}}})).toThrow('protected profile changed');
 });

 it.each([['test','deployed'],['deployed','test'],['deployed','deployed']] as const)('refuses receipt scope %s and dispatch scope %s before any runtime or broker call',async(receiptScope,dispatchScope)=>{
  const f=await setup({qualificationPatch:{scope:receiptScope}}),loaded=await f.prepare();
  const deps={...f.deps,scope:dispatchScope,broker:()=>{throw Error('broker called');}};
  await expect(runCampaign(loaded.dir,await f.approve(loaded),deps)).rejects.toThrow('requires test scope');
  expect(f.calls.some((spec:contracts.Obj)=>spec.executable===loaded.c.execution.runtime_path)).toBe(false);
  expect(f.claims.size).toBe(0);
  await expect(fs.stat(path.join(loaded.dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
 });
});

describe('Codemode stopped evidence',()=>{
 it.each([false,true])('accounts for the wrapper and nested tool once with host teacher %s',async hostGenerate=>{
  const f=await setup({hostGenerate}),{loaded,run}=await execute(f);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  expect(run.report.recommendation).toBe('composed');
  expect(f.claims.size).toBe(hostGenerate?4:7);
  expect(f.hostRuns).toHaveLength(hostGenerate?3:0);
  expect(run.report.accounting.search.model_requests.observed).toBe(7);
  expect(run.report.accounting.search.tool_calls).toMatchObject({observed:8,reserved:0,status:'observed'});
  const baseline=await json(path.join(loaded.dir,'runs/baseline/capture.json'));
  const composed=await json(path.join(loaded.dir,'runs/composed_probe/capture.json'));
  expect(baseline.callable_tool_names).toEqual(['read','ls']);
  expect(composed.callable_tool_names).toEqual(['read']);
  expect(baseline.declared_tools).not.toEqual(composed.declared_tools);
  expect(baseline.capability.declared_description_sha256).toBe(composed.capability.declared_description_sha256);
  expect(baseline.capability.constrained_sampling_sha256).toBe(composed.capability.constrained_sampling_sha256);
  for(const slot of loaded.map.slots){
   const result=await json(path.join(loaded.dir,'runs',slot.work_id,'result.json'));
   const held=await json(path.join(loaded.dir,'receipts',slot.work_id+'-base-result.json'));
   expect(same(held,result)).toBe(true);
   if(slot.kind==='proposal'){
    expect(result).toMatchObject({schema:target.result_schema,counts:{tool_calls:0,model_requests:1}});
    expect(result.counts).not.toHaveProperty('nested_tool_calls');
   }
   else expect(result).toMatchObject({schema:codemodeTarget.result_schema,counts:{tool_calls:2,nested_tool_calls:1}});
  }
  for(const teacherTask of f.proposalTasks){
   const projection=teacherTask.slice(teacherTask.indexOf('\n')+1);
   expect(projection).not.toMatch(/parent_call_id|nested_join|nested_tool_calls|constrained_sampling_sha256/);
  }
  const evidence=path.join(loaded.dir,'evidence.json');
  expect(await assess(loaded,evidence,f.deps,1_000_000)).toEqual(run.report);
  if(!hostGenerate){
   const inventoryFile=path.join(loaded.dir,'artifacts.json'),inventory=await json(inventoryFile);
   const timingPath='receipts/campaign-timing.json';
   await fs.unlink(path.join(loaded.dir,timingPath));
   await replace(inventoryFile,{...inventory,files:inventory.files.filter((file:contracts.Obj)=>file.path!==timingPath)});
   const withoutTiming=await assess(loaded,evidence,f.deps,1_000_000);
   expect(withoutTiming.recommendation).toBe('composed');
   expect(withoutTiming.accounting.search.elapsed_ms).toMatchObject({observed:null,status:'uncertain'});
  }
 },30_000);

 it('runs validation with only the Codemode baseline',async()=>{
  const f=await setup({validation:true}),{loaded,run}=await execute(f);
  expect(loaded.proposer).toBeNull();
  expect(run).toMatchObject({exit:0,report:{recommendation:'baseline',accounting:{search:{tool_calls:{observed:2}}}}});
  expect([...f.claims.values()].map(claim=>claim.work_id)).toEqual(['baseline']);
  expect(f.hostRuns).toHaveLength(0);
 });

 it('accounts for closed work after early cancellation with unclaimed slots',async()=>{
  const f=await setup(),loaded=await f.prepare(),controller=new AbortController(),process=f.deps.runProcess;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{
   ...f.deps,signal:controller.signal,
   runProcess:async(spec,signal)=>{
    const result=await process(spec,signal);
    const general='mfopt-'+loaded.c.campaign_id+'-general_proposal';
    if(spec.args[0]==='run'&&spec.args[spec.args.indexOf('--name')+1]===general){
     controller.abort();
     return {...result,cancelled:true};
    }
    return result;
   }
  });
  expect(run).toMatchObject({exit:130,status:'cancelled',report:{recommendation:'inconclusive'}});
  const inventory=await json(path.join(loaded.dir,'dispatch-inventory.json'));
  expect(Object.keys(inventory.claim_digests).sort()).toEqual(['baseline','general_proposal']);
  const timing=await json(path.join(loaded.dir,'receipts/campaign-timing.json'));
  expect(timing.closed).toBe(true);
  const report=await assess(loaded,path.join(loaded.dir,'evidence.json'),f.deps,1_000_000);
  expect(report.recommendation).toBe('inconclusive');
  const search=expect.objectContaining({
   model_requests:{observed:2,reserved:0,status:'observed'},
   tool_calls:{observed:2,reserved:0,status:'observed'},
   elapsed_ms:{observed:timing.elapsed_ms,reserved:0,status:'observed'}
  });
  expect(run.report.accounting.search).toEqual(search);
  expect(report.accounting.search).toEqual(search);
 },30_000);

 it.each([
  ['stdoutFamilyAt','baseline'],['recordFamilyAt','baseline'],['captureFamilyAt','baseline'],
  ['stdoutFamilyAt','general_proposal'],['captureFamilyAt','general_proposal'],['forgeSavedAt','composed_probe']
 ])('refuses %s in %s before recommending a candidate',async(field,work)=>{
  const f=await setup({[field]:work}),{loaded,run}=await execute(f);
  expect(run).toMatchObject({exit:3,status:'uncertain',report:{recommendation:'inconclusive'}});
  const evidence=await json(path.join(loaded.dir,'evidence.json'));
  expect(evidence.claims[work!]).toMatchObject({run_dir:null,result_sha256:null});
  expect(run.report.accounting.search.tool_calls.observed).toBeNull();
 },30_000);

 it.each(['capabilityDriftAt','descriptionDriftAt'])('refuses %s with otherwise closed candidate evidence',async field=>{
  const f=await setup({[field]:'composed_probe'}),{run}=await execute(f);
  expect(run).toMatchObject({exit:3,report:{
   recommendation:'inconclusive',observations:{runs:{composed:{complete:true}}}
  }});
 },30_000);

 it('discards a retained inspection when a captured file changes before terminal accounting',async()=>{
  const f=await setup(),loaded=await f.prepare(),executeProcess=f.deps.runProcess;
  let changed=false;
  const deps={...f.deps,runProcess:async(spec:Parameters<typeof executeProcess>[0],signal?:AbortSignal)=>{
   const reply=await executeProcess(spec,signal);
   if(!changed&&spec.args[1]==='inspect'&&await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false)){
    changed=true;
    const file=path.join(loaded.dir,'runs/baseline/capture.json'),capture=await json(file);
    await replace(file,{...capture,limits:{...capture.limits,tool_calls:1}});
    f.advance(spec.deadline-f.deps.now()+1);
    return {...reply,exit:null,timedOut:true};
   }
   return reply;
  }};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),deps);
  expect(changed).toBe(true);
  expect(run).toMatchObject({exit:124,report:{recommendation:'inconclusive'}});
  expect(run.report.accounting.search.tool_calls.observed).toBeNull();
  expect(run.report.accounting.search.provenance).toContainEqual(expect.objectContaining({work_id:'baseline',status:'uncertain'}));
 },30_000);

 it('validates proposal limits on fresh reads and fences evidence scope, reuse and library adoption',async()=>{
  const f=await setup(),{loaded,run}=await execute(f);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const original=await json(path.join(loaded.dir,'evidence.json'));
  const other=path.join(loaded.dir,'alternate-evidence.json');
  await write(other,{...original,protection_scope:'deployed'});
  await expect(assess(loaded,other,f.deps,1_000_000)).rejects.toThrow('requires test scope');
  await replace(other,{...original,reuse_evidence:{candidate_dir:'candidates/composed'}});
  await expect(assess(loaded,other,f.deps,1_000_000)).rejects.toThrow('empty reuse evidence');

  const missingTeacher=structuredClone(original);
  missingTeacher.claims.general_proposal.result_sha256=null;
  await replace(other,missingTeacher);
  const uncertain=await assess(loaded,other,f.deps,1_000_000);
  expect(uncertain.recommendation).toBe('inconclusive');
  expect(uncertain.accounting.search.model_requests).toMatchObject({observed:null,reserved:1,status:'uncertain'});

  const copied=path.join(f.root,'copied-proposal');
  await contracts.copyTree(path.join(loaded.dir,'runs/general_proposal'),copied);
  const capturePath=path.join(copied,'capture.json'),capture=await json(capturePath);
  await replace(capturePath,{...capture,limits:{...capture.limits,model_requests:2}});
  const changed=structuredClone(original);
  changed.claims.general_proposal.run_dir=copied;
  await replace(other,changed);
  await expect(assess(loaded,other,f.deps,1_000_000)).rejects.toThrow('slot public records mismatch');

  const dest=path.join(f.root,'library-entry'),approval=path.join(f.root,'library-adoption.json');
  await write(approval,{
   schema:'millforge-optimization-adoption/v1',owner_id:loaded.c.owner_id,campaign_id:loaded.c.campaign_id,
   campaign_digest:loaded.digest,report_digest:fileDigest(run.report),candidate_digest:run.report.candidate_digests.composed,
   destination:dest,expected_preimage:'absent',decision:'adopt',retention:'library_entry',library_entry:null
  });
  await expect(adopt(loaded,path.join(loaded.dir,'report.json'),approval,dest,f.deps)).rejects.toThrow('task-specific retention');
  await expect(fs.stat(dest)).rejects.toMatchObject({code:'ENOENT'});
 },30_000);

 it('requires a matched pair of ordinary Codemode records for savings',async()=>{
  const f=await setup(),{loaded,run}=await execute(f);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const evidence=await json(path.join(loaded.dir,'evidence.json')),file=path.join(loaded.dir,'ordinary-evidence.json');
  const baseline=await f.ordinary(loaded.baseline.dir,'baseline',2);
  const candidate=await f.ordinary(path.join(loaded.dir,'candidates/composed'),'candidate',1);
  const ref=async(dir:string)=>({
   run_dir:dir,capture_sha256:hash(await read(path.join(dir,'capture.json'))),
   result_sha256:hash(await read(path.join(dir,'result.json'))),trace_sha256:hash(await read(path.join(dir,'trace.jsonl')))
  });
  evidence.ordinary_execution={baseline:await ref(baseline),candidate:await ref(candidate)};
  await write(file,evidence);
  const matched=await assess(loaded,file,f.deps,1_000_000);
  expect(matched.accounting.amortization).toMatchObject({search_expense:7,per_run_saving:1,break_even_runs:7});
  expect(matched.accounting.ordinary_execution.candidate.tool_calls.observed).toBe(2);
  const native=await setup({codemode:false}),plain=await native.ordinary(native.campaign.baseline.profile_path,'native');
  evidence.ordinary_execution.baseline=await ref(plain);
  await replace(file,evidence);
  await expect(assess(loaded,file,f.deps,1_000_000)).rejects.toThrow('unsupported optimizer profile');
  evidence.ordinary_execution.baseline=null;
  await replace(file,evidence);
  await expect(assess(loaded,file,f.deps,1_000_000)).rejects.toThrow('object required');
 },30_000);
});
