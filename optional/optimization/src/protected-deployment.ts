/** Local authority for the one installed Codemode baseline. Actor isolation is measured separately. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {type Obj,canonical,exact,fail,hash,object,parse,read,same,schema,workMap} from './contracts.js';

const installedSuffix='/optimizer/node_modules/@millrace-ai/millforge-optimization/dist/src/cli.js';

export interface DeploymentPaths {root:string;entry:string;campaign:string;authority:string}

export function deploymentPaths(entry:string):DeploymentPaths|undefined {
 if(!entry.endsWith(installedSuffix))return undefined;
 const root=entry.slice(0,-installedSuffix.length);
 if(!root||!path.isAbsolute(root))return undefined;
 return {root,entry,campaign:path.join(root,'campaign'),authority:path.join(root,'authority/authority.json')};
}

export async function protectedPath(file:string,kind:'directory'|'file',owned=false):Promise<void> {
 const uid=process.getuid?.();
 if(process.platform!=='linux'||uid===undefined||uid===0||process.geteuid?.()!==uid||
    !path.isAbsolute(file)||await fs.realpath(file)!==file)fail('protected deployment requires canonical paths and a non-root Linux operator');
 for(let current=file;;current=path.dirname(current)){
  const stat=await fs.lstat(current);
  const isFile=current===file&&kind==='file';
  if((isFile?!stat.isFile()||stat.nlink!==1:!stat.isDirectory())||stat.mode&0o022||
     (stat.uid!==uid&&(stat.uid!==0||current===file&&owned)))fail('unprotected deployment path');
  if(current===path.dirname(current))break;
 }
}

export async function verifyDeployment(paths:DeploymentPaths,dir:string):Promise<void> {
 if(dir!==paths.campaign)fail('unregistered Codemode campaign root');
 await protectedPath(paths.root,'directory',true);
 if((await fs.lstat(paths.root)).mode&0o077)fail('Codemode deployment root must be private to the operator');
 await protectedPath(paths.entry,'file',true);
 await protectedPath(dir,'directory',true);
}

export async function protectedInput(paths:DeploymentPaths,file:string,kind:'directory'|'file'):Promise<void> {
 if(!file.startsWith(paths.root+path.sep))fail('input is outside the protected deployment root');
 await protectedPath(file,kind);
}

export async function protectedRead(file:string,owned=true):Promise<Buffer> {
 await protectedPath(file,'file',owned);
 return read(file);
}

export async function optionalProtectedRead(file:string):Promise<Buffer|null> {
 try{return await protectedRead(file);}catch(error){
  if((error as NodeJS.ErrnoException).code==='ENOENT')return null;
  throw error;
 }
}

export function deployedAuthority(bytes:Buffer,c:Obj,dir:string,asOf:number):Obj {
 const a=exact(parse(bytes),['schema','owner_id','campaign_id','campaign_digest','execution_root','budget','expires_at']);
 schema(a,'millforge-optimization-authority/v1');
 const expires=typeof a.expires_at==='string'?Date.parse(a.expires_at):NaN;
 if(a.owner_id!==c.owner_id||a.campaign_id!==c.campaign_id||a.campaign_digest!==hash(canonical(c))||
    a.execution_root!==dir||!same(a.budget,c.budget)||!Number.isFinite(expires)||expires<=asOf){
  fail('authority identity/root/expiry mismatch');
 }
 return a;
}

/** The no-asset profile's digest and work map can both be verified before invoking the base. */
export async function deployedWorkMap(dir:string,c:Obj):Promise<Obj> {
 if(c.baseline.profile_path!=='profiles/baseline'||c.task.input_dir!=='inputs')fail('deployed Codemode requires captured input paths');
 const bytes=await protectedRead(path.join(dir,'profiles/baseline/profile.json'));
 const profile=object(parse(bytes));
 if(profile.schema!==c.target.profile_schema||profile.mode!=='agent'||!same(profile.tools,c.allowed_tools)||
    !same(profile.assets,[])||!same(profile.capabilities,{extensions:['pi-codemode/v1'],packages:[]})||
    profile.model?.base_url!==c.execution.model_endpoint.target||profile.model?.auth?.kind!=='env'||
    profile.model?.provider!=='local'||profile.model?.api!=='openai-completions'||
    hash(canonical({profile_sha256:hash(bytes),assets:[]}))!==c.baseline.profile_digest){
  fail('deployed Codemode requires the captured read/write profile');
 }
 const map=object(parse(await protectedRead(path.join(dir,'work-map.json'))));
 if(!same(map,workMap(c,profile,null)))fail('captured work map changed');
 const slot=map.slots[0];
 if(slot.limits.model_requests!==3||slot.limits.tool_calls!==5||slot.limits.elapsed_seconds!==30||slot.evaluation_seconds!==10){
  fail('deployed Codemode requires the bounded baseline work map');
 }
 return map;
}
