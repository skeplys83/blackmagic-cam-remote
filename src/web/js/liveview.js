// Live view: receives the /mirror stream, decodes video (WebCodecs, or MSE via mse.js) and plays the microphone sound.

// ---------- live view (screen mirror + mic) ----------
// Stream frames from /mirror: type u8, pts u64, size u32, payload.
// Types: 0 session (width,height), 1 H.264 config (SPS/PPS), 2 key frame, 3 delta frame, 4 PCM s16le 48 kHz stereo,
// 5 replaced (another page took the stream; don't reconnect).

const mirror = {
  crop: 'viewport', quality: 'medium', vw: 0, vh: 0, vp: null, abort: null, decoder: null,
  config: null, gotKey: false, frames: 0, bytes: 0, audioCtx: null, gain: null, playhead: 0, gen: 0,
  rot: 'auto', phoneRot: null,
};
try { mirror.rot = localStorage.getItem('rot') || 'auto'; } catch {}
const canvas = $('view'), ctx2d = canvas.getContext('2d');

function stageMsg(text) { // shown in a rounded box over the picture
  const box = document.createElement('span'); box.textContent = text || '';
  $('stageMsg').replaceChildren(...(text ? [box] : [])); $('stageMsg').hidden = !text;
}

// Where the camera picture sits on the phone screen. Asking the phone takes ~2.5 s (a UI dump), so after a
// resolution change the page first predicts the new area, then confirms it.
async function fetchViewport() {
  try {
    const r = await fetch('/viewport'); if (!r.ok) throw 0;
    const vp = await r.json();
    if (mirror.expect) { // right after a format change: ignore a layout the app hasn't updated yet
      const [x1, y1, x2, y2] = vp.viewport, ar = (x2 - x1) / (y2 - y1);
      const want = vp.screen[1] > vp.screen[0] ? mirror.expect.h / mirror.expect.w : mirror.expect.w / mirror.expect.h;
      if (Math.abs(ar - want) / want > 0.03) {
        if (++mirror.expect.tries < 6) setTimeout(fetchViewport, 1000);
        return;
      }
      mirror.expect = null;
    }
    rememberViewportSpace(vp);
    mirror.vp = vp;
  } catch { if (!mirror.expect) mirror.vp = null; }
}

// The app shows the picture as large as fits in a fixed area, centered. Remember the largest width and height
// seen per screen orientation, to predict the area for another aspect ratio.
const vpSpace = {};
function rememberViewportSpace(vp) {
  const key = vp.screen.join('x'), [x1, y1, x2, y2] = vp.viewport, s = vpSpace[key] || { w: 0, h: 0 };
  vpSpace[key] = { w: Math.max(s.w, x2 - x1), h: Math.max(s.h, y2 - y1), cx: (x1 + x2) / 2, cy: (y1 + y2) / 2 };
}

function predictViewport(width, height) {
  const vp = mirror.vp; if (!vp) return;
  const space = vpSpace[vp.screen.join('x')]; if (!space) return;
  const portrait = vp.screen[1] > vp.screen[0];
  const ar = portrait ? height / width : width / height; // the sensor is landscape; the picture turns with the screen
  let w = space.w, h = w / ar;
  if (h > space.h * 1.35) { h = space.h * 1.35; w = h * ar; } // can grow taller than seen so far, within reason
  mirror.vp = { screen: vp.screen, viewport: [space.cx - w / 2, space.cy - h / 2, space.cx + w / 2, space.cy + h / 2].map(Math.round) };
}

// Called when the camera reports a new video format.
function onVideoFormat(f) {
  if (!f?.width || !f?.height) return;
  predictViewport(f.width, f.height);
  mirror.expect = { w: f.width, h: f.height, tries: 0 };
  setTimeout(fetchViewport, 700); // confirm once the app has laid out the new size (retries if it hasn't yet)
}

function cropRect() {
  const { vw, vh, vp } = mirror;
  if (mirror.crop !== 'viewport' || !vp) return [0, 0, vw, vh];
  const [sw, sh] = vp.screen;
  if ((sw > sh) !== (vw > vh)) { fetchViewport(); return [0, 0, vw, vh]; } // rotated: refresh
  const kx = vw / sw, ky = vh / sh, [x1, y1, x2, y2] = vp.viewport;
  return [Math.round(x1 * kx), Math.round(y1 * ky), Math.round((x2 - x1) * kx), Math.round((y2 - y1) * ky)];
}

function codecString(cfg) {
  for (let i = 0; i + 4 < cfg.length; i++) {
    if (cfg[i] === 0 && cfg[i + 1] === 0 && cfg[i + 2] === 1 && (cfg[i + 3] & 0x1f) === 7) {
      const h = (b) => b.toString(16).padStart(2, '0');
      return 'avc1.' + h(cfg[i + 4]) + h(cfg[i + 5]) + h(cfg[i + 6]);
    }
  }
  return 'avc1.640028';
}

// Draw one decoded frame (VideoFrame or <video>) with the viewport crop and rotation.
function drawFrame(src) {
  const [sx, sy, sw, sh] = cropRect();
  if (!sw || !sh) return;
  const r = rotation(), side = r % 180 !== 0;
  const cw = side ? sh : sw, ch = side ? sw : sh;
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw; canvas.height = ch;
    $('stage').style.setProperty('--ar', `${cw} / ${ch}`);
    fitLiveTile();
  }
  ctx2d.setTransform(1, 0, 0, 1, cw / 2, ch / 2);
  ctx2d.rotate(r * Math.PI / 180);
  ctx2d.drawImage(src, sx, sy, sw, sh, -sw / 2, -sh / 2, sw, sh);
  ctx2d.setTransform(1, 0, 0, 1, 0, 0);
  mirror.frames++;
  stageMsg('');
}

// The whole live-view tile (heading, picture, toolbar, record button) fits in the window from where it starts:
// the picture's maximum height is the window height minus the tile's distance from the top of the page,
// minus everything else in the tile, minus a bottom margin.
function fitLiveTile() {
  const tile = $('liveTile'), stage = $('stage');
  if (!tile.offsetParent) return; // dashboard hidden
  const top = tile.getBoundingClientRect().top + window.scrollY;
  const chrome = tile.offsetHeight - stage.offsetHeight;
  stage.style.setProperty('--maxh', Math.max(160, window.innerHeight - top - chrome - 16) + 'px');
}
window.addEventListener('resize', fitLiveTile);
new ResizeObserver(fitLiveTile).observe(document.querySelector('#liveTile .toolbar')); // the toolbar can wrap

// WebCodecs (lowest latency) needs a secure page: https:// or http://localhost. Elsewhere, use MSE.
const useWebCodecs = window.isSecureContext && 'VideoDecoder' in window;
const MSE = window.ManagedMediaSource || window.MediaSource;

function newDecoder() {
  if (!useWebCodecs) return mse.start(mirror.config);
  if (mirror.decoder && mirror.decoder.state !== 'closed') mirror.decoder.close();
  mirror.decoder = new VideoDecoder({
    output: (frame) => { drawFrame(frame); frame.close(); },
    error: (e) => { console.warn('decoder', e); mirror.gotKey = false; },
  });
  mirror.decoder.configure({ codec: codecString(mirror.config), optimizeForLatency: true, hardwareAcceleration: 'prefer-hardware' });
  mirror.gotKey = false;
}

function onPacket(type, pts, data) {
  if (type === 0) {
    const dv = new DataView(data.buffer, data.byteOffset);
    mirror.vw = dv.getUint32(0); mirror.vh = dv.getUint32(4);
    fetchViewport();
  } else if (type === 1) {
    mirror.config = data.slice(); newDecoder();
  } else if ((type === 2 || type === 3) && !useWebCodecs) {
    mse.frame(type === 2, data);
  } else if (type === 2 || type === 3) {
    if (!mirror.decoder || mirror.decoder.state !== 'configured') return;
    if (type === 3 && !mirror.gotKey) return;
    let payload = data;
    if (type === 2) { // in-band SPS/PPS so the Annex B decoder can start here
      payload = new Uint8Array(mirror.config.length + data.length);
      payload.set(mirror.config); payload.set(data, mirror.config.length);
      mirror.gotKey = true;
    }
    if (mirror.decoder.decodeQueueSize > 30 && type === 3) return; // falling behind
    mirror.decoder.decode(new EncodedVideoChunk({ type: type === 2 ? 'key' : 'delta', timestamp: Number(pts), data: payload }));
  } else if (type === 4) {
    playPcm(data);
  } else if (type === 5) {
    mirror.replaced = true;
  }
}


function playPcm(data) {
  const ac = mirror.audioCtx; if (!ac) return;
  const n = data.length / 4;
  const buf = ac.createBuffer(2, n, 48000);
  const L = buf.getChannelData(0), R = buf.getChannelData(1);
  const dv = new DataView(data.buffer, data.byteOffset, data.length);
  for (let i = 0; i < n; i++) { L[i] = dv.getInt16(i * 4, true) / 32768; R[i] = dv.getInt16(i * 4 + 2, true) / 32768; }
  const now = ac.currentTime;
  if (mirror.playhead < now + 0.02 || mirror.playhead > now + 0.35) mirror.playhead = now + 0.08; // (re)sync, ~80 ms buffer
  const src = ac.createBufferSource(); src.buffer = buf; src.connect(mirror.gain); src.start(mirror.playhead);
  mirror.playhead += buf.duration;
}

// Stop the live view completely; closing the stream makes the helper end the capture session on the phone.
function stopMirror() {
  mirror.gen++; // cancels pending reconnects
  if (mirror.abort) { mirror.abort.abort(); mirror.abort = null; }
  if (mirror.decoder && mirror.decoder.state !== 'closed') mirror.decoder.close();
  mirror.decoder = null; mirror.config = null; mirror.gotKey = false;
  mse.stop();
  $('takeover').hidden = true;
  stageMsg('Connecting to phone…');
}

async function runMirror() {
  const gen = ++mirror.gen;
  if (mirror.abort) mirror.abort.abort();
  const ctrl = new AbortController(); mirror.abort = ctrl;
  mirror.replaced = false; $('takeover').hidden = true;
  stageMsg('Connecting to phone…');
  try {
    const r = await fetch(`/mirror?quality=${mirror.quality}&audio=mic`, { signal: ctrl.signal });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    const reader = r.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      mirror.bytes += value.length;
      const merged = new Uint8Array(buf.length + value.length); merged.set(buf); merged.set(value, buf.length); buf = merged;
      let off = 0;
      while (buf.length - off >= 13) {
        const dv = new DataView(buf.buffer, buf.byteOffset + off);
        const size = dv.getUint32(9);
        if (buf.length - off - 13 < size) break;
        onPacket(dv.getUint8(0), dv.getBigUint64(1), buf.subarray(off + 13, off + 13 + size));
        off += 13 + size;
      }
      buf = buf.slice(off);
    }
    throw new Error('stream ended');
  } catch (e) {
    if (gen !== mirror.gen) return; // replaced by a newer stream
    if (mirror.replaced) { stageMsg(''); $('takeover').hidden = false; return; }
    stageMsg('Live view disconnected (' + (e.message || e) + '). Reconnecting…');
    setTimeout(() => { if (gen === mirror.gen) runMirror(); }, 2500);
  }
}

// Clockwise rotation for the live view. Auto follows the phone's own screen rotation, which the
// mirrored picture already has, so it adds no turn; the buttons force a fixed rotation.
function rotation() { return mirror.rot === 'auto' ? 0 : +mirror.rot; }

function showRot() {
  setSeg('rot', mirror.rot);
  const b = $('rot').querySelector('[data-v="auto"]'), r = mirror.phoneRot;
  b.textContent = r != null ? `Auto (phone ${r}°)` : 'Auto';
}

async function pollOrientation() {
  try {
    const r = await fetch('/orientation'); if (!r.ok) return;
    mirror.phoneRot = (await r.json()).rotation; showRot();
  } catch {}
}

$('rot').onclick = (e) => {
  const v = e.target.dataset.v; if (!v) return;
  mirror.rot = v; showRot();
  try { localStorage.setItem('rot', v); } catch {}
};

$('crop').onclick = (e) => {
  const v = e.target.dataset.v; if (!v) return;
  mirror.crop = v; setSeg('crop', v); if (v === 'viewport') fetchViewport();
};
$('takeoverBtn').onclick = () => runMirror();
$('quality').onchange = (e) => {
  mirror.quality = e.target.value; runMirror();
  try { localStorage.setItem('quality', mirror.quality); } catch {}
};
try { const q = localStorage.getItem('quality'); if (q && $('quality').querySelector(`[value="${q}"]`)) { mirror.quality = q; $('quality').value = q; } } catch {}
$('sound').onclick = () => {
  if (!mirror.audioCtx) {
    mirror.audioCtx = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' });
    mirror.gain = mirror.audioCtx.createGain(); // full volume
    mirror.gain.connect(mirror.audioCtx.destination);
    $('sound').textContent = '🔊 Sound on'; $('sound').classList.add('active');
  } else {
    mirror.audioCtx.close(); mirror.audioCtx = null;
    $('sound').textContent = '🔇 Sound off'; $('sound').classList.remove('active');
  }
};

setInterval(() => {
  // Never empty and fixed width in CSS: a changing width re-wrapped the toolbar and resized the live view.
  $('vstat').textContent = mirror.frames ? `${mirror.frames} fps · ${(mirror.bytes * 8 / 1e6).toFixed(1)} Mbit/s` : '\u00a0';
  mirror.frames = 0; mirror.bytes = 0;
}, 1000);
setInterval(() => { if (dashboardOn && mirror.crop === 'viewport') fetchViewport(); }, 15000);
showRot(); setInterval(() => { if (dashboardOn) pollOrientation(); }, 1000);
