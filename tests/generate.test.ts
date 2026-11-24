import {afterEach,describe,expect,it,vi} from 'vitest';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {tmpdir} from 'node:os';
import {fork,type ChildProcess} from 'node:child_process';
import {createBinding} from '../src/bindings.js';
import {type Obj,type ProfileV1,parseJson,validateProfile as validateAnyProfile} from '../src/contracts.js';
const validateProfile=(value:unknown):ProfileV1=>validateAnyProfile(value) as ProfileV1;
const roots:string[]=[],peers:ChildProcess[]=[];
afterEach(async()=>{
  vi.restoreAllMocks();vi.unstubAllEnvs();
  for(const child of peers.splice(0)){
    if(child.exitCode!==null||child.signalCode!==null)continue;
    await new Promise<void>(resolve=>{const timer=setTimeout(()=>{child.kill('SIGKILL');},1500);child.once('exit',()=>{clearTimeout(timer);resolve();});if(child.connected)child.send({type:'stop'});else child.kill('SIGTERM');});
  }
  for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});
});
async function setup(api:ProfileV1['model']['api'],plan:Record<string,unknown>[]=[{kind:'normal'}],credential?:string){
  const root=await fs.mkdtemp(path.join(tmpdir(),'millforge-generate-'));roots.push(root);
  vi.stubEnv('PI_OFFLINE','1');vi.stubEnv('PI_CODING_AGENT_DIR',path.join(root,'pi'));vi.stubEnv('TMPDIR',path.join(root,'tmp'));await fs.mkdir(path.join(root,'tmp'));
  vi.stubEnv('OPENAI_API_KEY','AMBIENT_AUTH_SENTINEL');vi.stubEnv('ANTHROPIC_API_KEY','AMBIENT_AUTH_SENTINEL');
  const child=fork(new URL('./fixtures/m2/fake-http-peer.mjs',import.meta.url),[],{execArgv:[],stdio:['ignore','ignore','ignore','ipc'],env:{PATH:process.env.PATH??'',HOME:root}});peers.push(child);
  const requests:Record<string,unknown>[]=[];
  const ready=new Promise<string>((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('fake peer startup expired')),5000);
    child.on('message',message=>{const event=message as Record<string,unknown>;if(event.type==='request')requests.push(event);if(event.type==='ready'){clearTimeout(timer);resolve(String(event.baseUrl));}});
    child.once('error',error=>{clearTimeout(timer);reject(error);});child.once('exit',()=>{clearTimeout(timer);reject(new Error('fake peer exited before ready'));});
  });
  child.send({type:'start',api,plan});const base=await ready;
  const p=validateProfile(parseJson(await fs.readFile(new URL('../profiles/generate/profile.json',import.meta.url))));
  p.model.api=api;p.model.base_url=base+(api==='openai-completions'?'/v1':'');
  if(api==='anthropic-messages')p.model.compat={};
  p.model.credential_env=credential?'TEST_MILLFORGE_KEY':null;
  const trace:{kind:string;payload:Obj}[]=[];const abort=new AbortController();
  const bind=()=>createBinding({profile:p,limits:p.limits,signal:abort.signal,deadline:performance.now()+5000,onTrace:(kind,payload)=>trace.push({kind,payload}),...(credential?{credential}:{})});
  const context={systemPrompt:p.instructions,messages:[{role:'user' as const,content:'fixed fake task',timestamp:Date.now()}]};
  return {root,p,trace,abort,requests,bind,context};
}
const apis=['openai-completions','anthropic-messages'] as const;
describe('generate::one_dispatch and controls through the real pinned public APIs',()=>{
  it.each(apis)('sends one selected native request without ambient authentication: %s',async api=>{
    const f=await setup(api);const binding=await f.bind();const result=await binding.generate(f.context);
    expect(result.text).toBe('done');expect(binding.httpAttempts()).toBe(1);expect(binding.inferenceReturned()).toBe(true);
    expect(f.requests).toHaveLength(1);expect(f.requests[0]).toMatchObject({method:'POST',authorization:false,apiKey:false,path:api==='openai-completions'?'/v1/chat/completions':'/v1/messages?beta=true'});
    expect(f.requests[0]!.controls).toMatchObject({model:f.p.model.id,stream:true,temperature:f.p.sampling.temperature});
    expect(f.trace.filter(t=>t.kind==='provider_payload')).toHaveLength(1);expect(f.trace.filter(t=>t.kind==='http_attempt')).toHaveLength(1);
    expect(result.usage).toMatchObject({input_tokens:7,output_tokens:3,cache_read_tokens:0});
    await expect(binding.generate(f.context)).rejects.toThrow();expect(f.requests).toHaveLength(1);expect(binding.httpAttempts()).toBe(1);
  });
  it('uses public OpenAI samplingParams for explicit top-p',async()=>{
    const f=await setup('openai-completions');f.p.sampling.top_p=0.75;const binding=await f.bind();await binding.generate(f.context);expect(f.requests[0]!.controls).toMatchObject({top_p:0.75});
  });
  it.each(apis)('retains a lower positive native context-clamped ceiling: %s',async api=>{
    const f=await setup(api);f.p.model.context_tokens=4097;const binding=await f.bind();await binding.generate(f.context);
    const controls=f.requests[0]!.controls as Record<string,number>;const field=api==='anthropic-messages'?'max_tokens':String(f.p.model.compat.maxTokensField);
    expect(controls[field]).toBeGreaterThan(0);expect(controls[field]).toBeLessThan(f.p.model.max_output_tokens);
    expect(f.trace.find(t=>t.kind==='provider_payload')!.payload.controls).toMatchObject({[field]:controls[field]});
  });
  it.each(apis)('keeps absent usage unknown despite native normalized zeros: %s',async api=>{
    const f=await setup(api,[{kind:'normal',usage:false}]);const binding=await f.bind();expect((await binding.generate(f.context)).usage).toBeNull();
  });
  it.each(apis)('checks the actual serialized body after public onPayload: %s',async api=>{
    for(const invalid of [0,true,'128','widen']){
      const f=await setup(api);const binding=await f.bind();const stringify=JSON.stringify;let intercepted=0;
      const spy=vi.spyOn(JSON,'stringify').mockImplementation((value,replacer,space)=>{
        if(value&&typeof value==='object'&&value.model===f.p.model.id&&value.stream===true&&Array.isArray(value.messages)){
          intercepted++;const field=api==='anthropic-messages'?'max_tokens':String(f.p.model.compat.maxTokensField);value={...value,[field]:invalid==='widen'?f.p.model.max_output_tokens+1:invalid};
        }
        return stringify(value,replacer,space);
      });
      try{await expect(binding.generate(f.context)).rejects.toThrow();expect(intercepted).toBeGreaterThan(0);expect(binding.httpAttempts()).toBe(0);expect(f.requests).toHaveLength(0);expect(f.trace.filter(t=>t.kind==='provider_payload')).toHaveLength(1);expect(f.trace.filter(t=>t.kind==='http_attempt')).toHaveLength(0);}finally{spy.mockRestore();}
    }
  });
  it.each(apis)('refuses a final-body control mismatch before HTTP: %s',async api=>{
    const f=await setup(api);const binding=await f.bind();const stringify=JSON.stringify;let intercepted=0;
    const spy=vi.spyOn(JSON,'stringify').mockImplementation((value,replacer,space)=>{if(value&&typeof value==='object'&&value.model===f.p.model.id&&value.stream===true&&Array.isArray(value.messages)){intercepted++;value={...value,temperature:0.99};}return stringify(value,replacer,space);});
    try{await expect(binding.generate(f.context)).rejects.toThrow();expect(intercepted).toBeGreaterThan(0);expect(binding.httpAttempts()).toBe(0);expect(f.requests).toHaveLength(0);}finally{spy.mockRestore();}
  });
  it.each(apis)('rejects an unselected identity before any dispatch: %s',async api=>{
    const f=await setup(api);const binding=await f.bind();const pi=await import('@earendil-works/pi-ai');const message=await binding.provider.streamSimple({...binding.model,baseUrl:'http://127.0.0.1:1'},pi.normalizeContext(f.context)).result();
    expect(message.stopReason).toBe('error');expect(binding.httpAttempts()).toBe(0);expect(f.requests).toHaveLength(0);
  });
});
describe('generate::finish_rejections and finite transport',()=>{
  it.each(apis.flatMap(api=>['empty','missing-finish','length','unknown-finish','tool','malformed','disconnect'].map(kind=>({api,kind}))))('refuses $api $kind without a repair call',async({api,kind})=>{
    const f=await setup(api,[{kind}]);const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(f.requests).toHaveLength(1);expect(binding.httpAttempts()).toBe(1);expect(f.trace.filter(t=>t.kind==='model_end')).toHaveLength(1);
  });
  it.each(['pause_turn','stop_sequence'])('refuses Anthropic native %s even when normalized to stop',async reason=>{
    const f=await setup('anthropic-messages',[{kind:'normal',reason}]);const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(f.requests).toHaveLength(1);expect(f.trace.find(t=>t.kind==='model_end')!.payload.stop).not.toBe('normal');
  });
  it.each(apis.flatMap(api=>[429,500].map(status=>({api,status}))))('does not retry $api HTTP $status',async({api,status})=>{
    const f=await setup(api,[{kind:'status',status},{kind:'normal'}]);const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(f.requests).toHaveLength(1);expect(binding.httpAttempts()).toBe(1);expect(JSON.stringify(f.trace)).not.toContain('SECRET_SENTINEL');
  });
  it.each(apis)('refuses redirects instead of dispatching to another route: %s',async api=>{
    const f=await setup(api,[{kind:'redirect'},{kind:'normal'}]);const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(f.requests).toHaveLength(1);expect(binding.httpAttempts()).toBe(1);
  });
  it.each(apis)('aborts an in-flight request under the original deadline: %s',async api=>{
    const f=await setup(api,[{kind:'delay',delayMs:500}]);const binding=await createBinding({profile:f.p,limits:f.p.limits,signal:f.abort.signal,deadline:performance.now()+100,onTrace:(kind,payload)=>f.trace.push({kind,payload})});
    await expect(binding.generate(f.context)).rejects.toThrow();expect(binding.httpAttempts()).toBeLessThanOrEqual(1);expect(f.trace.filter(t=>t.kind==='model_end')).toHaveLength(1);
  });
  it.each(apis)('refuses cancellation before any effect: %s',async api=>{
    const f=await setup(api);f.abort.abort();const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(binding.httpAttempts()).toBe(0);expect(f.requests).toHaveLength(0);
  });
});
describe('generate::redaction and output admission',()=>{
  it.each(apis)('omits private reasoning and redacts credential text across chunks: %s',async api=>{
    const f=await setup(api,[{kind:'reasoning',textChunks:['SECRET_','SENTINEL']}],'SECRET_SENTINEL');const binding=await f.bind();const result=await binding.generate(f.context);
    expect(result.text).toBe('[redacted]');const saved=JSON.stringify(f.trace);expect(saved).not.toContain('SECRET_SENTINEL');expect(saved).not.toContain('PRIVATE_REASONING_SENTINEL');expect(saved).not.toContain('AMBIENT_AUTH_SENTINEL');expect(f.trace.find(t=>t.kind==='output_partial')!.payload.text).toBe('[redacted]');
  });
  it.each(apis)('stops before retaining an oversized text chunk: %s',async api=>{
    const f=await setup(api,[{kind:'normal',text:'x'.repeat(2048)}]);f.p.limits.output_bytes=1024;const binding=await f.bind();await expect(binding.generate(f.context)).rejects.toThrow();expect(f.trace.filter(t=>t.kind==='output_partial')).toHaveLength(0);expect(binding.httpAttempts()).toBe(1);
  });
});
