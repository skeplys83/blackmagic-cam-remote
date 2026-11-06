// Setup screen (checks, QR/code pairing, connect, launch) and starting the dashboard once everything is ready.

// ---------- setup ----------

// The dashboard only runs while every check passes. The helper pushes the setup state as it changes
// (the "setup" events on /events): phone connection and "app in front" within a fraction of a second, the rest every few seconds.
// If anything fails, the dashboard stops (live view, events, polling) and the setup screen shows.
let lastSetup = null; // dashboardOn lives in core.js (loaded first)

function renderSetup(st) {
  lastSetup = st;
  for (const [key, checks] of Object.entries(st.groups)) {
    const sec = $('step-' + key);
    const good = checks.every(c => c.ok || c.optional);
    sec.classList.toggle('ok', good); sec.classList.toggle('bad', !good);
    sec.querySelector('.badge').textContent = good ? 'ready' : 'needs attention';
    sec.querySelector('.checks').innerHTML = checks.map(c => `<div class="check ${c.ok ? 'ok' : c.optional ? 'opt' : 'bad'}">
        <span class="ic">${c.ok ? '✓' : c.optional ? '!' : '✕'}</span><span>${esc(c.title)}</span>
        ${c.detail ? `<span class="d">${esc(c.detail)}</span>` : ''}${!c.ok && c.fix ? `<span class="fix">${esc(c.fix)}</span>` : ''}</div>`).join('');
  }
  const dev = (st.devices || []).find(d => d.state === 'device');
  if (dev?.model) $('phoneName').textContent = dev.model;
  const phoneOk = st.groups.phone.every(c => c.ok);
  $('pairBox').hidden = phoneOk;
  $('launchRow').hidden = !st.groups.app.some(c => c.id === 'app-front' && !c.ok);
}

function applySetup(st) {
  renderSetup(st);
  renderLive(st.live);
  if (st.ready) { if (!dashboardOn) startDashboard(); }
  else stopDashboard();
}

// Live status pills in the header: phone connection and Blackmagic Camera in front.
function renderLive(live) {
  const pill = (id, ok, text) => { $(id).textContent = text; $(id).className = 'pill ' + (ok ? 'ok' : 'bad'); };
  if (!live) { pill('livePhone', false, 'Helper offline'); pill('liveApp', false, 'App'); return; }
  pill('livePhone', live.connected, live.connected ? (live.wireless ? 'Wi-Fi' : 'USB') : 'Offline');
  pill('liveApp', live.inFront, live.inFront ? 'App open' : 'App closed');
  $('livePhone').title = live.connected ? `Phone connected over ${live.wireless ? 'wireless debugging' : 'USB'}` : 'Phone not connected';
  $('liveApp').title = live.inFront ? 'Blackmagic Camera is open in front' : 'Blackmagic Camera is not in front';
}

const HELPER_DOWN = { ready: false, live: null, groups: { computer: [{ ok: false, title: 'Helper running', detail: "can't reach the helper",
  fix: 'Start it again (docker compose up -d).' }], phone: [], app: [] } };

// The page's single live stream: setup state + camera properties (browsers allow only ~6 connections per server).
function watchSetup() {
  const es = new EventSource('/events');
  es.addEventListener('setup', (e) => applySetup(JSON.parse(e.data)));
  es.addEventListener('snapshot', (e) => onCameraSnapshot(JSON.parse(e.data)));
  es.onmessage = (e) => onCameraChange(JSON.parse(e.data));
  es.onerror = () => { P['@connected'] = false; applySetup(HELPER_DOWN); }; // EventSource reconnects by itself
}

async function setupAction(action, body, btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/setup/' + action, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error);
    toast(j.message || 'Done');
  } catch (e) { toast(e.message); }
  btn.disabled = false;
}
let qrTimer = null;
async function pollQr() {
  try {
    const r = await fetch('/setup/qr-status', { method: 'POST' }); const q = await r.json();
    $('qrStatus').textContent = q.message || '';
    if (q.knownIp) prefillAddresses(q.knownIp);
    if (['done', 'failed', 'timeout'].includes(q.state)) {
      clearInterval(qrTimer); qrTimer = null; $('qrBtn').textContent = 'Show a new QR code';
      if (q.state !== 'done') $('qrBox').hidden = true; else setTimeout(() => { $('qrBox').hidden = true; }, 3000);
    }
  } catch {}
}
$('qrBtn').onclick = async () => {
  $('qrBtn').disabled = true;
  try {
    const r = await fetch('/setup/qr', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ip: $('qrIp').value.trim() === ADDR_PREFIX ? '' : $('qrIp').value.trim() }) }); // the bare prefix = not entered
    const j = await r.json(); if (!r.ok) throw new Error(j.error);
    if (j.ip) $('qrIp').value = j.ip;
    $('qrBox').innerHTML = j.svg; $('qrBox').hidden = false; $('qrStatus').textContent = 'Scan the code with the phone';
    $('qrBtn').textContent = 'Show a new QR code';
    if (!qrTimer) qrTimer = setInterval(pollQr, 1000);
  } catch (e) { $('qrStatus').textContent = e.message; }
  $('qrBtn').disabled = false;
};
$('pairBtn').onclick = (e) => setupAction('pair', { address: $('pairAddr').value, code: $('pairCode').value }, e.target);
$('connBtn').onclick = (e) => setupAction('connect', { address: $('connAddr').value }, e.target);
$('launchBtn').onclick = (e) => setupAction('launch', {}, e.target);

function startDashboard() {
  dashboardOn = true;
  document.body.classList.remove('setup-mode');
  $('setup').hidden = true; $('dash').hidden = false;
  fitLiveTile();
  if (!useWebCodecs && !MSE) stageMsg("This browser can't show the live view. Use a current Chrome, Edge, Firefox or Safari.");
  else runMirror();
  startEvents();
  loadClips();
  loadAutomation(); pollPhone();
}

function stopDashboard() {
  $('dash').hidden = true; $('setup').hidden = false;
  document.body.classList.add('setup-mode');
  if (!dashboardOn) return;
  dashboardOn = false;
  stopMirror();
  stopEvents();
}

setInterval(() => { if (dashboardOn) pollPhone(); }, 5000);

// Prefill the address fields: the phone's IP if known from an earlier pairing (the port changes, so the pair/connect
// fields end in ":"), otherwise the usual home-network prefix. Only fields the user hasn't typed into are touched.
const ADDR_PREFIX = '192.168.';
function prefillAddresses(ip) {
  for (const [id, v] of [['qrIp', ip || ADDR_PREFIX], ['pairAddr', ip ? ip + ':' : ADDR_PREFIX], ['connAddr', ip ? ip + ':' : ADDR_PREFIX]]) {
    const el = $(id);
    if (!el.dataset.typed && document.activeElement !== el) el.value = v;
  }
}
for (const id of ['qrIp', 'pairAddr', 'connAddr']) {
  $(id).addEventListener('input', (e) => { e.target.dataset.typed = '1'; });
  $(id).addEventListener('focus', (e) => { const el = e.target; setTimeout(() => el.setSelectionRange(el.value.length, el.value.length)); });
}
prefillAddresses();
fetch('/setup/qr-status', { method: 'POST' }).then(r => r.json()).then(q => { if (q.knownIp) prefillAddresses(q.knownIp); }).catch(() => {});
watchSetup();
