// Finite controlled tool outputs. No provider or user workspace access.
import {writeFileSync} from 'node:fs';
const [mode,arg]=process.argv.slice(2);
if(mode==='bytes')process.stdout.write('x'.repeat(Number(arg)));
else if(mode==='wait'){process.on('SIGTERM',()=>{});setInterval(()=>{},1000);}
else if(mode==='late'){const {spawn}=await import('node:child_process');spawn(process.execPath,['-e',`setTimeout(()=>require('node:fs').writeFileSync(${JSON.stringify(arg)},'late'),700)`],{stdio:'ignore',detached:false});}
else if(mode==='env')process.stdout.write(JSON.stringify(Object.keys(process.env).sort()));
else if(mode==='mark'){writeFileSync(arg,'effect');process.stdout.write('marked');}
else throw new Error('unknown finite tool mode');
