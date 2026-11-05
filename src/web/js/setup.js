// Setup screen (checks, QR/code pairing, connect, launch) and starting the dashboard once everything is ready.

// ---------- setup ----------

// The dashboard only runs while every check passes. If one fails, it stops (live view, events, polling)
// and the setup screen shows until everything is ready again.
let dashboardOn = false, notReadyCount = 0, lastSetup = null;

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
  $('launchRow').hidden = !st.groups.app.some(c => c.title === 'Blackmagic Camera open' && !c.ok);
}

async function checkSetup() {
  let st;
  try { st = await (await fetch('/setup')).json(); }
  catch { st = { ready: false, groups: { computer: [{ ok: false, title: 'Helper running', detail: "can't reach the helper", fix: 'Start it again (docker compose up -d).' }], phone: [], app: [] } }; }
  renderSetup(st);
  if (st.ready) {
    notReadyCount = 0;
    if (!dashboardOn) startDashboard();
  } else if (++notReadyCount >= (dashboardOn ? 2 : 1)) { // while in use, one failed check may be a hiccup; two in a row stop it
    stopDashboard();
  }
  setTimeout(checkSetup, dashboardOn ? 5000 : 2000);
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
    if (q.knownIp && !$('qrIp').value) $('qrIp').value = q.knownIp;
    if (['done', 'failed', 'timeout'].includes(q.state)) {
      clearInterval(qrTimer); qrTimer = null; $('qrBtn').textContent = 'Show a new QR code';
      if (q.state !== 'done') $('qrBox').hidden = true; else setTimeout(() => { $('qrBox').hidden = true; }, 3000);
    }
  } catch {}
}
$('qrBtn').onclick = async () => {
  $('qrBtn').disabled = true;
  try {
    const r = await fetch('/setup/qr', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ip: $('qrIp').value.trim() }) });
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
  $('setup').hidden = true; $('dash').hidden = false;
  if (!useWebCodecs && !MSE) stageMsg("This browser can't show the live view. Use a current Chrome, Edge, Firefox or Safari.");
  else runMirror();
  startEvents();
  loadClips();
  loadAutomation(); pollPhone();
}

function stopDashboard() {
  $('dash').hidden = true; $('setup').hidden = false;
  if (!dashboardOn) return;
  dashboardOn = false;
  stopMirror();
  stopEvents();
}

setInterval(() => { if (dashboardOn) pollPhone(); }, 5000);

// Prefill the phone IP if it's known from an earlier pairing.
fetch('/setup/qr-status', { method: 'POST' }).then(r => r.json()).then(q => { if (q.knownIp) $('qrIp').value = q.knownIp; }).catch(() => {});
checkSetup();
