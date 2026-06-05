// Headless profiling for the analytics suite — proves it stays snappy at the worst case
// the webapp allows (MAX_ANALYSIS_TILES = 64 -> an 8x8 grid of 256px tiles = 2048x2048).
//   bun run bench
import * as A from "../src/analytics.js";

const TILE = 256;
const mpp = 1.4;

function makeMosaic(tilesX, tilesY) {
  const w = tilesX * TILE;
  const h = tilesY * TILE;
  const m = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let v = 23 + 15 * Math.sin(x / 40) * Math.cos(y / 37);
      if (((x >> 7) + (y >> 7)) % 7 === 0) v = 0;
      if ((x % 24 < 4) && (y % 24 < 4)) v = 1;
      let hv = Math.max(0, Math.min(254, Math.round(v)));
      if (x > w - 120 && y > h - 120) hv = 255;
      m[y * w + x] = hv;
    }
  }
  return { m, w, h };
}

const hrNow = typeof Bun !== "undefined"
  ? () => performance.now()
  : () => Number(process.hrtime.bigint()) / 1e6;

function bench(tilesX, tilesY, runs) {
  const { m, w, h } = makeMosaic(tilesX, tilesY);
  const opts = { forestThr: 5, mpp, minGapPx: 4 };
  for (let i = 0; i < 2; i++) A.analyzeMosaic(m, w, h, opts);
  const totals = [];
  let last;
  for (let i = 0; i < runs; i++) {
    const t = hrNow();
    last = A.analyzeMosaic(m, w, h, opts);
    totals.push(hrNow() - t);
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

const runtime = typeof Bun !== "undefined" ? `Bun ${Bun.version}` : `Node ${process.version}`;
console.log(`CHM analytics benchmark — ${runtime}`);
bench(4, 4, 30);
const med = bench(8, 8, 30);

{
  const { m, w, h } = makeMosaic(8, 8);
  const cx = w / 2;
  const cy = h / 2;
  const r = w * 0.45;
  const ring = [cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy];
  const ts = [];
  let masked = 0;
  for (let i = 0; i < 30; i++) {
    const c = m.slice();
    const t = hrNow();
    A.clipToPolygon(c, w, h, ring);
    ts.push(hrNow() - t);
    if (i === 0) for (let k = 0; k < c.length; k++) if (c[k] === 255) masked++;
  }
  ts.sort((a, b) => a - b);
  console.log("\nclipToPolygon (8x8, half-area diamond)");
  console.log(`  median ${ts[ts.length >> 1].toFixed(1)} ms   masked ${((100 * masked) / (w * h)).toFixed(0)}% -> nodata`);
}

const BUDGET = 80;
console.log(`\nworst-case budget: ${BUDGET} ms  ->  ${med <= BUDGET ? "PASS" : "FAIL"} (${med.toFixed(1)} ms)`);
process.exit(med <= BUDGET ? 0 : 1);
