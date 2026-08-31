/* dishwatcher dashboard.
 *
 * Behaviour follows the TigerHub rules:
 *   - paint from cache first, never a cold spinner over data we already have
 *   - only changed values animate; a refresh returning the same numbers is silent
 *   - never replay the entrance animation on refresh
 *   - status never pulses
 *   - stale data plus "updated Nm ago" beats an error screen
 */

const $ = (id) => document.getElementById(id);
const CACHE_KEY = 'dishwatcher.snapshot.v2';

let lastValues = {};
let lastGoodAt = null;
let firstPaintDone = false;

/* ---------- theme ---------- */

(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('dishwatcher.theme'); } catch (e) { /* private mode */ }
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  $('theme').addEventListener('click', () => {
    const now = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', now);
    try { localStorage.setItem('dishwatcher.theme', now); } catch (e) { /* ignore */ }
  });
})();

/* ---------- helpers ---------- */

function setText(id, value, { animate = true } = {}) {
  const el = $(id);
  if (!el) return;
  const str = (value === null || value === undefined || value === '') ? '--' : String(value);
  el.classList.remove('skeleton');
  if (lastValues[id] === str) return;          // unchanged: do nothing at all
  el.textContent = str;
  if (animate && firstPaintDone) {             // never replay entrance on refresh
    el.classList.remove('changed');
    void el.offsetWidth;
    el.classList.add('changed');
  }
  lastValues[id] = str;
}

function humanDuration(sec) {
  if (sec === null || sec === undefined) return '--';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm ? `${h}h ${rm}m` : `${h}h`;
}

/* "say it in words where words are clearer" */
function describeState(state) {
  switch (state) {
    case 'CLEAR':     return { word: 'Clean', label: 'nothing in the sink', cls: 'state-clear' };
    case 'CONFIRMED': return { word: 'Dishes', label: 'waiting out the grace period', cls: 'state-dirty' };
    case 'ALERTED':   return { word: 'Overdue', label: 'you have been told', cls: 'state-alerted' };
    default:          return { word: '----', label: 'state', cls: 'state-unknown' };
  }
}

function pill(el, kind, text) {
  el.className = 'pill pill-' + kind;
  el.innerHTML = '<span class="dot"></span>' + text;   // colour never travels alone
}

/* ---------- rendering ---------- */

function render(d) {
  // calibration first: if this is bad, nothing else on the page is meaningful
  const cal = d.calibration || {};
  const banner = $('calib-banner');
  if (cal.valid) {
    banner.hidden = true;
  } else {
    banner.hidden = false;
    $('calib-reason').textContent = cal.reason || 'unknown reason';
  }

  // hero
  const st = describeState(cal.valid ? d.state : null);
  $('hero').className = 'hero ' + st.cls;
  setText('state-value', st.word);
  setText('state-label', st.label, { animate: false });
  setText('since', humanDuration(d.seconds_in_state));

  /* The state machine needs a majority of recent frames to agree before it
   * moves. Without showing that, a fresh dirty reading next to a "Clean" hero
   * looks like a contradiction rather than a vote in progress. */
  const cons = d.consensus;
  if (cons && cons.size) {
    const need = cons.threshold, votes = cons.positive;
    setText('consensus', `${votes}/${need} say dishes`);
  } else {
    setText('consensus', 'no frames yet');
  }
  setText('alert-in', d.seconds_until_alert === null || d.seconds_until_alert === undefined
    ? 'not counting' : humanDuration(d.seconds_until_alert));

  // detector
  if (cal.valid && d.ssim_score !== null && d.ssim_score !== undefined) {
    setText('ssim-value', d.ssim_score.toFixed(3));
  } else {
    setText('ssim-value', '----');
  }
  setText('threshold', d.ssim_threshold !== undefined ? d.ssim_threshold.toFixed(2) : '--');
  setText('labels', (d.labels && d.labels.length) ? d.labels.join(', ') : 'nothing recognised');
  setText('latency', d.inference_ms !== undefined && d.inference_ms !== null
    ? Math.round(d.inference_ms) + ' ms' : '--');

  renderHeat(d.ssim_tiles, d.ssim_threshold);

  // camera health
  const cam = d.camera || {};
  const link = $('cam-link');
  if (!d.camera_seen) pill(link, 'mute', 'never seen');
  else if (cam.healthy === false) pill(link, 'bad', 'wedged');
  else pill(link, 'ok', 'streaming');
  link.classList.remove('skeleton');

  setText('cam-last', cam.seconds_since_last_frame !== undefined
    ? humanDuration(cam.seconds_since_last_frame) + ' ago' : '--');
  setText('cam-reopens', cam.reopens !== undefined ? cam.reopens : '--');
  setText('cam-motion', cam.motion_state || '--');
  // v1 sat at 23.7; show it plainly so a regression is visible
  setText('cam-flap', cam.flap_ratio !== undefined ? cam.flap_ratio.toFixed(2) : '--');

  // frame
  if (d.latest_frame_url) {
    const img = $('frame');
    if (img.dataset.src !== d.latest_frame_url) {
      img.dataset.src = d.latest_frame_url;
      img.src = d.latest_frame_url + '?t=' + Date.now();
    }
  }
  $('frame-age').textContent = d.latest_frame_age_seconds !== undefined && d.latest_frame_age_seconds !== null
    ? 'captured ' + humanDuration(d.latest_frame_age_seconds) + ' ago' : '';

  // the reference, so a bad calibration is visible instead of implicit
  if (cal.valid) {
    const stamp = cal.reference_shape ? cal.reference_shape.join('x') : '';
    const full = $('ref-full'), roi = $('ref-roi');
    if (full.dataset.stamp !== stamp) {
      full.dataset.stamp = stamp;
      full.src = '/calibration/reference.jpg?t=' + Date.now();
      roi.src = '/calibration/reference.jpg?roi_only=1&t=' + Date.now();
    }
    $('ref-note').textContent = cal.roi && cal.roi.sink
      ? 'sink area ' + cal.roi.sink.join(', ') : '';
  } else {
    $('ref-full').removeAttribute('src');
    $('ref-roi').removeAttribute('src');
    $('ref-note').textContent = 'no reference set';
  }

  renderEvents(d.events || []);

  lastGoodAt = Date.now();
  firstPaintDone = true;
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(d)); } catch (e) { /* ignore */ }
}

function renderHeat(tiles, threshold) {
  const el = $('heat');
  if (!tiles || !tiles.length) {
    if (!el.dataset.empty) { el.innerHTML = ''; el.dataset.empty = '1'; }
    return;
  }
  delete el.dataset.empty;
  const g = Math.round(Math.sqrt(tiles.length));
  el.style.gridTemplateColumns = `repeat(${g}, 1fr)`;

  const sig = tiles.map(t => t.score.toFixed(2)).join(',');
  if (el.dataset.sig === sig) return;    // unchanged: do not repaint
  el.dataset.sig = sig;

  el.innerHTML = '';
  const th = threshold || 0.82;
  for (const t of tiles) {
    const cell = document.createElement('i');
    // below threshold reads as "changed"; above ramps opacity by how close it is
    if (t.score < th) {
      cell.style.background = 'var(--warn)';
    } else {
      const room = Math.max(0.0001, 1 - th);
      const frac = Math.min(1, Math.max(0, (t.score - th) / room));
      cell.style.background = 'var(--ok)';
      cell.style.opacity = (0.25 + 0.75 * frac).toFixed(2);
    }
    cell.title = `${t.score.toFixed(3)}`;
    el.appendChild(cell);
  }
}

function renderEvents(events) {
  const el = $('events');
  if (!events.length) {
    if (el.dataset.state !== 'empty') {
      el.innerHTML = '<div class="empty">No events yet</div>';
      el.dataset.state = 'empty';
    }
    return;
  }
  const sig = events.map(e => e.id || (e.at + e.kind)).join('|');
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.dataset.state = 'list';

  el.innerHTML = '';
  for (const e of events.slice(0, 40)) {
    const row = document.createElement('div');
    row.className = 'event';

    const when = document.createElement('span');
    when.className = 'when';
    when.textContent = e.at ? new Date(e.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';

    const what = document.createElement('span');
    what.className = 'what';
    what.textContent = e.message || e.kind || '';

    row.appendChild(when);
    row.appendChild(what);

    // "a badge only when the status is not the default"
    if (e.kind && e.kind !== 'info') {
      const p = document.createElement('span');
      const kind = e.kind === 'alert' ? 'bad' : e.kind === 'dirty' ? 'warn' : 'mute';
      pill(p, kind, e.kind);
      row.appendChild(p);
    }
    el.appendChild(row);
  }
}

/* ---------- connection: SSE with polling fallback ---------- */

function markStale() {
  if (!lastGoodAt) return;
  const age = Math.round((Date.now() - lastGoodAt) / 1000);
  // stale data + a quiet note, not an error screen
  pill($('conn'), 'warn', 'updated ' + humanDuration(age) + ' ago');
}

let pollTimer = null;
let sse = null;

function startPolling(why) {
  if (pollTimer) return;              // already polling
  if (sse) { try { sse.close(); } catch (e) {} sse = null; }
  console.info('falling back to polling:', why);
  const tick = async () => {
    try {
      const r = await fetch('/status', { credentials: 'same-origin' });
      if (r.ok) { render(await r.json()); pill($('conn'), 'ok', 'live'); }
      else markStale();
    } catch (e) { markStale(); }
    pollTimer = setTimeout(tick, 5000);
  };
  tick();
}

/* Server-sent events, with a hard fallback.
 *
 * Behind the Authentik outpost the stream can connect and then deliver
 * nothing, because a proxy in the path buffers it. The page then sat on
 * skeletons forever: the old code only fell back if the EventSource
 * constructor threw, which it does not in that case. So if no message
 * arrives shortly after opening, give up on the stream and poll instead. */
function connect() {
  let opened = false;

  try {
    sse = new EventSource('/status/stream', { withCredentials: true });
  } catch (e) {
    return startPolling('EventSource unavailable');
  }

  // if the stream is silent, it is useless however healthy it looks
  const silenceTimer = setTimeout(() => {
    if (!opened) startPolling('no data within 6s of connecting');
  }, 6000);

  sse.onmessage = (ev) => {
    opened = true;
    clearTimeout(silenceTimer);
    try {
      render(JSON.parse(ev.data));
      pill($('conn'), 'ok', 'live');
    } catch (e) { /* keep the last good render */ }
  };

  sse.onerror = () => {
    clearTimeout(silenceTimer);
    markStale();
    try { sse.close(); } catch (e) {}
    sse = null;
    // a stream that errors before ever delivering is not worth retrying
    if (opened) setTimeout(connect, 4000);
    else startPolling('stream errored before delivering anything');
  };
}

/* ---------- actions ---------- */

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

$('set-ref').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    const res = await post('/calibration/reference');
    if (!res.valid) alert('Reference saved, but calibration is still incomplete:\n\n' + res.reason);
  } catch (err) {
    alert('Could not set reference: ' + err.message);
  } finally { e.target.disabled = false; }
});

$('clear-calib').addEventListener('click', async (e) => {
  if (!confirm('Clear the reference and sink area? Detection stops until you set them again.')) return;
  e.target.disabled = true;
  try { await post('/calibration/clear'); } catch (err) { alert(err.message); }
  finally { e.target.disabled = false; }
});

$('set-roi').addEventListener('click', () => {
  const cur = prompt('Sink area as x1,y1,x2,y2 (pixels in the captured frame):', '');
  if (!cur) return;
  const parts = cur.split(',').map(s => parseInt(s.trim(), 10));
  if (parts.length !== 4 || parts.some(isNaN)) return alert('Need four numbers: x1,y1,x2,y2');
  post('/calibration/roi', { sink: parts })
    .then(res => { if (!res.valid) alert('Saved, but still not calibrated:\n\n' + res.reason); })
    .catch(err => alert('Could not set sink area: ' + err.message));
});

/* ---------- live view ----------
 * The Pi cannot accept inbound connections, so it pushes frames to the server
 * while a lease is held and the browser reads them back as multipart MJPEG.
 * Opening the stream renews the lease; closing the tab lets it lapse, so the
 * Pi stops on its own rather than streaming forever.
 */

let liveOn = false;

function setLive(on) {
  liveOn = on;
  const img = $('live'), still = $('frame'), badge = $('live-badge');
  $('live-toggle').textContent = on ? 'Stop live view' : 'Live view';
  $('view-title').textContent = on ? 'Live view' : 'Latest capture';
  badge.hidden = !on;
  still.hidden = on;
  img.hidden = !on;
  if (on) {
    img.src = '/live.mjpg?t=' + Date.now();
  } else {
    img.removeAttribute('src');   // drops the connection so the lease lapses
  }
}

$('live-toggle').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    if (liveOn) { await post('/live/stop'); setLive(false); }
    else { await post('/live/request'); setLive(true); }
  } catch (err) {
    alert('Live view failed: ' + err.message);
  } finally { e.target.disabled = false; }
});

// a wedged or absent camera cannot serve live frames; say so rather than
// showing a broken image icon
$('live').addEventListener('error', () => {
  if (liveOn) { $('live-badge').className = 'pill pill-warn live-badge'; }
});
$('live').addEventListener('load', () => {
  $('live-badge').className = 'pill pill-bad live-badge';
});

/* ---------- roommates + blame clips ---------- */

let peopleCache = [];

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map(w => w[0] || '').join('').toUpperCase();
}

function renderRoster(list, counts) {
  peopleCache = list;
  const el = $('roster');
  const sig = JSON.stringify([list, counts]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;

  if (!list.length) {
    el.innerHTML = '<div class="empty">Nobody added yet. Add a roommate to start tagging clips.</div>';
    return;
  }
  el.innerHTML = '';
  for (const p of list) {
    const row = document.createElement('div');
    row.className = 'person';

    if (p.photo_url) {
      const img = document.createElement('img');
      img.className = 'avatar';
      img.src = p.photo_url;
      img.alt = p.name;
      // photos are refused over the public route on purpose; fall back to
      // initials rather than showing a broken image
      img.onerror = () => {
        const ph = document.createElement('div');
        ph.className = 'avatar placeholder';
        ph.textContent = initials(p.name);
        ph.title = 'photo is only shown on the local network';
        img.replaceWith(ph);
      };
      row.appendChild(img);
    } else {
      const ph = document.createElement('div');
      ph.className = 'avatar placeholder';
      ph.textContent = initials(p.name);
      row.appendChild(ph);
    }

    const who = document.createElement('div');
    who.className = 'who';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = p.name;
    const ct = document.createElement('div');
    ct.className = 'ct';
    const n = counts[p.name] || 0;
    // say it in words; "0 clips" reads worse than "nothing pinned on them"
    ct.textContent = n === 0 ? 'nothing pinned on them' : (n === 1 ? '1 clip' : n + ' clips');
    who.appendChild(nm); who.appendChild(ct);
    row.appendChild(who);

    const acts = document.createElement('div');
    acts.className = 'acts';

    const photoBtn = document.createElement('button');
    photoBtn.className = 'iconbtn';
    photoBtn.textContent = p.photo_url ? 'change photo' : 'add photo';
    photoBtn.onclick = () => pickPhoto(p.id);
    acts.appendChild(photoBtn);

    const del = document.createElement('button');
    del.className = 'iconbtn';
    del.textContent = 'remove';
    del.onclick = async () => {
      if (!confirm('Remove ' + p.name + '? Their clip tags go too.')) return;
      await fetch('/people/' + p.id, { method: 'DELETE', credentials: 'same-origin' });
      loadPeople(); loadClips();
    };
    acts.appendChild(del);

    row.appendChild(acts);
    el.appendChild(row);
  }
}

function pickPhoto(pid) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = async () => {
    if (!input.files || !input.files[0]) return;
    const fd = new FormData();
    fd.append('photo', input.files[0]);
    try {
      const r = await fetch('/people/' + pid + '/photo', {
        method: 'POST', body: fd, credentials: 'same-origin' });
      if (!r.ok) throw new Error(await r.text());
      loadPeople();
    } catch (e) { alert('Could not upload the photo: ' + e.message); }
  };
  input.click();
}

function renderClips(clips) {
  const el = $('clips');
  // over the tunnel the video bytes are refused, so say why instead of
  // rendering a row of dead players
  const offsite = location.protocol === 'https:' && !location.hostname.startsWith('100.');
  if (!clips.length) {
    if (el.dataset.state !== 'empty') {
      el.innerHTML = '<div class="empty">No clips yet. One is recorded when somebody walks away from the sink.</div>';
      el.dataset.state = 'empty';
    }
    return;
  }
  const sig = JSON.stringify(clips.map(c => [c.url, c.tag && c.tag.person_id]));
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.dataset.state = 'list';

  el.innerHTML = '';
  for (const c of clips) {
    const card = document.createElement('div');
    card.className = 'clip';

    if (offsite) {
      const ph = document.createElement('div');
      ph.className = 'poster';
      ph.style.display = 'grid';
      ph.style.placeItems = 'center';
      ph.style.padding = '0 18px';
      ph.style.textAlign = 'center';
      ph.style.fontSize = '.78rem';
      ph.style.color = 'var(--on-surface-variant)';
      ph.textContent = 'Playable on the local network only';
      card.appendChild(ph);
    } else {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'none';
      if (c.thumb_url) vid.poster = c.thumb_url;
      vid.src = c.url;
      card.appendChild(vid);
    }

    const meta = document.createElement('div');
    meta.className = 'meta';

    const when = document.createElement('div');
    when.className = 'when';
    when.textContent = c.timestamp || c.filename || '';
    meta.appendChild(when);

    const row = document.createElement('div');
    row.className = 'tagrow';

    const sel = document.createElement('select');
    const none = document.createElement('option');
    none.value = ''; none.textContent = 'nobody tagged';
    sel.appendChild(none);
    for (const p of peopleCache) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name;
      if (c.tag && c.tag.person_id === p.id) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = async () => {
      const name = (c.filename || c.url.split('/').pop());
      try {
        await fetch('/clips/' + encodeURIComponent(name) + '/tag', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ person_id: sel.value || null }),
        });
        loadClips(); loadPeople();
      } catch (e) { alert('Could not tag the clip: ' + e.message); }
    };
    row.appendChild(sel);

    // a badge only when it is tagged; an untagged clip says so in the dropdown
    if (c.tag) {
      const p = document.createElement('span');
      pill(p, 'warn', c.tag.name);
      row.appendChild(p);
    }

    meta.appendChild(row);
    card.appendChild(meta);
    el.appendChild(card);
  }
}

async function loadPeople() {
  try {
    const r = await fetch('/people', { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    renderRoster(d.people || [], d.counts || {});
  } catch (e) { /* leave the last render */ }
}

async function loadClips() {
  try {
    const r = await fetch('/clips', { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    renderClips(d.clips || []);
  } catch (e) { /* leave the last render */ }
}

$('add-person').addEventListener('click', async () => {
  const input = $('new-name');
  const name = input.value.trim();
  if (!name) return;
  try {
    const r = await fetch('/people', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    input.value = '';
    loadPeople(); loadClips();
  } catch (e) { alert('Could not add them: ' + e.message); }
});

$('new-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('add-person').click();
});

/* ---------- boot: cache first ---------- */

(function boot() {
  try {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) { render(JSON.parse(cached)); pill($('conn'), 'mute', 'cached'); }
  } catch (e) { /* ignore */ }
  connect();
  loadPeople();
  loadClips();
  // clips only change when somebody walks past the sink, so this is unhurried
  setInterval(loadClips, 30000);
  setInterval(() => { if (lastGoodAt && Date.now() - lastGoodAt > 15000) markStale(); }, 5000);
})();
