// Live camera state (Server-Sent Events) and the main camera controls: record, exposure, white balance, focus, lens.

// ---------- live camera state (Server-Sent Events from the phone's event websocket) ----------
// P holds the latest value of every property, keyed like '/video/iso'. The phone screen is 'LCD' in events, 'Device' in REST.

const P = {};
const lastEvent = {}; // property -> time of its last pushed update
const FALLBACK = ['/system/videoFormat', '/system/codecFormat', '/system/format', '/system/supportedFormats', '/system/audioCodec', '/camera/power', '/media/workingset',
  '/video/autoExposure', '/video/supportedISOs', '/video/iso', '/video/shutter', '/video/supportedShutters', '/video/whiteBalance',
  '/lens/iris', '/lens/focus/autoFocus', '/lens/focus', '/lens/zoom/description', '/lens/zoom', '/lens/cameras', '/transports/0/record',
  '/transports/0/timecode', '/presets', '/presets/active', '/monitoring/focusAssist', '/monitoring/zebra', '/monitoring/frameGuideRatio',
  '/monitoring/frameGrids', '/monitoring/safeAreaPercent',
  ...['cleanFeed', 'displayLUT', 'zebra', 'focusAssist', 'frameGuide', 'frameGrids', 'safeArea', 'falseColor'].flatMap(k => [`/monitoring/LCD/${k}`, `/monitoring/HDMI/${k}`])];
const restPath = (prop) => prop.replace('/monitoring/LCD/', '/monitoring/Device/').slice(1);

async function refresh(props) { // REST read into P (fallback, and right after our own writes)
  await Promise.all(props.map(async (prop) => {
    try { const v = await api(restPath(prop)); if (v != null) P[prop] = v; } catch {}
  }));
  scheduleRender();
}

let renderQueued = false;
function scheduleRender() { if (!renderQueued) { renderQueued = true; requestAnimationFrame(() => { renderQueued = false; render(); }); } }

// The page has one live stream (/events, opened in setup.js) carrying both the setup state and the camera's
// properties; these two handle the camera part.
function onCameraSnapshot(data) {
  Object.assign(P, data);
  if (dashboardOn) startEvents();
  scheduleRender();
}

function onCameraChange({ p, v }) {
  if (p === '/transports/0/record' && P[p] && P[p].recording && !v.recording) setTimeout(loadClips, 1500); // a clip just finished
  if (p === '/presets') setTimeout(() => refresh(['/presets/active']), 200);
  P[p] = v; lastEvent[p] = Date.now(); scheduleRender();
}

// When the dashboard starts: fill in anything the camera didn't push in its snapshot.
function startEvents() {
  const missing = FALLBACK.filter(k => !(k in P));
  if (missing.length) refresh(missing);
}
function stopEvents() {}

// The timecode isn't always pushed while recording, so read it twice a second then.
setInterval(async () => {
  if (!dashboardOn || !state.recording) return;
  try { P['/transports/0/timecode'] = await api('transports/0/timecode'); scheduleRender(); } catch {}
}, 500);

// Light safety net in case an event is missed.
setInterval(() => { if (dashboardOn) refresh(['/transports/0/record', '/camera/power', '/media/workingset']); }, 10000);

function render() {
  const online = P['@connected'] !== false && !!P['/transports/0/record'];
  setConn(online);
  const rec = P['/transports/0/record'];
  if (rec) {
    state.recording = !!rec.recording;
    $('rec').classList.toggle('on', state.recording);
    $('rec').title = (state.recording ? 'Stop' : 'Start') + ' recording (space)';
    $('liveTile').classList.toggle('recording', state.recording); // red box + timecode while recording
    $('recTc').hidden = !state.recording;
  }
  const tc = P['/transports/0/timecode'];
  if (tc?.display) $('recTcText').textContent = tc.display;

  const sys = P['/system'], pow = P['/camera/power'], media = P['/media/workingset'];
  const vf = P['/system/videoFormat'] || sys?.videoFormat, cf = P['/system/codecFormat'] || sys?.codecFormat; // the /system event isn't shaped like REST /system
  if (vf && cf) $('fmt').textContent = `${vf.name.replace(' @ ', ' ')} ${cf.codec.split(':')[0]}`; // narrow sidebar: wraps per word
  if (pow) $('bat').textContent = pow.batteries?.[0] ? pow.batteries[0].chargeRemainingPercent + '%' : pow.source;
  if (media) { const d = media.workingset.find(w => w.activeDisk) || media.workingset[0]; $('left').textContent = fmtMins(d?.remainingRecordTime); }

  const ae = P['/video/autoExposure'], isos = P['/video/supportedISOs'], iso = P['/video/iso'];
  const shut = P['/video/shutter'], shuts = P['/video/supportedShutters'], wb = P['/video/whiteBalance'], iris = P['/lens/iris'];
  if (ae) setSeg('ae', ae.mode === 'Off' ? 'Off' : 'Continuous');
  const manualExp = ae && ae.mode === 'Off';
  if (isos) fillSelect($('iso'), isos.supportedISOs, v => v);
  if (iso) {
    const sel = $('iso');
    sel.querySelectorAll('[data-auto]').forEach(o => o.remove());
    if (![...sel.options].some(o => +o.value === iso.iso)) sel.insertAdjacentHTML('afterbegin', `<option value="${iso.iso}" data-auto>${iso.iso} (auto)</option>`);
    sel.value = iso.iso;
  }
  if (shuts) fillSelect($('shutter'), shuts.shutterSpeeds, v => '1/' + v);
  if (shut) {
    const sel = $('shutter');
    sel.querySelectorAll('[data-auto]').forEach(o => o.remove());
    if (![...sel.options].some(o => +o.value === shut.shutterSpeed)) sel.insertAdjacentHTML('afterbegin', `<option value="${shut.shutterSpeed}" data-auto>1/${shut.shutterSpeed} (auto)</option>`);
    sel.value = shut.shutterSpeed;
  }
  $('iso').disabled = $('shutter').disabled = !manualExp;
  if (iris) $('iris').textContent = 'f/' + iris.apertureStop.toFixed(1) + ' (fixed)';
  if (wb) {
    setSlider('wb', wb.whiteBalance, 'wbOut', wb.whiteBalance + ' K');
    setSlider('tint', wb.tint, 'tintOut', wb.tint);
    state.tint = wb.tint;
  }

  const af = P['/lens/focus/autoFocus'], focus = P['/lens/focus'], zoomD = P['/lens/zoom/description'], zoom = P['/lens/zoom'], lenses = P['/lens/cameras'];
  if (af) setSeg('af', af.enabled ? 'Continuous' : 'off');
  if (focus) { state.focusDistance = focus.focusDistance; setSlider('focus', focus.normalized, 'focusOut', Math.round(focus.normalized * 100) + '%'); }
  $('focus').disabled = !!(af && af.enabled);
  if (zoomD) { $('zoom').dataset.min = zoomD.focalLength.min; $('zoom').dataset.max = zoomD.focalLength.max; }
  if (zoom) setSlider('zoom', zoom.normalized, 'zoomOut', zoom.focalLength + ' mm');
  if (lenses) {
    const avail = lenses.cameras.filter(c => c.isAvailable);
    const box = $('lenses');
    const key = avail.map(c => c.id).join(',');
    if (box.dataset.key !== key) {
      box.dataset.key = key;
      box.innerHTML = avail.map(c => `<button class="btn" data-v="${c.id}">${c.facing === 'front' ? 'Front' : c.zoomFactor} · ${c.focalLength}mm</button>`).join('');
    }
    const act = lenses.cameras.find(c => c.isActive);
    if (act) { setSeg('lenses', act.id); $('lensNow').textContent = `${act.facing === 'front' ? 'Front' : act.zoomFactor} · ${act.focalLength}mm`; }
  }

  renderFormat(); renderMonitoring(); renderPresets();
}

// ---------- controls ----------

// This app's API wants request bodies shaped like its GET responses.
$('rec').onclick = () => send('transports/0/record', 'PUT', { recording: !state.recording, clipName: null }, state.recording ? 'Stop' : 'Record');
document.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && !$('player').open && !['INPUT', 'SELECT', 'BUTTON'].includes(document.activeElement.tagName)) { e.preventDefault(); $('rec').click(); }
});

$('ae').onclick = (e) => { const v = e.target.dataset.v; if (v) send('video/autoExposure', 'PUT', { mode: v, type: null }, 'Auto exposure'); };
$('iso').onchange = (e) => send('video/iso', 'PUT', { iso: +e.target.value }, 'ISO');
$('shutter').onchange = (e) => send('video/shutter', 'PUT', { shutterSpeed: +e.target.value }, 'Shutter');
$('wbAuto').onclick = () => send('video/whiteBalance/doAuto', 'PUT', undefined, 'Auto WB');
$('af').onclick = (e) => {
  const v = e.target.dataset.v; if (!v) return;
  send('lens/focus/autoFocus', 'PUT', { supported: true, enabled: v !== 'off', mode: v === 'off' ? 'OneShot' : v, state: 'Idle', errors: null }, 'Autofocus');
};
$('afOnce').onclick = () => send('lens/focus/doAutoFocus', 'PUT', undefined, 'Focus once');
$('lenses').onclick = (e) => { const v = e.target.closest('button')?.dataset.v; if (v) send('lens/cameras/active', 'PUT', { id: v }, 'Lens'); };

// Sliders: send while dragging (throttled), don't let polling fight the user.
function slider(id, out, fmt, push) {
  const el = $(id); let t = 0, pending = null;
  const flush = () => { if (pending == null) return; const v = pending; pending = null; push(v); };
  el.addEventListener('pointerdown', () => dragging.add(id));
  el.addEventListener('pointerup', () => { flush(); setTimeout(() => dragging.delete(id), 800); });
  el.addEventListener('input', () => {
    const v = +el.value; $(out).textContent = fmt(v); pending = v;
    const now = Date.now(); if (now - t > 120) { t = now; flush(); }
  });
  el.addEventListener('change', flush);
}
slider('wb', 'wbOut', v => v + ' K', v => send('video/whiteBalance', 'PUT', { whiteBalance: v, tint: state.tint ?? 0 }, 'White balance'));
slider('tint', 'tintOut', v => v, v => send('video/whiteBalanceTint', 'PUT', { whiteBalanceTint: v }, 'Tint'));
slider('focus', 'focusOut', v => Math.round(v * 100) + '%', v => send('lens/focus', 'PUT', { normalized: v }, 'Focus')); // only normalized: the app uses focusDistance instead if both are sent
slider('zoom', 'zoomOut', v => {
  const z = $('zoom'); const mn = +z.dataset.min || 0, mx = +z.dataset.max || 1;
  return Math.round(mn + (mx - mn) * v) + ' mm';
}, v => send('lens/zoom', 'PUT', { normalized: v }, 'Zoom'));
