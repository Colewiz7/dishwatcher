// app.js - dishwatcher dashboard v5

// -- state --
let currentState = 'CLEAR';
let notifyOn = false;
let soundOn = false;
let graceTarget = null;
let graceTick = null;
let sse = null;
let reconnDelay = 1000;
let chart = null;
let hasReference = false;
let timelineMode = 'all';

const emoji = {CLEAR:'\u2713', DETECTED:'\uD83D\uDD0D', CONFIRMED:'\u23F3', ALERTED:'\uD83D\uDEA8'};
const titles = {CLEAR:'sink is clear', DETECTED:'dishes detected', CONFIRMED:'dishes confirmed', ALERTED:'wash your dishes'};

// -- sse --

function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource('/stream');

  sse.addEventListener('init', e => {
    reconnDelay = 1000;
    setConn(true);
    const d = JSON.parse(e.data);
    if (d.status) updateStatus(d.status);
    if (d.stats) updateStats(d.stats);
    hasReference = d.has_reference || false;
    updateRefBtn();
    loadTimeline(timelineMode);
    loadEvents();
    initSettingsPanel(d);
  });

  sse.addEventListener('detection', e => onDetection(JSON.parse(e.data)));
  sse.addEventListener('state', e => onStateChange(JSON.parse(e.data)));
  sse.addEventListener('heartbeat', () => setConn(true));
  sse.addEventListener('config', e => {
    const d = JSON.parse(e.data);
    if (d.config) { renderSettings(d.config); applyUiConfig(d.config); }
    toast('settings updated');
  });
  sse.addEventListener('admin', e => {
    const d = JSON.parse(e.data);
    if (d.has_reference !== undefined) {
      hasReference = d.has_reference;
      updateRefBtn();
    }
    toast('admin action: ' + (d.action || 'done'));
  });

  sse.onerror = () => {
    setConn(false);
    setTimeout(() => { reconnDelay = Math.min(reconnDelay * 1.5, 15000); connectSSE(); }, reconnDelay);
  };
}

function setConn(ok) {
  document.getElementById('dot').className = 'dot ' + (ok ? 'on' : 'off');
  document.getElementById('connTxt').textContent = ok ? 'live' : 'reconnecting';
}

// -- detection event --

function onDetection(d) {
  // feed image
  if (d.image_file) {
    const img = document.getElementById('feedImg');
    img.src = '/view/image/' + d.image_file + '?t=' + Date.now();
    img.style.display = 'block';
    document.getElementById('feedEmpty').style.display = 'none';
    document.getElementById('feedBar').style.display = 'flex';
    document.getElementById('feedMs').textContent = (d.inference_ms || 0).toFixed(1) + ' ms';
    document.getElementById('feedTime').textContent = new Date(d.timestamp).toLocaleTimeString();
    document.getElementById('feedMode').textContent = d.capture_mode || '--';
  }

  // ssim meter
  updateMeter(d.ssim_score, d.dishes_found);

  // consensus
  if (d.consensus) updateConsensus(d.consensus);

  // state
  if (d.state) updateStateDisplay(d.state, d);

  // grace timer
  if (d.grace_remaining && d.grace_remaining !== 'None' && d.grace_remaining !== '0:00:00') {
    startGrace(d.grace_remaining);
  } else if (d.state === 'CLEAR') {
    stopGrace();
  }

  // reference check
  if (d.has_reference !== undefined) {
    hasReference = d.has_reference;
    updateRefBtn();
  }

  // add to timeline
  addTimelineItem({
    type: 'image',
    filename: d.image_file,
    timestamp: d.timestamp,
    dishes: d.dishes_found,
    ssim: d.ssim_score,
    labels: d.labels || [],
  });

  // add blame clip as separate timeline entry
  if (d.video_file) {
    addTimelineItem({
      type: 'video',
      filename: d.video_file,
      timestamp: d.timestamp,
      thumb_url: d.video_thumb ? '/view/thumb/' + d.video_thumb : null,
    });
  }

  // browser notification on alert
  if (d.should_alert) {
    notify('dishes have been there too long!', d.image_file);
    if (soundOn) beep();
  }
}

function onStateChange(d) {
  if (d.status) updateStatus(d.status);
  const prev = d.previous_state || '?';
  const next = d.state;
  toast(prev + ' \u2192 ' + next);
  addEvent({timestamp: new Date().toISOString(), from_state: prev, to_state: next, reason: d.reason || ''});

  if (next === 'ALERTED') { notify('ALERT: wash your dishes!'); if (soundOn) beep(); }
  if (next === 'CLEAR' && (prev === 'CONFIRMED' || prev === 'ALERTED')) notify('dishes cleared!');
}

// -- ui updates --

function updateStatus(s) {
  updateStateDisplay(s.state, s);
  if (s.consensus) updateConsensus(s.consensus);
}

function updateStateDisplay(state, data) {
  currentState = state;
  const orb = document.getElementById('orb');
  orb.className = 'orb ' + state;
  orb.innerHTML = emoji[state] || '?';
  document.getElementById('stateTitle').textContent = titles[state] || state;

  let sub = '';
  if (data && data.dishes_since) {
    const since = new Date(data.dishes_since);
    const mins = Math.round((Date.now() - since.getTime()) / 60000);
    sub = 'since ' + since.toLocaleTimeString() + ' (' + mins + ' min)';
  } else if (state === 'CLEAR') {
    sub = 'no dishes detected';
  }
  document.getElementById('stateSub').textContent = sub;
}

function updateMeter(ssim, dirty) {
  ssim = ssim || 0;
  const pct = Math.round(ssim * 100);
  const fill = document.getElementById('meterFill');
  fill.style.width = pct + '%';
  fill.style.background = dirty ? 'var(--red)' : ssim < 0.9 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('ssimVal').textContent = 'SSIM ' + ssim.toFixed(3);
}

function updateConsensus(c) {
  let html = '';
  for (let i = 0; i < c.window; i++) {
    if (i < c.buffer.length) {
      html += '<div class="cdot ' + (c.buffer[i] ? 'y' : 'n') + '">' + (c.buffer[i] ? '\u2713' : '\u2717') + '</div>';
    } else {
      html += '<div class="cdot e"></div>';
    }
  }
  document.getElementById('cDots').innerHTML = html;
  document.getElementById('cRatio').textContent = c.positive + '/' + c.window;
  const pct = Math.round(c.confidence * 100);
  document.getElementById('cPct').textContent = pct + '%';
  const fill = document.getElementById('cFill');
  fill.style.width = pct + '%';
  fill.style.background = pct >= 70 ? 'var(--orange)' : pct >= 40 ? 'var(--yellow)' : 'var(--green)';
}

function updateStats(s) {
  document.getElementById('sFrames').textContent = s.today_frames || 0;
  const rate = s.today_frames ? Math.round((s.today_dishes || 0) / s.today_frames * 100) : 0;
  document.getElementById('sRate').textContent = rate + '%';
  document.getElementById('sSsim').textContent = s.avg_dish_confidence ? s.avg_dish_confidence.toFixed(3) : '--';
  document.getElementById('sAlerts').textContent = s.total_alerts || 0;
  if (s.hourly) updateChart(s.hourly);
}

function updateRefBtn() {
  const btn = document.getElementById('refBtn');
  if (hasReference) {
    btn.textContent = '\u2713 reference set (click to update)';
    btn.className = 'ref-btn has-ref';
  } else {
    btn.textContent = 'set clean reference';
    btn.className = 'ref-btn';
  }
}

// -- grace timer --

function startGrace(remaining) {
  const parts = remaining.replace(/\.\d+$/, '').split(':').map(Number);
  let sec = 0;
  if (parts.length === 3) sec = parts[0]*3600 + parts[1]*60 + parts[2];
  else if (parts.length === 2) sec = parts[0]*60 + parts[1];
  else sec = parts[0];

  graceTarget = Date.now() + sec * 1000;
  document.getElementById('timerBadge').textContent = 'active';
  document.getElementById('timerSub').textContent = 'until alert fires';
  if (graceTick) clearInterval(graceTick);
  graceTick = setInterval(tickGrace, 1000);
  tickGrace();
}

function tickGrace() {
  if (!graceTarget) return;
  const rem = Math.max(0, graceTarget - Date.now());
  const h = Math.floor(rem/3600000), m = Math.floor((rem%3600000)/60000), s = Math.floor((rem%60000)/1000);
  const el = document.getElementById('timerVal');
  el.textContent = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  el.className = 'timer' + (rem < 300000 ? ' urgent' : '');
  if (rem <= 0) stopGrace();
}

function stopGrace() {
  graceTarget = null;
  if (graceTick) { clearInterval(graceTick); graceTick = null; }
  document.getElementById('timerVal').textContent = '--:--';
  document.getElementById('timerVal').className = 'timer';
  document.getElementById('timerBadge').textContent = 'inactive';
  document.getElementById('timerSub').textContent = 'no active timer';
}

// -- timeline --

function addTimelineItem(item) {
  const tl = document.getElementById('timeline');
  const el = makeTimelineEl(item);
  tl.prepend(el);
  while (tl.children.length > 60) tl.removeChild(tl.lastChild);
}

function makeTimelineEl(item) {
  const el = document.createElement('div');
  el.className = 'tl-item';

  const isVideo = item.type === 'video';
  const thumbUrl = isVideo
    ? (item.thumb_url || '')
    : '/view/image/' + item.filename;
  const time = item.timestamp
    ? new Date(item.timestamp).toLocaleString([], {hour:'numeric',minute:'2-digit',month:'short',day:'numeric'})
    : item.timestamp_fmt || '';

  let tag = '';
  if (isVideo) {
    tag = '<span class="tl-tag video">\u25B6 blame clip</span>';
  } else {
    tag = item.dishes
      ? '<span class="tl-tag dirty">dirty</span>'
      : '<span class="tl-tag clean">clean</span>';
  }

  const ssimTxt = item.ssim !== undefined && item.ssim !== null ? ' ssim ' + Number(item.ssim).toFixed(3) : '';
  const labelsTxt = (item.labels || []).join(', ');

  // video thumb: use actual thumbnail if available, otherwise play icon
  const thumbHtml = (isVideo && !item.thumb_url)
    ? '<div class="tl-thumb" style="display:flex;align-items:center;justify-content:center;color:var(--accent);font-size:1.2rem">\u25B6</div>'
    : '<img class="tl-thumb" src="' + thumbUrl + '" loading="lazy">';

  el.innerHTML =
    thumbHtml +
    '<div class="tl-meta">' +
      '<div class="tl-time">' + time + '</div>' +
      '<div class="tl-status">' + (isVideo ? 'blame clip' : (item.dishes ? 'dishes detected' : 'clear') + ssimTxt) + '</div>' +
      (labelsTxt ? '<div class="tl-labels">' + labelsTxt + '</div>' : '') +
      (item.size_kb ? '<div class="tl-labels">' + item.size_kb + ' KB</div>' : '') +
    '</div>' +
    tag;

  el.onclick = () => {
    // deselect others
    document.querySelectorAll('.tl-item.active').forEach(x => x.classList.remove('active'));
    el.classList.add('active');

    if (isVideo) {
      playVideo('/view/video/' + item.filename);
    } else {
      hideVideo();
      const img = document.getElementById('feedImg');
      img.src = thumbUrl;
      img.style.display = 'block';
      document.getElementById('feedEmpty').style.display = 'none';
      document.getElementById('feedBar').style.display = 'flex';
      document.getElementById('feedTime').textContent = time;
    }
  };

  return el;
}

async function loadTimeline(mode) {
  timelineMode = mode || 'all';

  // highlight active tab
  document.getElementById('tlAll').style.color = mode === 'all' ? 'var(--accent)' : '';
  document.getElementById('tlVids').style.color = mode === 'videos' ? 'var(--accent)' : '';

  const tl = document.getElementById('timeline');
  tl.innerHTML = '';

  try {
    if (mode === 'all' || mode === 'images') {
      const r = await fetch('/view/list?limit=30');
      const imgs = await r.json();
      imgs.forEach(img => {
        addTimelineItem({
          type: 'image', filename: img.filename, timestamp_fmt: img.timestamp,
          dishes: img.dishes_found, ssim: null, labels: [],
        });
      });
    }

    if (mode === 'all' || mode === 'videos') {
      const r = await fetch('/view/videos?limit=20');
      const vids = await r.json();
      vids.forEach(v => {
        addTimelineItem({
          type: 'video', filename: v.filename, timestamp_fmt: v.timestamp,
          dishes: null, ssim: null, labels: [],
          thumb_url: v.thumb_url || null, size_kb: v.size_kb || null,
        });
      });
    }

    // sort by dom order (newest first is already handled by prepend)
  } catch(e) { console.error('loadTimeline:', e); }

  // show latest image
  if (mode !== 'videos') {
    try {
      const img = document.getElementById('feedImg');
      img.src = '/view/latest.jpg?t=' + Date.now();
      img.style.display = 'block';
      document.getElementById('feedEmpty').style.display = 'none';
      document.getElementById('feedBar').style.display = 'flex';
    } catch(e) {}
  }
}

// -- video player --

function playVideo(url) {
  const player = document.getElementById('vplayer');
  const vid = document.getElementById('vplayerVid');
  vid.src = url;
  player.classList.add('show');
  vid.play().catch(() => {});
}

function hideVideo() {
  const player = document.getElementById('vplayer');
  const vid = document.getElementById('vplayerVid');
  vid.pause();
  vid.src = '';
  player.classList.remove('show');
}

// -- events --

function addEvent(ev) {
  const list = document.getElementById('evtList');
  const el = document.createElement('div');
  el.className = 'erow';
  const time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';
  el.innerHTML =
    '<div class="edot s"></div>' +
    '<span style="color:var(--tx-1)">' + ev.from_state + ' \u2192 ' + ev.to_state + '</span>' +
    '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.65rem;color:var(--tx-2);margin-left:6px">' + (ev.reason || '') + '</span>' +
    '<span class="etime">' + time + '</span>';
  list.prepend(el);
  while (list.children.length > 30) list.removeChild(list.lastChild);
  document.getElementById('evtCount').textContent = list.children.length;
}

async function loadEvents() {
  try {
    const r = await fetch('/status/events?limit=20');
    const data = await r.json();
    document.getElementById('evtList').innerHTML = '';
    data.reverse().forEach(ev => addEvent(ev));
  } catch(e) {}
}

// -- stats polling --
setInterval(async () => {
  try {
    const r = await fetch('/status/stats');
    updateStats(await r.json());
  } catch(e) {}
}, 30000);

// -- chart --

function updateChart(hourly) {
  const labels = hourly.map(h => h.hour + ':00');
  const frames = hourly.map(h => h.frames);
  const dishes = hourly.map(h => h.dishes);

  if (!chart) {
    const ctx = document.getElementById('chart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'frames', data: frames, backgroundColor: '#ffffff12', borderColor: '#ffffff22', borderWidth: 1, borderRadius: 3, order: 2 },
          { label: 'dirty', data: dishes, backgroundColor: '#f9731644', borderColor: '#f97316', borderWidth: 1, borderRadius: 3, order: 1 },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { intersect: false, mode: 'index' },
        scales: {
          x: { grid: { color: '#ffffff08' }, ticks: { color: '#8a8b92', font: { size: 10 } } },
          y: { grid: { color: '#ffffff08' }, ticks: { color: '#8a8b92', font: { size: 10 } }, beginAtZero: true },
        },
        plugins: {
          legend: { labels: { color: '#8a8b92', font: { size: 10 }, boxWidth: 10, padding: 12 } },
          tooltip: { backgroundColor: '#1e1f22ee', titleColor: '#f0f0f2', bodyColor: '#b8b9be', borderColor: '#ffffff1a', borderWidth: 1, cornerRadius: 8, padding: 8 },
        }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = frames;
    chart.data.datasets[1].data = dishes;
    chart.update('none');
  }
}

// -- reference --

async function setReference() {
  if (!confirm(hasReference
    ? 'update the clean reference image? make sure the sink is clean right now.'
    : 'save the current view as the clean reference? make sure the sink is clean.'))
    return;

  try {
    const r = await fetch('/admin/set-reference', { method: 'POST' });
    const d = await r.json();
    if (d.status === 'ok') {
      hasReference = true;
      updateRefBtn();
      toast('reference saved' + (d.roi ? ', sink roi detected' : ''));
    } else {
      toast('failed: ' + (d.detail || d.message || 'unknown error'));
    }
  } catch(e) {
    toast('error: ' + e.message);
  }
}

// -- admin --

async function adminPost(url) {
  try {
    const r = await fetch(url, { method: 'POST' });
    const d = await r.json();
    toast(d.message || d.status || 'done');
  } catch(e) {
    toast('failed: ' + e.message);
  }
}

// -- toast --

function toast(msg) {
  const c = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 200); }, 3500);
}

// -- lightbox --

function openLb(src) {
  const isVid = src && (src.endsWith('.mp4') || src.endsWith('.avi'));
  if (isVid) {
    document.getElementById('lbImg').style.display = 'none';
    const v = document.getElementById('lbVid');
    v.src = src; v.style.display = 'block';
  } else {
    document.getElementById('lbVid').style.display = 'none';
    document.getElementById('lbImg').src = src;
    document.getElementById('lbImg').style.display = 'block';
  }
  document.getElementById('lb').classList.add('open');
}

function closeLb() {
  document.getElementById('lb').classList.remove('open');
  document.getElementById('lbVid').pause();
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLb(); });

// -- notifications --

function toggleNotify() {
  if (!notifyOn) {
    if ('Notification' in window) {
      Notification.requestPermission().then(p => {
        notifyOn = p === 'granted';
        document.getElementById('notifyBtn').classList.toggle('on', notifyOn);
        toast(notifyOn ? 'notifications on' : 'notifications blocked');
      });
    }
  } else {
    notifyOn = false;
    document.getElementById('notifyBtn').classList.remove('on');
    toast('notifications off');
  }
}

function notify(msg, imgFile) {
  if (!notifyOn) return;
  try {
    const opts = { body: msg, icon: '/icon.png' };
    if (imgFile) opts.image = '/view/image/' + imgFile;
    new Notification('dishwatcher', opts);
  } catch(e) {}
}

// -- sound --

function toggleSound() {
  soundOn = !soundOn;
  document.getElementById('soundBtn').classList.toggle('on', soundOn);
  toast(soundOn ? 'sound on' : 'sound off');
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.2, 0.4].forEach((t, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value = i < 2 ? 880 : 1100;
      g.gain.value = 0.15;
      o.start(ctx.currentTime + t);
      o.stop(ctx.currentTime + t + (i < 2 ? 0.12 : 0.3));
    });
  } catch(e) {}
}

// -- mobile panels --

function showPanel(id, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.bnav-btn').forEach(b => b.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  if (btn) btn.classList.add('on');
}

// -- go --

connectSSE();

// -- settings --

let settingsPassword = '';
let settingsUnlocked = false;
let settingsData = {};
let passwordRequired = false;

function renderSettings(schema) {
  settingsData = schema;
  const container = document.getElementById('settingsGroups');
  container.innerHTML = '';

  // group settings by their group key
  const groups = {};
  for (const [key, s] of Object.entries(schema)) {
    const g = s.group || 'other';
    if (!groups[g]) groups[g] = [];
    groups[g].push({key, ...s});
  }

  const groupOrder = ['detection', 'camera', 'video', 'timing', 'notifications', 'ui', 'admin'];
  const groupLabels = {
    detection: 'detection', camera: 'camera', video: 'video',
    timing: 'timing', notifications: 'notifications', ui: 'dashboard', admin: 'admin'
  };

  for (const gKey of groupOrder) {
    const items = groups[gKey];
    if (!items) continue;

    const groupEl = document.createElement('div');
    groupEl.className = 'settings-group';
    groupEl.innerHTML = '<div class="settings-group-title">' + (groupLabels[gKey] || gKey) + '</div>';

    for (const s of items) {
      const row = document.createElement('div');
      row.className = 'setting-row';

      let ctrl = '';
      if (s.type === 'bool') {
        ctrl = '<div class="toggle ' + (s.value ? 'on' : '') + '" data-key="' + s.key + '" onclick="toggleSetting(this)"></div>';
      } else if (s.type === 'int' || s.type === 'float') {
        const step = s.step || (s.type === 'float' ? 0.01 : 1);
        const min = s.min !== undefined ? ' min="' + s.min + '"' : '';
        const max = s.max !== undefined ? ' max="' + s.max + '"' : '';
        ctrl = '<input type="number" data-key="' + s.key + '" value="' + s.value + '" step="' + step + '"' + min + max + '>';
      } else if (s.type === 'select') {
        const opts = (s.options || []).map(o =>
          '<option value="' + o + '"' + (o === s.value ? ' selected' : '') + '>' + o + '</option>'
        ).join('');
        ctrl = '<select data-key="' + s.key + '">' + opts + '</select>';
      } else if (s.type === 'password') {
        ctrl = '<input type="password" data-key="' + s.key + '" value="' + (s.value || '') + '" placeholder="leave empty to disable">';
      } else {
        ctrl = '<input type="text" data-key="' + s.key + '" value="' + (s.value || '') + '">';
      }

      row.innerHTML =
        '<div class="setting-label">' +
          '<div class="name">' + (s.label || s.key) + '</div>' +
          (s.desc ? '<div class="desc">' + s.desc + '</div>' : '') +
        '</div>' +
        '<div class="setting-ctrl">' + ctrl + '</div>';

      groupEl.appendChild(row);
    }

    container.appendChild(groupEl);
  }
}

function toggleSetting(el) {
  el.classList.toggle('on');
}

async function unlockSettings() {
  const pw = document.getElementById('settingsPw').value;

  try {
    const r = await fetch('/config/check-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw}),
    });
    const d = await r.json();

    if (d.valid) {
      settingsPassword = pw;
      settingsUnlocked = true;
      document.getElementById('settingsLock').style.display = 'none';
      document.getElementById('settingsBody').style.display = 'block';
      loadSettings();
      toast('settings unlocked');
    } else {
      toast('wrong password');
      document.getElementById('settingsPw').value = '';
    }
  } catch(e) {
    toast('error: ' + e.message);
  }
}

async function loadSettings() {
  try {
    const r = await fetch('/config/schema');
    const schema = await r.json();
    renderSettings(schema);
    applyUiConfig(schema);
  } catch(e) {
    toast('failed to load settings');
  }
}

async function saveSettings() {
  const changes = {};

  // collect all values from the form
  document.querySelectorAll('#settingsGroups [data-key]').forEach(el => {
    const key = el.dataset.key;
    const schema = settingsData[key];
    if (!schema) return;

    if (schema.type === 'bool') {
      changes[key] = el.classList.contains('on');
    } else if (schema.type === 'int') {
      changes[key] = parseInt(el.value) || 0;
    } else if (schema.type === 'float') {
      changes[key] = parseFloat(el.value) || 0;
    } else {
      changes[key] = el.value;
    }
  });

  try {
    const r = await fetch('/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: settingsPassword, changes}),
    });
    const d = await r.json();

    if (d.status === 'ok') {
      toast('saved: ' + (d.changed || []).join(', '));
      loadSettings();
    } else {
      toast('save failed: ' + (d.detail || 'unknown'));
    }
  } catch(e) {
    toast('error: ' + e.message);
  }
}

function applyUiConfig(schema) {
  // show/hide dashboard sections based on ui_ settings
  const map = {
    'ui_show_chart': '.chart-box',
    'ui_show_consensus': '#panelStatus .card:nth-child(1)',
    'ui_show_timer': '#panelStatus .card:nth-child(2)',
    'ui_show_stats': '#panelStatus .card:nth-child(3)',
    'ui_show_events': '#panelStatus .card:nth-child(4)',
  };

  // find parent cards by content instead of nth-child (more robust)
  if (schema.ui_show_chart) {
    const chartCard = document.querySelector('.chart-box');
    if (chartCard) chartCard.closest('.card').style.display = schema.ui_show_chart.value ? '' : 'none';
  }
}

function initSettingsPanel(data) {
  passwordRequired = data.password_required || false;

  if (!passwordRequired) {
    // no password set, unlock immediately
    settingsUnlocked = true;
    document.getElementById('settingsLock').style.display = 'none';
    document.getElementById('settingsBody').style.display = 'block';
  }

  if (data.config) {
    renderSettings(data.config);
    applyUiConfig(data.config);
  }
}
