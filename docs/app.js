// Meta CHM v2 — serverless canopy-height analysis.
// One raster source of RAW height (uint8 metres in R, lossless WebP PMTiles on source.coop).
// maplibre `raster-color` colorizes + thresholds it on the GPU, live from the sliders.
// Quantitative metrics (cover/area/mean/max/histogram) read the actual tile pixels via the
// PMTiles JS API — gated behind a zoom so we never scan the whole globe.

// maplibregl + pmtiles are UMD globals (loaded via <script> in index.html).
const { PMTiles } = pmtiles;

const PMTILES_URL = "https://data.source.coop/tge-labs/meta-chm-v2/pmtiles/chm_height.pmtiles";
const EOX_YEAR = 2024; // Sentinel-2 cloudless mosaic year (EOX::Maps)
const COG_BASE =
  "https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm";
const MAX_M = 60; // slider ceiling (canopy rarely exceeds ~50 m)
const MIN_ANALYSIS_ZOOM = 11; // metrics only when the view is a handful of tiles
const MAX_ANALYSIS_TILES = 64;
// Bright warm->magenta ramp (low->high). Every stop is high-luminance so even low canopy
// (the most common) stays visible; none are dark. Non-green so it pops over the Sentinel-2
// forest backdrop. Low = bright yellow, tall = hot magenta.
const RAMP = ["#ffe87a", "#ffab4a", "#fb6a63", "#f5359a", "#c81e8c"];

const $ = (id) => document.getElementById(id);
const state = { mode: "ramp", hmin: 10, hmax: 60, forest: 5, opacity: 0.75 };

// ---- maplibre + pmtiles ----
// MapLibre has no GPU `raster-color`, so we colorize the raw grayscale height tiles
// ourselves: a custom `chm://` protocol pulls each tile's bytes from the PMTiles archive,
// maps height(m) -> RGBA through a lookup table (rebuilt from the sliders), and hands
// MapLibre a ready-to-draw PNG. PMTiles caches the raw bytes, so recoloring is canvas-only.
const pm = new PMTiles(PMTILES_URL);
maplibregl.addProtocol("chm", chmProtocol);

// Sentinel-2 cloudless basemap (EOX::Maps) as an inline raster style — no external
// style.json dependency, and gives the canopy a real-world satellite backdrop.
const EOX_TILES = `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-${EOX_YEAR}_3857/default/g/{z}/{y}/{x}.jpg`;
const EOX_ATTR =
  `Sentinel-2 cloudless ${EOX_YEAR} by <a href="https://s2maps.eu" target="_blank" rel="noopener">EOX IT Services GmbH</a>`;
const baseStyle = {
  version: 8,
  sources: {
    s2: {
      type: "raster",
      tiles: [EOX_TILES],
      tileSize: 256,
      maxzoom: 16, // EOX overzooms server-side past native ~z14
      attribution: EOX_ATTR,
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#060a0f" } },
    { id: "s2", type: "raster", source: "s2" },
  ],
};

const map = new maplibregl.Map({
  container: "map",
  style: baseStyle,
  center: [13.4, 52.5],
  zoom: 9,
  maxZoom: 16,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
window.__map = map; // debugging handle

// Never fail silently again — surface load/source/tile errors on screen + console.
map.on("error", (e) => {
  const msg = (e && e.error && e.error.message) || "map error";
  console.error("[chm]", e && e.error ? e.error : e);
  toast(msg);
});

// One PMTiles archive (z0-8 overview + z10-14 detail), drawn as two raster layers so
// maplibre overzooms the z8 overview to fill z9 (no gap) and the crisp z10-14 takes over.
const CHM_LAYERS = ["chm-lo", "chm-hi"];
let colorVer = 0; // bumped to bust maplibre's tile cache when the colormap changes
const chmTiles = () => [`chm://{z}/{x}/{y}?v=${colorVer}`];

map.on("load", () => {
  rebuildLUT();
  map.addSource("chm-lo", { type: "raster", tiles: chmTiles(), tileSize: 256, minzoom: 0, maxzoom: 8 });
  map.addSource("chm-hi", { type: "raster", tiles: chmTiles(), tileSize: 256, minzoom: 10, maxzoom: 14 });
  const paint = { "raster-opacity": state.opacity, "raster-resampling": "nearest" };
  map.addLayer({ id: "chm-lo", type: "raster", source: "chm-lo", paint });
  map.addLayer({ id: "chm-hi", type: "raster", source: "chm-hi", paint });
  map.on("moveend", scheduleMetrics);
  scheduleMetrics();
});

// ---- client-side colorization: height(metres) -> RGBA via a 256-entry LUT ----
const RAMP_RGB = RAMP.map((h) => [
  parseInt(h.slice(1, 3), 16),
  parseInt(h.slice(3, 5), 16),
  parseInt(h.slice(5, 7), 16),
]);
const LUT = new Uint8ClampedArray(256 * 4); // height byte -> rgba

function rampRGB(t) {
  const x = Math.max(0, Math.min(1, t)) * (RAMP_RGB.length - 1);
  const i = Math.min(RAMP_RGB.length - 2, Math.floor(x));
  const f = x - i;
  const a = RAMP_RGB[i];
  const b = RAMP_RGB[i + 1];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}
function rebuildLUT() {
  const { mode, hmin, hmax, forest } = state;
  const edges = [0, 2, 5, 10, 20]; // lower bound of each discrete class -> RAMP_RGB index
  for (let h = 0; h < 256; h++) {
    let rgb = null;
    // h===0 is no-canopy; h===255 is overview nodata fill — both stay transparent.
    if (h > 0 && h < 255) {
      if (mode === "forest") {
        if (h >= forest) rgb = RAMP_RGB[3];
      } else if (mode === "classes") {
        if (h >= hmin && h <= hmax) {
          let k = 0;
          for (let e = 0; e < edges.length; e++) if (h >= edges[e]) k = e;
          rgb = RAMP_RGB[k];
        }
      } else if (h >= hmin && h <= hmax) {
        rgb = rampRGB((h - hmin) / Math.max(1, hmax - hmin));
      }
    }
    const o = h * 4;
    if (rgb) {
      LUT[o] = rgb[0];
      LUT[o + 1] = rgb[1];
      LUT[o + 2] = rgb[2];
      LUT[o + 3] = 255;
    } else {
      LUT[o] = LUT[o + 1] = LUT[o + 2] = LUT[o + 3] = 0;
    }
  }
}

// Custom protocol: pull a tile from the PMTiles archive, recolor through the LUT -> PNG.
async function chmProtocol(params) {
  const m = params.url.match(/chm:\/\/(\d+)\/(\d+)\/(\d+)/);
  if (!m) return { data: await emptyTile() };
  const r = await pm.getZxy(+m[1], +m[2], +m[3]);
  if (!r) return { data: await emptyTile() }; // ocean / out-of-coverage tile
  const bmp = await createImageBitmap(new Blob([r.data], { type: "image/webp" }));
  const cv = document.createElement("canvas");
  cv.width = cv.height = 256;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(bmp, 0, 0, 256, 256);
  const img = cx.getImageData(0, 0, 256, 256);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const o = d[i] * 4; // R channel = height in metres
    d[i] = LUT[o];
    d[i + 1] = LUT[o + 1];
    d[i + 2] = LUT[o + 2];
    d[i + 3] = LUT[o + 3];
  }
  cx.putImageData(img, 0, 0);
  const blob = await new Promise((res) => cv.toBlob(res, "image/png"));
  return { data: await blob.arrayBuffer() };
}
let _emptyTile;
function emptyTile() {
  if (!_emptyTile) {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 256;
    _emptyTile = new Promise((res) => cv.toBlob((b) => b.arrayBuffer().then(res), "image/png"));
  }
  return _emptyTile;
}

let recolorTimer;
function applyColor() {
  rebuildLUT();
  clearTimeout(recolorTimer);
  recolorTimer = setTimeout(() => {
    colorVer++; // new URL -> maplibre refetches; raw bytes are cached, so this is canvas-only
    for (const id of ["chm-lo", "chm-hi"]) {
      const s = map.getSource(id);
      if (s && s.setTiles) s.setTiles(chmTiles());
    }
  }, 140); // debounce slider drags
}
function applyOpacity() {
  for (const id of CHM_LAYERS) {
    if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", state.opacity);
  }
}

// ---- controls ----
function syncLabels() {
  $("range-label").textContent = `${state.hmin}–${state.hmax} m`;
  $("forest-label").textContent = `${state.forest} m`;
  $("forest-row").hidden = state.mode !== "forest";
  $("legend-min").textContent = state.hmin;
  $("legend-mid").textContent = Math.round((state.hmin + state.hmax) / 2);
  $("legend-max").textContent = `${state.hmax} m`;
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
  applyOpacity();
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
    ctx.fillStyle = h >= lo && h <= hi ? "#ff5db1" : "rgba(150,135,150,0.4)";
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
