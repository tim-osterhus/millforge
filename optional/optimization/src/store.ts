/** Ordinary owned files, captured inputs and deterministic profile materialization. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {type Obj,absolute,budget,canonical,campaign,checked,codemode,isV2,copyTree,directory,exact,excluded,fail,fresh,hash,id,integer,json,list,member,object,parse,read,relative,requireOptimizerScope,roleTarget,same,schema,sha,text,tree,workIds,workMap,write} from './contracts.js';
import {type Dependencies,baseCommand,matchQualification,profile,verifyBase} from './lifecycle.js';
import {baseVersion,protocol} from './versions.js';
import {type DeploymentPaths,deployedAuthority,deployedWorkMap,deploymentPaths,optionalProtectedRead,protectedInput,protectedRead,verifyDeployment} from './protected-deployment.js';
export interface Loaded {dir:string;c:Obj;digest:string;map:Obj;baseline:Awaited<ReturnType<typeof profile>>;proposer:Awaited<ReturnType<typeof profile>>|null;protectedCodemode?:true}
export const resolveRef=(root:string,ref:string):string=>path.isAbsolute(ref)?ref:path.resolve(root,ref);
export function effectiveBudget(c:Obj):number{return c.budget.elapsed_seconds*1000;}
async function campaignProfiles(c:Obj,root:string,deps:Dependencies,deadline:number):Promise<Pick<Loaded,'baseline'|'proposer'>>{
 const baseline=await profile(c.base,resolveRef(root,c.baseline.profile_path),deps,deadline,roleTarget(c,false).profile_schema);
 const proposer=c.proposal_profile?await profile(c.base,resolveRef(root,c.proposal_profile.profile_path),deps,deadline,roleTarget(c,true).profile_schema):null;
 if(baseline.digest!==c.baseline.profile_digest||baseline.profile.mode!=='agent')fail('baseline profile identity/mode mismatch');
 for(const tool of c.allowed_tools)if(!baseline.profile.tools.includes(tool))fail('unapproved tool');
 if(proposer){
  const p=proposer.profile;
  if(proposer.digest!==c.proposal_profile.profile_digest||p.mode!=='generate'||p.tools.length||
     p.capabilities.extensions.length||p.capabilities.packages.length)fail('proposer profile identity/mode mismatch');
 }
 return {baseline,proposer};
}
async function copyProfile(loaded:Awaited<ReturnType<typeof profile>>,dest:string):Promise<void>{await fresh(dest);await write(path.join(dest,'profile.json'),await read(path.join(loaded.dir,'profile.json'),4194304));for(const [rel,data]of loaded.assets)await write(path.join(dest,rel),data);}
export async function evaluator(c:Obj):Promise<Obj>{const root=await directory(c.task.evaluator_path);if((await tree(root)).digest!==c.task.evaluator_digest)fail('evaluator changed');const m=await json(path.join(root,'evaluator.json'));exact(m,['schema','evaluator_id','check_ids','entrypoint','files']);schema(m,'millforge-optimization-evaluator/v1');if(m.evaluator_id!==c.task.evaluator_id||!relative(m.entrypoint).endsWith('.mjs'))fail('evaluator identity');const ids=list(m.check_ids,256).map(v=>id(v));if(!ids.length||new Set(ids).size!==ids.length)fail('evaluator check IDs');const info=await tree(root);const actual=info.files.filter(f=>f.path!=='evaluator.json').map(f=>({path:f.path,sha256:f.value}));if(!same(actual,m.files)||!actual.some(f=>f.path===m.entrypoint))fail('evaluator closure mismatch');return m;}
export async function taskInput(c:Obj,root:string):Promise<Obj>{const dir=resolveRef(root,c.task.input_dir),info=await tree(dir);if(info.digest!==c.task.input_digest)fail('task input changed');for(const f of info.files)if(f.path!=='task.json'&&!f.path.startsWith('workspace/'))fail('undeclared task input');for(const f of info.files)if(f.mode&0o111)fail('executable task input');const task=await json(path.join(dir,'task.json'));exact(task,['schema','task','context']);schema(task,'millforge-optimization-task/v1');text(task.task);for(const value of list(task.context,1024))text(value);return task;}
export async function prepareCampaign(file:string,output:string,baseBin:string,deps:Dependencies):Promise<Loaded>{
 const authorRoot=path.dirname(path.resolve(file)),c=campaign(await json(file));if(!isV2(c))fail('new campaigns require campaign v2','UNSUPPORTED_VERSION');if(c.base.package_version!==baseVersion)fail('new campaigns require Millforge '+baseVersion,'UNSUPPORTED_VERSION');if(await fs.realpath(baseBin)!==c.base.cli_path)fail('base CLI differs');await verifyBase(c.base);
 const deadline=deps.now()+effectiveBudget(c),{baseline,proposer}=await campaignProfiles(c,authorRoot,deps,deadline);
 c.task.input_dir=resolveRef(authorRoot,c.task.input_dir);c.task.evaluator_path=resolveRef(authorRoot,c.task.evaluator_path);await taskInput(c,authorRoot);await evaluator(c);const dir=await fresh(output);await fs.mkdir(path.join(dir,'profiles'));await copyProfile(baseline,path.join(dir,'profiles/baseline'));if(proposer)await copyProfile(proposer,path.join(dir,'profiles/proposer'));
 const seen=new Set<string>();for(const a of c.assets){if(seen.has(a.path))fail('duplicate eligible asset');seen.add(a.path);const source=await checked(baseline.dir,a.path),st=await fs.lstat(source),data=await read(source,4194304);if(st.mode&0o111||hash(data)!==a.sha256)fail('eligible asset mismatch');new TextDecoder('utf-8',{fatal:true}).decode(data);const target=path.join(dir,'profiles/baseline',a.path);try{const current=await read(target);if(!current.equals(data))fail('eligible asset conflict');}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')await write(target,data);else throw e;}}
 await copyTree(c.task.input_dir,path.join(dir,'inputs'));c.task.input_dir='inputs';c.baseline.profile_path='profiles/baseline';if(c.proposal_profile)c.proposal_profile.profile_path='profiles/proposer';if(c.library){const lib=resolveRef(authorRoot,c.library.path);if((await tree(lib)).digest!==c.library.digest)fail('library changed');await (await import('./evaluation.js')).validateLibrary(lib,c,deps,deadline,new Set());await copyTree(lib,path.join(dir,'library'),16*67108864);c.library.path='library';}
 const frozen=campaign(c),map=workMap(frozen,baseline.profile,proposer?.profile??null);const task=await taskInput(frozen,dir);for(const p of [baseline.profile,...(proposer?[proposer.profile]:[])])if(Buffer.byteLength(task.task)+task.context.reduce((n:number,v:string)=>n+Buffer.byteLength(v),0)>p.limits.input_bytes)fail('task input exceeds profile');if(c.validation_exclusions)excluded(baseline.profile,baseline.assets,c.validation_exclusions);
 await write(path.join(dir,'campaign.json'),frozen);await write(path.join(dir,'work-map.json'),map);await write(path.join(dir,'approval-template.json'),{schema:'millforge-optimization-authority/v1',owner_id:c.owner_id,campaign_id:c.campaign_id,campaign_digest:map.campaign_digest,execution_root:dir,budget:c.budget,expires_at:null});return loadCampaign(dir,baseBin,deps,deadline);
}
async function optionalRead(file:string):Promise<Buffer|null>{
 try{return await read(file);}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return null;throw error;}
}
type Captured = Pick<Loaded,'dir'|'c'>;
async function capturedCampaign(input:string):Promise<Captured>{
 const dir=await directory(input);
 return {dir,c:campaign(await json(path.join(dir,'campaign.json')))};
}
async function verifyRun(dir:string,c:Obj,bytes:Buffer):Promise<Obj>{
 const digest=hash(canonical(c));
 const run=exact(parse(bytes),['schema','campaign_id','campaign_digest','execution_root','authority_sha256','started_at','elapsed_seconds','optimizer']);
 schema(run,'millforge-optimization-run/v1');
 if(run.campaign_id!==c.campaign_id||run.campaign_digest!==digest||run.execution_root!==dir||
    typeof run.started_at!=='string'||!Number.isFinite(Date.parse(run.started_at)))fail('run identity mismatch');
 sha(run.authority_sha256);
 integer(run.elapsed_seconds,1,c.budget.elapsed_seconds);
 const map=exact(await json(path.join(dir,'work-map.json')),['schema','campaign_id','campaign_digest','slots']);
 schema(map,'millforge-optimization-work-map/v1');
 const admitted=list(map.slots,workIds.length).map(slot=>slot.work_id);
 if(map.campaign_id!==c.campaign_id||map.campaign_digest!==digest||
    !same(admitted,c.purpose==='search'?workIds:['baseline']))fail('work map identity mismatch');
 return run;
}
/** Verify the retained proof at its recorded start, independently of current run authority. */
async function verifyDeployedReadback(dir:string,c:Obj,paths:DeploymentPaths,evidenceFile?:string):Promise<void>{
 if(!codemode(c))fail('protected campaign root requires Codemode');
 requireOptimizerScope(c,'deployed',true);
 await verifyDeployment(paths,dir);
 if(!same(parse(await protectedRead(path.join(dir,'campaign.json'))),c))fail('captured campaign changed');
 const map=await deployedWorkMap(dir,c);
 const run=await verifyRun(dir,c,await protectedRead(path.join(dir,'run.json')));
 const optimizer={path:paths.entry,sha256:hash(await protectedRead(paths.entry)),...protocol};
 if(!same(run.optimizer,optimizer))fail('retained optimizer identity mismatch');
 const authority=await protectedRead(path.join(dir,'receipts/authority.json'));
 if(hash(authority)!==run.authority_sha256)fail('captured authority changed');
 const approval=deployedAuthority(authority,c,dir,Date.parse(run.started_at));
 if(run.elapsed_seconds!==approval.budget.elapsed_seconds)fail('run authority budget mismatch');
 const qualification=await protectedRead(path.join(dir,'receipts/qualification.json'));
 if(hash(qualification)!==c.execution.qualification.sha256)fail('qualification changed');
 matchQualification(parse(qualification),c,'deployed',null,true);
 const evidencePath=path.join(dir,'evidence.json');
 if(evidenceFile&&path.resolve(evidenceFile)!==evidencePath)fail('assessment requires fixed retained evidence');
 const evidenceBytes=await optionalProtectedRead(evidencePath);
 if(evidenceFile&&!evidenceBytes)fail('retained evidence missing');
 const digest=hash(canonical(c)),slot=map.slots[0];
 const evidence=evidenceBytes?object(parse(evidenceBytes)):null;
 if(evidence){
  schema(evidence,'millforge-optimization-evidence/v1');
  requireOptimizerScope(c,evidence.protection_scope,true);
  if(evidence.campaign_id!==c.campaign_id||evidence.bindings!==null||!same(evidence.reuse_evidence,[])||evidence.ordinary_execution!==null||
     Object.keys(evidence.claims).length!==1||!evidence.claims.baseline||
     ['general','domain','composed'].some(kind=>evidence.runs[kind]!==null||evidence.evaluations[kind]!==null))fail('deployed evidence identity mismatch');
  for(const link of [evidence.claims.baseline,evidence.runs.baseline]){
   if(link?.run_dir!=null&&link.run_dir!=='runs/baseline')fail('deployed evidence requires the retained baseline');
  }
 }
 for(const [name,kind] of [['baseline-closure','closure'],['baseline-evaluation','evaluation']] as const){
  const bytes=await optionalProtectedRead(path.join(dir,'receipts',name+'.json'));
  const expected=kind==='closure'?evidence?.claims.baseline.closure_receipt_sha256:evidence?.evaluations.baseline?.receipt_sha256;
  if(evidence&&(bytes?hash(bytes):null)!==(expected??null))fail('deployed receipt hash mismatch');
  if(!bytes)continue;
  const receipt=parse(bytes);
  schema(receipt,'millforge-optimization-'+kind+'/v1');
  requireOptimizerScope(c,receipt.scope,true);
  if(receipt.campaign_id!==c.campaign_id||receipt.work_id!==slot.work_id||receipt.request_id!==slot.request_id||
     kind==='closure'&&evidence&&receipt.claim_sha256!==evidence.claims.baseline.claim_sha256||
     kind==='evaluation'&&receipt.qualification_sha256!==c.execution.qualification.sha256)fail('deployed receipt identity mismatch');
 }
 const reportBytes=await optionalProtectedRead(path.join(dir,'report.json'));
 if(reportBytes){
  const report=parse(reportBytes);
  schema(report,'millforge-optimization-report/v1');
  requireOptimizerScope(c,report.protection_scope,true);
  if(report.campaign_id!==c.campaign_id||report.campaign_digest!==digest||
     !evidence||report.evidence_digest!==hash(canonical(evidence)+'\n'))fail('deployed report identity mismatch');
 }
 const inventory=await optionalProtectedRead(path.join(dir,'dispatch-inventory.json'));
 if(inventory||reportBytes){
  if(!inventory||!evidenceBytes)fail('terminal deployment evidence missing');
  const artifacts=parse(await protectedRead(path.join(dir,'artifacts.json')));
  schema(artifacts,'millforge-optimization-artifacts/v1');
  if(artifacts.campaign_digest!==digest||artifacts.campaign_id!==c.campaign_id)fail('artifact inventory mismatch');
  const files=new Set<string>();
  for(const item of list(artifacts.files)){
   const file=await checked(dir,relative(item.path));
   if(files.has(item.path)||hash(await protectedRead(file))!==item.sha256)fail('retained artifact changed');
   files.add(item.path);
  }
  for(const name of ['run.json','receipts/authority.json','receipts/qualification.json','evidence.json','dispatch-inventory.json']){
   if(!files.has(name))fail('terminal deployment artifact missing');
  }
 }
 await protectedInput(paths,c.base.prefix_path,'directory');
 await protectedInput(paths,c.base.cli_path,'file');
}
/** Detect an interrupted start while the retained qualification is absent. */
async function checkCodemodeReadScope({dir,c}:Captured,evidenceFile?:string):Promise<boolean>{
 if(!codemode(c))return false;
 const started=await optionalRead(path.join(dir,'run.json'));
 if(started){
  const origin=parse(started);
  const paths=typeof origin.optimizer?.path==='string'?deploymentPaths(origin.optimizer.path):undefined;
  if(paths&&paths.campaign===origin.execution_root)fail('unregistered Codemode campaign root');
 }
 const report=await optionalRead(path.join(dir,'report.json'));
 const evidence=await optionalRead(path.join(dir,'evidence.json'));
 for(const bytes of [report,evidence])if(bytes)requireOptimizerScope(c,parse(bytes).protection_scope);
 const qualification=await optionalRead(path.join(dir,'receipts/qualification.json'));
 if(qualification){
  if(hash(qualification)!==c.execution.qualification.sha256)fail('qualification changed');
  matchQualification(parse(qualification),c,'test',null);
 }else if(report||evidence||await optionalRead(path.join(dir,'dispatch-inventory.json'))){
  fail('retained qualification missing');
 }
 for(const work of workIds){
  const closure=await optionalRead(path.join(dir,'receipts',work+'-closure.json'));
  if(closure){
   requireOptimizerScope(c,parse(closure).scope);
   if(!qualification)fail('retained qualification missing');
  }
 }
 if(!qualification&&started){
  if(evidenceFile)fail('retained qualification missing');
  await verifyRun(dir,c,started);
  return true;
 }
 if(evidenceFile)requireOptimizerScope(c,(await json(evidenceFile)).protection_scope);
 return false;
}
export async function verifyCodemodeAssessmentScope(dir:string,c:Obj,evidenceFile:string,paths?:DeploymentPaths):Promise<true|void>{
 if(paths?.campaign===dir){
  await verifyDeployedReadback(dir,c,paths,evidenceFile);
  return true;
 }
 await checkCodemodeReadScope({dir,c},evidenceFile);
}
export async function inspectCapturedCampaign(input:string,baseBin:string,deps:Dependencies):Promise<Obj>{
 const captured=await capturedCampaign(input);
 if(deps.codemodeDeployment?.campaign===captured.dir){
  await verifyDeployedReadback(captured.dir,captured.c,deps.codemodeDeployment);
  return inspectCampaign(await loadCapturedCampaign(captured,baseBin,deps,undefined,true));
 }
 if(!await checkCodemodeReadScope(captured))return inspectCampaign(await loadCapturedCampaign(captured,baseBin,deps));
 if(await fs.realpath(baseBin)!==captured.c.base.cli_path)fail('base CLI mismatch');
 return inspectionResult(captured.c.campaign_id,null,null,true);
}
export async function loadCampaign(input:string,baseBin:string,deps:Dependencies,deadline?:number,beforeBase?:(dir:string,c:Obj)=>Promise<true|void>):Promise<Loaded>{
 const captured=await capturedCampaign(input);
 const protectedCodemode=await beforeBase?.(captured.dir,captured.c);
 return loadCapturedCampaign(captured,baseBin,deps,deadline,protectedCodemode);
}
async function loadCapturedCampaign({dir,c}:Captured,baseBin:string,deps:Dependencies,deadline?:number,protectedCodemode?:true|void):Promise<Loaded>{deadline=deadline??deps.now()+effectiveBudget(c);await verifyBase(c.base);if(await fs.realpath(baseBin)!==c.base.cli_path)fail('base CLI mismatch');const {baseline,proposer}=await campaignProfiles(c,dir,deps,deadline);const expected=workMap(c,baseline.profile,proposer?.profile??null),map=await json(path.join(dir,'work-map.json'));if(!same(map,expected))fail('captured work map changed');await taskInput(c,dir);if(c.validation_exclusions)excluded(baseline.profile,baseline.assets,c.validation_exclusions);if(c.library&&(await tree(resolveRef(dir,c.library.path),16*67108864)).digest!==c.library.digest)fail('captured library changed');return {dir,c,digest:hash(canonical(c)),map,baseline,proposer,...(protectedCodemode?{protectedCodemode}:{})};}
export function proposal(v:unknown,loaded:Loaded,kind:string,parents:string[]):Obj {const p=exact(v,['schema','campaign_id','kind','baseline_digest','parent_candidates','instruction_text','tools','assets','rationale']);schema(p,'millforge-optimization-proposal/v1');if(p.campaign_id!==loaded.c.campaign_id||p.kind!==kind||p.baseline_digest!==loaded.baseline.digest||!same(p.parent_candidates,parents))fail('proposal identity mismatch');text(p.instruction_text,65536);text(p.rationale,8192);const tools=list(p.tools,7);if(codemode(loaded.c)&&!tools.length)fail('Codemode requires at least one native tool');let at=0;for(const tool of tools){const index=loaded.baseline.profile.tools.indexOf(tool,at);if(index<0||!loaded.c.allowed_tools.includes(tool))fail('tool subset/order mismatch');at=index+1;}const selected=new Set<string>();for(const a of list(p.assets,128)){exact(a,['path','kind','text']);relative(a.path);text(a.text,4194304);if(selected.has(a.path)||!loaded.c.assets.some((allowed:Obj)=>allowed.path===a.path&&allowed.kind===a.kind))fail('asset not eligible');selected.add(a.path);}const old=loaded.baseline.profile;if(!loaded.c.mutable_fields.includes('/instructions')&&p.instruction_text!==old.instructions||!loaded.c.mutable_fields.includes('/tools')&&!same(tools,old.tools))fail('protected field changed');return p;}
/** Apply precisely the proposal mutation policy to an already materialized/reuse subject. */
export function validateSubject(loaded:Loaded,subject:{profile:Obj;assets:Map<string,Buffer>}):void{
 const baseline=loaded.baseline.profile,candidate=subject.profile;
 const eligible=new Set(loaded.c.assets.map((a:Obj)=>a.path));
 const edits=candidate.assets.filter((a:Obj)=>eligible.has(a.path)).map((a:Obj)=>({
  path:a.path,kind:a.kind,text:new TextDecoder('utf-8',{fatal:true}).decode(subject.assets.get(a.path)??fail('subject asset missing'))
 }));
 proposal({schema:'millforge-optimization-proposal/v1',campaign_id:loaded.c.campaign_id,kind:'general',baseline_digest:loaded.baseline.digest,parent_candidates:[],instruction_text:candidate.instructions,tools:candidate.tools,assets:edits,rationale:''},loaded,'general',[]);
 const protectedPart=(value:Obj):Obj=>{const {instructions:_i,tools:_t,assets:_a,...rest}=value;return rest;};
 if(!same(protectedPart(candidate),protectedPart(baseline)))fail('protected profile changed');
 for(const asset of candidate.assets)if(!eligible.has(asset.path)){
  const original=baseline.assets.find((a:Obj)=>a.path===asset.path);
  if(!original||!same(original,asset))fail('asset not eligible');
 }
 for(const [rel,data]of loaded.baseline.assets)if(!eligible.has(rel)&&!subject.assets.get(rel)?.equals(data))fail('protected asset changed');
 const sameAssets=subject.assets.size===loaded.baseline.assets.size&&[...subject.assets].every(([rel,data])=>loaded.baseline.assets.get(rel)?.equals(data));
 if(!loaded.c.mutable_fields.includes('/assets')&&(!sameAssets||!same(candidate.assets,baseline.assets)))fail('protected assets changed');
}
export async function materialize(loaded:Loaded,value:unknown,output:string,deps:Dependencies,deadline:number,kind?:string,parents?:string[]):Promise<Awaited<ReturnType<typeof profile>>>{
 const v=value as Obj;
 const p=proposal(value,loaded,kind??member(v.kind,['general','domain','composed']),parents??(v.kind==='composed'?list(v.parent_candidates,2):[]));
 const baseline=loaded.baseline.profile;
 const eligible=new Set(loaded.c.assets.map((a:Obj)=>a.path));
 // A proposal supplies editable slots only. Unselected operator assets are protected.
 const assets=new Map<string,Buffer>([...loaded.baseline.assets].filter(([rel])=>!eligible.has(rel)));
 for(const a of p.assets)assets.set(a.path,Buffer.from(a.text));
 const unchangedAssets=assets.size===loaded.baseline.assets.size&&[...assets].every(([rel,data])=>loaded.baseline.assets.get(rel)?.equals(data));
 if(!loaded.c.mutable_fields.includes('/assets')&&!unchangedAssets)fail('protected assets changed');
 const unchanged=p.instruction_text===baseline.instructions&&same(p.tools,baseline.tools)&&unchangedAssets;
 const next={...baseline,instructions:p.instruction_text,tools:p.tools,assets:!loaded.c.mutable_fields.includes('/assets')?baseline.assets:[...assets].sort(([a],[b])=>a<b?-1:a>b?1:0).map(([rel,data])=>({
  path:rel,kind:baseline.assets.find((a:Obj)=>a.path===rel)?.kind??loaded.c.assets.find((a:Obj)=>a.path===rel).kind,sha256:hash(data)
 }))};
 let total=Buffer.byteLength(canonical(next)+'\n');
 for(const data of assets.values())total+=data.length;
 if(total>4194304)fail('authored bytes exceed base limit');
 await fresh(output);
 await write(path.join(output,'profile.json'),unchanged?await read(path.join(loaded.baseline.dir,'profile.json')):canonical(next)+'\n');
 for(const [rel,data]of assets)await write(path.join(output,rel),data);
 const result=await profile(loaded.c.base,output,deps,deadline,roleTarget(loaded.c,false).profile_schema);
 validateSubject(loaded,result);
 return result;
}
export async function candidateCommand(loaded:Loaded,file:string,output:string,deps:Dependencies):Promise<Obj>{const p=await json(file);let parents:string[]=[];if(p.kind==='composed'){for(const k of ['general','domain']){const v=await profile(loaded.c.base,path.join(loaded.dir,'candidates',k),deps,deps.now()+effectiveBudget(loaded.c),roleTarget(loaded.c,false).profile_schema);validateSubject(loaded,v);parents.push(v.digest);}}const result=await materialize(loaded,p,output,deps,deps.now()+effectiveBudget(loaded.c),p.kind,parents);return {profile_digest:result.digest,path:result.dir};}
export async function inspectCampaign(loaded:Loaded):Promise<Obj>{let inventory:Obj|null=null;try{inventory=await json(path.join(loaded.dir,'dispatch-inventory.json'));}catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;}if(inventory){exact(inventory,['schema','campaign_id','campaign_digest','admitted_work_ids','claim_digests','terminal_status']);schema(inventory,'millforge-optimization-dispatch-inventory/v1');if(inventory.campaign_digest!==loaded.digest||!same(inventory.admitted_work_ids,loaded.map.slots.map((s:Obj)=>s.work_id)))fail('inventory identity');for(const [work,digest]of Object.entries(inventory.claim_digests)){if(!loaded.map.slots.some((s:Obj)=>s.work_id===work)||hash(await read(path.join(loaded.dir,'claims',work+'.json')))!==digest)fail('inventory claim mismatch');}const claims=await fs.readdir(path.join(loaded.dir,'claims')).catch(e=>{if(e.code==='ENOENT')return [];throw e;});if(!same(claims.sort(),Object.keys(inventory.claim_digests).map(k=>k+'.json').sort()))fail('inventory missing claim');}const run=await fs.stat(path.join(loaded.dir,'run.json')).then(()=>true).catch(e=>{if(e.code==='ENOENT')return false;throw e;});let report:Obj|null=null;
 try{report=await json(path.join(loaded.dir,'report.json'));if(report.schema!=='millforge-optimization-report/v1'||report.campaign_digest!==loaded.digest)fail('report identity mismatch');}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
 if(report){const bytes=await read(path.join(loaded.dir,'receipts/qualification.json'));if(hash(bytes)!==loaded.c.execution?.qualification.sha256)fail('qualification changed');const scope=member(parse(bytes).scope,['test','deployed']);requireOptimizerScope(loaded.c,scope,loaded.protectedCodemode);if(report.protection_scope!==scope)fail('report protection scope mismatch');}
 return inspectionResult(loaded.c.campaign_id,inventory,report,run,loaded.protectedCodemode&&inventory?'deployed':null);
}
function inspectionResult(campaignId:string,inventory:Obj|null,report:Obj|null,run:boolean,scope:string|null=null):Obj{
 const dispatch=inventory?.terminal_status??(run?'uncertain':'prepared');
 const status=dispatch==='completed'&&report?.recommendation==='inconclusive'?'failed':dispatch;
 const exitCode=status==='prepared'?null:status==='cancelled'?130:status==='timeout'?124:status==='completed'?0:3;
 return {campaign_id:campaignId,status,dispatch_status:dispatch,recommendation:report?.recommendation??null,protection_scope:report?.protection_scope??scope,exit_code:exitCode,exit_code_source:'saved_disposition',inventory};
}
export async function libraryProjection(loaded:Loaded,deps:Dependencies,deadline:number):Promise<Obj[]>{if(!loaded.c.library)return [];const dir=resolveRef(loaded.dir,loaded.c.library.path);await (await import('./evaluation.js')).validateLibrary(dir,loaded.c,deps,deadline,new Set());const out:Obj[]=[];for(const name of (await fs.readdir(dir)).sort()){const entry=await json(path.join(dir,name,'entry.json'));const assets:Obj[]=[];for(const a of entry.worker_assets){const data=await read(await checked(entry.lesson_kind==='negative'?path.join(dir,name,'audit-subject'):path.join(dir,name),a.path),4194304);if(hash(data)!==a.sha256)fail('worker asset changed');assets.push({path:a.path,text:new TextDecoder('utf-8',{fatal:true}).decode(data)});}out.push({cue:entry.cue,lesson:entry.lesson,lesson_kind:entry.lesson_kind,compatibility:entry.compatibility,assets});}return out;}
export async function artifactInventory(root:string,c:Obj):Promise<Obj>{const out:Obj[]=[];const visit=async(dir:string):Promise<void>=>{for(const name of (await fs.readdir(dir)).sort()){const p=path.join(dir,name),rel=path.relative(root,p).split(path.sep).join('/');if(rel==='artifacts.json'||rel==='approval-template.json'||rel.startsWith('workers/'))continue;const st=await fs.lstat(p);if(st.isSymbolicLink())fail('artifact symlink');if(st.isDirectory())await visit(p);else if(st.isFile())out.push({kind:name==='profile.json'?'profile':name.replace(/\.[^.]*$/,''),path:rel,sha256:hash(await read(p))});else fail('special artifact');}};await visit(root);return {schema:'millforge-optimization-artifacts/v1',campaign_id:c.campaign_id,campaign_digest:hash(canonical(c)),files:out};}
