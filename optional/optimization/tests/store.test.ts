import {describe,it,expect,afterEach} from 'vitest';import fs from 'node:fs/promises';import path from 'node:path';import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';import {prepareCampaign,loadCampaign,inspectCampaign,candidateCommand} from '../src/store.js';import {runCampaign} from '../src/method.js';import {adopt,assess,assessCommand,fileDigest,compatibility,validateLibrary} from '../src/evaluation.js';import {makeHarness} from './fixtures/fake-process.mjs';
const roots:string[]=[],pending=new Set<Promise<void>>();
// These cases include repeated assessment/adoption or fresh-pair audit publication.
// A Vitest case timeout does not cancel its async body; join it before removing fixtures.
function joined(body:(...args:any[])=>Promise<void>):(...args:any[])=>Promise<void>{return(...args:any[])=>{const work=body(...args);pending.add(work);void work.then(()=>pending.delete(work),()=>pending.delete(work));return work;};}
afterEach(async()=>{await Promise.allSettled([...pending]);for(const p of roots.splice(0))await fs.rm(p,{recursive:true,force:true});},30000);async function setup(options:any={}){const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-store-'));roots.push(root);return makeHarness(root,{...contracts,prepareCampaign},options) as Promise<any>;}
it('reads a frozen 0.2.0 campaign against its original base and refuses fresh preparation or dispatch', async ()=>{
 const f=await setup({validation:true});
 const current=await f.prepare(),file=path.join(current.dir,'campaign.json');
 const historical={...current.c,base:{...current.c.base,package_version:'0.2.0'}};
 const historicalMap=contracts.workMap(historical,current.baseline.profile,null);
 const oldBytes=Buffer.from(contracts.canonical(historical)+'\n');
 await fs.writeFile(file,oldBytes);
 await fs.writeFile(path.join(current.dir,'work-map.json'),contracts.canonical(historicalMap)+'\n');
 const loaded=await loadCampaign(current.dir,f.baseBin,f.deps);
 expect(loaded.c.base).toEqual(historical.base);
 expect(await inspectCampaign(loaded)).toMatchObject({status:'prepared'});
 expect(await fs.readFile(file)).toEqual(oldBytes);
 expect(()=>contracts.campaign({...historical,base:{...historical.base,package_version:'0.2.2'}})).toThrow('base identity mismatch');
 const fresh=path.join(f.root,'legacy-new');
 await fs.writeFile(f.author,contracts.canonical({...f.campaign,base:historical.base})+'\n');
 await expect(prepareCampaign(f.author,fresh,f.baseBin,f.deps)).rejects.toMatchObject({code:'UNSUPPORTED_VERSION'});
 await expect(fs.lstat(fresh)).rejects.toMatchObject({code:'ENOENT'});
 await expect(runCampaign(loaded.dir,await f.approve(loaded),f.deps)).rejects.toMatchObject({code:'UNSUPPORTED_VERSION'});
 await expect(fs.lstat(path.join(loaded.dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
 expect(f.claims.size).toBe(0);
 await fs.appendFile(f.baseBin,'// changed base\n');
 await expect(loadCampaign(current.dir,f.baseBin,f.deps)).rejects.toThrow('base identity changed');
});
async function approval(f:any,loaded:any,report:any,dest:string,decision='adopt',retention='task_specific',entry:any=null){const a={schema:'millforge-optimization-adoption/v1',owner_id:'owner',campaign_id:loaded.c.campaign_id,campaign_digest:loaded.digest,report_digest:fileDigest(report),candidate_digest:retention==='library_entry'?entry.profile_digest:report.recommendation==='baseline'?loaded.baseline.digest:report.candidate_digests[report.recommendation]??null,destination:dest,expected_preimage:'absent',decision,retention,library_entry:entry};const file=path.join(f.root,'approval-'+path.basename(dest)+'.json');await contracts.write(file,a);return file;}
async function executed(f:any){const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);return {loaded,report:run.report};}
async function collect(source:string,prefix:string,destRoot:string,refs:any[]){const info=await contracts.tree(source);for(const file of info.files){if(file.path.startsWith('workers/'))continue;const rel=prefix+'/'+file.path,data=await contracts.read(path.join(source,file.path));await contracts.write(path.join(destRoot,rel),data);const name=path.basename(file.path);let kind='asset';if(name==='campaign.json')kind='campaign';else if(name==='evidence.json')kind='evidence';else if(name==='report.json')kind='report';else if(name==='work-map.json')kind='work_map';else if(name==='dispatch-inventory.json')kind='dispatch_inventory';else if(name==='artifacts.json')kind='artifact_inventory';else if(name==='run.json')kind='run';else if(file.path.startsWith('claims/'))kind='claim';else if(name==='qualification.json')kind='qualification';else if(name==='authority.json')kind='authority';else if(name==='campaign-timing.json')kind='timing';else if(name.endsWith('-base-result.json'))kind='result';else if(name.endsWith('-closure.json'))kind='closure';else if(name.endsWith('-broker.json'))kind='broker';else if(name.endsWith('-evaluation.json'))kind='evaluation';else if(name==='result.json')kind='result';else if(name==='capture.json')kind='capture';else if(name==='trace.jsonl')kind='trace';else if(name==='request.json')kind='request';else if(name==='profile.json')kind='profile';refs.push({kind,path:rel,sha256:contracts.hash(data)});}}
async function promotionFixture(negative:boolean,variant='match',exclusions:string[]=[]):Promise<any>{
 const source=await setup({regression:negative}),{loaded,report}=await executed(source);
 const originalSubjectDir=path.join(loaded.dir,'candidates/general'),originalSubject=await source.load(originalSubjectDir);
 let subjectDir=originalSubjectDir,subject=originalSubject;
 if(variant==='extra_tool'){
  subjectDir=path.join(source.root,'unapproved-subject');await fs.mkdir(subjectDir);
  await contracts.write(path.join(subjectDir,'profile.json'),{...originalSubject.profile,tools:['read','bash']});
  for(const asset of originalSubject.profile.assets)await contracts.write(path.join(subjectDir,asset.path),await contracts.read(path.join(originalSubjectDir,asset.path)));
  subject=await source.load(subjectDir);
 }
 const common=await setup({sharedBase:source.campaign.base,id:'fresh-common',validation:true,taskId:variant==='same_task'?'task':'fresh',taskText:variant==='same_task'?'Return a useful fixture deliverable.':'A new independent task.',regression:negative,equalOutcome:['tie','elapsed'].includes(variant),sameMetrics:variant==='tie',elapsedByWork:variant==='elapsed'?{baseline:50}:undefined}),candidate=await setup({sharedBase:source.campaign.base,id:'fresh-candidate',validation:true,taskId:variant==='same_task'?'task':'fresh',taskText:variant==='same_task'?'Return a useful fixture deliverable.':'A new independent task.',regression:negative,exclusions,limitAt:variant==='limited_extra'?'baseline':undefined,equalOutcome:['tie','elapsed'].includes(variant),sameMetrics:variant==='tie',elapsedByWork:variant==='elapsed'?{baseline:20}:undefined});candidate.campaign.baseline={profile_path:subjectDir,profile_digest:subject.digest};candidate.campaign.assets=subject.profile.assets;candidate.campaign.allowed_tools=subject.profile.tools;await fs.writeFile(candidate.author,contracts.canonical(candidate.campaign)+'\n');const ca=await executed(common),cb=await executed(candidate);if(variant==='limited_extra'){expect((await contracts.json(path.join(cb.loaded.dir,'runs/baseline/result.json'))).output).toBeNull();expect(cb.report.observations.runs.baseline).toMatchObject({status:'failed',complete:true,diagnostic:{code:'LIMIT_EXHAUSTED'}});expect(cb.report.observations.runs.baseline.checks.filter((c:any)=>c.pass)).toHaveLength(2);expect(ca.report.observations.runs.baseline.checks.filter((c:any)=>c.pass)).toHaveLength(1);}const extraPairs:any[]=[];
 if(variant==='mixed'){
  const ma=await setup({sharedBase:source.campaign.base,id:'mixed-common',validation:true,taskId:'mixed',taskText:'Another fresh independent task.',regression:!negative});
  const mb=await setup({sharedBase:source.campaign.base,id:'mixed-candidate',validation:true,taskId:'mixed',taskText:'Another fresh independent task.',regression:!negative,exclusions});
  mb.campaign.baseline={profile_path:subjectDir,profile_digest:subject.digest};mb.campaign.assets=subject.profile.assets;mb.campaign.allowed_tools=subject.profile.tools;await fs.writeFile(mb.author,contracts.canonical(mb.campaign)+'\n');
  const ea=await executed(ma),eb=await executed(mb);extraPairs.push({common:{campaign_dir:ea.loaded.dir,campaign_digest:ea.loaded.digest,report_digest:fileDigest(ea.report)},candidate:{campaign_dir:eb.loaded.dir,campaign_digest:eb.loaded.digest,report_digest:fileDigest(eb.report)}});
 }
 const earlier=await contracts.json(path.join(loaded.dir,'evidence.json'));for(const [kind,ref]of Object.entries(earlier.runs) as any)if(ref){ref.run_dir=path.join(loaded.dir,ref.run_dir);const work=kind==='baseline'?'baseline':kind+'_probe';earlier.claims[work].run_dir=ref.run_dir;}for(const link of Object.values(earlier.claims) as any[])if(link.run_dir&&!path.isAbsolute(link.run_dir))link.run_dir=path.join(loaded.dir,link.run_dir);
 const ordinaryDirs:any={};
 if(['ordinary','relative'].includes(variant)){
  ordinaryDirs.baseline=await source.ordinary(loaded.baseline.dir,'audit-baseline',3,40);
  ordinaryDirs.candidate=await source.ordinary(subjectDir,'audit-candidate',1,20);
  const ref=async(dir:string)=>({run_dir:dir,capture_sha256:contracts.hash(await contracts.read(path.join(dir,'capture.json'))),result_sha256:contracts.hash(await contracts.read(path.join(dir,'result.json'))),trace_sha256:contracts.hash(await contracts.read(path.join(dir,'trace.jsonl')))});
  earlier.ordinary_execution={baseline:await ref(ordinaryDirs.baseline),candidate:await ref(ordinaryDirs.candidate)};
 }
 earlier.reuse_evidence={candidate_dir:subjectDir,candidate_digest:subject.digest,source_candidate_digest:originalSubject.digest,asset_digests:subject.profile.assets,lesson_kind:negative?'negative':'positive',source_refs:await Promise.all(['campaign','evidence','report'].map(async kind=>({kind,path:path.join(loaded.dir,kind+'.json'),sha256:contracts.hash(await contracts.read(path.join(loaded.dir,kind+'.json')))}))),fresh_task_evidence:[{common:{campaign_dir:ca.loaded.dir,campaign_digest:ca.loaded.digest,report_digest:fileDigest(ca.report)},candidate:{campaign_dir:cb.loaded.dir,campaign_digest:cb.loaded.digest,report_digest:fileDigest(cb.report)}},...extraPairs]};const input=path.join(source.root,'reuse-input.json');
 if(variant==='relative'){
  const relative=(value:string)=>path.relative(source.root,value);
  for(const ref of Object.values(earlier.runs) as any[])if(ref)ref.run_dir=relative(ref.run_dir);
  for(const ref of Object.values(earlier.claims) as any[])if(ref.run_dir)ref.run_dir=relative(ref.run_dir);
  earlier.reuse_evidence.candidate_dir=relative(earlier.reuse_evidence.candidate_dir);
  for(const ref of earlier.reuse_evidence.source_refs)ref.path=relative(ref.path);
  for(const pair of earlier.reuse_evidence.fresh_task_evidence)for(const side of ['common','candidate'])pair[side].campaign_dir=relative(pair[side].campaign_dir);
  for(const side of ['baseline','candidate'])earlier.ordinary_execution[side].run_dir=relative(earlier.ordinary_execution[side].run_dir);
 }
 await contracts.write(input,earlier);const output=path.join(source.root,'reuse-assessment'),promoted=await assessCommand(loaded,input,output,source.deps);expect(promoted.observations.reuse_assessment.scope).toBe('cross_task');const refs:any[]=[],approvalRoot=path.join(source.root,'promotion-input');await fs.mkdir(approvalRoot);await collect(loaded.dir,'audit/source',approvalRoot,refs);await collect(ca.loaded.dir,'audit/common',approvalRoot,refs);await collect(cb.loaded.dir,'audit/candidate',approvalRoot,refs);await collect(output,'audit/extended',approvalRoot,refs);if(['ordinary','relative'].includes(variant)){await collect(ordinaryDirs.baseline,'audit/ordinary-baseline',approvalRoot,refs);await collect(ordinaryDirs.candidate,'audit/ordinary-candidate',approvalRoot,refs);}const subjectPrefix='audit/subject';await collect(subjectDir,subjectPrefix,approvalRoot,refs);const entry={schema:'millforge-optimization-library-entry/v1',entry_id:'lesson',cue:'Check declared artifacts',lesson:'A scoped whole-profile contrast.',lesson_kind:negative?'negative':'positive',profile_digest:subject.digest,source_candidate_digest:originalSubject.digest,asset_digests:subject.profile.assets,worker_assets:subject.profile.assets.map((a:any)=>({path:a.path,sha256:a.sha256})),compatibility:compatibility(loaded.c),source_refs:refs,fresh_task_evidence:promoted.observations.reuse_assessment.fresh_task_evidence,excluded_task_facts:exclusions};const destination=path.join(source.root,'entry');const a=await approval(source,loaded,promoted,destination,negative?'decline':'adopt','library_entry',entry);const placed=path.join(approvalRoot,'approval.json');await fs.copyFile(a,placed);await adopt(loaded,path.join(output,'report.json'),placed,destination,source.deps);if(negative)await expect(fs.stat(path.join(destination,'profile.json'))).rejects.toThrow();else expect((await source.load(destination)).digest).toBe(subject.digest);const selected=path.join(source.root,'library');await fs.mkdir(selected);await fs.rename(destination,path.join(selected,'lesson'));await expect(validateLibrary(selected,loaded.c,source.deps,1000000,new Set())).resolves.toBeUndefined();return {source,loaded,report,subject,subjectDir,promoted,entry,selected,approvalRoot,ordinaryDirs};}
describe('explicit retention and strict materialization',()=>{
 it('adoption_and_reuse copies only exact plain recommended bytes and rejects stale/repeated decisions',joined(async()=>{const f=await setup(),{loaded,report}=await executed(f),destination=path.join(f.root,'kept'),a=await approval(f,loaded,report,destination);expect((await adopt(loaded,path.join(loaded.dir,'report.json'),a,destination,f.deps)).decision).toBe('adopt');expect((await f.load(destination)).digest).toBe(report.candidate_digests.composed);await expect(adopt(loaded,path.join(loaded.dir,'report.json'),a,destination,f.deps)).rejects.toThrow();const next=path.join(f.root,'declined'),decline=await approval(f,loaded,report,next,'decline');await adopt(loaded,path.join(loaded.dir,'report.json'),decline,next,f.deps);await expect(fs.stat(path.join(next,'profile.json'))).rejects.toThrow();}),30000);
 it('protected_fields refuses reordered tools, foreign assets and changed identity before materialization',async()=>{const f=await setup(),{loaded}=await executed(f),proposal=await contracts.json(path.join(loaded.dir,'proposals/general.json'));proposal.tools=['ls','read'];const file=path.join(f.root,'bad.json');await contracts.write(file,proposal);await expect(candidateCommand(loaded,file,path.join(f.root,'bad-candidate'),f.deps)).rejects.toThrow('order');});
 it('last_task_carryover starts another campaign from its declared common baseline',joined(async()=>{const f=await setup(),{loaded,report}=await executed(f),dest=path.join(f.root,'kept');await adopt(loaded,path.join(loaded.dir,'report.json'),await approval(f,loaded,report,dest),dest,f.deps);const next=await f.prepare('next-capture');expect(next.baseline.digest).toBe(loaded.baseline.digest);expect(next.baseline.digest).not.toBe(report.candidate_digests.composed);expect(next.c.library).toBeNull();}),30000);
 it.each([false,true])('paired_validation_promotion recomputes local positive/negative provenance (negative=%s)',joined(async(negative:boolean)=>{await promotionFixture(negative);}),30000);
 it.each(['tie','mixed','elapsed','same_task','limited_extra'])('fresh-pair promotion refuses %s evidence',joined(async(variant:string)=>{await expect(promotionFixture(false,variant)).rejects.toThrow(variant==='same_task'?'fresh pair identity mismatch':'fresh contrast cannot promote');}),30000);
 it('reuse refuses a subject that adds a tool outside the source allowed subset',joined(async()=>{await expect(promotionFixture(false,'extra_tool')).rejects.toThrow('tool subset/order mismatch');}),30000);
 it('library audit requires local ordinary records even while original external directories exist',joined(async()=>{
  const f=await promotionFixture(false,'ordinary');
  expect(f.promoted.accounting.ordinary_execution.baseline.model_requests.observed).toBe(3);
  await expect(validateLibrary(f.selected,f.loaded.c,f.source.deps,1000000,new Set())).resolves.toBeUndefined();
  const library=path.join(f.source.root,'external-only-library'),copy=path.join(library,'lesson');await fs.mkdir(library);await contracts.copyTree(path.join(f.selected,'lesson'),copy);
  const entry={...f.entry,source_refs:f.entry.source_refs.filter((r:any)=>!r.path.startsWith('audit/ordinary-'))};
  for(const name of ['ordinary-baseline','ordinary-candidate'])await fs.rm(path.join(copy,'audit',name),{recursive:true});
  await fs.writeFile(path.join(copy,'entry.json'),contracts.canonical(entry)+'\n');
  await expect(fs.stat(path.join(f.ordinaryDirs.baseline,'result.json'))).resolves.toBeDefined();
  const before=f.source.calls.length;
  await expect(validateLibrary(library,f.loaded.c,f.source.deps,1000000,new Set())).rejects.toThrow('missing local audit closure');
  const inspected=f.source.calls.slice(before).filter((call:any)=>call.args[1]==='inspect').map((call:any)=>call.args[3]);
  expect(inspected).not.toContain(f.ordinaryDirs.baseline);expect(inspected).not.toContain(f.ordinaryDirs.candidate);
 }),30000);
 it('missing promotion closure leaves no destination or owned staging directory',joined(async()=>{
  const f=await promotionFixture(false),missing=f.entry.source_refs.find((ref:any)=>ref.kind==='closure'&&ref.path.startsWith('audit/source/'));
  expect(missing).toBeDefined();const broken={...f.entry,source_refs:f.entry.source_refs.filter((ref:any)=>ref.path!==missing.path)};
  const destination=path.join(f.source.root,'missing-closure-entry'),a=await approval(f.source,f.loaded,f.promoted,destination,'adopt','library_entry',broken),placed=path.join(f.approvalRoot,'missing-closure-approval.json');await fs.copyFile(a,placed);
  await expect(adopt(f.loaded,path.join(f.source.root,'reuse-assessment/report.json'),placed,destination,f.source.deps)).rejects.toThrow(/ENOENT|missing local audit closure/);
  await expect(fs.stat(destination)).rejects.toMatchObject({code:'ENOENT'});
  expect((await fs.readdir(f.source.root)).filter(name=>name.startsWith('.mfopt-entry-'))).toEqual([]);
 }),30000);
 it('one-slot validation admits baseline only and refuses an expanded work map before spending',async()=>{const f=await setup({validation:true}),loaded=await f.prepare();expect(loaded.map.slots.map((s:any)=>s.work_id)).toEqual(['baseline']);expect(loaded.c.proposal_profile).toBeNull();expect((await runCampaign(loaded.dir,await f.approve(loaded),f.deps)).exit).toBe(0);expect([...f.claims.values()].map((c:any)=>c.work_id)).toEqual(['baseline']);const other=await f.prepare('tampered');const map=await contracts.json(path.join(other.dir,'work-map.json'));map.slots.push({...map.slots[0],work_id:'general_proposal'});await fs.writeFile(path.join(other.dir,'work-map.json'),contracts.canonical(map)+'\n');await expect(runCampaign(other.dir,await f.approve(other,'tampered-approval.json'),f.deps)).rejects.toThrow('work map changed');});
 it('protected baseline assets carry through omitted editable slots, including immutable assets',joined(async()=>{for(const immutableAssets of [false,true]){const f=await setup({protectedAsset:true,noEditableAssets:immutableAssets,immutableAssets}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);for(const kind of ['general','domain','composed']){const candidate=await f.load(path.join(loaded.dir,'candidates',kind));expect(candidate.profile.assets.find((a:any)=>a.path==='context/protected.txt')).toEqual(loaded.baseline.profile.assets.find((a:any)=>a.path==='context/protected.txt'));expect((await fs.readFile(path.join(loaded.dir,'candidates',kind,'context/protected.txt'),'utf8'))).toBe('PROTECTED_ASSET_SENTINEL');}for(const task of f.proposalTasks){const payload=JSON.parse(task.slice(task.indexOf('\n')+1));expect(payload.allowed_edits.baseline.assets.some((a:any)=>a.path==='context/protected.txt')).toBe(false);expect(contracts.canonical(payload.allowed_edits)).not.toContain('PROTECTED_ASSET_SENTINEL');}}}),30000);
 it('library projection runs a fixed snapshot, refuses contamination/scope/size and rechecks promotion exclusions',joined(async()=>{
  const fixture=await promotionFixture(false,'match',['EXCLUDED_TASK_SENTINEL']);const {source,loaded,promoted,entry,selected,approvalRoot}=fixture;
  const next=await setup({sharedBase:source.campaign.base,id:'library-task',taskId:'library-task',taskText:'Use an explicitly selected lesson on a new task.'});next.campaign.library={path:selected,digest:(await contracts.tree(selected)).digest};await fs.writeFile(next.author,contracts.canonical(next.campaign)+'\n');const captured=await next.prepare(),snapshot=(await contracts.tree(path.join(captured.dir,'library'))).digest;
  const run=await runCampaign(captured.dir,await next.approve(captured),next.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);expect(captured.baseline.digest).toBe(loaded.baseline.digest);expect((await contracts.tree(path.join(captured.dir,'library'))).digest).toBe(snapshot);for(const task of next.proposalTasks){const payload=JSON.parse(task.slice(task.indexOf('\n')+1));expect(payload.library).toHaveLength(1);expect(Object.keys(payload.library[0]).sort()).toEqual(['assets','compatibility','cue','lesson','lesson_kind']);expect(task).not.toContain('EXCLUDED_TASK_SENTINEL');expect(payload.library[0].assets.map((a:any)=>a.path)).toEqual(entry.worker_assets.map((a:any)=>a.path));}
  await expect(validateLibrary(selected,loaded.c,{...source.deps,scope:'deployed'},1000000,new Set())).rejects.toThrow('library protection scope mismatch');
  const contaminated=await setup({validation:true,exclusions:['baseline guidance']});await expect(contaminated.prepare()).rejects.toThrow('excluded task fact remains');expect(contaminated.claims.size).toBe(0);
  const badEntry={...entry,cue:'EXCLUDED_TASK_SENTINEL'};const badDestination=path.join(source.root,'contaminated-promotion'),badApproval=await approval(source,loaded,promoted,badDestination,'adopt','library_entry',badEntry),placed=path.join(approvalRoot,'bad-approval.json');await fs.copyFile(badApproval,placed);await expect(adopt(loaded,path.join(source.root,'reuse-assessment/report.json'),placed,badDestination,source.deps)).rejects.toThrow('lesson contains excluded fact');await expect(fs.stat(badDestination)).rejects.toMatchObject({code:'ENOENT'});
  const oversized=path.join(selected,'lesson','000-oversized.txt');await fs.writeFile(oversized,'');await fs.truncate(oversized,67108865);await expect(validateLibrary(selected,loaded.c,source.deps,1000000,new Set())).rejects.toThrow('bounded regular file');await fs.unlink(oversized);
  const many=path.join(source.root,'too-many');await fs.mkdir(many);for(let i=0;i<17;i++)await fs.mkdir(path.join(many,'entry'+i));await expect(validateLibrary(many,loaded.c,source.deps,1000000,new Set())).rejects.toThrow('entry count');
 }),30000);
 it('library_projection_and_provenance refuses unsupported hand-written entries',async()=>{const f=await setup(),loaded=await f.prepare(),lib=path.join(f.root,'library');await fs.mkdir(path.join(lib,'invented'),{recursive:true});await contracts.write(path.join(lib,'invented/entry.json'),{schema:'millforge-optimization-library-entry/v1'});await expect(validateLibrary(lib,loaded.c,f.deps,1000000,new Set())).rejects.toThrow();});
 it('closeout_n1_relative_assess_output_adopt_binds_stored_evidence_and_preserves_claims',joined(async()=>{
  const f=await setup(),{loaded}=await executed(f),input=path.join(loaded.dir,'evidence.json'),before=await contracts.read(input);
  const claims=await Promise.all(loaded.map.slots.map(async(s:any)=>[s.work_id,contracts.hash(await contracts.read(path.join(loaded.dir,'claims',s.work_id+'.json')))]));
  const output=path.join(f.root,'separate-assessment'),report=await assessCommand(loaded,input,output,f.deps),stored=await contracts.json(path.join(output,'evidence.json'));
  expect(report.evidence_digest).toBe(contracts.hash(await contracts.read(path.join(output,'evidence.json'))));
  expect(report.evidence_digest).not.toBe(contracts.hash(before));
  for(const [kind,ref]of Object.entries(stored.runs) as any)if(ref){expect(path.isAbsolute(ref.run_dir)).toBe(true);expect(stored.claims[kind==='baseline'?'baseline':kind+'_probe'].run_dir).toBe(ref.run_dir);}
  expect(await contracts.read(input)).toEqual(before);
  for(const [work,digest]of claims)expect(contracts.hash(await contracts.read(path.join(loaded.dir,'claims',work+'.json')))).toBe(digest);
  const second=path.join(f.root,'second-assessment'),again=await assessCommand(loaded,path.join(output,'evidence.json'),second,f.deps);expect(again).toEqual(report);
  const destination=path.join(f.root,'relative-kept');await adopt(loaded,path.join(output,'report.json'),await approval(f,loaded,report,destination),destination,f.deps);
  expect((await f.load(destination)).digest).toBe(report.candidate_digests[report.recommendation]);
  stored.runs.general.run_dir=path.join(f.root,'missing-run');stored.claims.general_probe.run_dir=stored.runs.general.run_dir;
  await fs.writeFile(path.join(output,'evidence.json'),contracts.canonical(stored)+'\n');
  const bad=path.join(f.root,'changed-ref-destination');await expect(adopt(loaded,path.join(output,'report.json'),await approval(f,loaded,report,bad),bad,f.deps)).rejects.toThrow();await expect(fs.stat(bad)).rejects.toMatchObject({code:'ENOENT'});
 }),30000);
 it('closeout_n1_nested_relative_reuse_and_ordinary_paths_retain_original_meaning',joined(async()=>{
  const f=await promotionFixture(false,'relative'),file=path.join(f.source.root,'reuse-assessment/evidence.json'),stored=await contracts.json(file);
  expect(path.isAbsolute(stored.reuse_evidence.candidate_dir)).toBe(true);
  expect(stored.reuse_evidence.source_refs.every((ref:any)=>path.isAbsolute(ref.path))).toBe(true);
  for(const pair of stored.reuse_evidence.fresh_task_evidence)for(const side of ['common','candidate'])expect(path.isAbsolute(pair[side].campaign_dir)).toBe(true);
  for(const side of ['baseline','candidate'])expect(path.isAbsolute(stored.ordinary_execution[side].run_dir)).toBe(true);
  expect(f.promoted.evidence_digest).toBe(contracts.hash(await contracts.read(file)));
  expect(f.promoted.accounting.ordinary_execution.baseline.model_requests.observed).toBe(3);
  await expect(validateLibrary(f.selected,f.loaded.c,f.source.deps,1000000,new Set())).resolves.toBeUndefined();
 }),30000);
 it('closeout_n1_n2_cancelled_partial_evidence_fresh_assess_and_inconclusive_decline',joined(async()=>{
  const f=await setup(),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const result=await original(spec,signal,cleanup);if(spec.args[0]==='run'&&spec.args[spec.args.indexOf('--name')+1]==='mfopt-'+loaded.c.campaign_id+'-general_proposal'){controller.abort();return {...result,cancelled:true};}return result;
  }});
  expect(run).toMatchObject({exit:130,status:'cancelled'});
  const output=path.join(f.root,'cancelled-assessment'),report=await assessCommand(loaded,path.join(loaded.dir,'evidence.json'),output,f.deps);
  expect(report.recommendation).toBe('inconclusive');expect(report.accounting.search.model_requests.observed).toBe(2);
  const destination=path.join(f.root,'inconclusive-decline');await adopt(loaded,path.join(output,'report.json'),await approval(f,loaded,report,destination,'decline'),destination,f.deps);
  expect(await fs.readdir(destination)).toEqual(['adoption.json']);
  expect(await contracts.json(path.join(destination,'adoption.json'))).toMatchObject({
   schema:'millforge-optimization-adoption/v1',decision:'decline',retention:'task_specific',candidate_digest:null,library_entry:null,
   campaign_digest:loaded.digest,report_digest:fileDigest(report),destination
  });
 }),30000);

 it('closeout_n1_review_malformed_path_shapes_are_typed_and_leave_sources_unchanged',joined(async()=>{
  const f=await setup(),{loaded}=await executed(f),source=path.join(loaded.dir,'evidence.json'),before=await contracts.read(source),base=await contracts.json(source);
  const reuse=()=>({candidate_dir:path.join(loaded.dir,'candidates/general'),candidate_digest:'0'.repeat(64),source_candidate_digest:'0'.repeat(64),asset_digests:[],lesson_kind:'positive',source_refs:[],fresh_task_evidence:[]});
  const ordinary=(value:any)=>({run_dir:value,capture_sha256:'0'.repeat(64),result_sha256:'0'.repeat(64),trace_sha256:'0'.repeat(64)});
  const mutations=[
   (e:any)=>{delete e.runs;},
   (e:any)=>{e.runs=[];},
   (e:any)=>{e.claims.baseline.run_dir=7;},
   (e:any)=>{e.reuse_evidence={...reuse(),source_refs:[{kind:'campaign',path:7,sha256:'0'.repeat(64)}]};},
   (e:any)=>{e.reuse_evidence={...reuse(),fresh_task_evidence:[{common:7,candidate:{campaign_dir:'/candidate',campaign_digest:'0'.repeat(64),report_digest:'0'.repeat(64)}}]};},
   (e:any)=>{e.ordinary_execution={baseline:ordinary(7),candidate:ordinary('/candidate')};}
  ];
  for(let index=0;index<mutations.length;index++){
   const evidence=structuredClone(base);mutations[index]!(evidence);
   const input=path.join(f.root,'malformed-'+index+'.json'),output=path.join(f.root,'malformed-output-'+index);await contracts.write(input,evidence);
   const raw=await contracts.read(input);
   await expect(assessCommand(loaded,input,output,f.deps)).rejects.toMatchObject({code:'INVALID_INPUT'});
   await expect(assess(loaded,input,f.deps,1000000)).rejects.toMatchObject({code:'INVALID_INPUT'});
   await expect(fs.stat(output)).rejects.toMatchObject({code:'ENOENT'});expect(await contracts.read(input)).toEqual(raw);
  }
  expect(await contracts.read(source)).toEqual(before);
 }),30000);
 it('closeout_n1_review_assessment_refusal_removes_only_fresh_owned_output_and_allows_retry',joined(async()=>{
  const f=await setup(),{loaded}=await executed(f),source=path.join(loaded.dir,'evidence.json'),before=await contracts.read(source),evidence=await contracts.json(source);
  for(const ref of Object.values(evidence.runs) as any[])if(ref)ref.run_dir=path.join(loaded.dir,ref.run_dir);
  for(const ref of Object.values(evidence.claims) as any[])if(ref.run_dir)ref.run_dir=path.join(loaded.dir,ref.run_dir);
  evidence.runs.general.run_dir=evidence.claims.general_probe.run_dir=path.join(f.root,'missing-run');
  const input=path.join(f.root,'missing-evidence.json'),output=path.join(f.root,'fresh-assess-output');await contracts.write(input,evidence);
  await expect(assessCommand(loaded,input,output,f.deps)).rejects.toMatchObject({code:'ENOENT'});
  await expect(fs.stat(output)).rejects.toMatchObject({code:'ENOENT'});expect(await contracts.read(source)).toEqual(before);
  const report=await assessCommand(loaded,source,output,f.deps);expect(report.recommendation).toBe('composed');
  const stored=await contracts.read(path.join(output,'evidence.json')),saved=await contracts.read(path.join(output,'report.json'));
  await expect(assessCommand(loaded,source,output,f.deps)).rejects.toMatchObject({code:'EEXIST'});
  expect(await contracts.read(path.join(output,'evidence.json'))).toEqual(stored);expect(await contracts.read(path.join(output,'report.json'))).toEqual(saved);
 }),30000);

 it('closeout_n1_assess_refusal_preserves_replacement_output_identity',joined(async()=>{
  const f=await setup(),{loaded}=await executed(f),source=path.join(loaded.dir,'evidence.json'),before=await contracts.read(source),original=f.deps.runProcess;
  const output=path.join(f.root,'swapped-assessment'),parked=path.join(f.root,'parked-assessment'),probeRun=path.join(loaded.dir,'runs/baseline');
  let swapped=false,replacementDev=0,replacementIno=0;
  const deps={...f.deps,runProcess:async(spec:any,signal?:AbortSignal,cleanup?:()=>number)=>{
   const reply=await original(spec,signal,cleanup);
   if(!swapped&&spec.args[0]===loaded.c.base.cli_path&&spec.args[1]==='inspect'&&spec.args[2]==='--run'&&spec.args[3]===probeRun){
    swapped=true;await fs.rename(output,parked);await fs.mkdir(output);await contracts.write(path.join(output,'replacement.txt'),'PRESERVE_REPLACEMENT');
    const replacement=await fs.lstat(output);replacementDev=replacement.dev;replacementIno=replacement.ino;
    return {...reply,exit:1};
   }
   return reply;
  }};
  await expect(assessCommand(loaded,source,output,deps)).rejects.toMatchObject({code:'IO_ERROR'});
  expect(swapped).toBe(true);expect(await fs.readdir(output)).toEqual(['replacement.txt']);expect((await contracts.read(path.join(output,'replacement.txt'))).toString()).toBe('PRESERVE_REPLACEMENT');
  const replacement=await fs.lstat(output),created=await fs.lstat(parked);expect(replacement.dev).toBe(replacementDev);expect(replacement.ino).toBe(replacementIno);expect(replacement.ino).not.toBe(created.ino);
  expect(await contracts.read(source)).toEqual(before);await expect(fs.stat(path.join(parked,'evidence.json'))).resolves.toBeDefined();
 }),30000);

});
