"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "mapillaryToken";
const ACTIVE = new Set(["queued", "searching", "downloading"]);
const CELL_DEG = 0.01; // must match INITIAL_CELL_DEG in downloader.py

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
  remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

// ---------------------------------------------------------------- map setup

const map = L.map("map", { preferCanvas: true }).setView([48.0, 9.0], 6);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const areaLayer = new L.FeatureGroup().addTo(map);
const pointsLayer = L.layerGroup().addTo(map);
const canvas = L.canvas({ padding: 0.5 });

map.addControl(new L.Control.Draw({
  draw: {
    rectangle: { shapeOptions: { color: "#05a66b", weight: 2 } },
    polygon: false, polyline: false, circle: false, marker: false, circlemarker: false,
  },
  edit: { featureGroup: areaLayer, remove: false },
}));

map.on(L.Draw.Event.CREATED, (e) => setArea(e.layer.getBounds()));
map.on(L.Draw.Event.EDITED, () => {
  const layer = areaLayer.getLayers()[0];
  if (layer) setArea(layer.getBounds(), false);
});

let area = null; // [west, south, east, north]

function setArea(bounds, redraw = true) {
  const w = clampLon(bounds.getWest()), e = clampLon(bounds.getEast());
  const s = clampLat(bounds.getSouth()), n = clampLat(bounds.getNorth());
  area = [w, s, e, n].map((v) => +v.toFixed(6));
  if (redraw) {
    areaLayer.clearLayers();
    areaLayer.addLayer(L.rectangle([[s, w], [n, e]], { color: "#05a66b", weight: 2, fillOpacity: 0.05 }));
  }
  updateAreaInfo();
}

function clampLon(v) { return Math.max(-180, Math.min(180, v)); }
function clampLat(v) { return Math.max(-85, Math.min(85, v)); }

function updateAreaInfo() {
  const el = $("area-info");
  if (!area) {
    el.textContent = "No area selected.";
    return;
  }
  const [w, s, e, n] = area;
  const midLat = ((s + n) / 2) * Math.PI / 180;
  const widthKm = (e - w) * 111.32 * Math.cos(midLat);
  const heightKm = (n - s) * 110.54;
  const cells = Math.ceil((e - w) / CELL_DEG) * Math.ceil((n - s) / CELL_DEG);
  el.innerHTML = "";
  el.append(
    `West ${w}, South ${s}, East ${e}, North ${n}`,
    document.createElement("br"),
    `≈ ${widthKm.toFixed(1)} × ${heightKm.toFixed(1)} km (${(widthKm * heightKm).toFixed(1)} km²), ${cells} search cell(s)`,
  );
}

$("use-view").addEventListener("click", () => setArea(map.getBounds()));
$("clear-area").addEventListener("click", () => {
  area = null;
  areaLayer.clearLayers();
  updateAreaInfo();
});

$("place-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("place").value.trim();
  if (!q) return;
  try {
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + encodeURIComponent(q);
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    const hits = await res.json();
    if (!hits.length) {
      alert("Place not found.");
      return;
    }
    const [s, n, w, e] = hits[0].boundingbox.map(Number);
    const bounds = L.latLngBounds([s, w], [n, e]);
    map.fitBounds(bounds);
    setArea(bounds);
  } catch (err) {
    alert("Place search failed: " + err);
  }
});

// ------------------------------------------------------------------ token

const savedToken = store.get(TOKEN_KEY);
if (savedToken) {
  $("token").value = savedToken;
  $("remember-token").checked = true;
}
$("remember-token").addEventListener("change", () => {
  if (!$("remember-token").checked) store.remove(TOKEN_KEY);
});

fetch("/api/config").then((r) => r.json()).then((cfg) => {
  if (cfg.has_server_token) {
    $("token").placeholder = "optional - server has MAPILLARY_TOKEN set";
  }
  $("job-output").dataset.root = cfg.output_root;
});

// ------------------------------------------------------------------- jobs

let currentJob = null;
let pollTimer = null;
let lastPointsLoad = 0;

async function startJob(dryRun) {
  if (!area) {
    alert("Select an area first (draw a rectangle, search a place or use the visible map area).");
    return;
  }
  const token = $("token").value.trim();
  if ($("remember-token").checked && token) store.set(TOKEN_KEY, token);

  const body = {
    bbox: area,
    token,
    name: $("name").value.trim(),
    image_size: $("image-size").value,
    start_date: $("start-date").value || null,
    end_date: $("end-date").value || null,
    include_panoramas: $("include-pano").checked,
    min_distance_m: Number($("min-distance").value) || 0,
    max_images: Number($("max-images").value) || null,
    dry_run: dryRun,
  };
  const res = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Could not start job");
    return;
  }
  pointsLayer.clearLayers();
  lastPointsLoad = 0;
  showJob(data);
  poll();
}

$("count-btn").addEventListener("click", () => startJob(true));
$("download-btn").addEventListener("click", () => startJob(false));
$("cancel-btn").addEventListener("click", async () => {
  if (!currentJob) return;
  await fetch(`/api/jobs/${currentJob.id}/cancel`, { method: "POST" });
  poll();
});

async function poll() {
  clearTimeout(pollTimer);
  if (!currentJob) return;
  const res = await fetch(`/api/jobs/${currentJob.id}`);
  if (!res.ok) return;
  const job = await res.json();
  const wasActive = ACTIVE.has(currentJob.status);
  showJob(job);

  const isActive = ACTIVE.has(job.status);
  const searched = job.status === "downloading" || !isActive;
  const now = Date.now();
  if (searched && (lastPointsLoad === 0 || (isActive && now - lastPointsLoad > 5000) || (wasActive && !isActive))) {
    lastPointsLoad = now;
    loadPoints(job);
  }
  if (isActive) pollTimer = setTimeout(poll, 1000);
}

function showJob(job) {
  currentJob = job;
  $("job").hidden = false;
  $("job-name").textContent = job.name + (job.config.dry_run ? " (count only)" : "");
  const status = $("job-status");
  status.textContent = job.status;
  status.className = "badge " + job.status;

  const st = job.stats;
  const active = ACTIVE.has(job.status);
  const progress = $("progress");
  if (job.status === "searching" || job.status === "queued") {
    $("phase-label").textContent = `Searching area cells: ${st.cells_done} / ${st.cells_total}`;
    progress.max = Math.max(st.cells_total, 1);
    progress.value = st.cells_done;
  } else {
    const done = st.downloaded + st.failed;
    $("phase-label").textContent = job.config.dry_run
      ? "Count finished"
      : `Downloading: ${done} / ${st.to_download}`;
    progress.max = Math.max(st.to_download, 1);
    progress.value = job.config.dry_run ? progress.max : done;
  }

  const rows = [
    ["Images found", st.found],
    ["Selected (after filters)", st.selected],
  ];
  if (!job.config.dry_run) {
    rows.push(
      ["Downloaded", st.downloaded],
      ["Already on disk", st.skipped],
      ["Failed", st.failed],
      ["Data", formatBytes(st.bytes)],
    );
  }
  const dl = $("stats");
  dl.innerHTML = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = typeof v === "number" ? v.toLocaleString() : v;
    dl.append(dt, dd);
  }

  $("job-error").hidden = !job.error;
  $("job-error").textContent = job.error;
  $("job-output").textContent = job.config.dry_run ? "" : `Output folder: ${job.config.output_dir}`;
  $("cancel-btn").hidden = !active;
  $("count-btn").disabled = active;
  $("download-btn").disabled = active;
  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = job.log.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

async function loadPoints(job) {
  const res = await fetch(`/api/jobs/${job.id}/points`);
  if (!res.ok) return;
  const points = await res.json();
  pointsLayer.clearLayers();
  const style = getComputedStyle(document.documentElement);
  const colorTodo = style.getPropertyValue("--point").trim();
  const colorDone = style.getPropertyValue("--point-done").trim();
  for (const [id, lat, lon, downloaded] of points) {
    const marker = L.circleMarker([lat, lon], {
      renderer: canvas,
      radius: 3,
      weight: 0,
      fillOpacity: 0.8,
      fillColor: downloaded ? colorDone : colorTodo,
    });
    marker.on("click", () => showImagePopup(job, id, lat, lon, downloaded, marker));
    pointsLayer.addLayer(marker);
  }
}

function showImagePopup(job, id, lat, lon, downloaded, marker) {
  const div = document.createElement("div");
  div.className = "popup";
  const link = document.createElement("a");
  link.href = `https://www.mapillary.com/app/?pKey=${encodeURIComponent(id)}`;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = `Image ${id}`;
  div.append(link, document.createElement("br"), `${lat.toFixed(6)}, ${lon.toFixed(6)}`);
  if (downloaded) {
    const img = document.createElement("img");
    img.src = `/api/jobs/${job.id}/images/${encodeURIComponent(id)}`;
    img.alt = `Mapillary image ${id}`;
    img.addEventListener("load", () => marker.getPopup()?.update());
    div.append(img);
  }
  marker.bindPopup(div, { maxWidth: 300 }).openPopup();
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(1)} ${units[i]}`;
}

// Re-attach to the newest job after a page reload.
fetch("/api/jobs").then((r) => r.json()).then((jobs) => {
  if (jobs.length) {
    showJob(jobs[0]);
    const [w, s, e, n] = jobs[0].config.bbox;
    setArea(L.latLngBounds([s, w], [n, e]));
    map.fitBounds(areaLayer.getBounds());
    poll();
  }
});
