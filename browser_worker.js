/* Browser transport only. All cookies and captured session headers stay in memory. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const {randomUUID} = require('node:crypto');

let browser, socket, sequence = 0, startup;
const waits = new Map(), sessions = new Map(), roles = new Map(), captured = new Map();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function send(method, params = {}, sessionId) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {waits.delete(id); reject(Error('timeout'));}, 18000);
    waits.set(id, {resolve, reject, timeout});
    socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
  });
}
function allowed(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === 'https:' && (u.hostname === 'seat.njtech.edu.cn' ||
      u.hostname === 'vpnlib.njtech.edu.cn' || u.hostname.endsWith('.vpnlib.njtech.edu.cn')) &&
      /\/index\.php\/graphql\/?$/.test(u.pathname);
  } catch {return false;}
}
function event(message) {
  const p = message.params || {}, sid = message.sessionId;
  if (message.method === 'Target.attachedToTarget') {
    const info = p.targetInfo;
    sessions.set(p.sessionId, {context: info.browserContextId || sessions.get(sid)?.context, target: info.targetId, frames: new Map()});
    for (const method of ['Network.enable', 'Runtime.enable']) send(method, {}, p.sessionId).catch(() => {});
    send('Target.setAutoAttach', {autoAttach:true, waitForDebuggerOnStart:false, flatten:true}, p.sessionId).catch(() => {});
  } else if (message.method === 'Runtime.executionContextCreated') {
    const item = sessions.get(sid), context = p.context;
    if (item && context.auxData?.isDefault && context.auxData.frameId) item.frames.set(context.auxData.frameId, context.id);
  } else if (message.method === 'Runtime.executionContextDestroyed') {
    const item = sessions.get(sid);
    if (item) for (const [frame, id] of item.frames) if (id === p.executionContextId) item.frames.delete(frame);
  } else if (message.method === 'Target.detachedFromTarget') {
    sessions.delete(p.sessionId);
    for (const [role, value] of captured) if (value.session === p.sessionId) captured.delete(role);
  } else if (message.method === 'Network.requestWillBeSent') {
    const item = sessions.get(sid);
    if (!item || !allowed(p.request.url)) return;
    const role = [...roles].find(([, context]) => context === item.context)?.[0];
    if (!role) return;
    const headers = {};
    for (const [key, value] of Object.entries(p.request.headers)) {
      if (!/^(cookie|host|origin|referer|content-length|connection|accept-encoding|user-agent|sec-.*|:.*)$/i.test(key)) headers[key] = value;
    }
    headers['content-type'] = 'application/json';
    captured.set(role, {endpoint:p.request.url, headers, session:sid, frame:p.frameId});
  }
}
async function openBrowser() {
  if (socket?.readyState === WebSocket.OPEN) return;
  const candidates = [
    path.join(process.env.PROGRAMFILES || '', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || '', 'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env.PROGRAMFILES || '', 'Microsoft/Edge/Application/msedge.exe'),
  ];
  const executable = candidates.find(x => fs.existsSync(x));
  if (!executable) throw Error('browser missing');
  const profile = path.join(process.argv[2], 'browser-' + randomUUID());
  fs.mkdirSync(profile, {recursive:true});
  browser = spawn(executable, ['--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
    '--user-data-dir=' + profile,'--no-first-run','--no-default-browser-check','--disable-sync',
    '--disable-background-networking',...(process.argv.includes('--smoke-test')?['--headless=new']:[]),'about:blank'], {stdio:'ignore', windowsHide:true});
  browser.on('error', () => {});
  let details;
  for (let i=0; i<80; i++) {
    await sleep(200);
    try {details = fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split(/\r?\n/); break;} catch {}
  }
  if (!details) throw Error('browser failed');
  socket = new WebSocket('ws://127.0.0.1:' + details[0] + details[1]);
  await new Promise((resolve,reject) => {socket.addEventListener('open',resolve,{once:true}); socket.addEventListener('error',reject,{once:true});});
  socket.addEventListener('message', e => {
    let msg; try {msg=JSON.parse(String(e.data));} catch {return;}
    if (msg.id) {
      const wait=waits.get(msg.id);
      if (wait) {clearTimeout(wait.timeout);waits.delete(msg.id);msg.error ? wait.reject(Error('browser error')) : wait.resolve(msg.result);}
    } else event(msg);
  });
  socket.addEventListener('close', () => {
    sessions.clear(); captured.clear(); roles.clear();
    for (const wait of waits.values()) {clearTimeout(wait.timeout);wait.reject(Error('closed'));}
    waits.clear(); startup=undefined;
  });
  await send('Target.setDiscoverTargets',{discover:true});
  await send('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:false,flatten:true});
}
async function run(command) {
  if (command.method === 'login') {
    if (!['A','B'].includes(command.role)) throw Error('invalid role');
    if (!startup) startup=openBrowser().catch(e=>{startup=undefined;throw e;});
    await startup;
    let context = roles.get(command.role);
    if (!context) {
      context=(await send('Target.createBrowserContext',{disposeOnDetach:true})).browserContextId;
      roles.set(command.role,context);
    }
    await send('Target.createTarget',{url:'https://vpnlib.njtech.edu.cn/enlink/sso/login',browserContextId:context,newWindow:true});
    return {opened:true};
  }
  if (command.method === 'fetch') {
    const connection = captured.get(command.role);
    if (!connection || !allowed(connection.endpoint)) throw Error('open library page');
    const info = sessions.get(connection.session), contextId = info?.frames.get(connection.frame);
    if (!contextId) throw Error('page unavailable');
    // No arbitrary script endpoint is accepted from the HTTP API.
    const expression = `(async () => {
      const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),12000);
      try {
        const response=await fetch(${JSON.stringify(connection.endpoint)}, {
          method:'POST',credentials:'include',headers:${JSON.stringify(connection.headers)},
          body:${JSON.stringify(JSON.stringify(command.payload))},signal:controller.signal
        });
        if (!response.ok) throw Error('request failed');
        return await response.json();
      } finally {clearTimeout(timer);}
    })()`;
    const result = await send('Runtime.evaluate',{expression,contextId,awaitPromise:true,returnByValue:true},connection.session);
    if (result.exceptionDetails || !result.result?.value) throw Error('request failed');
    return result.result.value;
  }
  throw Error('invalid operation');
}
async function shutdown() {
  if (socket?.readyState===WebSocket.OPEN) {
    for (const context of roles.values()) await send('Target.disposeBrowserContext',{browserContextId:context}).catch(()=>{});
    await send('Browser.close').catch(()=>{});
  }
}
if (process.argv.includes('--smoke-test')) {
  (async()=>{
    try {
      await openBrowser();
      const targets=[];
      for (const role of ['A','B']) {
        const context=(await send('Target.createBrowserContext',{disposeOnDetach:true})).browserContextId;
        roles.set(role,context);
        targets.push((await send('Target.createTarget',{url:'about:blank',browserContextId:context})).targetId);
      }
      for (let i=0;i<40&&!targets.every(t=>[...sessions.values()].some(s=>s.target===t&&s.frames.size));i++)await sleep(100);
      const ids=targets.map(t=>[...sessions].find(([,s])=>s.target===t)?.[0]);
      if(ids.some(id=>!id))throw Error('attach failed');
      await send('Runtime.evaluate',{expression:'globalThis.__seatRole="A"'},ids[0]);
      const isolated=await send('Runtime.evaluate',{expression:'typeof globalThis.__seatRole',returnByValue:true},ids[1]);
      if(isolated.result.value!=='undefined')throw Error('contexts not isolated');
      if(allowed('https://unrelated.invalid/index.php/graphql/')||!allowed('https://seat.njtech.edu.cn/index.php/graphql/'))throw Error('endpoint check failed');
      process.stdout.write(JSON.stringify({browser:'ok',isolatedAccounts:2,realAccountOperations:0})+'\n');
    } catch {process.stdout.write('{"smoke":"failed"}\n');process.exitCode=1;}
    finally {await shutdown();}
  })();
} else {
const input=readline.createInterface({input:process.stdin});
let chain=Promise.resolve();
input.on('line', line => {
  chain=chain.then(async()=>{
    let command;
    try {command=JSON.parse(line); const result=await run(command);process.stdout.write(JSON.stringify({id:command.id,result})+'\n');}
    catch {process.stdout.write(JSON.stringify({id:command?.id,error:'browser unavailable'})+'\n');}
  });
});
input.on('close',async()=>{
  await shutdown();
  process.exit(0);
});
}
