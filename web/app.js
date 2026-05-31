// Serverless STAC tile browser for Meta CHM v2 (ml3).
// Filters the published stac-geoparquet in-browser with duckdb-wasm (HTTP range reads +
// row-group pruning over source.coop, which sends CORS headers), then offers direct
// downloads of the COG tiles from Meta's bucket. Downloads are plain links, so they are
// NOT subject to CORS — only the parquet read is, and source.coop allows it.

import maplibregl from "https://esm.sh/maplibre-gl@4.7.1";
import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";

const ITEMS_URL = "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet";
const S3_PREFIX = "s3://dataforgood-fb-data/";
const HTTPS_PREFIX = "https://dataforgood-fb-data.s3.amazonaws.com/";
const MAX_TILES = 2000;

const $ = (id) => document.getElementById(id);
const httpsHref = (s3) => s3.replace(S3_PREFIX, HTTPS_PREFIX);
const fmtBytes = (n) => {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

let lastRows = [];

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: [13.4, 52.5],
  zoom: 8,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

map.on("load", () => {
  map.addSource("tiles", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "tiles-fill",
    type: "fill",
    source: "tiles",
    paint: { "fill-color": "#4ade80", "fill-opacity": 0.12 },
  });
  map.addLayer({
    id: "tiles-line",
    type: "line",
    source: "tiles",
    paint: { "line-color": "#4ade80", "line-width": 1.1, "line-opacity": 0.85 },
  });
  map.addLayer({
    id: "tiles-hl",
    type: "fill",
    source: "tiles",
    paint: { "fill-color": "#d9f99d", "fill-opacity": 0.35 },
    filter: ["==", "id", "__none__"],
  });

  map.on("mousemove", "tiles-fill", (e) => {
    const id = e.features[0]?.properties.id;
    map.setFilter("tiles-hl", ["==", "id", id ?? "__none__"]);
    map.getCanvas().style.cursor = "pointer";
    highlightRow(id, true);
  });
  map.on("mouseleave", "tiles-fill", () => {
    map.setFilter("tiles-hl", ["==", "id", "__none__"]);
    map.getCanvas().style.cursor = "";
    highlightRow(null);
  });
});

let conn;
async function initDB() {
  const bundles = duckdb.getJsDelivrBundles();
  const bundle = await duckdb.selectBundle(bundles);
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }),
  );
  const worker = new Worker(workerUrl);
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  conn = await db.connect();
  await conn.query("INSTALL httpfs; LOAD httpfs;");
}

async function search() {
  if (!conn) return;
  const btn = $("search");
  btn.disabled = true;
  $("search-label").textContent = "searching…";
  const b = map.getBounds();
  const [w, s, e, n] = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  const sql = `
    SELECT id,
           assets.chm.href        AS href,
           assets.chm."file:size" AS size,
           bbox.xmin AS xmin, bbox.ymin AS ymin, bbox.xmax AS xmax, bbox.ymax AS ymax
    FROM read_parquet('${ITEMS_URL}')
    WHERE bbox.xmin <= ${e} AND bbox.xmax >= ${w}
      AND bbox.ymin <= ${n} AND bbox.ymax >= ${s}
    LIMIT ${MAX_TILES + 1}`;
  try {
    const res = await conn.query(sql);
    const rows = res.toArray().map((r) => {
      const o = r.toJSON();
      return {
        id: o.id,
        href: o.href,
        size: o.size == null ? null : Number(o.size),
        xmin: o.xmin,
        ymin: o.ymin,
        xmax: o.xmax,
        ymax: o.ymax,
      };
    });
    render(rows);
  } catch (err) {
    toast(`query failed: ${err.message ?? err}`);
  } finally {
    btn.disabled = false;
    $("search-label").textContent = "Search this view";
  }
}

function render(rows) {
  const capped = rows.length > MAX_TILES;
  lastRows = capped ? rows.slice(0, MAX_TILES) : rows;

  map.getSource("tiles").setData(toFC(lastRows));

  const total = lastRows.reduce((a, r) => a + (r.size ?? 0), 0);
  $("summary").hidden = false;
  $("summary").innerHTML = capped
    ? `<b>${MAX_TILES.toLocaleString()}+</b> tiles in view — zoom in to narrow. Showing first ${MAX_TILES.toLocaleString()} · <b>${fmtBytes(total)}</b>`
    : `<b>${lastRows.length.toLocaleString()}</b> tiles · <b>${fmtBytes(total)}</b> total`;
  $("bulk").hidden = lastRows.length === 0;

  const ol = $("results");
  ol.innerHTML = "";
  lastRows.forEach((r, i) => {
    const li = document.createElement("li");
    li.dataset.id = r.id;
    li.style.animationDelay = `${Math.min(i, 40) * 8}ms`;
    li.innerHTML = `
      <div class="meta">
        <span class="qk">${r.id}</span>
        <span class="sz">${fmtBytes(r.size)}</span>
      </div>
      <a class="dl" href="${httpsHref(r.href)}" download>download</a>`;
    li.addEventListener("mouseenter", () => {
      map.setFilter("tiles-hl", ["==", "id", r.id]);
    });
    li.addEventListener("mouseleave", () => {
      map.setFilter("tiles-hl", ["==", "id", "__none__"]);
    });
    ol.appendChild(li);
  });
  if (lastRows.length === 0) toast("no tiles here — try a land area");
}

function highlightRow(id, scroll = false) {
  document.querySelectorAll("#results li").forEach((li) => {
    const on = li.dataset.id === id;
    li.classList.toggle("hover", on);
    if (on && scroll) li.scrollIntoView({ block: "nearest" });
  });
}

// ---- bulk export helpers (no byte reads → no CORS needed) ----
function download(name, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

$("copy-urls").onclick = async () => {
  const urls = lastRows.map((r) => httpsHref(r.href)).join("\n");
  await navigator.clipboard.writeText(urls);
  toast(`copied ${lastRows.length} URLs`);
};
$("dl-script").onclick = () => {
  const lines = [
    "#!/usr/bin/env bash",
    "# Meta CHM v2 ml3 — selected tiles. Source bucket is anonymous (no AWS creds needed).",
    `# ${lastRows.length} tiles`,
    "set -euo pipefail",
    ...lastRows.map(
      (r) =>
        `aws s3 cp ${r.href} . --no-sign-request   # or: curl -O ${httpsHref(r.href)}`,
    ),
  ];
  download("download_chm_tiles.sh", lines.join("\n"), "text/x-shellscript");
};
$("dl-geojson").onclick = () => {
  download("chm_tiles.geojson", JSON.stringify(toFC(lastRows)), "application/geo+json");
};

// ---- geojson builders ----
function emptyFC() {
  return { type: "FeatureCollection", features: [] };
}
function toFC(rows) {
  return {
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      properties: { id: r.id, href: httpsHref(r.href), size: r.size },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [r.xmin, r.ymin],
            [r.xmax, r.ymin],
            [r.xmax, r.ymax],
            [r.xmin, r.ymax],
            [r.xmin, r.ymin],
          ],
        ],
      },
    })),
  };
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

$("search").onclick = search;

initDB()
  .then(() => {
    $("search").disabled = false;
    $("search-label").textContent = "Search this view";
  })
  .catch((err) => {
    $("search-label").textContent = "engine failed to load";
    toast(`duckdb-wasm init failed: ${err.message ?? err}`);
  });
