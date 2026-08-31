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

function connect() {
  let es;
  try {
    es = new EventSource('status/stream');
  } catch (e) {
    return poll();
  }
  es.onmessage = (ev) => {
    try {
      render(JSON.parse(ev.data));
      pill($('conn'), 'ok', 'live');
    } catch (e) { /* keep last good render */ }
  };
  es.onerror = () => {
    markStale();
    es.close();
    setTimeout(connect, 4000);
  };
}

async function poll() {
  try {
    const r = await fetch('status');
    if (r.ok) {
      render(await r.json());
      pill($('conn'), 'ok', 'live');
    } else markStale();
  } catch (e) { markStale(); }
  setTimeout(poll, 5000);
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
    const res = await post('calibration/reference');
    if (!res.valid) alert('Reference saved, but calibration is still incomplete:\n\n' + res.reason);
  } catch (err) {
    alert('Could not set reference: ' + err.message);
  } finally { e.target.disabled = false; }
});

$('clear-calib').addEventListener('click', async (e) => {
  if (!confirm('Clear the reference and sink area? Detection stops until you set them again.')) return;
  e.target.disabled = true;
  try { await post('calibration/clear'); } catch (err) { alert(err.message); }
  finally { e.target.disabled = false; }
});

$('set-roi').addEventListener('click', () => {
  const cur = prompt('Sink area as x1,y1,x2,y2 (pixels in the captured frame):', '');
  if (!cur) return;
  const parts = cur.split(',').map(s => parseInt(s.trim(), 10));
  if (parts.length !== 4 || parts.some(isNaN)) return alert('Need four numbers: x1,y1,x2,y2');
  post('calibration/roi', { sink: parts })
    .then(res => { if (!res.valid) alert('Saved, but still not calibrated:\n\n' + res.reason); })
    .catch(err => alert('Could not set sink area: ' + err.message));
});

/* ---------- boot: cache first ---------- */

(function boot() {
  try {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) { render(JSON.parse(cached)); pill($('conn'), 'mute', 'cached'); }
  } catch (e) { /* ignore */ }
  connect();
  setInterval(() => { if (lastGoodAt && Date.now() - lastGoodAt > 15000) markStale(); }, 5000);
})();
