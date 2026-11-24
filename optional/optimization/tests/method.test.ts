import {describe,it,expect,afterEach} from 'vitest';
import fs from 'node:fs/promises';import path from 'node:path';import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';import {prepareCampaign,inspectCampaign} from '../src/store.js';import {runCampaign} from '../src/method.js';import {makeHarness} from './fixtures/fake-process.mjs';
const roots:string[]=[],pending=new Set<Promise<void>>();
function joined(body:(...args:any[])=>Promise<void>):(...args:any[])=>Promise<void>{return(...args:any[])=>{const work=body(...args);pending.add(work);void work.then(()=>pending.delete(work),()=>pending.delete(work));return work;};}
afterEach(async()=>{await Promise.allSettled([...pending]);for(const p of roots.splice(0))await fs.rm(p,{recursive:true,force:true});},30000);
async function setup(options:any={}){const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-method-'));roots.push(root);return makeHarness(root,{...contracts,prepareCampaign},options) as Promise<any>;}
describe('complete fixed fake method',()=>{
 it('matched_composition dispatches all seven slots and preserves unfavorable observations',async()=>{const f=await setup(),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(run.report.recommendation).toBe('composed');expect([...f.claims.values()].map((x:any)=>x.work_id)).toEqual([...contracts.workIds]);expect(run.report.accounting.search.model_requests.observed).toBe(7);expect(run.report.accounting.search.tokens).toBeNull();expect(run.report.protection_scope).toBe('test');expect(run.report.observations.edit_assessments.every((x:any)=>x.verdict==='confounded')).toBe(true);});
 it('refuses a missing inspected result without inventing completion or dispatching later slots',async()=>{const f=await setup(),loaded=await f.prepare(),original=f.deps.runProcess;const deps={...f.deps,runProcess:async(spec:any,signal?:AbortSignal)=>{const reply=await original(spec,signal);if(spec.args[1]==='inspect'){const value=JSON.parse(reply.stdout);value.result=null;return {...reply,stdout:contracts.canonical(value)};}return reply;}};const run=await runCampaign(loaded.dir,await f.approve(loaded),deps);expect(run.exit).toBe(3);expect(run.report.recommendation).toBe('inconclusive');expect(f.claims.size).toBe(1);expect(await contracts.json(path.join(loaded.dir,'claims/baseline.json'))).toHaveProperty('reserved_model_requests',8);expect((await contracts.json(path.join(loaded.dir,'evidence.json'))).runs.baseline).toBeNull();});
 it('keeps baseline for complete regression and counts proposals once',async()=>{const f=await setup({regression:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(run.report.recommendation).toBe('baseline');expect(f.proposalTasks).toHaveLength(3);});
 it('retains earlier access tokens in memory for composition redaction',async()=>{const f=await setup({envAuth:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(f.proposalTasks).toHaveLength(3);for(const task of f.proposalTasks)expect(task).not.toContain('scoped-baseline');expect(f.proposalTasks[2]).toContain('[REDACTED_BROKER_ACCESS]');});
 it('a single limited probe with extra passed checks is never assessed as helped',async()=>{const f=await setup({limitAt:'general_probe',singleEdit:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);const observation=run.report.observations.runs.general;expect(observation).toMatchObject({status:'failed',complete:true,diagnostic:{code:'LIMIT_EXHAUSTED'}});expect(observation.checks.filter((c:any)=>c.pass)).toHaveLength(2);expect(run.report.observations.edit_assessments.find((e:any)=>e.candidate_digest===run.report.candidate_digests.general).verdict).toBe('confounded');});
 it('closed_limit_regression keeps failed diagnostic distinct from completed output',async()=>{const f=await setup({limitAt:'general_probe'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(f.proposalTasks).toHaveLength(3);expect(run.report.observations.runs.general.status).toBe('failed');expect(run.report.recommendation).not.toBe('general');});
 it.each([{regression:false,equalOutcome:false,verdict:'helped'},{regression:true,equalOutcome:false,verdict:'hurt'},{regression:false,equalOutcome:true,verdict:'no_observed_effect'}])('edit_verdicts observes $verdict for one isolated instruction edit',async options=>{const f=await setup({...options,singleEdit:true,sameMetrics:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(run.report.observations.edit_assessments).toHaveLength(3);expect(run.report.observations.edit_assessments.map((x:any)=>x.verdict)).toEqual([options.verdict,options.verdict,options.verdict]);});
 it('invalid proposal stops its descendants while the independent domain branch completes',async()=>{const f=await setup({invalidProposalAt:'general_proposal'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(3);expect([...f.claims.values()].map((c:any)=>c.work_id)).toEqual(['baseline','general_proposal','domain_proposal','domain_probe']);expect(run.report.observations.runs.domain.complete).toBe(true);expect(run.report.observations.runs.general).toBeNull();expect(run.report.recommendation).toBe('inconclusive');});
 it('candidate metric ties prefer general, then domain, then composed',async()=>{const f=await setup({sameMetrics:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(0);expect(run.report.recommendation).toBe('general');});
 it('ranking preserves common-baseline checks without requiring the intermediate winners new checks',async()=>{const f=await setup({sameMetrics:true,checkIds:['fixed','a','b','extra'],checksByWork:{baseline:[true,false,false,false],general_probe:[true,true,false,false],domain_probe:[true,false,true,true],composed_probe:[true,false,false,false]}}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(run.report.recommendation).toBe('domain');});
 it.each(['missingRecordAt','baseCountGreaterAt'])('missing records or broker coverage mismatch stay uncertain (%s)',async option=>{const f=await setup({[option]:'baseline'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(3);expect(run.status).toBe('uncertain');expect(f.claims.size).toBe(1);});

 it('inspect distinguishes completed dispatch from an inconclusive comparison',async()=>{const f=await setup({mismatchedCaptureAt:'domain_probe'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(3);const view=await inspectCampaign(loaded);expect(view).toMatchObject({dispatch_status:'completed',status:'failed',recommendation:'inconclusive',protection_scope:'test',exit_code:3,exit_code_source:'saved_disposition'});});
 it('closeout_n2_cancel_before_admission_creates_no_run_or_claim',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),approval=await f.approve(loaded),controller=new AbortController();controller.abort();
  await expect(runCampaign(loaded.dir,approval,{...f.deps,signal:controller.signal})).rejects.toMatchObject({code:'CANCELLED'});
  expect(f.claims.size).toBe(0);await expect(fs.stat(path.join(loaded.dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
 }));
 it.each(['baseline','general_proposal'])('closeout_n2_cancel_preserves_known_ledger_before_or_after_evaluated_probe_%s',joined(async work=>{
  const usage={input_tokens:7,output_tokens:3,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:2};
  const f=await setup({usage}),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;
  let cancelAt:number|undefined;const bounds:number[]=[],brokerBounds:number[]=[];
  const close=f.broker.close.bind(f.broker);f.broker.close=async(id:string,bound?:number)=>{if(controller.signal.aborted)brokerBounds.push(bound!);return close(id,bound);};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   if(controller.signal.aborted)bounds.push(spec.deadline);
   const reply=await original(spec,signal,cleanup);
   if(spec.args[0]==='run'&&spec.args[spec.args.indexOf('--name')+1]==='mfopt-'+loaded.c.campaign_id+'-'+work){cancelAt=f.deps.now();controller.abort();f.advance(1200);return {...reply,cancelled:true};}
   return reply;
  }});
  expect(run).toMatchObject({exit:130,status:'cancelled'});
  expect(run.report.recommendation).toBe('inconclusive');
  expect(run.report.accounting.search.model_requests).toMatchObject({observed:work==='baseline'?1:2,reserved:0,status:'observed'});
  expect(run.report.accounting.search.tokens.filter((x:any)=>x.unit==='reasoning_tokens').reduce((n:number,x:any)=>n+x.value,0)).toBe(work==='baseline'?2:4);
  expect(run.report.observations.runs.baseline===null).toBe(work==='baseline');
  expect([...f.claims.values()].map((x:any)=>x.work_id)).toEqual(work==='baseline'?['baseline']:['baseline','general_proposal']);
  expect(bounds.length).toBeGreaterThan(0);expect(bounds.every(bound=>bound===cancelAt!+5000)).toBe(true);
  expect(brokerBounds.every(bound=>bound===cancelAt!+5000)).toBe(true);
  expect(await inspectCampaign(loaded)).toMatchObject({status:'cancelled',exit_code:130});
 }),30000);
 it.each([{expired:false,unclosed:false},{expired:true,unclosed:false},{expired:false,unclosed:true}])('closeout_n2_cancel_during_final_assess_keeps_disposition_and_original_remainder_%s',joined(async({expired,unclosed})=>{
  const f=await setup({usage:{input_tokens:7,output_tokens:3,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:2}}),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;
  let triggered=false,cancelAt=0,actualCallsAt=0;const terminalBounds:number[]=[];
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   if(triggered)terminalBounds.push(spec.deadline);
   const reply=await original(spec,signal,cleanup);
   if(!triggered&&spec.args[1]==='inspect'&&await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false)){
    triggered=true;cancelAt=f.deps.now();actualCallsAt=f.calls.length;controller.abort();f.advance(expired?5000:1200);
    return {...reply,exit:null,cancelled:true,closed:!unclosed};
   }
   return reply;
  }});
  expect(triggered).toBe(true);expect(run).toMatchObject(unclosed?{exit:3,status:'uncertain'}:{exit:130,status:'cancelled'});
  expect(run.report.recommendation).toBe('inconclusive');expect(run.report.accounting.search.model_requests.observed).toBe(7);
  expect(run.report.accounting.search.tokens.filter((x:any)=>x.unit==='reasoning_tokens').reduce((n:number,x:any)=>n+x.value,0)).toBe(14);
  expect(f.claims.size).toBe(7);expect(terminalBounds.every(bound=>bound===cancelAt+5000)).toBe(true);
  if(expired){expect(f.calls.length).toBe(actualCallsAt);expect(run.report.observations.runs.baseline.checks).toBeDefined();}
  else if(!unclosed)expect(terminalBounds.length).toBeGreaterThan(0);
  expect(await inspectCampaign(loaded)).toMatchObject(unclosed?{status:'uncertain',dispatch_status:'uncertain',exit_code:3}:{status:'cancelled',dispatch_status:'cancelled',exit_code:130});
 }),30000);
 it('closeout_n2_expired_original_ceiling_retains_known_and_reserved_without_new_processes',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),original=f.deps.runProcess;let expiredAt=0,callsAt=0;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);
   if(spec.args[0]==='run'&&spec.args[spec.args.indexOf('--name')+1]==='mfopt-'+loaded.c.campaign_id+'-general_proposal'){f.advance(1200000);expiredAt=f.deps.now();callsAt=f.calls.length;}
   return reply;
  }});
  expect(expiredAt).toBeGreaterThan(0);expect(f.calls.length).toBe(callsAt);
  expect(run).toMatchObject({exit:3,status:'uncertain'});expect(run.report.recommendation).toBe('inconclusive');
  expect(run.report.accounting.search.model_requests).toMatchObject({observed:null,reserved:1,status:'uncertain'});
  expect(run.report.accounting.search.provenance.find((p:any)=>p.known_subtotals).known_subtotals).toEqual({model_requests:1,tool_calls:0});
  expect(run.report.accounting.search.tokens).toBeNull();expect(run.report.accounting.search.cost).toBeNull();expect(f.claims.size).toBe(2);
 }),30000);
 it('closeout_n2_final_assess_expiry_is_closed_timeout_without_deadline_extension',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),original=f.deps.runProcess;let expired=false,callsAt=0,originalCeiling=0;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);
   if(!expired&&spec.args[1]==='inspect'&&await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false)){expired=true;originalCeiling=spec.deadline;f.advance(originalCeiling-f.deps.now()+1);callsAt=f.calls.length;}
   return reply;
  }});
  expect(expired).toBe(true);expect(run).toMatchObject({exit:124,status:'timeout'});expect(f.calls.length).toBe(callsAt);
  expect(run.report.accounting.search.model_requests.observed).toBe(7);expect(run.report.recommendation).toBe('inconclusive');
  expect(await inspectCampaign(loaded)).toMatchObject({status:'timeout',exit_code:124});
 }),30000);

 it.each([false,true])('closeout_n2_review_cancel_during_broker_admission_preserves_not_started_unknown_%s',joined(async brokerUnknown=>{
  const f=await setup({brokerUnknown}),loaded=await f.prepare(),controller=new AbortController(),admit=f.broker.admit.bind(f.broker);
  f.broker.admit=async(value:any)=>{const access=await admit(value);controller.abort();return access;};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal});
  expect(run).toMatchObject(brokerUnknown?{exit:3,status:'uncertain'}:{exit:130,status:'cancelled'});
  const closure=await contracts.json(path.join(loaded.dir,'receipts/baseline-closure.json'));
  expect(closure.worker).toMatchObject({process:'not_started',container:'not_started'});
  expect(closure.evaluator).toMatchObject({process:'not_started',container:'not_started'});
  expect(closure.broker).toBe(brokerUnknown?'uncertain':'closed');
  expect(closure.writers).toBe(brokerUnknown?'may_live':'closed');
  expect(f.calls.filter((call:any)=>call.args[0]==='run')).toHaveLength(0);expect(f.claims.size).toBe(1);
  expect(await inspectCampaign(loaded)).toMatchObject(brokerUnknown?{status:'uncertain',exit_code:3}:{status:'cancelled',exit_code:130});
 }),30000);
 it('closeout_n2_review_cancel_during_evaluator_preparation_never_marks_it_started',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;
  const probeRun=path.join(loaded.dir,'runs/baseline');let inspected=false,triggered=false,inspectionDir:string|null=null;
  const deps={...f.deps,signal:controller.signal,now:()=>{
   if(inspected&&!triggered){triggered=true;controller.abort();}
   return f.deps.now();
  },runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);if(spec.args[0]===loaded.c.base.cli_path&&spec.args[1]==='inspect'&&spec.args[2]==='--run'&&spec.args[3]===probeRun){inspected=true;inspectionDir=spec.args[3];}return reply;
  }};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),deps);
  expect(triggered).toBe(true);expect(inspectionDir).toBe(probeRun);expect(run).toMatchObject({status:'cancelled',exit:130});
  const closure=await contracts.json(path.join(loaded.dir,'receipts/baseline-closure.json'));
  expect(closure.worker).toMatchObject({process:'closed',container:'closed'});expect(closure.evaluator).toMatchObject({process:'not_started',container:'not_started'});expect(closure.status).toBe('closed');
  expect(f.calls.filter((call:any)=>call.args[0]==='run')).toHaveLength(1);expect(f.claims.size).toBe(1);
 }),30000);
 it('closeout_n2_review_pre_spawn_worker_deadline_preserves_not_started_and_closed_timeout',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),admit=f.broker.admit.bind(f.broker);let clockChecks=0,admitted=false;
  f.broker.admit=async(value:any)=>{const access=await admit(value);admitted=true;return access;};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,now:()=>{
   if(admitted&&++clockChecks===1)f.advance(loaded.map.slots[0].limits.elapsed_seconds*1000+1);
   return f.deps.now();
  }});
  expect(run).toMatchObject({exit:124,status:'timeout'});
  const closure=await contracts.json(path.join(loaded.dir,'receipts/baseline-closure.json'));
  expect(closure.worker).toMatchObject({process:'not_started',container:'not_started'});expect(closure.evaluator).toMatchObject({process:'not_started',container:'not_started'});expect(closure).toMatchObject({broker:'closed',writers:'closed',status:'closed'});
  expect(f.calls.filter((call:any)=>call.args[0]==='run')).toHaveLength(0);expect(f.claims.size).toBe(1);
 }),30000);
 it('v2 refuses a worker-rewritten saved Result before trusting the probe',joined(async()=>{
  const f=await setup({forgeSavedAt:'general_probe'}),loaded=await f.prepare();
  const run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run).toMatchObject({exit:3,status:'uncertain'});
  expect(run.report.recommendation).toBe('inconclusive');
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));
  expect(evidence.runs.general).toBeNull();expect(evidence.evaluations.general).toBeNull();
  const anchor=await contracts.json(path.join(loaded.dir,'receipts/general_probe-base-result.json'));
  const saved=await contracts.json(path.join(loaded.dir,'runs/general_probe/result.json'));
  expect(saved.output).not.toBe(anchor.output);
 }),30000);
 it.each([true,false])('closeout_n2_review_early_closed_read_timeout_is_overall_only_%s',joined(async overall=>{
  const f=await setup(),loaded=await f.prepare(),original=f.deps.runProcess,probeRun=path.join(loaded.dir,'runs/baseline');let triggered=false,stoppedAt=0,readBound=0,callsAt=0,inspectionDir:string|null=null;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);
   if(!triggered&&spec.args[0]===loaded.c.base.cli_path&&spec.args[1]==='inspect'&&spec.args[2]==='--run'&&spec.args[3]===probeRun){
    const final=await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false);
    if(final===overall){triggered=true;inspectionDir=spec.args[3];readBound=spec.deadline;f.advance(spec.deadline-f.deps.now()-1000);stoppedAt=f.deps.now();callsAt=f.calls.length;return {...reply,exit:null,timedOut:true,closed:true};}
   }
   return reply;
  }});
  expect(triggered).toBe(true);expect(inspectionDir).toBe(probeRun);expect(stoppedAt).toBeLessThan(readBound);expect(f.calls.length).toBe(callsAt);
  expect(run).toMatchObject(overall?{status:'timeout',exit:124}:{status:'uncertain',exit:3});
  expect(run.report.recommendation).toBe('inconclusive');expect(f.claims.size).toBe(overall?7:1);
  if(overall)expect(run.report.accounting.search.model_requests.observed).toBe(7);
  expect(await inspectCampaign(loaded)).toMatchObject(overall?{status:'timeout',exit_code:124}:{status:'uncertain',exit_code:3});
 }),30000);
 it('closeout_n2_review_already_terminal_assessment_failure_is_not_retried',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;let terminalReads=0;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);
   if(spec.args[0]==='run'&&spec.args[spec.args.indexOf('--name')+1]==='mfopt-'+loaded.c.campaign_id+'-general_proposal'){controller.abort();return {...reply,cancelled:true};}
   if(spec.args[1]==='inspect'&&await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false)){terminalReads++;expect(signal).toBeUndefined();return {...reply,exit:1};}
   return reply;
  }});
  expect(terminalReads).toBe(1);expect(run).toMatchObject({status:'cancelled',exit:130});
  expect(run.report.accounting.search.model_requests.observed).toBe(2);expect(run.report.recommendation).toBe('inconclusive');expect(f.claims.size).toBe(2);
 }),30000);

 it('closeout_n2_review_short_broker_protocol_timeout_keeps_unknown_admission_as_3',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),admit=f.broker.admit.bind(f.broker);
  f.broker.admit=async(value:any)=>{await admit(value);throw new contracts.OptimizationError('TIMEOUT','synthetic short protocol timeout');};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run).toMatchObject({exit:3,status:'uncertain'});expect(f.calls.filter((call:any)=>call.args[0]==='run')).toHaveLength(0);
  const closure=await contracts.json(path.join(loaded.dir,'receipts/baseline-closure.json'));expect(closure.worker).toMatchObject({process:'not_started',container:'not_started'});expect(run.report.recommendation).toBe('inconclusive');
 }),30000);

 it('closeout_n2_c4_tolerated_evaluator_rm_timeout_does_not_capture_terminal_deadline',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),original=f.deps.runProcess,controller=new AbortController();
  const runtime=loaded.c.execution.runtime_path;let firstEvaluatorCid:string|null=null,nextWorkerCid:string|null=null,injected=false;
  let originalCeiling=0,rmBound=0,nextBound=0,nextAt=0,getterValue:number|undefined,rmSignal:AbortSignal|undefined;
  const finalBounds:number[]=[],finalSignals:(AbortSignal|undefined)[]=[];
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const enteredAt=f.deps.now(),reply=await original(spec,signal,cleanup);
   if(spec.executable===runtime&&spec.args[0]==='version')originalCeiling=spec.deadline;
   if(spec.executable===runtime&&spec.args[0]==='run'){
    const name=spec.args[spec.args.indexOf('--name')+1],cidfile=spec.args[spec.args.indexOf('--cidfile')+1];
    if(name==='mfopt-'+loaded.c.campaign_id+'-baseline-eval')firstEvaluatorCid=(await contracts.read(cidfile)).toString().trim();
    if(name==='mfopt-'+loaded.c.campaign_id+'-general_proposal')nextWorkerCid=(await contracts.read(cidfile)).toString().trim();
   }
   if(!injected&&firstEvaluatorCid&&spec.executable===runtime&&spec.args[0]==='rm'&&spec.args[1]===firstEvaluatorCid){
    injected=true;rmBound=spec.deadline;rmSignal=signal;getterValue=cleanup?.();
    f.advance(Math.max(0,spec.deadline-f.deps.now()));
    return {...reply,exit:null,timedOut:true,closed:true};
   }
   if(nextWorkerCid&&nextBound===0&&spec.executable===runtime&&spec.args[0]==='inspect'&&spec.args.at(-1)===nextWorkerCid){nextBound=spec.deadline;nextAt=enteredAt;}
   if(spec.args[0]===loaded.c.base.cli_path&&await fs.stat(path.join(loaded.dir,'evidence.json')).then(()=>true,()=>false)){finalBounds.push(spec.deadline);finalSignals.push(signal);}
   return reply;
  }});
  expect(injected).toBe(true);expect(rmSignal).toBeUndefined();expect(getterValue).toBeUndefined();
  expect(run,JSON.stringify(run.report)).toMatchObject({status:'completed',exit:0});expect(run.report.recommendation).toBe('composed');expect(f.claims.size).toBe(7);
  expect(nextBound).toBeGreaterThan(rmBound);expect(nextBound).toBe(nextAt+5000);
  expect(originalCeiling).toBeGreaterThan(nextBound);expect(finalBounds.length).toBeGreaterThan(0);expect(finalBounds.every(bound=>bound===originalCeiling)).toBe(true);expect(finalSignals.every(signal=>signal===controller.signal)).toBe(true);
 }),30000);

 it('duplicate edit IDs keep the first general/domain/composed entry and its references',joined(async()=>{
  const f=await setup({singleEdit:true,instructionByKind:{general:'shared guidance',domain:'shared guidance'},toolsByKind:{general:['read'],domain:['read']}}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.report.candidate_digests.general).toBe(run.report.candidate_digests.domain);
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json')),general=evidence.runs.general,domain=evidence.runs.domain;expect(general.result_sha256).not.toBe(domain.result_sha256);
  const shared=run.report.observations.edit_assessments.filter((e:any)=>e.candidate_digest===run.report.candidate_digests.general);expect(shared.map((e:any)=>e.field).sort()).toEqual(['/instructions','/tools']);
  for(const e of shared){expect(e.evidence_refs).toContain(general.result_sha256);expect(e.evidence_refs).not.toContain(domain.result_sha256);expect(e.verdict).toBe('confounded');}
 }),30000);
});

