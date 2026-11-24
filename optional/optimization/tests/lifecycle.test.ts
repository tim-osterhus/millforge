import {describe,it,expect,afterEach,vi} from 'vitest';
import {spawn} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {performance} from 'node:perf_hooks';
import {existsSync} from 'node:fs';
vi.mock('node:child_process',async importOriginal=>({...await importOriginal<typeof import('node:child_process')>(),spawn:vi.fn()}));import fs from 'node:fs/promises';import path from 'node:path';import {tmpdir} from 'node:os';
import * as contracts from '../src/contracts.js';import {hostAdmission,hostSpec,matchQualification,qualify,stdoutResult} from '../src/lifecycle.js';import {prepareCampaign,inspectCampaign} from '../src/store.js';import {runCampaign} from '../src/method.js';import {authAdmission,accessEnvironment,workerSpec,evaluatorSpec,processSpec,runProcess,productionDependencies} from '../src/lifecycle.js';import {makeHarness} from './fixtures/fake-process.mjs';
const roots:string[]=[];afterEach(async()=>{vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();for(const p of roots.splice(0))await fs.rm(p,{recursive:true,force:true});});async function setup(options:any={}){const root=await fs.mkdtemp(path.join(tmpdir(),'mfopt-life-'));roots.push(root);return makeHarness(root,{...contracts,prepareCampaign},options) as Promise<any>;}
describe('ownership and finite admission',()=>{
 it('claim_aftermath forbids local rerun and broker replay across fresh approved roots',async()=>{const f=await setup(),loaded=await f.prepare();expect((await runCampaign(loaded.dir,await f.approve(loaded),f.deps)).exit).toBe(0);await expect(runCampaign(loaded.dir,path.join(f.root,'approval.json'),f.deps)).rejects.toThrow('already started');const second=await f.prepare('copied');const count=f.calls.filter((s:any)=>s.args[0]==='run').length;expect((await runCampaign(second.dir,await f.approve(second,'approval2.json'),f.deps)).exit).toBe(3);expect(f.calls.filter((s:any)=>s.args[0]==='run')).toHaveLength(count);expect((await inspectCampaign(loaded)).status).toBe('completed');});
 it('cancel_and_uncertainty stops independent domain_probe after uncertain general_probe',async()=>{const f=await setup({uncertainAt:'general_probe'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(3);expect(run.status).toBe('uncertain');expect([...f.claims.values()].map((x:any)=>x.work_id)).not.toContain('domain_probe');expect(run.report.recommendation).toBe('inconclusive');});
 it('stable_limits_and_fit preserves paired request limits despite unequal admission overhead',async()=>{const a=await setup({id:'validation-a',validation:true,admissionMs:2}),b=await setup({id:'validation-b',validation:true,admissionMs:17}),la=await a.prepare(),lb=await b.prepare();expect((await runCampaign(la.dir,await a.approve(la),a.deps)).exit).toBe(0);expect((await runCampaign(lb.dir,await b.approve(lb),b.deps)).exit).toBe(0);const ra=await contracts.json(path.join(la.dir,'requests/baseline.json')),rb=await contracts.json(path.join(lb.dir,'requests/baseline.json'));expect(ra.limits).toEqual(rb.limits);expect(ra.limits.model_timeout_seconds).toBe(55);});
 it('repeated cancellation retains 130 after proven closure and signal-free bounded saved-run inspection',async()=>{
  const f=await setup(),loaded=await f.prepare(),controller=new AbortController(),original=f.deps.runProcess;
  let cancelled=false,cleanupDeadline:number|undefined;
  const validations:{signal:AbortSignal|undefined;deadline:number}[]=[];
  const deps={...f.deps,signal:controller.signal,runProcess:async(spec:any,signal?:AbortSignal)=>{
   // The shared fake rejects aborted signals just like production runProcess.
   if(cancelled&&spec.executable===loaded.c.execution.runtime_path&&spec.args[0]==='inspect')cleanupDeadline??=spec.deadline;
   if(cancelled&&spec.args[1]==='inspect')validations.push({signal,deadline:spec.deadline});
   const result=await original(spec,signal);
   if(!cancelled&&spec.args[0]==='run'&&spec.args.includes('mfopt-fixture-baseline')){
    cancelled=true;controller.abort();controller.abort();
   }
   return result;
  }};
  const run=await runCampaign(loaded.dir,await f.approve(loaded),deps);
  expect(controller.signal.aborted).toBe(true);expect(run.exit).toBe(130);expect(run.status).toBe('cancelled');
  expect(f.claims.size).toBe(1);expect(run.report.recommendation).toBe('inconclusive');
  expect(cleanupDeadline).toBeDefined();expect(validations[0]).toEqual({signal:undefined,deadline:cleanupDeadline});
  expect(f.calls.filter((spec:any)=>spec.executable===loaded.c.execution.runtime_path&&spec.args[0]==='run')).toHaveLength(1);
  const closure=await contracts.json(path.join(loaded.dir,'receipts/baseline-closure.json'));
  expect(closure.status).toBe('closed');expect(closure.worker.process).toBe('closed');expect(closure.worker.container).toBe('closed');
  expect(closure.broker).toBe('closed');expect(closure.writers).toBe('closed');expect(closure.evaluator.process).toBe('not_started');
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));
  expect(evidence.claims.baseline.result_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect((await contracts.json(path.join(loaded.dir,'receipts/campaign-timing.json'))).closed).toBe(true);
  const inspected=await inspectCampaign(loaded);
  expect(inspected.status).toBe('cancelled');expect(inspected.dispatch_status).toBe('cancelled');expect(inspected.exit_code).toBe(130);
 });
 it('default deployed admission refuses test qualification before a claim',async()=>{const f=await setup(),loaded=await f.prepare();await expect(runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,scope:'deployed'})).rejects.toThrow('scope');expect(f.claims.size).toBe(0);});
 it('checks auth/endpoint without changing OAuth or reasoning',()=>{const oauth={model:{base_url:'https://api.openai.com/v1',auth:{kind:'oauth',provider:'openai'}}};expect(()=>authAdmission(oauth,oauth.model.base_url,{kind:'network',env_name:null})).toThrow('unqualified');expect(()=>authAdmission({model:{base_url:'x',auth:{kind:'none'}}},'other',{kind:'network',env_name:null})).toThrow('endpoint');expect(()=>accessEnvironment({model:{auth:{kind:'env',name:'A'}}},{campaign_digest:'x',request_id:'r',route_digest:'y',access:{env_name:'B',value:'token'}},'x','r','y')).toThrow();});
 it('evaluator timeout is bounded and never yields an adoption recommendation',async()=>{const f=await setup({evaluatorTimeout:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit).toBe(124);expect(run.report.recommendation).toBe('inconclusive');expect(f.claims.size).toBe(1);});
});

// Literal launch-contract checks certify argument construction only, not deployed containment.
describe('launch_contract argument boundaries',()=>{
 const captured={campaign_id:'launch-contract',base:{prefix_path:'/owned/base',cli_path:'/owned/base/dist/node/src/cli.js'},task:{evaluator_path:'/operator/evaluator'},execution:{runtime_path:'/owned/bin/podman',network:{name:'captured-internal'},image_digest:'sha256:'+'a'.repeat(64),evaluator_image_digest:'sha256:'+'b'.repeat(64)}};
 it('worker uses the captured immutable image, internal network and exact protected/writable mounts',()=>{
  const spec=workerSpec(captured,'baseline','/owned/profile','/owned/request.json','/owned/workspace','/owned/output','/owned/worker.cid',123456,{CLAIM_ACCESS:'synthetic-scoped-token'});
  expect(spec).toEqual({executable:'/owned/bin/podman',cwd:'/owned/output',deadline:123456,maxOutput:96*1024*1024,env:{PATH:path.dirname(process.execPath),CLAIM_ACCESS:'synthetic-scoped-token'},args:[
   'run','--pull=never','--name','mfopt-launch-contract-baseline','--cidfile','/owned/worker.cid',
   '--rm=false','--read-only','--userns=keep-id','--cap-drop=ALL','--security-opt=no-new-privileges',
   '--network','captured-internal','--pids-limit=128','--memory=2g','--memory-swap=2g','--cpus=4',
   '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--workdir','/workspace',
   '--volume','/owned/base:/millforge:ro','--volume','/owned/profile:/profile:ro',
   '--volume','/owned/request.json:/request.json:ro','--volume','/owned/workspace:/workspace:rw',
   '--volume','/owned/output:/output:rw','--env','CLAIM_ACCESS',
   'sha256:'+'a'.repeat(64),'/usr/local/bin/node','/millforge/dist/node/src/cli.js',
   'run','--profile','/profile','--request','/request.json','--output-dir','/output/run'
  ]});
  expect(spec.args.join(' ')).not.toContain('synthetic-scoped-token');
  expect(spec.args).not.toContain('--privileged');expect(spec.args).not.toContain('--auth-home');
  // An exact env/mount allowlist excludes ambient HOME, cloud credentials, auth dirs and host sockets.
  expect(Object.keys(spec.env).sort()).toEqual(['CLAIM_ACCESS','PATH']);
  expect(spec.args.filter((_,i)=>spec.args[i-1]==='--volume')).toHaveLength(5);
 });
 it('evaluator has no network or broker access and mounts only stopped deliverables, evaluator and owned receipts',()=>{
  const spec=evaluatorSpec(captured,'baseline','/owned/stopped-deliverables','/owned/evaluation-output','/owned/evaluation.cid','checks/run.mjs',234567);
  expect(spec).toEqual({executable:'/owned/bin/podman',cwd:'/owned/evaluation-output',deadline:234567,maxOutput:96*1024*1024,env:{PATH:path.dirname(process.execPath)},args:[
   'run','--pull=never','--name','mfopt-launch-contract-baseline-eval','--cidfile','/owned/evaluation.cid',
   '--rm=false','--read-only','--userns=keep-id','--cap-drop=ALL','--security-opt=no-new-privileges',
   '--network=none','--pids-limit=128','--memory=2g','--memory-swap=2g','--cpus=4',
   '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--workdir','/deliverables',
   '--volume','/operator/evaluator:/evaluator:ro','--volume','/owned/stopped-deliverables:/deliverables:ro',
   '--volume','/owned/evaluation-output:/receipts:rw','sha256:'+'b'.repeat(64),
   '/usr/local/bin/node','/evaluator/checks/run.mjs','--deliverables','/deliverables','--output','/receipts/evaluation.json'
  ]});
  expect(spec.args).not.toContain('--env');expect(spec.env).not.toHaveProperty('CLAIM_ACCESS');
  expect(spec.args).not.toContain('--privileged');expect(spec.args).not.toContain('--auth-home');
 });
 it('process arguments keep the original deadline, byte ceiling and explicit environment without a shell wrapper',()=>{
  expect(processSpec('/owned/bin/podman',['version','--format','json'],'/owned/bin',345678)).toEqual({
   executable:'/owned/bin/podman',args:['version','--format','json'],cwd:'/owned/bin',deadline:345678,
   env:{PATH:path.dirname(process.execPath)},maxOutput:96*1024*1024
  });
 });
 it.each([false,true])('campaign dispatch keeps worker/evaluator boundaries with env auth %s',async envAuth=>{
  const f=await setup({envAuth}),loaded=await f.prepare(),started=f.deps.now();
  expect((await runCampaign(loaded.dir,await f.approve(loaded),f.deps)).exit).toBe(0);
  const launches=f.calls.filter((spec:any)=>spec.executable===loaded.c.execution.runtime_path&&spec.args[0]==='run');
  const workers=launches.filter((spec:any)=>!spec.args.includes('--network=none'));
  const evaluators=launches.filter((spec:any)=>spec.args.includes('--network=none'));
  expect(workers).toHaveLength(7);expect(evaluators).toHaveLength(4);
  for(const spec of launches){
   expect(Number.isFinite(spec.deadline)).toBe(true);expect(spec.deadline).toBeGreaterThan(0);
   expect(spec.deadline).toBeLessThanOrEqual(started+loaded.c.budget.elapsed_seconds*1000);
   expect(spec.maxOutput).toBe(96*1024*1024);expect(spec.args).toContain('--pull=never');
   expect(spec.args).toContain('--read-only');expect(spec.args).toContain('--cap-drop=ALL');
   expect(spec.args).toContain('--security-opt=no-new-privileges');expect(spec.args).not.toContain('--privileged');
   expect(spec.args).not.toContain('--auth-home');
  }
  for(const spec of workers){
   const work=spec.args[spec.args.indexOf('--name')+1].slice(('mfopt-'+loaded.c.campaign_id+'-').length);
   const mounts=spec.args.filter((_:string,i:number)=>spec.args[i-1]==='--volume');
   expect(mounts).toHaveLength(5);
   expect(mounts).toContain(loaded.c.base.prefix_path+':/millforge:ro');
   expect(mounts.filter((value:string)=>value.endsWith(':ro'))).toHaveLength(3);
   expect(mounts.filter((value:string)=>value.endsWith(':rw'))).toHaveLength(2);
   for(const destination of ['/profile:ro','/request.json:ro','/workspace:rw','/output:rw'])expect(mounts.filter((value:string)=>value.endsWith(':'+destination))).toHaveLength(1);
   expect(mounts.some((value:string)=>value.includes(':/evaluator:')||value.includes(':/receipts:'))).toBe(false);
   expect(spec.args[spec.args.indexOf('--network')+1]).toBe(loaded.c.execution.network.name);
   expect(spec.args).toContain(loaded.c.execution.image_digest);
   expect(spec.args.slice(-8)).toEqual(['/millforge/bin/base.mjs','run','--profile','/profile','--request','/request.json','--output-dir','/output/run']);
   expect(spec.env).toEqual({PATH:path.dirname(process.execPath),...(envAuth?{CLAIM_ACCESS:'scoped-'+work}:{})});
   expect(spec.args.filter((_:string,i:number)=>spec.args[i-1]==='--env')).toEqual(envAuth?['CLAIM_ACCESS']:[]);
   if(envAuth)expect(spec.args.join(' ')).not.toContain('scoped-'+work);
  }
  for(const spec of evaluators){
   const mounts=spec.args.filter((_:string,i:number)=>spec.args[i-1]==='--volume');
   expect(mounts).toHaveLength(3);expect(mounts).toContain(loaded.c.task.evaluator_path+':/evaluator:ro');
   expect(mounts.filter((value:string)=>value.endsWith(':/deliverables:ro'))).toHaveLength(1);
   expect(mounts.filter((value:string)=>value.endsWith(':/receipts:rw'))).toHaveLength(1);
   expect(spec.env).toEqual({PATH:path.dirname(process.execPath)});expect(spec.args).not.toContain('--env');
   expect(spec.args).not.toContain('--network');expect(spec.args).toContain(loaded.c.execution.evaluator_image_digest);
   expect(spec.args.slice(-5)).toEqual(['/evaluator/run.mjs','--deliverables','/deliverables','--output','/receipts/evaluation.json']);
  }
 });
});

// No real child, network peer or process-group signal is used by these clocked cases.
describe('C4 shared cleanup deadline',()=>{
 function clockedChild(){
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});let now=0,groupPresent=true;
  vi.spyOn(performance,'now').mockImplementation(()=>now);
  const child=Object.assign(new EventEmitter(),{pid:2147483647,stdout:new PassThrough(),stderr:new PassThrough()});
  vi.mocked(spawn).mockReset().mockReturnValue(child as unknown as ReturnType<typeof spawn>);
  const signals:Parameters<typeof process.kill>[1][]=[];
  vi.spyOn(process,'kill').mockImplementation((_pid,signal)=>{
   if(signal===0&&!groupPresent)throw Object.assign(new Error('absent'),{code:'ESRCH'});
   if(signal!==0)signals.push(signal);return true;
  });
  return {child,signals,async advance(ms:number){now+=ms;await vi.advanceTimersByTimeAsync(ms);},reap(){groupPresent=false;child.stdout.end();child.stderr.end();child.emit('close',0,null);}};
 }
 it('stalled early cancellation ends at one shared bound, not the long worker deadline',async()=>{
  const f=clockedChild(),controller=new AbortController(),bound=vi.fn(()=>5000);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),controller.signal,bound);
  let returned=false;void pending.then(()=>{returned=true;});
  controller.abort();controller.abort();
  expect(bound).toHaveBeenCalledTimes(1);expect(f.signals).toEqual(['SIGTERM']);
  await f.advance(1250);expect(f.signals).toEqual(['SIGTERM','SIGKILL']);
  await f.advance(3749);expect(returned).toBe(false);
  await f.advance(1);const result=await pending;
  expect(result).toMatchObject({closed:false,cancelled:true,timedOut:false});
  expect(bound).toHaveBeenCalledTimes(1);expect(f.signals).toEqual(['SIGTERM','SIGKILL','SIGKILL']);
  // A late close/drain cannot retroactively turn the returned uncertain result into closure.
  f.reap();expect(result.closed).toBe(false);
 });
 it('standalone cancellation captures five seconds once without a campaign getter',async()=>{
  const f=clockedChild(),controller=new AbortController();
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),controller.signal);
  controller.abort();await f.advance(5000);
  expect(await pending).toMatchObject({closed:false,cancelled:true});
 });
 it('clips the shared stop deadline to the earlier original process ceiling',async()=>{
  const f=clockedChild(),controller=new AbortController(),bound=vi.fn(()=>5000);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',1000),controller.signal,bound);
  let returned=false;void pending.then(()=>{returned=true;});controller.abort();
  await f.advance(999);expect(returned).toBe(false);await f.advance(1);
  expect(await pending).toMatchObject({closed:false,cancelled:true,timedOut:false});expect(bound).toHaveBeenCalledTimes(1);
 });
 it('an already expired shared bound grants no new cleanup allowance',async()=>{
  const f=clockedChild(),controller=new AbortController();
  await f.advance(100);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),controller.signal,()=>50);
  controller.abort();await f.advance(0);
  expect(await pending).toMatchObject({closed:false,cancelled:true});
 });
 it('leader exit without a drained close remains uncertain at the same shared deadline',async()=>{
  const f=clockedChild(),controller=new AbortController();
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),controller.signal,()=>800);
  controller.abort();f.child.emit('exit',0,null);await f.advance(800);
  expect(await pending).toMatchObject({closed:false,cancelled:true});
 });
 it('only actual reaped/drained close plus an absent owned group proves closed cancellation',async()=>{
  const f=clockedChild(),controller=new AbortController();
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),controller.signal,()=>5000);
  controller.abort();await f.advance(100);f.reap();
  expect(await pending).toMatchObject({closed:true,cancelled:true,exit:0});
  await f.advance(10000);expect(f.signals).toEqual(['SIGTERM']);
 });
 it('a close event with a surviving owned group cannot claim physical closure',async()=>{
  const f=clockedChild(),bound=vi.fn(()=>5000);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),undefined,bound);
  f.child.stdout.end();f.child.stderr.end();f.child.emit('close',0,null);
  expect(await pending).toMatchObject({closed:false,exit:0});expect(f.signals).toEqual(['SIGKILL']);
 });
 it('an unrequested normal close never captures the terminal episode getter',async()=>{
  const f=clockedChild(),bound=vi.fn(()=>5000);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',180000),undefined,bound);
  f.reap();expect(await pending).toMatchObject({closed:true,cancelled:false});expect(bound).not.toHaveBeenCalled();
 });
 it('normal timeout and repeated abort share the first captured stop deadline',async()=>{
  const f=clockedChild(),controller=new AbortController(),bound=vi.fn(()=>3000);
  const pending=runProcess(processSpec('/synthetic/worker',[],'/synthetic',3000),controller.signal,bound);
  await f.advance(1500);controller.abort();controller.abort();await f.advance(1500);
  expect(await pending).toMatchObject({closed:false,timedOut:true,cancelled:true});expect(bound).toHaveBeenCalledTimes(1);
 });
 it.each([{constructor:10000,override:400,bound:400},{constructor:700,override:20000,bound:700},{constructor:10000,override:20000,bound:2000},{constructor:10000,override:undefined,bound:2000}])('broker close tightens deadline without widening constructor or two-second cap: %j',async row=>{
  const f=clockedChild();
  const fetch=vi.fn((_url:string,options:RequestInit)=>new Promise((_resolve,reject)=>{options.signal!.addEventListener('abort',()=>reject(new Error('bounded close')),{once:true});}));
  vi.stubGlobal('fetch',fetch);
  const broker=productionDependencies().broker({broker:{endpoint:'http://127.0.0.1:1'}},row.constructor);
  let returned=false;
  const pending=broker.close('synthetic-id',row.override);void pending.catch(()=>{returned=true;});
  const rejection=expect(pending).rejects.toThrow('bounded close');
  await f.advance(row.bound-1);expect(returned).toBe(false);await f.advance(1);await rejection;
  expect(fetch).toHaveBeenCalledTimes(1);expect(fetch.mock.calls[0]![0]).toBe('http://127.0.0.1:1/v1/claims/synthetic-id/close');
  expect(fetch.mock.calls[0]![1]).toMatchObject({method:'POST',redirect:'error',body:'{}'});
 });
 it('expired broker-close bound refuses before any request',async()=>{
  clockedChild();const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
  const broker=productionDependencies().broker({broker:{endpoint:'http://127.0.0.1:1'}},10000);
  await expect(broker.close('synthetic-id',0)).rejects.toMatchObject({code:'TIMEOUT'});expect(fetch).not.toHaveBeenCalled();
 });
});
describe('O2-E2 versions, host_generate and parent-held Result',()=>{
 it('v2 execution is strict and new prepare refuses v1 before creating output',async()=>{
  const f=await setup();expect(contracts.campaign(f.campaign).schema).toBe('millforge-optimization-campaign/v2');
  for(const mutate of [(c:any)=>{c.execution.extra=1;},(c:any)=>{delete c.execution.host_environment;},(c:any)=>{c.execution.proposal_execution={kind:'host_generate'};},(c:any)=>{c.execution.proposal_execution={kind:'host_generate',auth_home:'relative'};},(c:any)=>{c.execution.host_environment={HOME:'/h',XDG_RUNTIME_DIR:'/r',PATH:'/p',EXTRA:'x'};},(c:any)=>{c.execution.proposal_execution=null;}]){const c=structuredClone(f.campaign);mutate(c);expect(()=>contracts.campaign(c)).toThrow();}
  const legacy=await setup({legacyV1:true,id:'legacy'});expect(contracts.campaign(legacy.campaign).schema).toBe('millforge-optimization-campaign/v1');
  await expect(legacy.prepare()).rejects.toMatchObject({code:'UNSUPPORTED_VERSION'});await expect(fs.lstat(path.join(legacy.root,'captured'))).rejects.toMatchObject({code:'ENOENT'});expect(legacy.claims.size).toBe(0);
 });
 it('shared v2 qualification matcher refuses evaluator, proposer, host-fact and scope swaps',async()=>{
  const f=await setup(),q=await contracts.json(f.campaign.execution.qualification.path);expect(matchQualification(q,f.campaign,'test',null).schema).toBe('millforge-optimization-qualification/v2');
  for(const mutate of [(x:any)=>{x.evaluator.evaluator_id='other';},(x:any)=>{x.evaluator.evaluator_digest='0'.repeat(64);},(x:any)=>{x.evaluator.mode='executed';},(x:any)=>{x.proposal_profile_digest='0'.repeat(64);},(x:any)=>{x.host_facts.rootless=false;},(x:any)=>{x.host_facts.network_sha256='0'.repeat(64);},(x:any)=>{x.execution.image_digest='sha256:'+'0'.repeat(64);},(x:any)=>{x.schema='millforge-optimization-qualification/v1';}]){const x=structuredClone(q);mutate(x);expect(()=>matchQualification(x,f.campaign,'test',null)).toThrow();}
  expect(()=>matchQualification(q,f.campaign,'deployed',null)).toThrow();expect(()=>matchQualification(q,f.campaign,'test','0'.repeat(64))).toThrow();
  const swapped=await setup({id:'swapped',qualificationPatch:{evaluator:{evaluator_id:'checks',evaluator_digest:'0'.repeat(64),mode:'data_only'}}}),loaded=await swapped.prepare();
  await expect(runCampaign(loaded.dir,await swapped.approve(loaded),swapped.deps)).rejects.toThrow('evaluator');expect(swapped.claims.size).toBe(0);
 });
 it('v1 run refuses before claims or run publication',async()=>{
  const f=await setup({legacyV1:true,id:'legacy-run'}),dir=path.join(f.root,'historical-prepared');await fs.mkdir(dir);
  const baseline=await f.load(f.campaign.baseline.profile_path),proposer=await f.load(f.campaign.proposal_profile.profile_path),map=contracts.workMap(f.campaign,baseline.profile,proposer.profile);
  await contracts.write(path.join(dir,'campaign.json'),f.campaign);await contracts.write(path.join(dir,'work-map.json'),map);
  const loaded={dir,c:f.campaign,digest:map.campaign_digest};
  await expect(runCampaign(dir,await f.approve(loaded),f.deps)).rejects.toMatchObject({code:'UNSUPPORTED_VERSION'});
  expect(f.claims.size).toBe(0);await expect(fs.lstat(path.join(dir,'run.json'))).rejects.toMatchObject({code:'ENOENT'});
 });
 it('captured host environment reaches every Podman client without container or host-teacher leakage',async()=>{
  const f=await setup({hostGenerate:true,envAuth:true}),host={HOME:'/owned/home',XDG_RUNTIME_DIR:'/owned/runtime',PATH:'/owned/bin'};
  f.campaign.execution.host_environment=host;
  // The shared env-auth fixture applies env auth to both profiles; host_generate
  // deliberately allows only an auth:none fake or the official OAuth teacher.
  const proposerFile=path.join(f.campaign.proposal_profile.profile_path,'profile.json'),proposer=await contracts.json(proposerFile);proposer.model.auth={kind:'none'};
  await fs.writeFile(proposerFile,contracts.canonical(proposer)+'\n');f.campaign.proposal_profile.profile_digest=(await f.load(f.campaign.proposal_profile.profile_path)).digest;
  const qfile=f.campaign.execution.qualification.path,q=await contracts.json(qfile);q.execution.host_environment=host;q.proposal_profile_digest=f.campaign.proposal_profile.profile_digest;
  await fs.writeFile(qfile,contracts.canonical(q)+'\n');f.campaign.execution.qualification.sha256=contracts.hash(await fs.readFile(qfile));
  await fs.writeFile(f.author,contracts.canonical(f.campaign)+'\n');
  // Exercise the exact owned-container stop path as well as normal inspect/removal.
  const original=f.deps.runProcess;let firstInspect=true;
  f.deps.runProcess=async(spec:any,signal?:AbortSignal)=>{const r=await original(spec,signal);if(spec.executable===f.campaign.execution.runtime_path&&spec.args[0]==='inspect'&&firstInspect){firstInspect=false;const value=JSON.parse(r.stdout);value[0].State.Running=true;r.stdout=contracts.canonical(value);}return r;};
  const loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);
  const clients=f.calls.filter((call:any)=>call.executable===f.campaign.execution.runtime_path);
  expect(new Set(clients.map((call:any)=>call.args[0]))).toEqual(new Set(['version','info','network','image','run','inspect','kill','rm']));
  for(const call of clients){
   expect(call.env).toMatchObject(host);expect(Object.keys(call.env).every(k=>['HOME','XDG_RUNTIME_DIR','PATH','CLAIM_ACCESS'].includes(k))).toBe(true);
   const forwarded=call.args.filter((_:string,i:number)=>call.args[i-1]==='--env');expect(forwarded.every((key:string)=>key==='CLAIM_ACCESS')).toBe(true);
   if(call.args.includes('--network=none')){expect(forwarded).toEqual([]);expect(call.env).not.toHaveProperty('CLAIM_ACCESS');}
  }
  expect(f.hostRuns).toHaveLength(3);
  for(const call of f.calls.filter((call:any)=>call.executable!==f.campaign.execution.runtime_path))expect(call.env).toEqual({PATH:path.dirname(process.execPath)});
 });
 it.each([false,true])('v2 accepts exact immutable OCI config ID with prefixed inspect Id %s',async prefixed=>{
  const f=await setup({validation:true}),original=f.deps.runProcess;
  f.deps.runProcess=async(spec:any,signal?:AbortSignal)=>{const r=await original(spec,signal);if(spec.executable===f.campaign.execution.runtime_path&&spec.args[0]==='image'){const value=spec.args.at(-1);r.stdout=contracts.canonical([{Id:prefixed?value:value.slice(7),Digest:'sha256:'+'0'.repeat(64)}]);}return r;};
  const loaded=await f.prepare();expect((await runCampaign(loaded.dir,await f.approve(loaded),f.deps)).exit).toBe(0);
  const launches=f.calls.filter((call:any)=>call.executable===f.campaign.execution.runtime_path&&call.args[0]==='run');
  expect(launches).toHaveLength(2);for(const call of launches)expect(call.args).toContain(call.args.includes('--network=none')?loaded.c.execution.evaluator_image_digest:loaded.c.execution.image_digest);
 });
 it('wrong OCI config IDs refuse before a v2 broker claim',async()=>{
  const f=await setup({validation:true}),original=f.deps.runProcess;
  f.deps.runProcess=async(spec:any,signal?:AbortSignal)=>{const r=await original(spec,signal);if(spec.executable===f.campaign.execution.runtime_path&&spec.args[0]==='image')r.stdout=contracts.canonical([{Id:'1'.repeat(64),Digest:'sha256:'+'0'.repeat(64)}]);return r;};
  const loaded=await f.prepare();await expect(runCampaign(loaded.dir,await f.approve(loaded),f.deps)).rejects.toThrow('image identity');expect(f.claims.size).toBe(0);
 });
 it('historical v1 image admission keeps its exact manifest Digest rule',async()=>{
  const f=await setup({legacyV1:true});expect((await qualify(f.campaign,f.deps,1000)).qualification.schema).toBe('millforge-optimization-qualification/v1');
  const original=f.deps.runProcess;
  f.deps.runProcess=async(spec:any,signal?:AbortSignal)=>{const r=await original(spec,signal);if(spec.executable===f.campaign.execution.runtime_path&&spec.args[0]==='image')r.stdout=contracts.canonical([{Id:spec.args.at(-1).slice(7),Digest:'sha256:'+'0'.repeat(64)}]);return r;};
  await expect(qualify(f.campaign,f.deps,2000)).rejects.toThrow('image identity');expect(f.claims.size).toBe(0);
 });
 it('host_generate admission accepts none/OAuth tuples and refuses env, home, tools, mode and endpoint drift',async()=>{
  const f=await setup({hostGenerate:true}),p=(await f.load(path.join(f.root,'proposer'))).profile,c=f.campaign;expect(()=>hostAdmission(p,c,null)).not.toThrow();
  expect(()=>hostAdmission(p,c,{kind:'network',env_name:null})).toThrow('broker proposal route');
  const oauth=structuredClone(c);oauth.execution.proposal_execution.auth_home='/owned/auth';oauth.execution.model_endpoint.proposal='https://api.openai.com/v1';const op=structuredClone(p);op.model={...op.model,provider:'openai',api:'openai-responses',base_url:'https://api.openai.com/v1',auth:{kind:'oauth',provider:'openai'}};expect(()=>hostAdmission(op,oauth,null)).not.toThrow();
  expect(hostSpec(oauth,'/profile','/request.json','/output','/work',1000).args.slice(-2)).toEqual(['--auth-home','/owned/auth']);
  const noHome=structuredClone(oauth);noHome.execution.proposal_execution.auth_home=null;expect(()=>hostAdmission(op,noHome,null)).toThrow();expect(()=>hostAdmission(p,oauth,null)).toThrow();
  for(const mutate of [(x:any)=>{x.model.auth={kind:'env',name:'KEY'};},(x:any)=>{x.mode='agent';},(x:any)=>{x.tools=['read'];},(x:any)=>{x.capabilities.extensions=['x'];},(x:any)=>{x.limits.model_requests=2;},(x:any)=>{x.model.base_url='http://127.0.0.1:2/v1';}]){const x=structuredClone(p);mutate(x);expect(()=>hostAdmission(x,c,null)).toThrow();}
 });
 it('host_generate claims before spawn, uses the public base argument array and source-labelled base accounting without broker',async()=>{
  const f=await setup({hostGenerate:true}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.exit,JSON.stringify(run.report)).toBe(0);
  expect(f.hostRuns).toHaveLength(3);for(const spec of f.hostRuns){expect(spec.executable).toBe(process.execPath);expect(spec.args.slice(0,4)).toEqual([loaded.c.base.cli_path,'run','--profile',loaded.proposer.dir]);expect(spec.args).not.toContain('--auth-home');expect(Object.keys(spec.env)).toEqual(['PATH']);}
  expect([...f.claims.values()].every((x:any)=>x.route==='target')).toBe(true);expect(f.claims.size).toBe(4);
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));for(const work of ['general_proposal','domain_proposal','compose_proposal']){expect(evidence.claims[work].broker_receipt_sha256).toBeNull();const closure=await contracts.json(path.join(loaded.dir,'receipts',work+'-closure.json'));expect(closure).toMatchObject({broker:'not_admitted',status:'closed',writers:'closed'});expect(closure.worker).toMatchObject({process:'closed',container:'not_started'});await expect(fs.lstat(path.join(loaded.dir,'receipts',work+'-broker.json'))).rejects.toMatchObject({code:'ENOENT'});}
  const host=run.report.accounting.search.provenance.filter((x:any)=>x.accounting_source==='base_generate');expect(host.map((x:any)=>[x.work_id,x.model_requests,x.model_starts])).toEqual([['general_proposal',1,1],['domain_proposal',1,1],['compose_proposal',1,1]]);
  expect(run.report.accounting.search.provenance.filter((x:any)=>x.accounting_source==='broker')).toHaveLength(4);
 });
 it('host_generate failed client prefixes stay separate, and missing publication stays uncertain',async()=>{
  for(const [prefix,starts] of [[[0,0],0],[[1,0],0],[[1,1],1]] as const){const f=await setup({hostGenerate:true,id:'prefix-'+prefix.join(''),hostPrefixAt:{general_proposal:prefix}}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);expect(run.status).toBe('failed');expect(run.report.recommendation).toBe('inconclusive');const entry=run.report.accounting.search.provenance.find((x:any)=>x.work_id==='general_proposal'&&x.accounting_source==='base_generate');expect(entry).toMatchObject({model_requests:starts===0?0:prefix[1],model_starts:prefix[0]});}
  const missing=await setup({hostGenerate:true,id:'host-missing',noStdoutAt:'general_proposal'}),loaded=await missing.prepare(),run=await runCampaign(loaded.dir,await missing.approve(loaded),missing.deps);expect(run.status).toBe('uncertain');expect(run.report.accounting.search.model_requests.status).toBe('uncertain');expect(missing.hostRuns).toHaveLength(1);
 });
 it('host cancellation after claim and before spawn retains not-started closure',async()=>{
  const f=await setup({hostGenerate:true}),loaded=await f.prepare(),controller=new AbortController();
  const run=await runCampaign(loaded.dir,await f.approve(loaded),{...f.deps,signal:controller.signal,now:()=>{if(existsSync(path.join(loaded.dir,'claims/general_proposal.json')))controller.abort();return f.deps.now();}});
  expect(run.status).toBe('cancelled');expect(f.hostRuns).toHaveLength(0);
  const closure=await contracts.json(path.join(loaded.dir,'receipts/general_proposal-closure.json'));
  expect(closure).toMatchObject({broker:'not_admitted',status:'closed',worker:{process:'not_started',container:'not_started'}});
 });
 it('saved-chain replacement after the public stdout Result is refused with no trusted accounting',async()=>{
  const f=await setup({forgeSavedAt:'baseline'}),loaded=await f.prepare(),run=await runCampaign(loaded.dir,await f.approve(loaded),f.deps);
  expect(run.status).toBe('uncertain');expect(run.report.recommendation).toBe('inconclusive');
  const anchor=await contracts.json(path.join(loaded.dir,'receipts/baseline-base-result.json'));expect(anchor.output).toBe('done');
  const evidence=await contracts.json(path.join(loaded.dir,'evidence.json'));expect(evidence.claims.baseline.result_sha256).toBeNull();expect(evidence.runs.baseline).toBeNull();
  expect(()=>stdoutResult('')).toThrow();expect(()=>stdoutResult('{"schema":"millforge-result/v3"}\n{}')).toThrow();expect(()=>stdoutResult('{"schema":"millforge-result/v2"}')).toThrow();
 });
});
