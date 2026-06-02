// Headless profiling for the analytics suite — proves it stays snappy at the worst case
// the webapp allows (MAX_ANALYSIS_TILES = 64 -> an 8x8 grid of 256px tiles = 2048x2048).
//   node docs/analytics.bench.mjs
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const A = require("./analytics.js");

const TILE = 256;
const mpp = 1.4; // ~metres/pixel at z14

// Build a synthetic mosaic that stresses every metric: undulating canopy (rumple + sigma),
// scattered circular gaps (connected components), a few non-forest swaths (big components,
// edges/core), and a nodata corner (255 handling). w x h in tiles.
function makeMosaic(tilesX, tilesY) {
  const w = tilesX * TILE;
  const h = tilesY * TILE;
  const m = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      // base canopy 8..38 m, smoothly varying so rumple/sigma are non-trivial
      let v = 23 + 15 * Math.sin(x / 40) * Math.cos(y / 37);
      // non-forest swaths (clearings) -> large sub-threshold components
      if (((x >> 7) + (y >> 7)) % 7 === 0) v = 0;
      // scattered small gaps every ~24 px
      if ((x % 24 < 4) && (y % 24 < 4)) v = 1;
      let hv = Math.max(0, Math.min(254, Math.round(v)));
      // nodata corner
      if (x > w - 120 && y > h - 120) hv = 255;
      m[y * w + x] = hv;
    }
  }
  return { m, w, h };
}

function bench(tilesX, tilesY, runs) {
  const { m, w, h } = makeMosaic(tilesX, tilesY);
  const opts = { forestThr: 5, mpp, minGapPx: 4 };
  // warmup (JIT)
  for (let i = 0; i < 2; i++) A.analyzeMosaic(m, w, h, opts);
  const totals = [];
  let last;
  for (let i = 0; i < runs; i++) {
    const t = Number(process.hrtime.bigint());
    last = A.analyzeMosaic(m, w, h, opts);
    totals.push((Number(process.hrtime.bigint()) - t) / 1e6);
  }
  totals.sort((a, b) => a - b);
  const med = totals[totals.length >> 1];
  const max = totals[totals.length - 1];
  const px = w * h;
  console.log(`\n${tilesX}x${tilesY} tiles  (${w}x${h} = ${(px / 1e6).toFixed(1)}M px)`);
  console.log(`  total   median ${med.toFixed(1)} ms   max ${max.toFixed(1)} ms   (${runs} runs)`);
  console.log(
    `  stages  summary ${last.ms.summary.toFixed(1)} | rumple ${last.ms.rumple.toFixed(1)} | ` +
      `gaps ${last.ms.gaps.toFixed(1)} | edges ${last.ms.edges.toFixed(1)} ms`,
  );
  console.log(
    `  result  cover ${last.coverPct.toFixed(1)}% | p98 ${last.p.p98}m | sigma ${last.std.toFixed(1)}m | ` +
      `rumple ${last.rumple.toFixed(2)} | gaps ${last.gap.count} (frac ${last.gap.fractionPct.toFixed(1)}%) | ` +
      `core ${last.edge.corePct.toFixed(1)}%`,
  );
  return med;
}

console.log("CHM analytics benchmark — Node " + process.version);
bench(4, 4, 30); // typical zoomed-in view
const med = bench(8, 8, 30); // worst case the app permits (64 tiles)
const BUDGET = 80;
console.log(`\nworst-case budget: ${BUDGET} ms  ->  ${med <= BUDGET ? "PASS" : "FAIL"} (${med.toFixed(1)} ms)`);
process.exit(med <= BUDGET ? 0 : 1);
