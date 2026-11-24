import {afterEach,describe,expect,it,vi} from 'vitest';
import fs from 'node:fs/promises';
import * as path from 'node:path';
import {tmpdir} from 'node:os';
import {type Json,type Obj,type Profile,object,parseJson,validateProfile} from '../src/contracts.js';
import {copyProfile,createProfile,diffProfiles,editProfile,hash,loadProfile,resolveProfile,restoreProfile,selectProfile,atomicNew} from '../src/profile.js';
const roots:string[]=[];
afterEach(async()=>{vi.restoreAllMocks();for(const root of roots.splice(0))await fs.rm(root,{recursive:true,force:true});});
async function fixture(){const root=await fs.mkdtemp(path.join(tmpdir(),'millforge-profile-'));roots.push(root);const value=parseJson(await fs.readFile(new URL('../profiles/generate/profile.json',import.meta.url)));const manifest=path.join(root,'manifest.json');await fs.writeFile(manifest,JSON.stringify(value));return {root,value:object(value),manifest,dir:path.join(root,'profile')};}
function set(value:Obj,pointer:string,replacement:Json):void{const parts=pointer.split('/');let current=value;for(const key of parts.slice(0,-1))current=object(current[key]);current[parts.at(-1)!]=replacement;}
describe('strict authored profile boundary',()=>{
  it.each(['{"x":1,"x":2}','{"x":{"a":1,"a":2}}','{"x":1e999}','{"x":NaN}','{"x":true,}','[1,]','{}false','"\\ud800"'])('rejects invalid JSON %s',raw=>{expect(()=>parseJson(raw)).toThrow();});
  it('rejects invalid UTF-8 and preserves prototype-like data without pollution',()=>{expect(()=>parseJson(Buffer.from([0xc3,0x28]))).toThrow();expect(object(parseJson('{"__proto__":{"polluted":true}}')).__proto__).toEqual({polluted:true});expect(({} as Record<string,unknown>).polluted).toBeUndefined();});
  it.each([
    ['schema','millforge-profile/v2'],['revision',true],['revision',0],['revision',1.5],['profile_id','../wrong'],['runtime/pi_version','latest'],['model/api','unknown'],['model/api',['anthropic-messages']],['model/api',{}],['model/base_url','https://user:password@example.test'],['model/base_url','http://localhost/?key=value'],['model/base_url','file:///tmp/model'],...[' https://example.test','https://example.test\n','https://exa\tmple.test','https:\\example.test','http://@host/','http://host/\u007f'].map(v=>['model/base_url',v] as [string,Json]),['model/credential_env','secret-key-literal!'],['model/context_tokens',false],['model/compat/supportsFinishReason',false],['sampling/temperature',true],['sampling/top_p',0],['tools',['read']],['assets',[{path:'../bad.md',kind:'context',sha256:'a'.repeat(64)}]],['assets',[{path:'assets\\bad.md',kind:'context',sha256:'a'.repeat(64)}]],['assets',[{path:'.history/1/a.md',kind:'context',sha256:'a'.repeat(64)}]],...['.HISTORY/1/a.md','Profile.json','PROFILE.json/child.md','.Millforge-Write.Lock/a.md'].map(v=>['assets',[{path:v,kind:'context',sha256:'a'.repeat(64)}]] as [string,Json]),['capabilities/extensions',['plugin']],['terminal/required_keys',['key']],['terminal/outcomes',['yes']],['unknown',1],['model/extra',1]
  ] as [string,Json][])('refuses %s corruption',async(pointer,replacement)=>{const f=await fixture();set(f.value,pointer,replacement);expect(()=>validateProfile(f.value)).toThrow();});
  it('validates Anthropic authoring while refusing incompatible top_p',async()=>{const f=await fixture();const model=object(f.value.model);model.api='anthropic-messages';model.compat={};expect(validateProfile(f.value).model.api).toBe('anthropic-messages');object(f.value.sampling).top_p=0.7;expect(()=>validateProfile(f.value)).toThrow();});
});
describe('ordinary-directory lifecycle',()=>{
  it('creates, clones, selects explicitly, exports/imports and rejects existing destinations',async()=>{const f=await fixture();const created=await createProfile(f.dir,f.manifest);const clone=await copyProfile(f.dir,path.join(f.root,'clone'),'new-id');expect(clone.profile.profile_id).toBe('new-id');expect(clone.profile.revision).toBe(1);const exported=await copyProfile(f.dir,path.join(f.root,'export'));expect(exported.digest).toBe(created.digest);const imported=await copyProfile(exported.dir,path.join(f.root,'import'));expect(imported.digest).toBe(created.digest);await expect(createProfile(f.dir,f.manifest)).rejects.toThrow();const env={HOME:f.root};await selectProfile(f.dir,env);expect(await resolveProfile(undefined,env)).toBe(f.dir);expect(await resolveProfile(clone.dir,env)).toBe(clone.dir);expect(JSON.parse(await fs.readFile(path.join(f.root,'.config/millforge/selection.json'),'utf8'))).toEqual({path:f.dir});});
  it('preserves exact complete manifest/assets in history and restores increasing revisions',async()=>{const f=await fixture();await fs.mkdir(f.dir);await fs.mkdir(path.join(f.dir,'assets'));const before=Buffer.from('old context');await fs.writeFile(path.join(f.dir,'assets/context.md'),before);f.value.assets=[{path:'assets/context.md',sha256:hash(before),kind:'context'}];const original=Buffer.from(JSON.stringify(f.value)+'\n');await fs.writeFile(path.join(f.dir,'profile.json'),original);const initial=await loadProfile(f.dir);f.value.instructions='new instructions';await fs.writeFile(f.manifest,JSON.stringify(f.value));const edited=await editProfile(f.dir,f.manifest);expect(edited.profile.revision).toBe(2);expect(await fs.readFile(path.join(f.dir,'.history/1/profile.json'))).toEqual(original);expect(await fs.readFile(path.join(f.dir,'.history/1/assets/context.md'))).toEqual(before);const restored=await restoreProfile(f.dir,1);expect(restored.profile.revision).toBe(3);expect(restored.profile.instructions).toBe(initial.profile.instructions);expect((await loadProfile(path.join(f.dir,'.history/2'))).profile.instructions).toBe('new instructions');expect((await copyProfile(f.dir,path.join(f.root,'export'))).profile.revision).toBe(3);await expect(fs.lstat(path.join(f.root,'export/.history'))).rejects.toThrow();});
  it('refuses invalid skill replacement before changing history or manifest',async()=>{const f=await fixture();await createProfile(f.dir,f.manifest);const original=await fs.readFile(path.join(f.dir,'profile.json'));await fs.mkdir(path.join(f.dir,'assets'));await fs.writeFile(path.join(f.dir,'assets/support.md'),'support');f.value.assets=[{path:'assets/support.md',sha256:hash('support'),kind:'skill'}];await fs.writeFile(f.manifest,JSON.stringify(f.value));await expect(editProfile(f.dir,f.manifest)).rejects.toThrow(/SKILL/);expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(original);await expect(fs.access(path.join(f.dir,'.history'))).rejects.toThrow();});
  it('refuses stale edits and occupied concurrent-write reservation without overwriting history',async()=>{const f=await fixture();await createProfile(f.dir,f.manifest);await fs.writeFile(path.join(f.dir,'.millforge-write.lock'),'owned elsewhere');await expect(editProfile(f.dir,f.manifest)).rejects.toThrow(/reservation/);await fs.unlink(path.join(f.dir,'.millforge-write.lock'));await editProfile(f.dir,f.manifest);await expect(editProfile(f.dir,f.manifest)).rejects.toThrow(/revision/);await fs.writeFile(path.join(f.dir,'.millforge-write.lock'),'other');await expect(restoreProfile(f.dir,1)).rejects.toThrow(/reservation/);expect((await loadProfile(f.dir)).profile.revision).toBe(2);});
  it('accepts one concurrent edit and refuses the other unchanged-revision writer',async()=>{const f=await fixture();await createProfile(f.dir,f.manifest);const outcomes=await Promise.allSettled([editProfile(f.dir,f.manifest),editProfile(f.dir,f.manifest)]);expect(outcomes.filter(o=>o.status==='fulfilled')).toHaveLength(1);expect(outcomes.filter(o=>o.status==='rejected')).toHaveLength(1);expect((await loadProfile(f.dir)).profile.revision).toBe(2);});
  it.each(['symlink','executable','hash','utf8','support'])('refuses unsafe resource %s',async(kind)=>{const f=await fixture();await fs.mkdir(f.dir);await fs.mkdir(path.join(f.dir,'assets'));const file=path.join(f.dir,'assets/data.md');let data=Buffer.from('resource');if(kind==='utf8')data=Buffer.from([0xff]);await fs.writeFile(file,data);if(kind==='symlink'){await fs.unlink(file);await fs.symlink(f.manifest,file);}if(kind==='executable')await fs.chmod(file,0o700);f.value.assets=[{path:'assets/data.md',sha256:kind==='hash'?'0'.repeat(64):hash(data),kind:kind==='support'?'skill':'context'}];await fs.writeFile(path.join(f.dir,'profile.json'),JSON.stringify(f.value));await expect(loadProfile(f.dir)).rejects.toThrow();});
  it('refuses undeclared active symlinks on import/export and never adopts undeclared ordinary bytes',async()=>{const f=await fixture();await createProfile(f.dir,f.manifest);await fs.writeFile(path.join(f.dir,'unselected.txt'),'not selected');const clone=await copyProfile(f.dir,path.join(f.root,'clean'));await expect(fs.access(path.join(clone.dir,'unselected.txt'))).rejects.toThrow();await fs.symlink(f.manifest,path.join(f.dir,'bad'));await expect(copyProfile(f.dir,path.join(f.root,'unsafe'))).rejects.toThrow(/symlink/);});
  it('excludes history from digest and produces sorted structural differences',async()=>{const f=await fixture();const first=await createProfile(f.dir,f.manifest);await fs.mkdir(path.join(f.dir,'.history'));await fs.writeFile(path.join(f.dir,'.history/ignored'),'old');expect((await loadProfile(f.dir)).digest).toBe(first.digest);await copyProfile(f.dir,path.join(f.root,'other'),'other');const diff=await diffProfiles(f.dir,path.join(f.root,'other')) as Obj;expect((diff.changes as Obj[]).map(c=>c.path)).toEqual(['/profile_id']);});
});

describe('bounded publication repair',()=>{
  async function historyFixture(){
    const f=await fixture();await fs.mkdir(f.dir);await fs.mkdir(path.join(f.dir,'assets'));
    const assets=[['assets/a.md','old a'],['assets/b.md','old b']];
    f.value.assets=assets.map(([name,data])=>({path:name!,sha256:hash(data!),kind:'context'}));
    for(const [name,data]of assets)await fs.writeFile(path.join(f.dir,name!),data!);
    await fs.writeFile(path.join(f.dir,'profile.json'),JSON.stringify(f.value)+'\n');
    // Remove both resources from the next active manifest, leaving their ordinary bytes untouched.
    f.value.assets=[];await fs.writeFile(f.manifest,JSON.stringify(f.value));await editProfile(f.dir,f.manifest);
    return f;
  }
  it('admits an edit selecting existing bytes but refuses restore over unselected bytes even if identical',async()=>{
    const f=await historyFixture();const prior=await fs.readFile(path.join(f.dir,'profile.json'));
    await expect(restoreProfile(f.dir,1)).rejects.toThrow(/unselected/);
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);
    await expect(fs.access(path.join(f.dir,'.history/2'))).rejects.toThrow();
    const active=object(parseJson(prior));active.assets=[{path:'assets/a.md',sha256:hash('old a'),kind:'context'}];
    await fs.writeFile(f.manifest,JSON.stringify(active));
    expect((await editProfile(f.dir,f.manifest)).profile.revision).toBe(3);
  });
  it.each(['parent','target'])('preflights every restore destination before changing selected bytes: %s',async(conflict)=>{
    const f=await historyFixture();await fs.unlink(path.join(f.dir,'assets/a.md'));await fs.unlink(path.join(f.dir,'assets/b.md'));
    if(conflict==='parent'){await fs.rmdir(path.join(f.dir,'assets'));await fs.writeFile(path.join(f.dir,'assets'),'unselected parent');}
    else await fs.mkdir(path.join(f.dir,'assets/b.md'));
    const prior=await fs.readFile(path.join(f.dir,'profile.json'));await expect(restoreProfile(f.dir,1)).rejects.toThrow(/conflicting/);
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);await expect(fs.access(path.join(f.dir,'.history/2'))).rejects.toThrow();
  });
  it.each(['asset','manifest'])('rolls back a handled %s failure and retries using byte-identical complete history',async(failure)=>{
    const f=await historyFixture();await fs.unlink(path.join(f.dir,'assets/a.md'));await fs.unlink(path.join(f.dir,'assets/b.md'));await fs.rmdir(path.join(f.dir,'assets'));
    const prior=await fs.readFile(path.join(f.dir,'profile.json'));const rename=fs.rename,link=fs.link;
    const inject=Object.assign(new Error('injected publish failure'),{code:'EIO'});
    const renameSpy=vi.spyOn(fs,'rename').mockImplementation(async(from,to)=>{if(failure==='manifest'&&to===path.join(f.dir,'profile.json'))throw inject;return rename(from,to);});
    const linkSpy=vi.spyOn(fs,'link').mockImplementation(async(from,to)=>{if(failure==='asset'&&to===path.join(f.dir,'assets/b.md'))throw inject;return link(from,to);});
    await expect(restoreProfile(f.dir,1)).rejects.toThrow(/injected publish failure/);
    expect(failure==='asset'?linkSpy:renameSpy).toHaveBeenCalledWith(expect.anything(),path.join(f.dir,failure==='asset'?'assets/b.md':'profile.json'));
    renameSpy.mockRestore();linkSpy.mockRestore();
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);expect((await loadProfile(f.dir)).profile.revision).toBe(2);
    expect((await fs.readdir(f.dir)).sort()).toEqual(['.history','profile.json']);
    expect(await fs.readFile(path.join(f.dir,'.history/2/profile.json'))).toEqual(prior);
    expect((await restoreProfile(f.dir,1)).profile.revision).toBe(3);
    expect(await fs.readFile(path.join(f.dir,'assets/a.md'),'utf8')).toBe('old a');
    expect((await fs.readdir(f.dir)).some(name=>name.includes('.tmp-')||name.includes('lock'))).toBe(false);
  });
  it('restores overwritten selected asset bytes after a later restore write fails',async()=>{
    const f=await historyFixture();const active=object(parseJson(await fs.readFile(path.join(f.dir,'profile.json'))));
    active.assets=['a','b'].map(name=>({path:`assets/${name}.md`,sha256:hash('current '+name),kind:'context'}));
    for(const name of ['a','b'])await fs.writeFile(path.join(f.dir,`assets/${name}.md`),'current '+name);
    const prior=Buffer.from(JSON.stringify(active)+'\n');await fs.writeFile(path.join(f.dir,'profile.json'),prior);
    const rename=fs.rename;const renameSpy=vi.spyOn(fs,'rename').mockImplementation(async(from,to)=>{if(to===path.join(f.dir,'assets/b.md'))throw new Error('injected second asset failure');return rename(from,to);});
    await expect(restoreProfile(f.dir,1)).rejects.toThrow(/injected second asset failure/);
    expect(renameSpy).toHaveBeenCalledWith(expect.anything(),path.join(f.dir,'assets/b.md'));
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);
    expect(await fs.readFile(path.join(f.dir,'assets/a.md'),'utf8')).toBe('current a');
    expect(await fs.readFile(path.join(f.dir,'assets/b.md'),'utf8')).toBe('current b');
    expect((await loadProfile(f.dir)).profile.revision).toBe(2);
  });
  it('preserves a competing asset change and reports incomplete rollback with exact history retained',async()=>{
    const f=await historyFixture();await fs.unlink(path.join(f.dir,'assets/a.md'));await fs.unlink(path.join(f.dir,'assets/b.md'));
    const prior=await fs.readFile(path.join(f.dir,'profile.json'));const rename=fs.rename;
    const renameSpy=vi.spyOn(fs,'rename').mockImplementation(async(from,to)=>{if(to===path.join(f.dir,'profile.json')){await fs.writeFile(path.join(f.dir,'assets/a.md'),'competing bytes');throw new Error('injected manifest failure');}return rename(from,to);});
    await expect(restoreProfile(f.dir,1)).rejects.toThrow(/rollback incomplete.*manual recovery/);
    expect(renameSpy).toHaveBeenCalledWith(expect.anything(),path.join(f.dir,'profile.json'));
    expect(await fs.readFile(path.join(f.dir,'assets/a.md'),'utf8')).toBe('competing bytes');
    await expect(fs.access(path.join(f.dir,'assets/b.md'))).rejects.toThrow();
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);
    expect(await fs.readFile(path.join(f.dir,'.history/2/profile.json'))).toEqual(prior);
    await expect(fs.access(path.join(f.dir,'.millforge-write.lock'))).rejects.toThrow();
  });
  it('preserves an identical competing file when exclusive publication refuses EEXIST',async()=>{
    const f=await historyFixture();await fs.unlink(path.join(f.dir,'assets/a.md'));await fs.unlink(path.join(f.dir,'assets/b.md'));
    const prior=await fs.readFile(path.join(f.dir,'profile.json'));const link=fs.link;
    const linkSpy=vi.spyOn(fs,'link').mockImplementation(async(from,to)=>{if(to===path.join(f.dir,'assets/a.md'))await fs.writeFile(to,'old a');return link(from,to);});
    await expect(restoreProfile(f.dir,1)).rejects.toMatchObject({code:'EEXIST'});
    expect(linkSpy).toHaveBeenCalledWith(expect.anything(),path.join(f.dir,'assets/a.md'));
    expect(await fs.readFile(path.join(f.dir,'assets/a.md'),'utf8')).toBe('old a');
    expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(prior);
    expect((await loadProfile(f.dir)).profile.revision).toBe(2);
    expect(await fs.readFile(path.join(f.dir,'.history/2/profile.json'))).toEqual(prior);
    await expect(fs.access(path.join(f.dir,'.millforge-write.lock'))).rejects.toThrow();
    expect((await fs.readdir(path.join(f.dir,'assets')))).toEqual(['a.md']);
  });
  it.each(['extra','incomplete','different'])('refuses %s existing history without changing it',async(kind)=>{
    const f=await fixture();await createProfile(f.dir,f.manifest);await fs.mkdir(path.join(f.dir,'.history'));await fs.mkdir(path.join(f.dir,'.history/1'));
    const raw=await fs.readFile(path.join(f.dir,'profile.json'));
    if(kind!=='incomplete')await fs.writeFile(path.join(f.dir,'.history/1/profile.json'),kind==='different'?Buffer.concat([raw,Buffer.from(' ') ]):raw);
    if(kind==='extra')await fs.writeFile(path.join(f.dir,'.history/1/extra.txt'),'preserved');
    await expect(editProfile(f.dir,f.manifest)).rejects.toThrow();expect(await fs.readFile(path.join(f.dir,'profile.json'))).toEqual(raw);
    if(kind==='extra')expect(await fs.readFile(path.join(f.dir,'.history/1/extra.txt'),'utf8')).toBe('preserved');
  });
  it('removes its reservation even when closing it throws',async()=>{
    const f=await fixture();await createProfile(f.dir,f.manifest);const open=fs.open;
    const openSpy=vi.spyOn(fs,'open').mockImplementation(async(file,flags,mode)=>{const handle=await open(file,flags,mode);if(file===path.join(f.dir,'.millforge-write.lock')){const close=handle.close.bind(handle);handle.close=async()=>{await close();throw new Error('injected close failure');};}return handle;});
    await expect(editProfile(f.dir,f.manifest)).rejects.toThrow(/injected close failure/);
    expect(openSpy).toHaveBeenCalledWith(path.join(f.dir,'.millforge-write.lock'),'wx',0o600);await expect(fs.access(path.join(f.dir,'.millforge-write.lock'))).rejects.toThrow();
  });
  it('removes its temporary file after a failed write and leaves an existing final untouched',async()=>{
    const f=await fixture();const final=path.join(f.root,'record.json');await fs.writeFile(final,'prior');const open=fs.open;
    const openSpy=vi.spyOn(fs,'open').mockImplementation(async(file,flags,mode)=>{const handle=await open(file,flags,mode);if(String(file).includes('.tmp-'))handle.writeFile=async()=>{throw new Error('injected write failure');};return handle;});
    await expect(atomicNew(final,'new')).rejects.toThrow(/injected write failure/);
    expect(openSpy).toHaveBeenCalledWith(expect.stringContaining(final+'.tmp-'),'wx',0o600);expect(await fs.readFile(final,'utf8')).toBe('prior');expect((await fs.readdir(f.root)).some(name=>name.includes('.tmp-'))).toBe(false);
  });
});

// The ordinary directory lifecycle is schema-generic. The v3 tuple survives every operation.
it('round-trips profile v3 through create, clone, edit, export, import and restore',async()=>{const f=await fixture(),p=object(parseJson(await fs.readFile(new URL('./fixtures/codemode/profile.json',import.meta.url))));await fs.writeFile(f.manifest,JSON.stringify(p));const original=await createProfile(f.dir,f.manifest);expect((await copyProfile(f.dir,path.join(f.root,'clone'),'v3-clone')).profile.schema).toBe('millforge-profile/v3');p.instructions='edited v3';await fs.writeFile(f.manifest,JSON.stringify(p));expect((await editProfile(f.dir,f.manifest)).profile.revision).toBe(2);const exported=await copyProfile(f.dir,path.join(f.root,'export')),imported=await copyProfile(exported.dir,path.join(f.root,'import'));expect(imported.digest).toBe(exported.digest);expect(imported.profile.capabilities).toEqual({extensions:['pi-codemode/v1'],packages:[]});const restored=await restoreProfile(f.dir,1);expect(restored.profile).toMatchObject({schema:'millforge-profile/v3',revision:3,instructions:original.profile.instructions,capabilities:original.profile.capabilities});});
