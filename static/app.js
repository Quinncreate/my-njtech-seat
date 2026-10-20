'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {draft:'草稿',scheduled:'等待开始',running:'运行中',recovering:'正在核对',switching:'正在切换',finishing:'正在退座',paused:'已暂停',attention:'需要处理',completed:'已结束'};
let state, csrf='', refreshing=false, editorId=null, seatAbort, roomSequence=0, toastTimer, exiting=false;
let editorSequence=0, operating=false, lastRendered='', seatLayout=null, seatLoading=false, mapZoom=1, editorTask=null;
let currentView='tasks', selectedTaskId=null, initialized=false;
const dialog=$('#editor'), form=$('#task-form');
const viewNames={overview:['南京工业大学 · 江浦校区','自习工作台','账号、预约和接棒状态，尽在这里。'],tasks:['南京工业大学 · 江浦校区','我的自习任务','把预约安排好，把时间留给专注。'],new:['南京工业大学 · 江浦校区','选择你的自习座位','找到一个好位置，开始今天的专注。']};
function isEditorOpen(){return currentView==='new' && !dialog.hidden;}
function invalidateEditor(){editorSequence++;roomSequence++;seatAbort?.abort();seatLayout=null;seatLoading=false;}
function setView(view, push=true){
  if(!viewNames[view])view='tasks';
  if(currentView==='new' && view!=='new')invalidateEditor();
  currentView=view;
  document.querySelectorAll('[data-view]').forEach(el=>{el.hidden=el.dataset.view!==view;});
  document.querySelectorAll('[data-nav]').forEach(el=>{el.classList.toggle('selected',el.dataset.nav===view);if(el.dataset.nav===view)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  const [eyebrow,title,description]=viewNames[view];
  $('#page-eyebrow').textContent=eyebrow;$('#page-title').textContent=view==='new'&&editorId?'编辑自习任务':title;
  $('#page-description').textContent=description;$('#breadcrumb-title').textContent=view==='new'&&editorId?'编辑任务':title;
  $('#create').hidden=view==='new';$('#back-tasks').hidden=view!=='new';
  if(push && location.hash!=='#'+view)history.pushState(null,'','#'+view);
  window.scrollTo({top:0});
  if(view==='new')requestAnimationFrame(resizeMap);
}
function navigate(view){if(view==='new'){if(!isEditorOpen())openEditor();}else setView(view);}
function localDate(epoch){
  if(!epoch)return '';
  const p=Object.fromEntries(new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(epoch*1000)).map(x=>[x.type,x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
function defaultSchedule(epoch){
  const local=localDate(epoch), date=local.slice(0,10);
  let day=date;
  if(local.slice(11)>='22:00'){
    const [y,m,d]=date.split('-').map(Number);day=new Date(Date.UTC(y,m-1,d+1)).toISOString().slice(0,10);
  }
  return {name:'自习',start:day+'T07:00',end:day+'T22:00'};
}
function fieldValue(label,value){return `<div class="detail-field"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;}
function renderSummary(){
  if(!isEditorOpen())return;
  const seat=seatLayout?.seats.find(s=>s.key===form.elements.seat.value);
  const lib=form.elements.lib.selectedOptions[0]?.textContent || '未选择';
  $('#room-summary').textContent=lib;
  $('#seat-status-tag').textContent=seat?'已选 '+seat.name+' 号':'等待选座';
  $('#seat-status-tag').classList.toggle('has-selection',Boolean(seat));
  const when=value=>value?value.replace('T',' '):'立即开始';
  const fields=[['任务名称',form.elements.name.value || '自习'],['馆室',lib],['座位',seat?seat.name:'未选择'],['当前状态',seat?'准备保存':'等待选座'],['开始时间',when(form.elements.start.value)],['结束时间',form.elements.end.value?when(form.elements.end.value):'持续轮换']];
  $('#task-summary').innerHTML=fields.map(([label,value])=>fieldValue(label,value)).join('');
  const start=form.elements.start.value,end=form.elements.end.value,now=localDate(state.now);
  $('#schedule-hint').textContent=end && end<=now?'结束时间已过，请选择新的自习时间。':start && start<=now?'开始时间已过，点击“开始”后立即执行，结束时间按上方设置。':start?'任务开始后，会等待上方设置的开始时间。':'点击“开始”后立即执行。';
}
function renderTasks(){
  const tasks=state.tasks.filter(t=>t.mode===state.mode);
  if(!tasks.some(t=>t.id===selectedTaskId))selectedTaskId=tasks.find(t=>['running','recovering','scheduled','switching','finishing'].includes(t.status))?.id || tasks[0]?.id || null;
  $('#task-count').textContent=tasks.length+' 条记录';
  $('#task-list').innerHTML=tasks.length?tasks.map(t=>`<button type="button" class="task-row ${t.id===selectedTaskId?'selected':''}" data-select-task="${esc(t.id)}" aria-pressed="${t.id===selectedTaskId}"><div class="task-row-top"><strong>${esc(t.name)}</strong><span class="pill ${t.status==='running'?'blue':t.status==='attention'?'amber':'gray'}">${esc(labels[t.status]||'待核对')}</span></div><p>${esc(t.lib_name||'馆室 '+t.lib)}</p><span class="task-row-meta">座位 ${esc(t.seat_name)} · ${timeText(t.created)}</span></button>`).join(''):'<div class="empty"><span class="empty-symbol">▤</span><strong>还没有自习任务</strong><p>点击“新建任务”开始安排。</p></div>';
  const t=tasks.find(t=>t.id===selectedTaskId);
  $('#task-number').textContent=t?'任务 #'+t.id.slice(0,6):'等待选择';
  if(!t){$('#task-detail').innerHTML='<div class="empty detail-empty"><span class="empty-symbol">▦</span><strong>在这里查看任务详情</strong><p>选择左侧任务，查看持座账号、执行状态和接棒记录。</p></div>';return;}
  const running=['running','recovering','scheduled'].includes(t.status),buttons=[];
  if(['draft','paused','attention'].includes(t.status)&&!t.finish_requested){
    buttons.push(`<button class="secondary" data-edit="${esc(t.id)}">编辑任务</button>`);
    buttons.push(`<button class="primary" data-action="${t.status==='draft'?'start':'resume'}" data-id="${esc(t.id)}">${t.status==='draft'?'开始':'恢复'}</button>`);
  }
  if(running&&!t.finish_requested&&!t.manual_handoff)buttons.push(`<button class="secondary" data-action="pause" data-id="${esc(t.id)}">暂停</button>`);
  const other=t.owner==='A'?'B':t.owner==='B'?'A':null;
  const wait=other?Math.max(0,Math.ceil(t.acquired+65-state.now),state.accounts[other].cooldown||0):0;
  const controlsReady=['0.5.0','0.5.1'].includes(state.version);
  let handoffReason=!controlsReady?'后台正在更新，请稍后刷新':!other?'实际持座后可手动换号':t.pending||t.manual_handoff?'正在核对上次操作':!['running','paused'].includes(t.status)?'当前任务不能切换':!Object.values(state.accounts).every(a=>a.ready)?'请先恢复两个账号的登录':state.accounts[other].reservation?'接棒账号已有预约':state.accounts[t.owner].reservation?.seat!==t.seat||state.accounts[t.owner].reservation?.lib!==t.lib?'当前持座状态需要核对':wait?`保护等待还剩 ${wait} 秒`:'';
  if(t.status!=='completed'&&!t.finish_requested)buttons.push(`<button class="secondary" data-action="handoff" data-id="${esc(t.id)}" data-owner="${esc(t.owner||'')}" ${handoffReason?'disabled':''} title="${esc(handoffReason||'让另一个账号接棒同一座位')}">${other?'切换至账号 '+other:'切换持座账号'}${wait?'（'+wait+'秒）':''}</button>`);
  if(t.status!=='completed')buttons.push(`<button class="secondary" data-action="finish" data-id="${esc(t.id)}" ${t.status==='finishing'||!controlsReady?'disabled':''}>${t.status==='finishing'?'正在退座…':t.finish_requested?'重试退座':'结束轮换'}</button>`);
  else buttons.push(`<button class="secondary" data-action="rerun" data-id="${esc(t.id)}">重跑为新任务</button>`);
  const cooldownWait=t.status==='running'&&!t.owner&&!t.pending&&t.next>state.now;
  const stage=cooldownWait?'账号冷却 · '+Math.ceil(t.next-state.now)+' 秒':labels[t.status]||'待核对';
  const fields=[['目标座位',t.seat_name],['当前阶段',stage],['持座账号',t.owner?'账号 '+t.owner:'尚未持座'],['已完成接棒',t.rotations+' 次'],['开始时间',t.start?timeText(t.start):'立即开始'],['结束时间',t.end?timeText(t.end):'持续轮换'],['预约到期',t.expires?timeText(t.expires):'暂无预约'],['下一动作',running&&t.next?timeText(t.next):t.status==='scheduled'?timeText(t.start):'—']];
  const events=state.events.filter(e=>e.task===t.id).slice(0,8);
  const actionHelp=t.status==='completed'?'':t.finish_requested?'已停止自动轮换，确认退座后才会显示已结束。':'暂停保留座位 · 结束轮换会退座 · 换号让另一账号接棒同一座位';
  $('#task-detail').innerHTML=`<div class="detail-title"><div><h3>${esc(t.name)}</h3><p>${esc(t.lib_name||'馆室 '+t.lib)}</p></div><span class="pill ${t.status==='running'?'blue':t.status==='attention'?'amber':'gray'}">${esc(labels[t.status]||'待核对')}</span></div><div class="task-actions">${buttons.join('')}${actionHelp?`<p class="task-action-help">${esc(actionHelp)}</p>`:''}</div><div class="detail-grid">${fields.map(([label,value])=>fieldValue(label,value)).join('')}</div><p class="task-message">${esc(t.message)}</p><div class="detail-events"><p class="eyebrow">EVENTS</p><h3>最近事件</h3>${events.length?events.map(e=>`<div class="detail-event"><time>${timeText(e.time)}</time><p>${esc(e.message)}</p></div>`).join(''):'<p class="helper">任务运行后将在这里记录接棒与状态变化。</p>'}</div>`;
}
function render(){
  const live=state.mode==='live',tasks=state.tasks.filter(t=>t.mode===state.mode);
  $('#mode').value=state.mode;
  $('#mode-banner').classList.toggle('live',live);
  $('#mode-banner').textContent=live?'真实预约模式 · 固定使用 A、B 两个账号，打开程序自动恢复登录。创建任务后，在「我的任务」开始预约。':'模拟演示 · 当前座位和账号为虚拟数据，可在「学习概览」切换为真实账号。';
  $('#advance').hidden=live;$('#login-help').hidden=!live;
  const running=tasks.filter(t=>['running','recovering','scheduled','switching','finishing'].includes(t.status)).length;
  $('#running-stat').innerHTML=running+'<small>个</small>';
  $('#active-badge').textContent=running?running+' 个任务运行中':'暂无运行任务';
  $('#rotation-stat').innerHTML=tasks.reduce((n,t)=>n+t.rotations,0)+'<small>次</small>';
  const ready=Object.values(state.accounts).filter(a=>a.ready).length;
  $('#account-stat').textContent=ready+'/2 账号就绪';$('#ready-stat').textContent=ready+' / 2';
  $('#account-list').innerHTML=['A','B'].map(role=>{
    const a=state.accounts[role],holding=a.reservation;
    const label=!a.ready?'需要验证':holding?'正在持座':a.cooldown?'冷却 '+a.cooldown+' 秒':'准备就绪';
    const detail=holding?'当前预约 · '+timeText(holding.expires)+' 到期':live?(a.ready?'本机登录已保留 · '+esc(a.label||'固定账号 '+role):esc(a.reason||'正在恢复固定账号，过期时请重新登录')):'模拟账号 · 无需登录';
    return `<article class="account-card"><div class="avatar ${role==='B'?'b':''}">${role}</div><div class="account-info"><strong>账号 ${role}<span class="subtle"> · ${role==='A'?'主账号':'接棒账号'}</span></strong><p>${detail}</p></div><span class="pill ${!a.ready?'gray':holding?'blue':a.cooldown?'amber':''}">${label}</span>${live?`<div class="account-buttons"><button class="text-button account-login" data-login="${role}">${a.ready?'查看登录':'重新登录'}</button><button class="text-button account-switch" data-switch="${role}">更换绑定账号</button></div>`:''}</article>`;
  }).join('');
  $('#events').innerHTML=state.events.length?state.events.slice(0,12).map(e=>`<div class="event"><time>${timeText(e.time)}</time><span>${esc(e.message)}</span></div>`).join(''):'<p>开始使用后，这里会记录任务的变化。</p>';
  renderTasks();
  if(operating)document.querySelectorAll('main > header button, .intro button, #overview button, #tasks button, #mode, #exit').forEach(el=>{el.disabled=true;});
}

function toast(message, error=false) {
  clearTimeout(toastTimer); const el=$('#toast');el.textContent=message;el.className=error?'error':'';el.hidden=false;
  toastTimer=setTimeout(()=>{el.hidden=true;},6500);
}
async function api(url, body, signal) {
  const controller=new AbortController();
  const cancel=()=>controller.abort();
  signal?.addEventListener('abort',cancel,{once:true});
  if (signal?.aborted) controller.abort();
  const timer=setTimeout(()=>controller.abort(),body ? 80000 : (url==='/api/state' ? 8000 : 70000));
  try {
    const response=await fetch(url,{method:body?'POST':'GET',signal:controller.signal,
      headers:body?{'Content-Type':'application/json','X-Seat-Token':csrf}:{},
      ...(body?{body:JSON.stringify(body)}:{})});
    const data=await response.json();
    if (!response.ok) {
      const error=Error(data.error || '暂时无法操作，请稍后重试。');
      error.taskId=data.task_id;throw error;
    }
    return data;
  } finally {clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
}
function timeText(epoch) {
  return epoch ? new Date(epoch*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : '尚未安排';
}

async function refresh() {
  if(refreshing || exiting) return;
  refreshing=true;
  try {
    state=await api('/api/state');csrf=state.csrf;
    const countdowns=state.tasks.map(t=>t.owner?Math.max(0,Math.ceil(t.acquired+65-state.now)):0);
    const content=JSON.stringify({mode:state.mode,accounts:state.accounts,tasks:state.tasks,events:state.events,countdowns});
    if(content!==lastRendered){render();lastRendered=content;}
    $('#connection').textContent='控制台在线';
    if(!initialized){initialized=true;const view=location.hash.slice(1)||'tasks';if(view==='new')openEditor();else setView(view,false);}
  } catch {$('#connection').textContent='连接暂时中断 · 正在重试';}
  finally {refreshing=false;}
}
async function operate(button, fn, message) {
  if(button.disabled || operating) return;
  operating=true;
  document.querySelectorAll('main > header button, .intro button, #overview button, #tasks button, #mode, #exit').forEach(el=>{el.disabled=true;});
  button.disabled=true;
  try {await fn();if(message)toast(message);await refresh();}
  catch(e){
    if(e.taskId && state?.tasks.some(t=>t.id===e.taskId && t.mode===state.mode)){
      selectedTaskId=e.taskId;setView('tasks');renderTasks();
    }
    toast(e.name==='AbortError'?'请求超时，状态未确认，请刷新核对后再操作。':e.message,true);
  }
  finally{operating=false;document.querySelectorAll('main > header button, .intro button, #overview button, #tasks button, #mode, #exit').forEach(el=>{el.disabled=exiting;});if(state)render();if(isEditorOpen())seatSelection();}
}

function originalSeat(s) {
  return editorTask && String(editorTask.lib)===String(seatLayout?.lib) && editorTask.seat===s.key;
}
function canSelect(s) {return s.available===true || originalSeat(s);}
function mapPlaceholder(title, detail, error=false) {
  $('#seat-map').innerHTML=`<div class="map-placeholder ${error?'map-error':''}"><span>▦</span><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>`;
}
function seatSelection() {
  const selected=seatLayout?.seats.find(s=>s.key===form.elements.seat.value);
  $('#save-task').disabled=seatLoading || operating || !selected || !canSelect(selected);
  $('#selected-seat').textContent=selected ? `已选座位 ${selected.name}${selected.available?' · 空闲':' · 保留原任务座位'}${seatLayout.source==='demo'?' · 模拟座位':''}` : '尚未选择座位';
  $('#selected-seat').classList.toggle('has-selection',Boolean(selected));
  $('#seat-map').querySelectorAll('[data-seat]').forEach(el=>{
    const chosen=el.dataset.seat===selected?.key;
    el.classList.toggle('selected',chosen);
    if(el.matches('[role=button],button'))el.setAttribute('aria-pressed',String(chosen));
  });
  renderSummary();
}
function filterMap() {
  const query=$('#seat-search').value.trim().toLowerCase();
  $('#seat-map').querySelectorAll('[data-seat]').forEach(el=>{
    const matched=!query || el.dataset.name.toLowerCase().includes(query);
    el.classList.toggle('dimmed',!matched);
    if(el.classList.contains('seat-number'))el.hidden=!matched;
    if(query && document.activeElement===$('#seat-search') && el.dataset.name.toLowerCase()===query)el.scrollIntoView({block:'nearest',inline:'center'});
  });
}
function resizeMap() {
  const svg=$('#seat-map .seat-svg');if(!svg)return;
  const ratio=Number(svg.dataset.ratio);if(!Number.isFinite(ratio)||ratio<=0)return;
  const width=Math.max(220,($('#seat-map').clientWidth-2)*mapZoom);
  svg.setAttribute('width',String(width));
  svg.setAttribute('height',String(width*ratio));
  $('#zoom-fit').textContent=mapZoom===1?'适应':Math.round(mapZoom*100)+'%';
}
function drawMap() {
  const seats=seatLayout.seats;
  const positioned=seats.length && seats.every(s=>Number.isFinite(s.x)&&Number.isFinite(s.y));
  for(const id of ['#zoom-in','#zoom-out','#zoom-fit'])$(id).disabled=!positioned;
  if(!seats.length){mapPlaceholder('当前馆室没有返回座位','请稍后刷新，或选择其他馆室。');return;}
  if(positioned){
    const width=Math.max(5,seatLayout.max_x||0,...seats.map(s=>s.x))+2;
    const height=Math.max(5,seatLayout.max_y||0,...seats.map(s=>s.y))+2;
    // Position comes from x/y; opaque seat keys are never parsed as coordinates.
    const scale=24, pad=20;
    const xs=[...new Set(seats.map(s=>s.x))].sort((a,b)=>a-b), ys=[...new Set(seats.map(s=>s.y))].sort((a,b)=>a-b);
    const pitch=Math.min(3,...xs.slice(1).map((x,i)=>x-xs[i]),...ys.slice(1).map((y,i)=>y-ys[i]));
    const size=Math.max(7,Math.min(34,pitch*scale-4));
    const w=width*scale+pad*2,h=height*scale+pad*2;
    $('#seat-map').innerHTML=`<svg class="seat-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" data-ratio="${h/w}" role="group" aria-label="${esc(seatLayout.name)}座位分布">${seats.map(s=>{
      const x=pad+s.x*scale,y=pad+s.y*scale, selectable=canSelect(s);
      return `<g class="seat-node ${esc(s.state)}" data-seat="${esc(s.key)}" data-name="${esc(s.name)}" ${selectable?'role="button" tabindex="0"':'aria-disabled="true"'} aria-label="${esc(s.name+'号座位，'+s.status+(originalSeat(s)&&!s.available?'，原任务座位':''))}" transform="translate(${x},${y})"><title>${esc(s.name+' · '+s.status)}</title><rect x="${-size/2}" y="${-size/2}" width="${size}" height="${size}" rx="4"/><text>${esc(s.name)}</text></g>`;
    }).join('')}</svg>`;
    resizeMap();
  }else{
    $('#seat-map').innerHTML='<div class="seat-number-list">'+seats.map(s=>`<button type="button" class="seat-number ${esc(s.state)}" data-seat="${esc(s.key)}" data-name="${esc(s.name)}" ${canSelect(s)?'':'disabled'} aria-label="${esc(s.name+'号座位，'+s.status)}">${esc(s.name)}</button>`).join('')+'</div>';
  }
  $('#map-source').textContent=seatLayout.source==='demo'?'演示布局 · 虚拟座位，不代表实际馆室':positioned?'学校座位坐标 · 未提供门窗位置':'学校未提供完整坐标，显示座位号列表';
  filterMap();seatSelection();
}
async function loadSeats(lib, selected='', keepView=false) {
  seatAbort?.abort();const controller=seatAbort=new AbortController(), seq=++roomSequence;
  seatLayout=null;seatLoading=Boolean(lib);
  form.elements.seat.innerHTML='<option value="">正在加载座位…</option>';form.elements.seat.disabled=true;$('#save-task').disabled=true;
  $('#refresh-seats').disabled=true;$('#seat-search').disabled=true;
  for(const id of ['#zoom-in','#zoom-out','#zoom-fit'])$(id).disabled=true;
  $('#map-counts').textContent=lib?'正在读取空闲情况…':'选择馆室后查看空闲情况';
  $('#map-updated').textContent='';$('#selected-seat').textContent='尚未选择座位';
  $('#selected-seat').classList.remove('has-selection');
  $('#seat-hint').textContent='只允许新选明确空闲的座位；状态不明的座位不可选。';
  renderSummary();
  if(!keepView){mapZoom=1;$('#seat-search').value='';}
  mapPlaceholder(lib?'正在加载座位…':'先选馆室，再找座位','空闲座位和分布图会一起更新。');
  if(!lib){form.elements.seat.innerHTML='<option value="">请先选择馆室</option>';return;}
  try {
    const layout=await api('/api/seatmap?lib='+encodeURIComponent(lib),undefined,controller.signal);
    if(seq!==roomSequence || String(form.elements.lib.value)!==String(lib) || !isEditorOpen())return;
    if(String(layout.lib)!==String(lib)||!Array.isArray(layout.seats))throw Error('座位信息不完整，请刷新后重试。');
    seatLayout=layout;seatLoading=false;
    if(!keepView && layout.source==='school' && layout.seats.length>100){
      const xs=layout.seats.map(s=>s.x).filter(Number.isFinite);
      const readableWidth=(Math.max(5,layout.max_x||0,...xs)+2)*24+40;
      mapZoom=Math.min(8,Math.max(2,readableWidth/Math.max(220,$('#seat-map').clientWidth-2)));
    }
    renderSeatOptions(selected);
    const available=layout.seats.filter(s=>s.available===true).length;
    $('#map-title').textContent=layout.name || '实时座位场景图';
    $('#map-counts').innerHTML=`<strong>${available}</strong> 个空闲座位 <span class="subtle">/ 共 ${layout.seats.length} 个${layout.source==='demo'?' · 模拟演示':''}</span>`;
    $('#map-updated').textContent='每 20 秒刷新 · '+new Date(layout.observed_at*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
    $('#refresh-seats').disabled=false;$('#seat-search').disabled=!layout.seats.length;
    $('#seat-hint').textContent=selected && !form.elements.seat.value?'原选择已不可用，请重新双击白色空闲座位。':available?'双击白色空闲座位即可选中，选中后变为绿色；红色座位不可选。也可从列表选择。':layout.is_open?'暂时没有空闲座位，可稍后刷新或切换馆室。':'馆室当前未开放，请选择其他馆室。';
    drawMap();
  } catch(e) {
    if(seq===roomSequence && isEditorOpen()){
      seatLoading=false;form.elements.seat.innerHTML='<option value="">加载失败，请刷新座位</option>';
      $('#refresh-seats').disabled=false;$('#map-counts').textContent='空闲情况暂时无法确认';
      mapPlaceholder('座位信息读取失败','请刷新座位后再选择，旧的座位状态不会继续用于选座。',true);
      if(e.name!=='AbortError')toast(e.message,true);
    }
  }
}

function renderSeatOptions(selected=form.elements.seat.value){
  if(!seatLayout)return;
  const options=seatLayout.seats.filter(s=>$('#show-all-seats').checked || canSelect(s));
  form.elements.seat.innerHTML='<option value="">请选择座位</option>'+options.map(s=>`<option value="${esc(s.key)}" ${canSelect(s)?'':'disabled'}>${esc(s.name)} · ${esc(s.available?'空闲':originalSeat(s)?'原任务座位（仅保留）':s.status)}</option>`).join('');
  form.elements.seat.disabled=!options.some(canSelect);
  form.elements.seat.value=options.some(s=>s.key===selected && canSelect(s))?selected:'';
}
async function openEditor(id=null){
  if(!state || operating)return;
  const seq=++editorSequence;editorId=id;form.reset();
  const task=editorTask=id?state.tasks.find(t=>t.id===id):null;
  $('#editor-mode').textContent=state.mode==='demo'?'当前为模拟演示，保存和开始均不会操作真实预约。':'当前为真实账号模式，请核对馆室和座位。';
  $('#save-task').textContent=task?'保存修改':'创建任务';
  form.elements.lib.innerHTML='<option value="">正在加载馆室…</option>';form.elements.lib.disabled=true;
  form.elements.seat.innerHTML='<option value="">请先选择馆室</option>';form.elements.seat.disabled=true;$('#save-task').disabled=true;
  if(task){for(const key of ['name','lead'])form.elements[key].value=task[key];for(const key of ['start','end'])form.elements[key].value=localDate(task[key]);}
  else{const defaults=defaultSchedule(state.now);for(const key of ['name','start','end'])form.elements[key].value=defaults[key];}
  setView('new');loadSeats('');$('#map-title').textContent='实时座位场景图';renderSummary();
  try{
    const libs=await api('/api/libraries');
    if(seq!==editorSequence || !isEditorOpen())return;
    const groups=[...new Set(libs.map(x=>x.building))];
    form.elements.lib.innerHTML='<option value="">请选择馆室</option>'+groups.map(building=>`<optgroup label="${esc(building)} · 江浦校区">${libs.filter(x=>x.building===building).map(x=>`<option value="${x.id}" ${x.is_open?'':'disabled'}>${esc(building+' / '+x.name+' / '+x.floor)}${x.is_open?'':'（未开放）'}</option>`).join('')}</optgroup>`).join('');form.elements.lib.disabled=false;
    const lib=task?.lib || libs.find(x=>x.is_open)?.id;
    if(lib){form.elements.lib.value=lib;renderSummary();await loadSeats(form.elements.lib.value,task?.seat || '');}
    else $('#seat-hint').textContent='当前没有可选馆室，请稍后重试。';
  }catch(e){if(seq===editorSequence && isEditorOpen()){form.elements.lib.innerHTML='<option value="">加载失败，请重试新建任务</option>';renderSummary();toast(e.message,true);}}
}
for(const el of document.querySelectorAll('a[href^="#"]'))el.addEventListener('click',e=>{e.preventDefault();if(operating){toast('正在处理，请稍等。');return;}navigate(el.getAttribute('href').slice(1));});
window.addEventListener('popstate',()=>{const view=location.hash.slice(1)||'tasks';if(view==='new'){if(!isEditorOpen())openEditor();}else setView(view,false);});
$('#create').addEventListener('click',()=>openEditor());
for(const id of ['#back-tasks','#cancel-editor'])$(id).addEventListener('click',()=>setView('tasks'));
form.elements.lib.addEventListener('change',()=>{renderSummary();loadSeats(form.elements.lib.value);});
form.elements.seat.addEventListener('change',seatSelection);
form.addEventListener('input',renderSummary);
$('#show-all-seats').addEventListener('change',()=>{renderSeatOptions();seatSelection();});
$('#refresh-seats').addEventListener('click',()=>loadSeats(form.elements.lib.value,form.elements.seat.value,true));
$('#seat-search').addEventListener('input',filterMap);
function selectMapSeat(target){
  if(seatLoading || operating || !seatLayout)return;
  const s=seatLayout.seats.find(s=>s.key===target?.dataset.seat);
  if(!s || !canSelect(s))return;
  form.elements.seat.value=s.key;seatSelection();
}
$('#seat-map').addEventListener('dblclick',e=>{e.preventDefault();selectMapSeat(e.target.closest('[data-seat]'));});
$('#seat-map').addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-seat][role=button],button[data-seat]:not(:disabled)')){e.preventDefault();selectMapSeat(e.target);}});
for(const [id,change] of [['#zoom-in',.5],['#zoom-out',-.5],['#zoom-fit',0]])$(id).addEventListener('click',()=>{mapZoom=change?Math.min(8,Math.max(1,mapZoom+change)):1;resizeMap();});
window.addEventListener('resize',()=>{
  const svg=$('#seat-map .seat-svg');
  if(svg && isEditorOpen() && mapZoom>1){
    mapZoom=Math.min(8,Math.max(1,Number(svg.getAttribute('width'))/Math.max(220,$('#seat-map').clientWidth-2)));
  }
  resizeMap();
});
form.addEventListener('submit',e=>{
  e.preventDefault();
  if(seatLoading || !seatLayout || !seatLayout.seats.some(s=>s.key===form.elements.seat.value && canSelect(s)))return;
  const parseTime=value=>value?new Date(value+':00+08:00').getTime()/1000:0;
  const body={name:form.elements.name.value,lib:Number(form.elements.lib.value),lib_name:form.elements.lib.selectedOptions[0]?.textContent,
    seat:form.elements.seat.value,lead:Number(form.elements.lead.value),start:parseTime(form.elements.start.value),end:parseTime(form.elements.end.value)};
  if(body.end && body.end<=Math.max(body.start,state.now)){toast('结束时间需要晚于开始时间和当前时间。',true);return;}
  operate($('#save-task'),async()=>{
    const controls=[...form.querySelectorAll('input,select,#refresh-seats,.zoom-controls button,#cancel-editor')].map(el=>[el,el.disabled]);
    controls.forEach(([el])=>{el.disabled=true;});
    try{
      const result=await api(editorId?`/api/tasks/${editorId}/edit`:'/api/tasks',body);
      selectedTaskId=editorId || result.id;setView('tasks');
    }finally{controls.forEach(([el,disabled])=>{el.disabled=disabled;});}
  },'任务已保存。');
});
$('#task-list').addEventListener('click',e=>{const button=e.target.closest('[data-select-task]');if(!button || operating)return;selectedTaskId=button.dataset.selectTask;renderTasks();});
$('#task-detail').addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.edit){openEditor(button.dataset.edit);return;}
  const {id,action}=button.dataset;if(!action)return;
  if(state.mode==='live'&&['start','resume'].includes(action)&&!confirm('开始后，程序会使用两个账号操作真实预约，轮换时会先退座再由另一账号接棒。请确认两个账号不同，并已核对座位。'))return;
  if(action==='finish'&&!confirm('结束轮换并退座？程序将停止接棒并释放本任务座位，确认退座后显示已结束。'))return;
  if(action==='handoff'&&state.mode==='live'&&!confirm('让另一账号立即接棒同一座位？程序会先核对，再退座并预约；原来暂停的任务仍保持暂停。'))return;
  operate(button,async()=>{const result=await api(`/api/tasks/${id}/${action}`,action==='handoff'?{owner:button.dataset.owner}:action==='finish'?{release:true}:{});if(action==='rerun')selectedTaskId=result.id;},action==='rerun'?'已复制为草稿，请核对后开始。':action==='finish'?'正在结束并核对退座结果。':action==='handoff'?'正在核对并切换持座账号。':'任务状态已更新。');
});
$('#account-list').addEventListener('click',e=>{
  const button=e.target.closest('[data-login],[data-switch]');if(!button)return;
  if(button.dataset.switch){
    if(!confirm('更换账号 '+button.dataset.switch+'？将新建独立登录档案，原绑定不会用于新账号。请先结束当前任务。'))return;
    operate(button,()=>api('/api/accounts/switch',{role:button.dataset.switch}),'新登录档案已打开，登录后自动绑定；另一个账号保持原绑定。');
  }else operate(button,()=>api('/api/accounts/login',{role:button.dataset.login}),'固定账号的学校窗口已打开，完成登录后会自动验证。');
});
$('#verify').addEventListener('click',e=>operate(e.currentTarget,()=>api('/api/accounts/verify',{}),'账号状态已刷新。'));
$('#advance').addEventListener('click',e=>operate(e.currentTarget,()=>api('/api/demo/advance',{}),'模拟时间已推进，已检查一次接棒。'));
$('#mode').addEventListener('change',e=>{
  const mode=e.target.value;
  if(mode==='live'&&!confirm('切换到真实账号模式？登录由你在学校官网完成，开始任务后会操作真实预约。')){e.target.value=state.mode;return;}
  operate(e.target,()=>api('/api/mode',{mode}),'运行模式已切换。').then(()=>{$('#mode').value=state.mode;});
});
$('#exit').addEventListener('click',e=>{
  if(!confirm('退出后台程序？退出期间不会执行轮换。任务将保存，下次启动先核对预约再恢复；当前预约不会主动退座。'))return;
  operate(e.currentTarget,async()=>{await api('/api/shutdown',{});exiting=true;$('#connection').textContent='后台已退出';},'后台已退出，可以关闭此页面。');
});
refresh();setInterval(refresh,3000);
setInterval(()=>{if(isEditorOpen() && !seatLoading && !operating && form.elements.lib.value)loadSeats(form.elements.lib.value,form.elements.seat.value,true);},20000);
