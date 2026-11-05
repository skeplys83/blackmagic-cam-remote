// Recordings list, player and downloads.

// ---------- recordings ----------

function fmtSize(b) { return b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(1) + ' MB'; }
function esc(s) { return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function loadClips() {
  const box = $('clips');
  try {
    const r = await fetch('/clips'); const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    if (!j.clips.length) { box.innerHTML = '<div class="empty">No recordings yet.</div>'; return; }
    box.innerHTML = j.clips.map(c => {
      const q = `name=${encodeURIComponent(c.name)}`;
      const date = new Date(c.mtime * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      return `<div class="card">
        <div class="thumb" data-name="${esc(c.name)}" style="background-image:url('/clips/thumb?${q}&size=${c.size}&mtime=${c.mtime}')"></div>
        <div class="meta"><div class="name" title="${esc(c.name)}">${esc(c.name)}</div>
          <div class="sub"><span>${date} · ${fmtSize(c.size)}</span><a class="btn small" href="/clips/file?${q}&download=1">Download</a></div></div>
      </div>`;
    }).join('');
  } catch (e) { box.innerHTML = `<div class="empty">Couldn't list recordings: ${esc(String(e.message || e))}</div>`; }
}

$('clips').onclick = (e) => {
  const t = e.target.closest('.thumb'); if (!t) return;
  const q = 'name=' + encodeURIComponent(t.dataset.name);
  $('playerVideo').src = '/clips/file?' + q;
  $('playerName').textContent = t.dataset.name;
  $('playerDl').href = '/clips/file?' + q + '&download=1';
  $('player').showModal(); $('playerVideo').play().catch(() => {});
};
$('playerClose').onclick = () => $('player').close();
$('player').addEventListener('close', () => { $('playerVideo').pause(); $('playerVideo').removeAttribute('src'); $('playerVideo').load(); });
$('clipsRefresh').onclick = loadClips;
