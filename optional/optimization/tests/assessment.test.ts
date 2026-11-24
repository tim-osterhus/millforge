import {describe,it,expect,afterEach} from 'vitest';import fs from 'node:fs/promises';import path from 'node:path';import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';import {prepareCampaign} from '../src/store.js';import {runCampaign} from '../src/method.js';import {compare,assess} from '../src/evaluation.js';import {brokerReceipt} from '../src/lifecycle.js';import {makeHarness} from './fixtures/fake-process.mjs';
const roots:string[]=[],pending=new Set<Promise<void>>();
// These cases include repeated assessment/adoption or fresh-pair audit publication.
// A Vitest case timeout does not cancel its async body; join it before removing fixtures.
function joined(body:(...args:any[])=>Promise<void>):(...args:any[])=>Promise<void>{return(...args:any[])=>{const work=body(...args);pending.add(work);void work.then(()=>pending.delete(work),()=>pending.delete(work));return work;};}
afterEach(async()=>{await Promise.allSettled([...pending]);for(const p of roots.splice(0))await fs.rm(p,{recursive:true,force:true});},30000);async function setup(options:any={}){const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-assess-'));roots.push(root);return makeHarness(root,{...contracts,prepareCampaign},options) as Promise<any>;}
function observation(checks:boolean[],requests:number|null=1):any{return {complete:true,result:{status:'completed'},profile:{schema:'millforge-profile/v2',instructions:'x',tools:[],assets:[],model:{}},request:{task:'same',context:[]},capture:{limits:{},binding:{},runtime_guards:{},versions:{}},evaluation:{checks:checks.map((pass,i)=>({check_id:'c'+i,pass}))},metrics:{model_requests:requests,tool_calls:0,elapsed_ms:10}};}
describe('deterministic comparison and evidence',()=>{
 it('keeps baseline ties, rejects regressions and preserves unknown deciding metrics',()=>{expect(compare(observation([true]),observation([true])).verdict).toBe('tie');expect(compare(observation([true]),observation([false])).verdict).toBe('adverse');expect(compare(observation([true]),observation([true],null)).verdict).toBe('inconclusive');expect(compare(observation([false]),observation([true])).metric).toBe('checks');});
 it('closed limited observations never count as preferred or tied successful evidence',()=>{const a=observation([true,false]),b=observation([true,true]);b.result={status:'failed',diagnostic:{code:'LIMIT_EXHAUSTED'}};expect(compare(a,b).verdict).toBe('inconclusive');b.evaluation.checks=a.evaluation.checks;expect(compare(a,b).verdict).toBe('inconclusive');b.evaluation.checks=[{check_id:'c0',pass:false},{check_id:'c1',pass:false}];expect(compare(a,b).verdict).toBe('adverse');});
 it('broker_claim_accounting refuses count bypass/config drift and unknown metric shape',async()=>{const f=await setup();const receipt:any={schema:'millforge-optimization-broker/v1',campaign_digest:contracts.hash(contracts.canonical(f.campaign)),request_id:'r',route_digest:f.campaign.execution.broker.route_digest,config_digest:f.campaign.execution.broker.config_digest,reserved_model_requests:1,attempted:1,admitted:1,dispatched:1,denied:0,status:'closed',usage:null,cost:null};expect(brokerReceipt(receipt,f.campaign,'r',1).admitted).toBe(1);expect(()=>brokerReceipt({...receipt,admitted:2},f.campaign,'r',1)).toThrow();expect(()=>brokerReceipt({...receipt,usage:{}},f.campaign,'r',1)).toThrow();expect(()=>brokerReceipt({...receipt,config_digest:'0'.repeat(64)},f.campaign,'r',1)).toThrow();});
 it('accounting_scopes recomputes exact saved report and retains unknown usage/cost',joined(async()=>{const f=await setup(),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(await assess(loaded,path.join(loaded.dir,'evidence.json'),f.deps,1000000)).toEqual(run.report);expect(run.report.accounting.search.cost).toBeNull();expect(run.report.accounting.search.model_requests).toMatchObject({observed:7,reserved:0,status:'observed'});}),30000);
 it('ordinary execution and amortization bind plain receipts and preserve reasoning usage',joined(async()=>{
  const usage={input_tokens:7,output_tokens:3,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:2};const f=await setup({usage}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const sourceTokens=run.report.accounting.search.tokens;expect(sourceTokens.filter((x:any)=>x.unit==='reasoning_tokens').reduce((n:number,x:any)=>n+x.value,0)).toBe(14);expect(sourceTokens.filter((x:any)=>x.unit==='output_tokens').reduce((n:number,x:any)=>n+x.value,0)).toBe(21);
  const baseline=await f.ordinary(loaded.baseline.dir,'baseline',3,40),candidate=await f.ordinary(path.join(loaded.dir,'candidates/composed'),'candidate',1,20),evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));
  for(const [kind,ref]of Object.entries(evidence.runs) as any)if(ref){ref.run_dir=path.join(loaded.dir,ref.run_dir);const work=kind==='baseline'?'baseline':kind+'_probe';evidence.claims[work].run_dir=ref.run_dir;}for(const link of Object.values(evidence.claims) as any[])if(link.run_dir&&!path.isAbsolute(link.run_dir))link.run_dir=path.join(loaded.dir,link.run_dir);
  const ref=async(dir:string)=>({run_dir:dir,capture_sha256:contracts.hash(await contracts.read(path.join(dir,'capture.json'))),result_sha256:contracts.hash(await contracts.read(path.join(dir,'result.json'))),trace_sha256:contracts.hash(await contracts.read(path.join(dir,'trace.jsonl')))});
  evidence.ordinary_execution={baseline:await ref(baseline),candidate:await ref(candidate)};const file=path.join(f.root,'ordinary-evidence.json');await contracts.write(file,evidence);const report=await assess(loaded,file,f.deps,1000000);expect(report.accounting.ordinary_execution.baseline.model_requests.observed).toBe(3);expect(report.accounting.amortization).toMatchObject({search_expense:7,per_run_saving:2,break_even_runs:4});
  const noSaving=await f.ordinary(path.join(loaded.dir,'candidates/composed'),'no-saving',3,20);evidence.ordinary_execution.candidate=await ref(noSaving);const noSavingFile=path.join(f.root,'no-saving-evidence.json');await contracts.write(noSavingFile,evidence);expect((await assess(loaded,noSavingFile,f.deps,1000000)).accounting.amortization.break_even_runs).toBeNull();
  const mismatch=await contracts.json(path.join(noSaving,'request.json'));mismatch.task='A different task';await fs.writeFile(path.join(noSaving,'request.json'),contracts.canonical(mismatch)+'\n');const bad=path.join(f.root,'mismatch-evidence.json');await contracts.write(bad,evidence);await expect(assess(loaded,bad,f.deps,1000000)).rejects.toThrow('ordinary execution envelopes differ');
 }),30000);
 it('protection rejects corrupt retained checks/receipts rather than trusting success text',joined(async()=>{const f=await setup(),loaded=await f.prepare();const run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);const file=path.join(loaded.dir,'receipts/general-evaluation.json'),value=await contracts.json(file);value.protection='unproven';await fs.writeFile(file,contracts.canonical(value)+'\n');await expect(assess(loaded,path.join(loaded.dir,'evidence.json'),f.deps,1000000)).rejects.toThrow('artifact changed');}),30000);
 it('hosted_rubric_denial and direct inference charge/denial use the fake boundary predicate',async()=>{const f=await setup();await expect(f.broker.fetchRubric()).rejects.toThrow('denied');await f.broker.admit({campaign_digest:'d',request_id:'r',work_id:'baseline',route:'target',reserved_model_requests:1,deadline:'future'});expect(await f.broker.infer('r','target','d')).toBe(true);expect(await f.broker.infer('r','target','d')).toBe(false);expect(await f.broker.infer('r','proposal','d')).toBe(false);expect(await f.broker.infer('wrong','target','d')).toBe(false);const receipt=await f.broker.close('r');expect(receipt).toMatchObject({attempted:3,admitted:1,denied:2});const leaking=await setup({leakRubric:true}),loaded=await leaking.prepare();expect(await leaking.broker.fetchRubric()).toBe('PRIVATE_RUBRIC_SENTINEL');await expect(runCampaign(loaded.dir,await leaking.approve(loaded),leaking.deps)).rejects.toThrow('unproven');expect(leaking.claims.size).toBe(0);});
 it('hosted_rubric_denial/unknown broker coverage never supports a protected recommendation',async()=>{const f=await setup({brokerUnknown:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(3);expect(run.report.recommendation).toBe('inconclusive');expect(f.claims.size).toBe(1);});
 it('closeout_n3_matched_nonbaseline_pair_isolates_an_edit_in_a_bundle',joined(async()=>{
  const f=await setup({immutableAssets:true,instructionByKind:{general:'shared improved guidance',domain:'domain improved guidance',composed:'shared improved guidance'},toolsByKind:{general:['read','ls'],domain:['read','ls'],composed:['read']}});
  const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const edits=run.report.observations.edit_assessments,tools=edits.filter((e:any)=>e.field==='/tools');
  expect(tools).toHaveLength(1);
  expect(tools[0]).toMatchObject({baseline_digest:run.report.candidate_digests.general,candidate_digest:run.report.candidate_digests.composed,verdict:'helped'});
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));
  expect(tools[0].evidence_refs).toEqual([evidence.runs.general.result_sha256,evidence.evaluations.general.receipt_sha256,evidence.runs.composed.result_sha256,evidence.evaluations.composed.receipt_sha256]);
  expect(f.claims.size).toBe(7);expect(f.proposalTasks).toHaveLength(3);
 }),30000);
 it('closeout_n3_unmatched_nonbaseline_pair_stays_confounded_without_extra_calls',joined(async()=>{
  const f=await setup({immutableAssets:true,mismatchedCaptureAt:'composed_probe',instructionByKind:{general:'shared improved guidance',domain:'domain improved guidance',composed:'shared improved guidance'},toolsByKind:{general:['read','ls'],domain:['read','ls'],composed:['read']}});
  const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.report.recommendation).toBe('inconclusive');
  expect(run.report.observations.edit_assessments.filter((e:any)=>e.field==='/tools').every((e:any)=>e.verdict==='confounded')).toBe(true);
  expect(f.claims.size).toBe(7);
 }),30000);

 it('closeout_n3_contradictory_existing_isolating_pairs_stay_confounded',joined(async()=>{
  const f=await setup({immutableAssets:true,instructionByKind:{general:'improved guidance',domain:'baseline guidance',composed:'improved guidance'},toolsByKind:{general:['read','ls'],domain:['read'],composed:['read']},elapsedByWork:{baseline:10,general_probe:40,domain_probe:30,composed_probe:20}});
  const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const tools=run.report.observations.edit_assessments.filter((e:any)=>e.field==='/tools');
  expect(tools.length).toBeGreaterThan(0);expect(tools.every((e:any)=>e.verdict==='confounded')).toBe(true);expect(f.claims.size).toBe(7);
 }),30000);

 it('closeout_n3_limited_left_side_never_supports_helped_or_no_effect',joined(async()=>{
  const f=await setup({immutableAssets:true,limitAt:'general_probe',instructionByKind:{general:'shared improved guidance',domain:'domain improved guidance',composed:'shared improved guidance'},toolsByKind:{general:['read','ls'],domain:['read','ls'],composed:['read']}});
  const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.exit,JSON.stringify(run.report)).toBe(0);
  expect(run.report.observations.runs.general).toMatchObject({status:'failed',complete:true,diagnostic:{code:'LIMIT_EXHAUSTED'}});
  const tools=run.report.observations.edit_assessments.filter((e:any)=>e.field==='/tools');
  expect(tools).toHaveLength(1);expect(tools[0]).toMatchObject({baseline_digest:run.report.candidate_digests.general,candidate_digest:run.report.candidate_digests.composed,verdict:'confounded'});
  expect(f.claims.size).toBe(7);
 }),30000);

 it.each([false,true])('closeout_n2_review_selected_broker_metrics_survive_unknown_totals_extra_request_%s',joined(async extraRequest=>{
  const f=await setup({uncertainAt:'general_proposal'}),loaded=await f.prepare(),close=f.broker.close.bind(f.broker),requestId=loaded.map.slots[0].request_id;
  const source_receipt=contracts.hash('synthetic broker metric source'),route_digest=loaded.c.execution.broker.route_digest;
  const usage=[{value:9,unit:'input_tokens',source:'reported',source_receipt,route_digest},{value:3,unit:'cache_read_tokens',source:'reported',source_receipt,route_digest},{value:2,unit:'cache_write_tokens',source:'reported',source_receipt,route_digest},{value:4,unit:'reasoning_tokens',source:'reported',source_receipt,route_digest}];
  const cost=[{value:0.25,unit:'USD',source:'estimated',source_receipt,route_digest}];
  let added=false;
  f.broker.close=async(id:string,bound?:number)=>{
   if(id===requestId&&extraRequest&&!added){added=true;expect(await f.broker.infer(id,'target',loaded.digest)).toBe(true);}
   const receipt=await close(id,bound);return id===requestId?{...receipt,usage,cost}:receipt;
  };
  const run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps),ledger=run.report.accounting.search;
  expect(run).toMatchObject({exit:3,status:'uncertain'});expect(ledger.tokens).toBeNull();expect(ledger.cost).toBeNull();
  expect(ledger.model_requests).toMatchObject({observed:null,reserved:1,status:'uncertain'});
  const known=ledger.provenance.find((p:any)=>p.work_id==='baseline'&&Object.hasOwn(p,'selected_tokens'));
  expect(known).toMatchObject({known_base_tokens:null,known_base_cost:null,base_covers_broker:!extraRequest});
  expect(known.selected_tokens).toEqual(usage);expect(known.selected_cost).toEqual(cost);
  expect(ledger.provenance.find((p:any)=>p.known_subtotals).known_subtotals.model_requests).toBe(extraRequest?2:1);
  expect(f.claims.size).toBe(2);
 }),30000);

 it('pure v2 assessment requires the exact parent-held public Result anchor',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const evidenceFile=path.join(loaded.dir,'evidence.json'),anchorFile=path.join(loaded.dir,'receipts/general_probe-base-result.json'),original=await contracts.read(anchorFile),inventoryFile=path.join(loaded.dir,'artifacts.json'),originalInventory=await contracts.read(inventoryFile),inventory=JSON.parse(originalInventory.toString());
  await assess(loaded,evidenceFile,f.deps,1000000);
  const changed=JSON.parse(original.toString());changed.output='FORGED_ALTERNATIVE_OUTPUT';await fs.rm(anchorFile);await contracts.write(anchorFile,changed);
  const anchorEntry=inventory.files.find((entry:any)=>entry.path==='receipts/general_probe-base-result.json');expect(anchorEntry).toBeDefined();anchorEntry.sha256=contracts.hash(await contracts.read(anchorFile));await fs.writeFile(inventoryFile,contracts.canonical(inventory)+'\n');
  await expect(assess(loaded,evidenceFile,f.deps,1000000)).rejects.toThrow('parent-held');
  await fs.rm(anchorFile);inventory.files=inventory.files.filter((entry:any)=>entry.path!=='receipts/general_probe-base-result.json');await fs.writeFile(inventoryFile,contracts.canonical(inventory)+'\n');await expect(assess(loaded,evidenceFile,f.deps,1000000)).rejects.toThrow('anchor');
  await contracts.write(anchorFile,original);await fs.writeFile(inventoryFile,originalInventory);expect((await assess(loaded,evidenceFile,f.deps,1000000)).recommendation).toBe(run.report.recommendation);
 }),30000);
 it('host proposal accounting refuses a broker receipt reference',joined(async()=>{
  const f=await setup({hostGenerate:true}),loaded=await f.prepare();expect((await runCampaign(loaded.dir,await f.approve(loaded),f.deps)).exit).toBe(0);
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));evidence.claims.general_proposal.broker_receipt_sha256='0'.repeat(64);
  const file=path.join(loaded.dir,'invalid-host-evidence.json');await contracts.write(file,evidence);
  await expect(assess(loaded,file,f.deps,1e9)).rejects.toThrow('host slot has no broker receipt');
 }),30000);
});

