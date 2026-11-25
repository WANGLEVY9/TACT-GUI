const $ = id => document.getElementById(id);
const state = {
  view: 'explorer', archive: [], profiles: [], jobs: [], run: null, detail: null,
  caseId: null, role: null, trace: null, index: 0, timer: null, running: false,
  traceTicket: 0, detailTicket: 0, checked: '', manifestReady: false,
};
const label = {
  direct: 'Direct', guarded_direct: 'Stop-aware', self_review: 'Self-review',
  independent_review: 'Independent diagnosis', verified: 'Verified', incomplete: 'Incomplete',
  unresolved: 'Unresolved', queued: 'Queued', running: 'Running',
  interrupted: 'Partial', unrecorded: 'Unrecorded',
};
const fmt = value => value == null ? '—' : Number(value).toLocaleString('en-US');
const caseName = id => id.replace(/-h([23])$/, ' · $1 calls').replace(/-/g, ' ');
const endpoint = (path, values={}) => path + '?' + new URLSearchParams(values);
const node = (tag, text, css) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (css) element.className = css;
  return element;
};
const button = (text, click, css='button') => {
  const element = node('button',text,css); element.type='button';
  element.addEventListener('click',click); return element;
};
const cls = status => status==='verified' ? 'ok' : ['incomplete','unresolved'].includes(status) ? 'warn' : '';
const query = new URLSearchParams(location.search);
state.run = query.get('run');
state.caseId = query.get('case');
state.role = query.get('role');
state.index = Math.max(0,Number(query.get('frame')) || 0);

async function api(path, body) {
  const result = await fetch(path,body===undefined ? {} : {
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),
  });
  const data = await result.json();
  if (!result.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function options(element, items, value) {
  const previous = element.value;
  const next = items.map(([id,name])=>[String(id),String(name)]);
  const key = JSON.stringify(next);
  if (element.dataset.options !== key) {
    element.replaceChildren(...next.map(([id,name])=>new Option(name,id)));
    element.dataset.options = key;
  }
  const selection = value ?? previous;
  if (next.some(([id])=>id===selection)) element.value=selection;
}
function toast(text) {
  $('toast').textContent=text;
  $('toast').hidden=false;
  clearTimeout(toast.timer);
  toast.timer=setTimeout(()=>$('toast').hidden=true,3600);
}
function persist() {
  if (!state.run) return;
  const params = {run:state.run};
  if (state.caseId) params.case=state.caseId;
  if (state.role) params.role=state.role;
  params.frame=String(state.index);
  history.replaceState(null,'','?'+new URLSearchParams(params));
}
function switchView(view) {
  state.view=view;
  for (const el of document.querySelectorAll('.view')) el.hidden=el.id!==view;
  for (const el of document.querySelectorAll('.nav')) {
    const active=el.dataset.view===view;
    el.classList.toggle('active',active);
    if (active) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');
  }
  $('crumb').textContent={explorer:'Run explorer',library:'Test library',artifacts:'Artifacts'}[view];
  if (view==='library') renderLibrary();
  if (view==='artifacts') renderArtifacts();
  if (view!=='explorer') stopPlay();
}

function renderRuns() {
  const rows=state.archive;
  $('run-count').textContent=rows.length;
  const choices=rows.map(r=>[r.id,r.name]);
  if (state.run && !rows.some(r=>r.id===state.run)) choices.unshift([state.run,'Preparing selected run…']);
  options($('run-select'),choices,state.run);
  options($('artifact-run'),choices,state.run);
  const nav=rows.map(run=>{
    const b=button('',()=>{switchView('explorer');selectRun(run.id);},'run-link'+(run.id===state.run?' selected':''));
    const parts=run.name.split(' / ');
    const status=state.jobs.some(j=>j.run===run.id && j.exit===null) ? 'Recording' : run.complete ? 'Complete' : 'Partial recording';
    b.append(node('strong',parts[0].replace(/-/g,' ')),node('small',parts.slice(1).join(' / ') || status));
    b.title=run.name;
    if (run.id===state.run) b.setAttribute('aria-current','true');
    return b;
  });
  $('run-nav').replaceChildren(...nav);
}
function renderJob() {
  const active=state.jobs.find(j=>j.exit===null);
  state.running=Boolean(active);
  $('stop').hidden=!active;
  $('stop').disabled=false;
  $('connection').replaceChildren(node('span',null,'dot'),document.createTextNode(active?' Recording run':' Local console'));
  const job=active || state.jobs[0];
  const banner=$('status-banner');
  banner.hidden=!job;
  if (!job) return;
  const message=active ? 'Run in progress' : job.exit===0 ? 'Run complete' : job.exit<0 ? 'Run stopped' : 'Run failed';
  const last=job.log.trim().split('\n').filter(Boolean).pop() || 'Preparing the emulator…';
  banner.replaceChildren(node('b',message),document.createTextNode(last+' '),button('View process output',()=>{
    openPreview('Process output',job.log || 'No process output yet.');
  },'link-btn'));
  updateStart();
}
function metric(title,value,detail,fraction) {
  const card=node('div',null,'metric');
  card.append(node('span',title),node('strong',value),node('small',detail));
  if (fraction!=null) {
    const bar=node('div',null,'bar-track'); const fill=node('i');
    fill.style.width=Math.min(100,Math.max(0,100*fraction))+'%'; bar.append(fill); card.append(bar);
  }
  return card;
}
function renderDetail() {
  const d=state.detail;
  $('run-content').hidden=!d;
  $('empty-run').hidden=Boolean(d) || Boolean(state.run);
  if (!d) {
    $('run-caption').textContent=state.run?'Preparing selected run…':'Browse local recordings and configure Android experiments.';
    renderArtifacts();
    return;
  }
  const entry=state.archive.find(x=>x.id===state.run);
  const date=new Date(d.created).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  const status=d.complete?'Archived':d.active?'Recording':'Partial recording';
  $('run-caption').textContent=(entry?.name || d.id)+'  ·  '+date+'  ·  '+status;
  const count=d.counts;
  const total=Object.values(count).reduce((a,b)=>a+b,0);
  const terminal=(count.verified||0)+(count.incomplete||0)+(count.unresolved||0);
  const limit=d.budget.limits?.model_calls, spent=d.budget.spent?.model_calls;
  const reserved=d.budget.reserved?.model_calls || 0;
  $('metrics').replaceChildren(
    metric('Branches recorded',fmt(terminal)+' / '+fmt(total),d.cases.length+' checkpoint conditions',terminal/(total||1)),
    metric('Verified endpoints',fmt(count.verified||0),(count.incomplete||0)+' incomplete · '+(count.unresolved||0)+' unresolved',(count.verified||0)/(total||1)),
    metric('Settled model calls',fmt(spent)+' / '+fmt(limit),reserved+' calls held · '+(d.activity.responses||0)+' responses',limit?(spent||0)/limit:null),
    metric('Captured frames',fmt(d.activity.frames||0),fmt(d.activity.tokens||0)+' recorded tokens',null),
  );
  if (!d.cases.some(c=>c.id===state.caseId)) {
    state.caseId=(d.cases.find(c=>c.id==='calculator-multiply-h2') || d.cases.find(c=>c.arms.some(a=>a.frames)) || d.cases[0])?.id;
  }
  const selected=d.cases.find(c=>c.id===state.caseId);
  options($('case-select'),d.cases.map(c=>[c.id,caseName(c.id)]),state.caseId);
  if (selected) {
    if (!selected.arms.some(a=>a.role===state.role)) state.role=selected.arms.find(a=>a.frames)?.role || selected.arms[0]?.role;
    options($('role-select'),selected.arms.map(a=>[a.role,label[a.role] || a.role]),state.role);
  }
  renderMatrix();
  renderArtifacts();
}
function renderMatrix() {
  const d=state.detail;
  if (!d) return;
  const roles=Object.keys(d.roles);
  $('matrix-total').textContent=d.cases.length+' CONDITIONS';
  const head=node('thead'); const row=node('tr');
  for (const title of ['Checkpoint',...roles.map(role=>label[role])]) {
    const th=node('th',title); th.scope='col'; row.append(th);
  }
  head.append(row);
  const body=node('tbody');
  for (const c of d.cases) {
    const tr=node('tr'); tr.append(node('td',caseName(c.id)));
    for (const role of roles) {
      const arm=c.arms.find(a=>a.role===role);
      const td=node('td');
      if (arm) {
        const text=label[arm.state]+(arm.lost_goal?' · Goal lost':'');
        const b=button(text,()=>chooseCase(c.id,role),cls(arm.state));
        b.setAttribute('aria-label',caseName(c.id)+', '+label[role]+', '+text);
        td.append(b);
      } else td.textContent='—';
      tr.append(td);
    }
    body.append(tr);
  }
  $('matrix').replaceChildren(head,body);
}
function verifyRow(name,value,tone) {
  const row=node('div',null,'verify-row');
  row.append(node('span',name),node('b',value,tone)); return row;
}
function actionField(name,value) {
  const box=node('div'); box.append(node('span',name,'label'),node('strong',value)); return box;
}
const booleanLabel = (value,yes,no) => value==null?'—':value?yes:no;
function frameType(frame) {
  return frame.role==='review' ? 'Review' : frame.action?.action==='terminate' ? 'Stop' :
    frame.action?.action==='left_click' ? (frame.executed?'Tap':'Proposed tap') : frame.text?'Response':'Capture';
}
function capturedFrame() { return state.trace?.frames[state.index]; }
function setFrame(index) {
  if (!state.trace?.frames.length) return;
  state.index=Math.max(0,Math.min(index,state.trace.frames.length-1));
  renderTrace();
  persist();
}
function renderTrace() {
  const t=state.trace;
  const f=capturedFrame();
  const length=t?.frames.length ?? 0;
  $('stage-case').textContent=t ? caseName(t.case) : '—';
  $('stage-aim').textContent=t?.goal || '';
  $('stage-index').textContent=f ? String(state.index+1).padStart(2,'0')+' / '+String(length).padStart(2,'0'):'—';
  $('stage-meta').textContent=f ? frameType(f) : '';
  $('phone').hidden=!f;
  $('no-frame').hidden=Boolean(f);
  const img=$('frame-img'), point=$('target-point'), full=$('frame-full');
  full.hidden=!f;
  if (f) full.href=f.image;
  else full.removeAttribute('href');
  if (f && img.getAttribute('src')!==f.image) img.src=f.image;
  if (!f) img.removeAttribute('src');
  point.hidden=!(f?.action?.coordinate && f.action.action==='left_click');
  if (!point.hidden) {
    point.style.left=(f.action.coordinate[0]/10)+'%';
    point.style.top=(f.action.coordinate[1]/10)+'%';
  }
  $('previous').disabled=!f || state.index<=0;
  $('next').disabled=!f || state.index>=length-1;
  $('play').disabled=length<=1;
  $('frame-position').textContent=f ? (state.index+1)+' / '+length : '0 frames';
  $('timeline').replaceChildren(...(t?.frames || []).map((frame,index)=>{
    const b=button('',()=>{stopPlay();setFrame(index);},'frame-tab'+(index===state.index?' selected':''));
    b.append(node('strong',String(index+1).padStart(2,'0')),node('span',frameType(frame)));
    b.setAttribute('aria-label','Frame '+(index+1)+', '+frameType(frame));
    b.setAttribute('aria-pressed',String(index===state.index));
    return b;
  }));
  const body=$('action-body');
  $('action-chip').textContent=f?.role || 'No frame';
  if (!f) body.replaceChildren(node('p','Select a strategy with recorded frames.'));
  else {
    const title=node('div',frameType(f),'action-name');
    const desc=node('div',f.role==='review'?'Advice for the executor':f.executed?'Confirmed in the action log':
      f.action?.action==='terminate'?'Model requested termination':f.text?'No executed action matched this response':
      t.active?'Waiting for model response':'No response recorded','action-meta');
    const fields=node('div',null,'fields');
    fields.append(actionField('Coordinates',f.action?.coordinate?.join(', ') || '—'),actionField('Model tokens',fmt(f.tokens)),
                  actionField('Call index',f.index+1),actionField('Action index',f.action_index ?? '—'));
    if (f.model) fields.title=f.model;
    const details=node('details',null,'response');
    details.open=Boolean(body.querySelector('details[open]'));
    details.append(node('summary','Model response'),node('pre',f.text || 'Response unavailable.'));
    body.replaceChildren(title,desc,fields,details);
  }
  const verified=t?.verification || {};
  const progress=t?.progress || {};
  const v=$('verification-body');
  v.replaceChildren(
    verifyRow('Goal after this action',f?.goal_after==null && f?.live_goal!=null ?
      booleanLabel(f.live_goal,'Met · live','Unmet · live') : booleanLabel(f?.goal_after,'Met','Unmet'),
      f?.goal_after?'yes':''),
    verifyRow('Terminal goal',booleanLabel(t?.success,'Verified','Incomplete'),t?.success===1?'yes':t?.success===0?'no':''),
    verifyRow('Replay result',booleanLabel(verified.replay,'Met','Unmet'),verified.replay?'yes':''),
    verifyRow('First confirmed action',progress.first_confirmed_action ?? '—'),
    verifyRow('Goal lost later',booleanLabel(progress.lost_goal,'Yes','No'),progress.lost_goal?'no':''),
    verifyRow('Device operations',fmt(t?.actual?.device_steps)),
  );
  if (t?.error) v.append(verifyRow('Failure',t.error));
}
function stopPlay() {
  clearInterval(state.timer); state.timer=null;
  $('play').textContent='▶'; $('play').setAttribute('aria-label','Play sequence');
}
function play() {
  if (state.timer) {stopPlay();return;}
  if (state.trace?.frames.length<=1) return;
  if (state.index>=state.trace.frames.length-1) setFrame(0);
  $('play').textContent='Ⅱ'; $('play').setAttribute('aria-label','Pause sequence');
  state.timer=setInterval(()=>{
    if (state.index>=state.trace.frames.length-1) {stopPlay();return;}
    setFrame(state.index+1);
  },1100);
}

async function loadTrace(force=false) {
  if (!state.run || !state.caseId || !state.role) return;
  const ticket=++state.traceTicket;
  try {
    const t=await api(endpoint('/api/trace',{run:state.run,case:state.caseId,role:state.role}));
    if (ticket!==state.traceTicket) return;
    if (!force && JSON.stringify(t)===JSON.stringify(state.trace)) return;
    state.trace=t;
    state.index=Math.max(0,Math.min(state.index,t.frames.length-1));
    renderTrace(); persist();
  } catch (error) { if (force && ticket===state.traceTicket) toast(error.message); }
}
async function loadDetail(force=false) {
  if (!state.run) return;
  const selected=state.run;
  const ticket=++state.detailTicket;
  try {
    const d=await api(endpoint('/api/run',{run:selected}));
    if (ticket!==state.detailTicket || selected!==state.run) return;
    if (force || JSON.stringify(state.detail)!==JSON.stringify(d)) {
      state.detail=d; renderDetail();
    }
    await loadTrace(force);
  } catch (error) {
    const job=state.jobs.find(j=>j.run===selected);
    if (job && job.exit!==null) $('run-caption').textContent='No recording was written for this run. See process output above.';
    else if (force && !job) toast(error.message);
  }
}
function chooseCase(caseId,role) {
  stopPlay();
  state.caseId=caseId; state.role=role; state.index=0; state.trace=null;
  renderDetail();renderTrace();loadTrace(true);
}
function selectRun(id) {
  stopPlay();
  state.run=id;state.caseId=null;state.role=null;state.index=0;state.detail=null;state.trace=null;
  state.traceTicket++;
  renderRuns();renderDetail();persist();loadDetail(true);
}
async function refresh() {
  try {
    const data=await api('/api/state');
    const changed=JSON.stringify(data.runs)!==JSON.stringify(state.archive);
    state.archive=data.runs;
    state.manifestReady=data.manifest_ready;
    const jobsChanged=JSON.stringify(data.jobs)!==JSON.stringify(state.jobs);
    if (jobsChanged || $('connection').dataset.ready!=='true') {
      state.jobs=data.jobs; renderJob(); $('connection').dataset.ready='true';
    }
    updateStart();
    if (!state.run) state.run=state.archive[0]?.id;
    if (changed || jobsChanged) renderRuns();
    const pending=state.jobs.some(j=>j.run===state.run && j.exit===null);
    if (state.run && (pending || !state.detail || !state.detail.complete)) await loadDetail();
    else if (!state.run) renderDetail();
  } catch (error) {
    $('run-caption').textContent='Unable to read the local console.';
  } finally {
    setTimeout(refresh,state.running?1500:5000);
  }
}

function renderLibrary() {
  options($('library-profile'),state.profiles.map(p=>[p.id,p.name]));
  const p=state.profiles.find(x=>x.id===$('library-profile').value);
  const keyword=$('library-search').value.trim().toLowerCase();
  const filtered=p?.cases.filter(c=>[c.id,c.app_group,c.goal].join(' ').toLowerCase().includes(keyword)) || [];
  $('library-grid').replaceChildren(...(filtered.length ? filtered.map(c=>{
    const card=node('article',null,'panel case-card');
    card.append(node('div',c.app_group+' · '+c.slots+' calls','label accent-label'),node('h3',caseName(c.id)),
                node('p',c.goal),button('Use in a run',()=>openLauncher(p.id,c.id)));
    return card;
  }) : [node('p','No checkpoints match your filters.')]));
}
function openPreview(title,text) {
  $('artifact-title').textContent=title;
  $('artifact-preview').textContent=text;
  if (!$('artifact-dialog').open) $('artifact-dialog').showModal();
}
function renderArtifacts() {
  const artifacts=state.detail?.artifacts || [];
  $('artifact-caption').textContent=state.run?'Files for '+state.run:'Reports and analyses for the selected recording.';
  $('artifact-list').replaceChildren(...(artifacts.length?artifacts.map(a=>{
    const row=node('div',null,'artifact-item');
    const name=node('div'); name.append(node('strong',a.label),node('small',a.name+' · '+fmt(a.bytes)+' bytes','mono'));
    const url=endpoint('/api/artifact',{run:state.run,path:a.path});
    const preview=button('Preview',async()=>{
      try {
        const response=await fetch(url);
        if(!response.ok) throw new Error('Artifact unavailable');
        openPreview(a.label,await response.text());
      } catch (error) {toast(error.message);}
    });
    const download=node('a','Download','button');
    download.href=url;download.download=a.name;
    const controls=node('div',null,'bar-right');controls.append(preview,download);
    row.append(name,controls);return row;
  }):[node('p','No artifacts for the selected run.','empty')]));
}

function currentProfile() {return state.profiles.find(p=>p.id===$('form-profile').value);}
function selection() {
  const value=$('form-slots').value;
  return {
    profile:$('form-profile').value, serial:$('form-device').value, slots:value?Number(value):null,
    call_limit:$('form-limit').valueAsNumber, seed:$('form-seed').valueAsNumber, timeout:$('form-timeout').valueAsNumber,
    guard:$('form-guard').checked, progress:$('form-progress').checked,
    cases:[...$('form-cases').querySelectorAll('input:checked')].map(input=>input.value),
  };
}
function clearPlan() {
  state.checked='';
  $('plan-preview').hidden=true;$('confirm-row').hidden=true;$('form-confirm').checked=false;
  $('form-error').hidden=true;updateStart();
}
function updateStart() {
  $('start').disabled=state.running || !state.manifestReady || !state.checked || !$('form-device').value || !$('form-confirm').checked;
}
function formError(message) { $('form-error').textContent=message;$('form-error').hidden=false; }
function syncForm(caseId) {
  const p=currentProfile();
  if (!p) return;
  const focused=p.cases.find(c=>c.id===caseId);
  $('form-seed').value=p.seed;
  $('form-slots').value='';
  $('form-slots').disabled=p.cases.some(c=>c.base_checkpoint);
  $('form-guard').checked=p.guard;
  $('form-progress').checked=p.progress;
  $('form-cases').replaceChildren(...p.cases.map(c=>{
    const line=node('label',null,'check');
    const input=node('input');input.type='checkbox';input.value=c.id;
    input.checked=!focused || c.id===caseId || (focused.base_checkpoint && focused.base_checkpoint===c.base_checkpoint);
    input.dataset.base=c.base_checkpoint || '';
    line.append(input,node('span',caseName(c.id)),node('small',c.slots+' calls'));
    return line;
  }));
  const selected=new Set(selection().cases);
  $('select-all').textContent=selected.size===p.cases.length?'Clear selection':'Select all';
  $('form-limit').value=p.cases.filter(c=>selected.has(c.id)).reduce((sum,c)=>sum+c.slots*(p.guard?4:3),0);
  clearPlan();
}
async function openLauncher(profile, caseId) {
  options($('form-profile'),state.profiles.map(p=>[p.id,p.name]),profile);
  syncForm(caseId);
  options($('form-device'),[['','Checking ADB devices…']],'');
  if (!$('run-dialog').open) $('run-dialog').showModal();
  try {
    const devices=await api('/api/devices');
    options($('form-device'),devices.length ? [['','Select device'],...devices.map(x=>[x,x])] : [['','No emulator connected']],devices.length===1?devices[0]:'');
    if (!devices.length) formError('Connect a dedicated BudgetMASPilot emulator to start. You can still check the plan.');
    else if (!state.manifestReady) formError('Freeze the application manifest before starting a run.');
  } catch (error) {formError(error.message);}
}
async function previewPlan() {
  if (!$('run-form').reportValidity()) return;
  clearPlan();
  const payload=selection();
  const key=JSON.stringify(payload);
  try {
    const plan=await api('/api/plan',payload);
    if (key!==JSON.stringify(selection())) return;
    state.checked=key;
    $('plan-preview').textContent=plan.totals.model_calls+' model calls max · '+plan.totals.device_steps+
      ' device operations max · '+plan.model;
    $('plan-preview').hidden=false;$('confirm-row').hidden=false;
    if (!payload.serial) formError('Plan is valid. Connect and select the emulator to enable Start run.');
    else if (!state.manifestReady) formError('Plan is valid. Freeze the app manifest before starting.');
    updateStart();
  } catch (error) {formError(error.message);}
}

for (const el of document.querySelectorAll('.nav')) el.addEventListener('click',()=>switchView(el.dataset.view));
for (const id of ['launch','empty-launch','library-launch']) $(id).addEventListener('click',()=>openLauncher());
for (const id of ['run-select','artifact-run']) $(id).addEventListener('change',event=>selectRun(event.target.value));
$('case-select').addEventListener('change',event=>chooseCase(event.target.value,state.role));
$('role-select').addEventListener('change',event=>chooseCase(state.caseId,event.target.value));
$('previous').addEventListener('click',()=>{stopPlay();setFrame(state.index-1);});
$('next').addEventListener('click',()=>{stopPlay();setFrame(state.index+1);});
$('play').addEventListener('click',play);
$('library-profile').addEventListener('change',renderLibrary);
$('library-search').addEventListener('input',renderLibrary);
$('form-profile').addEventListener('change',()=>syncForm());
$('run-form').addEventListener('input',event=>{
  if (event.target.id==='form-confirm') {updateStart();return;}
  if (event.target.dataset.base) {
    for (const input of $('form-cases').querySelectorAll('input')) {
      if (input.dataset.base===event.target.dataset.base) input.checked=event.target.checked;
    }
  }
  clearPlan();
});
$('select-all').addEventListener('click',()=>{
  const inputs=[...$('form-cases').querySelectorAll('input')];
  const wanted=!inputs.every(input=>input.checked);
  inputs.forEach(input=>input.checked=wanted);
  $('select-all').textContent=wanted?'Clear selection':'Select all'; clearPlan();
});
$('preview').addEventListener('click',previewPlan);
$('run-form').addEventListener('submit',async event=>{
  event.preventDefault();const payload=selection();
  if (state.checked!==JSON.stringify(payload) || !$('form-confirm').checked) return;
  $('start').disabled=true;
  try {
    const job=await api('/api/start',{...payload,confirmed:true});
    $('run-dialog').close();switchView('explorer');
    state.jobs.unshift({id:job.job,run:job.run,exit:null,log:''});
    renderJob();selectRun(job.run);toast('Run submitted to '+payload.serial+'.');
  } catch (error) {formError(error.message);updateStart();}
});
$('stop').addEventListener('click',async()=>{
  const job=state.jobs.find(j=>j.exit===null);
  if (!job || !window.confirm('Stop the active run? Recorded frames and budget reservations will remain.')) return;
  $('stop').disabled=true;
  try {await api('/api/stop',{job:job.id});toast('Stop signal sent.');}
  catch(error) {toast(error.message);$('stop').disabled=false;}
});
for (const el of document.querySelectorAll('[data-close]')) el.addEventListener('click',()=>$(el.dataset.close).close());
document.addEventListener('keydown',event=>{
  if (state.view!=='explorer' || $('run-dialog').open || $('artifact-dialog').open ||
      ['INPUT','BUTTON','SELECT','TEXTAREA','SUMMARY'].includes(event.target.tagName)) return;
  if (event.key==='ArrowRight' || event.key==='ArrowLeft') {
    event.preventDefault();stopPlay();setFrame(state.index+(event.key==='ArrowRight'?1:-1));
  }
});
api('/api/profiles').then(profiles=>{state.profiles=profiles;renderLibrary();}).catch(e=>toast(e.message));
refresh();
