// Setup screen (checks, QR/code pairing, connect, launch) and starting the dashboard once everything is ready.

// ---------- setup ----------

let booted = false, lastSetup = null;

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
  const failing = Object.values(st.groups).flat().filter(c => !c.ok && !c.optional).map(c => c.title);
  $('setupBannerText').textContent = 'Not ready: ' + failing.join(', ');
}

async function checkSetup() {
  let st;
  try { st = await (await fetch('/setup')).json(); }
  catch { st = { ready: false, groups: { computer: [{ ok: false, title: 'Helper running', detail: "can't reach the helper", fix: 'Start it again (docker compose up -d).' }], phone: [], app: [] } }; }
  renderSetup(st);
  if (st.ready && !booted) boot();
  if (booted) { $('setupBanner').hidden = st.ready; if (st.ready) $('setup').hidden = true; }
  else $('setup').hidden = false;
  setTimeout(checkSetup, booted && st.ready ? 10000 : 2000);
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
    if (['done', 'failed', 'timeout'].includes(q.state)) {
      clearInterval(qrTimer); qrTimer = null; $('qrBtn').textContent = 'Show a new QR code';
      if (q.state !== 'done') $('qrBox').hidden = true; else setTimeout(() => { $('qrBox').hidden = true; }, 3000);
    }
  } catch {}
}
$('qrBtn').onclick = async () => {
  $('qrBtn').disabled = true;
  try {
    const r = await fetch('/setup/qr', { method: 'POST' }); const j = await r.json(); if (!r.ok) throw new Error(j.error);
    $('qrBox').innerHTML = j.svg; $('qrBox').hidden = false; $('qrStatus').textContent = 'Scan the code with the phone';
    $('qrBtn').textContent = 'Show a new QR code';
    if (!qrTimer) qrTimer = setInterval(pollQr, 1000);
  } catch (e) { $('qrStatus').textContent = e.message; }
  $('qrBtn').disabled = false;
};
$('pairBtn').onclick = (e) => setupAction('pair', { address: $('pairAddr').value, code: $('pairCode').value }, e.target);
$('connBtn').onclick = (e) => setupAction('connect', { address: $('connAddr').value }, e.target);
$('launchBtn').onclick = (e) => setupAction('launch', {}, e.target);
$('setupBannerBtn').onclick = () => { $('setup').hidden = !$('setup').hidden; };

// Start the dashboard only once everything is set up.
function boot() {
  booted = true;
  $('setup').hidden = true; $('dash').hidden = false;
  if (!useWebCodecs && !MSE) stageMsg("This browser can't show the live view. Use a current Chrome, Edge, Firefox or Safari.");
  else runMirror();
  startEvents();
  loadClips();
  loadAutomation(); pollPhone(); setInterval(pollPhone, 5000);
}

checkSetup();
