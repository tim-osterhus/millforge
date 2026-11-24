/** Explicit Millforge-owned OAuth storage and public SDK operations. No ambient discovery. */
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import {randomUUID} from 'node:crypto';
import {fork} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline/promises';
import type {AuthInteraction,Credential,CredentialStore,OAuthCredential,Provider,AuthResult} from '@earendil-works/pi-ai';
import {type Obj,MillforgeError,bytes,canonical,exact,parseJson,refuse} from './contracts.js';
import {publicError,regularFile,writeExclusive} from './profile.js';

export const oauthProvider='openai';
export const oauthBase='https://api.openai.com/v1';
const margin=5*60*1000;
export type AuthCommand='login'|'status'|'refresh'|'logout';
export interface AuthOptions {command:AuthCommand;provider:string;authHome?:string;receipt?:string;minValiditySeconds?:number;env:NodeJS.ProcessEnv}
export interface OAuthLease {credential:OAuthCredential;store:CredentialStore;auth:AuthResult;descriptor:Obj}
export function authHome(explicit:string|undefined,env:NodeJS.ProcessEnv):string {
  const home=explicit??(env.XDG_CONFIG_HOME?path.join(env.XDG_CONFIG_HOME,'millforge','auth'):env.HOME?path.join(env.HOME,'.config','millforge','auth'):refuse('auth home is unavailable','AUTH_UNAVAILABLE'));
  if(!path.isAbsolute(home))refuse('auth home must be absolute');return path.resolve(home);
}
/** Check each existing path component before creating or reading an owned file. */
async function realPath(file:string,missing=false):Promise<void>{
  let current=path.parse(file).root;
  for(const part of file.slice(current.length).split(path.sep).filter(Boolean)){
    current=path.join(current,part);try{const st=await fs.lstat(current);if(st.isSymbolicLink())refuse('symlinked auth path refused','AUTH_UNAVAILABLE');}catch(error){if(missing&&(error as NodeJS.ErrnoException).code==='ENOENT')return;throw error;}
  }
}
export async function ownedAuthHome(home:string,create=false):Promise<void>{
  try{await checkOwnedAuthHome(home,create);}catch(error){if(error instanceof MillforgeError&&error.code==='AUTH_UNAVAILABLE')throw error;throw new MillforgeError('AUTH_UNAVAILABLE','owned OAuth home is missing or unsafe');}
}
async function checkOwnedAuthHome(home:string,create:boolean):Promise<void>{
  await realPath(home,create);let made=false;
  try{const st=await fs.lstat(home);if(!st.isDirectory()||st.isSymbolicLink())refuse('auth home must be a real directory','AUTH_UNAVAILABLE');}
  catch(error){if(!create||(error as NodeJS.ErrnoException).code!=='ENOENT')throw new MillforgeError('AUTH_UNAVAILABLE','owned auth home is unavailable');await fs.mkdir(path.dirname(home),{recursive:true,mode:0o700});await realPath(path.dirname(home));await fs.mkdir(home,{mode:0o700});made=true;}
  const marker=path.join(home,'ownership.json');
  if(made)await writeExclusive(marker,JSON.stringify({schema:'millforge-auth/v1',application:'millforge'})+'\n');
  const owner=exact(parseJson(await regularFile(marker,2048)),['schema','application']);
  if(owner.schema!=='millforge-auth/v1'||owner.application!=='millforge')refuse('foreign auth home refused','AUTH_UNAVAILABLE');
  for(const name of ['ownership.json','auth.json','host-id.json']){try{await realPath(path.join(home,name));const st=await fs.lstat(path.join(home,name));if(!st.isFile()||(st.mode&0o077)!==0)refuse('auth file ownership or mode is unsafe','AUTH_UNAVAILABLE');}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}}
  const st=await fs.stat(home);if((st.mode&0o077)!==0)refuse('auth home mode must be 0700','AUTH_UNAVAILABLE');
}
export function oauthCredential(value:unknown):OAuthCredential{
  if(!value||typeof value!=='object'||Array.isArray(value))refuse('selected OAuth connection is unavailable','AUTH_UNAVAILABLE');
  const c=value as Record<string,unknown>;
  if(c.type!=='oauth'||typeof c.access!=='string'||!c.access||c.access.startsWith('sk-')||typeof c.refresh!=='string'||!c.refresh||typeof c.expires!=='number'||!Number.isSafeInteger(c.expires)||c.expires<=0||typeof c.clientId!=='string'||!c.clientId||!Array.isArray(c.scopes)||!c.scopes.includes('chatgpt.tokens.use.direct'))refuse('selected OAuth connection is malformed','AUTH_UNAVAILABLE');
  return structuredClone(c) as OAuthCredential;
}
/** Selected-provider snapshots never run key commands, refresh, write or touch another store. */
export function readOnlyOAuthStore(credential:OAuthCredential):CredentialStore{
  const snapshot=oauthCredential(credential);
  const check=(signal?:AbortSignal):void=>{if(signal?.aborted)refuse('auth admission cancelled','CANCELLED');};
  return {
    async list(options){check(options?.signal);return [{providerId:oauthProvider,type:'oauth'}];},
    async read(provider,options){check(options?.signal);return provider===oauthProvider?structuredClone(snapshot):undefined;},
    async modify(){return refuse('in-run OAuth refresh/write is unavailable','AUTH_UNAVAILABLE');},
    async delete(){refuse('in-run OAuth deletion is unavailable','AUTH_UNAVAILABLE');}
  };
}
export function oauthOnly(provider:Provider):Provider{
  if(provider.id!==oauthProvider||provider.baseUrl!==oauthBase||!provider.auth.oauth)refuse('public OAuth provider identity differs','UNSUPPORTED_CAPABILITY');
  return {...provider,auth:{oauth:provider.auth.oauth}};
}
export async function resolveOAuthLease(home:string,provider:Provider,minimumValidityMs:number,signal?:AbortSignal):Promise<OAuthLease>{
  if(provider.id!==oauthProvider||provider.baseUrl!==oauthBase)refuse('OAuth origin fence failed','UNSUPPORTED_BINDING');
  await ownedAuthHome(home);const pi=await import('@earendil-works/pi-coding-agent');
  const credential=oauthCredential(pi.readStoredCredential(oauthProvider,path.join(home,'auth.json')));
  const minimum=Math.max(margin,minimumValidityMs);
  if(!Number.isSafeInteger(minimum)||minimum<0||Date.now()+minimum>=credential.expires)refuse('OAuth lease cannot cover the original invocation','AUTH_UNAVAILABLE');
  const store=readOnlyOAuthStore(credential),api=await import('@earendil-works/pi-ai');
  const entries=await store.list(signal?{signal}:{});if(entries.length!==1||entries[0]?.type!=='oauth')refuse('OAuth type gate failed','AUTH_UNAVAILABLE');
  const models=api.createModels({credentials:store});models.setProvider(oauthOnly(provider));
  const auth=await models.getAuth(oauthProvider,{minOAuthValidityMs:minimum,...(signal?{signal}:{})}).catch(()=>refuse('public OAuth lease resolution failed','AUTH_UNAVAILABLE'));
  if(auth?.source!=='OAuth'||auth.auth.apiKey!==credential.access||auth.auth.baseUrl&&auth.auth.baseUrl!==oauthBase||auth.auth.apiKey.startsWith('sk-'))refuse('OAuth resolution changed the selected lease','AUTH_UNAVAILABLE');
  return {credential,store,auth,descriptor:{kind:'oauth',provider_id:oauthProvider,reference:home,lease:{resolved_at:new Date().toISOString(),sdk_expires_at:credential.expires,minimum_validity_ms:minimum,refresh_in_run:false}}};
}
export function ownedEnvironment(dir:string,source:NodeJS.ProcessEnv,auth=false):NodeJS.ProcessEnv{
  const env:NodeJS.ProcessEnv={PATH:source.PATH??'',HOME:path.join(dir,'pi'),PI_OFFLINE:'1',PI_CODING_AGENT_DIR:path.join(dir,'pi'),TMPDIR:path.join(dir,'tmp'),TMP:path.join(dir,'tmp'),TEMP:path.join(dir,'tmp')};
  for(const key of ['LANG','LC_ALL','LC_CTYPE'])if(source[key])env[key]=source[key];
  if(auth)env.PI_OAUTH_CALLBACK_HOST='127.0.0.1';return env;
}
interface Receipt extends Obj {schema:'millforge-auth-receipt/v1';command:AuthCommand;provider:'openai';auth_home:string;started_at:string;ended_at:string;status:'completed'|'failed'|'cancelled'|'uncertain';counts:Obj;closure:Obj;diagnostic:Obj|null}
export interface AuthExecution {home:string;command:AuthCommand;receipt?:string;minValiditySeconds?:number}
/** Runs only after its caller establishes the owned offline environment. Tests may inject public interactions. */
export async function executeAuth(input:AuthExecution,interaction:AuthInteraction,cancelled:()=>boolean=()=>false):Promise<Receipt>{
  const started=new Date().toISOString();await ownedAuthHome(input.home,input.command==='login');
  const destination=input.receipt?path.resolve(input.receipt):path.join(input.home,'receipts',randomUUID()+'.json');
  await realPath(destination,true);await fs.mkdir(path.dirname(destination),{recursive:true,mode:0o700});await realPath(path.dirname(destination));
  const reservation=await fs.open(destination,'wx',0o600),identity=await reservation.stat();await reservation.close();
  let before:Credential|undefined,after:Credential|undefined,haveBefore=false,haveAfter=false,startedMutation=false,loginShown=false;
  const receipt:Receipt={schema:'millforge-auth-receipt/v1',command:input.command,provider:'openai',auth_home:input.home,started_at:started,ended_at:started,status:'failed',counts:{login_attempts:0,refresh_attempts:0,store_mutations:0},closure:{callback_listener:'not_started',refresh:'not_started',persistence:'not_started'},diagnostic:{code:'AUTH_UNAVAILABLE',message:'authentication did not complete'}};
  try{
    if(process.env.PI_OFFLINE!=='1'||process.env.PI_OAUTH_CALLBACK_HOST!=='127.0.0.1')refuse('auth environment was not prepared','UNSUPPORTED_CAPABILITY');
    const pi=await import('@earendil-works/pi-coding-agent');
    before=pi.readStoredCredential(oauthProvider,path.join(input.home,'auth.json'));haveBefore=true;
    if(input.command!=='login')oauthCredential(before);else if(before!==undefined)refuse('an existing connection cannot be re-registered implicitly','AUTH_UNAVAILABLE');
    if(cancelled())refuse('authentication cancelled before admission','CANCELLED');
    if(input.command==='status'){
      const c=oauthCredential(before);receipt.counts={login_attempts:0,refresh_attempts:0,store_mutations:0};
      // Nonsecret lease data is returned separately from credentials, never identity claims.
      receipt.diagnostic=null;receipt.status='completed';
      interaction.notify({type:'info',message:JSON.stringify({provider:oauthProvider,type:'oauth',sdk_expires_at:c.expires,remaining_validity_ms:Math.max(0,c.expires-Date.now())})});
    }else{
      const runtime=await pi.ModelRuntime.create({authPath:path.join(input.home,'auth.json'),modelsPath:null,refreshOnCreate:false,allowModelNetwork:false});
      const metadata=await runtime.listCredentials();
      if(metadata.some(x=>x.providerId!==oauthProvider||x.type!=='oauth'))refuse('unselected or API-key credential entry refused','AUTH_UNAVAILABLE');
      if(input.command==='login'&&metadata.length||input.command!=='login'&&(metadata.length!==1||metadata[0]?.type!=='oauth'))refuse('OAuth credential type gate failed','AUTH_UNAVAILABLE');
      if(cancelled())refuse('authentication cancelled before mutation','CANCELLED');
      if(input.command==='login'){
        let host:string;const hostPath=path.join(input.home,'host-id.json');
        try{const stored=exact(parseJson(await regularFile(hostPath,2048)),['schema','id']);if(stored.schema!=='millforge-host/v1'||typeof stored.id!=='string'||!/^[a-f0-9-]{36}$/i.test(stored.id))refuse('invalid stable host ID','AUTH_UNAVAILABLE');host=stored.id;}
        catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;host=randomUUID();await writeExclusive(hostPath,JSON.stringify({schema:'millforge-host/v1',id:host})+'\n');}
        startedMutation=true;receipt.closure.persistence='may_live';receipt.counts.login_attempts=1;receipt.closure.callback_listener='may_live';
        await runtime.login(oauthProvider,'oauth',{...interaction,notify:event=>{if(event.type==='auth_url')loginShown=true;interaction.notify(event);}},{getDeviceId:()=>host,agentName:'Millforge'});
        receipt.closure.callback_listener='closed';
      }else if(input.command==='refresh'){
        startedMutation=true;receipt.closure.persistence='may_live';
        receipt.counts.refresh_attempts=null;receipt.closure.refresh='may_live';
        const result=await runtime.getAuth(oauthProvider,{minOAuthValidityMs:Math.max(margin,(input.minValiditySeconds??0)*1000)});
        if(result?.source!=='OAuth'||!result.auth.apiKey||result.auth.apiKey.startsWith('sk-'))refuse('public OAuth refresh did not resolve','AUTH_UNAVAILABLE');
        receipt.closure.refresh='closed';
      }else{startedMutation=true;receipt.closure.persistence='may_live';await runtime.logout(oauthProvider);}
      await runtime.refresh({allowNetwork:false});
      after=pi.readStoredCredential(oauthProvider,path.join(input.home,'auth.json'));haveAfter=true;
      if(input.command==='logout'){if(after!==undefined)refuse('logout persistence was not observed','AUTH_UNAVAILABLE');}else oauthCredential(after);
      receipt.closure.persistence='closed';receipt.status=cancelled()?'cancelled':'completed';receipt.diagnostic=cancelled()?{code:'CANCELLED',message:'cancellation requested; SDK operation and persistence settled'}:null;
    }
  }catch(error){
    const errno=(error as NodeJS.ErrnoException)?.code;
    const problem=error instanceof MillforgeError?error:typeof errno==='string'&&/^E[A-Z]+$/.test(errno)?publicError(error):new MillforgeError('AUTH_UNAVAILABLE','public OAuth operation failed');receipt.status=startedMutation?'uncertain':cancelled()||problem.code==='CANCELLED'?'cancelled':'failed';
    receipt.diagnostic={code:problem.code,message:problem.message};
    if(startedMutation&&input.command==='login'){receipt.closure.callback_listener='closed';if(loginShown)receipt.status='uncertain';}
  }
  finally{
    if(haveBefore&&!haveAfter){try{const pi=await import('@earendil-works/pi-coding-agent');after=pi.readStoredCredential(oauthProvider,path.join(input.home,'auth.json'));haveAfter=true;}catch{/* Unknown snapshot is recorded, never invented. */}}
    receipt.counts.store_mutations=haveBefore&&haveAfter?(canonical((before??null) as unknown as Obj)===canonical((after??null) as unknown as Obj)?0:1):null;
    receipt.ended_at=new Date().toISOString();
    const current=await fs.lstat(destination);if(!current.isFile()||current.isSymbolicLink()||current.ino!==identity.ino||current.dev!==identity.dev)refuse('auth receipt reservation changed','IO_ERROR');
    const temporary=destination+'.'+randomUUID()+'.tmp';await writeExclusive(temporary,JSON.stringify(receipt)+'\n');await fs.rename(temporary,destination);
  }
  return receipt;
}
/** Keep auth work outside the finite invocation worker; cancellation never kills critical persistence. */
export async function runAuthCommand(options:AuthOptions):Promise<number>{
  if(options.provider!==oauthProvider)refuse('only the selected OpenAI OAuth provider is admitted','UNSUPPORTED_BINDING');
  const home=authHome(options.authHome,options.env);await ownedAuthHome(home,options.command==='login');
  const dir=await fs.mkdtemp(path.join(home,'.command-'));await fs.chmod(dir,0o700);await fs.mkdir(path.join(dir,'pi'),{mode:0o700});await fs.mkdir(path.join(dir,'tmp'),{mode:0o700});
  const env={...ownedEnvironment(dir,options.env,true),MILLFORGE_AUTH_WORKER:'1'};
  const child=fork(new URL('./auth.js',import.meta.url),[],{env,stdio:['inherit','inherit','inherit','ipc'],execArgv:[]});
  const cancel=():void=>{if(child.connected)child.send({cancel:true},()=>{});};process.on('SIGINT',cancel);process.on('SIGTERM',cancel);
  try{return await new Promise<number>((resolve,reject)=>{child.once('error',reject);child.on('message',(message:{ready?:boolean})=>{if(message.ready)child.send({home,command:options.command,...(options.receipt?{receipt:path.resolve(options.receipt)}:{}),...(options.minValiditySeconds===undefined?{}:{minValiditySeconds:options.minValiditySeconds})});});child.once('exit',code=>resolve(code??2));});}
  finally{process.off('SIGINT',cancel);process.off('SIGTERM',cancel);await fs.rm(dir,{recursive:true,force:true});}
}
if(process.env.MILLFORGE_AUTH_WORKER==='1'&&process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  let cancelled=false,running=false;const promptAbort=new AbortController();
  const cancel=():void=>{cancelled=true;promptAbort.abort();};process.on('SIGINT',cancel);process.on('SIGTERM',cancel);
  process.on('message',(input:AuthExecution|{cancel:true})=>{if('cancel'in input){cancel();return;}if(running)return;running=true;
    const interaction:AuthInteraction={notify:event=>{if(event.type==='auth_url')process.stderr.write(event.url+'\n'+(event.instructions??'')+'\n');else if(event.type==='info'||event.type==='progress')process.stderr.write(event.message+'\n');},prompt:async prompt=>{const rl=createInterface({input:process.stdin,output:process.stderr,terminal:process.stdin.isTTY});try{return await rl.question(prompt.message+' ',{signal:AbortSignal.any([promptAbort.signal,...(prompt.signal?[prompt.signal]:[])])});}finally{rl.close();}}};
    void executeAuth(input,interaction,()=>cancelled).then(receipt=>{process.stdout.write(JSON.stringify(receipt)+'\n');process.exitCode=receipt.status==='completed'?0:receipt.status==='cancelled'?130:2;process.disconnect?.();},()=>{process.stderr.write(JSON.stringify({code:'AUTH_UNAVAILABLE',message:'owned OAuth command failed'})+'\n');process.exitCode=2;process.disconnect?.();});
  });process.send?.({ready:true});
}
