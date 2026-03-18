// state
let currentState = 'CLEAR';
let notificationsEnabled = false;
let soundEnabled = false;
let graceTarget = null;
let graceInterval = null;
let sse = null;
let reconnectDelay = 1000;

const stateEmoji = {CLEAR:'&#x2713;', DETECTED:'&#x1f50d;', CONFIRMED:'&#x23f3;', ALERTED:'&#x1f6a8;'};
const stateTitle = {
  CLEAR:'Sink is clear',
  DETECTED:'Dishes detected',
  CONFIRMED:'Dishes confirmed',
  ALERTED:'Alert! Wash your dishes'
};

// sse connection
function connectSSE() {
  if (sse) { sse.close(); }
  sse = new EventSource('/stream');

  sse.addEventListener('init', e => {
    reconnectDelay = 1000;
    setConnected(true);
    const d = JSON.parse(e.data);
    if (d.status) updateStatus(d.status);
    if (d.stats) updateStats(d.stats);
    loadHistory();
    loadEvents();
    loadGallery();
  });

  sse.addEventListener('detection', e => {
    const d = JSON.parse(e.data);
    handleDetection(d);
  });

  sse.addEventListener('state', e => {
    const d = JSON.parse(e.data);
    handleStateChange(d);
  });

  sse.addEventListener('heartbeat', () => { setConnected(true); });
  sse.addEventListener('admin', () => { toast('Admin action executed','&#x2699;'); });

  sse.onerror = () => {
    setConnected(false);
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 1.5, 15000);
      connectSSE();
    }, reconnectDelay);
  };
}

function setConnected(ok) {
  const dot = document.getElementById('connDot');
  const txt = document.getElementById('connText');
  dot.className = 'conn-dot ' + (ok ? 'live' : 'dead');
  txt.textContent = ok ? 'live' : 'reconnecting';
}

// handle detection from server
function handleDetection(d) {
  // update feed image
  if (d.image_file) {
    const img = document.getElementById('feedImg');
    const placeholder = document.getElementById('feedPlaceholder');
    const overlay = document.getElementById('feedOverlay');
    img.src = '/view/image/' + d.image_file + '?t=' + Date.now();
    img.style.display = 'block';
    placeholder.style.display = 'none';
    overlay.style.display = 'flex';
    document.getElementById('feedInference').textContent = d.inference_ms.toFixed(1) + ' ms';
    document.getElementById('feedTime').textContent = new Date(d.timestamp).toLocaleTimeString();
    document.getElementById('feedMode').textContent = d.capture_mode;
  }

  // update consensus
  if (d.consensus) updateConsensus(d.consensus);

  // update state
  if (d.state) updateStateDisplay(d.state, d);

  // update grace timer
  if (d.grace_remaining && d.grace_remaining !== 'None') {
    startGraceTimer(d.grace_remaining, d.dishes_since);
  } else if (d.state === 'CLEAR') {
    stopGraceTimer();
  }

  // prepend to detection list
  prependDetection(d);

  // browser notification if alert
  if (d.should_alert) {
    notify('Dishes have been in the sink too long!', d.image_file);
    if (soundEnabled) playAlertSound();
  }
}

function handleStateChange(d) {
  if (d.status) updateStatus(d.status);
  const prev = d.previous_state || '?';
  const next = d.state;
  toast(prev + ' → ' + next, stateEmoji[next] || '&#x27a1;');

  if (next === 'ALERTED') {
    notify('ALERT: Dishes need washing!');
    if (soundEnabled) playAlertSound();
  } else if (next === 'CLEAR' && (prev === 'CONFIRMED' || prev === 'ALERTED')) {
    notify('Dishes cleared! Sink is clean.');
  }

  prependEvent({timestamp: new Date().toISOString(), from_state: prev, to_state: next, reason: d.reason || ''});
}

// ui updates
function updateStatus(s) {
  updateStateDisplay(s.state, s);
  if (s.consensus) updateConsensus(s.consensus);
}

function updateStateDisplay(state, data) {
  currentState = state;
  const orb = document.getElementById('stateOrb');
  orb.className = 'state-orb ' + state;
  orb.innerHTML = stateEmoji[state] || '?';

  document.getElementById('stateTitle').textContent = stateTitle[state] || state;

  let sub = '';
  if (data && data.dishes_since) {
    const since = new Date(data.dishes_since);
    const mins = Math.round((Date.now() - since.getTime()) / 60000);
    sub = 'Dishes since ' + since.toLocaleTimeString() + ' (' + mins + ' min ago)';
  } else if (state === 'CLEAR') {
    sub = 'No dishes detected';
  }
  document.getElementById('stateSub').textContent = sub;
}

function updateConsensus(c) {
  const dots = document.getElementById('consensusDots');
  let html = '';
  for (let i = 0; i < c.window; i++) {
    if (i < c.buffer.length) {
      html += '<div class="cbuf-dot ' + (c.buffer[i] ? 'pos' : 'neg') + '">' + (c.buffer[i] ? '&#x2713;' : '&#x2717;') + '</div>';
    } else {
      html += '<div class="cbuf-dot empty"></div>';
    }
  }
  dots.innerHTML = html;

  document.getElementById('consensusRatio').textContent = c.positive + '/' + c.window;
  const pct = Math.round(c.confidence * 100);
  document.getElementById('consensusPct').textContent = pct + '%';

  const fill = document.getElementById('consensusFill');
  fill.style.width = pct + '%';
  if (pct >= 70) fill.style.background = 'var(--orange)';
  else if (pct >= 40) fill.style.background = 'var(--yellow)';
  else fill.style.background = 'var(--green)';
}

function updateStats(s) {
  document.getElementById('statFrames').textContent = s.today_frames || 0;
  const rate = s.today_frames ? Math.round((s.today_dishes || 0) / s.today_frames * 100) : 0;
  document.getElementById('statDishRate').textContent = rate + '%';
  document.getElementById('statInference').textContent = (s.avg_inference_ms || 0).toFixed(1) + ' ms';
  document.getElementById('statAlerts').textContent = s.total_alerts || 0;
  document.getElementById('statHour').textContent = (s.hour_frames || 0) + ' frames';
  document.getElementById('statTransitions').textContent = s.total_transitions || 0;

  if (s.hourly) updateChart(s.hourly);
}

// grace timer countdown
function startGraceTimer(remaining, dishesSince) {
  // parse remaining like "1:23:45" or "0:45:12.345"
  const parts = remaining.replace(/\.\d+$/,'').split(':').map(Number);
  let totalSec = 0;
  if (parts.length === 3) totalSec = parts[0]*3600 + parts[1]*60 + parts[2];
  else if (parts.length === 2) totalSec = parts[0]*60 + parts[1];
  else totalSec = parts[0];

  graceTarget = Date.now() + totalSec * 1000;

  document.getElementById('timerState').textContent = 'active';
  document.getElementById('timerSub').textContent = 'Until alert fires';

  if (graceInterval) clearInterval(graceInterval);
  graceInterval = setInterval(tickGrace, 1000);
  tickGrace();
}

function tickGrace() {
  if (!graceTarget) return;
  const rem = Math.max(0, graceTarget - Date.now());
  const h = Math.floor(rem / 3600000);
  const m = Math.floor((rem % 3600000) / 60000);
  const s = Math.floor((rem % 60000) / 1000);
  const display = document.getElementById('timerDisplay');
  display.textContent = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  display.className = 'timer' + (rem < 300000 ? ' urgent' : '');
  if (rem <= 0) stopGraceTimer();
}

function stopGraceTimer() {
  graceTarget = null;
  if (graceInterval) { clearInterval(graceInterval); graceInterval = null; }
  document.getElementById('timerDisplay').textContent = '--:--';
  document.getElementById('timerDisplay').className = 'timer';
  document.getElementById('timerState').textContent = 'inactive';
  document.getElementById('timerSub').textContent = 'No active timer';
}

// detection + event lists
function prependDetection(d) {
  const list = document.getElementById('detList');
  const el = document.createElement('div');
  el.className = 'list-row';
  const icon = d.dishes_found ? 'dishes' : 'clear';
  const labels = (d.labels || []).map(l => '<span>' + l + '</span>').join('');
  const time = new Date(d.timestamp).toLocaleTimeString();
  el.innerHTML = '<div class="list-icon ' + icon + '"></div>'
    + '<span style="color:var(--tx-1);font-size:.78rem">' + (d.dishes_found ? d.detection_count + ' dish' + (d.detection_count !== 1 ? 'es' : '') : 'Clear') + '</span>'
    + '<div class="list-labels">' + labels + '</div>'
    + (d.capture_mode ? '<span style="font-family:var(--font-mono);font-size:.62rem;color:var(--tx-3)">' + d.capture_mode + '</span>' : '')
    + '<span class="list-time">' + time + '</span>';
  list.prepend(el);

  // cap at 50
  while (list.children.length > 50) list.removeChild(list.lastChild);
  document.getElementById('detCount').textContent = list.children.length;
}

function prependEvent(ev) {
  const list = document.getElementById('eventList');
  const el = document.createElement('div');
  el.className = 'list-row';
  const time = new Date(ev.timestamp).toLocaleTimeString();
  el.innerHTML = '<div class="list-icon state"></div>'
    + '<span class="state-tag ' + ev.to_state + '">' + ev.from_state + ' → ' + ev.to_state + '</span>'
    + '<span style="font-size:.72rem;color:var(--tx-2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (ev.reason || '') + '</span>'
    + '<span class="list-time">' + time + '</span>';
  list.prepend(el);
  while (list.children.length > 30) list.removeChild(list.lastChild);
  document.getElementById('eventCount').textContent = list.children.length;
}

// load initial data from api
async function loadHistory() {
  try {
    const r = await fetch('/status/history?limit=30');
    const data = await r.json();
    const list = document.getElementById('detList');
    list.innerHTML = '';
    data.reverse().forEach(d => {
      prependDetection({
        timestamp: d.timestamp, dishes_found: !!d.dishes_found,
        detection_count: d.detection_count || 0,
        labels: d.labels ? d.labels.split(',').filter(Boolean) : [],
        capture_mode: '', image_file: d.image_file,
      });
    });
  } catch(e) { console.error('loadHistory:', e); }
}

async function loadEvents() {
  try {
    const r = await fetch('/status/events?limit=20');
    const data = await r.json();
    const list = document.getElementById('eventList');
    list.innerHTML = '';
    data.reverse().forEach(ev => prependEvent(ev));
  } catch(e) { console.error('loadEvents:', e); }
}

async function loadGallery() {
  try {
    const r = await fetch('/view/list?limit=20');
    const data = await r.json();
    const g = document.getElementById('gallery');
    g.innerHTML = '';
    data.forEach((img, i) => {
      const el = document.createElement('img');
      el.className = 'gallery-thumb' + (i === 0 ? ' active' : '');
      el.src = img.url;
      el.loading = 'lazy';
      el.onclick = () => {
        document.getElementById('feedImg').src = img.url;
        document.getElementById('feedImg').style.display = 'block';
        document.getElementById('feedPlaceholder').style.display = 'none';
        document.getElementById('feedOverlay').style.display = 'flex';
        document.getElementById('feedTime').textContent = img.timestamp;
        g.querySelectorAll('.gallery-thumb').forEach(t => t.classList.remove('active'));
        el.classList.add('active');
      };
      g.appendChild(el);
    });

    // show latest
    if (data.length > 0) {
      const img = document.getElementById('feedImg');
      img.src = data[0].url;
      img.style.display = 'block';
      document.getElementById('feedPlaceholder').style.display = 'none';
      document.getElementById('feedOverlay').style.display = 'flex';
      document.getElementById('feedTime').textContent = data[0].timestamp;
    }
  } catch(e) { console.error('loadGallery:', e); }
}

// poll stats every 30s
setInterval(async () => {
  try {
    const r = await fetch('/status/stats');
    updateStats(await r.json());
  } catch(e) {}
}, 30000);

// chart
let chart = null;
function updateChart(hourly) {
  const labels = hourly.map(h => h.hour + ':00');
  const frames = hourly.map(h => h.frames);
  const dishes = hourly.map(h => h.dishes);

  if (!chart) {
    const ctx = document.getElementById('timelineChart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: 'Total frames',
            data: frames,
            backgroundColor: '#ffffff12',
            borderColor: '#ffffff22',
            borderWidth: 1,
            borderRadius: 3,
            order: 2,
          },
          {
            label: 'Dishes detected',
            data: dishes,
            backgroundColor: '#f9731644',
            borderColor: '#f97316',
            borderWidth: 1,
            borderRadius: 3,
            order: 1,
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: 'index' },
        scales: {
          x: {
            grid: { color: '#ffffff08' },
            ticks: { color: '#8a8b92', font: { family: "'JetBrains Mono'", size: 10 } },
          },
          y: {
            grid: { color: '#ffffff08' },
            ticks: { color: '#8a8b92', font: { family: "'JetBrains Mono'", size: 10 } },
            beginAtZero: true,
          }
        },
        plugins: {
          legend: {
            labels: { color: '#8a8b92', font: { family: "'JetBrains Mono'", size: 11 }, boxWidth: 12, padding: 16 }
          },
          tooltip: {
            backgroundColor: '#1e1f22ee',
            titleColor: '#f0f0f2',
            bodyColor: '#b8b9be',
            borderColor: '#ffffff1a',
            borderWidth: 1,
            titleFont: { family: "'JetBrains Mono'" },
            bodyFont: { family: "'JetBrains Mono'" },
            cornerRadius: 8,
            padding: 10,
          }
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

// toast notifications
function toast(msg, icon) {
  const c = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = '<span class="toast-icon">' + (icon || '&#x2139;') + '</span>' + msg;
  c.appendChild(el);
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 300); }, 4000);
}

// lightbox
function openLightbox(src) {
  document.getElementById('lightboxImg').src = src;
  document.getElementById('lightbox').classList.add('open');
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

// browser notifications
function toggleNotifications() {
  if (!notificationsEnabled) {
    if ('Notification' in window) {
      Notification.requestPermission().then(p => {
        notificationsEnabled = p === 'granted';
        document.getElementById('notifyBtn').classList.toggle('active', notificationsEnabled);
        toast(notificationsEnabled ? 'Notifications enabled' : 'Notifications blocked', '&#x1f514;');
      });
    } else {
      toast('Browser does not support notifications', '&#x26a0;');
    }
  } else {
    notificationsEnabled = false;
    document.getElementById('notifyBtn').classList.remove('active');
    toast('Notifications disabled', '&#x1f515;');
  }
}

function notify(msg, imageFile) {
  if (!notificationsEnabled) return;
  try {
    const opts = { body: msg, icon: '/icon.png', badge: '/icon.png' };
    if (imageFile) opts.image = '/view/image/' + imageFile;
    new Notification('Dish Watcher', opts);
  } catch(e) { console.error('Notification error:', e); }
}

// alert sound (web audio api, no files needed)
function toggleSound() {
  soundEnabled = !soundEnabled;
  document.getElementById('soundBtn').classList.toggle('active', soundEnabled);
  toast(soundEnabled ? 'Sound alerts on' : 'Sound alerts off', '&#x1f50a;');
}

function playAlertSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    // beep beep beeeep
    [0, 0.2, 0.4].forEach((delay, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = i < 2 ? 880 : 1100;
      gain.gain.value = 0.15;
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + (i < 2 ? 0.12 : 0.3));
    });
  } catch(e) {}
}

// admin actions
async function adminAction(url, method) {
  try {
    const r = await fetch(url, { method });
    const d = await r.json();
    toast(d.message || d.status || 'Done', '&#x2699;');
  } catch(e) {
    toast('Action failed: ' + e.message, '&#x26a0;');
  }
}

// go
connectSSE();
