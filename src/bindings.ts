/** Selected public Pi AI bindings. No provider discovery, private resolver or persisted raw payload. */
import type {Api,AssistantMessage,AssistantMessageEventStream,Context,Model,Provider,SimpleStreamOptions,StreamFunction,TranscriptContext} from '@earendil-works/pi-ai';
import {type Capture,type Json,type Limits,type Obj,type Profile,type ProfileV1,type ProfileV2,type ProfileV3,type ProfileV4,type ThinkingLevel,type Usage,type UsageV3,currentRuntimeGuards,MillforgeError,bytes,canonical,exact,integer,limitKeys,object,parseJson,refuse,sourcePin,validateLimits,validateProfile} from './contracts.js';
import type {PythonActionObserver} from './python-r1-host.js';
import {type OAuthLease,oauthBase,oauthOnly,resolveOAuthLease} from './auth.js';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {findPackageJSON} from 'node:module';
import {dirname,join} from 'node:path';

export interface BindingOptions {
  profile:Profile;limits:Limits;signal:AbortSignal;deadline:number;
  onTrace:(kind:string,payload:Obj)=>void;onPartial?:(text:string)=>void;
  credential?:string;fetch?:typeof globalThis.fetch;authHome?:string;profileDigest?:string;getCredential?:()=>Promise<string|undefined>;
  pythonObserver?:Pick<PythonActionObserver,'begin'|'observe'|'complete'>;
}
export interface BindingCompletion {message:AssistantMessage;text:string;usage:Usage|null}
export interface Binding {
  model:Model<Api>;provider:Provider;binding:Capture['binding'];
  generate:(context:Context)=>Promise<BindingCompletion>;assertNormal:(message:AssistantMessage)=>void;
  usage:()=>Usage|UsageV3|null;httpAttempts:()=>number;inferenceReturned:()=>boolean;
  thinkingLevel?:ThinkingLevel;oauthLease?:OAuthLease;finalText?:(message:AssistantMessage)=>string;
}
const MAX_WIRE_BYTES=32*1024*1024;
const emptyUsage=():Usage=>({input_tokens:null,output_tokens:null,cache_read_tokens:null,cache_write_tokens:null});
function record(value:unknown):Record<string,unknown>|undefined{return value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:undefined;}
function token(value:unknown):number|null{return typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?value:null;}

/** Hash the actual API-specific member, without retaining declarations or request content. */
export function codemodeDeclaration(value:unknown,api:string):Obj{
  try{
    const payload=object(value);if(!Array.isArray(payload.tools))refuse('codemode declaration missing');
    const declarations=payload.tools.filter(entry=>{const t=object(entry);return api==='anthropic-messages'?t.name==='codemode':api==='openai-completions'?object(t.type==='custom'?t.custom:t.function).name==='codemode':t.name==='codemode';});
    if(declarations.length!==1)refuse('codemode declaration must be unique');
    const tool=object(declarations[0]),body=api==='openai-completions'?object(tool.type==='custom'?tool.custom:tool.function):tool;
    let grammar:Json|undefined,schema:Json|undefined;
    if(api==='anthropic-messages')schema=object(body.input_schema);
    else if(tool.type==='custom'){
      const format=exact(body.format,['type',...(api==='openai-responses'?['syntax','definition']:['grammar'])]);if(format.type!=='grammar')refuse('unsupported codemode format');
      const g=api==='openai-responses'?format:exact(format.grammar,['syntax','definition']);
      if(typeof g.syntax!=='string'||typeof g.definition!=='string')refuse('invalid codemode grammar');grammar=g;
    }else if(tool.type==='function')schema=object(body.parameters);
    else refuse('unsupported codemode declaration shape');
    const sha=(v:Json):string=>createHash('sha256').update(canonical(v)).digest('hex');
    return grammar?{format:'grammar',grammar_sha256:sha(grammar),schema_sha256:null}:{format:'json-schema',grammar_sha256:null,schema_sha256:sha(schema!)};
  }catch{return refuse('unsupported or missing codemode declaration','INCOMPATIBLE_ROUTE');}
}

export async function createBinding(options:BindingOptions):Promise<Binding>{
  const selected=validateProfile(structuredClone(options.profile));
  if(selected.schema!=='millforge-profile/v1')return createSuccessorBinding({...options,profile:selected});
  return createLegacyBinding({...options,profile:selected});
}
async function createLegacyBinding(options:BindingOptions&{profile:ProfileV1}):Promise<Binding>{
  const profile=options.profile,limits=validateLimits({...options.limits},profile.mode);
  for(const key of limitKeys)if(limits[key]>profile.limits[key])refuse('binding limits widen profile');
  if(profile.model.credential_env!==null&&!options.credential)refuse('named credential unavailable','AUTH_UNAVAILABLE');
  if(profile.model.credential_env===null&&options.credential!==undefined)refuse('unexpected credential for keyless profile');
  const publicPi=await import('@earendil-works/pi-ai');
  const native=profile.model.api==='openai-completions'?await import('@earendil-works/pi-ai/api/openai-completions'):await import('@earendil-works/pi-ai/api/anthropic-messages');
  const nativeStream=native.streamSimple as StreamFunction<Api,SimpleStreamOptions>;
  const providerId='millforge-selected';
  const suffix=profile.model.api==='openai-completions'?'/chat/completions':'/v1/messages?beta=true';
  // Both pinned SDK clients use baseURL + resource path, then URL.toString().
  const requestUrl=new URL(profile.model.base_url+(profile.model.base_url.endsWith('/')?suffix.slice(1):suffix)).href;
  const model={id:profile.model.id,name:profile.model.id,api:profile.model.api,provider:providerId,baseUrl:profile.model.base_url,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},reasoning:false,contextWindow:profile.model.context_tokens,maxTokens:profile.model.max_output_tokens,compat:structuredClone(profile.model.compat)} as Model<Api>;
  const binding:Capture['binding']={api:model.api,version:profile.runtime.binding_version,provider_id:providerId,model_id:model.id,base_url:model.baseUrl,request_url:requestUrl,source_pin:sourcePin,compat:{authored_overrides:structuredClone(profile.model.compat),internal_resolution:'unknown'},requested_options:{...profile.sampling,thinking:'off',max_retries:0}};
  const abort=new AbortController();let fault:MillforgeError|undefined,attempts=0,httpCount=0,active=false,returned=false,partialBytes=0,retainedBytes=0;
  const usages:Usage[]=[];
  const redact=(value:string):string=>options.credential?value.split(options.credential).join('[redacted]'):value;
  function fail(code:MillforgeError['code'],message:string):never{fault??=new MillforgeError(code,message);abort.abort();throw fault;}
  function check():void{
    if(fault)throw fault;
    if(performance.now()>=options.deadline)fail('TIMEOUT','work deadline expired');
    if(options.signal.aborted)fail('CANCELLED','invocation cancelled');
  }
  function identity(selected:Model<Api>):void{if(selected.api!==model.api||selected.id!==model.id||selected.provider!==model.provider||selected.baseUrl!==model.baseUrl)fail('INCOMPATIBLE_ROUTE','unselected dispatch identity');}
  function controls(value:unknown):Obj{
    const p=object(value,'provider payload');
    const own=(key:string):unknown=>Object.hasOwn(p,key)?p[key]:undefined;
    if(own('model')!==model.id||own('stream')!==true||own('temperature')!==profile.sampling.temperature)fail('INCOMPATIBLE_ROUTE','required payload control absent or changed');
    const top=own('top_p');if(profile.sampling.top_p===null?top!==undefined:top!==profile.sampling.top_p)fail('INCOMPATIBLE_ROUTE','top-p control absent or changed');
    const field=profile.model.api==='openai-completions'?String(profile.model.compat.maxTokensField):'max_tokens';
    integer(own(field),1,profile.model.max_output_tokens);
    if(own(field==='max_tokens'?'max_completion_tokens':'max_tokens')!==undefined)fail('INCOMPATIBLE_ROUTE','unexpected alternate token control');
    // Only this allowlist leaves the live request. Null denotes absence, never wire null.
    return {model:model.id,stream:true,temperature:profile.sampling.temperature,top_p:top===undefined?null:top as number,max_tokens:field==='max_tokens'?own(field) as number:null,max_completion_tokens:field==='max_completion_tokens'?own(field) as number:null};
  }
  function totalUsage():Usage|null{
    if(!usages.length)return null;const result=emptyUsage();
    for(const key of Object.keys(result) as (keyof Usage)[]){let sum=0,known=true;for(const value of usages){if(value[key]===null){known=false;break;}sum+=value[key]!;if(!Number.isSafeInteger(sum)){known=false;break;}}result[key]=known?sum:null;}
    return Object.values(result).every(value=>value===null)?null:result;
  }
  const normal=(message:AssistantMessage):boolean=>message.stopReason==='stop'&&(profile.model.api==='openai-completions'?['stop','end']:['end_turn']).includes(message.rawStopReason??'');
  const text=(message:AssistantMessage):string=>redact(message.content.filter(part=>part.type==='text').map(part=>part.text).join(''));
  function assertNormal(message:AssistantMessage):void{
    check();if(!normal(message)||message.content.some(part=>part.type==='toolCall'))fail('INVALID_FINISH','native normal finish required');
    const value=text(message);if(!value.length)fail('INVALID_FINISH','empty model output');if(bytes(value)>limits.output_bytes)fail('OUTPUT_LIMIT','model output exceeds limit');
  }
  function start(selected:Model<Api>,context:TranscriptContext,caller?:SimpleStreamOptions):AssistantMessageEventStream{
    const output=publicPi.createAssistantMessageEventStream();
    void (async()=>{
      let attempt=0,endEmitted=false,ended:AssistantMessage|undefined;
      try{
        check();identity(selected);if(active)fail('LIMIT_EXHAUSTED','parallel model dispatch refused');
        if(attempts>=limits.model_requests)fail('LIMIT_EXHAUSTED','model request ceiling reached');
        active=true;attempt=++attempts;returned=false;options.onTrace('model_start',{attempt});
        const observed=emptyUsage();usages.push(observed);let streamedText='';let payload:Obj|undefined,attemptHttp=0,responseBytes=0;
        const remaining=Math.min(limits.model_timeout_seconds*1000,options.deadline-performance.now());
        if(remaining<=0)fail('TIMEOUT','model deadline expired');
        const signal=AbortSignal.any([options.signal,abort.signal,AbortSignal.timeout(Math.max(1,Math.floor(remaining))),...(caller?.signal?[caller.signal]:[])]);
        const countedFetch:typeof globalThis.fetch=async(input,init)=>{
          check();if(signal.aborted)fail('TIMEOUT','model request aborted');
          const url=typeof input==='string'?input:input instanceof URL?input.href:input.url;
          if(url!==requestUrl||init?.method?.toUpperCase()!=='POST')fail('INCOMPATIBLE_ROUTE','unselected HTTP route');
          if(!payload||attemptHttp||profile.mode==='generate'&&httpCount)fail('LIMIT_EXHAUSTED','extra HTTP dispatch refused');
          const body=init?.body;if(typeof body!=='string'||bytes(body)>MAX_WIRE_BYTES)fail('CONTEXT_LIMIT','unsupported or oversized outgoing body');
          const actual=controls(parseJson(body));if(canonical(actual)!==canonical(payload))fail('INCOMPATIBLE_ROUTE','outgoing controls differ from payload observation');
          const headers=new Headers(init.headers);
          if(profile.model.credential_env===null&&(headers.has('authorization')||headers.has('x-api-key')))fail('AUTH_UNAVAILABLE','keyless request carried authentication');
          if(profile.model.credential_env!==null&&(profile.model.api==='openai-completions'?headers.get('authorization')!=='Bearer '+options.credential||headers.has('x-api-key'):headers.get('x-api-key')!==options.credential||headers.has('authorization')))fail('AUTH_UNAVAILABLE','unselected authentication route');
          check();attemptHttp++;httpCount++;options.onTrace('http_attempt',{attempt,source:'counted_fetch',method:'POST',url});
          const response=await (options.fetch??globalThis.fetch)(input,{...init,signal,redirect:'manual'});
          if(response.status>=300&&response.status<400){await response.body?.cancel();fail('INCOMPATIBLE_ROUTE','redirect refused');}
          if(!response.body)return response;
          const reader=response.body.getReader();
          const bodyStream=new ReadableStream<Uint8Array>({
            async pull(controller){try{const next=await reader.read();if(next.done){controller.close();return;}responseBytes+=next.value.byteLength;if(responseBytes>MAX_WIRE_BYTES){await reader.cancel();fail('OUTPUT_LIMIT','encoded response exceeds bound');}controller.enqueue(next.value);}catch(error){controller.error(error);}},
            async cancel(){await reader.cancel();}
          });
          return new Response(bodyStream,{status:response.status,statusText:response.statusText,headers:response.headers});
        };
        const nativeOptions:SimpleStreamOptions={signal,timeoutMs:Math.max(1,Math.floor(remaining)),apiKey:options.credential??'millforge-no-auth',headers:profile.model.credential_env===null?{authorization:null,'x-api-key':null}:{},env:{},fetch:countedFetch,maxRetries:0,maxRetryDelayMs:1,temperature:profile.sampling.temperature,maxTokens:profile.model.max_output_tokens,cacheRetention:'none',...(profile.model.api==='openai-completions'&&profile.sampling.top_p!==null?{samplingParams:{top_p:profile.sampling.top_p}}:{}),
          onPayload:(value,actualModel)=>{check();identity(actualModel);if(payload)fail('LIMIT_EXHAUSTED','additional payload attempt refused');payload=Object.freeze(controls(value));options.onTrace('provider_payload',{attempt,source:'pi_onPayload',api:model.api,provider_id:model.provider,model_id:model.id,base_url:model.baseUrl,controls:{...payload}});return undefined;},
          onProviderStreamEvent:(value,actualModel)=>{
            identity(actualModel);const raw=record(value);if(!raw)return;
            if(profile.model.api==='openai-completions'){
              const usage=record(raw.usage);if(usage){observed.input_tokens=token(usage.prompt_tokens);observed.output_tokens=token(usage.completion_tokens);observed.cache_read_tokens=token(record(usage.prompt_tokens_details)?.cached_tokens);}
            }else{
              const usage=record(raw.type==='message_start'?record(raw.message)?.usage:raw.usage);if(usage)for(const [to,from]of [['input_tokens','input_tokens'],['output_tokens','output_tokens'],['cache_read_tokens','cache_read_input_tokens'],['cache_write_tokens','cache_creation_input_tokens']] as const)if(Object.hasOwn(usage,from))observed[to]=token(usage[from]);
            }
          }
        };
        const stream=nativeStream(selected,context,nativeOptions);
        for await(const event of stream){
          if(event.type==='text_delta'){
            check();partialBytes+=bytes(event.delta);if(partialBytes>limits.output_bytes)fail('OUTPUT_LIMIT','aggregate model text exceeds limit');
            streamedText+=event.delta; // Bounded by the aggregate ceiling; redact across arbitrary chunk boundaries.
          }
          if(event.type==='done'||event.type==='error'){
            const original=event.type==='done'?event.message:event.error;
            if(bytes(original.content.filter(part=>part.type==='text').map(part=>part.text).join(''))>limits.output_bytes)fail('OUTPUT_LIMIT','model text exceeds limit');
            const message={...original,content:original.content.map(part=>part.type==='text'?{...part,text:redact(part.text)}:part)};
            delete message.errorMessage;delete message.diagnostics;
            const tool=message.stopReason==='toolUse'&&(profile.model.api==='openai-completions'?['tool_calls','function_call']:['tool_use']).includes(message.rawStopReason??'');
            let stop=normal(message)?'normal':tool?'tool':message.stopReason==='length'?'length':message.stopReason==='aborted'?'aborted':'unknown';
            if(event.type==='error')stop='error';
            if(event.type==='error'||!normal(message)&&!tool){
              if(!fault)fault=new MillforgeError(signal.aborted?'TIMEOUT':event.type==='error'?'PROVIDER_ERROR':'INVALID_FINISH','model request did not finish normally');
              message.stopReason=signal.aborted?'aborted':'error';message.errorMessage='model request failed';delete message.rawStopReason;
            }
            returned=event.type==='done'&&!!original.rawStopReason;
            const partial=redact(streamedText);if(partial){retainedBytes+=bytes(partial);if(retainedBytes>limits.output_bytes)fail('OUTPUT_LIMIT','sanitized model text exceeds limit');options.onTrace('output_partial',{text:partial});options.onPartial?.(partial);}
            ended=message;endEmitted=true;options.onTrace('model_end',{attempt,stop,usage:Object.values(observed).every(value=>value===null)?null:observed as unknown as Obj});
            if(message.stopReason==='error'||message.stopReason==='aborted')output.push({type:'error',reason:message.stopReason,error:message});else output.push({...event,message} as typeof event);
          }else output.push(event);
        }
        if(!ended)fail('PROVIDER_ERROR','provider stream ended without a result');
      }catch(error){
        const diagnostic=error instanceof MillforgeError?error:fault??new MillforgeError(options.signal.aborted?'CANCELLED':performance.now()>=options.deadline?'TIMEOUT':'PROVIDER_ERROR','model request failed');fault??=diagnostic;abort.abort();
        if(attempt&&!endEmitted){endEmitted=true;try{options.onTrace('model_end',{attempt,stop:'error',usage:null});}catch{/* The owned trace writer retains its original failure. */}}
        const message:AssistantMessage={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:options.signal.aborted?'aborted':'error',errorMessage:diagnostic.message,timestamp:Date.now()};
        output.push({type:'error',reason:message.stopReason as 'error'|'aborted',error:message});
      }finally{active=false;output.end();}
    })();
    return output;
  }
  const provider=publicPi.createProvider({id:providerId,name:'Millforge selected route',baseUrl:model.baseUrl,models:[model],auth:{apiKey:{name:'Explicit launch credential',resolve:async()=>({auth:{apiKey:options.credential??'millforge-no-auth'},source:'explicit launch'})}},api:{stream:(selected,context,caller)=>start(selected,context,caller),streamSimple:start}});
  return {model,provider,binding,assertNormal,usage:totalUsage,httpAttempts:()=>httpCount,inferenceReturned:()=>returned,
    generate:async context=>{const message=await start(model,publicPi.normalizeContext(context)).result();assertNormal(message);return {message,text:text(message),usage:totalUsage()};}
  };
}

/** The successor keeps native models/continuations intact and projects only public request facts. */
async function createSuccessorBinding(options:BindingOptions&{profile:ProfileV2|ProfileV3|ProfileV4}):Promise<Binding>{
  const profile=options.profile,limits=validateLimits({...options.limits},profile.mode,profile.schema);
  const pythonObserver=profile.schema==='millforge-profile/v4'?options.pythonObserver:undefined;
  if(profile.schema==='millforge-profile/v4'&&!pythonObserver)refuse('Python action observer is required','UNSUPPORTED_CAPABILITY');
  for(const key of limitKeys)if(limits[key]>profile.limits[key])refuse('binding limits widen profile');
  const api=await import('@earendil-works/pi-ai'),pi=await import('@earendil-works/pi-coding-agent');
  const empty=new api.InMemoryCredentialStore();
  const catalog=await pi.ModelRuntime.create({credentials:empty,modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
  const catalogModel=catalog.getModel(profile.model.provider,profile.model.id),catalogProvider=catalog.getProvider(profile.model.provider);
  let model:Model<Api>,catalogHash:string|null=null,catalogCompat:Obj={};
  const overrides=structuredClone(profile.model.metadata.overrides);
  if(profile.model.metadata.source==='pinned-catalog'){
    if(!catalogModel||catalogModel.api!==profile.model.api||catalogModel.baseUrl!==profile.model.base_url)refuse('selected pinned catalog model is unavailable','UNSUPPORTED_CAPABILITY');
    model={...structuredClone(catalogModel),...overrides,compat:{...structuredClone(catalogModel.compat??{}),...profile.model.compat}} as Model<Api>;
    catalogCompat=structuredClone(catalogModel.compat??{}) as Obj;
    // Hash package data only for provenance. Dispatch/capabilities come from the public runtime.
    if(!/^[a-z0-9-]+$/.test(profile.model.provider))refuse('catalog provenance is unavailable','UNSUPPORTED_CAPABILITY');
    const packageFile=findPackageJSON('@earendil-works/pi-ai',import.meta.url);
    if(!packageFile)refuse('catalog package provenance is unavailable','UNSUPPORTED_CAPABILITY');
    catalogHash=createHash('sha256').update(await readFile(join(dirname(packageFile),'dist','providers','data',profile.model.provider+'.json'))).digest('hex');
  }else{
    model={...overrides,id:profile.model.id,api:profile.model.api,provider:profile.model.provider,baseUrl:profile.model.base_url,compat:structuredClone(profile.model.compat),cost:overrides.cost??{input:0,output:0,cacheRead:0,cacheWrite:0}} as unknown as Model<Api>;
  }
  if(overrides.cost===null)model.cost={input:0,output:0,cacheRead:0,cacheWrite:0};
  if(profile.model.context_tokens>model.contextWindow||profile.model.max_output_tokens!==null&&profile.model.max_output_tokens>model.maxTokens)refuse('authored token budget exceeds selected capacity','UNSUPPORTED_CAPABILITY');
  const supported=api.getSupportedThinkingLevels(model);
  if(profile.reasoning!==undefined&&!supported.includes(profile.reasoning))refuse('selected reasoning level is unsupported','UNSUPPORTED_CAPABILITY');
  const thinking=profile.reasoning??api.clampThinkingLevel(model,'medium');
  const mapped=model.thinkingLevelMap?.[thinking]??(thinking==='off'?null:thinking);
  const cache=profile.cache?.retention??'short';
  const native=profile.model.api==='openai-responses'?await import('@earendil-works/pi-ai/api/openai-responses'):profile.model.api==='openai-completions'?await import('@earendil-works/pi-ai/api/openai-completions'):await import('@earendil-works/pi-ai/api/anthropic-messages');
  const nativeStream=native.streamSimple as StreamFunction<Api,SimpleStreamOptions>;
  if(profile.model.auth.kind==='oauth'&&(!catalogProvider||profile.model.provider!=='openai'||profile.model.api!=='openai-responses'||model.baseUrl!==oauthBase))refuse('OAuth public route is unavailable','UNSUPPORTED_CAPABILITY');
  const admissionProvider=api.createProvider({id:model.provider,name:catalogProvider?.name??model.provider,baseUrl:model.baseUrl,models:[model],auth:profile.model.auth.kind==='oauth'?oauthOnly(catalogProvider!).auth:{apiKey:{name:'Capability admission without credentials',resolve:async()=>undefined}},api:{stream:nativeStream,streamSimple:nativeStream}});
  catalog.registerNativeProvider(admissionProvider);await catalog.refresh({allowNetwork:false,signal:options.signal});
  if(canonical(catalog.getModel(model.provider,model.id) as unknown as Obj)!==canonical(model as unknown as Obj))refuse('runtime model differs before auth admission','UNSUPPORTED_CAPABILITY');
  let lease:OAuthLease|undefined,credential=options.credential;
  if(profile.model.auth.kind==='oauth'){
    if(!catalogProvider||profile.model.provider!=='openai'||profile.model.api!=='openai-responses'||model.baseUrl!==oauthBase)refuse('OAuth public route is unavailable','UNSUPPORTED_CAPABILITY');
    if(!options.authHome)refuse('selected OAuth home is unavailable','AUTH_UNAVAILABLE');
    lease=await resolveOAuthLease(options.authHome,oauthOnly(catalogProvider),limits.elapsed_seconds*1000+300000,options.signal);credential=lease.credential.access;
  }else if(profile.model.auth.kind==='env'){credential=credential??await options.getCredential?.();if(!credential)refuse('named credential unavailable','AUTH_UNAVAILABLE');}
  else if(profile.model.auth.kind==='none'&&credential!==undefined)refuse('unexpected credential for keyless profile','AUTH_UNAVAILABLE');
  const requestUrl=profile.model.base_url.replace(/\/$/,'')+(profile.model.api==='openai-responses'?'/responses':profile.model.api==='openai-completions'?'/chat/completions':'/v1/messages?beta=true');
  const selected:Obj={};for(const key of ['name','input','cost','reasoning','thinkingLevelMap','contextWindow','maxTokens','promptCache','inputLimits','samplingParams','samplingParamsByThinkingLevel'])selected[key]=(key==='cost'&&overrides.cost===null?null:(model as unknown as Obj)[key])??null;
  const binding:Capture['binding']={api:model.api,version:2,provider_id:model.provider,model_id:model.id,base_url:model.baseUrl,request_url:requestUrl,source_pin:sourcePin,model_metadata:{source:{kind:profile.model.metadata.source,source_pin:sourcePin,catalog_sha256:catalogHash,profile_digest:options.profileDigest??null},authored_overrides:overrides,selected},compat:{catalog_values:catalogCompat,authored_overrides:structuredClone(profile.model.compat),internal_resolution:'unknown'},auth:lease?.descriptor??{kind:profile.model.auth.kind,provider_id:model.provider,reference:profile.model.auth.kind==='env'?profile.model.auth.name:null,lease:null},requested_options:{...profile.sampling,max_output_tokens:profile.model.max_output_tokens,thinking:profile.reasoning??null,cache_retention:profile.cache?.retention??null,max_retries:0},resolved_options:{thinking,thinking_source:profile.reasoning===undefined?'pi_default':'profile',mapped_effort:mapped,cache_retention:cache,cache_source:profile.cache===undefined?'pi_default':'profile'}};
  let fault:MillforgeError|undefined,attempts=0,httpCount=0,active=false,returned=false,partialBytes=0,lastFinal='';
  const abort=new AbortController(),usages:UsageV3[]=[];
  const scrub=(v:string):string=>{for(const secret of [credential,lease?.credential.refresh].filter((x):x is string=>!!x).sort((a,b)=>b.length-a.length))v=v.split(secret).join('[redacted]');return v;};
  const fail=(code:MillforgeError['code'],message:string):never=>{fault??=new MillforgeError(code,message);abort.abort();throw fault;};
  const check=():void=>{if(fault)throw fault;if(options.signal.aborted)fail('CANCELLED','invocation cancelled');if(performance.now()>=options.deadline)fail('TIMEOUT','work deadline expired');if(lease&&Date.now()+Math.max(0,options.deadline-performance.now())+300000>=lease.credential.expires)fail('AUTH_UNAVAILABLE','OAuth lease no longer covers dispatch');};
  const identity=(m:Model<Api>):void=>{if(m.id!==model.id||m.provider!==model.provider||m.api!==model.api||m.baseUrl!==model.baseUrl)fail('INCOMPATIBLE_ROUTE','selected dispatch identity changed');};
  const count=(v:unknown):number|null=>{if(v===undefined||v===null)return null;if(typeof v!=='number'||!Number.isSafeInteger(v)||v<0)return fail('PROVIDER_ERROR','malformed native token count');return v;};
  function controls(value:unknown):Obj{
    const p=object(value),r=record(p.reasoning),t=record(p.thinking),cacheOptions=record(p.prompt_cache_options);
    for(const key of ['store','temperature','top_p','max_tokens','max_completion_tokens','max_output_tokens','reasoning_effort','reasoning','thinking'])if(p[key]===null)fail('INCOMPATIBLE_ROUTE','wire null is not control absence');
    const present=(items:unknown):boolean=>Array.isArray(items)&&items.some(item=>{const q=record(item);return !!q&&(q.cache_control!==undefined||Array.isArray(q.content)&&q.content.some(v=>record(v)?.cache_control!==undefined));});
    const c:Obj={model:p.model??null,stream:p.stream??null,store:p.store??null,temperature:p.temperature??null,top_p:p.top_p??null,max_tokens:p.max_tokens??null,max_completion_tokens:p.max_completion_tokens??null,max_output_tokens:p.max_output_tokens??null,reasoning_effort:p.reasoning_effort??r?.effort as string??null,reasoning_summary:r?.summary as string??null,encrypted_context_requested:Array.isArray(p.include)&&p.include.includes('reasoning.encrypted_content'),thinking:t?{type:t.type as string,budget_tokens:t.budget_tokens as number??null}:null,cache:{prompt_cache_key_present:p.prompt_cache_key!==undefined,prompt_cache_retention:p.prompt_cache_retention??null,prompt_cache_options:cacheOptions?{mode:cacheOptions.mode as string??null,ttl:cacheOptions.ttl as string??null}:null,message_cache_control_present:present(p.messages),tool_cache_control_present:present(p.tools)}};
    if(c.model!==model.id||c.stream!==true)fail('INCOMPATIBLE_ROUTE','payload identity/control changed');
    for(const key of ['temperature','top_p'] as const)if(profile.sampling[key]!==null&&c[key]!==profile.sampling[key])fail('INCOMPATIBLE_ROUTE','requested sampling control omitted');
    const ceilings=['max_tokens','max_completion_tokens','max_output_tokens'].map(k=>c[k]).filter(v=>v!==null);
    for(const ceiling of ceilings)integer(ceiling,1,profile.model.max_output_tokens??Number.MAX_SAFE_INTEGER);
    if(ceilings.length>1||profile.model.max_output_tokens!==null&&ceilings.length!==1)fail('INCOMPATIBLE_ROUTE','requested token ceiling changed');
    if(profile.model.api==='openai-responses'&&(c.store!==false||c.temperature!==null||c.top_p!==null||ceilings.length||c.reasoning_effort!==mapped))fail('INCOMPATIBLE_ROUTE','OAuth native controls changed');
    if(profile.model.api==='openai-responses'&&thinking!=='off'&&(c.reasoning_summary===null||c.encrypted_context_requested!==true))fail('INCOMPATIBLE_ROUTE','native reasoning continuation controls missing');
    if(cache==='none'&&(object(c.cache).prompt_cache_key_present||object(c.cache).message_cache_control_present||object(c.cache).tool_cache_control_present))fail('INCOMPATIBLE_ROUTE','cache none changed');
    return c;
  }
  const totalUsage=():UsageV3|null=>{if(!usages.length)return null;const out:UsageV3={input_tokens:null,output_tokens:null,cache_read_tokens:null,cache_write_tokens:null,reasoning_tokens:null};for(const k of Object.keys(out) as (keyof UsageV3)[]){let sum=0;out[k]=0;for(const u of usages){if(u[k]===null){out[k]=null;break;}sum+=u[k]!;if(!Number.isSafeInteger(sum))fail('PROVIDER_ERROR','unsafe aggregate token count');out[k]=sum;}}return Object.values(out).every(x=>x===null)?null:out;};
  const isNormal=(m:AssistantMessage):boolean=>m.stopReason==='stop'&&(profile.model.api==='openai-responses'?m.rawStopReason==='completed':(profile.model.api==='openai-completions'?['stop','end']:['end_turn']).includes(m.rawStopReason??''));
  const assertNormal=(m:AssistantMessage):void=>{check();if(!isNormal(m)||m.content.some(x=>x.type==='toolCall'))fail('INVALID_FINISH','native final completion required');if(!lastFinal.trim())fail('INVALID_FINISH',profile.model.api==='openai-responses'?'native completed response has no finalized authoritative text':'native final completion required');if(bytes(lastFinal)>limits.output_bytes)fail('OUTPUT_LIMIT','final output exceeds ceiling');};
  function start(m:Model<Api>,context:TranscriptContext,caller?:SimpleStreamOptions):AssistantMessageEventStream{
    const output=api.createAssistantMessageEventStream();
    void(async()=>{
      let attempt=0,emitted=false,ended=false,payload:Obj|undefined,declaration:Obj|undefined,attemptHttp=0,responseBytes=0,text='',terminal:Parameters<typeof output.push>[0]|undefined;
      const observed:UsageV3={input_tokens:null,output_tokens:null,cache_read_tokens:null,cache_write_tokens:null,reasoning_tokens:null};
      let nativeStatus:string|null=null,nativeTerminal:string|null=null,responseModel:string|null=null;
      const anthropic:{input:number|null;read:number|null;write:number|null}={input:null,read:null,write:null};
      const messageItems=new Map<number,{phase:'commentary'|'final_answer'|null;text:string;size:number;done:boolean}>();let messageBytes=0;
      const observeMessage=(index:unknown,item:Record<string,unknown>,source:'added'|'done'|'terminal'):void=>{
        if(item.type!=='message')return;
        if(typeof index!=='number'||!Number.isSafeInteger(index)||index<0)return fail('INVALID_FINISH','finalized native message has no valid output position');
        const prior=messageItems.get(index);
        // Optional terminal copies cannot overwrite an item.done reset, including an empty one.
        if(source==='terminal'&&prior?.done)return;
        const phase=item.phase??(source==='terminal'?prior?.phase:null)??null;if(phase!==null&&phase!=='commentary'&&phase!=='final_answer')return fail('INVALID_FINISH','unsupported native message phase');
        if(source==='added'){if(!prior)messageItems.set(index,{phase,text:'',size:0,done:false});return;}
        if(item.status!==undefined&&item.status!=='completed')return fail('INVALID_FINISH','native message item is unfinished');
        let text='',size=0;
        for(const value of Array.isArray(item.content)?item.content:[]){const part=record(value);if(!part)continue;const valueText=part.type==='output_text'?part.text:part.type==='refusal'?part.refusal:undefined;if(valueText===undefined)continue;if(typeof valueText!=='string')return fail('INVALID_FINISH','malformed finalized native text');size+=bytes(valueText);if(size>limits.output_bytes)return fail('OUTPUT_LIMIT','finalized native text exceeds byte ceiling');text+=valueText;}
        if(prior?.done){if(prior.phase!==phase||prior.text!==text)return fail('INVALID_FINISH','inconsistent finalized native message');return;}
        // A compact terminal copy need not repeat delta text retained by the SDK.
        if(source==='terminal'&&prior&&size===0){prior.phase=phase;return;}
        const total=messageBytes-(prior?.size??0)+size;if(total>limits.output_bytes)return fail('OUTPUT_LIMIT','native message text exceeds byte ceiling');messageBytes=total;messageItems.set(index,{phase,text,size,done:true});
      };
      try{
        check();identity(m);
        // Measure the whole native representation privately; never trim opaque continuation to fit.
        if(bytes(JSON.stringify(context))>limits.context_bytes)fail('CONTEXT_LIMIT','native context representation exceeds byte ceiling');
        if(active||attempts>=limits.model_requests)fail('LIMIT_EXHAUSTED','model request admission refused');active=true;attempt=++attempts;returned=false;usages.push(observed);options.onTrace('model_start',{attempt});
        pythonObserver?.begin();
        const remaining=Math.min(limits.model_timeout_seconds*1000,options.deadline-performance.now());
        const signal=AbortSignal.any([abort.signal,options.signal,AbortSignal.timeout(Math.max(1,Math.floor(remaining))),...(caller?.signal?[caller.signal]:[])]);
        const countedFetch:typeof fetch=async(input,init)=>{
          check();const url=typeof input==='string'?input:input instanceof URL?input.href:input.url;
          if(url!==requestUrl||init?.method?.toUpperCase()!=='POST')fail('INCOMPATIBLE_ROUTE','unselected HTTP route');
          if(!payload||attemptHttp||profile.mode==='generate'&&httpCount)return fail('LIMIT_EXHAUSTED','extra HTTP attempt refused');
          const body=init?.body;if(typeof body!=='string'||bytes(body)>currentRuntimeGuards.encoded_request_bytes)return fail('CONTEXT_LIMIT','encoded request ceiling exceeded');
          const outgoing=parseJson(body);if(canonical(controls(outgoing))!==canonical(payload))fail('INCOMPATIBLE_ROUTE','serialized controls changed');
          if(profile.schema==='millforge-profile/v3'&&canonical(codemodeDeclaration(outgoing,profile.model.api))!==canonical(declaration!))fail('INCOMPATIBLE_ROUTE','serialized codemode declaration changed');
          const headers=new Headers(init?.headers);
          if(lease){if(headers.get('authorization')!=='Bearer '+lease.credential.access||lease.credential.access.startsWith('sk-')||headers.has('x-api-key'))fail('AUTH_UNAVAILABLE','OAuth lease/class changed');}
          else if(profile.model.auth.kind==='none'&&(headers.has('authorization')||headers.has('x-api-key')))fail('AUTH_UNAVAILABLE','keyless request gained credentials');
          attemptHttp++;httpCount++;options.onTrace('http_attempt',{attempt,source:'counted_fetch',method:'POST',url:requestUrl});
          const response=await(options.fetch??globalThis.fetch)(input,{...init,redirect:'error'});if(!response.body)return response;
          const reader=response.body.getReader();return new Response(new ReadableStream<Uint8Array>({async pull(controller){try{const next=await reader.read();if(next.done){controller.close();return;}responseBytes+=next.value.byteLength;if(responseBytes>currentRuntimeGuards.encoded_response_bytes){await reader.cancel();fail('OUTPUT_LIMIT','encoded response ceiling exceeded');}controller.enqueue(next.value);}catch(error){controller.error(error);}},async cancel(){await reader.cancel();}}),{status:response.status,statusText:response.statusText,headers:response.headers});
        };
        const nativeOptions:SimpleStreamOptions={signal,timeoutMs:Math.max(1,Math.floor(remaining)),apiKey:credential??'millforge-no-auth',...(profile.model.auth.kind==='none'?{headers:{authorization:null,'x-api-key':null}}:lease?.auth.auth.headers?{headers:lease.auth.auth.headers}:{}),env:{},fetch:countedFetch,maxRetries:0,maxRetryDelayMs:1,...(profile.sampling.temperature===null?{}:{temperature:profile.sampling.temperature}),...(profile.sampling.top_p===null?{}:{samplingParams:{top_p:profile.sampling.top_p}}),...(profile.model.max_output_tokens===null?{}:{maxTokens:profile.model.max_output_tokens}),...(thinking==='off'?{}:{reasoning:thinking}),...(profile.cache?{cacheRetention:cache}:{}),...(caller?.sessionId?{sessionId:caller.sessionId}:{}),
          onPayload(value,actual){check();identity(actual);if(payload)fail('LIMIT_EXHAUSTED','duplicate payload refused');payload=controls(value);if(profile.schema==='millforge-profile/v3'){try{declaration=codemodeDeclaration(value,profile.model.api);}catch{fail('INCOMPATIBLE_ROUTE','unsupported or missing codemode declaration');}}options.onTrace('provider_payload',{attempt,source:'pi_onPayload',api:model.api,provider_id:model.provider,model_id:model.id,base_url:model.baseUrl,controls:payload,...(declaration?{codemode_declaration:declaration}:{})});return undefined;},
          onProviderStreamEvent(value,actual){identity(actual);pythonObserver?.observe(value);const raw=record(value);if(!raw)return;
            if(profile.model.api==='openai-responses'){
              if(raw.type==='response.output_item.added'||raw.type==='response.output_item.done'){const item=record(raw.item);if(item)observeMessage(raw.output_index,item,raw.type==='response.output_item.added'?'added':'done');}
              if(raw.type==='response.output_text.delta'||raw.type==='response.refusal.delta'){const index=raw.output_index,slot=typeof index==='number'?messageItems.get(index):undefined;if(slot&&!slot.done){if(typeof raw.delta!=='string')return fail('INVALID_FINISH','malformed native text delta');const size=bytes(raw.delta);if(messageBytes+size>limits.output_bytes)return fail('OUTPUT_LIMIT','native message text exceeds byte ceiling');slot.text+=raw.delta;slot.size+=size;messageBytes+=size;}}
              if(typeof raw.type==='string'&&['response.completed','response.incomplete','response.failed'].includes(raw.type)){nativeTerminal=raw.type;const response=record(raw.response);nativeStatus=typeof response?.status==='string'?response.status:null;responseModel=typeof response?.model==='string'?response.model:null;const u=record(response?.usage);if(u){observed.input_tokens=count(u.input_tokens);observed.output_tokens=count(u.output_tokens);observed.cache_read_tokens=count(record(u.input_tokens_details)?.cached_tokens);observed.reasoning_tokens=count(record(u.output_tokens_details)?.reasoning_tokens);}if(raw.type==='response.completed'&&nativeStatus==='completed'&&Array.isArray(response?.output))response.output.forEach((value,index)=>{const item=record(value);if(item)observeMessage(index,item,'terminal');});}
            }else if(profile.model.api==='openai-completions'){
              if(typeof raw.model==='string')responseModel=raw.model;const choices=raw.choices;if(Array.isArray(choices))for(const v of choices){const c=record(v);if(typeof c?.finish_reason==='string'){nativeTerminal='chat.finish_reason';nativeStatus=c.finish_reason;}}
              const u=record(raw.usage);if(u){observed.input_tokens=count(u.prompt_tokens);observed.output_tokens=count(u.completion_tokens);observed.cache_read_tokens=count(record(u.prompt_tokens_details)?.cached_tokens);observed.reasoning_tokens=count(record(u.completion_tokens_details)?.reasoning_tokens);}
            }else{
              const message=record(raw.message);if(typeof message?.model==='string')responseModel=message.model;
              const delta=record(raw.delta);if(typeof delta?.stop_reason==='string'){nativeTerminal='anthropic.message_delta';nativeStatus=delta.stop_reason;}
              const u=record(raw.type==='message_start'?message?.usage:raw.usage);if(u)for(const [to,from] of [['input','input_tokens'],['read','cache_read_input_tokens'],['write','cache_creation_input_tokens']] as const)if(Object.hasOwn(u,from))anthropic[to]=count(u[from]);
              if(u&&Object.hasOwn(u,'output_tokens'))observed.output_tokens=count(u.output_tokens);observed.cache_read_tokens=anthropic.read;observed.cache_write_tokens=anthropic.write;
              if(Object.values(anthropic).every(x=>x!==null)){const total=anthropic.input!+anthropic.read!+anthropic.write!;observed.input_tokens=count(total);}
            }
          }};
        for await(const event of nativeStream(m,context,nativeOptions)){
          if(event.type==='text_delta'){check();partialBytes+=bytes(event.delta);if(partialBytes>limits.output_bytes)fail('OUTPUT_LIMIT','visible model text exceeds ceiling');text+=event.delta;}
          if(event.type==='done'||event.type==='error'){
            const message=event.type==='done'?event.message:event.error;ended=true;
            const terminalOkay=profile.model.api!=='openai-responses'||nativeTerminal==='response.completed'&&nativeStatus==='completed'&&message.rawStopReason==='completed';
            const tool=message.stopReason==='toolUse'&&terminalOkay&&(profile.model.api==='openai-responses'||(profile.model.api==='openai-completions'?['tool_calls','function_call']:['tool_use']).includes(message.rawStopReason??''));
            const normal=isNormal(message)&&terminalOkay;
            let stop=normal?'normal':tool?'tool':event.type==='error'?'error':message.stopReason==='length'?'length':message.stopReason==='aborted'?'aborted':'unknown';
            returned=event.type==='done'&&terminalOkay;
            if(observed.input_tokens!==null&&observed.input_tokens>profile.model.context_tokens)fail('CONTEXT_LIMIT','reported native input exceeds threshold');
            if(!normal&&!tool){fault??=new MillforgeError('INVALID_FINISH','native terminal evidence is not normal');stop=event.type==='error'?'error':stop;}
            if(profile.model.api==='openai-responses'){
              // Genuine completion seals text slots just as the native SDK does, even without item.done.
              const items=normal||tool?[...messageItems].sort(([a],[b])=>a-b).map(([,item])=>item):[],final=items.filter(item=>item.phase==='final_answer');
              lastFinal=scrub((final.length?final:items.filter(item=>item.phase===null)).map(item=>item.text).join(''));
            }else lastFinal=scrub(message.content.filter(c=>c.type==='text').map(c=>c.text).join(''));
            if(text){const partial=scrub(text);options.onTrace('output_partial',{text:partial});options.onPartial?.(partial);}
            emitted=true;returned=event.type==='done'&&terminalOkay;options.onTrace('model_end',{attempt,stop,usage:Object.values(observed).every(v=>v===null)?null:observed as unknown as Obj,response_model:responseModel,native_status:nativeStatus,native_terminal:nativeTerminal});
            terminal=fault?{type:'error',reason:'error',error:{...message,stopReason:'error',errorMessage:fault.message}}:event;
          }else output.push(event);
        }
        if(!ended)fail('PROVIDER_ERROR','native stream ended without result');
      }catch(error){const problem=error instanceof MillforgeError?error:new MillforgeError(options.signal.aborted?'CANCELLED':'PROVIDER_ERROR','native request failed');fault??=problem;abort.abort();if(attempt&&!emitted)try{options.onTrace('model_end',{attempt,stop:'error',usage:Object.values(observed).every(v=>v===null)?null:observed as unknown as Obj,response_model:responseModel,native_status:nativeStatus,native_terminal:nativeTerminal});}catch{/* Keep original failure. */}const message:AssistantMessage={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'error',errorMessage:problem.message,timestamp:Date.now()};terminal={type:'error',reason:'error',error:message};}
      // A terminal push resolves result(); publish only after native iteration has settled.
      finally{active=false;if(terminal?.type==='done')pythonObserver?.complete(terminal.message);else pythonObserver?.begin();if(terminal)output.push(terminal);output.end();}
    })();return output;
  }
  const provider=api.createProvider({id:model.provider,name:catalogProvider?.name??model.provider,baseUrl:model.baseUrl,models:[model],auth:lease?oauthOnly(catalogProvider!).auth:{apiKey:{name:'Explicit launch auth',resolve:async()=>({auth:{apiKey:credential??'millforge-no-auth'},source:'explicit launch'})}},api:{stream:(m,c,o)=>start(m,c,o),streamSimple:start}});
  return {model,provider,binding,thinkingLevel:thinking,...(lease?{oauthLease:lease}:{}),assertNormal,finalText:()=>lastFinal,usage:totalUsage,httpAttempts:()=>httpCount,inferenceReturned:()=>returned,generate:async context=>{const message=await start(model,api.normalizeContext(context)).result();assertNormal(message);return {message,text:lastFinal,usage:totalUsage()};}};
}
