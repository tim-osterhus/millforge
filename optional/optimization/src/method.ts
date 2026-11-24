 /** The fixed seven-item algorithm. No generic scheduler, resume or implicit adoption. */
 import fs from 'node:fs/promises';
 import path from 'node:path';
 import {
  type Obj,OptimizationError,adverse,campaign,canonical,closed,codemode,copyTree,directory,exact,fail,fresh,hash,hostGenerate,isV2,json,object,parse,read,requireOptimizerScope,roleTarget,same,tree,write
 }
 from './contracts.js';
 import {
  type Dependencies,type ProcessSpec,accessEnvironment,assertParentHeldResult,assertSlotRecords,authAdmission,baseCommand,brokerReceipt,containerClosure,evaluatorSpec,hostAdmission,hostClosed,hostCounts,hostSpec,matchQualification,notStarted,profile,qualify,stdoutResult,verifyBase,workerSpec
 }
 from './lifecycle.js';
 import {
  type Loaded,artifactInventory,evaluator,libraryProjection,loadCampaign,materialize,resolveRef,taskInput
 }
 from './store.js';
 import {
  assess,compatibility,fileDigest,searchLedger,validateChecks
 }
 from './evaluation.js';
 import {baseVersion,protocol} from './versions.js';
import {deployedAuthority,deployedWorkMap,optionalProtectedRead,protectedInput,protectedPath,protectedRead,verifyDeployment} from './protected-deployment.js';
 export interface CampaignResult {
  exit:number;
  status:string;
  report:Obj
 }
 function blankLink():Obj{
  return {
   claim_sha256:null,run_dir:null,result_sha256:null,trace_sha256:null,closure_receipt_sha256:null,broker_receipt_sha256:null
  }
  ;
 }
 function instruction(kind:string):string{
  return kind==='general'?'Propose portable general harness guidance grounded in the stopped baseline. Return only instruction_text,tools,assets,rationale JSON.':kind==='domain'?'Propose task-domain harness guidance grounded in the stopped baseline. Return only instruction_text,tools,assets,rationale JSON.':'Compose the two stopped probe candidates using their observed evidence. Keep baseline or a parent when appropriate. Explain retained/rejected edits in rationale. Return only instruction_text,tools,assets,rationale JSON.';
 }
 function redact(value:unknown,tokens:string[]):any {
  let s=canonical(value);
  for(const token of tokens)if(token)s=s.split(JSON.stringify(token).slice(1,-1)).join('[REDACTED_BROKER_ACCESS]');
  return parse(s);
 }
 async function projection(loaded:Loaded,kind:string,observations:Obj[],deps:Dependencies,deadline:number,tokens:string[]):Promise<string>{
  const editable=loaded.baseline.profile;
  const payload={
   task:(await taskInput(loaded.c,loaded.dir)).task,context:(await taskInput(loaded.c,loaded.dir)).context,allowed_edits:{
    mutable_fields:loaded.c.mutable_fields,allowed_tools:loaded.c.allowed_tools,assets:loaded.c.assets.map((a:Obj)=>({
     path:a.path,kind:a.kind
    }
    )),baseline:{
     instruction_text:editable.instructions,tools:editable.tools,assets:[...loaded.baseline.assets].filter(([rel])=>loaded.c.assets.some((a:Obj)=>a.path===rel)).map(([rel,data])=>({
      path:rel,kind:editable.assets.find((a:Obj)=>a.path===rel).kind,text:data.toString('utf8')
     }
     ))
    }
   }
   ,library:await libraryProjection(loaded,deps,deadline),observations:observations.map(o=>({
    kind:o.kind,profile_digest:o.profile_digest,events:o.trace.flatMap((event:Obj)=>event.kind==='output_partial'?[{
     kind:'output_partial',text:event.payload.text
    }
    ]:event.kind==='tool_start'?[{
     kind:'tool_start',name:event.payload.name,arguments:event.payload.arguments
    }
    ]:event.kind==='tool_end'?[{
     kind:'tool_end',status:event.payload.status,text:event.payload.text,truncated:event.payload.truncated
    }
    ]:[]),checks:o.evaluation.checks.map((c:Obj)=>({
     check_id:c.check_id,pass:c.pass
    }
    )),diagnostic:o.result.diagnostic?.code??null,counts:{
     model_requests:o.broker.status==='closed'?o.broker.admitted:null,tool_calls:closed(o.result)?o.result.counts.tool_calls:null,elapsed_ms:closed(o.result)?o.result.timing.elapsed_ms:null
    }
   }
   ))
  }
  ;
  return instruction(kind)+'\n'+canonical(redact(payload,tokens));
 }
 async function admitDeployedCodemode(campaignDir:string,approvalFile:string,c:Obj,deps:Dependencies){
  const paths=deps.codemodeDeployment;
  requireOptimizerScope(c,deps.scope,paths!==undefined);
  if(!paths)fail('unregistered Codemode installation');
  const dir=await directory(campaignDir);
  await verifyDeployment(paths,dir);
  if(path.resolve(approvalFile)!==paths.authority)fail('approval is outside the protected authority path');
  await protectedPath(path.dirname(paths.authority),'directory',true);
  const authorityBytes=await protectedRead(paths.authority);
  const approval=deployedAuthority(authorityBytes,c,dir,Date.now());
  const campaignFile=path.join(dir,'campaign.json');
  const campaignBytes=await protectedRead(campaignFile);
  if(!same(c,parse(campaignBytes)))fail('captured campaign changed');
  const map=await deployedWorkMap(dir,c);
  const execution=c.execution;
  await protectedInput(paths,execution.qualification.path,'file');
  const qualificationBytes=await protectedRead(execution.qualification.path);
  if(hash(qualificationBytes)!==execution.qualification.sha256)fail('qualification changed');
  matchQualification(object(parse(qualificationBytes)),c,'deployed',null,true);
  if(await optionalProtectedRead(path.join(dir,'run.json')))fail('campaign already started');
  await protectedInput(paths,c.base.prefix_path,'directory');
  await protectedInput(paths,c.base.cli_path,'file');
  await protectedInput(paths,execution.runtime_path,'file');
  if(execution.runtime!=='podman'||hash(await read(execution.runtime_path))!==execution.runtime_sha256)fail('runtime changed');
  await protectedInput(paths,c.task.evaluator_path,'directory');
  await taskInput(c,dir);
  await evaluator(c);
  return {dir,paths,map,approval,authorityBytes,qualificationBytes,campaignBytes};
 }
 export async function runCampaign(campaignDir:string,approvalFile:string,deps:Dependencies):Promise<CampaignResult>{
  const begin=deps.now(),startedAt=new Date().toISOString();
  const initial=campaign(await json(path.join(campaignDir,'campaign.json')));
  if(path.resolve(campaignDir)===deps.codemodeDeployment?.campaign&&!codemode(initial))fail('protected campaign root requires Codemode');
  const deployed=codemode(initial)&&deps.scope==='deployed'?await admitDeployedCodemode(campaignDir,approvalFile,initial,deps):null;
  const validationDeadline=Math.min(begin+initial.budget.elapsed_seconds*1000,
    deployed?deps.now()+Date.parse(deployed.approval.expires_at)-Date.now():Infinity);
  const baselineReserve=(initial.budget.executor_seconds+initial.budget.evaluation_seconds+5)*1000;
  const validationDeps=deployed?{...deps,runProcess:async(spec:ProcessSpec,signal?:AbortSignal)=>{
   if(Date.now()>=Date.parse(deployed.approval.expires_at))fail('authority expired before validation');
   if(deps.now()+baselineReserve>=validationDeadline)fail('insufficient remaining captured slot budget','INSUFFICIENT_TIME');
   return deps.runProcess(spec,signal);
  }}:deps;
  const loaded=await loadCampaign(campaignDir,initial.base.cli_path,validationDeps,validationDeadline,async(dir,c)=>{
   if(deployed){
    if(dir!==deployed.dir||!same(c,initial))fail('captured campaign changed');
    return true;
   }
   requireOptimizerScope(c,deps.scope);
   if(codemode(c)){
    const bytes=await read(c.execution.qualification.path);
    if(hash(bytes)!==c.execution.qualification.sha256)fail('qualification changed');
    matchQualification(object(parse(bytes)),c,deps.scope,null);
   }
  });
  const {c,dir,digest,map}=loaded,a=deployed?.approval??await json(approvalFile);
  exact(a,['schema','owner_id','campaign_id','campaign_digest','execution_root','budget','expires_at']);
  if(a.schema!=='millforge-optimization-authority/v1'||a.owner_id!==c.owner_id||a.campaign_id!==c.campaign_id||a.campaign_digest!==digest||a.execution_root!==dir||!Number.isFinite(Date.parse(a.expires_at))||Date.parse(a.expires_at)<=Date.now())fail('authority identity/root/expiry mismatch');
  for(const key of Object.keys(c.budget)){
   if(c.budget[key]===null){
    if(a.budget[key]!==null)fail('authority purpose differs');
   }
   else if(typeof a.budget[key]!=='number'||!Number.isSafeInteger(a.budget[key])||a.budget[key]>c.budget[key])fail('authority widens/invalid budget');
  }
  exact(a.budget,Object.keys(c.budget));
  const sum=(key:string):number=>map.slots.reduce((n:number,s:Obj)=>n+s.limits[key],0);
  if(a.budget.max_work_items<map.slots.length||a.budget.max_model_requests<sum('model_requests')||a.budget.max_tool_calls<sum('tool_calls')||a.budget.elapsed_seconds*1000<=map.slots.reduce((n:number,s:Obj)=>n+(s.limits.elapsed_seconds+s.evaluation_seconds)*1000,5000)||map.slots.some((s:Obj)=>s.limits.elapsed_seconds>(s.kind==='proposal'?a.budget.proposal_seconds:a.budget.executor_seconds)||s.evaluation_seconds>a.budget.evaluation_seconds))fail('authority cannot fit frozen map');
  const deadline=Math.min(begin+a.budget.elapsed_seconds*1000,deps.now()+Date.parse(a.expires_at)-Date.now());
  const supplied=deps,inspections=new Map<string,Obj>(),tokens:string[]=[];
  let terminalDeadline:number|undefined,processUncertain=false,overallTimedOut=false;
  const stopDeadline=():number=>terminalDeadline??=Math.min(deadline,supplied.now()+5000);
  const onAbort=():void=>{stopDeadline();};
  supplied.signal?.addEventListener('abort',onAbort,{once:true});
  deps={...supplied,runProcess:async(spec,signal,cleanup)=>{
   if(supplied.now()>=spec.deadline)fail('process deadline expired','TIMEOUT');
   const result=await supplied.runProcess(spec,signal,cleanup??(signal?stopDeadline:undefined));
   if(!result.closed)processUncertain=true;
   if(result.timedOut&&spec.deadline>=deadline){overallTimedOut=true;stopDeadline();}
   return result;
  }};
  const cleanupBound=():number=>terminalDeadline??Math.min(deadline,deps.now()+5000);
  try{
  try{
   await fs.lstat(path.join(dir,'run.json'));
   fail('campaign already started');
  }
  catch(e){
   if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;
  }
  if(!isV2(c))fail('new runs require campaign v2','UNSUPPORTED_VERSION');
  if(c.base.package_version!==baseVersion)fail('new runs require Millforge '+baseVersion,'UNSUPPORTED_VERSION');
  if(deps.signal?.aborted)fail('cancelled before admission','CANCELLED');
  if(!deployed){await verifyBase(c.base);await evaluator(c);}
  const qualified=await qualify(c,deps,deadline,loaded.proposer?.digest??null,deployed?.qualificationBytes);
  authAdmission(loaded.baseline.profile,c.execution.model_endpoint.target,qualified.identity.access_modes.target);
  const host=hostGenerate(c);
  if(loaded.proposer&&host)hostAdmission(loaded.proposer.profile,c,qualified.identity.access_modes.proposal);
  else if(loaded.proposer)authAdmission(loaded.proposer.profile,c.execution.model_endpoint.proposal,qualified.identity.access_modes.proposal);
  else if(qualified.identity.access_modes.proposal!==null)fail('validation must not expose a proposal route');
  const optimizerPath=await fs.realpath(new URL(import.meta.url.endsWith('.ts')?'./cli.ts':'./cli.js',import.meta.url)),optimizer={
   path:deployed?.paths.entry??optimizerPath,sha256:hash(await read(deployed?.paths.entry??optimizerPath)),...protocol
  }
  ;
  if(deployed){
   await verifyDeployment(deployed.paths,dir);
   await verifyBase(c.base);
   if(!(await protectedRead(path.join(dir,'campaign.json'))).equals(deployed.campaignBytes)||
      !same(map,deployed.map)||!same(await deployedWorkMap(dir,c),map)||
      !(await protectedRead(deployed.paths.authority)).equals(deployed.authorityBytes)||
      !(await protectedRead(c.execution.qualification.path)).equals(deployed.qualificationBytes)||
      hash(await protectedRead(c.execution.runtime_path,false))!==c.execution.runtime_sha256){
    fail('protected deployment input changed');
   }
   await taskInput(c,dir);
   await evaluator(c);
   if(deps.signal?.aborted)fail('cancelled before admission','CANCELLED');
   deployedAuthority(deployed.authorityBytes,c,dir,Date.now());
   if(deps.now()+baselineReserve>=deadline){
    fail('insufficient remaining captured slot budget','INSUFFICIENT_TIME');
   }
  }
  await write(path.join(dir,'run.json'),{
   schema:'millforge-optimization-run/v1',campaign_id:c.campaign_id,campaign_digest:digest,execution_root:dir,authority_sha256:hash(deployed?.authorityBytes??await read(approvalFile)),started_at:deployed?new Date().toISOString():startedAt,elapsed_seconds:a.budget.elapsed_seconds,optimizer
  }
  );
  await write(path.join(dir,'receipts/qualification.json'),deployed?.qualificationBytes??await read(c.execution.qualification.path));
  await write(path.join(dir,'receipts/authority.json'),deployed?.authorityBytes??await read(approvalFile));
  const evidence:Obj={
   schema:'millforge-optimization-evidence/v1',campaign_id:c.campaign_id,protection_scope:deps.scope,bindings:c.bindings,runs:{
    baseline:null,general:null,domain:null,composed:null
   }
   ,evaluations:{
    baseline:null,general:null,domain:null,composed:null
   }
   ,claims:Object.fromEntries(map.slots.map((s:Obj)=>[s.work_id,blankLink()])),reuse_evidence:[],ordinary_execution:null
  }
  ;
  const claimDigests:Obj={
  }
  ,observed:Obj={
  }
  ,candidates:Obj={
  }
  ;
  let terminal='completed',exit=0,uncertain=false;
  const failedWork=new Set<string>();
  // These are owned method parents. fresh() intentionally never adopts or recursively creates its destination.
  for(const name of ['workers','runs','candidates'])await fresh(path.join(dir,name));
  for(const slot of map.slots){
   if(slot.parents.some((parent:string)=>failedWork.has(parent))){
    failedWork.add(slot.work_id);
    continue;
   }
   let slotFailed=false;
   const work=slot.work_id as string,isProposal=slot.kind==='proposal',kind=work==='baseline'?'baseline':work.startsWith('general')?'general':work.startsWith('domain')?'domain':'composed';
   let worker=notStarted(),evaluation=notStarted(),brokerState='not_admitted',writers='closed',claim:Obj|null=null,result:Obj|null=null,broker:Obj|null=null,evalOutput:Obj|null=null;
   const link=evidence.claims[work],hostSlot=isProposal&&host;
   let slotCleanupDeadline:number|undefined,invocationExpiredBeforeSpawn=false,hostSpawned=false;
   try{
    if(deps.signal?.aborted)fail('cancelled','CANCELLED');
    if(deps.now()>=deadline)fail('campaign deadline expired','TIMEOUT');
    let p=loaded.baseline;
    if(isProposal)p=loaded.proposer!;
    else if(kind!=='baseline'){
     if(!candidates[kind])fail('missing candidate dependency');
     p=candidates[kind];
    }
    const task=await taskInput(c,dir);
    let taskText=task.task,context=task.context;
    if(isProposal){
     const dependencies=kind==='composed'?[observed.general,observed.domain]:[observed.baseline];
     if(dependencies.some(x=>!x))fail('missing stopped proposal dependency');
     taskText=await projection(loaded,kind,dependencies,deps,deadline,tokens);
     context=[];
    }
    if(Buffer.byteLength(taskText)+context.reduce((n:number,v:string)=>n+Buffer.byteLength(v),0)>slot.limits.input_bytes)fail('assembled input exceeds proposer/worker limit');
    if(deps.now()+(slot.limits.elapsed_seconds+slot.evaluation_seconds+5)*1000>deadline)fail('insufficient remaining captured slot budget','INSUFFICIENT_TIME');
    const workRoot=path.join(dir,'workers',work);
    await fresh(workRoot);
    const workspace=path.join(workRoot,'workspace'),inputWorkspace=path.join(dir,'inputs/workspace');
    try{
     await copyTree(inputWorkspace,workspace);
    }
    catch(e){
     if((e as NodeJS.ErrnoException).code==='ENOENT')await fresh(workspace);
     else throw e;
    }
    const output=await fresh(path.join(workRoot,'output'));
    const request={
     schema:'millforge-request/v1',request_id:slot.request_id,profile_id:p.profile.profile_id,profile_digest:p.digest,expected_mode:p.profile.mode,task:taskText,context,workspace:p.profile.mode==='agent'?'/workspace':null,limits:slot.limits,correlation:{
      campaign_id:c.campaign_id,work_id:work
     }
    }
    ;
    const requestFile=path.join(dir,'requests',work+'.json');
    await write(requestFile,request);
    claim={
     schema:'millforge-optimization-claim/v1',campaign_id:c.campaign_id,campaign_digest:digest,work_id:work,request_id:slot.request_id,reserved_model_requests:slot.limits.model_requests,reserved_tool_calls:slot.limits.tool_calls,reserved_elapsed_ms:(slot.limits.elapsed_seconds+slot.evaluation_seconds)*1000,timestamp:new Date().toISOString(),execution_root:dir
    }
    ;
    await write(path.join(dir,'claims',work+'.json'),claim);
    link.claim_sha256=claimDigests[work]=fileDigest(claim);
    const cidfile=path.join(workRoot,'worker.cid'),slotDeadline=Math.min(deadline-5000,deps.now()+slot.limits.elapsed_seconds*1000);
    let spec:ProcessSpec;
    if(hostSlot){
     // Host teacher: the claim above is durable; no broker admission/close and no container.
     spec=hostSpec(c,p.dir,requestFile,path.join(output,'run'),workRoot,slotDeadline);
    }
    else{
     if(deps.signal?.aborted)fail('cancelled before broker admission','CANCELLED');
     if(deps.now()>=deadline)fail('campaign deadline expired','TIMEOUT');
     brokerState='uncertain';
     let access:Obj;
     try{
      access=await qualified.broker.admit({
       campaign_digest:digest,work_id:work,request_id:slot.request_id,route:isProposal?'proposal':'target',reserved_model_requests:slot.limits.model_requests,deadline:new Date(Date.now()+Math.max(0,Math.min(deadline-deps.now(),slot.limits.elapsed_seconds*1000))).toISOString()
      }
      );
     }
     catch(e){
      if((e as Obj).code==='CLAIM_EXISTS')brokerState='not_admitted';
      throw e;
     }
     const accessEnv=accessEnvironment(p.profile,access,digest,slot.request_id,c.execution.broker.route_digest);
     for(const value of Object.values(accessEnv))if(value)tokens.push(value);
     spec=workerSpec(c,work,p.dir,requestFile,workspace,output,cidfile,slotDeadline,accessEnv);
    }
    if(deps.signal?.aborted)fail('cancelled before worker spawn','CANCELLED');
    if(deps.now()>=spec.deadline){invocationExpiredBeforeSpawn=true;fail('worker deadline expired before spawn','TIMEOUT');}
    worker={
     ...notStarted(),process:'may_live',container:hostSlot?'not_started':'may_live'
    }
    ;
    hostSpawned=hostSlot;
    const process=await deps.runProcess(spec,deps.signal);
    if(process.cancelled||process.timedOut)stopDeadline();
    const cleanupDeadline=slotCleanupDeadline=cleanupBound();
    worker=hostSlot?{pid:process.pid,pid_start:process.pid_start,pgid:process.pgid,container_id:null,process:process.closed?'closed':'may_live',container:'not_started'}:await containerClosure(c,process,cidfile,deps,cleanupDeadline);
    if(worker.process!=='closed'||!hostSlot&&worker.container!=='closed')fail('worker closure uncertain','CLOSURE_UNCERTAIN');
    if(!hostSlot){
     broker=brokerReceipt(await qualified.broker.close(slot.request_id,slotCleanupDeadline??cleanupBound()),c,slot.request_id,slot.limits.model_requests);
     brokerState=broker.status==='closed'?'closed':'uncertain';
     await write(path.join(dir,'receipts',work+'-broker.json'),broker);
     link.broker_receipt_sha256=fileDigest(broker);
     if(brokerState!=='closed')fail('broker closure uncertain','CLOSURE_UNCERTAIN');
    }
    // Retain the original public stdout Result outside writable mounts before consuming the saved chain.
    const anchor=stdoutResult(process.stdout,roleTarget(c,isProposal).result_schema);
    await write(path.join(dir,'receipts',work+'-base-result.json'),anchor);
    const runDir=path.join(dir,'runs',work);
    const runBytes=Math.max(67108864,slot.limits.trace_bytes+slot.limits.tool_output_bytes+slot.limits.output_bytes+33554432);
    await copyTree(path.join(output,'run'),runDir,runBytes);
    const runTree=codemode(c)?(await tree(runDir,runBytes)).digest:null;
    // Stopped-run validation is cleanup, not a new worker effect; preserve its original bound.
    const {signal:_cancel,...cleanupDeps}=deps;
    const inspected=await baseCommand(c.base,['inspect','--run',runDir],cleanupDeps,cleanupDeadline);
    result=object(inspected.result);
    assertParentHeldResult(anchor,result);
    assertSlotRecords(inspected,c,slot,p.digest,p.profile.profile_id);
    if(!hostSlot&&result.counts.model_requests>broker!.admitted)fail('base counts outside broker coverage');
    link.run_dir='runs/'+work;
    link.result_sha256=hash(await read(path.join(runDir,'result.json')));
    link.trace_sha256=hash(await read(path.join(runDir,'trace.jsonl')));
    inspections.set(runDir,{result_sha256:link.result_sha256,trace_sha256:link.trace_sha256,runTree,runBytes,value:inspected});
    if(process.timedOut)fail('worker timed out','TIMEOUT');
    if(process.cancelled||deps.signal?.aborted)fail('worker cancelled','CANCELLED');
    if(!(hostSlot?hostClosed(result):closed(result)))fail('base run closure uncertain','CLOSURE_UNCERTAIN');
    if(hostSlot&&!hostCounts((await read(path.join(runDir,'trace.jsonl'))).toString('utf8'),result))fail('host generate accounting inconsistent','CLOSURE_UNCERTAIN');
    if(result.status!=='completed'&&!adverse(result))fail('worker failed');
    if(kind==='baseline'&&result.status!=='completed'&&!(c.purpose==='baseline_validation'&&adverse(result)))fail('baseline must complete');
    if(isProposal){
     if(result.status!=='completed')fail('proposal failed');
     const draft=typeof result.output==='string'?parse(result.output):result.output;
     exact(draft,['instruction_text','tools','assets','rationale']);
     const parents=kind==='composed'?[candidates.general.digest,candidates.domain.digest]:[],value={
      schema:'millforge-optimization-proposal/v1',campaign_id:c.campaign_id,kind,baseline_digest:loaded.baseline.digest,parent_candidates:parents,...draft
     }
     ;
     await write(path.join(dir,'proposals',kind+'.json'),value);
     candidates[kind]=await materialize(loaded,value,path.join(dir,'candidates',kind),deps,deadline,kind,parents);
    }
    else{
     const manifest=await evaluator(c),deliverables=path.join(workRoot,'deliverables');
     await copyTree(workspace,deliverables);
     const delivered=(await tree(deliverables)).digest,receiptDir=await fresh(path.join(workRoot,'evaluator-output')),evalCid=path.join(workRoot,'evaluator.cid');
     const spec=evaluatorSpec(c,work,deliverables,receiptDir,evalCid,manifest.entrypoint,Math.min(deadline-5000,deps.now()+slot.evaluation_seconds*1000));
     if(deps.signal?.aborted)fail('cancelled before evaluator spawn','CANCELLED');
     if(deps.now()>=spec.deadline){invocationExpiredBeforeSpawn=true;fail('evaluator deadline expired before spawn','TIMEOUT');}
     evaluation={
      ...notStarted(),process:'may_live',container:'may_live'
     }
     ;
     const evalProcess=await deps.runProcess(spec,deps.signal);
     if(evalProcess.cancelled||evalProcess.timedOut)stopDeadline();
     slotCleanupDeadline=cleanupBound();
     evaluation=await containerClosure(c,evalProcess,evalCid,deps,slotCleanupDeadline);
     if(evaluation.process!=='closed'||evaluation.container!=='closed')fail('evaluator closure uncertain','CLOSURE_UNCERTAIN');
     if(evalProcess.timedOut)fail('evaluator timeout','TIMEOUT');
     if(evalProcess.cancelled||deps.signal?.aborted)fail('evaluator cancelled','CANCELLED');
     if(evalProcess.exit!==0)fail('evaluator failed');
     const raw=await read(path.join(receiptDir,'evaluation.json'),65536),outputValue=parse(raw);
     exact(outputValue,['schema','evaluator_id','deliverable_digest','checks']);
     if(outputValue.schema!=='millforge-optimization-evaluator-output/v1'||outputValue.evaluator_id!==c.task.evaluator_id||outputValue.deliverable_digest!==delivered||!same(outputValue.checks.map((x:Obj)=>x.check_id),manifest.check_ids))fail('evaluator identity/check set mismatch');
     validateChecks(outputValue.checks);
     if((await tree(deliverables)).digest!==delivered)fail('deliverable mutated during evaluation','CLOSURE_UNCERTAIN');
     evalOutput={
      ...outputValue,sha256:hash(raw)
     }
     ;
    }
   }
   catch(error){
    slotFailed=true;
    const code=(error as Obj).code;
    const unknownRecord=claim!==null&&link.result_sha256===null&&(hostSpawned||worker.process==='closed'&&worker.container==='closed'&&brokerState==='closed');
    // A pre-spawn cancel/timeout may still close its admitted broker in finally.
    // Every possibly started actor and every unresolved final broker stays uncertain.
    const noStartStop=(code==='CANCELLED'||code==='TIMEOUT'&&invocationExpiredBeforeSpawn)&&[worker,evaluation].every(x=>x.process==='not_started'&&x.container==='not_started');
    uncertain=processUncertain||unknownRecord||code==='CLOSURE_UNCERTAIN'||brokerState==='uncertain'&&!noStartStop||[worker,evaluation].some(x=>x.process==='may_live'||x.container==='may_live');
    terminal=uncertain?'uncertain':code==='CANCELLED'||deps.signal?.aborted?'cancelled':code==='TIMEOUT'||overallTimedOut?'timeout':'failed';
    if(uncertain||terminal==='cancelled'||terminal==='timeout')stopDeadline();
    exit=uncertain?3:terminal==='cancelled'?130:terminal==='timeout'?124:3;
   }
   finally{
    if(claim){
     if(brokerState==='uncertain')try{
      const receipt=brokerReceipt(await qualified.broker.close(slot.request_id,slotCleanupDeadline??cleanupBound()),c,slot.request_id,slot.limits.model_requests);
      await write(path.join(dir,'receipts',work+'-broker.json'),receipt);
      broker=receipt;
      link.broker_receipt_sha256=fileDigest(receipt);
      brokerState=receipt.status;
     }
     catch{
      /* Retain uncertain reservation. */
     }
     const status=brokerState==='uncertain'||[worker,evaluation].some(x=>x.process==='may_live'||x.container==='may_live')?'uncertain':'closed';
     if(status==='uncertain'){
      uncertain=true;
      terminal='uncertain';
      exit=3;
      writers='may_live';
     }
     const closure={
      schema:'millforge-optimization-closure/v1',campaign_id:c.campaign_id,work_id:work,request_id:slot.request_id,claim_sha256:link.claim_sha256,scope:deps.scope,worker,evaluator: evaluation,broker:brokerState,writers,status
     }
     ;
     await write(path.join(dir,'receipts',work+'-closure.json'),closure);
     link.closure_receipt_sha256=fileDigest(closure);
     if(!isProposal&&result&&broker&&evalOutput&&status==='closed'){
      const ev={
       schema:'millforge-optimization-evaluation/v1',campaign_id:c.campaign_id,work_id:work,request_id:slot.request_id,scope:deps.scope,task_digest:c.task.input_digest,evaluator_digest:c.task.evaluator_digest,deliverable_digest:evalOutput.deliverable_digest,evaluator_output_sha256:evalOutput.sha256,closure_receipt_sha256:link.closure_receipt_sha256,qualification_sha256:c.execution.qualification.sha256,checks:evalOutput.checks,passed:evalOutput.checks.every((x:Obj)=>x.pass),protection:'proven'
      }
      ;
      await write(path.join(dir,'receipts',kind+'-evaluation.json'),ev);
      evidence.evaluations[kind]={
       task_digest:c.task.input_digest,evaluator_digest:c.task.evaluator_digest,deliverable_digest:ev.deliverable_digest,passed:ev.passed,checks:ev.checks,protection:ev.protection,receipt_sha256:fileDigest(ev)
      }
      ;
      evidence.runs[kind]={
       request_id:slot.request_id,profile_digest:result.profile_digest,run_dir:link.run_dir,result_sha256:link.result_sha256,trace_sha256:link.trace_sha256,closure_receipt_sha256:link.closure_receipt_sha256,broker_receipt_sha256:link.broker_receipt_sha256
      }
      ;
      const trace=(await read(path.join(dir,link.run_dir,'trace.jsonl'))).toString().trimEnd().split('\n').filter(Boolean).map(parse);
      observed[kind]={
       kind,profile_digest:result.profile_digest,result,evaluation:ev,broker,trace
      }
      ;
     }
    }
   }
   if(slotFailed)failedWork.add(work);
   if(uncertain||terminal==='cancelled'||terminal==='timeout')break;
  }
  const terminalState=():void=>{
   if(uncertain||processUncertain){uncertain=true;terminal='uncertain';exit=3;stopDeadline();}
   else if(supplied.signal?.aborted||terminal==='cancelled'){terminal='cancelled';exit=130;stopDeadline();}
   else if(overallTimedOut||deps.now()>=deadline||terminal==='timeout'){terminal='timeout';exit=124;stopDeadline();}
  };
  terminalState();
  const inventory:Obj={
   schema:'millforge-optimization-dispatch-inventory/v1',campaign_id:c.campaign_id,campaign_digest:digest,
   admitted_work_ids:map.slots.map((s:Obj)=>s.work_id),claim_digests:claimDigests,terminal_status:terminal
  };
  const timing={schema:'millforge-optimization-timing/v1',campaign_digest:digest,elapsed_ms:Math.max(0,deps.now()-begin),closed:!uncertain};
  await write(path.join(dir,'evidence.json'),evidence);
  const retained:Obj={inventory,timing,artifacts:await artifactInventory(dir,c)};
  const {signal:_cancel,...cleanupDeps}=deps;
  let report:Obj|undefined,assessmentError:unknown;
  const firstAssessmentLive=terminalDeadline===undefined;
  try{
   const bound=terminalDeadline??deadline;
   if(deps.now()>=bound)fail('assessment deadline expired','TIMEOUT');
   report=await assess(loaded,path.join(dir,'evidence.json'),firstAssessmentLive?deps:cleanupDeps,bound,new Set(),undefined,retained);
  }catch(error){assessmentError=error;}
  terminalState();
  if(!report&&firstAssessmentLive&&terminal==='cancelled'&&deps.now()<stopDeadline()){
   try{
    report=await assess(loaded,path.join(dir,'evidence.json'),cleanupDeps,stopDeadline(),new Set(),undefined,retained);
   }catch(error){assessmentError=error;}
  }
  terminalState();
  if(!report){
   const search=await searchLedger(loaded,evidence,cleanupDeps,terminalDeadline??deadline,deps.scope,undefined,dir,{inventory,timing,inspections});
   report={
    schema:'millforge-optimization-report/v1',campaign_id:c.campaign_id,protection_scope:deps.scope,campaign_digest:digest,
    evidence_digest:fileDigest(evidence),baseline_digest:loaded.baseline.digest,
    candidate_digests:Object.fromEntries(Object.entries(observed).filter(([kind])=>kind!=='baseline').map(([kind,o])=>[kind,(o as Obj).profile_digest])),
    observations:{
     runs:Object.fromEntries(['baseline','general','domain','composed'].map(kind=>{
      const o=observed[kind];
      return [kind,o?{status:o.result.status,diagnostic:o.result.diagnostic,complete:false,
       metrics:{model_requests:o.broker.admitted,tool_calls:o.result.counts.tool_calls,elapsed_ms:o.result.timing.elapsed_ms},checks:o.evaluation.checks}:null];
     })),
     edit_assessments:[],reuse_assessment:{scope:'task_specific',candidate_digest:null,source_candidate_digest:null,asset_digests:[],lesson_kind:null,fresh_task_evidence:[],limitations:['Terminal assessment incomplete']}
    },
    recommendation:'inconclusive',reason:'Assessment refused: '+((assessmentError as Error)?.message??'unknown'),compatibility:compatibility(c),
    accounting:{search,ordinary_execution:null,amortization:null}
   };
   if(terminal==='completed'){terminal='failed';exit=3;}
  }
  terminalState();
  if(['cancelled','timeout','uncertain'].includes(terminal)){
   report.recommendation='inconclusive';
   report.reason='Terminal '+terminal+'; retained observations/accounting do not authorize adoption';
  }
  if(report.recommendation==='inconclusive')exit=exit||3;
  // Publish terminal disposition once, after cancellation-sensitive assessment.
  // No further processes or broker calls occur beyond this commit point.
  supplied.signal?.removeEventListener('abort',onAbort);
  inventory.terminal_status=terminal;
  timing.elapsed_ms=Math.max(0,deps.now()-begin);
  timing.closed=!uncertain;
  if(report.accounting.search.elapsed_ms.observed!==null)report.accounting.search.elapsed_ms.observed=uncertain?null:timing.elapsed_ms;
  if(uncertain)report.accounting.search.elapsed_ms.status='uncertain';
  await write(path.join(dir,'receipts/campaign-timing.json'),timing);
  await write(path.join(dir,'dispatch-inventory.json'),inventory);
  await write(path.join(dir,'artifacts.json'),await artifactInventory(dir,c));
  await write(path.join(dir,'report.json'),report);
  return {exit,status:terminal,report};
  }finally{
   supplied.signal?.removeEventListener('abort',onAbort);
   tokens.fill('');
  }
 }
