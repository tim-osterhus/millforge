/** Ordinary-directory profile lifecycle; no provider or optional-module discovery. */
import fs from 'node:fs/promises';
import * as path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {type Json,type Obj,type Profile,type Resource,MillforgeError,absolute,canonical,exact,id,integer,parseJson,refuse,relative,validateProfile} from './contracts.js';
export const hash = (data: Uint8Array|string): string => createHash('sha256').update(data).digest('hex');
const MAX_AUTHORED=4194304;
export interface LoadedProfile {dir:string;profile:Profile;raw:Buffer;assets:Map<string,Buffer>;resources:Resource[];digest:string}
export async function regularFile(file: string, max=MAX_AUTHORED): Promise<Buffer> {
  const st=await fs.lstat(file);if(!st.isFile()||st.isSymbolicLink()||st.size>max)refuse('expected bounded regular file');
  const handle=await fs.open(file,'r');try{const before=await handle.stat();if(before.dev!==st.dev||before.ino!==st.ino)refuse('file changed while opening','PROFILE_CHANGED');const data=await handle.readFile();const after=await handle.stat();if(data.length>max||before.size!==after.size||before.mtimeMs!==after.mtimeMs||data.length!==before.size)refuse('file changed while reading','PROFILE_CHANGED');return data;}finally{await handle.close();}
}
export async function checkedPath(root: string, rel: string): Promise<string> {
  relative(rel);let current=root;for(const part of rel.split('/')){current=path.join(current,part);const st=await fs.lstat(current);if(st.isSymbolicLink())refuse('symlink traversal refused');}return current;
}
async function directory(dir: string): Promise<string> {
  const resolved=path.resolve(dir);const st=await fs.lstat(resolved);if(!st.isDirectory()||st.isSymbolicLink())refuse('profile must be a real directory');return resolved;
}
function decode(data:Buffer):string{try{return new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(data);}catch{return refuse('asset must be strict UTF-8');}}
export function profileDigest(raw:Buffer,resources:Resource[]):string {
  const manifest:Obj={profile_sha256:hash(raw),assets:resources.map(r=>({path:r.path,sha256:r.sha256,size:r.size})).sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0)};
  return hash(canonical(manifest));
}
export async function loadProfile(dir: string):Promise<LoadedProfile>{
  const root=await directory(dir);const raw=await regularFile(await checkedPath(root,'profile.json'));const profile=validateProfile(parseJson(raw));const assets=new Map<string,Buffer>();const resources:Resource[]=[];let total=raw.length;
  for(const a of profile.assets){const file=await checkedPath(root,a.path);const st=await fs.lstat(file);if(st.mode&0o111)refuse('executable assets refused');const data=await regularFile(file);decode(data);total+=data.length;if(total>MAX_AUTHORED)refuse('authored bytes exceed four MiB');if(hash(data)!==a.sha256)refuse('asset digest changed','PROFILE_CHANGED');assets.set(a.path,data);resources.push({...a,size:data.length});}
  if(total>MAX_AUTHORED)refuse('authored bytes exceed four MiB');
  // Every selected skill support file belongs to a declared SKILL.md root.
  const skills=profile.assets.filter(a=>a.kind==='skill');const roots=skills.filter(a=>path.posix.basename(a.path)==='SKILL.md').map(a=>path.posix.dirname(a.path));
  for(const a of skills)if(!roots.some(root=>root==='.'||a.path.startsWith(root+'/')))refuse('skill support file lacks declared SKILL.md');
  return {dir:root,profile,raw,assets,resources,digest:profileDigest(raw,resources)};
}
export function authored(profile:Profile):Buffer{return Buffer.from(JSON.stringify(profile,null,2)+'\n');}
async function removeTemp(file:string):Promise<void>{
  try{await fs.unlink(file);}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
}
export async function writeExclusive(file:string,data:Uint8Array|string):Promise<void>{
  const h=await fs.open(file,'wx',0o600);
  try{try{await h.writeFile(data);await h.sync();}finally{await h.close();}}
  catch(error){await removeTemp(file);throw error;}
}
async function atomicNewOwned(file:string,data:Uint8Array|string,published:()=>void):Promise<void>{
  const temp=file+'.tmp-'+randomUUID();
  try{await writeExclusive(temp,data);await fs.link(temp,file);published();}finally{await removeTemp(temp);} // Mark ownership after link, before temp cleanup can fail.
}
export async function atomicNew(file:string,data:Uint8Array|string):Promise<void>{
  await atomicNewOwned(file,data,()=>{});
}
async function atomicReplace(file:string,data:Uint8Array|string):Promise<void>{
  const temp=file+'.tmp-'+randomUUID();
  try{await writeExclusive(temp,data);await fs.rename(temp,file);}catch(error){await removeTemp(temp);throw error;}
}
async function copyLoaded(loaded:LoadedProfile,dest:string,raw=loaded.raw):Promise<void>{
  await fs.mkdir(dest,{mode:0o700});try{for(const [rel,data]of loaded.assets){const file=path.join(dest,rel);await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});await writeExclusive(file,data);}await writeExclusive(path.join(dest,'profile.json'),raw);}catch(error){throw error;} // Partial owned destinations are preserved for diagnosis.
}
async function safeActiveTree(root:string):Promise<void>{
  const visit=async(dir:string):Promise<void>=>{for(const name of await fs.readdir(dir)){if(dir===root&&(name==='.history'||name==='.millforge-write.lock'))continue;const file=path.join(dir,name);const st=await fs.lstat(file);if(st.isSymbolicLink())refuse('symlinks refused on import/export');if(st.isDirectory())await visit(file);else if(!st.isFile()||(st.mode&0o111))refuse('special or executable files refused on import/export');}};await visit(root);
}
export async function createProfile(dest:string,manifest:string):Promise<LoadedProfile>{
  const raw=await regularFile(manifest);const p=validateProfile(parseJson(raw));if(p.assets.length)refuse('create manifest must have no assets');if(raw.length>MAX_AUTHORED)refuse('profile too large');await fs.mkdir(path.resolve(dest),{mode:0o700});await writeExclusive(path.join(path.resolve(dest),'profile.json'),raw);return loadProfile(dest);
}
export async function copyProfile(source:string,dest:string,newId?:string):Promise<LoadedProfile>{
  const loaded=await loadProfile(source);await safeActiveTree(loaded.dir);let raw=loaded.raw;if(newId!==undefined){id(newId);raw=authored({...loaded.profile,profile_id:newId,revision:1});}await copyLoaded(loaded,path.resolve(dest),raw);return loadProfile(dest);
}
export interface SelectionEnv {HOME?:string;XDG_CONFIG_HOME?:string}
function selectionPath(env:SelectionEnv):string {
  const base=env.XDG_CONFIG_HOME|| (env.HOME?path.join(env.HOME,'.config'):refuse('HOME or XDG_CONFIG_HOME required'));absolute(base);return path.join(base,'millforge','selection.json');
}
export async function selectProfile(dir:string,env:SelectionEnv):Promise<string>{
  const loaded=await loadProfile(dir);const file=selectionPath(env);await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});await atomicReplace(file,JSON.stringify({path:loaded.dir})+'\n');return loaded.dir;
}
export async function resolveProfile(explicit:string|undefined,env:SelectionEnv):Promise<string>{
  if(explicit!==undefined)return path.resolve(explicit);const selected=exact(parseJson(await regularFile(selectionPath(env),16384)),['path']);return absolute(selected.path);
}
async function withReservation<T>(dir:string,work:()=>Promise<T>):Promise<T>{
  const lock=path.join(dir,'.millforge-write.lock');let handle;
  try{handle=await fs.open(lock,'wx',0o600);}catch(error){if((error as NodeJS.ErrnoException).code==='EEXIST')refuse('profile write reservation already exists','PROFILE_CHANGED');throw error;}
  try{return await work();}finally{try{await handle.close();}finally{await fs.unlink(lock);}}
}
/** Reuse only an exact complete history snapshot; never repair or overwrite it. */
async function retainHistory(current:LoadedProfile,history:string):Promise<void>{
  try{await copyLoaded(current,history);return;}
  catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error;}
  const saved=await loadProfile(history);
  if(!saved.raw.equals(current.raw)||saved.assets.size!==current.assets.size)refuse('history snapshot differs','PROFILE_CHANGED');
  for(const [rel,data]of current.assets)if(!saved.assets.get(rel)?.equals(data))refuse('history snapshot differs','PROFILE_CHANGED');
  const files=new Set(['profile.json',...current.assets.keys()]);const dirs=new Set<string>();
  for(const rel of files){let parent=path.posix.dirname(rel);while(parent!=='.'){dirs.add(parent);parent=path.posix.dirname(parent);}}
  const visit=async(dir:string,prefix=''):Promise<void>=>{for(const name of await fs.readdir(dir)){
    const rel=prefix?prefix+'/'+name:name;const file=path.join(dir,name);const st=await fs.lstat(file);
    if(st.isSymbolicLink())refuse('unsafe history snapshot');
    if(st.isDirectory()){if(!dirs.has(rel))refuse('extra history directory');await visit(file,rel);}
    else if(!st.isFile()||!files.has(rel)||(st.mode&0o111))refuse('extra or unsafe history file');
  }};await visit(history);
}
interface AssetWrite {file:string;data:Buffer;before:Buffer|null}
async function preflightAssets(current:LoadedProfile,nextAssets:Map<string,Buffer>,restore:boolean):Promise<AssetWrite[]>{
  const writes:AssetWrite[]=[];
  for(const [rel,data]of nextAssets){
    let file=current.dir,missing=false;
    const parts=rel.split('/');
    for(let i=0;i<parts.length;i++){
      file=path.join(file,parts[i]!);if(missing)continue;
      try{const st=await fs.lstat(file);
        if(st.isSymbolicLink()||(i<parts.length-1?!st.isDirectory():!st.isFile()||!!(st.mode&0o111)))refuse('conflicting asset destination');
        if(i===parts.length-1&&restore&&!current.assets.has(rel))refuse('restore target is unselected');
      }catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;missing=true;}
    }
    if(missing&&!restore)refuse('asset preimage changed','PROFILE_CHANGED');
    let before:Buffer|null=null;if(!missing)before=await regularFile(file);
    if(before&&(!before.equals(current.assets.get(rel)??before)||!restore&&!before.equals(data)))refuse('asset preimage changed','PROFILE_CHANGED');
    if(!before?.equals(data))writes.push({file,data,before});
  }
  return writes;
}
async function publishRevision(current:LoadedProfile,next:Profile,nextAssets:Map<string,Buffer>,restore=false):Promise<LoadedProfile>{
  if(current.profile.revision===Number.MAX_SAFE_INTEGER)refuse('revision exhausted');
  next={...next,revision:current.profile.revision+1};validateProfile(next);const raw=authored(next),root=current.dir;
  if(raw.length+[...nextAssets.values()].reduce((n,data)=>n+data.length,0)>MAX_AUTHORED)refuse('authored bytes exceed limit');
  const writes=await preflightAssets(current,nextAssets,restore);
  const historyRoot=path.join(root,'.history');try{await fs.mkdir(historyRoot,{mode:0o700});}catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error;const st=await fs.lstat(historyRoot);if(!st.isDirectory()||st.isSymbolicLink())refuse('unsafe history directory');}
  await retainHistory(current,path.join(historyRoot,String(current.profile.revision)));
  const manifest=path.join(root,'profile.json');
  if(!(await regularFile(manifest)).equals(current.raw))refuse('manifest preimage changed','PROFILE_CHANGED');
  const changed:AssetWrite[]=[],createdDirs:string[]=[];
  try{
    for(const write of writes){
      const rel=path.relative(root,path.dirname(write.file));let dir=root;
      for(const part of rel?rel.split(path.sep):[]){dir=path.join(dir,part);try{await fs.mkdir(dir,{mode:0o700});createdDirs.push(dir);}catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error;const st=await fs.lstat(dir);if(!st.isDirectory()||st.isSymbolicLink())refuse('asset parent changed','PROFILE_CHANGED');}}
      if(write.before===null){await atomicNewOwned(write.file,write.data,()=>{changed.push(write);});}else{
        if(!(await regularFile(await checkedPath(root,path.relative(root,write.file).split(path.sep).join('/')))).equals(write.before))refuse('asset preimage changed','PROFILE_CHANGED');
        await atomicReplace(write.file,write.data);changed.push(write);
      }
    }
    if(!(await regularFile(manifest)).equals(current.raw))refuse('manifest preimage changed','PROFILE_CHANGED');
    await atomicReplace(manifest,raw); // Commit point: never roll assets back after this succeeds.
  }catch(error){
    let recovered=true;
    for(const write of changed.reverse())try{
      const rel=path.relative(root,write.file).split(path.sep).join('/');
      let actual:Buffer;try{actual=await regularFile(await checkedPath(root,rel));}catch(error){if(write.before===null&&(error as NodeJS.ErrnoException).code==='ENOENT')continue;throw error;}
      if(write.before?.equals(actual))continue;
      if(!actual.equals(write.data)){recovered=false;continue;}
      if(write.before===null)await fs.unlink(write.file);else await atomicReplace(write.file,write.before);
    }catch{recovered=false;}
    for(const dir of createdDirs.reverse())try{await fs.rmdir(dir);}catch{recovered=false;}
    if(!recovered)refuse('profile publish failed; rollback incomplete; complete prior snapshot retained in history; manual recovery required','IO_ERROR');
    throw error;
  }
  return loadProfile(root);
}
export async function editProfile(dir:string,manifest:string):Promise<LoadedProfile>{
  const root=await directory(dir);const replacement=validateProfile(parseJson(await regularFile(manifest)));
  return withReservation(root,async()=>{const current=await loadProfile(root);if(replacement.revision!==current.profile.revision||replacement.profile_id!==current.profile.profile_id)refuse('supplied revision or ID differs','PROFILE_CHANGED');
    const selected=new Map<string,Buffer>();let total=authored(replacement).length;for(const a of replacement.assets){const data=await regularFile(await checkedPath(root,a.path));if(hash(data)!==a.sha256)refuse('replacement asset digest mismatch','PROFILE_CHANGED');decode(data);if((await fs.lstat(path.join(root,a.path))).mode&0o111)refuse('executable asset');total+=data.length;selected.set(a.path,data);}if(total>MAX_AUTHORED)refuse('authored bytes exceed limit');return publishRevision(current,replacement,selected);});
}
export async function restoreProfile(dir:string,revision:number):Promise<LoadedProfile>{
  integer(revision,1);const root=await directory(dir);return withReservation(root,async()=>{const current=await loadProfile(root);const history=await checkedPath(root,`.history/${revision}`);const saved=await loadProfile(history);if(saved.profile.revision!==revision||saved.profile.profile_id!==current.profile.profile_id)refuse('historical identity mismatch');return publishRevision(current,saved.profile,saved.assets,true);});
}
export async function diffProfiles(left:string,right:string):Promise<Json>{
  const a=await loadProfile(left),b=await loadProfile(right);const changes:Obj[]=[];const pointer=(key:string):string=>key.replace(/~/g,'~0').replace(/\//g,'~1');
  const walk=(x:Json|undefined,y:Json|undefined,p:string):void=>{if(x!==undefined&&y!==undefined&&canonical(x)===canonical(y))return;if(x!==null&&y!==null&&typeof x==='object'&&typeof y==='object'&&!Array.isArray(x)&&!Array.isArray(y)&&x!==undefined&&y!==undefined){for(const key of [...new Set([...Object.keys(x),...Object.keys(y)])].sort())walk(x[key],y[key],p+'/'+pointer(key));}else changes.push({path:p,before:x??null,after:y??null,before_present:x!==undefined,after_present:y!==undefined});};
  walk(a.profile as unknown as Obj,b.profile as unknown as Obj,'');const assets:Obj[]=[];for(const rel of [...new Set([...a.assets.keys(),...b.assets.keys()])].sort()){const before=a.assets.get(rel),after=b.assets.get(rel);if(before&&after&&hash(before)===hash(after))continue;assets.push({path:rel,before:before?hash(before):null,after:after?hash(after):null});}return {changes,assets};
}
export function publicError(error:unknown):MillforgeError {
  if(error instanceof MillforgeError)return error;
  const code=(error as NodeJS.ErrnoException)?.code;if(code==='EEXIST')return new MillforgeError('INVALID_INPUT','destination or immutable record already exists');if(code==='ENOENT')return new MillforgeError('IO_ERROR','required file or directory is missing');return new MillforgeError('IO_ERROR','file operation failed');
}
