// Meta CHM v2 — serverless canopy-height analysis.
// One raster source of RAW height (uint8 metres in R, lossless WebP PMTiles on source.coop).
// maplibre `raster-color` colorizes + thresholds it on the GPU, live from the sliders.
// Quantitative metrics (cover/area/mean/max/histogram) read the actual tile pixels via the
// PMTiles JS API — gated behind a zoom so we never scan the whole globe.

import maplibregl from "https://esm.sh/maplibre-gl@4.7.1";
import { PMTiles, Protocol } from "https://esm.sh/pmtiles@3.2.1";

const PMTILES_URL = "https://data.source.coop/tge-labs/meta-chm-v2/pmtiles/chm_height.pmtiles";
const COG_BASE =
  "https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm";
const MAX_M = 60; // slider ceiling (canopy rarely exceeds ~50 m)
const MIN_ANALYSIS_ZOOM = 11; // metrics only when the view is a handful of tiles
const MAX_ANALYSIS_TILES = 64;
const RAMP = ["#ffffe5", "#d9f0a3", "#78c679", "#238443", "#004529"]; // YlGn

const $ = (id) => document.getElementById(id);
const state = { mode: "ramp", hmin: 0, hmax: 50, forest: 5, opacity: 0.9 };

// ---- maplibre + pmtiles ----
const protocol = new Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
const pm = new PMTiles(PMTILES_URL);
protocol.add(pm); // share the instance so metrics + map reuse one cache

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: [13.4, 52.5],
  zoom: 9,
  maxZoom: 16,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

// One PMTiles archive (z0-8 overview + z10-14 detail), used as two raster layers so
// maplibre overzooms the z8 overview to fill z9 (no gap) and the crisp z10-14 takes over.
const CHM_LAYERS = ["chm-lo", "chm-hi"];
const RASTER_PAINT = () => ({
  "raster-opacity": state.opacity,
  "raster-resampling": "nearest", // preserve exact height values
  "raster-color-mix": [255, 0, 0, 0], // height(m) = R channel * 255
  "raster-color-range": [0, 255],
  "raster-color": colorExpr(),
});

map.on("load", () => {
  map.addSource("chm-lo", {
    type: "raster",
    url: `pmtiles://${PMTILES_URL}`,
    tileSize: 256,
    maxzoom: 8, // overzoomed (upscaled) for z9+ until chm-hi covers z10+
  });
  map.addSource("chm-hi", {
    type: "raster",
    url: `pmtiles://${PMTILES_URL}`,
    tileSize: 256,
    minzoom: 10,
    maxzoom: 14,
  });
  map.addLayer({ id: "chm-lo", type: "raster", source: "chm-lo", paint: RASTER_PAINT() });
  map.addLayer({ id: "chm-hi", type: "raster", source: "chm-hi", paint: RASTER_PAINT() });
  map.on("moveend", scheduleMetrics);
  scheduleMetrics();
});

// ---- raster-color expressions (metres on ["raster-value"]) ----
function transparent() {
  return "rgba(0,0,0,0)";
}
function rampExpr(lo, hi) {
  hi = Math.max(hi, lo + 1);
  const stops = [0, transparent()];
  if (lo > 0) stops.push(lo - 0.001, transparent());
  for (let k = 0; k < RAMP.length; k++) {
    stops.push(lo + (k / (RAMP.length - 1)) * (hi - lo), RAMP[k]);
  }
  stops.push(hi + 0.001, transparent());
  return ["interpolate", ["linear"], ["raster-value"], ...stops];
}
function classesExpr(lo, hi) {
  // discrete height classes within [lo,hi]; transparent outside
  const edges = [2, 5, 10, 20, 30];
  const expr = ["step", ["raster-value"], transparent()];
  let first = true;
  for (let k = 0; k < RAMP.length; k++) {
    const e = k === 0 ? Math.max(lo, 0.5) : edges[k - 1];
    if (e <= lo || e > hi) continue;
    expr.push(e, RAMP[k]);
    first = false;
  }
  if (first) expr.push(Math.max(lo, 0.5), RAMP[2]);
  // clip above hi
  return ["case", [">", ["raster-value"], hi], transparent(), expr];
}
function forestExpr(thr) {
  return ["step", ["raster-value"], transparent(), Math.max(thr, 0.5), "#238443"];
}
function colorExpr() {
  if (state.mode === "forest") return forestExpr(state.forest);
  if (state.mode === "classes") return classesExpr(state.hmin, state.hmax);
  return rampExpr(state.hmin, state.hmax);
}
function applyColor() {
  const expr = colorExpr();
  for (const id of CHM_LAYERS) {
    if (!map.getLayer(id)) continue;
    map.setPaintProperty(id, "raster-color", expr);
    map.setPaintProperty(id, "raster-opacity", state.opacity);
  }
}

// ---- controls ----
function syncLabels() {
  $("range-label").textContent = `${state.hmin}–${state.hmax} m`;
  $("forest-label").textContent = `${state.forest} m`;
  $("forest-row").hidden = state.mode !== "forest";
}
$("mode").onchange = (e) => {
  state.mode = e.target.value;
  syncLabels();
  applyColor();
};
$("hmin").oninput = (e) => {
  state.hmin = Math.min(+e.target.value, state.hmax - 1);
  e.target.value = state.hmin;
  syncLabels();
  applyColor();
  scheduleMetrics();
};
$("hmax").oninput = (e) => {
  state.hmax = Math.max(+e.target.value, state.hmin + 1);
  e.target.value = state.hmax;
  syncLabels();
  applyColor();
  scheduleMetrics();
};
$("forest-thr").oninput = (e) => {
  state.forest = +e.target.value;
  syncLabels();
  applyColor();
  scheduleMetrics();
};
$("opacity").oninput = (e) => {
  state.opacity = +e.target.value / 100;
  applyColor();
};

// ---- tile math ----
const lon2x = (lon, z) => Math.floor(((lon + 180) / 360) * 2 ** z);
const lat2y = (lat, z) => {
  const r = (lat * Math.PI) / 180;
  return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z);
};
function tileToQuadkey(x, y, z) {
  let qk = "";
  for (let i = z; i > 0; i--) {
    let d = 0;
    const m = 1 << (i - 1);
    if (x & m) d += 1;
    if (y & m) d += 2;
    qk += d;
  }
  return qk;
}

// ---- zoom-gated metrics (read actual heights from the data tiles) ----
let metricsTimer;
function scheduleMetrics() {
  clearTimeout(metricsTimer);
  metricsTimer = setTimeout(computeMetrics, 350);
}
async function computeMetrics() {
  const z = Math.floor(map.getZoom());
  $("metrics-z").textContent = `z${z}`;
  const body = $("metrics-body");
  const hist = $("hist");
  if (z < MIN_ANALYSIS_ZOOM) {
    body.className = "muted";
    body.textContent = `Zoom to ≥ ${MIN_ANALYSIS_ZOOM} to measure the visible canopy.`;
    hist.hidden = true;
    return;
  }
  const tz = Math.min(z, 14);
  const b = map.getBounds();
  const x0 = lon2x(b.getWest(), tz);
  const x1 = lon2x(b.getEast(), tz);
  const y0 = lat2y(b.getNorth(), tz);
  const y1 = lat2y(b.getSouth(), tz);
  const tiles = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) tiles.push([x, y]);
  if (tiles.length > MAX_ANALYSIS_TILES) {
    body.className = "muted";
    body.textContent = `View spans ${tiles.length} tiles — zoom in a little to measure.`;
    hist.hidden = true;
    return;
  }
  body.className = "muted";
  body.textContent = "Measuring…";

  const counts = new Uint32Array(256);
  let valid = 0;
  for (const [x, y] of tiles) {
    const px = await tilePixels(tz, x, y);
    if (!px) continue;
    for (let i = 0; i < px.length; i += 4) {
      const h = px[i]; // R = height (m)
      if (h > 0) {
        counts[h]++;
        valid++;
      }
    }
  }
  if (!valid) {
    body.className = "muted";
    body.textContent = "No canopy in view.";
    hist.hidden = true;
    return;
  }
  // metres per pixel at this zoom + view-centre latitude
  const latC = (b.getNorth() + b.getSouth()) / 2;
  const mpp = (156543.03392 * Math.cos((latC * Math.PI) / 180)) / 2 ** tz;
  const pxArea = mpp * mpp; // m² per pixel
  const lo = state.mode === "forest" ? state.forest : state.hmin;
  const hi = state.mode === "forest" ? 255 : state.hmax;
  let inRange = 0;
  let sum = 0;
  let max = 0;
  for (let h = 1; h < 256; h++) {
    sum += h * counts[h];
    if (counts[h]) max = h;
    if (h >= lo && h <= hi) inRange += counts[h];
  }
  const areaKm2 = (inRange * pxArea) / 1e6;
  const cover = (100 * inRange) / valid;
  const mean = sum / valid;

  body.className = "";
  body.innerHTML = `
    <div class="stat"><span>Canopy cover (in range)</span><b>${cover.toFixed(1)}%</b></div>
    <div class="stat"><span>Area in range</span><b>${areaKm2.toFixed(areaKm2 < 10 ? 2 : 0)} km²</b></div>
    <div class="stat"><span>Mean height</span><b>${mean.toFixed(1)} m</b></div>
    <div class="stat"><span>Max height</span><b>${max} m</b></div>`;
  drawHist(counts, lo, hi);
}

const _bmpCanvas = document.createElement("canvas");
_bmpCanvas.width = _bmpCanvas.height = 256;
const _bmpCtx = _bmpCanvas.getContext("2d", { willReadFrequently: true });
async function tilePixels(z, x, y) {
  try {
    const r = await pm.getZxy(z, x, y);
    if (!r) return null;
    const bmp = await createImageBitmap(new Blob([r.data], { type: "image/webp" }));
    _bmpCtx.clearRect(0, 0, 256, 256);
    _bmpCtx.drawImage(bmp, 0, 0);
    return _bmpCtx.getImageData(0, 0, 256, 256).data;
  } catch {
    return null;
  }
}

function drawHist(counts, lo, hi) {
  const c = $("hist");
  c.hidden = false;
  const ctx = c.getContext("2d");
  const W = c.width;
  const H = c.height;
  ctx.clearRect(0, 0, W, H);
  const top = Math.min(50, counts.length); // 0–50 m
  let peak = 1;
  for (let h = 1; h < top; h++) peak = Math.max(peak, counts[h]);
  const bw = W / (top - 1);
  for (let h = 1; h < top; h++) {
    const bh = (counts[h] / peak) * (H - 14);
    ctx.fillStyle = h >= lo && h <= hi ? "#4ade80" : "rgba(135,153,138,0.4)";
    ctx.fillRect((h - 1) * bw, H - bh - 12, Math.max(bw - 1, 1), bh);
  }
  ctx.fillStyle = "#87998a";
  ctx.font = '10px "IBM Plex Mono", monospace';
  ctx.fillText("0", 0, H - 1);
  ctx.fillText("25 m", W / 2 - 10, H - 1);
  ctx.fillText("50", W - 14, H - 1);
}

// ---- lightweight download: COGs intersecting the current view (z10 quadkeys, no duckdb) ----
$("export").onclick = () => {
  const b = map.getBounds();
  const x0 = lon2x(b.getWest(), 10);
  const x1 = lon2x(b.getEast(), 10);
  const y0 = lat2y(b.getNorth(), 10);
  const y1 = lat2y(b.getSouth(), 10);
  const qks = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) qks.push(tileToQuadkey(x, y, 10));
  if (qks.length > 400) {
    toast(`view covers ${qks.length} tiles — zoom in to export a smaller set`);
    return;
  }
  const lines = [
    "#!/usr/bin/env bash",
    `# Meta CHM v2 ml3 — ${qks.length} source COGs intersecting the current view (anonymous).`,
    "set -euo pipefail",
    ...qks.map((qk) => `curl -O ${COG_BASE}/${qk}.tif`),
  ];
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/x-shellscript" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "download_chm_tiles.sh";
  a.click();
  URL.revokeObjectURL(url);
  toast(`${qks.length} tiles → download_chm_tiles.sh`);
};

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2800);
}

syncLabels();
