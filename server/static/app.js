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
const CACHE_KEY = "dishwatcher.snapshot.v3";

let lastValues = {};
let lastGoodAt = null;
let firstPaintDone = false;
let currentView = "overview";
let clipsCache = [];
let clipFilter = "all";
let clipLimit = 40;
let selectedClip = null;
let snapshot = null;
let snapshotReceived = 0;
let toastTimer;
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}

/* ---------- theme ---------- */

(function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem("dishwatcher.theme");
  } catch (e) {
    /* private mode */
  }
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  $("theme").addEventListener("click", () => {
    const now =
      document.documentElement.getAttribute("data-theme") === "dark"
        ? "light"
        : "dark";
    document.documentElement.setAttribute("data-theme", now);
    try {
      localStorage.setItem("dishwatcher.theme", now);
    } catch (e) {
      /* ignore */
    }
  });
})();

/* ---------- helpers ---------- */

function setText(id, value, { animate = true } = {}) {
  const el = $(id);
  if (!el) return;
  const str =
    value === null || value === undefined || value === ""
      ? "--"
      : String(value);
  el.classList.remove("skeleton");
  if (lastValues[id] === str) return; // unchanged: do nothing at all
  el.textContent = str;
  if (animate && firstPaintDone) {
    // never replay entrance on refresh
    el.classList.remove("changed");
    void el.offsetWidth;
    el.classList.add("changed");
  }
  lastValues[id] = str;
}

function humanDuration(sec) {
  if (sec === null || sec === undefined) return "--";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h >= 24) return Math.floor(h / 24) + "d " + (h % 24) + "h";
  const rm = m % 60;
  return rm ? `${h}h ${rm}m` : `${h}h`;
}

/* "say it in words where words are clearer" */
function describeState(state) {
  switch (state) {
    case "CLEAR":
      return {
        word: "Clean",
        label: "nothing in the sink",
        cls: "state-clear",
      };
    case "CONFIRMED":
      return {
        word: "Dishes",
        label: "waiting out the grace period",
        cls: "state-dirty",
      };
    case "ALERTED":
      return {
        word: "Overdue",
        label: "Dishes are ready for a cleanup.",
        cls: "state-alerted",
      };
    default:
      return {
        word: "Not ready",
        label: "Set up detection to start watching.",
        cls: "state-unknown",
      };
  }
}

function pill(el, kind, text) {
  el.className = "pill pill-" + kind;
  el.innerHTML = '<span class="dot"></span>' + text; // colour never travels alone
}

/* ---------- rendering ---------- */

function render(d, fresh = true) {
  snapshot = d;
  if (fresh) snapshotReceived = Date.now();
  // calibration first: if this is bad, nothing else on the page is meaningful
  const cal = d.calibration || {};
  const banner = $("calib-banner");
  if (cal.valid) {
    banner.hidden = true;
  } else {
    banner.hidden = false;
    $("calib-reason").textContent = cal.reason || "unknown reason";
  }

  // hero
  const st = describeState(cal.valid ? d.state : null);
  $("hero").className = "hero " + st.cls;
  setText("state-value", st.word);
  setText("state-label", st.label, { animate: false });
  setText("since", humanDuration(d.seconds_in_state));

  /* The state machine needs a majority of recent frames to agree before it
   * moves. Without showing that, a fresh dirty reading next to a "Clean" hero
   * looks like a contradiction rather than a vote in progress. */
  const cons = d.consensus;
  if (cons && cons.size) {
    const votes = cons.positive;
    setText("consensus", `${votes} of ${cons.size} see dishes`);
  } else {
    setText("consensus", "no frames yet");
  }
  setText(
    "alert-in",
    d.seconds_until_alert === null || d.seconds_until_alert === undefined
      ? d.state === "ALERTED"
        ? "Already sent"
        : "None scheduled"
      : humanDuration(d.seconds_until_alert),
  );

  // detector
  if (cal.valid && d.ssim_score !== null && d.ssim_score !== undefined) {
    setText("ssim-value", d.ssim_score.toFixed(3));
  } else {
    setText("ssim-value", "----");
  }
  setText(
    "threshold",
    d.ssim_threshold !== undefined ? d.ssim_threshold.toFixed(2) : "--",
  );
  setText(
    "labels",
    d.labels && d.labels.length ? d.labels.join(", ") : "nothing recognised",
  );
  setText(
    "latency",
    d.inference_ms !== undefined && d.inference_ms !== null
      ? Math.round(d.inference_ms) + " ms"
      : "--",
  );

  renderHeat(d.ssim_tiles, d.ssim_threshold);

  // camera health
  const cam = d.camera || {};
  const link = $("cam-link");
  if (!d.camera_seen) pill(link, "mute", "never seen");
  else if (cam.report_age_seconds > 90) pill(link, "bad", "Offline");
  else if (cam.healthy === false) pill(link, "bad", "Needs attention");
  else pill(link, "ok", "Connected");
  link.classList.remove("skeleton");

  setText(
    "cam-last",
    cam.seconds_since_last_frame !== undefined
      ? humanDuration(cam.seconds_since_last_frame) + " ago"
      : "--",
  );
  setText("cam-reopens", cam.reopens !== undefined ? cam.reopens : "--");
  setText("cam-motion", cam.motion_state || "--");
  // v1 sat at 23.7; show it plainly so a regression is visible
  setText(
    "cam-flap",
    cam.flap_ratio !== undefined ? cam.flap_ratio.toFixed(2) : "--",
  );
  setText(
    "cam-temp",
    cam.temperature_c != null
      ? cam.temperature_c.toFixed(1) + "°C"
      : "Not reported",
  );
  setText(
    "cam-preview",
    cam.preview_fps_limit === 0
      ? "Paused to cool down"
      : cam.preview_fps_limit != null
        ? "Up to " + cam.preview_fps_limit + " fps"
        : "Up to 2 fps",
  );

  // frame
  if (d.latest_frame_url && !roiEditing) {
    const img = $("frame");
    if (img.dataset.src !== d.latest_frame_url) {
      img.onload = () => {
        img.dataset.src = d.latest_frame_url;
        if (!liveOn || !$("live").getAttribute("src")) img.hidden = false;
        $("camera-empty").hidden = true;
      };
      img.onerror = () => {
        $("live-message").textContent =
          "Capture unavailable. Retrying on the next update.";
      };
      img.src = d.latest_frame_url + "?t=" + Date.now();
    }
  }
  if (!liveOn)
    $("frame-age").textContent =
      d.latest_frame_age_seconds !== undefined &&
      d.latest_frame_age_seconds !== null
        ? "captured " + humanDuration(d.latest_frame_age_seconds) + " ago"
        : "";

  // the reference, so a bad calibration is visible instead of implicit
  if (cal.valid && currentView === "setup") {
    const stamp = cal.reference_shape ? cal.reference_shape.join("x") : "";
    const full = $("ref-full"),
      roi = $("ref-roi");
    if (full.dataset.stamp !== stamp) {
      full.dataset.stamp = stamp;
      full.src = "/calibration/reference.jpg?t=" + Date.now();
      roi.src = "/calibration/reference.jpg?roi_only=1&t=" + Date.now();
    }
    $("ref-note").textContent =
      cal.roi && cal.roi.sink ? "sink area " + cal.roi.sink.join(", ") : "";
  } else if (!cal.valid) {
    $("ref-full").removeAttribute("src");
    $("ref-roi").removeAttribute("src");
    $("ref-note").textContent = "no reference set";
  }

  renderEvents(d.events || []);

  if (fresh) lastGoodAt = Date.now();
  firstPaintDone = true;
  if (fresh)
    try {
      localStorage.setItem(
        CACHE_KEY,
        JSON.stringify({ data: d, savedAt: lastGoodAt }),
      );
    } catch (e) {
      /* ignore */
    }
}

function renderHeat(tiles, threshold) {
  const el = $("heat");
  if (!tiles || !tiles.length) {
    if (!el.dataset.empty) {
      el.innerHTML = "";
      el.dataset.empty = "1";
    }
    return;
  }
  delete el.dataset.empty;
  const g = Math.round(Math.sqrt(tiles.length));
  el.style.gridTemplateColumns = `repeat(${g}, 1fr)`;

  const sig = tiles.map((t) => t.score.toFixed(2)).join(",");
  if (el.dataset.sig === sig) return; // unchanged: do not repaint
  el.dataset.sig = sig;

  el.innerHTML = "";
  const th = threshold || 0.82;
  for (const t of tiles) {
    const cell = document.createElement("i");
    // below threshold reads as "changed"; above ramps opacity by how close it is
    if (t.score < th) {
      cell.style.background = "var(--warn)";
    } else {
      const room = Math.max(0.0001, 1 - th);
      const frac = Math.min(1, Math.max(0, (t.score - th) / room));
      cell.style.background = "var(--ok)";
      cell.style.opacity = (0.25 + 0.75 * frac).toFixed(2);
    }
    cell.title = `${t.score.toFixed(3)}`;
    el.appendChild(cell);
  }
}

function renderEvents(events) {
  const el = $("events");
  events = events.filter((e) => !e.message?.startsWith("retention removed"));
  if (!events.length) {
    delete el.dataset.sig;
    if (el.dataset.state !== "empty") {
      el.innerHTML = '<div class="empty">No events yet</div>';
      el.dataset.state = "empty";
    }
    return;
  }
  const sig = events.map((e) => e.id || e.at + e.kind).join("|");
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.dataset.state = "list";

  el.innerHTML = "";
  for (const e of events.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "event";

    const when = document.createElement("span");
    when.className = "when";
    when.textContent = e.at
      ? new Date(e.at).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";

    const what = document.createElement("span");
    what.className = "what";
    what.textContent = e.message || e.kind || "";

    row.appendChild(when);
    row.appendChild(what);

    // "a badge only when the status is not the default"
    if (e.kind && e.kind !== "info") {
      const p = document.createElement("span");
      const kind =
        e.kind === "alert" ? "bad" : e.kind === "dirty" ? "warn" : "mute";
      pill(p, kind, e.kind);
      row.appendChild(p);
    }
    el.appendChild(row);
  }
}

/* ---------- connection: SSE with polling fallback ---------- */

function markStale() {
  if (!lastGoodAt) {
    pill($("conn"), "warn", "Reconnecting");
    return;
  }
  const age = Math.round((Date.now() - lastGoodAt) / 1000);
  // stale data + a quiet note, not an error screen
  pill($("conn"), "warn", "updated " + humanDuration(age) + " ago");
}

let pollTimer = null;
let sse = null;

function startPolling(why) {
  if (pollTimer) return; // already polling
  if (sse) {
    try {
      sse.close();
    } catch (e) {}
    sse = null;
  }
  console.info("falling back to polling:", why);
  const tick = async () => {
    try {
      if (document.hidden) return;
      const r = await fetch("/status", {
        credentials: "same-origin",
        signal: AbortSignal.timeout(10000),
        cache: "no-store",
      });
      if (r.ok) {
        render(await r.json());
        pill($("conn"), "ok", "Connected");
      } else markStale();
    } catch (e) {
      markStale();
    } finally {
      pollTimer = setTimeout(tick, 5000);
    }
  };
  tick();
}

/* ---------- actions ---------- */

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

$("set-ref").addEventListener("click", async (e) => {
  if (
    !confirm("Save the latest capture as clean? Make sure the sink is empty.")
  )
    return;
  e.target.disabled = true;
  try {
    const res = await post("/calibration/reference");
    $("ref-full").dataset.stamp = "";
    await refreshStatus();
    toast("Clean reference saved");
    if (!res.valid)
      alert(
        "Reference saved, but calibration is still incomplete:\n\n" +
          res.reason,
      );
  } catch (err) {
    alert("Could not set reference: " + err.message);
  } finally {
    e.target.disabled = false;
  }
});

$("clear-calib").addEventListener("click", async (e) => {
  if (
    !confirm(
      "Clear the reference and sink area? Detection stops until you set them again.",
    )
  )
    return;
  e.target.disabled = true;
  try {
    await post("/calibration/clear");
    await refreshStatus();
  } catch (err) {
    alert(err.message);
  } finally {
    e.target.disabled = false;
  }
});

// Declared up here because the sink-area editor below turns live view off
// before drawing, and `let` is not hoisted: referencing it from the editor
// while the declaration sat lower down threw a ReferenceError that killed
// every handler after it.
let liveOn = false;

/* ---------- sink area editor ----------
 *
 * Setting the ROI used to mean typing four pixel coordinates into a prompt.
 * That is unpleasant, and a wrong box breaks detection without saying so, which
 * is the same class of failure as the uncalibrated detector. Now you drag a box
 * on the actual frame.
 *
 * The canvas is displayed at whatever size the layout gives it, but the ROI has
 * to be in the frame's own pixels, so every coordinate is scaled by the ratio
 * between the natural image size and the rendered size. Getting that wrong
 * yields a box that looks right and detects the wrong region.
 */

let roiEditing = false;
let roiStart = null;
let roiBox = null;

function frameEl() {
  return $("frame");
}

function roiScale() {
  const img = frameEl();
  const r = img.getBoundingClientRect();
  if (!img.naturalWidth || !r.width) return null;
  return {
    sx: img.naturalWidth / r.width,
    sy: img.naturalHeight / r.height,
    rect: r,
  };
}

function drawRoi() {
  const cv = $("roi-canvas");
  const img = frameEl();
  const r = img.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr);
  cv.height = Math.round(r.height * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, r.width, r.height);

  // dim everything outside the box so the chosen region reads clearly
  ctx.fillStyle = "rgba(0,0,0,0.45)";
  ctx.fillRect(0, 0, r.width, r.height);

  if (!roiBox) return;
  const { x1, y1, x2, y2 } = roiBox;
  ctx.clearRect(x1, y1, x2 - x1, y2 - y1);

  const accent =
    getComputedStyle(document.documentElement)
      .getPropertyValue("--primary")
      .trim() || "#ffb693";
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

  const s = roiScale();
  if (s) {
    const w = Math.round((x2 - x1) * s.sx),
      h = Math.round((y2 - y1) * s.sy);
    ctx.fillStyle = accent;
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.fillText(`${w} x ${h}px`, x1 + 6, Math.max(14, y1 - 6));
  }
}

function pointIn(ev) {
  const r = $("roi-canvas").getBoundingClientRect();
  const p = ev.touches ? ev.touches[0] : ev;
  return {
    x: Math.max(0, Math.min(r.width, p.clientX - r.left)),
    y: Math.max(0, Math.min(r.height, p.clientY - r.top)),
  };
}

function startRoiEdit() {
  const img = frameEl();
  if (!img.naturalWidth) {
    return alert("No frame to draw on yet. Wait for the camera to send one.");
  }
  roiEditing = true;
  roiBox = null;
  if (liveOn) setLive(false); // draw on a still, not a moving picture
  $("live-toggle").disabled = true;
  $("roi-canvas").hidden = false;
  $("roi-hint").hidden = false;
  $("roi-save").hidden = false;
  $("roi-cancel").hidden = false;
  $("set-roi").hidden = true;
  drawRoi();
}

function endRoiEdit() {
  roiEditing = false;
  roiStart = null;
  roiBox = null;
  $("roi-canvas").hidden = true;
  $("roi-hint").hidden = true;
  $("roi-save").hidden = true;
  $("roi-cancel").hidden = true;
  $("set-roi").hidden = false;
  $("live-toggle").disabled = false;
}

(function wireRoi() {
  const cv = $("roi-canvas");

  const down = (ev) => {
    if (!roiEditing) return;
    ev.preventDefault();
    roiStart = pointIn(ev);
    roiBox = null;
  };
  const move = (ev) => {
    if (!roiEditing || !roiStart) return;
    ev.preventDefault();
    const p = pointIn(ev);
    roiBox = {
      x1: Math.min(roiStart.x, p.x),
      y1: Math.min(roiStart.y, p.y),
      x2: Math.max(roiStart.x, p.x),
      y2: Math.max(roiStart.y, p.y),
    };
    drawRoi();
  };
  const up = () => {
    roiStart = null;
  };

  cv.addEventListener("mousedown", down);
  cv.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
  cv.addEventListener("touchstart", down, { passive: false });
  cv.addEventListener("touchmove", move, { passive: false });
  window.addEventListener("touchend", up);
  window.addEventListener("resize", () => {
    // A draft uses rendered pixels; do not save stale coordinates after rotation.
    if (roiEditing) {
      roiStart = null;
      roiBox = null;
      drawRoi();
    }
  });
})();

$("set-roi").addEventListener("click", startRoiEdit);
$("roi-cancel").addEventListener("click", endRoiEdit);

$("roi-save").addEventListener("click", async (e) => {
  if (!roiBox) return alert("Drag a box around the sink first.");
  const s = roiScale();
  if (!s)
    return alert("Could not measure the frame. Try again once it has loaded.");

  // canvas pixels -> frame pixels; the server stores the frame's coordinates
  const sink = [
    Math.round(roiBox.x1 * s.sx),
    Math.round(roiBox.y1 * s.sy),
    Math.round(roiBox.x2 * s.sx),
    Math.round(roiBox.y2 * s.sy),
  ];
  if (sink[2] - sink[0] < 32 || sink[3] - sink[1] < 32) {
    return alert(
      "That box is too small to compare reliably. Draw a bigger one.",
    );
  }

  e.target.disabled = true;
  try {
    const res = await post("/calibration/roi", { sink });
    if (!res.valid)
      alert("Saved, but calibration is still incomplete:\n\n" + res.reason);
    endRoiEdit();
    $("ref-full").dataset.stamp = "";
    await refreshStatus();
  } catch (err) {
    alert("Could not save the sink area: " + err.message);
  } finally {
    e.target.disabled = false;
  }
});

/* ---------- roommates + blame clips ---------- */

let peopleCache = [];

function initials(name) {
  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0] || "")
    .join("")
    .toUpperCase();
}

function renderRoster(list, counts) {
  peopleCache = list;
  const el = $("roster");
  const sig = JSON.stringify([list, counts]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;

  if (!list.length) {
    el.innerHTML =
      '<div class="empty">Nobody added yet. Add a roommate to start tagging clips.</div>';
    return;
  }
  el.innerHTML = "";
  for (const p of list) {
    const row = document.createElement("div");
    row.className = "person";

    if (p.photo_url) {
      const img = document.createElement("img");
      img.className = "avatar";
      img.src = p.photo_url;
      img.alt = p.name;
      // Keep the name readable if the photo has been removed.
      img.onerror = () => {
        const ph = document.createElement("div");
        ph.className = "avatar placeholder";
        ph.textContent = initials(p.name);
        ph.title = "Photo unavailable";
        img.replaceWith(ph);
      };
      row.appendChild(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "avatar placeholder";
      ph.textContent = initials(p.name);
      row.appendChild(ph);
    }

    const who = document.createElement("div");
    who.className = "who";
    const nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = p.name;
    const ct = document.createElement("div");
    ct.className = "ct";
    const n = counts[p.name] || 0;
    // say it in words; "0 clips" reads worse than "nothing pinned on them"
    ct.textContent =
      n === 0 ? "nothing pinned on them" : n === 1 ? "1 clip" : n + " clips";
    who.appendChild(nm);
    who.appendChild(ct);
    row.appendChild(who);

    const acts = document.createElement("div");
    acts.className = "acts";

    const photoBtn = document.createElement("button");
    photoBtn.className = "iconbtn";
    photoBtn.textContent = p.photo_url ? "change photo" : "add photo";
    photoBtn.onclick = () => pickPhoto(p.id);
    acts.appendChild(photoBtn);

    const del = document.createElement("button");
    del.className = "iconbtn";
    del.textContent = "remove";
    del.onclick = async () => {
      if (!confirm("Remove " + p.name + "? Their clip tags go too.")) return;
      await fetch("/people/" + p.id, {
        method: "DELETE",
        credentials: "same-origin",
      });
      loadPeople();
      loadClips();
    };
    acts.appendChild(del);

    row.appendChild(acts);
    el.appendChild(row);
  }
}

function pickPhoto(pid) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = async () => {
    if (!input.files || !input.files[0]) return;
    const fd = new FormData();
    fd.append("photo", input.files[0]);
    try {
      const r = await fetch("/people/" + pid + "/photo", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error(await r.text());
      loadPeople();
    } catch (e) {
      alert("Could not upload the photo: " + e.message);
    }
  };
  input.click();
}

function renderClips(clips) {
  clipsCache = clips;
  setText("nav-clip-count", clips.length, { animate: false });
  const filtered = clips.filter(
    (c) => clipFilter === "all" || (clipFilter === "tagged" ? !!c.tag : !c.tag),
  );
  const visible = currentView === "overview" ? filtered.slice(0, 6) : filtered;
  setText("clip-count", filtered.length, { animate: false });
  $("more-clips").hidden =
    currentView !== "clips" || clips.length < clipLimit || clipLimit >= 200;
  const el = $("clips");
  const sig = JSON.stringify([currentView, clipFilter, visible]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = clips.length
      ? "No " +
        clipFilter +
        " clips in this batch. Try another filter or load more."
      : "No clips yet. Your next sink visit will appear here.";
    el.append(empty);
    return;
  }
  for (const c of visible) {
    const card = document.createElement("article");
    card.className = "clip";
    const preview = document.createElement("button");
    preview.className = "clip-preview";
    preview.setAttribute("aria-label", "Play clip from " + c.timestamp);
    if (c.thumb_url) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.src = c.thumb_url;
      img.alt = "";
      img.onerror = () => img.remove();
      preview.append(img);
    }
    const play = document.createElement("span");
    play.className = "clip-play";
    play.innerHTML = '<span aria-hidden="true">▶</span>';
    preview.append(play);
    preview.onclick = () => openClip(c);
    const meta = document.createElement("div");
    meta.className = "meta";
    const when = document.createElement("div");
    when.className = "when";
    when.textContent = c.timestamp;
    const tags = document.createElement("div");
    tags.className = "tagrow";
    const person = document.createElement("span");
    person.textContent = c.tag ? c.tag.name : "Not tagged yet";
    const size = document.createElement("span");
    size.textContent = c.size_kb
      ? (c.size_kb / 1024).toFixed(1) + " MB"
      : "Sink visit";
    tags.append(person, size);
    meta.append(when, tags);
    card.append(preview, meta);
    el.append(card);
  }
}

function openClip(clip) {
  selectedClip = clip;
  setLive(false);
  const vid = $("clip-player");
  $("clip-dialog-title").textContent = clip.timestamp || "Sink visit";
  $("player-error").hidden = true;
  vid.poster = clip.thumb_url || "";
  vid.src = clip.url;
  $("clip-original").href = clip.url;
  updateClipPeople();
  $("clip-dialog").showModal();
  vid.play().catch(() => {
    /* Native play control remains available. */
  });
}

function updateClipPeople() {
  if (!selectedClip) return;
  const sel = $("clip-person");
  sel.replaceChildren(new Option("Nobody tagged", ""));
  for (const person of peopleCache) sel.add(new Option(person.name, person.id));
  sel.value = selectedClip.tag?.person_id || "";
}

$("close-clip").onclick = () => $("clip-dialog").close();
$("clip-dialog").addEventListener("close", () => {
  const vid = $("clip-player");
  vid.pause();
  vid.removeAttribute("src");
  vid.load();
  selectedClip = null;
});
$("clip-player").onerror = () => {
  if (selectedClip) $("player-error").hidden = false;
};
$("clip-person").onchange = async () => {
  if (!selectedClip) return;
  const sel = $("clip-person");
  sel.disabled = true;
  try {
    await post("/clips/" + encodeURIComponent(selectedClip.filename) + "/tag", {
      person_id: sel.value || null,
    });
    selectedClip.tag = sel.value
      ? {
          person_id: sel.value,
          name: peopleCache.find((p) => p.id === sel.value)?.name,
        }
      : null;
    toast(sel.value ? "Roommate tagged" : "Tag cleared");
    await Promise.all([loadClips(), loadPeople()]);
  } catch (e) {
    updateClipPeople();
    toast("Could not save the tag: " + e.message);
  } finally {
    sel.disabled = false;
  }
};

async function loadPeople() {
  try {
    const r = await fetch("/people", { credentials: "same-origin" });
    if (!r.ok) return;
    const d = await r.json();
    renderRoster(d.people || [], d.counts || {});
    updateClipPeople();
  } catch (e) {
    /* leave the last render */
  }
}

async function loadClips() {
  try {
    const r = await fetch("/clips?limit=" + clipLimit, {
      credentials: "same-origin",
      signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) throw new Error("Clips could not be loaded (" + r.status + ")");
    const d = await r.json();
    renderClips(d.clips || []);
    $("clips-error").hidden = true;
  } catch (e) {
    $("clips-error").textContent =
      "Couldn’t refresh clips. Your last results are still here. Try Refresh.";
    $("clips-error").hidden = false;
  }
}

$("add-person").addEventListener("click", async () => {
  const input = $("new-name");
  const name = input.value.trim();
  if (!name) return;
  try {
    const r = await fetch("/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    input.value = "";
    loadPeople();
    loadClips();
  } catch (e) {
    alert("Could not add them: " + e.message);
  }
});

$("new-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    $("add-person").click();
  }
});

/* Complete JPEG responses avoid MJPEG buffering through SSO and Cloudflare.
   One request at a time; decode before swapping; retry without blanking the
   last frame. The lease expires independently when a viewer disappears. */
let liveTimer,
  liveAbort,
  liveGeneration = 0,
  liveURL = null,
  liveStarted = 0;
let liveFrameAt = 0,
  liveSeq = null;
async function liveTick(generation) {
  if (!liveOn || document.hidden || generation !== liveGeneration) return;
  liveAbort = new AbortController();
  const timeout = setTimeout(() => liveAbort?.abort(), 8000);
  let delay = 500;
  try {
    const r = await fetch("/live.jpg?t=" + Date.now(), {
      cache: "no-store",
      signal: liveAbort.signal,
    });
    if (!r.ok)
      throw new Error(
        r.status === 401 || r.status === 403
          ? "Sign in again to view the camera."
          : "Waiting for the camera",
      );
    if (!r.headers.get("content-type")?.includes("image/jpeg"))
      throw new Error("Sign in again to view the camera.");
    const seq = r.headers.get("X-Frame-Seq");
    const frameAt = Number(r.headers.get("X-Frame-At")) * 1000;
    const blob = await r.blob();
    if (!liveOn || generation !== liveGeneration) return;
    if (seq !== liveSeq) {
      const url = URL.createObjectURL(blob);
      const check = new Image();
      check.src = url;
      try {
        await check.decode();
      } catch (e) {
        URL.revokeObjectURL(url);
        throw e;
      }
      if (!liveOn || generation !== liveGeneration) {
        URL.revokeObjectURL(url);
        return;
      }
      const previous = liveURL;
      liveURL = url;
      $("live").src = url;
      $("live").hidden = false;
      $("frame").hidden = true;
      $("camera-empty").hidden = true;
      if (previous) URL.revokeObjectURL(previous);
      liveSeq = seq;
    }
    liveFrameAt = frameAt;
    pill(
      $("live-badge"),
      Date.now() - frameAt < 4000 ? "ok" : "warn",
      Date.now() - frameAt < 4000 ? "Live" : "Delayed",
    );
    $("live-message").textContent =
      "Low-bandwidth preview · " +
      (snapshot?.camera?.preview_fps_limit || 2) +
      " fps limit";
    $("frame-age").textContent =
      "Frame received " + humanDuration((Date.now() - frameAt) / 1000) + " ago";
  } catch (e) {
    if (!liveOn || generation !== liveGeneration) return;
    delay = 2000;
    const waiting = !liveFrameAt && Date.now() - liveStarted < 40000;
    pill($("live-badge"), "warn", waiting ? "Connecting" : "Reconnecting");
    $("live-message").textContent =
      snapshot?.camera?.preview_fps_limit === 0
        ? "Preview paused while the Pi cools down. It will resume automatically."
        : waiting
          ? "Waking the preview. This can take a few seconds."
          : e.message.includes("Sign in")
            ? e.message
            : "Camera delayed. Reconnecting automatically…";
  } finally {
    clearTimeout(timeout);
    if (liveOn && generation === liveGeneration && !document.hidden)
      liveTimer = setTimeout(() => liveTick(generation), delay);
  }
}

function setLive(enabled) {
  if (enabled && roiEditing) endRoiEdit();
  liveOn = enabled;
  liveGeneration++;
  clearTimeout(liveTimer);
  liveAbort?.abort();
  $("live-toggle").textContent = enabled ? "■ Stop live" : "▶ Watch live";
  $("live-toggle").setAttribute("aria-pressed", String(enabled));
  $("view-title").textContent = enabled
    ? "Your kitchen, right now."
    : "Latest capture";
  if (enabled) {
    liveStarted = Date.now();
    pill($("live-badge"), "warn", "Connecting");
    liveTick(liveGeneration);
  } else {
    $("live").hidden = true;
    $("live").removeAttribute("src");
    if (liveURL) URL.revokeObjectURL(liveURL);
    liveURL = null;
    liveSeq = null;
    liveFrameAt = 0;
    $("frame").hidden = !$("frame").naturalWidth;
    pill($("live-badge"), "mute", "Snapshot");
    $("live-message").textContent = "Live view starts when you need it.";
    if (snapshot) render(snapshot, false);
    // Do not cancel another viewer's lease; ours expires on the server.
  }
}
$("live-toggle").onclick = () => setLive(!liveOn);
$("fullscreen").onclick = () => {
  const promise = document.fullscreenElement
    ? document.exitFullscreen()
    : $("viewport").requestFullscreen?.();
  promise?.catch(() => toast("Full screen is unavailable in this browser."));
};
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    liveGeneration++;
    clearTimeout(liveTimer);
    liveAbort?.abort();
    if (liveOn) {
      pill($("live-badge"), "mute", "Paused");
      $("live-message").textContent =
        "Preview paused while this tab is hidden.";
    }
  } else {
    refreshStatus();
    if (liveOn) {
      liveStarted = Date.now();
      liveTick(++liveGeneration);
    }
  }
});
window.addEventListener("pagehide", () => {
  clearTimeout(liveTimer);
  liveAbort?.abort();
});

async function refreshStatus() {
  try {
    const r = await fetch("/status", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) throw new Error("Status unavailable");
    render(await r.json());
    pill($("conn"), "ok", "Connected");
  } catch (e) {
    markStale();
  }
}

const viewCopy = {
  overview: [
    "THE SHARED KITCHEN",
    "A sink check, at a glance.",
    "What’s happening now, and what happened earlier.",
  ],
  clips: [
    "THE REPLAY",
    "Every sink visit. One place.",
    "Watch a clip, put a name to it, get on with your day.",
  ],
  household: [
    "THE HOUSEHOLD",
    "A shared sink. A shared effort.",
    "Manage your roommates and their clip tags.",
  ],
  setup: [
    "BEHIND THE SCENES",
    "Make “clean” mean clean.",
    "Camera health, your reference image, and the area being watched.",
  ],
};
function navigate() {
  const next = location.hash.slice(1);
  currentView = viewCopy[next] ? next : "overview";
  if (roiEditing) endRoiEdit();
  if (!["overview", "setup"].includes(currentView)) setLive(false);
  const copy = viewCopy[currentView];
  ["page-eyebrow", "page-title", "page-sub"].forEach((id, i) =>
    setText(id, copy[i], { animate: false }),
  );
  document.querySelectorAll("[data-view]").forEach((a) => {
    if (a.dataset.view === currentView) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("camera-section").hidden = !["overview", "setup"].includes(currentView);
  $("clips-section").hidden = !["overview", "clips"].includes(currentView);
  $("household-section").hidden = currentView !== "household";
  $("setup-section").hidden = currentView !== "setup";
  $("setup-actions").hidden = currentView !== "setup";
  $("all-clips").hidden = currentView !== "overview";
  renderClips(clipsCache);
  if (snapshot) render(snapshot, false);
}
window.addEventListener("hashchange", navigate);
document.querySelectorAll("[data-filter]").forEach(
  (btn) =>
    (btn.onclick = () => {
      clipFilter = btn.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-pressed", String(b === btn));
      });
      renderClips(clipsCache);
    }),
);
$("refresh-clips").onclick = loadClips;
$("more-clips").onclick = () => {
  clipLimit = Math.min(200, clipLimit + 40);
  loadClips();
};
$("person-form").onsubmit = (e) => e.preventDefault();
$("today").textContent = new Date().toLocaleDateString([], {
  weekday: "long",
  month: "short",
  day: "numeric",
});

/* ---------- boot: cache first ---------- */

(function boot() {
  try {
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) {
      const saved = JSON.parse(cached);
      lastGoodAt = saved.savedAt;
      render(saved.data, false);
      markStale();
    }
  } catch (e) {
    /* ignore */
  }
  navigate();
  startPolling("bounded requests work across the authenticated proxy");
  loadPeople().then(loadClips);
  // clips only change when somebody walks past the sink, so this is unhurried
  setInterval(() => {
    if (!document.hidden) loadClips();
  }, 30000);
  setInterval(() => {
    if (lastGoodAt && Date.now() - lastGoodAt > 15000) markStale();
  }, 5000);
})();
