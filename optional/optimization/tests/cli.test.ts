import {describe,it,expect} from 'vitest';import {main,protocol} from '../src/cli.js';
describe('optional public command boundary',()=>{
 it('protocol is exact and side effect free',async()=>{let out='',err='';expect(await main(['protocol'],s=>out+=s,s=>err+=s)).toBe(0);expect(out).toBe(JSON.stringify(protocol)+'\n');expect(err).toBe('');});
 it.each([[],['protocol','extra'],['inspect'],['run','--campaign','x','--base-bin','a','--base-bin','b'],['prepare','--fake','true']].map(args=>({args})))('refuses malformed arguments %j',async({args})=>{let out='',err='';expect(await main(args,s=>out+=s,s=>err+=s)).toBe(2);expect(out).toBe('');expect(JSON.parse(err).code).toBe('INVALID_INPUT');});
});
