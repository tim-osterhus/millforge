/** Local scripted protocol peer. IPC contains only selected test observations, never raw requests. */
import {createServer} from 'node:http';
let server;
const send=value=>{if(process.connected)process.send(value);};
process.on('message',async config=>{
  if(config.type==='stop'){server?.closeAllConnections();if(server)server.close(()=>process.exit(0));else process.exit(0);return;}
  if(config.type!=='start'||server)return;
  let count=0;
  server=createServer(async(req,res)=>{
    const chunks=[];let size=0;
    for await(const chunk of req){size+=chunk.length;if(size>32*1024*1024){res.writeHead(413);res.end();return;}chunks.push(chunk);}
    let body;try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{res.writeHead(400);res.end();return;}
    count++;
    const controls={};for(const key of ['model','stream','temperature','top_p','max_tokens','max_completion_tokens'])if(Object.hasOwn(body,key))controls[key]=body[key];
    send({type:'request',count,path:req.url,method:req.method,authorization:Object.hasOwn(req.headers,'authorization'),apiKey:Object.hasOwn(req.headers,'x-api-key'),controls});
    const plan=config.plan??[{kind:'normal'}],step=plan[Math.min(count-1,plan.length-1)]??{kind:'normal'};
    if(step.kind==='disconnect'){req.socket.destroy();return;}
    if(step.kind==='redirect'){res.writeHead(307,{location:'/unselected'});res.end();return;}
    if(step.kind==='status'){res.writeHead(step.status??429,{'content-type':'application/json','retry-after':'0'});res.end(JSON.stringify({error:{type:'invalid_request_error',message:'fake provider error SECRET_SENTINEL'}}));return;}
    if(step.delayMs||step.kind==='delay')await new Promise(resolve=>setTimeout(resolve,step.delayMs??200));
    if(res.destroyed)return;
    res.writeHead(200,{'content-type':'text/event-stream','cache-control':'no-cache'});
    if(step.kind==='malformed'){res.end('data: {broken\n\n');return;}
    const anthropic=config.api==='anthropic-messages';
    const event=(type,data)=>res.write((anthropic?'event: '+type+'\n':'')+'data: '+JSON.stringify(data)+'\n\n');
    const text=step.kind==='empty'?'':step.text??'done';
    const usage=step.usage===false?undefined:{input_tokens:7,output_tokens:3,cache_read_input_tokens:0,cache_creation_input_tokens:0};
    if(anthropic){
      event('message_start',{type:'message_start',message:{id:'fake-message',type:'message',role:'assistant',model:'fake-model',content:[],stop_reason:null,usage:usage??{}}});
      if(step.kind==='tool'){
        event('content_block_start',{type:'content_block_start',index:0,content_block:{type:'tool_use',id:step.toolId??'call-1',name:step.toolName??'read',input:{}}});
        event('content_block_delta',{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(step.toolArguments??{path:'input.txt'})}});
      }else{
        if(step.kind==='reasoning'){
          event('content_block_start',{type:'content_block_start',index:0,content_block:{type:'thinking',thinking:''}});
          event('content_block_delta',{type:'content_block_delta',index:0,delta:{type:'thinking_delta',thinking:'PRIVATE_REASONING_SENTINEL'}});
          event('content_block_stop',{type:'content_block_stop',index:0});
        }
        const index=step.kind==='reasoning'?1:0;
        event('content_block_start',{type:'content_block_start',index,content_block:{type:'text',text:''}});
        for(const chunk of step.textChunks??[text])if(chunk)event('content_block_delta',{type:'content_block_delta',index,delta:{type:'text_delta',text:chunk}});
        if(step.finishDelayMs)await new Promise(resolve=>setTimeout(resolve,step.finishDelayMs));
        event('content_block_stop',{type:'content_block_stop',index});
      }
      if(step.kind==='tool')event('content_block_stop',{type:'content_block_stop',index:0});
      if(step.kind!=='missing-finish')event('message_delta',{type:'message_delta',delta:{stop_reason:step.reason??(step.kind==='tool'?'tool_use':step.kind==='length'?'max_tokens':step.kind==='pause'?'pause_turn':step.kind==='unknown-finish'?'alien':'end_turn'),stop_sequence:null},...(usage?{usage}:{})});
      event('message_stop',{type:'message_stop'});
    }else{
      const frame=(delta,finish_reason=null)=>({id:'fake-message',object:'chat.completion.chunk',created:1,model:'fake-model',choices:[{index:0,delta,finish_reason}]});
      if(step.kind==='tool')event('',frame({role:'assistant',tool_calls:[{index:0,id:step.toolId??'call-1',type:'function',function:{name:step.toolName??'read',arguments:JSON.stringify(step.toolArguments??{path:'input.txt'})}}]}));
      else for(const chunk of step.textChunks??[text])event('',frame({role:'assistant',content:chunk,...(step.kind==='reasoning'?{reasoning_content:'PRIVATE_REASONING_SENTINEL'}:{})}));
      if(step.finishDelayMs)await new Promise(resolve=>setTimeout(resolve,step.finishDelayMs));
      if(step.kind!=='missing-finish')event('',frame({},step.reason??(step.kind==='tool'?'tool_calls':step.kind==='length'?'length':step.kind==='unknown-finish'?'alien':'stop')));
      if(usage)event('',{id:'fake-message',object:'chat.completion.chunk',created:1,model:'fake-model',choices:[],usage:{prompt_tokens:7,completion_tokens:3,prompt_tokens_details:{cached_tokens:0}}});
      res.write('data: [DONE]\n\n');
    }
    res.end();
  });
  server.listen(0,'127.0.0.1',()=>send({type:'ready',baseUrl:'http://127.0.0.1:'+server.address().port}));
});
process.on('disconnect',()=>{server?.closeAllConnections();server?.close();process.exit(0);});
