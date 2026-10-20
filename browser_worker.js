/* Chrome keeps each account's login in its own local profile. No password storage. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {randomUUID,createHash}=require('node:crypto');
const {sessionHeaders}=require('./browser_headers');

const smoke=process.argv.includes('--smoke-test');
const runtime=process.argv[2];
const profiles=smoke?path.join(runtime,'profiles'):path.join(process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local'),'MyNjtechSeat','BrowserProfiles');
const manifestFile=path.join(profiles,'accounts.json');
const portal='https://vpnlib.njtech.edu.cn/enlink/sso/login';
const connections=new Map();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
fs.mkdirSync(profiles,{recursive:true});
process.on('uncaughtExceptionMonitor',error=>{
  try {fs.mkdirSync(runtime,{recursive:true});fs.appendFileSync(path.join(runtime,'transport-errors.log'),JSON.stringify({name:error.name,code:error.code,frames:String(error.stack||'').split('\n').slice(1,5)})+'\n');}catch{}
});
let manifest;
try {manifest=JSON.parse(fs.readFileSync(manifestFile,'utf8'));} catch(error){
  // An unreadable binding must never silently select a different profile.
  if(error.code!=='ENOENT')throw Error('saved account bindings unavailable');
  manifest={version:1,accounts:{}};
}
if(manifest?.version!==1||!manifest.accounts||typeof manifest.accounts!=='object'||Array.isArray(manifest.accounts))throw Error('saved account bindings invalid');
function saveManifest(){
  const temp=manifestFile+'.tmp';
  const data=JSON.stringify(manifest);
  fs.writeFileSync(temp,data);
  try {fs.renameSync(temp,manifestFile);}catch(error){
    // Some Windows filesystem providers cannot rename across their overlay.
    if(error.code!=='EXDEV')throw error;
    fs.writeFileSync(manifestFile,data);
  }
}
function account(role){
  if(!['A','B'].includes(role))throw Error('invalid account');
  if(!manifest.accounts[role]){manifest.accounts[role]={profile:role.toLowerCase()+'-'+randomUUID(),salt:randomUUID()};saveManifest();}
  const info=manifest.accounts[role];
  if(typeof info.profile!=='string'||!info.profile.startsWith(role.toLowerCase()+'-')||typeof info.salt!=='string')throw Error('saved account binding invalid');
  return info;
}
function accountHash(info,value){return createHash('sha256').update(info.salt+'\0'+value).digest('hex');}
function allowed(raw){
  try {const u=new URL(raw);return u.protocol==='https:'&&(u.hostname==='seat.njtech.edu.cn'||u.hostname==='vpnlib.njtech.edu.cn'||u.hostname.endsWith('.vpnlib.njtech.edu.cn'))&&/\/index\.php\/graphql\/?$/.test(u.pathname);}catch{return false;}
}
function loginIdentifier(request){
  // Only extract the login name; request bodies and password fields are never saved.
  if(request.method!=='POST'||!request.postData)return;
  let url;try {url=new URL(request.url);}catch{return;}
  if(url.protocol!=='https:'||!url.hostname.endsWith('.njtech.edu.cn')||!/login|auth|sso|cas/i.test(url.pathname))return;
  const keys=['username','userName','loginName','loginname','account'];
  const form=new URLSearchParams(request.postData);
  let values={};try {values=JSON.parse(request.postData);}catch{}
  for(const key of keys){const value=form.get(key)||values?.[key];if(typeof value==='string'&&/^\d{8,16}$/.test(value.trim()))return value.trim();}
}
function status(){return Object.fromEntries(['A','B'].map(role=>{
  const info=account(role),c=connections.get(role);
  return [role,{saved:fs.existsSync(path.join(profiles,info.profile,'Default')),bound:Boolean(info.accountHash),label:info.label||'固定账号 '+role,identityMismatch:Boolean(info.identityMismatch),connected:Boolean(c?.socket?.readyState===WebSocket.OPEN)}];
}));}
class Connection {
  constructor(role){this.role=role;this.info=account(role);this.waits=new Map();this.sessions=new Map();this.sequence=0;}
  send(method,params={},sessionId){
    const id=++this.sequence;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.waits.delete(id);reject(Error('browser timeout'));},18000);
      this.waits.set(id,{resolve,reject,timer});
      this.socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
    });
  }
  async attach(sessionId,info){
    if(info.type!=='page'&&info.type!=='iframe')return;
    if(this.sessions.has(sessionId))return;
    this.sessions.set(sessionId,{target:info.targetId,frames:new Map()});
    for(const method of ['Network.enable','Runtime.enable'])await this.send(method,{},sessionId).catch(()=>{});
    await this.send('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:false,flatten:true},sessionId).catch(()=>{});
  }
  event(message){
    const p=message.params||{},sid=message.sessionId;
    if(message.method==='Target.attachedToTarget')this.attach(p.sessionId,p.targetInfo).catch(()=>{});
    else if(message.method==='Runtime.executionContextCreated'){
      const item=this.sessions.get(sid),context=p.context;
      if(item&&context.auxData?.isDefault&&context.auxData.frameId)item.frames.set(context.auxData.frameId,context.id);
    }else if(message.method==='Runtime.executionContextDestroyed'){
      const item=this.sessions.get(sid);if(item)for(const [frame,id] of item.frames)if(id===p.executionContextId)item.frames.delete(frame);
    }else if(message.method==='Target.detachedFromTarget'){
      this.sessions.delete(p.sessionId);if(this.captured?.session===p.sessionId)this.captured=undefined;
    }else if(message.method==='Network.requestWillBeSent'){
      const identifier=loginIdentifier(p.request);
      if(identifier){
        this.candidate={hash:accountHash(this.info,identifier),label:'尾号 '+identifier.slice(-4)};
        this.info.identityMismatch=Boolean(this.info.accountHash&&this.info.accountHash!==this.candidate.hash);saveManifest();
      }
      if(!this.sessions.has(sid)||!allowed(p.request.url))return;
      this.captured={endpoint:p.request.url,headers:sessionHeaders(p.request.headers),session:sid,frame:p.frameId};
      this.clicked?.delete(this.sessions.get(sid).target);
      // A school seat-page entry contains no query parameters or session tokens.
      try {const doc=new URL(p.documentURL);if(allowed(doc.origin+'/index.php/graphql/')){const entry=doc.origin+doc.pathname;if(this.info.entry!==entry){this.info.entry=entry;saveManifest();}}}catch{}
    }
  }
  async start(){
    const candidates=[path.join(process.env.PROGRAMFILES||'','Google/Chrome/Application/chrome.exe'),path.join(process.env['PROGRAMFILES(X86)']||'','Microsoft/Edge/Application/msedge.exe'),path.join(process.env.LOCALAPPDATA||'','Google/Chrome/Application/chrome.exe'),path.join(process.env.PROGRAMFILES||'','Microsoft/Edge/Application/msedge.exe')];
    const executable=candidates.find(p=>fs.existsSync(p));if(!executable)throw Error('browser missing');
    const profile=path.join(profiles,this.info.profile);
    fs.mkdirSync(path.join(profile,'Default'),{recursive:true});
    const prefsFile=path.join(profile,'Default','Preferences');
    let prefs={};try {prefs=JSON.parse(fs.readFileSync(prefsFile,'utf8'));}catch{}
    prefs.credentials_enable_service=false;
    prefs.profile={...prefs.profile,password_manager_enabled:false};
    prefs.session={...prefs.session,restore_on_startup:1};
    fs.writeFileSync(prefsFile,JSON.stringify(prefs));
    const portFile=path.join(profile,'DevToolsActivePort');let oldPort;
    try {oldPort=fs.readFileSync(portFile,'utf8');}catch{}
    // Passing a startup URL disables Chrome's session-cookie/tab restoration.
    // A school login browser is a GUI window. Hiding it prevents the user from
    // reaching an expired session or completing the login form.
    this.proc=spawn(executable,['--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'--restore-last-session','--no-first-run','--no-default-browser-check','--disable-sync','--disable-background-networking',...(smoke?['--headless=new']:[])],{stdio:'ignore',windowsHide:smoke});
    this.proc.on('error',()=>{});
    let details;
    for(let i=0;i<80;i++){
      await sleep(200);
      try {const raw=fs.readFileSync(portFile,'utf8');if(raw===oldPort&&i<10)continue;details=raw.trim().split(/\r?\n/);break;}catch{}
    }
    if(!details)throw Error('browser failed');
    this.socket=new WebSocket('ws://127.0.0.1:'+details[0]+details[1]);
    await new Promise((resolve,reject)=>{this.socket.addEventListener('open',resolve,{once:true});this.socket.addEventListener('error',reject,{once:true});});
    this.socket.addEventListener('message',e=>{
      let msg;try {msg=JSON.parse(String(e.data));}catch{return;}
      if(msg.id){const w=this.waits.get(msg.id);if(w){clearTimeout(w.timer);this.waits.delete(msg.id);msg.error?w.reject(Error('browser error')):w.resolve(msg.result);}}
      else this.event(msg);
    });
    this.socket.addEventListener('close',()=>{this.captured=undefined;this.sessions.clear();for(const w of this.waits.values()){clearTimeout(w.timer);w.reject(Error('browser closed'));}this.waits.clear();this.startup=undefined;});
    await this.send('Target.setDiscoverTargets',{discover:true});
    await this.send('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:false,flatten:true});
    const {targetInfos}=await this.send('Target.getTargets');
    for(const info of targetInfos.filter(i=>i.type==='page')){
      if(![...this.sessions.values()].some(s=>s.target===info.targetId)){
        const {sessionId}=await this.send('Target.attachToTarget',{targetId:info.targetId,flatten:true});await this.attach(sessionId,info);
      }
    }
    if(!smoke)await this.openEntry(true);
  }
  async openEntry(refresh=false){
    const {targetInfos}=await this.send('Target.getTargets');
    const existing=targetInfos.find(t=>t.type==='page'&&this.info.entry&&t.url===this.info.entry)||
      targetInfos.find(t=>t.type==='page'&&/^https:\/\/[^/]*njtech\.edu\.cn\//.test(t.url));
    if(existing){
      await this.send('Target.activateTarget',{targetId:existing.targetId});
      const sid=[...this.sessions].find(([,s])=>s.target===existing.targetId)?.[0];
      if(refresh&&sid)await this.send('Page.reload',{},sid);
      return;
    }
    const target=targetInfos.find(t=>t.type==='page');
    if(target){
      const session=[...this.sessions].find(([,s])=>s.target===target.targetId)?.[0];
      if(session){await this.send('Page.navigate',{url:this.info.entry||portal},session);return;}
    }
    await this.send('Target.createTarget',{url:this.info.entry||portal,newWindow:true});
  }
  async openLogin(url=portal){
    const {targetInfos}=await this.send('Target.getTargets');
    const target=targetInfos.find(t=>t.type==='page'&&/^https:\/\/[^/]*njtech\.edu\.cn\//.test(t.url))||targetInfos.find(t=>t.type==='page');
    this.captured=undefined;
    let targetId=target?.targetId;
    if(targetId){
      let sid=[...this.sessions].find(([,s])=>s.target===targetId)?.[0];
      if(!sid){const attached=await this.send('Target.attachToTarget',{targetId,flatten:true});sid=attached.sessionId;await this.attach(sid,target);}
      await this.send('Page.navigate',{url},sid);
      await this.send('Page.bringToFront',{},sid).catch(()=>{});
    }else ({targetId}=await this.send('Target.createTarget',{url,newWindow:true}));
    // Activating a tab alone does not restore a minimized browser window.
    try {
      const {windowId,bounds}=await this.send('Browser.getWindowForTarget',{targetId});
      if(bounds.windowState==='minimized')await this.send('Browser.setWindowBounds',{windowId,bounds:{windowState:'normal'}});
    }catch{}
    await this.send('Target.activateTarget',{targetId});
  }
  async ready(){if(this.socket?.readyState===WebSocket.OPEN)return;if(!this.startup)this.startup=this.start().catch(e=>{this.startup=undefined;throw e;});await this.startup;}
  async discover(){
    if(this.captured)return true;
    for(const [sid,item] of this.sessions){
      if(this.clicked?.has(item.target))continue;
      try {
        const result=await this.send('Runtime.evaluate',{expression:`(()=>{const links=[...document.querySelectorAll('a,.img-name')].filter(e=>e.getClientRects().length&&e.textContent.includes('座位预约'));const link=links.find(e=>e.textContent.includes('测试'))||links[0];if(link){link.click();return true;}return false;})()`,returnByValue:true},sid);
        if(result.result?.value){(this.clicked??=new Set()).add(item.target);}
      }catch{}
    }
    return Boolean(this.captured);
  }
  async fetch(payload){
    if(this.info.identityMismatch){const error=Error('identity mismatch');error.code='IDENTITY_MISMATCH';throw error;}
    const c=this.captured,contextId=c&&this.sessions.get(c.session)?.frames.get(c.frame);
    if(!c||!allowed(c.endpoint)||!contextId)throw Error('open library page');
    const expression=`(async()=>{const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),12000);try {const response=await fetch(${JSON.stringify(c.endpoint)},{method:'POST',credentials:'include',headers:${JSON.stringify(c.headers)},body:${JSON.stringify(JSON.stringify(payload))},signal:controller.signal});if(!response.ok)throw Error('request failed');return await response.json();}finally{clearTimeout(timer);}})()`;
    const result=await this.send('Runtime.evaluate',{expression,contextId,awaitPromise:true,returnByValue:true},c.session);
    if(result.exceptionDetails||!result.result?.value)throw Error('request failed');
    return result.result.value;
  }
  confirm(){
    if(this.info.identityMismatch){const error=Error('identity mismatch');error.code='IDENTITY_MISMATCH';throw error;}
    if(this.candidate&&!this.info.accountHash){this.info.accountHash=this.candidate.hash;this.info.label=this.candidate.label;saveManifest();}
    return status()[this.role];
  }
  async close(){if(this.socket?.readyState===WebSocket.OPEN)await this.send('Browser.close').catch(()=>{});if(this.proc)await Promise.race([new Promise(r=>this.proc.once('exit',r)),sleep(2000)]);}
}
async function connection(role){let c=connections.get(role);if(!c){c=new Connection(role);connections.set(role,c);}await c.ready();return c;}
async function run(command){
  if(command.method==='status')return status();
  if(command.method==='restore'){
    await Promise.allSettled(['A','B'].map(role=>connection(role)));
    for(let i=0;i<20;i++){const found=await Promise.all(['A','B'].map(role=>connections.get(role)?.discover().catch(()=>false)));if(found.every(Boolean))break;await sleep(250);}
    return status();
  }
  if(command.method==='login'){const c=await connection(command.role);await c.openLogin();return {opened:true,...status()[command.role]};}
  if(command.method==='switch'){
    const role=command.role;account(role);await connections.get(role)?.close();connections.delete(role);
    manifest.accounts[role]={profile:role.toLowerCase()+'-'+randomUUID(),salt:randomUUID()};saveManifest();
    await connection(role);return {opened:true,...status()[role]};
  }
  if(command.method==='confirmIdentity')return (await connection(command.role)).confirm();
  if(command.method==='fetch'){const c=await connection(command.role);await c.discover();return c.fetch(command.payload);}
  throw Error('invalid operation');
}
async function shutdown(){await Promise.allSettled([...connections.values()].map(c=>c.close()));}
async function smokeTest(){
  const server=http.createServer((req,res)=>{res.setHeader('Set-Cookie',['seat_smoke=retained; Path=/; SameSite=Lax; Max-Age=3600','seat_session=retained; Path=/; SameSite=Lax']);res.end('<title>Profile test</title>');});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const url='http://127.0.0.1:'+server.address().port+'/';
  try {
    const rename=fs.renameSync;
    try {
      fs.renameSync=()=>{const error=Error('overlay rename');error.code='EXDEV';throw error;};
      saveManifest();
    }finally {fs.renameSync=rename;}
    if(JSON.parse(fs.readFileSync(manifestFile,'utf8')).version!==1)throw Error('manifest fallback');
    const [a,b]=await Promise.all(['A','B'].map(connection));
    const session=c=>[...c.sessions.keys()][0];
    await a.send('Runtime.evaluate',{expression:'globalThis.__seatRole="A"'},session(a));
    const isolated=await b.send('Runtime.evaluate',{expression:'typeof globalThis.__seatRole',returnByValue:true},session(b));
    if(isolated.result.value!=='undefined'||a.info.profile===b.info.profile)throw Error('account isolation');
    await a.send('Page.navigate',{url},session(a));
    let cookies;for(let i=0;i<20;i++){cookies=await a.send('Storage.getCookies');if(cookies.cookies.some(c=>c.name==='seat_smoke'))break;await sleep(100);}
    if(!cookies.cookies.some(c=>c.name==='seat_smoke'))throw Error('cookie not set');
    const cleanB=await b.send('Storage.getCookies');if(cleanB.cookies.some(c=>c.name==='seat_smoke'))throw Error('shared cookie');
    const profile=a.info.profile;await a.close();connections.delete('A');
    const restored=await connection('A');
    const saved=await restored.send('Storage.getCookies');
    if(restored.info.profile!==profile||!saved.cookies.some(c=>c.name==='seat_smoke')||!saved.cookies.some(c=>c.name==='seat_session'))throw Error('login profile not retained');
    const loginUrl=url+'?login-test=1';
    await restored.openLogin(loginUrl);
    let loginTargets;
    for(let i=0;i<20;i++){loginTargets=await restored.send('Target.getTargets');if(loginTargets.targetInfos.some(t=>t.type==='page'&&t.url===loginUrl))break;await sleep(100);}
    if(!loginTargets.targetInfos.some(t=>t.type==='page'&&t.url===loginUrl)||restored.info.profile!==profile||restored.captured)throw Error('login navigation failed');
    const fixed=account('A');fixed.accountHash=accountHash(fixed,'100000000001');restored.info.identityMismatch=true;
    let rejected=false;try {await restored.fetch({query:'query {}'});}catch(e){rejected=e.code==='IDENTITY_MISMATCH';}
    if(!rejected)throw Error('identity not enforced');
    if(!allowed('https://seat.njtech.edu.cn/index.php/graphql/')||allowed('https://unrelated.invalid/index.php/graphql/'))throw Error('endpoint guard');
    process.stdout.write(JSON.stringify({browser:'ok',isolatedAccounts:2,persistentProfile:true,loginNavigation:true,identityGuard:true,manifestFallback:true,realAccountOperations:0})+'\n');
  }finally {await shutdown();server.close();}
}
if(smoke)smokeTest().catch(error=>{process.stdout.write(JSON.stringify({smoke:'failed',reason:error.message})+'\n');process.exitCode=1;});
else {
  const input=readline.createInterface({input:process.stdin});let chain=Promise.resolve();
  input.on('line',line=>{chain=chain.then(async()=>{let command;try {command=JSON.parse(line);const result=await run(command);process.stdout.write(JSON.stringify({id:command.id,result})+'\n');}catch(e){process.stdout.write(JSON.stringify({id:command?.id,error:'browser unavailable',code:e.code==='IDENTITY_MISMATCH'?'IDENTITY_MISMATCH':undefined})+'\n');}});});
  input.on('close',async()=>{await shutdown();process.exit(0);});
}
