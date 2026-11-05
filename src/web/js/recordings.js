// Recordings list, player and downloads.

// ---------- recordings ----------

function fmtDuration(s) { // 13 → "0:13", 3725 → "1:02:05"
  if (s == null) return '';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}
const TRASH = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

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
        <div class="thumb" data-name="${esc(c.name)}" style="background-image:url('/clips/thumb?${q}&size=${c.size}&mtime=${c.mtime}')">
          <button class="clipdel" data-name="${esc(c.name)}" title="Delete this recording from the phone">${TRASH}</button>
          ${c.duration != null ? `<span class="clipdur">${fmtDuration(c.duration)}</span>` : ''}
        </div>
        <div class="meta"><div class="name" title="${esc(c.name)}">${esc(c.name)}</div>
          <div class="sub"><span>${date} · ${fmtSize(c.size)}</span><a class="btn small" href="/clips/file?${q}&download=1">Download</a></div></div>
      </div>`;
    }).join('');
  } catch (e) { box.innerHTML = `<div class="empty">Couldn't list recordings: ${esc(String(e.message || e))}</div>`; }
}

// Delete: first tap arms the button ("Delete?"), a second tap within 4 s deletes the clip from the phone.
async function deleteClip(btn) {
  if (!btn.classList.contains('armed')) {
    btn.classList.add('armed'); btn.innerHTML = 'Delete?';
    setTimeout(() => { if (btn.isConnected && btn.classList.contains('armed')) { btn.classList.remove('armed'); btn.innerHTML = TRASH; } }, 4000);
    return;
  }
  btn.disabled = true; btn.innerHTML = '…';
  try {
    const r = await fetch('/clips/file?name=' + encodeURIComponent(btn.dataset.name), { method: 'DELETE' });
    const j = await r.json(); if (!r.ok) throw new Error(j.error);
    btn.closest('.card').remove();
    if (!$('clips').querySelector('.card')) $('clips').innerHTML = '<div class="empty">No recordings yet.</div>';
    toast('Deleted ' + btn.dataset.name);
  } catch (e) { toast('Delete: ' + e.message); btn.disabled = false; btn.classList.remove('armed'); btn.innerHTML = TRASH; }
}

$('clips').onclick = (e) => {
  const del = e.target.closest('.clipdel');
  if (del) { e.stopPropagation(); deleteClip(del); return; }
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
