/** Installed package fake-method and public CLI smoke; loopback only, no auth/providers. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {pathToFileURL} from 'node:url';
import {makeHarness} from './fixtures/fake-process.mjs';
const options={};for(let i=2;i<process.argv.length;i+=2){const key=process.argv[i],value=process.argv[i+1];if(!['--base-bin','--optimizer-bin','--output','--base-tar'].includes(key)||!value||options[key])throw new Error('invalid smoke arguments');options[key]=value;}for(const key of ['--base-bin','--optimizer-bin','--output',...(options['--base-tar']?['--base-tar']:[])])if(!path.isAbsolute(options[key]??''))throw new Error('absolute smoke inputs required');
const base=await fs.realpath(options['--base-bin']),optimizer=await fs.realpath(options['--optimizer-bin']),output=options['--output'];await fs.mkdir(output);const optRoot=path.resolve(path.dirname(optimizer),'../..'),prefix=base.slice(0,base.indexOf('/node_modules/'));if(!prefix||!optRoot.startsWith(path.dirname(output)+path.sep)||!prefix.startsWith(path.dirname(output)+path.sep))throw new Error('smoke requires an owned sibling install prefix');
const modules=async name=>import(new URL('./'+name+'.js',pathToFileURL(optimizer)).href);
const contracts=await modules('contracts'),store=await modules('store'),method=await modules('method'),evaluation=await modules('evaluation'),commands=[];
const run=async(name,bin,args,expect=0)=>{
 const env={PATH:path.dirname(process.execPath)+':/usr/bin:/bin',HOME:path.join(output,'home'),XDG_CONFIG_HOME:path.join(output,'config'),TMPDIR:path.join(output,'tmp')};
 await fs.mkdir(env.TMPDIR,{recursive:true});
 return new Promise((resolve,reject)=>{
  const child=spawn(process.execPath,[bin,...args],{env,stdio:['ignore','pipe','pipe']});let stdout='',stderr='';
  child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);child.once('error',reject);
  child.once('close',code=>{
   let error=null;try{if(stderr.trim())error=JSON.parse(stderr);}catch{error={message:stderr};}
   commands.push({name,exit:code,...(error?{error}:{})});
   if(code!==expect)reject(new Error(name+' exit '+code+': '+stderr));
   else{try{resolve(stdout?JSON.parse(stdout):null);}catch(cause){reject(cause);}}
  });
 });
};
let httpRequests=0;const server=createServer(async(req,res)=>{let size=0;for await(const chunk of req){size+=chunk.length;if(size>1048576){res.writeHead(413);res.end();return;}}if(req.method!=='POST'||req.url!=='/v1/chat/completions'){res.writeHead(404);res.end();return;}httpRequests++;res.writeHead(200,{'content-type':'text/event-stream'});const frame=(delta,finish_reason=null)=>({id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture-model',choices:[{index:0,delta,finish_reason}]});res.write('data: '+JSON.stringify(frame({role:'assistant',content:'retained result'}))+'\n\n');res.write('data: '+JSON.stringify(frame({},'stop'))+'\n\n');res.write('data: '+JSON.stringify({id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture-model',choices:[],usage:{prompt_tokens:7,completion_tokens:3,prompt_tokens_details:{cached_tokens:0}}})+'\n\n');res.end('data: [DONE]\n\n');});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let hidden=false;const hiddenAddon=path.join(output,'hidden-addon');
try{
 const endpoint='http://127.0.0.1:'+server.address().port+'/v1',f=await makeHarness(path.join(output,'method'),{...contracts,...store},{endpoint,publicBasePeer:true}),loaded=await f.prepare(),result=await method.runCampaign(loaded.dir,await f.approve(loaded),f.deps);if(result.exit!==0||result.report.recommendation!=='composed'||f.claims.size!==7)throw new Error('installed seven-slot method failed: '+JSON.stringify(result));const recomputed=await evaluation.assess(loaded,path.join(loaded.dir,'evidence.json'),f.deps,1000000);if(!contracts.same(recomputed,result.report))throw new Error('installed assessment differs');
 // Public optimizer commands use an explicit synthetic peer for these fake records.
 const assessed=path.join(output,'public-assessment');
 const publicReport=await run('public-relative-assess',optimizer,['assess','--campaign',loaded.dir,'--evidence',path.join(loaded.dir,'evidence.json'),'--output',assessed,'--base-bin',f.baseBin]);
 const storedEvidence=await contracts.json(path.join(assessed,'evidence.json'));
 if(publicReport.evidence_digest!==contracts.hash(await contracts.read(path.join(assessed,'evidence.json')))||!path.isAbsolute(storedEvidence.runs.baseline.run_dir))throw new Error('public assessment lost original-relative meaning');
 const destination=path.join(output,'retained'),approval=path.join(output,'adoption.json');
 await contracts.write(approval,{schema:'millforge-optimization-adoption/v1',owner_id:'owner',campaign_id:loaded.c.campaign_id,campaign_digest:loaded.digest,report_digest:evaluation.fileDigest(publicReport),candidate_digest:publicReport.candidate_digests.composed,destination,expected_preimage:'absent',decision:'adopt',retention:'task_specific',library_entry:null});
 await run('public-relative-adopt',optimizer,['adopt','--campaign',loaded.dir,'--report',path.join(assessed,'report.json'),'--approval',approval,'--destination',destination,'--base-bin',f.baseBin]);
 const declined=path.join(output,'declined'),declineApproval=path.join(output,'decline.json');
 await contracts.write(declineApproval,{...(await contracts.json(approval)),destination:declined,decision:'decline'});
 await evaluation.adopt(loaded,path.join(assessed,'report.json'),declineApproval,declined,f.deps);
 try{await method.runCampaign(loaded.dir,path.join(f.root,'approval.json'),f.deps);throw new Error('rerun unexpectedly accepted');}catch(e){if(!String(e.message).includes('already started'))throw e;}
 const identity=await run('protocol',optimizer,['protocol']);if(!contracts.same(identity,{protocol:'millforge-optimization/v1',package:'@millrace-ai/millforge-optimization',version:'0.1.1'}))throw new Error('protocol mismatch');
 const installedManifest=await contracts.json(path.resolve(path.dirname(base),'../../../package.json'));
 assert.equal(installedManifest.name,'@millrace-ai/millforge');
 assert.equal(installedManifest.version,'0.2.1');
 const selectedTar=options['--base-tar'];
 const actualBase={cli_path:base,cli_sha256:contracts.hash(await contracts.read(base)),prefix_path:prefix,tree_sha256:(await contracts.tree(prefix,Number.MAX_SAFE_INTEGER,true)).digest,artifact_sha256:contracts.hash(selectedTar?await contracts.read(selectedTar):'qualification-bound fixture provenance; no tar bytes claimed'),package_version:'0.2.1',pi_version:'1.4.0'},authored={...f.campaign,campaign_id:'public-smoke',base:actualBase};const author=path.join(output,'public-author.json');await contracts.write(author,authored);const publicDir=path.join(output,'public-campaign');await run('lazy-prepare',base,['optimize','prepare','--campaign',author,'--output',publicDir,'--optimizer-bin',optimizer]);const prepared=await run('lazy-inspect',base,['optimize','inspect','--campaign',publicDir,'--optimizer-bin',optimizer]);if(prepared.status!=='prepared')throw new Error('pure inspection state');const publicC=await contracts.json(path.join(publicDir,'campaign.json')),publicApproval=path.join(output,'public-approval.json');await contracts.write(publicApproval,{schema:'millforge-optimization-authority/v1',owner_id:'owner',campaign_id:publicC.campaign_id,campaign_digest:contracts.hash(contracts.canonical(publicC)),execution_root:publicDir,budget:publicC.budget,expires_at:new Date(Date.now()+3600000).toISOString()});await run('production-refuses-test-scope',base,['optimize','run','--campaign',publicDir,'--approval',publicApproval,'--optimizer-bin',optimizer],2);
 const refusal=commands.find(command=>command.name==='production-refuses-test-scope')?.error;
 if(refusal?.code!=='INVALID_INPUT'||refusal.message!=='qualification scope mismatch')throw new Error('production scope refusal reason differs: '+JSON.stringify(refusal));
 const publicClaims=await fs.readdir(path.join(publicDir,'claims')).catch(error=>{if(error.code==='ENOENT')return [];throw error;});
 if(publicClaims.length||await fs.stat(path.join(publicDir,'run.json')).then(()=>true,()=>false))throw new Error('production scope refusal admitted work');

 const checked=await run('plain-profile-validate',base,['profile','validate',destination]);if(checked.digest!==result.report.candidate_digests.composed)throw new Error('retained public profile digest differs');
 const workspace=path.join(output,'plain-workspace');await fs.mkdir(workspace);const plain=await contracts.json(path.join(destination,'profile.json')),request=path.join(output,'plain-request.json');await contracts.write(request,{schema:'millforge-request/v1',request_id:'retained-offline',profile_id:plain.profile_id,profile_digest:checked.digest,expected_mode:'agent',task:'Reply with retained result.',context:[],workspace,limits:{elapsed_seconds:20,model_timeout_seconds:15,tool_timeout_seconds:15},correlation:{}});await fs.rename(optRoot,hiddenAddon);hidden=true;const plainRun=path.join(output,'plain-run'),plainResult=await run('retained-run-addon-absent',base,['run','--profile',destination,'--request',request,'--output-dir',plainRun]);if(plainResult.status!=='completed'||plainResult.output!=='retained result')throw new Error('retained independent execution failed');const inspection=await run('plain-inspect-addon-absent',base,['inspect','--run',plainRun]);if(inspection.capture.versions.millforge!=='0.2.1'||inspection.result.counts.model_requests!==1||inspection.result.counts.cost!==null||httpRequests!==1)throw new Error('plain usage provenance differs');await fs.rename(hiddenAddon,optRoot);hidden=false;
 const codemode=options['--base-tar']?await codemodeRoundTrip(actualBase):null;
 await contracts.write(path.join(output,'result.json'),{schema:'millforge-optimization-installed-smoke/v1',status:'PASS',scope:'test',method_work_items:7,method_model_requests:7,plain_model_http_requests:httpRequests,ordinary_addon_absent:true,production_test_scope_refused:true,production_scope_refusal:refusal,public_relative_assess_adopt:true,public_assessment_base_peer:{path:f.baseBin,sha256:contracts.hash(await contracts.read(f.baseBin)),synthetic:true,scope:'test'},codemode,commands,model_provider_auth_calls:0,limitations:['Native method records and public native assess/adopt base peer are synthetic. Codemode method containment, broker and evaluator use injected test peers. The model peers bind only to loopback.']});
}finally{if(hidden)await fs.rename(hiddenAddon,optRoot);server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}

async function codemodeRoundTrip(actualBase){
 const {canonical,codemodeTarget,hash,json,read,same,write}=contracts;
 const lifecycle=await modules('lifecycle');
 const root=path.join(output,'codemode');
 await fs.mkdir(root);
 const translations=[],workers=[],requests={},errors=[];
 let active=null;
 const peer=createServer(async(req,res)=>{
  try{
   assert.equal(req.method,'POST');
   assert.equal(req.url,'/v1/chat/completions');
   assert.ok(active,'unexpected model request');
   const chunks=[];
   for await(const part of req)chunks.push(part);
   const body=JSON.parse(Buffer.concat(chunks));
   assert.equal(body.model,'fixture-model');
   const {work,broker,request,claim}=active;
   const index=requests[work]??0;
   requests[work]=index+1;
   const proposal=request.expected_mode==='generate';
   if(broker)assert.equal(await broker.infer(request.request_id,proposal?'proposal':'target',claim.campaign_digest),true);
   let delta,finish='stop';
   if(proposal){
    assert.equal(index,0);
    assert.equal(body.tools?.length??0,0);
    delta={role:'assistant',content:canonical({
     instruction_text:'baseline guidance',tools:work==='general_proposal'?['write']:['read','write'],
     assets:[],rationale:'Exercise the strict subset.'
    })};
   }else{
    const names=body.tools.map(tool=>tool.custom?.name??tool.function?.name);
    assert.ok(body.tools.some(tool=>tool.type==='custom'&&tool.custom.name==='codemode'),'missing grammar tool');
    assert.ok(names.includes('write'));
    if(index===0){
     const code='text(await tools.write({path:"effect.txt",content:'+JSON.stringify(names.includes('read')?'baseline':'kept')+'}));';
     delta={tool_calls:[{index:0,id:'fixture_wrapper',type:'custom',custom:{name:'codemode',input:code}}]};
     finish='tool_calls';
    }else{
     assert.equal(index,1);
     delta={role:'assistant',content:'codemode fixture complete'};
    }
   }
   const frame=(value,finish_reason)=>({id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture-model',choices:[{index:0,delta:value,finish_reason}]});
   res.writeHead(200,{'content-type':'text/event-stream'});
   for(const event of [frame(delta,null),frame({},finish)])res.write('data: '+JSON.stringify(event)+'\n\n');
   res.end('data: [DONE]\n\n');
  }catch(error){
   errors.push(String(error));
   res.writeHead(400);
   res.end('fixture rejected request');
  }
 });
 await new Promise(resolve=>peer.listen(0,'127.0.0.1',resolve));
 try{
  const endpoint='http://127.0.0.1:'+peer.address().port+'/v1';
  const f=await makeHarness(path.join(root,'method'),{...contracts,...store},{
   id:'codemode-smoke',codemode:true,nativeTools:['read','write'],noEditableAssets:true,endpoint,
   sharedBase:actualBase,
   evaluate:async deliverables=>{
    const content=await read(path.join(deliverables,'effect.txt'));
    return [content.length>0,content.toString()==='kept'];
   },
   runWorker:async({spec,signal,mounts,work,broker})=>{
    const requestFile=mounts['/request.json'],workspace=mounts['/workspace'];
    const original=await read(requestFile),request=JSON.parse(original);
    let translated=requestFile,translatedBytes=original;
    if(request.expected_mode==='agent'){
     const field=Buffer.from('"workspace":"/workspace"'),at=original.indexOf(field);
     assert.equal(request.workspace,'/workspace');
     assert.ok(at>=0&&at===original.lastIndexOf(field));
     translatedBytes=Buffer.concat([original.subarray(0,at),Buffer.from('"workspace":'+JSON.stringify(workspace)),original.subarray(at+field.length)]);
     assert.ok(same(JSON.parse(translatedBytes),{...request,workspace}));
     translated=path.join(root,'translated-requests',work+'.json');
     await write(translated,translatedBytes);
    }else assert.equal(request.workspace,null);
    const imageAt=spec.args.indexOf(f.campaign.execution.image_digest);
    const [node,...argv]=spec.args.slice(imageAt+1);
    assert.equal(node,'/usr/local/bin/node');
    const hostArgv=argv.map(arg=>{
     if(arg==='/request.json')return translated;
     const mount=Object.keys(mounts).find(key=>arg===key||arg.startsWith(key+'/'));
     return mount?mounts[mount]+arg.slice(mount.length):arg;
    });
    assert.equal(hostArgv[0],base);
    assert.equal(hostArgv[1],'run');
    translations.push({
     work_id:work,container_argv:[node,...argv],host_argv:[process.execPath,...hostArgv],mounts,
     original_path:requestFile,translated_path:translated,original_sha256:hash(original),translated_sha256:hash(translatedBytes),
     workspace:request.expected_mode==='agent'?{from:'/workspace',to:workspace}:null
    });
    const claim=[...f.claims.values()].find(value=>value.request_id===request.request_id);
    assert.ok(claim);
    active={work,broker,request,claim};
    try{
     const result=await lifecycle.runProcess({...spec,executable:process.execPath,args:hostArgv,cwd:workspace},signal);
     workers.push({work_id:work,exit:result.exit,closed:result.closed,timed_out:result.timedOut,stderr:result.stderr});
     return result;
    }finally{active=null;}
   }
  });
  const deps={
   ...f.deps,now:()=>performance.now(),
   runProcess:(spec,signal,cleanupDeadline)=>spec.args[0]===base
    ?lifecycle.runProcess(spec,signal,cleanupDeadline):f.deps.runProcess(spec,signal)
  };
  const captured=path.join(root,'captured'),cli=(name,command,args,exit=0)=>run(name,base,['optimize',command,...args,'--optimizer-bin',optimizer],exit);
  await cli('codemode-prepare','prepare',['--campaign',f.author,'--output',captured]);
  assert.equal((await cli('codemode-inspect-prepared','inspect',['--campaign',captured])).status,'prepared');
  const loaded=await store.loadCampaign(captured,base,deps),approval=await f.approve(loaded);
  const proposalFile=path.join(root,'strict-subset.json');
  await write(proposalFile,{
   schema:'millforge-optimization-proposal/v1',campaign_id:loaded.c.campaign_id,kind:'general',baseline_digest:loaded.baseline.digest,
   parent_candidates:[],instruction_text:'baseline guidance',tools:['write'],assets:[],rationale:'Keep the writer for the fixture.'
  });
  const proposed=await cli('codemode-candidate','candidate',['--campaign',captured,'--proposal',proposalFile,'--output',path.join(root,'preview')]);
  await cli('codemode-production-refused','run',['--campaign',captured,'--approval',approval],2);
  assert.deepEqual(commands.at(-1).error,{code:'INVALID_INPUT',message:'Codemode optimization requires test scope'});
  for(const entry of ['run.json','claims','requests','workers'])await assert.rejects(fs.stat(path.join(captured,entry)),{code:'ENOENT'});
  assert.deepEqual(requests,{});

  const executed=await method.runCampaign(captured,approval,deps);
  assert.equal(executed.exit,0,JSON.stringify({executed,workers,errors}));
  assert.equal(executed.report.recommendation,'general');
  assert.equal(executed.report.accounting.search.model_requests.observed,11);
  assert.equal(executed.report.accounting.search.tool_calls.observed,8);
  assert.equal(f.claims.size,7);
  assert.equal(workers.length,7);
  assert.ok(workers.every(worker=>worker.exit===0&&worker.closed&&!worker.timed_out));
  assert.equal(translations.filter(translation=>translation.workspace!==null).length,4);
  assert.ok(translations.every(translation=>(translation.original_sha256!==translation.translated_sha256)===(translation.workspace!==null)));
  assert.equal((await cli('codemode-inspect-completed','inspect',['--campaign',captured])).status,'completed');

  const baselineRun=path.join(captured,'runs/baseline'),probeRun=path.join(captured,'runs/general_probe');
  const baseline=await run('codemode-baseline-base-inspect',base,['inspect','--run',baselineRun]);
  const candidate=await run('codemode-subset-base-inspect',base,['inspect','--run',probeRun]);
  const baselineCap=baseline.capture.capability,candidateCap=candidate.capture.capability;
  assert.deepEqual(baseline.capture.callable_tool_names,['read','write']);
  assert.deepEqual(candidate.capture.callable_tool_names,['write']);
  assert.deepEqual(baseline.capture.tool_names,['read','write','codemode']);
  assert.deepEqual(candidate.capture.tool_names,['write','codemode']);
  assert.deepEqual(baseline.capture.declared_tools.map(tool=>tool.name),baseline.capture.tool_names);
  assert.deepEqual(candidate.capture.declared_tools,baseline.capture.declared_tools.filter(tool=>tool.name!=='read'));
  assert.equal(baselineCap.declared_description_sha256,candidateCap.declared_description_sha256);
  assert.equal(baselineCap.constrained_sampling_sha256,candidateCap.constrained_sampling_sha256);
  assert.ok(same(baselineCap,candidateCap));
  for(const [inspection,dir]of [[baseline,baselineRun],[candidate,probeRun]]){
   assert.equal(inspection.result.schema,codemodeTarget.result_schema);
   assert.equal(inspection.result.counts.tool_calls,2);
   assert.equal(inspection.result.counts.nested_tool_calls,1);
   assert.deepEqual(inspection.result,JSON.parse(await read(path.join(captured,'receipts',path.basename(dir)+'-base-result.json'))));
   const trace=(await read(path.join(dir,'trace.jsonl'))).toString().trimEnd().split('\n').map(line=>JSON.parse(line));
   assert.ok(trace.every(event=>event.schema===codemodeTarget.trace_schema));
   assert.ok(trace.some(event=>event.kind==='tool_start'&&event.payload.name==='codemode'&&event.payload.parent_call_id===null));
   assert.ok(trace.some(event=>event.kind==='tool_start'&&event.payload.name==='write'&&event.payload.parent_call_id==='fixture_wrapper'));
   assert.ok(trace.some(event=>event.kind==='nested_join'&&event.payload.outstanding===0));
  }

  const assessed=path.join(root,'assessment'),evidence=path.join(captured,'evidence.json');
  const report=await cli('codemode-assess','assess',['--campaign',captured,'--evidence',evidence,'--output',assessed]);
  assert.equal(report.recommendation,'general');
  assert.equal(report.protection_scope,'test');
  assert.equal(report.candidate_digests.general,candidate.result.profile_digest);
  assert.equal(report.candidate_digests.general,proposed.profile_digest);
  assert.equal(report.accounting.ordinary_execution,null);
  const destination=path.join(root,'adopted'),authority=path.join(root,'adoption-approval.json');
  await write(authority,{
   schema:'millforge-optimization-adoption/v1',owner_id:loaded.c.owner_id,campaign_id:loaded.c.campaign_id,
   campaign_digest:loaded.digest,report_digest:evaluation.fileDigest(report),candidate_digest:candidate.result.profile_digest,
   destination,expected_preimage:'absent',decision:'adopt',retention:'task_specific',library_entry:null
  });
  await cli('codemode-adopt','adopt',['--campaign',captured,'--report',path.join(assessed,'report.json'),'--approval',authority,'--destination',destination]);
  const adoptedProfile=await json(path.join(destination,'profile.json'));
  assert.equal(adoptedProfile.schema,codemodeTarget.profile_schema);
  assert.deepEqual(adoptedProfile.tools,['write']);
  assert.deepEqual(await read(path.join(destination,'profile.json')),await read(path.join(captured,'candidates/general/profile.json')));

  await fs.rename(optRoot,hiddenAddon);hidden=true;
  const checked=await run('codemode-adopted-validate-base-only',base,['profile','validate',destination]);
  assert.equal(checked.digest,candidate.result.profile_digest);
  const workspace=path.join(root,'ordinary-workspace'),requestFile=path.join(root,'ordinary-request.json'),ordinaryRun=path.join(root,'ordinary-run');
  await fs.mkdir(workspace);
  const request={schema:codemodeTarget.request_schema,request_id:'ordinary-codemode',profile_id:adoptedProfile.profile_id,profile_digest:checked.digest,
   expected_mode:'agent',task:'Use the retained writer.',context:[],workspace,limits:{},correlation:{}};
  await write(requestFile,request);
  active={work:'ordinary',request};
  const ordinary=await run('codemode-adopted-run-base-only',base,['run','--profile',destination,'--request',requestFile,'--output-dir',ordinaryRun]);
  active=null;
  const inspected=await run('codemode-adopted-inspect-base-only',base,['inspect','--run',ordinaryRun]);
  assert.deepEqual(inspected.result,ordinary);
  assert.deepEqual(inspected.capture.callable_tool_names,['write']);
  assert.deepEqual([...inspected.capture.tool_names].sort(),['codemode','write']);
  assert.equal(inspected.result.counts.tool_calls,2);
  assert.equal(inspected.result.counts.nested_tool_calls,1);
  assert.equal((await read(path.join(workspace,'effect.txt'))).toString(),'kept');
  await fs.rename(hiddenAddon,optRoot);hidden=false;
  assert.deepEqual(errors,[]);
  assert.deepEqual(requests,{baseline:2,general_proposal:1,domain_proposal:1,general_probe:2,domain_probe:2,compose_proposal:1,composed_probe:2,ordinary:2});
  const receipt={
   scope:'test',base:f.campaign.base,campaign_digest:loaded.digest,kind:report.recommendation,adopted_digest:checked.digest,
   captures:{
    baseline:{sha256:hash(await read(path.join(baselineRun,'capture.json'))),active_tools:baseline.capture.tool_names,callable_tools:baseline.capture.callable_tool_names,declared_tools:baseline.capture.declared_tools,capability:baselineCap},
    subset:{sha256:hash(await read(path.join(probeRun,'capture.json'))),active_tools:candidate.capture.tool_names,callable_tools:candidate.capture.callable_tool_names,declared_tools:candidate.capture.declared_tools,capability:candidateCap},
    ordinary:{sha256:hash(await read(path.join(ordinaryRun,'capture.json'))),active_tools:inspected.capture.tool_names,callable_tools:inspected.capture.callable_tool_names}
   },
   model_requests:requests,workers,translations,method_tool_calls:8,ordinary_tool_calls:2,ordinary_nested_tool_calls:1,ordinary_addon_absent:true
  };
  await write(path.join(root,'receipt.json'),receipt);
  return receipt;
 }finally{
  peer.closeAllConnections();
  await new Promise(resolve=>peer.close(resolve));
 }
}
