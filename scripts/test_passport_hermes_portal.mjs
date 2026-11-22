// Isolated phone setup UI test with a fake device: no credentials, accounts or physical badge.
// node scripts/test_passport_hermes_portal.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'releases/hermes-firmware-test/portal');fs.mkdirSync(out,{recursive:true});
const html=fs.readFileSync(path.join(root,'firmware/passport_device/main/wifi_portal_page.hpp'),'utf8').split('R"PORTAL(')[1].split(')PORTAL"')[0];
let language=0,pending=false;const posts=[];
const replies={'/hermes/prefs':()=>({enabled:true,origin:'https://badge.ts.net:8443',has_token:true,pending,uncertain:pending,task_id:'a'.repeat(32)}),'/wifi/status':()=>({phase:'idle',language,bluetooth_enabled:true,bluetooth_name:'AI-Passport-1234'}),'/wifi/scan':()=>({scanning:false,networks:[{ssid_hex:'486f6d65',rssi:-50,secure:true},{ssid_hex:'436166e9',rssi:-80,secure:false}]}),
 '/media/prefs':()=>({city:'杭州',country:'CN',province:'浙江',latitude:30.27,longitude:120.16,radios:18}),'/rates/prefs':()=>({base:'CNY',targets:['USD','JPY']}),
 '/podcast/list':()=>({items:[{id:1,n:'声动早咖啡'}]}),'/stock/list':()=>({items:[{id:'sh600519',n:'贵州茅台'}]}),'/wheel/list':()=>({modes:[{n:'吃饭',o:['面条','米饭']}]})};
const server=http.createServer(async(req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);return;}
 if(req.url==='/menu-editor.js'){res.setHeader('Content-Type','text/javascript');res.end(fs.readFileSync(path.join(root,'scripts/passport_menu.js')));return;}
 res.setHeader('Content-Type','application/json; charset=utf-8');
 if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;posts.push([req.url,JSON.parse(body||'{}')]);if(req.url==='/language')language=JSON.parse(body).language;res.end('{"saved":true}');return;}
 if(req.url==='/identity/data'){res.statusCode=404;res.end('{}');return;}
 const reply=replies[req.url.split('?')[0]];res.end(JSON.stringify(reply?reply():{}));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'passport-portal-en-'));
const chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let socket;
try{
 const portFile=path.join(profile,'DevToolsActivePort');
 for(let i=0;!fs.existsSync(portFile)&&i<150;i++)await pause(100);
 const debugPort=Number(fs.readFileSync(portFile,'utf8').split('\n')[0]);
 const target=(await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(x=>x.type==='page');
 socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
 let sequence=0;const waiting=new Map(),exceptions=[];
 socket.onmessage=event=>{const v=JSON.parse(event.data);if(v.id){const p=waiting.get(v.id);if(p){waiting.delete(v.id);v.error?p.reject(Error(JSON.stringify(v.error))):p.resolve(v.result);}}else if(v.method==='Runtime.exceptionThrown')exceptions.push(v.params.exceptionDetails);};
 const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;waiting.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const assert=(yes,message)=>{if(!yes)throw Error(message);};
 const until=async expression=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await pause(50);}throw Error('timeout: '+expression);};
 await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:390,height:1200,deviceScaleFactor:1,mobile:true});
 await call('Page.navigate',{url:base});await until(`document.readyState==='complete'&&typeof loadHermes==='function'`);
 await evaluate(`document.getElementById('tab-hermes').click()`);
 await until(`document.getElementById('hermes-origin').value==='https://badge.ts.net:8443'`);
 assert(await evaluate(`document.getElementById('wifi-panel').hidden&&!document.getElementById('hermes-panel').hidden`),'tabs failed');
 assert(await evaluate(`document.getElementById('hermes-token').value===''`),'token was exposed');
 let shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(path.join(out,'hermes-settings.png'),Buffer.from(shot.data,'base64'));
 await evaluate(`document.getElementById('hermes-form').requestSubmit()`);
 await until(`document.getElementById('hermes-feedback').textContent.includes('重启')`);
 assert(posts.some(([u,b])=>u==='/hermes'&&b.enabled===true&&b.token===''&&b.origin==='https://badge.ts.net:8443'),'save did not preserve token');
 pending=true;await evaluate(`document.getElementById('tab-hermes').click()`);
 await until(`!document.getElementById('hermes-pending').hidden`);
 assert(await evaluate(`document.getElementById('hermes-save').disabled&&document.getElementById('hermes-resolve').disabled`),'pending task was not locked');
 await evaluate(`document.getElementById('hermes-confirmed').click();document.getElementById('hermes-resolve').click()`);
 await until(`document.getElementById('hermes-feedback').textContent.includes('已解除')`);
 assert(posts.some(([u,b])=>u==='/hermes/resolve'&&b.confirmed_on_mac===true),'resolution lacked confirmation');
 assert(exceptions.length===0,'browser exceptions: '+JSON.stringify(exceptions));
 console.log('HERMES_PORTAL_PASS mode=1 keep_token=1 pending_lock=1 explicit_resolution=1');
 await call('Browser.close');
}finally{socket?.close();chrome.kill('SIGTERM');await new Promise(resolve=>server.close(resolve));await pause(300);fs.rmSync(profile,{recursive:true,force:true});}
