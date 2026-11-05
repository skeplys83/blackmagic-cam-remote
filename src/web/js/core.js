// Shared helpers: DOM shortcut, toasts, REST calls to the camera (via /api), small UI utilities.

const $ = (id) => document.getElementById(id);
let dashboardOn = false; // true while the dashboard runs (set by setup.js)
let state = { recording: false, online: false };
const dragging = new Set();

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), 2500);
}

async function api(path, method = 'GET', body) {
  const opt = { method };
  if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch('/api/' + path, opt);
  if (!r.ok) {
    const why = { 400: 'rejected by camera', 403: 'not allowed right now (try Manual exposure / focus first)', 502: 'phone unreachable' }[r.status] || ('error ' + r.status);
    throw new Error(why);
  }
  const txt = await r.text();
  return txt ? JSON.parse(txt) : null;
}

async function send(path, method, body, label) {
  const prop = ('/' + path).replace('/monitoring/Device/', '/monitoring/LCD/'), sent = Date.now();
  // Re-read the value only if the camera hasn't pushed it by then: a late read could otherwise overwrite
  // the newer pushed value with the old one (sliders "snapping back").
  try { await api(path, method, body); setTimeout(() => { if (!((lastEvent[prop] || 0) > sent)) refresh([prop]); }, 700); }
  catch (e) { toast((label || path) + ': ' + e.message); }
}

function setConn(ok) {
  state.online = ok;
  $('conn').className = 'dot ' + (ok ? 'ok' : 'bad');
  $('rec').disabled = !ok;
}

function setSeg(id, v) {
  for (const b of $(id).querySelectorAll('button')) b.classList.toggle('active', b.dataset.v === v);
}

function fmtMins(s) {
  if (s == null) return '–';
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function fillSelect(sel, values, fmt) {
  const key = values.join(',');
  if (sel.dataset.key === key) return;
  sel.dataset.key = key;
  sel.innerHTML = values.map(v => `<option value="${v}">${fmt(v)}</option>`).join('');
}

function setSlider(id, v, out, text) {
  if (!dragging.has(id)) $(id).value = v;
  $(out).textContent = text;
}
