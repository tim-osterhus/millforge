import {describe,it,expect} from 'vitest';
import {runProcess,processSpec} from '../src/lifecycle.js';
import {performance} from 'node:perf_hooks';
import {parse,budget,fullLimits,target,canonical,workMap} from '../src/contracts.js';
describe('strict current contracts',()=>{
 it('rejects duplicate/nonfinite/unknown input',()=>{for(const s of ['{"a":1,"a":2}','1e999','{} trailing'])expect(()=>parse(s)).toThrow();expect(()=>budget({max_work_items:7},'search')).toThrow();});
 it('keeps current family explicit and canonical',()=>{expect(target.request_schema).toBe('millforge-request/v1');expect(target.capture_schema).toBe('millforge-capture/v3');expect(canonical({z:1,a:2})).toBe('{"a":2,"z":1}');});
 it('stable_limits_and_fit clips integer timeouts without changing byte limits',()=>{const p={limits:{model_requests:8,tool_calls:24,elapsed_seconds:180,model_timeout_seconds:175,tool_timeout_seconds:175,input_bytes:100,context_bytes:200,output_bytes:100,tool_output_bytes:100,trace_bytes:20000}};const b={max_model_requests:35,max_tool_calls:96,executor_seconds:60,proposal_seconds:20};expect(fullLimits(p,b,false)).toMatchObject({elapsed_seconds:60,model_timeout_seconds:55,tool_timeout_seconds:55,input_bytes:100});expect(()=>workMap({campaign_id:'x',purpose:'baseline_validation',budget:{...b,max_model_requests:8,max_tool_calls:24,executor_seconds:180,evaluation_seconds:1,elapsed_seconds:185}},p,null)).toThrow('cannot fit');});
 it('retains a UTF-8 character split across owned process output chunks',async()=>{const spec=processSpec(process.execPath,['-e',"process.stdout.write(Buffer.from([0xe2]));setTimeout(()=>process.stdout.end(Buffer.from([0x82,0xac])),10)"],process.cwd(),performance.now()+10000);const result=await runProcess(spec);expect(result.exit).toBe(0);expect(result.closed).toBe(true);expect(result.stdout).toBe('€');});
});
