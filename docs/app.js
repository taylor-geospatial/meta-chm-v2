// Meta CHM v2 — serverless canopy-height analysis.
// Reads RAW height (uint8 metres) straight from the cloud-native COGs on source.coop (CORS +
// range): a global overview COG for z0-9, and the native ~1.19 m per-tile COGs for z10+. A
// custom `cog://` protocol decodes each tile with geotiff.js and colorizes it through a LUT
// rebuilt live from the sliders. Metrics read the same COG pixels, gated behind a zoom.

// maplibregl + GeoTIFF are UMD globals (loaded via <script> in index.html).
const COG_HTTPS = "https://data.source.coop/tge-labs/meta-chm-v2/chm"; // native per-tile COGs (CORS)
const OVERVIEW_URL = "https://data.source.coop/tge-labs/meta-chm-v2/overview/chm_overview_z8.tif"; // global z0-9
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
const aoi = { drawing: false, verts: [], ring: null }; // drawn area-of-interest polygon

// ---- client-side COG rendering ----
// A custom `cog://` protocol reads each map tile straight from the source.coop COGs with
// geotiff.js (range reads), maps height(m) -> RGBA through a LUT (rebuilt from the sliders),
// and hands MapLibre a ready-decoded ImageBitmap. Per-COG headers are cached, so once a COG
// is open its tiles decode in ~5 ms.
maplibregl.addProtocol("cog", cogProtocol);

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

// Two raster layers off the `cog://` protocol: the global overview COG drives z0-8 (maplibre
// overzooms it to fill z9), then the native per-tile COGs take over for z10-14.
const CHM_LAYERS = ["chm-lo", "chm-hi"];
let colorVer = 0; // bumped to bust maplibre's tile cache when the colormap changes
const chmTiles = () => [`cog://{z}/{x}/{y}?v=${colorVer}`];

map.on("load", () => {
  rebuildLUT();
  openCog(OVERVIEW_URL).catch(() => {}); // warm the global overview header before first paint
  map.addSource("chm-lo", { type: "raster", tiles: chmTiles(), tileSize: 256, minzoom: 0, maxzoom: 8 });
  map.addSource("chm-hi", { type: "raster", tiles: chmTiles(), tileSize: 256, minzoom: 10, maxzoom: 14 });
  const paint = { "raster-opacity": state.opacity, "raster-resampling": "nearest" };
  map.addLayer({ id: "chm-lo", type: "raster", source: "chm-lo", paint });
  map.addLayer({ id: "chm-hi", type: "raster", source: "chm-hi", paint });
  setupAOI();
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
        // Color maps to a FIXED 0..MAX_M scale, so a given height always gets the same
        // color; the range slider only filters which heights are visible, not their color.
        rgb = rampRGB(h / MAX_M);
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

// Open + cache one COG per URL. We list its image-pyramid levels (skipping the interleaved
// internal-mask IFDs, PhotometricInterpretation === 4 — selecting one would render garbage),
// ordered full-res first. Header parse is the only slow part and happens once per COG.
const cogCache = new Map(); // url -> Promise<{tiff, levels:[{idx,width}], imgs}>
function openCog(url) {
  let p = cogCache.get(url);
  if (!p) {
    p = (async () => {
      const tiff = await GeoTIFF.fromUrl(url);
      const n = await tiff.getImageCount();
      const levels = [];
      const imgs = {};
      for (let i = 0; i < n; i++) {
        const im = await tiff.getImage(i);
        imgs[i] = im;
        if (im.fileDirectory.PhotometricInterpretation !== 4) levels.push({ idx: i, width: im.getWidth() });
      }
      levels.sort((a, b) => b.width - a.width); // [full, ...coarser]
      return { tiff, levels, imgs };
    })();
    cogCache.set(url, p);
  }
  return p;
}

// Read a 256x256 block of RAW height for web tile z/x/y from the right COG + overview level
// (so each tile is ~one internal block read). Returns Uint8Array(65536) or null off-coverage.
async function cogReadR(z, x, y) {
  const overview = z <= 9;
  let url;
  let target; // pyramid level: 0 = full-res, higher = coarser
  let s10 = 1;
  if (overview) {
    url = OVERVIEW_URL;
    target = 8 - z; // overview COG full-res is z8 (global)
  } else {
    s10 = 1 << (z - 10);
    url = `${COG_HTTPS}/${tileToQuadkey(Math.floor(x / s10), Math.floor(y / s10), 10)}.tif`;
    target = 17 - z; // native COG full-res is z17 of its z10 tile
  }
  let c;
  try {
    c = await openCog(url);
  } catch {
    return null; // ocean / missing tile -> 404
  }
  const L = c.levels[Math.min(Math.max(target, 0), c.levels.length - 1)];
  const side = overview ? L.width / 2 ** z : L.width / s10; // full-res px per web tile at this level
  const px = overview ? x * side : (x % s10) * side;
  const py = overview ? y * side : (y % s10) * side;
  try {
    const bands = await c.imgs[L.idx].readRasters({
      window: [Math.round(px), Math.round(py), Math.round(px + side), Math.round(py + side)],
      width: 256,
      height: 256,
      resampleMethod: "nearest",
      fillValue: 0,
    });
    return bands[0]; // 1-band height
  } catch {
    return null;
  }
}

// Custom protocol: read height from the COG, colorize through the LUT -> ImageBitmap.
async function cogProtocol(params) {
  const m = params.url.match(/cog:\/\/(\d+)\/(\d+)\/(\d+)/);
  if (!m) return { data: await emptyTile() };
  const R = await cogReadR(+m[1], +m[2], +m[3]);
  if (!R) return { data: await emptyTile() };
  const img = new ImageData(256, 256);
  const d = img.data;
  for (let i = 0, p = 0; i < R.length; i++, p += 4) {
    const o = R[i] * 4; // height (m) -> rgba via LUT
    d[p] = LUT[o];
    d[p + 1] = LUT[o + 1];
    d[p + 2] = LUT[o + 2];
    d[p + 3] = LUT[o + 3];
  }
  return { data: await createImageBitmap(img) };
}
function emptyTile() {
  // fresh each call: maplibre transfers the bitmap to a worker (neuters it), so no sharing
  return createImageBitmap(new ImageData(256, 256));
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
// Web-Mercator lon/lat -> absolute pixel at a given zoom (worldpx = 256·2^z), used to map
// AOI vertices into mosaic pixel space for the polygon clip.
const lonToWorldPx = (lng, worldpx) => ((lng + 180) / 360) * worldpx;
const latToWorldPx = (lat, worldpx) => {
  const s = Math.sin((lat * Math.PI) / 180);
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * worldpx;
};
const viewBounds = () => {
  const b = map.getBounds();
  return { w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth() };
};
const polyBounds = (verts) => {
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (const [lng, lat] of verts) {
    if (lng < w) w = lng;
    if (lng > e) e = lng;
    if (lat < s) s = lat;
    if (lat > n) n = lat;
  }
  return { w, s, e, n };
};

// ---- zoom-gated metrics (read actual heights from the data tiles) ----
let metricsTimer;
function scheduleMetrics() {
  clearTimeout(metricsTimer);
  metricsTimer = setTimeout(computeMetrics, 350);
}
async function computeMetrics() {
  const z = Math.floor(map.getZoom());
  $("metrics-z").textContent = `z${z}`;
  $("scope-label").textContent = aoi.ring ? "AOI" : "view";
  const body = $("metrics-body");
  const hist = $("hist");
  if (z < MIN_ANALYSIS_ZOOM) {
    body.className = "muted";
    body.textContent = `Zoom to ≥ ${MIN_ANALYSIS_ZOOM} to measure the ${aoi.ring ? "AOI" : "visible canopy"}.`;
    hist.hidden = true;
    return;
  }
  const tz = Math.min(z, 14);
  // Analysis extent: a drawn AOI polygon if present, else the current view.
  const poly = aoi.ring;
  const bb = poly ? polyBounds(poly) : viewBounds();
  const x0 = lon2x(bb.w, tz);
  const x1 = lon2x(bb.e, tz);
  const y0 = lat2y(bb.n, tz);
  const y1 = lat2y(bb.s, tz);
  const tiles = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) tiles.push([x, y]);
  if (tiles.length > MAX_ANALYSIS_TILES) {
    body.className = "muted";
    body.textContent = `${poly ? "AOI" : "View"} spans ${tiles.length} tiles — zoom in${poly ? " or draw a smaller area" : " a little"} to measure.`;
    hist.hidden = true;
    return;
  }
  body.className = "muted";
  body.textContent = "Measuring…";

  // Assemble a height mosaic (R channel) of the visible tiles. Gap / edge / rumple metrics
  // need the spatial layout — a histogram alone can't see structure. Untouched cells stay
  // 255 (nodata) so ocean / out-of-coverage tiles are excluded from every statistic.
  const nx = x1 - x0 + 1;
  const ny = y1 - y0 + 1;
  const W = nx * 256;
  const H = ny * 256;
  const mosaic = new Uint8Array(W * H).fill(255);
  let any = false;
  for (const [x, y] of tiles) {
    const px = await tilePixels(tz, x, y);
    if (!px) continue;
    any = true;
    const ox = (x - x0) * 256;
    const oy = (y - y0) * 256;
    for (let ty = 0; ty < 256; ty++) {
      let dst = (oy + ty) * W + ox;
      let src = ty * 256;
      for (let tx = 0; tx < 256; tx++, dst++, src++) mosaic[dst] = px[src]; // R = height (m)
    }
  }
  if (!any) {
    body.className = "muted";
    body.textContent = "No data tiles in view.";
    hist.hidden = true;
    return;
  }
  // Clip the mosaic to the AOI polygon — pixels outside the ring become 255 (nodata) and so
  // drop out of every metric. Vertices map to mosaic pixels via the shared Mercator math.
  if (poly) {
    const worldpx = 256 * 2 ** tz;
    const ringPx = [];
    for (const [lng, lat] of poly) {
      ringPx.push(lonToWorldPx(lng, worldpx) - x0 * 256, latToWorldPx(lat, worldpx) - y0 * 256);
    }
    CHMAnalytics.clipToPolygon(mosaic, W, H, ringPx);
  }
  // metres per pixel at this zoom + extent-centre latitude
  const latC = (bb.n + bb.s) / 2;
  const mpp = (156543.03392 * Math.cos((latC * Math.PI) / 180)) / 2 ** tz;
  const pxArea = mpp * mpp; // m² per pixel
  const A = CHMAnalytics.analyzeMosaic(mosaic, W, H, { forestThr: state.forest, mpp });
  if (!A.canopy) {
    body.className = "muted";
    body.textContent = "No canopy in view.";
    hist.hidden = true;
    return;
  }

  // Range-slider filter: canopy within the chosen height band (or forest mode's threshold).
  const counts = A.counts;
  const lo = state.mode === "forest" ? state.forest : state.hmin;
  const hi = state.mode === "forest" ? 254 : state.hmax;
  let inRange = 0;
  for (let h = lo; h <= hi && h <= 254; h++) inRange += counts[h];
  const areaKm2 = (inRange * pxArea) / 1e6;
  const coverRange = (100 * inRange) / A.observed;
  const thr = state.forest;
  const scope = poly ? `AOI · ${fmtArea(A.observed * pxArea)}` : "View";

  body.className = "";
  body.innerHTML = `
    <div class="stat ghead"><span>${scope} · range ${lo}–${hi} m</span></div>
    <div class="stat"><span>Cover in range</span><b>${coverRange.toFixed(1)}%</b></div>
    <div class="stat"><span>Area in range</span><b>${fmtArea(areaKm2 * 1e6)}</b></div>
    <div class="stat ghead"><span>Canopy height</span></div>
    <div class="stat"><span>Mean (vegetated)</span><b>${A.mean.toFixed(1)} m</b></div>
    <div class="stat"><span title="98th pct — robust stand top (GEDI RH98 analogue)">Top height p98</span><b>${A.p.p98} m</b></div>
    <div class="stat"><span>Max</span><b>${A.max} m</b></div>
    <div class="stat"><span title="Std-dev of canopy height — structural variability">Rugosity σ</span><b>${A.std.toFixed(1)} m</b></div>
    <div class="stat ghead"><span>Structure · ≥ ${thr} m</span></div>
    <div class="stat"><span>Canopy cover</span><b>${A.coverPct.toFixed(1)}%</b></div>
    <div class="stat"><span title="Canopy 3-D surface area ÷ planar area — 1.0 = flat, higher = rougher">Rumple index</span><b>${A.rumple.toFixed(2)}×</b></div>
    <div class="stat"><span title="Connected sub-threshold openings in the canopy">Canopy gaps</span><b>${A.gap.count} · ${A.gap.fractionPct.toFixed(0)}%</b></div>
    <div class="stat"><span>Median / largest gap</span><b>${fmtArea(A.gap.medianM2)} / ${fmtArea(A.gap.largestM2)}</b></div>
    <div class="stat"><span title="Interior forest (no non-forest 4-neighbour) vs. edge">Core forest</span><b>${A.edge.corePct.toFixed(0)}% · edge ${A.edge.edgePct.toFixed(0)}%</b></div>
    <div class="stat foot mono"><span>${A.ms.total.toFixed(0)} ms · ${tiles.length} tiles · ${mpp.toFixed(1)} m/px</span></div>`;
  drawHist(counts, lo, hi);
}

// Format an area in m² as m², ha, or km² for readability.
function fmtArea(m2) {
  if (m2 >= 1e6) return `${(m2 / 1e6).toFixed(m2 < 1e7 ? 2 : 0)} km²`;
  if (m2 >= 1e4) return `${(m2 / 1e4).toFixed(1)} ha`;
  return `${Math.round(m2)} m²`;
}

// Metrics read the same COG height pixels as the renderer (R = height m), gated to z>=11.
async function tilePixels(z, x, y) {
  return cogReadR(z, x, y); // Uint8Array(65536) of height, or null
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
  const bb = aoi.ring ? polyBounds(aoi.ring) : viewBounds();
  const x0 = lon2x(bb.w, 10);
  const x1 = lon2x(bb.e, 10);
  const y0 = lat2y(bb.n, 10);
  const y1 = lat2y(bb.s, 10);
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

// ---- drawn AOI polygon: click to add vertices, click the first point / double-click to
// finish, Esc to cancel. Rendered as a maplibre GeoJSON layer; the finished ring restricts
// every metric to that area (see computeMetrics' clipToPolygon call). ----
function setupAOI() {
  map.addSource("aoi", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "aoi-fill", type: "fill", source: "aoi", filter: ["==", "$type", "Polygon"],
    paint: { "fill-color": "#ff5db1", "fill-opacity": 0.12 },
  });
  map.addLayer({
    id: "aoi-outline", type: "line", source: "aoi", filter: ["==", "$type", "Polygon"],
    paint: { "line-color": "#ff5db1", "line-width": 2 },
  });
  map.addLayer({
    id: "aoi-line", type: "line", source: "aoi", filter: ["==", "$type", "LineString"],
    paint: { "line-color": "#ff5db1", "line-width": 2, "line-dasharray": [2, 1] },
  });
  map.addLayer({
    id: "aoi-verts", type: "circle", source: "aoi", filter: ["==", "$type", "Point"],
    paint: {
      "circle-radius": ["case", ["==", ["get", "first"], true], 6, 4],
      "circle-color": ["case", ["==", ["get", "first"], true], "#ffffff", "#ff5db1"],
      "circle-stroke-color": "#ff5db1", "circle-stroke-width": 2,
    },
  });
  map.on("click", onAOIClick);
  map.on("dblclick", onAOIDblClick);
  map.on("mousemove", (e) => {
    if (aoi.drawing && aoi.verts.length) refreshAOI([e.lngLat.lng, e.lngLat.lat]);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && (aoi.drawing || aoi.ring)) clearAOI();
  });
}

function closeRing(verts) {
  const r = verts.slice();
  const f = r[0];
  const l = r[r.length - 1];
  if (r.length && (f[0] !== l[0] || f[1] !== l[1])) r.push(f);
  return r;
}
function aoiData(cursor) {
  const feats = [];
  if (aoi.ring) {
    feats.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [closeRing(aoi.ring)] } });
  } else if (aoi.verts.length) {
    const coords = aoi.verts.slice();
    if (cursor) coords.push(cursor);
    if (coords.length >= 2) feats.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } });
  }
  (aoi.ring || aoi.verts).forEach((c, i) =>
    feats.push({ type: "Feature", properties: { first: i === 0 }, geometry: { type: "Point", coordinates: c } }),
  );
  return { type: "FeatureCollection", features: feats };
}
function refreshAOI(cursor) {
  const s = map.getSource("aoi");
  if (s) s.setData(aoiData(cursor));
}
function startDraw() {
  clearAOI(false);
  aoi.drawing = true;
  aoi.verts = [];
  map.getCanvas().style.cursor = "crosshair";
  map.doubleClickZoom.disable();
  syncAOIButtons();
  toast("Click to add points · click the first point or double-click to finish · Esc to cancel");
}
function finishDraw() {
  if (aoi.verts.length < 3) {
    toast("Need at least 3 points");
    return;
  }
  aoi.ring = aoi.verts.slice();
  aoi.verts = [];
  aoi.drawing = false;
  map.getCanvas().style.cursor = "";
  map.doubleClickZoom.enable();
  refreshAOI();
  syncAOIButtons();
  scheduleMetrics();
}
function clearAOI(recompute = true) {
  aoi.drawing = false;
  aoi.verts = [];
  aoi.ring = null;
  map.getCanvas().style.cursor = "";
  map.doubleClickZoom.enable();
  refreshAOI();
  syncAOIButtons();
  if (recompute) scheduleMetrics();
}
function onAOIClick(e) {
  if (!aoi.drawing) return;
  if (aoi.verts.length >= 3) {
    const p0 = map.project(aoi.verts[0]);
    if (Math.hypot(p0.x - e.point.x, p0.y - e.point.y) < 12) {
      finishDraw();
      return;
    }
  }
  aoi.verts.push([e.lngLat.lng, e.lngLat.lat]);
  refreshAOI();
}
function onAOIDblClick(e) {
  if (!aoi.drawing) return;
  e.preventDefault();
  if (aoi.verts.length >= 4) aoi.verts.pop(); // drop the duplicate vertex the dblclick added
  finishDraw();
}
function syncAOIButtons() {
  const draw = $("draw-aoi");
  const clr = $("clear-aoi");
  draw.textContent = aoi.drawing ? "✓ Finish" : "▱ Draw AOI";
  draw.classList.toggle("active", aoi.drawing);
  clr.hidden = !(aoi.ring || aoi.drawing);
}
$("draw-aoi").onclick = () => (aoi.drawing ? finishDraw() : startDraw());
$("clear-aoi").onclick = () => clearAOI();
// test / debug hook: set the AOI polygon programmatically from [[lng,lat], ...]
window.__setAOI = (verts) => {
  aoi.ring = verts.map((v) => [v[0], v[1]]);
  aoi.verts = [];
  aoi.drawing = false;
  refreshAOI();
  syncAOIButtons();
  scheduleMetrics();
};
window.__clearAOI = () => clearAOI();

syncLabels();
