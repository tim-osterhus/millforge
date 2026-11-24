 /** Deterministic stopped-record comparison, accounting and explicit owner retention. */
 import fs from 'node:fs/promises';
 import path from 'node:path';
 import {
  type Obj,adverse,canonical,closed,codemode,codemodeTarget,copyTree,directory,exact,excluded,fail,fresh,hash,hostGenerate,id,isV2,json,list,member,object,parse,read,relative,requireOptimizerScope,roleTarget,same,schema,sha,text,tree,write
 }
 from './contracts.js';
 import {
  type Dependencies,assertParentHeldResult,assertSlotRecords,baseCommand,profile,brokerReceipt,hostClosed,hostCounts,matchQualification
 }
 from './lifecycle.js';
 import {
  type Loaded,artifactInventory,inspectCampaign,loadCampaign,materialize,resolveRef,taskInput,validateSubject
 }
 from './store.js';
 export const fileDigest=(value:unknown):string=>hash(canonical(value)+'\n');
 export interface Audit {
  root:string;
  refs:Obj[]
 }
 async function localFile(file:string,audit?:Audit):Promise<string>{
  if(!audit)return file;
  const resolved=path.resolve(file);
  if(!resolved.startsWith(audit.root+path.sep))fail('historical path is not local audit authority');
  return resolved;
 }
 async function byRef(kind:string,digest:string,audit:Audit,role?:(file:string)=>boolean):Promise<string>{
  for(const r of audit.refs.filter(r=>r.kind===kind&&(!role||role(r.path)))){
   const file=path.join(audit.root,relative(r.path));
   const bytes=await read(file);
   if(hash(bytes)!==r.sha256)fail('audit reference changed');
   if(r.sha256===digest||kind==='campaign'&&hash(canonical(await json(file)))===digest)return file;
  }
  return fail('missing local audit closure');
 }
 async function auditProfile(digest:string,base:Obj,deps:Dependencies,deadline:number,audit:Audit):Promise<Awaited<ReturnType<typeof profile>>>{
  for(const ref of audit.refs.filter(r=>r.kind==='profile')){
   const dir=path.dirname(await byRef('profile',ref.sha256,audit));
   const loaded=await profile(base,dir,deps,deadline);
   if(loaded.digest===digest)return loaded;
  }
  return fail('missing local subject profile');
 }
 async function resolveRun(root:string,ref:Obj,audit?:Audit):Promise<string>{
  if(audit)return path.dirname(await byRef('result',ref.result_sha256,audit,file=>path.posix.basename(file)==='result.json'));
  return resolveRef(root,ref.run_dir);
 }
 /** V2 only: the inspected saved Result must equal the parent-held original public stdout Result. */
 async function anchoredResult(loaded:Loaded,work:string,result:Obj,audit?:Audit):Promise<void>{
  if(!isV2(loaded.c))return;
  const file=audit?await byRef('result',fileDigest(result),audit,file=>path.posix.basename(file)===work+'-base-result.json'):path.join(loaded.dir,'receipts',work+'-base-result.json');
  let anchor:Obj;
  try{anchor=await json(file);}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')throw new Error('missing public Result anchor');throw e;}
  assertParentHeldResult(anchor,result);
 }
 async function matchSlot(loaded:Loaded,slot:Obj,inspected:Obj):Promise<void>{
  const p=slot.kind==='proposal'?loaded.proposer:loaded.baseline;
  if(!p)fail('missing captured proposer');
  let digest=p.digest;
  if(slot.work_id.endsWith('_probe')){
   // V2 probe identity comes from the parent-owned frozen request.
   if(isV2(loaded.c)){
    const request=await json(path.join(loaded.dir,'requests',slot.work_id+'.json'));
    if(request.schema!==roleTarget(loaded.c,false).request_schema||request.request_id!==slot.request_id||request.profile_id!==p.profile.profile_id||
       request.expected_mode!=='agent'||!same(request.limits,slot.limits))fail('probe request identity mismatch');
    digest=sha(request.profile_digest);
   }
   else digest=inspected.result.profile_digest;
  }
  assertSlotRecords(inspected,loaded.c,slot,digest,p.profile.profile_id);
 }
 async function ledgerInspection(loaded:Loaded,slot:Obj,link:Obj,dir:string,deps:Dependencies,deadline:number,retained?:Obj):Promise<{inspected:Obj;traceBytes:Buffer}>{
  const cached=retained?.inspections?.get(dir);
  let inspected:Obj;
  if(retained?.inspections){
   if(!cached||cached.result_sha256!==link.result_sha256||cached.trace_sha256!==link.trace_sha256)throw new Error('no validated terminal inspection');
   if(codemode(loaded.c)&&(await tree(dir,cached.runBytes)).digest!==cached.runTree)throw new Error('retained run changed after inspection');
   inspected=cached.value;
  }
  else inspected=await baseCommand(loaded.c.base,['inspect','--run',dir],deps,deadline);
  const traceBytes=await read(path.join(dir,'trace.jsonl'));
  // A changed cached run cannot contribute known accounting.
  if(hash(await read(path.join(dir,'result.json')))!==link.result_sha256||hash(traceBytes)!==link.trace_sha256)fail('slot public artifact digest mismatch');
  await matchSlot(loaded,slot,inspected);
  return {inspected,traceBytes};
 }
 async function qualification(loaded:Loaded,audit?:Audit):Promise<Obj>{
  const r=loaded.c.execution?.qualification;
  if(!r)fail('qualification missing');
  const file=audit?await byRef('qualification',r.sha256,audit):path.join(loaded.dir,'receipts/qualification.json');
  const bytes=await read(file);
  if(hash(bytes)!==r.sha256)fail('qualification changed');
  const q=object(parse(bytes));
  // Pure evidence check only: the shared matcher validates captured identities, not protection.
  return matchQualification(q,loaded.c,member(q.scope,['test','deployed']),loaded.proposer?.digest??null,loaded.protectedCodemode);
 }
 export function normalizedBinding(b:Obj):Obj {
  const value=JSON.parse(canonical(b));
  if(value.model_metadata?.source)value.model_metadata.source.profile_digest=null;
  if(value.auth?.lease){
   value.auth.lease.resolved_at=null;
   value.auth.lease.sdk_expires_at=null;
  }
  return value;
 }
 function envelope(run:Obj):Obj {
  const p=run.profile,r=run.request,c=run.capture;
  const {
   instructions:_i,tools:_t,assets:_a,...protectedProfile
  }
  =p;
  const common={
   profile:protectedProfile,task:r.task,context:r.context,limits:c.limits,binding:normalizedBinding(c.binding),runtime_guards:c.runtime_guards,versions:c.versions
  };
  if(c.schema!==codemodeTarget.capture_schema)return common;
  return {...common,capability:c.capability};
 }
 type Comparison={
  verdict:'preferred'|'adverse'|'tie'|'inconclusive';
  metric:string|null
 }
 ;
 function rankObserved(a:Obj,b:Obj):Comparison{
  const av=a.evaluation.checks.filter((x:Obj)=>x.pass).length;
  const bv=b.evaluation.checks.filter((x:Obj)=>x.pass).length;
  if(av!==bv)return {
   verdict:bv>av?'preferred':'adverse',metric:'checks'
  }
  ;
  for(const key of ['model_requests','tool_calls','elapsed_ms']){
   const x=a.metrics[key],y=b.metrics[key];
   if(x===null||y===null)return {
    verdict:'inconclusive',metric:key
   }
   ;
   if(x!==y)return {
    verdict:y<x?'preferred':'adverse',metric:key
   }
   ;
  }
  return {
   verdict:'tie',metric:null
  }
  ;
 }
 export function compare(a:Obj,b:Obj):Comparison{
  if(!a.complete||!b.complete||!same(envelope(a),envelope(b)))return {
   verdict:'inconclusive',metric:null
  }
  ;
  const baseline=new Map(a.evaluation.checks.map((x:Obj)=>[x.check_id,x.pass]));
  if(b.evaluation.checks.some((x:Obj)=>baseline.get(x.check_id)===true&&!x.pass))return {
   verdict:'adverse',metric:'checks'
  }
  ;
  const contrast=rankObserved(a,b);
  // Failed limited runs may demonstrate harm, never successful equality or improvement.
  if(b.result?.status!=='completed'&&contrast.verdict!=='adverse')return {verdict:'inconclusive',metric:contrast.metric};
  return contrast;
 }
 async function observedRun(loaded:Loaded,kind:string,ref:Obj,evaluationRef:Obj,root:string,deps:Dependencies,deadline:number,scope:string,audit?:Audit):Promise<Obj>{
  exact(ref,['request_id','profile_digest','run_dir','result_sha256','trace_sha256','closure_receipt_sha256','broker_receipt_sha256']);
  exact(evaluationRef,['task_digest','evaluator_digest','deliverable_digest','passed','checks','protection','receipt_sha256']);
  const dir=await resolveRun(root,ref,audit);
  if(hash(await read(path.join(dir,'result.json')))!==ref.result_sha256||hash(await read(path.join(dir,'trace.jsonl')))!==ref.trace_sha256)fail('run receipt changed');
  const work=kind==='baseline'?'baseline':kind+'_probe',slot=loaded.map.slots.find((s:Obj)=>s.work_id===work);
  if(!slot||ref.request_id!==slot.request_id)fail('probe slot mismatch');
  const inspected=await baseCommand(loaded.c.base,['inspect','--run',dir],deps,deadline),result=inspected.result,capture=inspected.capture;
  const p=await profile(loaded.c.base,path.join(dir,'profile'),deps,deadline,roleTarget(loaded.c,false).profile_schema);
  const request=await json(path.join(dir,'request.json'));
  await matchSlot(loaded,slot,inspected);
  if(result.profile_digest!==ref.profile_digest||p.digest!==ref.profile_digest)fail('run profile identity mismatch');
  if(codemode(loaded.c)){
   const task=await taskInput(loaded.c,loaded.dir);
   if(request.task!==task.task||!same(request.context,task.context))fail('probe task input mismatch');
   if(kind!=='baseline')validateSubject(loaded,p);
  }
  await anchoredResult(loaded,work,result,audit);
  const closureFile=audit?await byRef('closure',ref.closure_receipt_sha256,audit):path.join(loaded.dir,'receipts',work+'-closure.json'),brokerFile=audit?await byRef('broker',ref.broker_receipt_sha256,audit):path.join(loaded.dir,'receipts',work+'-broker.json');
  if(hash(await read(closureFile))!==ref.closure_receipt_sha256||hash(await read(brokerFile))!==ref.broker_receipt_sha256)fail('closure/broker hash mismatch');
  const closure=await json(closureFile);
  validateClosure(closure,loaded,slot,scope);
  const broker=brokerReceipt(await json(brokerFile),loaded.c,slot.request_id,slot.limits.model_requests);
  if(result.counts.model_requests>broker.admitted)fail('base counts outside broker coverage');
  const evalFile=audit?await byRef('evaluation',evaluationRef.receipt_sha256,audit):path.join(loaded.dir,'receipts',kind+'-evaluation.json');
  if(hash(await read(evalFile))!==evaluationRef.receipt_sha256)fail('evaluation changed');
  const ev=await json(evalFile);
  exact(ev,['schema','campaign_id','work_id','request_id','scope','task_digest','evaluator_digest','deliverable_digest','evaluator_output_sha256','closure_receipt_sha256','qualification_sha256','checks','passed','protection']);
  schema(ev,'millforge-optimization-evaluation/v1');
  if(ev.campaign_id!==loaded.c.campaign_id||ev.work_id!==work||ev.request_id!==slot.request_id||ev.scope!==scope||ev.task_digest!==loaded.c.task.input_digest||ev.evaluator_digest!==loaded.c.task.evaluator_digest||ev.closure_receipt_sha256!==ref.closure_receipt_sha256||ev.qualification_sha256!==loaded.c.execution.qualification.sha256||!same(ev.checks,evaluationRef.checks)||ev.passed!==evaluationRef.passed||ev.deliverable_digest!==evaluationRef.deliverable_digest)fail('evaluation identity mismatch');
  validateChecks(ev.checks);
  if(ev.passed!==ev.checks.every((x:Obj)=>x.pass))fail('evaluation conjunction mismatch');
  const stopped=!codemode(loaded.c)||closure.worker.process==='closed'&&closure.worker.container==='closed'&&
   closure.evaluator.process==='closed'&&closure.evaluator.container==='closed'&&closure.broker==='closed';
  const complete=stopped&&(result.status==='completed'||adverse(result))&&closed(result)&&closure.status==='closed'&&broker.status==='closed'&&ev.protection==='proven'&&evaluationRef.protection==='proven';
  return {
   kind,profile:p.profile,profile_digest:p.digest,request,capture,result,evaluation:ev,broker,closure,complete,adoptable:complete&&result.status==='completed',metrics:{
    model_requests:broker.status==='closed'?broker.admitted:null,tool_calls:closed(result)?result.counts.tool_calls:null,elapsed_ms:closed(result)?result.timing.elapsed_ms:null
   }
   ,refs:{
    result:ref.result_sha256,evaluation:evaluationRef.receipt_sha256
   }
   ,dir
  }
  ;
 }
 export function validateChecks(checks:unknown):void {
  const seen=new Set<string>();
  for(const v of list(checks,256)){
   const c=exact(v,['check_id','pass','detail']);
   id(c.check_id);
   if(seen.has(c.check_id)||typeof c.pass!=='boolean')fail('invalid check');
   seen.add(c.check_id);
   text(c.detail,8192);
  }
  if(!seen.size)fail('empty checks');
 }
 export function validateClosure(v:Obj,loaded:Loaded,slot:Obj,scope:string):void {
  exact(v,['schema','campaign_id','work_id','request_id','claim_sha256','scope','worker','evaluator','broker','writers','status']);
  schema(v,'millforge-optimization-closure/v1');
  if(v.campaign_id!==loaded.c.campaign_id||v.work_id!==slot.work_id||v.request_id!==slot.request_id||v.scope!==scope)fail('closure identity mismatch');
  sha(v.claim_sha256);
  for(const name of ['worker','evaluator']){
   const c=exact(v[name],['pid','pid_start','pgid','container_id','process','container']);
   for(const k of ['process','container'])member(c[k],['closed','may_live','not_started']);
  }
  member(v.broker,['closed','uncertain','not_admitted']);
  member(v.writers,['closed','may_live']);
  member(v.status,['closed','uncertain']);
  if(v.status==='closed'&&(v.writers!=='closed'||v.broker==='uncertain'||['worker','evaluator'].some(n=>[v[n].process,v[n].container].includes('may_live'))))fail('false closure');
 }
 function edits(base:Obj,candidate:Obj):Obj[]{
  const result:Obj[]=[];
  for(const field of ['instructions','tools'])if(!same(base[field],candidate[field]))result.push({
   field:'/'+field
  }
  );
  const a=new Map(base.assets.map((x:Obj)=>[x.path,x.sha256])),b=new Map(candidate.assets.map((x:Obj)=>[x.path,x.sha256]));
  for(const key of [...new Set([...a.keys(),...b.keys()])].sort())if(a.get(key)!==b.get(key))result.push({
   field:'/assets/'+key
  }
  );
  return result;
 }
 function editValue(profile:Obj,field:string):unknown {
  if(field==='/instructions')return profile.instructions;
  if(field==='/tools')return profile.tools;
  return profile.assets.find((asset:Obj)=>asset.path===field.slice('/assets/'.length))?.sha256??null;
 }
 function editAssessments(runs:Obj):Obj[]{
  const result=new Map<string,Obj>(),ordered=['baseline','general','domain','composed'].map(kind=>runs[kind]).filter(Boolean);
  const pairs:{before:Obj;after:Obj;field:string}[]=[];
  for(let i=0;i<ordered.length;i++)for(let j=i+1;j<ordered.length;j++){
   const before=ordered[i]!,after=ordered[j]!,changes=edits(before.profile,after.profile);
   if(changes.length===1)pairs.push({before,after,field:changes[0]!.field});
  }
  for(const kind of ['general','domain','composed']){
   const candidate=runs[kind],base=runs.baseline;
   if(!candidate||!base)continue;
   for(const edit of edits(base.profile,candidate.profile)){
    const from=editValue(base.profile,edit.field),to=editValue(candidate.profile,edit.field);
    const matches=pairs.flatMap(pair=>{
     if(pair.field!==edit.field)return [];
     if(same(editValue(pair.before.profile,edit.field),from)&&same(editValue(pair.after.profile,edit.field),to))return [pair];
     if(same(editValue(pair.after.profile,edit.field),from)&&same(editValue(pair.before.profile,edit.field),to))return [{...pair,before:pair.after,after:pair.before}];
     return [];
    });
    const chosen=matches[0]??{before:base,after:candidate,field:edit.field};
    const contrasts=matches.map(pair=>pair.before.result.status==='completed'?compare(pair.before,pair.after):{verdict:'inconclusive',metric:null});
    const consistent=contrasts.length>0&&contrasts.every(c=>c.verdict!=='inconclusive'&&c.verdict===contrasts[0]!.verdict);
    const verdict=consistent?({preferred:'helped',adverse:'hurt',tie:'no_observed_effect'} as Obj)[contrasts[0]!.verdict]:'confounded';
    const edit_id=hash(canonical([edit.field,chosen.before.profile_digest,chosen.after.profile_digest]));
    // Deterministic general/domain/composed order: the first entry for an edit ID is retained.
    if(result.has(edit_id))continue;
    result.set(edit_id,{
     edit_id,field:edit.field,baseline_digest:chosen.before.profile_digest,candidate_digest:chosen.after.profile_digest,
     evidence_refs:[...new Set((matches.length?matches:[chosen]).flatMap(pair=>[pair.before.refs.result,pair.before.refs.evaluation,pair.after.refs.result,pair.after.refs.evaluation]))],
     verdict,reason:consistent?String(contrasts[0]!.metric??'All observed metrics tie'):'No complete consistent isolating matched pair'
    });
   }
  }
  return [...result.values()];
 }
 function metric(value:number|null,reserved:number):Obj{
  return {
   observed:value,reserved:value===null?reserved:0,status:value===null?'uncertain':'observed'
  }
  ;
 }
 function tokens(result:Obj,receipt:string,route:string):Obj[]|null {
  if(result.counts.usage===null)return null;
  return ['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens'].map(unit=>({
   value:result.counts.usage[unit]??null,unit,source:'reported',source_receipt:receipt,route_digest:route
  }
  ));
 }
 function cost(result:Obj,receipt:string,route:string):Obj[]|null {
  const c=result.counts.cost;
  return c===null?null:[{
   value:c.amount,unit:c.currency,source:c.source,source_receipt:receipt,route_digest:route
  }
  ];
 }
 export async function searchLedger(loaded:Loaded,evidence:Obj,deps:Dependencies,deadline:number,scope:string,audit?:Audit,referenceRoot=loaded.dir,retained?:Obj):Promise<Obj>{
  const known={
   model_requests:0,tool_calls:0
  }
  ,reserved={
   model_requests:0,tool_calls:0,elapsed_ms:0
  }
  ;
  let uncertain=false;
  const provenance:Obj[]=[],tokenEntries:Obj[]=[],costEntries:Obj[]=[];
  let allTokens=true,allCost=true;
  const state=retained?.inventory?{inventory:retained.inventory}:await inspectCampaign(loaded);
  if(retained?.inventory){
   const inventory=exact(retained.inventory,['schema','campaign_id','campaign_digest','admitted_work_ids','claim_digests','terminal_status']);
   schema(inventory,'millforge-optimization-dispatch-inventory/v1');
   if(inventory.campaign_digest!==loaded.digest||!same(inventory.admitted_work_ids,loaded.map.slots.map((s:Obj)=>s.work_id)))fail('inventory identity');
   const names=await fs.readdir(path.join(loaded.dir,'claims')).catch(error=>{if(error.code==='ENOENT')return [];throw error;});
   if(!same(names.sort(),Object.keys(inventory.claim_digests).map(work=>work+'.json').sort()))fail('inventory missing claim');
   for(const [work,digest]of Object.entries(inventory.claim_digests))if(!loaded.map.slots.some((s:Obj)=>s.work_id===work)||hash(await read(path.join(loaded.dir,'claims',work+'.json')))!==digest)fail('inventory claim mismatch');
  }
  for(const slot of loaded.map.slots){
   const link=evidence.claims[slot.work_id];
   if(!link||link.claim_sha256===null){
    if(!state.inventory||state.inventory.claim_digests[slot.work_id])uncertain=true;
    continue;
   }
   const file=audit?await byRef('claim',link.claim_sha256,audit):path.join(loaded.dir,'claims',slot.work_id+'.json');
   if(hash(await read(file))!==link.claim_sha256)fail('claim hash mismatch');
   const claim=await json(file);
   exact(claim,['schema','campaign_id','campaign_digest','work_id','request_id','reserved_model_requests','reserved_tool_calls','reserved_elapsed_ms','timestamp','execution_root']);
   schema(claim,'millforge-optimization-claim/v1');
   if(claim.campaign_digest!==loaded.digest||claim.work_id!==slot.work_id||claim.request_id!==slot.request_id||claim.reserved_model_requests!==slot.limits.model_requests||claim.reserved_tool_calls!==slot.limits.tool_calls||claim.reserved_elapsed_ms!==(slot.limits.elapsed_seconds+slot.evaluation_seconds)*1000)fail('claim reservation mismatch');
   let valid=false;
   try{
    if(hostGenerate(loaded.c)&&slot.kind==='proposal'){
     // Host teacher: base-observed client HTTP admissions; no broker claim or receipt exists.
     if(link.broker_receipt_sha256!==null)fail('host slot has no broker receipt');
     if(!link.result_sha256||!link.closure_receipt_sha256)throw new Error('missing');
     const dir=await resolveRun(referenceRoot,link,audit);
     const {inspected,traceBytes}=await ledgerInspection(loaded,slot,link,dir,deps,deadline,retained);
     await anchoredResult(loaded,slot.work_id,inspected.result,audit);
     const closureFile=audit?await byRef('closure',link.closure_receipt_sha256,audit):path.join(loaded.dir,'receipts',slot.work_id+'-closure.json');
     if(hash(await read(closureFile))!==link.closure_receipt_sha256)fail('closure changed');
     const closure=await json(closureFile);
     validateClosure(closure,loaded,slot,scope);
     if(closure.claim_sha256!==link.claim_sha256||closure.status!=='closed'||closure.broker!=='not_admitted'||closure.worker.process!=='closed'||closure.worker.container!=='not_started'||closure.evaluator.process!=='not_started'||closure.evaluator.container!=='not_started'||!hostClosed(inspected.result))throw new Error('uncertain');
     const counted=hostCounts(traceBytes.toString('utf8'),inspected.result);
     if(!counted)throw new Error('uncertain');
     const p=loaded.proposer!.profile,route=hash(canonical([p.runtime,p.model,p.sampling,p.reasoning??null,p.cache??null,normalizedBinding(inspected.capture.binding)]));
     known.model_requests+=counted.http_attempts;
     provenance.push({
      work_id:slot.work_id,accounting_source:'base_generate',route_digest:route,claim_sha256:link.claim_sha256,model_requests:counted.http_attempts,model_starts:counted.model_starts,tool_calls:0,result_sha256:link.result_sha256,trace_sha256:link.trace_sha256,...(retained?.inspections?{inspection:'validated_run_local',fresh_inspection:false}:{})
     }
     );
     const ts=tokens(inspected.result,link.result_sha256,route),cs=cost(inspected.result,link.result_sha256,route);
     if(ts===null)allTokens=false;
     else tokenEntries.push(...ts);
     if(cs===null)allCost=false;
     else costEntries.push(...cs);
     valid=true;
    }
    else{
    if(!link.result_sha256||!link.broker_receipt_sha256||!link.closure_receipt_sha256)throw new Error('missing');
    const bfile=audit?await byRef('broker',link.broker_receipt_sha256,audit):path.join(loaded.dir,'receipts',slot.work_id+'-broker.json');
    if(hash(await read(bfile))!==link.broker_receipt_sha256)fail('broker receipt changed');
    const broker=brokerReceipt(await json(bfile),loaded.c,slot.request_id,slot.limits.model_requests),dir=await resolveRun(referenceRoot,link,audit);
    // Terminal fallback uses only this run's already validated, identity-bound reads.
    const {inspected}=await ledgerInspection(loaded,slot,link,dir,deps,deadline,retained);
    if(inspected.result.counts.model_requests>broker.admitted)fail('base counts outside broker coverage');
    await anchoredResult(loaded,slot.work_id,inspected.result,audit);
    const closureFile=audit?await byRef('closure',link.closure_receipt_sha256,audit):path.join(loaded.dir,'receipts',slot.work_id+'-closure.json');
    if(hash(await read(closureFile))!==link.closure_receipt_sha256)fail('closure changed');
    const closure=await json(closureFile);
    validateClosure(closure,loaded,slot,scope);
    if(closure.claim_sha256!==link.claim_sha256||closure.status!=='closed'||broker.status!=='closed'||!closed(inspected.result))throw new Error('uncertain');
    if(codemode(loaded.c)&&(closure.worker.process!=='closed'||closure.worker.container!=='closed'||closure.broker!=='closed'))throw new Error('uncertain');
    known.model_requests+=broker.admitted;
    known.tool_calls+=inspected.result.counts.tool_calls;
    provenance.push({
     work_id:slot.work_id,...(isV2(loaded.c)?{accounting_source:'broker'}:{}),route_digest:broker.route_digest,claim_sha256:link.claim_sha256,model_requests:broker.admitted,tool_calls:inspected.result.counts.tool_calls,...(retained?.inspections?{inspection:'validated_run_local',fresh_inspection:false}:{})
    }
    );
    const completeBase=broker.admitted===inspected.result.counts.model_requests,baseTokens=tokens(inspected.result,link.result_sha256,broker.route_digest),baseCost=cost(inspected.result,link.result_sha256,broker.route_digest),ts=broker.usage??(completeBase?baseTokens:null),cs=broker.cost??(completeBase?baseCost:null);
    provenance.push({
     work_id:slot.work_id,known_base_tokens:baseTokens,known_base_cost:baseCost,base_covers_broker:completeBase,selected_tokens:ts,selected_cost:cs
    }
    );
    if(ts===null)allTokens=false;
    else tokenEntries.push(...ts);
    if(cs===null)allCost=false;
    else costEntries.push(...cs);
    valid=true;
    }
   }
   catch(e){
    if(e instanceof Error&&['INVALID_INPUT','UNSUPPORTED_VERSION'].includes((e as Obj).code))throw e;
   }
   if(!valid){
    uncertain=true;
    reserved.model_requests+=slot.limits.model_requests;
    reserved.tool_calls+=slot.limits.tool_calls;
    reserved.elapsed_ms+=(slot.limits.elapsed_seconds+slot.evaluation_seconds)*1000;
    allTokens=false;
    allCost=false;
    provenance.push({
     work_id:slot.work_id,status:'uncertain',claim_sha256:link.claim_sha256
    }
    );
   }
  }
  const run=await json(path.join(loaded.dir,'run.json'));
  exact(run,['schema','campaign_id','campaign_digest','execution_root','authority_sha256','started_at','elapsed_seconds','optimizer']);
  schema(run,'millforge-optimization-run/v1');
  const authority=await read(path.join(loaded.dir,'receipts/authority.json'));
  if(hash(authority)!==run.authority_sha256)fail('captured authority changed');
  const approval=parseAuthority(authority);
  if(run.campaign_digest!==loaded.digest||run.campaign_id!==loaded.c.campaign_id||approval.campaign_digest!==loaded.digest||approval.execution_root!==run.execution_root||approval.owner_id!==loaded.c.owner_id)fail('run authority identity mismatch');
  let wall:number|null=null;
  const timingFile=path.join(loaded.dir,'receipts/campaign-timing.json');
  try{
   const timing=retained?.timing??await json(timingFile);
   exact(timing,['schema','campaign_digest','elapsed_ms','closed']);
   schema(timing,'millforge-optimization-timing/v1');
   if(timing.campaign_digest!==loaded.digest||typeof timing.elapsed_ms!=='number'||timing.elapsed_ms<0||!Number.isFinite(timing.elapsed_ms))fail('timing identity');
   if(timing.closed===true&&!uncertain)wall=timing.elapsed_ms;
  }
  catch(e){
   if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;
  }
  return {
   model_requests:metric(uncertain?null:known.model_requests,reserved.model_requests),tool_calls:metric(uncertain?null:known.tool_calls,reserved.tool_calls),elapsed_ms:metric(wall,reserved.elapsed_ms),tokens:allTokens?tokenEntries:null,cost:allCost?costEntries:null,provenance:[...provenance,{
    known_subtotals:known,started_at:run.started_at
   }
   ]
  }
  ;
 }
 async function ordinary(loaded:Loaded,evidence:Obj,subject:string|null,deps:Dependencies,deadline:number,referenceRoot=loaded.dir,audit?:Audit):Promise<Obj|null>{
  if(evidence.ordinary_execution===null)return null;
  const input=exact(evidence.ordinary_execution,['baseline','candidate']);
  const runs:Obj[]=[];
  for(const side of ['baseline','candidate']){
   const ref=exact(input[side],['run_dir','capture_sha256','result_sha256','trace_sha256']);
   const dir=await localFile(await resolveRun(referenceRoot,ref,audit),audit);
   for(const [key,file]of [['capture_sha256','capture.json'],['result_sha256','result.json'],['trace_sha256','trace.jsonl']])if(hash(await read(path.join(dir,file!)))!==ref[key!])fail('ordinary receipt mismatch');
   const inspected=await baseCommand(loaded.c.base,['inspect','--run',dir],deps,deadline);
   const p=await profile(loaded.c.base,path.join(dir,'profile'),deps,deadline,roleTarget(loaded.c,false).profile_schema);
   const request=await json(path.join(dir,'request.json'));
   if(p.digest!==(side==='baseline'?loaded.baseline.digest:subject)||!closed(inspected.result))fail('ordinary profile/closure mismatch');
   assertSlotRecords(inspected,loaded.c,{...loaded.map.slots[0],request_id:request.request_id},p.digest,p.profile.profile_id);
   runs.push({
    ...inspected,profile:p.profile,request,profile_digest:p.digest,receipt:ref.result_sha256
   }
   );
  }
  if(!same(envelope(runs[0]!),envelope(runs[1]!)))fail('ordinary execution envelopes differ');
  const task=await taskInput(loaded.c,loaded.dir);
  if(runs.some(r=>r.request.task!==task.task||!same(r.request.context,task.context)||!same(r.capture.limits,loaded.map.slots[0].limits)))fail('ordinary execution does not match evaluated task/limits');
  const ledgers:Obj={
  }
  ;
  for(let i=0;i<2;i++){
   const r=runs[i]!,route=hash(canonical([r.profile.runtime,r.profile.model,r.profile.sampling,r.profile.reasoning??null,r.profile.cache??null,normalizedBinding(r.capture.binding)]));
   ledgers[i===0?'baseline':'candidate']={
    model_requests:metric(r.result.counts.model_requests,0),tool_calls:metric(r.result.counts.tool_calls,0),elapsed_ms:metric(r.result.timing.elapsed_ms,0),tokens:tokens(r.result,r.receipt,route),cost:cost(r.result,r.receipt,route),provenance:[{
     source_receipt:r.receipt,route_digest:route
    }
    ]
   }
   ;
  }
  return ledgers;
 }
 export async function assess(loaded:Loaded,evidenceFile:string,deps:Dependencies,deadline:number,seen=new Set<string>(),audit?:Audit,retained?:Obj):Promise<Obj>{
  const inventory=retained?.artifacts??await json(path.join(loaded.dir,'artifacts.json'));
  exact(inventory,['schema','campaign_id','campaign_digest','files']);
  schema(inventory,'millforge-optimization-artifacts/v1');
  if(inventory.campaign_digest!==loaded.digest)fail('artifact inventory mismatch');
  for(const item of inventory.files){
   exact(item,['kind','path','sha256']);
   relative(item.path);
   if(hash(await read(path.join(loaded.dir,item.path)))!==item.sha256)fail('retained artifact changed');
  }
  const evidence=await json(evidenceFile);
  evidencePathShape(evidence,loaded);
  requireOptimizerScope(loaded.c,evidence.protection_scope,loaded.protectedCodemode);
  if(evidence.campaign_id!==loaded.c.campaign_id||!same(evidence.bindings,loaded.c.bindings))fail('evidence identity mismatch');
  const key=loaded.digest+':'+fileDigest(evidence);
  if(seen.has(key))fail('circular assessment');
  seen.add(key);
  try{
   const q=await qualification(loaded,audit);
   if(evidence.protection_scope!==q.scope)fail('protection scope mismatch');
   exact(evidence.runs,['baseline','general','domain','composed']);
   exact(evidence.evaluations,['baseline','general','domain','composed']);
   exact(evidence.claims,loaded.map.slots.map((s:Obj)=>s.work_id));
   const runs:Obj={
    baseline:null,general:null,domain:null,composed:null
   }
   ;
   for(const kind of Object.keys(runs)){
    if(evidence.runs[kind]===null){
     if(evidence.evaluations[kind]!==null)fail('orphan evaluation');
     continue;
    }
    if(evidence.evaluations[kind]===null)continue;
    runs[kind]=await observedRun(loaded,kind,evidence.runs[kind],evidence.evaluations[kind],path.dirname(evidenceFile),deps,deadline,q.scope,audit);
    const work=kind==='baseline'?'baseline':kind+'_probe',link=evidence.claims[work];
    for(const field of ['run_dir','result_sha256','trace_sha256','closure_receipt_sha256','broker_receipt_sha256'])if(link[field]!==evidence.runs[kind][field])fail('executor evidence/claim links differ');
   }
   let recommendation='inconclusive',reason='Incomplete or unmatched protected observations';
   const baseline=runs.baseline;
   if(loaded.c.purpose==='baseline_validation'){
    if(['general','domain','composed'].some(k=>runs[k]!==null))fail('validation has candidate');
    if(baseline?.complete){
     recommendation='baseline';
     reason='Complete validation-only observation';
    }
   }
   else if(baseline?.complete&&baseline.result.status==='completed'){
    const complete=['general','domain','composed'].every(k=>runs[k]?.complete);
    if(complete){
     recommendation='baseline';
     reason='No observed improvement';
     let best:Obj|null=null;
     for(const kind of ['general','domain','composed']){
      const r=runs[kind];
      if(!r.adoptable)continue;
      const eligibility=compare(baseline,r);
      if(eligibility.verdict==='inconclusive'){
       recommendation='inconclusive';
       reason='Unknown required comparison';
       break;
      }
      if(eligibility.verdict!=='preferred')continue;
      const contrast=best?rankObserved(best,r):{
       verdict:'preferred',metric:null
      }
      ;
      if(contrast.verdict==='inconclusive'){
       recommendation='inconclusive';
       reason='Unknown candidate ranking';
       break;
      }
      if(contrast.verdict==='preferred'){
       recommendation=kind;
       reason='Preferred matched checks/request/tool/time contrast';
       best=r;
      }
     }
    }
   }
   const candidate_digests:Obj={
   }
   ;
   for(const kind of ['general','domain','composed'])if(runs[kind])candidate_digests[kind]=runs[kind].profile_digest;
   let reuse={
    scope:'task_specific',candidate_digest:null,source_candidate_digest:null,asset_digests:[],lesson_kind:null,fresh_task_evidence:[],limitations:['Single-trial timing; operator task selection and contamination remain limitations']
   } as Obj;
   let validationReports:Obj[]=[];
   if(evidence.reuse_evidence!==null&&!(Array.isArray(evidence.reuse_evidence)&&evidence.reuse_evidence.length===0)){
    const result=await reuseAssessment(loaded,evidence.reuse_evidence,runs,recommendation,deps,deadline,seen,audit,path.dirname(evidenceFile));
    reuse=result.reuse;
    validationReports=result.reports;
   }
   const search=await searchLedger(loaded,evidence,deps,deadline,q.scope,audit,path.dirname(evidenceFile),retained);
   // Model and tool totals share the same stopped-work coverage check.
   if(codemode(loaded.c)&&search.model_requests.status==='uncertain'){
    recommendation='inconclusive';
    reason='Incomplete stopped work accounting';
   }
   const ordinary_execution=await ordinary(loaded,evidence,reuse.candidate_digest??candidate_digests[recommendation]??null,deps,deadline,path.dirname(evidenceFile),audit);
   let amortization:Obj|null=null;
   if(ordinary_execution){
    const metricName='model_requests',baselineCost=ordinary_execution.baseline[metricName].observed,candidateCost=ordinary_execution.candidate[metricName].observed;
    const expenses=[{
     campaign_digest:loaded.digest,report:null,expense:search[metricName].observed
    }
    ,...validationReports.map(r=>({
     campaign_digest:r.campaign_digest,report:fileDigest(r),expense:r.accounting.search[metricName].observed
    }
    ))];
    const unique=[...new Map(expenses.map(r=>[r.campaign_digest,r])).values()];
    const expense=unique.every(r=>r.expense!==null)?unique.reduce((n,r)=>n+r.expense,0):null,saving=baselineCost!==null&&candidateCost!==null?baselineCost-candidateCost:null;
    amortization={
     metric:{
      name:metricName,unit:'request'
     }
     ,search_expense:expense,per_run_saving:saving,break_even_runs:expense!==null&&saving!==null&&saving>0?Math.ceil(expense/saving):null,receipt_refs:unique,limitations:['Observed request arithmetic only; no billing forecast']
    }
    ;
   }
   return {
    schema:'millforge-optimization-report/v1',campaign_id:loaded.c.campaign_id,protection_scope:q.scope,campaign_digest:loaded.digest,evidence_digest:fileDigest(evidence),baseline_digest:loaded.baseline.digest,candidate_digests,observations:{
     runs:Object.fromEntries(Object.entries(runs).map(([kind,r])=>[kind,r?{
      status:(r as Obj).result.status,diagnostic:(r as Obj).result.diagnostic,complete:(r as Obj).complete,metrics:(r as Obj).metrics,checks:(r as Obj).evaluation.checks
     }
     :null])),edit_assessments:editAssessments(runs),reuse_assessment:reuse
    }
    ,recommendation,reason,compatibility:compatibility(loaded.c),accounting:{
     search,ordinary_execution,amortization
    }
   }
   ;
  }
  finally{
   seen.delete(key);
  }
 }
 function parseAuthority(bytes:Buffer):Obj{
  const a=parse(bytes);
  exact(a,['schema','owner_id','campaign_id','campaign_digest','execution_root','budget','expires_at']);
  schema(a,'millforge-optimization-authority/v1');
  return a;
 }
 export function compatibility(c:Obj):Obj{
  return {
   target:c.target,base_tree_sha256:c.base.tree_sha256,baseline_digest:c.baseline.profile_digest,allowed_tools:c.allowed_tools,assets:c.assets
  }
  ;
 }
 async function importCampaign(ref:Obj,baseBin:string,deps:Dependencies,deadline:number,seen:Set<string>,audit?:Audit):Promise<{
  loaded:Loaded;
  report:Obj
 }
 >{
  exact(ref,['campaign_dir','campaign_digest','report_digest']);
  const dir=audit?path.dirname(await byRef('campaign',ref.campaign_digest,audit)):ref.campaign_dir;
  const loaded=await loadCampaign(dir,baseBin,deps,deadline);
  if(loaded.digest!==ref.campaign_digest)fail('imported campaign mismatch');
  const reportFile=audit?await byRef('report',ref.report_digest,audit):path.join(dir,'report.json');
  const existing=await json(reportFile);
  if(fileDigest(existing)!==ref.report_digest)fail('imported report changed');
  const evidenceFile=audit?await byRef('evidence',existing.evidence_digest,audit):path.join(dir,'evidence.json'),report=await assess(loaded,evidenceFile,deps,deadline,seen,audit);
  if(!same(existing,report))fail('stale imported report');
  return {
   loaded,report
  }
  ;
 }
 async function reuseAssessment(source:Loaded,value:Obj,runs:Obj,recommendation:string,deps:Dependencies,deadline:number,seen:Set<string>,audit?:Audit,referenceRoot=source.dir):Promise<{
  reuse:Obj;
  reports:Obj[]
 }
 >{
  const r=exact(value,['candidate_dir','candidate_digest','source_candidate_digest','asset_digests','lesson_kind','source_refs','fresh_task_evidence']);
  member(r.lesson_kind,['positive','negative']);
  if(source.c.purpose!=='search'||recommendation==='inconclusive')fail('incomplete source cannot promote');
  const original=Object.values(runs).find((v:any)=>v?.profile_digest===r.source_candidate_digest) as Obj|undefined;
  if(!original||original.kind==='baseline'||!original.complete)fail('source candidate contrast missing');
  const sourceContrast=compare(runs.baseline,original);
  if(sourceContrast.verdict!==(r.lesson_kind==='positive'?'preferred':'adverse')||sourceContrast.metric==='elapsed_ms')fail('unsupported source lesson');
  const subject=audit?await auditProfile(r.candidate_digest,source.c.base,deps,deadline,audit):await profile(source.c.base,resolveRef(referenceRoot,r.candidate_dir),deps,deadline);
  if(subject.digest!==r.candidate_digest||!same(subject.profile.assets,r.asset_digests))fail('sanitized subject changed');
  validateSubject(source,subject);
  const refs=list(r.source_refs,4096);
  for(const required of ['campaign','evidence','report'])if(!refs.some((v:Obj)=>v.kind===required))fail('source provenance missing');
  for(const ref of refs){
   exact(ref,['kind','path','sha256']);
   const f=audit?await byRef(ref.kind,ref.sha256,audit):resolveRef(referenceRoot,ref.path);
   if(hash(await read(f))!==ref.sha256)fail('source reference changed');
  }
  const ownCampaign=refs.find((x:Obj)=>x.kind==='campaign');
  const ownCampaignFile=audit?await byRef('campaign',ownCampaign.sha256,audit):resolveRef(referenceRoot,ownCampaign.path);
  if(hash(canonical(await json(ownCampaignFile)))!==source.digest)fail('source campaign provenance mismatch');
  const oldEvidenceRef=refs.find((x:Obj)=>x.kind==='evidence'),oldReportRef=refs.find((x:Obj)=>x.kind==='report');
  const oldEvidenceFile=audit?await byRef('evidence',oldEvidenceRef.sha256,audit):resolveRef(referenceRoot,oldEvidenceRef.path),oldReportFile=audit?await byRef('report',oldReportRef.sha256,audit):resolveRef(referenceRoot,oldReportRef.path);
  const oldReport=await json(oldReportFile),oldRecomputed=await assess(source,oldEvidenceFile,deps,deadline,seen,audit);
  if(!same(oldReport,oldRecomputed)||!Object.values(oldReport.candidate_digests).includes(r.source_candidate_digest))fail('source report provenance mismatch');
  const fresh:Obj[]=[],reports:Obj[]=[];
  const taskIds=new Set<string>();
  for(const pair of list(r.fresh_task_evidence,16)){
   exact(pair,['common','candidate']);
   const a=await importCampaign({
    ...pair.common,campaign_dir:resolveRef(referenceRoot,pair.common.campaign_dir)
   }
   ,source.c.base.cli_path,deps,deadline,seen,audit),b=await importCampaign({
    ...pair.candidate,campaign_dir:resolveRef(referenceRoot,pair.candidate.campaign_dir)
   }
   ,source.c.base.cli_path,deps,deadline,seen,audit);
   if(a.loaded.c.schema!==source.c.schema||b.loaded.c.schema!==source.c.schema)fail('mixed campaign versions');
   if(a.loaded.c.purpose!=='baseline_validation'||b.loaded.c.purpose!=='baseline_validation'||a.loaded.baseline.digest!==source.baseline.digest||b.loaded.baseline.digest!==subject.digest||a.loaded.c.task.id===source.c.task.id||a.loaded.c.task.input_digest===source.c.task.input_digest||!same(a.loaded.c.task.id,b.loaded.c.task.id)||a.loaded.c.task.input_digest!==b.loaded.c.task.input_digest||a.loaded.c.task.evaluator_digest!==b.loaded.c.task.evaluator_digest||taskIds.has(a.loaded.c.task.id)||a.report.protection_scope!==b.report.protection_scope)fail('fresh pair identity mismatch');
   taskIds.add(a.loaded.c.task.id);
   if(a.report.protection_scope!==(await qualification(source,audit)).scope)fail('mixed protection scopes');
   if(a.report.recommendation!=='baseline'||b.report.recommendation!=='baseline')fail('incomplete fresh pair');
   excluded(subject.profile,subject.assets,b.loaded.c.validation_exclusions);
   const ea=await json(path.join(a.loaded.dir,'evidence.json')),eb=await json(path.join(b.loaded.dir,'evidence.json'));
   const ra=await observedRun(a.loaded,'baseline',ea.runs.baseline,ea.evaluations.baseline,a.loaded.dir,deps,deadline,a.report.protection_scope,audit),rb=await observedRun(b.loaded,'baseline',eb.runs.baseline,eb.evaluations.baseline,b.loaded.dir,deps,deadline,b.report.protection_scope,audit);
   if(ra.result.status!=='completed')fail('common baseline not completed');
   const comparison=compare(ra,rb);
   if(comparison.verdict!==(r.lesson_kind==='positive'?'preferred':'adverse')||comparison.metric==='elapsed_ms')fail('fresh contrast cannot promote');
   fresh.push({
    common:{
     campaign_digest:a.loaded.digest,report_digest:fileDigest(a.report)
    }
    ,candidate:{
     campaign_digest:b.loaded.digest,report_digest:fileDigest(b.report)
    }
    ,verdict:comparison.verdict,deciding_metric:comparison.metric,validation_exclusions:b.loaded.c.validation_exclusions,task_id:a.loaded.c.task.id
   }
   );
   reports.push(a.report,b.report);
  }
  if(!fresh.length)fail('fresh tasks required');
  return {
   reuse:{
    scope:'cross_task',candidate_digest:subject.digest,source_candidate_digest:r.source_candidate_digest,asset_digests:r.asset_digests,lesson_kind:r.lesson_kind,fresh_task_evidence:fresh,limitations:['Literal sanitization only; single-trial timing and operator task-selection/contamination limitations','An unchanged general checker may be reused; no independently fresh rubric is claimed']
   }
   ,reports
  }
  ;
 }
 const referenceKinds=['campaign','evidence','report','adoption','claim','authority','run','work_map','artifact_inventory','capture','request','result','trace','closure','broker','qualification','evaluation','dispatch_inventory','timing','profile','asset'];
 export async function validateLibrary(dir:string,c:Obj,deps:Dependencies,deadline:number,seen:Set<string>):Promise<void>{
  if(codemode(c))fail('Codemode campaigns do not admit a library');
  const entries=(await fs.readdir(dir)).sort();
  if(entries.length>16)fail('library entry count');
  for(const name of entries){
   id(name);
   const root=path.join(dir,name),entry=await json(path.join(root,'entry.json'));
   const scope=await validateEntry(entry,root,c,deps,deadline,seen);
   if(scope!==deps.scope)fail('library protection scope mismatch');
  }
 }
 async function validateEntry(entry:Obj,root:string,c:Obj,deps:Dependencies,deadline:number,seen:Set<string>):Promise<string>{
  exact(entry,['schema','entry_id','cue','lesson','lesson_kind','profile_digest','source_candidate_digest','asset_digests','worker_assets','compatibility','source_refs','fresh_task_evidence','excluded_task_facts']);
  schema(entry,'millforge-optimization-library-entry/v1');
  id(entry.entry_id);
  text(entry.cue,8192);
  text(entry.lesson,8192);
  member(entry.lesson_kind,['positive','negative']);
  if(!same(entry.compatibility,compatibility(c)))fail('library incompatible');
  const info=await tree(root);
  if(info.bytes>67108864)fail('entry too large');
  const refs=list(entry.source_refs,4096),seenPaths=new Set<string>();
  for(const r of refs){
   exact(r,['kind','path','sha256']);
   member(r.kind,referenceKinds);
   relative(r.path);
   sha(r.sha256);
   if(seenPaths.has(r.path)||r.path==='entry.json')fail('duplicate/circular entry reference');
   seenPaths.add(r.path);
   if(hash(await read(path.join(root,r.path)))!==r.sha256)fail('entry audit changed');
  }
  const subjectRoot=entry.lesson_kind==='negative'?path.join(root,'audit-subject'):root;
  const p=await profile(c.base,subjectRoot,deps,deadline);
  if(p.digest!==entry.profile_digest||!same(p.profile.assets,entry.asset_digests))fail('entry profile mismatch');
  const prefix=entry.lesson_kind==='negative'?'audit-subject/':'';
  const allowed=new Set(['entry.json','adoption.json',prefix+'profile.json',...p.profile.assets.map((a:Obj)=>prefix+a.path),...seenPaths]);
  if(info.files.some(f=>!allowed.has(f.path)))fail('undeclared entry file');
  for(const a of list(entry.worker_assets,128)){
   exact(a,['path','sha256']);
   if(!p.profile.assets.some((v:Obj)=>v.path===a.path&&v.sha256===a.sha256))fail('audit asset exposed to worker');
  }
  for(const fact of list(entry.excluded_task_facts,256))text(fact,8192,false);
  excluded(p.profile,p.assets,entry.excluded_task_facts);
  for(const fact of entry.excluded_task_facts)if(entry.cue.includes(fact)||entry.lesson.includes(fact))fail('lesson contains excluded fact');
  const audit={
   root,refs
  }
  ;
  let accepted=false,entryScope:string|null=null;
  for(const ref of refs.filter((r:Obj)=>r.kind==='report')){
   const saved=await json(path.join(root,ref.path));
   if(saved.observations?.reuse_assessment?.candidate_digest!==entry.profile_digest)continue;
   const campaignFile=await byRef('campaign',saved.campaign_digest,audit),loaded=await loadCampaign(path.dirname(campaignFile),c.base.cli_path,deps,deadline);
   if(loaded.c.schema!==c.schema)fail('mixed campaign versions');
   const evidenceFile=await byRef('evidence',saved.evidence_digest,audit),recomputed=await assess(loaded,evidenceFile,deps,deadline,seen,audit);
   if(!same(saved,recomputed))fail('entry source report stale');
   if(entryScope!==null&&entryScope!==recomputed.protection_scope)fail('mixed entry protection scopes');
   entryScope=recomputed.protection_scope;
   const reuse=saved.observations.reuse_assessment;
   if(reuse.scope!=='cross_task'||reuse.source_candidate_digest!==entry.source_candidate_digest||reuse.lesson_kind!==entry.lesson_kind||!same(reuse.asset_digests,entry.asset_digests)||!same(reuse.fresh_task_evidence,entry.fresh_task_evidence)||reuse.fresh_task_evidence.some((x:Obj)=>!same(x.validation_exclusions,entry.excluded_task_facts)))fail('entry promotion provenance differs');
   accepted=true;
  }
  if(!accepted||entryScope===null)fail('entry lacks recomputable promotion evidence');
  return entryScope;
 }
 function evidencePathShape(evidence:Obj,loaded:Loaded):void {
  exact(evidence,['schema','campaign_id','protection_scope','bindings','runs','evaluations','claims','reuse_evidence','ordinary_execution']);
  schema(evidence,'millforge-optimization-evidence/v1');
  const kinds=['baseline','general','domain','composed'];
  exact(evidence.runs,kinds);exact(evidence.evaluations,kinds);
  exact(evidence.claims,loaded.map.slots.map((slot:Obj)=>slot.work_id));
  for(const ref of Object.values(evidence.runs))if(ref!==null){
   const r=exact(ref,['request_id','profile_digest','run_dir','result_sha256','trace_sha256','closure_receipt_sha256','broker_receipt_sha256']);
   text(r.run_dir,4096,false);
  }
  for(const ref of Object.values(evidence.claims)){
   const r=exact(ref,['claim_sha256','run_dir','result_sha256','trace_sha256','closure_receipt_sha256','broker_receipt_sha256']);
   if(r.run_dir!==null)text(r.run_dir,4096,false);
  }
  const reuse=evidence.reuse_evidence;
  if(codemode(loaded.c)&&reuse!==null&&!(Array.isArray(reuse)&&reuse.length===0))fail('Codemode campaigns require empty reuse evidence');
  if(Array.isArray(reuse)){if(reuse.length!==0)fail('reuse evidence must be empty or one subject');}
  else if(reuse!==null){
   exact(reuse,['candidate_dir','candidate_digest','source_candidate_digest','asset_digests','lesson_kind','source_refs','fresh_task_evidence']);
   text(reuse.candidate_dir,4096,false);
   for(const value of list(reuse.source_refs,4096))text(exact(value,['kind','path','sha256']).path,4096,false);
   for(const value of list(reuse.fresh_task_evidence,16)){
    const pair=exact(value,['common','candidate']);
    for(const side of ['common','candidate'])text(exact(pair[side],['campaign_dir','campaign_digest','report_digest']).campaign_dir,4096,false);
   }
  }
  if(evidence.ordinary_execution!==null){
   const ordinary=exact(evidence.ordinary_execution,['baseline','candidate']);
   for(const side of ['baseline','candidate'])text(exact(ordinary[side],['run_dir','capture_sha256','result_sha256','trace_sha256']).run_dir,4096,false);
  }
 }
 function captureEvidencePaths(loaded:Loaded,evidence:Obj,root:string):Obj {
  evidencePathShape(evidence,loaded);
  const captured=structuredClone(evidence),resolve=(value:string):string=>resolveRef(root,value);
  for(const ref of Object.values(captured.runs) as (Obj|null)[])if(ref)ref.run_dir=resolve(ref.run_dir);
  for(const ref of Object.values(captured.claims) as Obj[])if(ref.run_dir!==null)ref.run_dir=resolve(ref.run_dir);
  const reuse=captured.reuse_evidence;
  if(reuse&&!Array.isArray(reuse)){
   reuse.candidate_dir=resolve(reuse.candidate_dir);
   for(const ref of reuse.source_refs)ref.path=resolve(ref.path);
   for(const pair of reuse.fresh_task_evidence)for(const side of ['common','candidate'])pair[side].campaign_dir=resolve(pair[side].campaign_dir);
  }
  if(captured.ordinary_execution)for(const side of ['baseline','candidate'])captured.ordinary_execution[side].run_dir=resolve(captured.ordinary_execution[side].run_dir);
  return captured;
 }
 export async function assessCommand(loaded:Loaded,evidenceFile:string,output:string,deps:Dependencies):Promise<Obj>{
  if(loaded.protectedCodemode){
   const target=path.resolve(output);
   if(target===loaded.dir||target.startsWith(loaded.dir+path.sep))fail('assessment output must be outside the retained campaign');
   await directory(path.dirname(target));
  }
  const evidence=captureEvidencePaths(loaded,await json(evidenceFile),path.dirname(path.resolve(evidenceFile)));
  const dir=await fresh(output),created=await fs.lstat(dir);
  try{
   const stored=path.join(dir,'evidence.json');
   await write(stored,evidence);
   const report=await assess(loaded,stored,deps,deps.now()+loaded.c.budget.elapsed_seconds*1000);
   await write(path.join(dir,'report.json'),report);
   return report;
  }catch(error){
   try{
    const current=await fs.lstat(dir);
    if(current.isDirectory()&&current.dev===created.dev&&current.ino===created.ino)await fs.rm(dir,{recursive:true,force:true});
   }
   finally{throw error;}
  }
 }
 export async function adopt(loaded:Loaded,reportFile:string,approvalFile:string,destination:string,deps:Dependencies):Promise<Obj>{
  const report=await json(reportFile);
  const evidenceFile=path.join(path.dirname(reportFile),'evidence.json');
  const recomputed=await assess(loaded,evidenceFile,deps,deps.now()+loaded.c.budget.elapsed_seconds*1000);
  if(!same(report,recomputed))fail('stale adoption report');
  const a=await json(approvalFile);
  exact(a,['schema','owner_id','campaign_id','campaign_digest','report_digest','candidate_digest','destination','expected_preimage','decision','retention','library_entry']);
  schema(a,'millforge-optimization-adoption/v1');
  if(a.owner_id!==loaded.c.owner_id||a.campaign_id!==loaded.c.campaign_id||a.campaign_digest!==loaded.digest||a.report_digest!==fileDigest(report)||path.resolve(destination)!==a.destination||a.expected_preimage!=='absent')fail('adoption approval mismatch');
  member(a.decision,['adopt','decline']);
  member(a.retention,['task_specific','library_entry']);
  if(codemode(loaded.c)&&a.retention!=='task_specific')fail('Codemode requires task-specific retention');
  if(a.retention==='task_specific'){
   if(a.library_entry!==null)fail('task retention has no library entry');
   const digest=report.recommendation==='baseline'?loaded.baseline.digest:report.candidate_digests[report.recommendation]??null;
   if(a.candidate_digest!==digest||a.decision==='adopt'&&(!digest||['baseline','inconclusive'].includes(report.recommendation)||loaded.c.purpose!=='search'))fail('candidate cannot be adopted');
   if(a.decision==='adopt'){
    const dir=path.join(loaded.dir,'candidates',report.recommendation),file=path.join(dir,'profile.json');
    const raw=await read(file);
    const candidate=await profile(loaded.c.base,dir,deps,deps.now()+loaded.c.budget.elapsed_seconds*1000,roleTarget(loaded.c,false).profile_schema);
    if(candidate.digest!==digest||!raw.equals(await read(file)))fail('adopted bytes changed');
    validateSubject(loaded,candidate);
    await fresh(destination);
    await write(path.join(destination,'profile.json'),raw);
    for(const [rel,data]of candidate.assets)await write(path.join(destination,rel),data);
   }
   else await fresh(destination);
  }
  else{
   const entry=objectEntry(a.library_entry),reuse=report.observations.reuse_assessment;
   if(reuse.scope!=='cross_task'||a.candidate_digest!==reuse.candidate_digest||entry.profile_digest!==reuse.candidate_digest||entry.source_candidate_digest!==reuse.source_candidate_digest||entry.lesson_kind!==reuse.lesson_kind||!same(entry.asset_digests,reuse.asset_digests)||!same(entry.fresh_task_evidence,reuse.fresh_task_evidence)||a.decision!==(entry.lesson_kind==='positive'?'adopt':'decline'))fail('library retention mismatch');
   const evidence=await json(evidenceFile),subject=await profile(loaded.c.base,resolveRef(path.dirname(evidenceFile),evidence.reuse_evidence.candidate_dir),deps,deps.now()+loaded.c.budget.elapsed_seconds*1000);
   if(subject.digest!==entry.profile_digest)fail('promotion subject changed');
   for(const fact of list(entry.excluded_task_facts,256))text(fact,8192,false);
   if(reuse.fresh_task_evidence.some((pair:Obj)=>!same(pair.validation_exclusions,entry.excluded_task_facts)))fail('promotion exclusions differ');
   excluded(subject.profile,subject.assets,entry.excluded_task_facts);
   for(const fact of entry.excluded_task_facts)if(entry.cue.includes(fact)||entry.lesson.includes(fact))fail('lesson contains excluded fact');
   const approvalRoot=path.dirname(path.resolve(approvalFile));
   const files=new Map<string,Buffer>();
   const subjectPrefix=entry.lesson_kind==='negative'?'audit-subject/':'';
   files.set(subjectPrefix+'profile.json',await read(path.join(subject.dir,'profile.json')));
   for(const [rel,data]of subject.assets)files.set(subjectPrefix+rel,data);
   for(const ref of entry.source_refs){
    relative(ref.path);
    if(ref.path==='entry.json'||files.has(ref.path))fail('conflicting promotion reference');
    const data=await read(path.join(approvalRoot,ref.path));
    if(hash(data)!==ref.sha256)fail('promotion audit changed');
    files.set(ref.path,data);
   }
   const total=[...files.values()].reduce((n,b)=>n+b.length,Buffer.byteLength(canonical(entry)+'\n')+Buffer.byteLength(canonical(a)+'\n'));
   if(total>67108864)fail('promotion closure too large');
   // Validate the complete local closure in an owned sibling before publication.
   try{await fs.lstat(destination);fail('destination already exists');}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
   const stage=await fs.mkdtemp(path.join(path.dirname(path.resolve(destination)),'.mfopt-entry-'));
   try{
    for(const [rel,data]of files)await write(path.join(stage,rel),data);
    await write(path.join(stage,'entry.json'),entry);
    await write(path.join(stage,'adoption.json'),a);
    await validateEntry(entry,stage,loaded.c,deps,deps.now()+loaded.c.budget.elapsed_seconds*1000,new Set());
    try{await fs.lstat(destination);fail('destination already exists');}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
    await fs.rename(stage,destination);
   }finally{await fs.rm(stage,{recursive:true,force:true});}
  }
  if(a.retention==='task_specific')await write(path.join(destination,'adoption.json'),a);
  return {
   decision:a.decision,retention:a.retention,destination,candidate_digest:a.candidate_digest,protection_scope:report.protection_scope
  }
  ;
 }
 function objectEntry(value:unknown):Obj {
  if(!value||typeof value!=='object'||Array.isArray(value))fail('library entry required');
  return value as Obj;
 }
