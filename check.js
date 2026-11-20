
const $ = id => document.getElementById(id);
let bots = [];
let toastTimer;
function toast(msg){$('toast').textContent=msg;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),3200)}
async function api(url, options={}) {
 const res = await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
 let data={};try{data=await res.json()}catch{}
 if(!res.ok){if(res.status===401){$('loginScreen').classList.remove('hidden')}throw new Error(data.error||`Request failed (${res.status})`)}
 return data;
}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function openModal(){$('formError').textContent='';$('hostBotForm').reset();$('requireVerification').checked=true;$('botModal').classList.remove('hidden')}
function closeModal(){$('botModal').classList.add('hidden')}
$('addBtn').addEventListener('click',openModal);
$('closeModal').addEventListener('click',closeModal);
$('cancelModal').addEventListener('click',closeModal);
$('botModal').addEventListener('click',e=>{if(e.target===$('botModal'))closeModal()});
$('logoutBtn').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}finally{$('loginScreen').classList.remove('hidden')}});
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('loginPassword').value})});$('loginPassword').value='';$('loginScreen').classList.add('hidden');await loadDashboard()}catch(err){$('loginError').textContent=err.message}});
$('hostBotForm').addEventListener('submit',async e=>{
 e.preventDefault();$('formError').textContent='';$('submitBot').disabled=true;$('submitBot').textContent='Connecting...';
 const body={name:$('botName').value.trim(),token:$('botToken').value.trim(),channel_link:$('channelLink').value.trim(),channel_ref:$('channelRef').value.trim(),youtube_link:$('youtubeLink').value.trim(),verify_channel:$('requireVerification').checked};
 try{await api('/api/bots',{method:'POST',body:JSON.stringify(body)});closeModal();toast('Bot configuration saved. Select Start to run it.');await loadDashboard()}
 catch(err){$('formError').textContent=err.message}
 finally{$('submitBot').disabled=false;$('submitBot').textContent='Host Bot Engine'}
});
$('searchInput').addEventListener('input',renderBots);
function renderBars(targetId, rows, labelKey, emptyText){
 const target=$(targetId);
 if(!rows||!rows.length){target.textContent=emptyText;return}
 const max=Math.max(...rows.map(x=>x.count),1);
 target.innerHTML=rows.map(x=>`<div class="flex items-center gap-2"><span class="w-24 shrink-0 truncate">${escapeHtml(x[labelKey])}</span><div class="flex-1 h-2 rounded-full bg-slate-800 overflow-hidden"><div class="h-full rounded-full bg-cyan-500" style="width:${Math.max(3,x.count/max*100)}%"></div></div><span class="w-8 text-right text-white">${x.count}</span></div>`).join('');
}
async function loadAnalytics(){
 try{
  const days=$('analyticsDays').value||'7';
  const a=await api(`/api/analytics?days=${encodeURIComponent(days)}`);
  $('anTotal').textContent=a.total_events;
  $('anSuccess').textContent=a.successful_events;
  $('anFailed').textContent=a.failed_events;
  $('anDuration').textContent=a.average_duration_ms+' ms';
  renderBars('analyticsDaily',a.daily,'date','No activity recorded for this period.');
  renderBars('analyticsCommands',a.commands,'name','No commands recorded for this period.');
  const botBox=$('analyticsBots');
  botBox.innerHTML=a.bots.length?a.bots.map(b=>`<span class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2">${escapeHtml(b.name)}: <strong class="text-white">${b.count}</strong></span>`).join(''):'No bot activity recorded.';
  $('analyticsNote').textContent=`Success rate: ${a.success_rate}% • Reporting window: ${a.period_days} days. Event records contain command category and timing only.`;
 }catch(err){$('analyticsNote').textContent='Analytics unavailable: '+err.message}
}
$('analyticsDays').addEventListener('change',loadAnalytics);

async function loadDashboard(){
 try{
  const d=await api('/api/dashboard');bots=d.bots||[];
  $('statTotalConfigs').textContent=d.total_configs;$('statRunningBots').textContent=d.running_bots;$('statRunningTotal').textContent=d.total_configs;$('statTotalLookups').textContent=d.total_lookups;$('statSuccessRate').textContent=d.api_success_rate+'%';$('botCountBadge').textContent=bots.length;
  $('serverStatus').textContent='Backend Online';$('healthDot').className='w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
  renderBots();
  await loadAnalytics();
 }catch(err){$('serverStatus').textContent='Backend unavailable';$('healthDot').className='w-2 h-2 rounded-full bg-red-500';$('botListContainer').innerHTML=`<div class="glass-panel rounded-xl p-5 text-sm text-red-300">${escapeHtml(err.message)}. Check backend deployment.</div>`}
}
function renderBots(){
 const q=$('searchInput').value.trim().toLowerCase();
 const shown=bots.filter(b=>(b.name+' '+b.username+' '+b.channel_ref).toLowerCase().includes(q));
 $('botCountBadge').textContent=bots.length;
 if(!shown.length){$('botListContainer').innerHTML=`<div class="glass-panel rounded-2xl p-12 text-center flex flex-col items-center"><div class="w-16 h-16 rounded-full bg-slate-900 border border-slate-800 flex items-center justify-center text-slate-600 text-2xl mb-3"><i class="fas fa-robot"></i></div><p class="text-xs text-slate-500">${bots.length?'No matching bots found.':'No configured bots yet. Click Host Another Bot to add one.'}</p></div>`;return}
 $('botListContainer').innerHTML=shown.map(b=>`<div class="glass-panel rounded-2xl p-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4"><div class="flex items-center gap-3 min-w-0"><div class="w-10 h-10 rounded-xl bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center text-cyan-400 text-lg shrink-0"><i class="fas fa-robot"></i></div><div class="min-w-0"><h3 class="text-white font-semibold text-sm break-words">${escapeHtml(b.name)}</h3><p class="text-[11px] text-slate-400 font-mono break-all">@${escapeHtml(b.username==='Not checked'?'not-checked':b.username)} • <span class="${b.active?'text-emerald-400':'text-slate-500'}">${b.active?'Running':'Stopped'}</span></p><p class="text-[10px] text-slate-500 mt-1">${b.verify_channel?'Channel verification ON':'Channel verification OFF'}</p></div></div><div class="flex items-center gap-2 w-full sm:w-auto justify-end"><button data-action="${b.active?'stop':'start'}" data-id="${b.id}" class="px-3 py-2 rounded-lg bg-slate-800 text-xs text-slate-200 hover:bg-slate-700">${b.active?'Stop':'Start'}</button><button data-action="delete" data-id="${b.id}" class="px-3 py-2 rounded-lg bg-red-950/40 border border-red-500/20 text-xs text-red-400"><i class="fas fa-trash"></i> Delete</button></div></div>`).join('');
}
$('botListContainer').addEventListener('click',async e=>{
 const btn=e.target.closest('button[data-action]');if(!btn)return;
 const id=btn.dataset.id,action=btn.dataset.action;btn.disabled=true;
 try{
  if(action==='delete'){if(!confirm('Delete this bot configuration?'))return;await api(`/api/bots/${id}`,{method:'DELETE'})}
  else await api(`/api/bots/${id}/${action}`,{method:'POST',body:'{}'});
  toast(action==='delete'?'Bot configuration deleted.':`Bot ${action==='start'?'started':'stopped'}.`);await loadDashboard();
 }catch(err){toast(err.message)}finally{btn.disabled=false}
});
(async()=>{try{const me=await api('/api/me');if(me.authenticated){$('loginScreen').classList.add('hidden');await loadDashboard()}else{$('loginScreen').classList.remove('hidden')}}catch{$('loginScreen').classList.remove('hidden');$('loginError').textContent='Backend connection failed.'}})();
