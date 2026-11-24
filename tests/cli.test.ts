import {afterEach,describe,expect,it,vi} from 'vitest';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:http';
import {main} from '../src/cli.js';
const roots:string[]=[];afterEach(async()=>{vi.unstubAllEnvs();for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});});
async function setup(){const root=await fs.mkdtemp(path.join(tmpdir(),'millforge-cli-'));roots.push(root);const manifest=path.join(root,'manifest.json');await fs.copyFile(new URL('../profiles/generate/profile.json',import.meta.url),manifest);return {root,manifest,dir:path.join(root,'profile')};}
async function invoke(args:string[],home:string){let out='',err='';const env=new Proxy({HOME:home},{get(target,key){if(key!=='HOME'&&key!=='XDG_CONFIG_HOME')throw new Error('unexpected environment access');return target[key as keyof typeof target];}});const exit=await main(args,env,s=>out+=s,s=>err+=s);return {exit,out,err};}
describe('standalone M1 CLI',()=>{
  it('performs ordinary profile operations with only supplied home/config access',async()=>{const f=await setup();expect((await invoke(['profile','create',f.dir,'--from',f.manifest],f.root)).exit).toBe(0);const shown=await invoke(['profile','show',f.dir],f.root);expect(JSON.parse(shown.out).profile.schema).toBe('millforge-profile/v1');expect(JSON.parse(shown.out).digest).toMatch(/^[a-f0-9]{64}$/);expect((await invoke(['profile','validate',f.dir],f.root)).exit).toBe(0);expect((await invoke(['profile','clone',f.dir,path.join(f.root,'clone'),'--id','copied'],f.root)).exit).toBe(0);expect((await invoke(['profile','select',f.dir],f.root)).exit).toBe(0);expect((await invoke(['profile','diff',f.dir,path.join(f.root,'clone')],f.root)).exit).toBe(0);expect((await invoke(['profile','export',f.dir,path.join(f.root,'export')],f.root)).exit).toBe(0);expect((await invoke(['profile','import',path.join(f.root,'export'),path.join(f.root,'import')],f.root)).exit).toBe(0);expect((await invoke(['profile','edit',f.dir,'--from',f.manifest],f.root)).exit).toBe(0);expect((await invoke(['profile','restore',f.dir,'--revision','1'],f.root)).exit).toBe(0);});
  it('refuses missing run inputs and optional optimization without creating an output directory',async()=>{const f=await setup();const output=path.join(f.root,'never-created');const run=await invoke(['run','--profile',f.dir,'--request',path.join(f.root,'missing-request'),'--output-dir',output],f.root);expect(run.exit).toBe(2);expect(JSON.parse(run.err).code).toBe('IO_ERROR');await expect(fs.lstat(output)).rejects.toThrow();const optimize=await invoke(['optimize','run','--optimizer-bin',path.join(f.root,'missing-optimizer')],f.root);expect(JSON.parse(optimize.err).code).toBe('OPTIMIZER_UNAVAILABLE');});
  it.each([[],['profile'],['profile','unknown'],['profile','create'],['profile','show','a','b'],['profile','validate','a','--unknown','x'],['profile','restore','a','--revision','true'],['inspect','--run','missing'],['run','--request','a','--request','b','--output-dir','out']].map(args=>({args})))('refuses malformed command %j',async({args})=>{const f=await setup();const result=await invoke(args,f.root);expect(result.exit).toBe(2);expect(result.out).toBe('');expect(JSON.parse(result.err).message).not.toContain('password');});
  it('has no static Pi, other-product or optional add-on imports',async()=>{
    for(const name of ['cli','contracts','profile','artifacts','optimization-dispatch']){
      const source=await fs.readFile(new URL(`../src/${name}.ts`,import.meta.url),'utf8');
      const imports=[...source.matchAll(/(?:from\s*|import\s*\()\s*['"]([^'"]+)['"]/g)].map(match=>match[1]!);
      for(const imported of imports){
        if(name==='cli'&&imported==='./optimization-dispatch.js')continue;
        expect(imported).not.toMatch(/@earendil|millrace-(?:plus|ai|os)|optimization/);
        if(name==='optimization-dispatch')expect(imported==='./versions.js'||imported.startsWith('node:')).toBe(true);
      }
    }
    const config=await fs.readFile(new URL('../vitest.config.ts',import.meta.url),'utf8');
    expect(config).toContain("include:['tests/*.test.ts']");
  });
});

const identity={protocol:'millforge-optimization/v1',package:'@millrace-ai/millforge-optimization',version:'0.1.1'};
async function fakeOptimizer(root:string,options:{response?:string;exit?:number;hangProtocol?:boolean;wait?:boolean;protocolExit?:number;mutate?:boolean;cleanupExit?:number;splitOutput?:boolean}={}){
  const bin=path.join(root,'millforge-optimize'),calls=path.join(root,'calls.jsonl'),ready=path.join(root,'ready');
  const marker=path.join(root,'cleanup-marker'),signals=path.join(root,'signals.jsonl');
  const commandSource=options.cleanupExit!==undefined?`
let stopping=false;
function stop(signal){
  fs.appendFileSync(${JSON.stringify(signals)},signal+'\\n');
  if(stopping)return;stopping=true;
  setTimeout(()=>{fs.writeFileSync(${JSON.stringify(marker)},'cleanup finished');process.exit(${options.cleanupExit});},1000);
}
process.on('SIGINT',()=>stop('SIGINT'));process.on('SIGTERM',()=>stop('SIGTERM'));
fs.writeFileSync(${JSON.stringify(ready)},String(process.pid));setInterval(()=>{},1000);
`:options.splitOutput?`
const out=Buffer.from('€'),err=Buffer.from('🌊');
process.stdout.write(out.subarray(0,1));process.stderr.write(err.subarray(0,2));
setTimeout(()=>{process.stdout.write(out.subarray(1));process.stderr.write(err.subarray(2));},50);
`:options.wait?`process.on('SIGINT',()=>process.exit(0));process.on('SIGTERM',()=>process.exit(0));fs.writeFileSync(${JSON.stringify(ready)},String(process.pid));setInterval(()=>{},1000);`:`process.stdout.write('delegated\\n');process.exitCode=${options.exit??0};`;
  await fs.writeFile(bin,`#!${process.execPath}
const fs=require('node:fs');
const args=process.argv.slice(2);
fs.appendFileSync(${JSON.stringify(calls)},JSON.stringify(args)+'\\n');
if(args[0]==='protocol'){
  ${options.mutate?"fs.appendFileSync(__filename,'\\n// replaced');":''}
  process.exitCode=${options.protocolExit??0};
  ${options.hangProtocol?"setInterval(()=>{},1000);":`process.stdout.write(${JSON.stringify(options.response??JSON.stringify(identity)+'\n')});`}
}else{
  ${commandSource}
}
`);
  await fs.chmod(bin,0o700);return {bin,calls,ready,marker,signals};
}
async function callLog(file:string):Promise<string[][]>{return (await fs.readFile(file,'utf8')).trim().split('\n').map(line=>JSON.parse(line) as string[]);}
async function waitFile(file:string):Promise<string>{for(let i=0;i<200;i++){try{return await fs.readFile(file,'utf8');}catch{await new Promise(resolve=>setTimeout(resolve,10));}}throw new Error('fake optimizer did not become ready');}
describe('optional optimizer foreground boundary',()=>{
  it('optional_optimizer_absent: ordinary commands never discover or probe PATH',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root);vi.stubEnv('PATH',f.root);
    await invoke(['profile','create',f.dir,'--from',f.manifest],f.root);
    expect((await invoke(['profile','validate',f.dir],f.root)).exit).toBe(0);
    expect((await invoke(['profile','show',f.dir],f.root)).exit).toBe(0);
    await invoke(['inspect','--run',path.join(f.root,'missing')],f.root);
    await invoke(['run','--request','missing','--output-dir',path.join(f.root,'output')],f.root);
    await invoke(['--help'],f.root);
    await expect(fs.stat(fake.calls)).rejects.toThrow();
    vi.stubEnv('PATH',path.join(f.root,'absent'));
    const result=await invoke(['optimize','inspect'],f.root);expect(result.exit).toBe(2);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_UNAVAILABLE');
  });
  it('optional_dispatch: explicit executable wins PATH, strips its option and forwards one owned base and exit',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{exit:7});const other=path.join(f.root,'other');await fs.mkdir(other);const unused=await fakeOptimizer(other);vi.stubEnv('PATH',other);
    const result=await invoke(['optimize','inspect','--campaign','saved','--optimizer-bin',fake.bin],f.root);
    expect(result.exit).toBe(7);expect(result.out).toBe('delegated\n');expect(result.err).toBe('');
    expect(await callLog(fake.calls)).toEqual([['protocol'],['inspect','--campaign','saved','--base-bin',await fs.realpath(fileURLToPath(new URL('../src/cli.ts',import.meta.url)))]]);
    await expect(fs.stat(unused.calls)).rejects.toThrow();
  });
  it('optional_dispatch: resolves PATH only inside optimize with selection-env Proxy intact',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root);vi.stubEnv('PATH',f.root);
    expect((await invoke(['optimize','--optimizer-bin',fake.bin,'inspect'],f.root)).exit).toBe(0);
    expect((await invoke(['optimize','inspect'],f.root)).exit).toBe(0);
    expect((await callLog(fake.calls)).length).toBe(4);
  });
  it.each([['--optimizer-bin'],['--optimizer-bin','relative'],['--optimizer-bin','/a','--optimizer-bin','/b'],['--base-bin','/a'],['--base-bin=/a'],['--optimizer-bin=/a']].map(flags=>({flags})))('refuses dispatch flags before executing: %j',async({flags})=>{
    const f=await setup(),fake=await fakeOptimizer(f.root);vi.stubEnv('PATH',f.root);
    const result=await invoke(['optimize','inspect',...flags],f.root);expect(result.exit).toBe(2);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_INCOMPATIBLE');await expect(fs.stat(fake.calls)).rejects.toThrow();
  });
  it.each([JSON.stringify({...identity,version:'0.1.0'}),JSON.stringify({...identity,version:'0.2.0'}),JSON.stringify({...identity,extra:true}),'noise\n'+JSON.stringify(identity),'{}',JSON.stringify(identity).replace('{','{"protocol":"millforge-optimization/v1",')])('refuses incompatible or noisy identity %s without campaign dispatch',async response=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{response:response+'\n'});const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);
    expect(result.exit).toBe(2);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_INCOMPATIBLE');expect(await callLog(fake.calls)).toEqual([['protocol']]);
  });
  it('refuses nonzero negotiation and changed executable identity before command dispatch',async()=>{
    for(const options of [{protocolExit:1},{mutate:true}]){
      const f=await setup(),fake=await fakeOptimizer(f.root,options);const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);
      expect(JSON.parse(result.err).code).toBe('OPTIMIZER_INCOMPATIBLE');expect(await callLog(fake.calls)).toEqual([['protocol']]);
    }
  });
  it('refuses a nonexecutable explicit optimizer without PATH fallback',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root);await fs.chmod(fake.bin,0o600);vi.stubEnv('PATH',f.root);
    const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_UNAVAILABLE');await expect(fs.stat(fake.calls)).rejects.toThrow();
  });
  it('refuses oversized protocol output',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{response:'x'.repeat(4097)});const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_INCOMPATIBLE');expect((await callLog(fake.calls)).length).toBe(1);
  });
  it('bounds hung protocol negotiation without campaign dispatch',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{hangProtocol:true});const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);expect(JSON.parse(result.err).code).toBe('OPTIMIZER_INCOMPATIBLE');expect(await callLog(fake.calls)).toEqual([['protocol']]);
  });
  it.each(['SIGINT','SIGTERM'] as const)('forwards %s and repeated cancellation through shared foreground cleanup',async signal=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{wait:true}),listeners=process.listenerCount(signal);
    const pending=invoke(['optimize','run','--optimizer-bin',fake.bin],f.root);const pid=Number(await waitFile(fake.ready));
    process.emit(signal);process.emit(signal);const result=await pending;expect(result.exit).toBe(0);expect(process.listenerCount(signal)).toBe(listeners);expect(()=>process.kill(pid,0)).toThrow();
    expect((await callLog(fake.calls)).length).toBe(2);
  });
  it.each([{signal:'SIGINT',exit:3},{signal:'SIGTERM',exit:130}] as const)('waits for delayed cleanup and preserves numeric $exit after repeated $signal',async({signal,exit})=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{cleanupExit:exit});
    const pending=invoke(['optimize','run','--optimizer-bin',fake.bin],f.root);const pid=Number(await waitFile(fake.ready));
    process.emit(signal);process.emit(signal);
    const result=await pending;
    expect(result.exit).toBe(exit);expect(result.err).toBe('');
    expect(await fs.readFile(fake.marker,'utf8')).toBe('cleanup finished');
    expect(await fs.readFile(fake.signals,'utf8')).toBe(signal+'\n');
    expect(()=>process.kill(pid,0)).toThrow();expect((await callLog(fake.calls)).length).toBe(2);
  });
  it('preserves UTF-8 characters split across stdout and stderr chunks',async()=>{
    const f=await setup(),fake=await fakeOptimizer(f.root,{splitOutput:true});
    const result=await invoke(['optimize','inspect','--optimizer-bin',fake.bin],f.root);
    expect(result.exit).toBe(0);expect(result.out).toBe('€');expect(result.err).toBe('🌊');
  });

});

it('runs and inspects profile v3 through the public CLI and built worker with exit cleanup',async()=>{
  const f=await setup(),p=JSON.parse(await fs.readFile(new URL('./fixtures/codemode/profile.json',import.meta.url),'utf8'));let calls=0;
  const server=createServer(async(req,res)=>{let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);calls++;expect(body.tools.some((t:{custom?:{name:string}})=>t.custom?.name==='codemode')).toBe(true);res.writeHead(200,{'content-type':'text/event-stream'});const delta=calls===1?{tool_calls:[{index:0,id:'public_parent',type:'custom',custom:{name:'codemode',input:'await tools.write({path:"effect.txt",content:"kept"}); text(await tools.read({path:"effect.txt"}));'}}]}:{role:'assistant',content:'done'};for(const chunk of [{choices:[{index:0,delta,finish_reason:null}]},{choices:[{index:0,delta:{},finish_reason:calls===1?'tool_calls':'stop'}]}])res.write('data: '+JSON.stringify({id:'public',model:'codemode-fixture',...chunk})+'\n\n');res.end('data: [DONE]\n\n');});
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));const address=server.address();if(!address||typeof address==='string')throw Error('address');p.model.base_url=`http://127.0.0.1:${address.port}/v1`;await fs.writeFile(f.manifest,JSON.stringify(p));expect((await invoke(['profile','create',f.dir,'--from',f.manifest],f.root)).exit).toBe(0);const shown=JSON.parse((await invoke(['profile','show',f.dir],f.root)).out),request=path.join(f.root,'request.json'),out=path.join(f.root,'run');await fs.writeFile(request,JSON.stringify({schema:'millforge-request/v1',request_id:'public-v3',profile_id:p.profile_id,profile_digest:shown.digest,expected_mode:'agent',task:'finish',context:[],workspace:f.root,limits:{},correlation:{}}));let result='',diagnostic='';
  const runtimeEnv={HOME:f.root,PATH:process.env.PATH??''},built=await import(pathToFileURL(path.resolve('dist/node/src/cli.js')).href) as typeof import('../src/cli.js');try{const exit=await built.main(['run','--profile',f.dir,'--request',request,'--output-dir',out],runtimeEnv,s=>result+=s,s=>diagnostic+=s);expect(exit,diagnostic).toBe(0);expect(JSON.parse(result)).toMatchObject({schema:'millforge-result/v4',status:'completed',counts:{tool_calls:3,nested_tool_calls:2}});const inspected=await invoke(['inspect','--run',out],f.root);expect(inspected.exit).toBe(0);expect(JSON.parse(inspected.out)).toMatchObject({result:{schema:'millforge-result/v4'},capture:{versions:{millforge:'0.2.1',pi:'1.4.0'}}});expect(calls).toBe(2);expect(await fs.readFile(path.join(f.root,'effect.txt'),'utf8')).toBe('kept');await expect(fs.lstat(path.join(out,'pi'))).rejects.toThrow();await expect(fs.lstat(path.join(out,'tmp'))).rejects.toThrow();}finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));}
},15000);
