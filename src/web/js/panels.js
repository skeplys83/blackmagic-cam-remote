// Panels below the live view: recording format, monitoring overlays, presets, phone health and automation.

// ---------- recording format ----------

let codecList = []; // /system/supportedCodecFormats isn't pushed as an event; read once
api('system/supportedCodecFormats').then(v => { codecList = (v || []).map(c => c.codec); scheduleRender(); }).catch(() => {});
const codecLabel = (c) => { const [k, q] = c.split(':'); return `${{ H265: 'H.265', H264: 'H.264', AV1: 'AV1' }[k] || k} ${q}`; };
const sameRes = (a, b) => a && b && a.width === b.width && a.height === b.height;

function formats() { return P['/system/supportedFormats']?.supportedFormats || []; }

function renderFormat() {
  const f = P['/system/format'], list = formats(); if (!f || !list.length) return;
  const idx = list.findIndex(x => sameRes(x.recordResolution, f.recordResolution) && sameRes(x.sensorResolution, f.sensorResolution));
  const res = $('fmtRes');
  const key = list.map(x => x.recordResolution.width + 'x' + x.recordResolution.height).join();
  if (res.dataset.key !== key) {
    res.dataset.key = key;
    res.innerHTML = list.map((x, i) => `<option value="${i}">${x.resolutionDescriptor.group} ${x.resolutionDescriptor.description} · ${x.recordResolution.width}×${x.recordResolution.height}</option>`).join('');
  }
  if (document.activeElement !== res) res.value = idx;
  const cur = list[idx] || list[0];
  const fps = $('fmtFps'); fps.innerHTML = cur.frameRates.map(r => `<option>${r}</option>`).join(''); fps.value = f.frameRate;
  const codecs = cur.codecs.length ? cur.codecs : codecList;
  const cs = $('fmtCodec'); cs.innerHTML = (codecs.length ? codecs : [f.codec]).map(c => `<option value="${c}">${codecLabel(c)}</option>`).join(''); cs.value = f.codec;
  $('fmtOff').checked = !!f.offSpeedEnabled;
  const off = $('fmtOffFps'); off.min = f.minOffSpeedFrameRate; off.max = f.maxOffSpeedFrameRate;
  if (document.activeElement !== off) off.value = f.offSpeedFrameRate;
  off.disabled = !f.offSpeedEnabled;
  $('fmtOffRange').textContent = `${f.minOffSpeedFrameRate}–${f.maxOffSpeedFrameRate} fps`;
  const a = P['/system/audioCodec']; $('fmtAudio').textContent = a ? a.codec : '–';
  for (const id of ['fmtRes', 'fmtFps', 'fmtCodec', 'fmtOff']) $(id).disabled = state.recording;
  if (state.recording) off.disabled = true;
  $('fmtNote').hidden = !state.recording;
}

function putFormat(changes) {
  const f = P['/system/format']; if (!f) return;
  const body = { codec: f.codec, frameRate: f.frameRate, maxOffSpeedFrameRate: f.maxOffSpeedFrameRate, minOffSpeedFrameRate: f.minOffSpeedFrameRate,
    offSpeedEnabled: f.offSpeedEnabled, offSpeedFrameRate: f.offSpeedFrameRate, recordResolution: f.recordResolution, sensorResolution: f.sensorResolution, ...changes };
  send('system/format', 'PUT', body, 'Recording format').then(() => refresh(['/system', '/system/format']));
}
// Resolution changes go through the helper (the app's API reverts them otherwise; see recformat.py).
$('fmtRes').onchange = async (e) => {
  const x = formats()[+e.target.value], f = P['/system/format']; if (!x || !f) return;
  const sel = e.target; sel.disabled = true; toast('Changing resolution…');
  try {
    const r = await fetch('/format/resolution', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ width: x.recordResolution.width, height: x.recordResolution.height, openGate: /open gate/i.test(x.resolutionDescriptor.description) }) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error);
    await refresh(['/system', '/system/format', '/system/videoFormat']);
    const now = P['/system/format'];
    if (now && !x.frameRates.includes(now.frameRate)) putFormat({ frameRate: x.frameRates[0] }); // not available at this size
    toast('Resolution: ' + j.name);
  } catch (err) { toast('Resolution: ' + err.message); refresh(['/system/format']); }
  sel.disabled = false;
};
$('fmtFps').onchange = (e) => putFormat({ frameRate: e.target.value });
$('fmtCodec').onchange = (e) => putFormat({ codec: e.target.value });
$('fmtOff').onchange = (e) => putFormat({ offSpeedEnabled: e.target.checked });
$('fmtOffFps').onchange = (e) => {
  const f = P['/system/format']; const v = Math.round(+e.target.value);
  if (f && v >= f.minOffSpeedFrameRate && v <= f.maxOffSpeedFrameRate) putFormat({ offSpeedFrameRate: v });
  else toast(`Off-speed must be ${f?.minOffSpeedFrameRate}–${f?.maxOffSpeedFrameRate} fps`);
};

// ---------- monitoring overlays ----------

const MON = [['focusAssist', 'Focus assist'], ['zebra', 'Zebra'], ['falseColor', 'False color'], ['frameGuide', 'Frame guide'],
  ['frameGrids', 'Grids'], ['safeArea', 'Safe area'], ['displayLUT', 'Display LUT'], ['cleanFeed', 'Clean feed']];
$('monTable').innerHTML = MON.map(([k, label]) => `<tr><td>${label}</td>` +
  ['LCD', 'HDMI'].map(d => d === 'LCD' && k === 'cleanFeed' ? '<td>–</td>' : `<td><button class="btn small tgl" data-d="${d}" data-k="${k}">–</button></td>`).join('') + '</tr>').join('');

function renderMonitoring() {
  for (const b of $('monTable').querySelectorAll('button')) {
    const v = P[`/monitoring/${b.dataset.d}/${b.dataset.k}`];
    b.textContent = v ? (v.enabled ? 'On' : 'Off') : '–';
    b.classList.toggle('active', !!v?.enabled);
  }
  const fa = P['/monitoring/focusAssist'];
  if (fa) {
    if (document.activeElement !== $('faMode')) $('faMode').value = String(fa.mode).replace(/\s/g, '');
    if (document.activeElement !== $('faColor')) $('faColor').value = fa.color;
    setSlider('faInt', fa.intensity, 'faIntOut', fa.intensity + '%');
  }
  const z = P['/monitoring/zebra']; if (z?.highlight && document.activeElement !== $('zebraLevel')) $('zebraLevel').value = z.highlight.level;
  const gr = P['/monitoring/frameGuideRatio'], sel = $('guideRatio');
  if (!sel.options.length) api('monitoring/frameGuideRatio/presets').then(v => { sel.innerHTML = v.presets.map(r => `<option>${r}</option>`).join(''); if (gr) sel.value = gr.ratio; }).catch(() => {});
  if (gr && document.activeElement !== sel) sel.value = gr.ratio;
  const g = P['/monitoring/frameGrids']?.frameGrids || [];
  for (const b of $('grids').querySelectorAll('button')) b.classList.toggle('active', g.includes(b.dataset.v));
  const sp = P['/monitoring/safeAreaPercent']; if (sp) setSlider('safePct', sp.percent, 'safePctOut', sp.percent + '%');
}

$('monTable').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const cur = P[`/monitoring/${b.dataset.d}/${b.dataset.k}`];
  const rest = `monitoring/${b.dataset.d === 'LCD' ? 'Device' : 'HDMI'}/${b.dataset.k}`;
  send(rest, 'PUT', { enabled: !cur?.enabled }, b.closest('tr').firstChild.textContent);
};
function putFocusAssist(changes) {
  const fa = P['/monitoring/focusAssist'] || {};
  send('monitoring/focusAssist', 'PUT', { mode: String(fa.mode || 'Peak').replace(/\s/g, ''), color: fa.color || 'Red', intensity: fa.intensity ?? 80, ...changes }, 'Focus assist');
}
$('faMode').onchange = (e) => putFocusAssist({ mode: e.target.value });
$('faColor').onchange = (e) => putFocusAssist({ color: e.target.value });
slider('faInt', 'faIntOut', v => v + '%', v => putFocusAssist({ intensity: v }));
$('zebraLevel').onchange = (e) => {
  const z = P['/monitoring/zebra'] || {};
  send('monitoring/zebra', 'PUT', { highlight: { level: +e.target.value, enabled: z.highlight?.enabled ?? false }, skinTone: z.skinTone ?? null }, 'Zebra level');
};
$('guideRatio').onchange = (e) => send('monitoring/frameGuideRatio', 'PUT', { ratio: e.target.value }, 'Frame guide');
$('grids').onclick = (e) => {
  const v = e.target.dataset.v; if (!v) return;
  const g = new Set(P['/monitoring/frameGrids']?.frameGrids || []);
  g.has(v) ? g.delete(v) : g.add(v);
  send('monitoring/frameGrids', 'PUT', { frameGrids: [...g] }, 'Grids');
};
slider('safePct', 'safePctOut', v => v + '%', v => send('monitoring/safeAreaPercent', 'PUT', { percent: v }, 'Safe area'));

// ---------- presets ----------

const presetLabel = (n) => n.replace(/\.bmcpreset$/i, '');
let presetConfirm = null;

function renderPresets() {
  const list = (P['/presets']?.presets || []).filter(n => !n.startsWith('bcr-')), active = P['/presets/active']?.preset || ''; // bcr-*: the helper's temporary presets
  const box = $('presetList');
  const key = JSON.stringify([list, active, presetConfirm]);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = list.length ? list.map(n => `<div class="preset${n === active ? ' active' : ''}">
      <span class="pname" title="${esc(n)}">${esc(presetLabel(n))}${n === active ? ' · active' : ''}</span>
      <button class="btn small" data-act="load" data-n="${esc(n)}">Load</button>
      <button class="btn small" data-act="update" data-n="${esc(n)}" title="Overwrite with the current camera settings">Update</button>
      <button class="btn small" data-act="delete" data-n="${esc(n)}">${presetConfirm === n ? 'Confirm delete' : 'Delete'}</button>
    </div>`).join('') : '<div class="empty">No presets yet.</div>';
}

$('presetList').onclick = async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const n = b.dataset.n, path = 'presets/' + encodeURIComponent(n);
  if (b.dataset.act === 'load') { await send('presets/active', 'PUT', { preset: n }, 'Load preset'); refresh(['/presets/active']); }
  if (b.dataset.act === 'update') { await send(path, 'PUT', undefined, 'Update preset'); toast(`Saved current settings to "${presetLabel(n)}"`); }
  if (b.dataset.act === 'delete') {
    if (presetConfirm !== n) { presetConfirm = n; renderPresets(); setTimeout(() => { if (presetConfirm === n) { presetConfirm = null; renderPresets(); } }, 4000); return; }
    presetConfirm = null; await send(path, 'DELETE', undefined, 'Delete preset'); refresh(['/presets', '/presets/active']);
  }
};
$('presetSave').onclick = async () => {
  const name = $('presetName').value.trim().replace(/\.bmcpreset$/i, '');
  if (!name || /[\x00-\x1F"\/:*?<>|\\]/.test(name)) { toast('Use a name without " / : * ? < > | \\'); return; }
  const file = name + '.bmcpreset';
  if ((P['/presets']?.presets || []).includes(file)) { toast(`"${name}" exists; use its Update button to overwrite`); return; }
  await send('presets/' + encodeURIComponent(file), 'PUT', undefined, 'Save preset');
  $('presetName').value = ''; refresh(['/presets', '/presets/active']);
};
$('presetName').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('presetSave').click(); });

// ---------- phone (adb) ----------

async function pollPhone() {
  try {
    const r = await fetch('/phone'); const p = await r.json(); if (!r.ok) throw new Error(p.error);
    const t = p.skinTemperature ?? p.battery.temperature;
    $('phTemp').textContent = t != null ? `${t.toFixed(1)} °C` + (p.battery.temperature != null ? ` · battery ${p.battery.temperature.toFixed(1)} °C` : '') : '–';
    $('phTemp').className = t >= 45 ? 'bad' : t >= 40 ? 'warn' : '';
    $('phThermal').textContent = p.thermal ?? '–';
    $('phThermal').className = ['severe', 'critical', 'emergency', 'shutdown'].includes(p.thermal) ? 'bad' : p.thermal === 'moderate' ? 'warn' : '';
    const w = p.wifi, q = w.rssi == null ? '' : w.rssi >= -55 ? 'excellent' : w.rssi >= -67 ? 'good' : w.rssi >= -75 ? 'fair' : 'weak';
    $('phWifi').textContent = w.rssi != null ? `${w.rssi} dBm (${q})` + (w.linkMbps ? ` · ${w.linkMbps} Mbit/s` : '') + (w.frequencyMHz ? ` · ${(w.frequencyMHz / 1000).toFixed(1)} GHz` : '') : 'not connected';
    $('phWifi').className = q === 'weak' ? 'bad' : q === 'fair' ? 'warn' : '';
    $('phWifi').title = w.ssid || '';
    $('phStorage').textContent = p.storage ? `${(p.storage.freeBytes / 1e9).toFixed(1)} GB of ${(p.storage.totalBytes / 1e9).toFixed(0)} GB` : '–';
    $('phStorage').className = p.storage && p.storage.freeBytes < 5e9 ? 'bad' : p.storage && p.storage.freeBytes < 15e9 ? 'warn' : '';
    $('phBattery').textContent = `${p.battery.level}%${p.battery.charging ? ' · charging' : ''}`;
    $('phBattery').className = !p.battery.charging && p.battery.level <= 15 ? 'bad' : !p.battery.charging && p.battery.level <= 30 ? 'warn' : '';
    $('phScreen').textContent = `${p.screenOff ? 'off' : 'on'} · DND ${p.dnd ? 'on' : 'off'}`;
  } catch (e) { $('phTemp').textContent = '–'; $('phScreen').textContent = 'adb not connected'; }
}

async function loadAutomation() {
  try { const a = await (await fetch('/automation')).json(); $('autoScreen').checked = a.screenOffWhileRecording; $('autoDnd').checked = a.dndWhileRecording; } catch {}
}
async function saveAutomation(changes) {
  try { await fetch('/automation', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) }); }
  catch { toast('Could not save setting'); }
  loadAutomation();
}
$('autoScreen').onchange = (e) => saveAutomation({ screenOffWhileRecording: e.target.checked });
$('autoDnd').onchange = (e) => saveAutomation({ dndWhileRecording: e.target.checked });
async function setScreen(on) {
  try {
    const r = await fetch('/screen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) });
    if (!r.ok) throw new Error((await r.json()).error);
  } catch (e) { toast('Screen: ' + e.message); }
  pollPhone();
}
$('screenOff').onclick = () => setScreen(false);
$('screenOn').onclick = () => setScreen(true);
