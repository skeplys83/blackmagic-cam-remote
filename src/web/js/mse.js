// Live view fallback for plain http:// pages: packs H.264 frames into fragmented MP4 and plays them with Media Source Extensions.

// ---------- live view fallback for plain http://: H.264 → fragmented MP4 → Media Source Extensions ----------

const mse = (() => {
  const cat = (parts) => {
    const n = parts.reduce((a, p) => a + p.length, 0), out = new Uint8Array(n);
    let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  };
  const u8 = (a) => a instanceof Uint8Array ? a : new Uint8Array(a);
  const u16 = (n) => [n >> 8 & 255, n & 255];
  const u32 = (n) => [n >>> 24 & 255, n >>> 16 & 255, n >>> 8 & 255, n & 255];
  const box = (type, ...parts) => {
    const body = cat(parts.map(u8));
    return cat([u8(u32(body.length + 8)), u8([...type].map(c => c.charCodeAt(0))), body]);
  };
  const full = (type, version, flags, ...parts) => box(type, [version, flags >> 16 & 255, flags >> 8 & 255, flags & 255], ...parts);
  const zeros = (n) => new Array(n).fill(0);
  const MATRIX = [...u32(0x10000), ...zeros(12), ...u32(0x10000), ...zeros(12), ...u32(0x40000000)];
  const TIMESCALE = 1e6, FRAME_US = Math.round(1e6 / 24);

  // Split Annex B (00 00 01 / 00 00 00 01 start codes) into NAL units.
  function nals(buf) {
    const out = []; let start = -1;
    for (let i = 0; i + 2 < buf.length; i++) {
      if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) {
        if (start >= 0) out.push(buf.subarray(start, i > 0 && buf[i - 1] === 0 ? i - 1 : i));
        start = i + 3; i += 2;
      }
    }
    if (start >= 0) out.push(buf.subarray(start));
    return out.filter(n => n.length);
  }

  function initSegment(sps, pps, w, h) {
    const avcC = box('avcC', [1, sps[1], sps[2], sps[3], 0xff, 0xe1, ...u16(sps.length)], sps, [1, ...u16(pps.length)], pps);
    const avc1 = box('avc1', zeros(6), u16(1), zeros(16), u16(w), u16(h), u32(0x480000), u32(0x480000), zeros(4), u16(1),
      zeros(32), u16(0x18), u16(0xffff), avcC);
    const stbl = box('stbl', full('stsd', 0, 0, u32(1), avc1), full('stts', 0, 0, u32(0)), full('stsc', 0, 0, u32(0)),
      full('stsz', 0, 0, u32(0), u32(0)), full('stco', 0, 0, u32(0)));
    const minf = box('minf', full('vmhd', 0, 1, zeros(8)), box('dinf', full('dref', 0, 0, u32(1), full('url ', 0, 1))), stbl);
    const mdia = box('mdia', full('mdhd', 0, 0, zeros(8), u32(TIMESCALE), u32(0), u16(0x55c4), u16(0)),
      full('hdlr', 0, 0, u32(0), [...'vide'].map(c => c.charCodeAt(0)), zeros(12), [...'Video\0'].map(c => c.charCodeAt(0))), minf);
    const trak = box('trak', full('tkhd', 0, 7, zeros(8), u32(1), zeros(4), u32(0), zeros(8), zeros(4), zeros(4), MATRIX,
      u32(w << 16), u32(h << 16)), mdia);
    const moov = box('moov', full('mvhd', 0, 0, zeros(8), u32(1000), u32(0), u32(0x10000), u16(0x100), zeros(10), MATRIX,
      zeros(24), u32(2)), trak, box('mvex', full('trex', 0, 0, u32(1), u32(1), u32(0), u32(0), u32(0))));
    const ftyp = box('ftyp', [...'isom'].map(c => c.charCodeAt(0)), u32(0x200), [...'isomiso6avc1mp41'].map(c => c.charCodeAt(0)));
    return cat([ftyp, moov]);
  }

  function fragment(seq, time, sample, key) {
    const hi = Math.floor(time / 2 ** 32), lo = time >>> 0;
    const trun = full('trun', 0, 0x701, u32(1), u32(108), u32(FRAME_US), u32(sample.length), u32(key ? 0x02000000 : 0x01010000));
    const traf = box('traf', full('tfhd', 0, 0x20000, u32(1)), full('tfdt', 1, 0, u32(hi), u32(lo)), trun);
    const moof = box('moof', full('mfhd', 0, 0, u32(seq)), traf); // 100 bytes, so the sample starts at 108
    return cat([moof, box('mdat', sample)]);
  }

  let video = null, ms = null, sb = null, queue = [], seq = 0, time = 0, gotKey = false, mime = '';

  function pump() {
    if (!sb || sb.updating || !queue.length) return;
    try { sb.appendBuffer(queue.shift()); } catch (e) { console.warn('mse append', e); queue = []; gotKey = false; }
  }

  function trim() { // keep the buffer short and stay at the live edge
    if (!video || !sb || sb.updating || !video.buffered.length) return;
    const end = video.buffered.end(video.buffered.length - 1);
    if (end - video.currentTime > 0.5) video.currentTime = end - 0.05;
    if (video.currentTime - video.buffered.start(0) > 10) { try { sb.remove(0, video.currentTime - 5); } catch {} }
    if (video.paused) video.play().catch(() => {});
  }
  setInterval(trim, 250);

  function drawLoop() {
    if (!video) return;
    if (video.readyState >= 2) drawFrame(video);
    if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(drawLoop); else requestAnimationFrame(drawLoop);
  }

  return {
    start(config) {
      if (!MSE) { stageMsg('This browser can\'t play the live view on a plain http:// page. Use https:// or a current Chrome, Edge, Firefox or Safari.'); return; }
      const units = nals(config), sps = units.find(n => (n[0] & 0x1f) === 7), pps = units.find(n => (n[0] & 0x1f) === 8);
      if (!sps || !pps) return;
      const h = (b) => b.toString(16).padStart(2, '0');
      mime = `video/mp4; codecs="avc1.${h(sps[1])}${h(sps[2])}${h(sps[3])}"`;
      if (!MSE.isTypeSupported(mime)) { stageMsg('This browser can\'t play H.264 here.'); return; }
      if (video) { video.removeAttribute('src'); video.load(); }
      video = document.createElement('video');
      video.muted = true; video.playsInline = true; video.autoplay = true; video.disableRemotePlayback = true;
      ms = new MSE(); sb = null; queue = [initSegment(sps, pps, mirror.vw, mirror.vh)]; seq = 0; time = 0; gotKey = false;
      ms.addEventListener('sourceopen', () => {
        sb = ms.addSourceBuffer(mime);
        sb.mode = 'sequence'; // frames play back to back; gaps while the screen is static don't stall the timeline
        sb.addEventListener('updateend', pump);
        pump();
      }, { once: true });
      video.src = URL.createObjectURL(ms);
      video.play().catch(() => {});
      drawLoop();
    },
    stop() {
      if (video) { video.removeAttribute('src'); video.load(); }
      video = null; ms = null; sb = null; queue = []; gotKey = false;
    },
    frame(key, data) {
      if (!ms) return;
      if (!key && !gotKey) return;
      if (key) gotKey = true;
      if (queue.length > 48) { queue = queue.slice(0, 1).filter(() => !sb); gotKey = false; return; } // falling behind: wait for a key frame
      const units = nals(data).filter(n => ![7, 8, 9].includes(n[0] & 0x1f)); // SPS/PPS live in the init segment; drop AUDs
      const sample = cat(units.flatMap(n => [u8(u32(n.length)), n]));
      queue.push(fragment(++seq, time, sample, key));
      time += FRAME_US;
      pump();
    },
  };
})();
