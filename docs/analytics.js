// Forest-structure analytics over a canopy-height mosaic (uint8 metres, R channel).
// Pure math, no DOM — runs both as a browser <script> (window.CHMAnalytics) and under
// Node `require()` so docs/analytics.bench.mjs can profile it headless.
//
// Pixel semantics (mirrors the colorization LUT in app.js):
//   h = 0        -> ground / no canopy   (counts as observed land, not canopy)
//   1 <= h <= 254-> canopy height, metres
//   h = 255      -> nodata fill          (excluded from every statistic)
//
// Metrics grounded in the CHM literature:
//   - canopy cover  : % of observed land with canopy >= threshold (FAO-style cover)
//   - p98 top height: robust stand-top height (GEDI RH98 analogue; max is outlier-noisy)
//   - rugosity (sigma): canopy-height variability
//   - rumple        : canopy 3-D surface area / planar area  (structural complexity)
//   - canopy gaps   : connected sub-threshold openings — gap fraction, count, size dist.
//   - edges/core    : forest fragmentation (edge vs. interior pixels, edge density)
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CHMAnalytics = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const NODATA = 255;

  // Histogram-derived height stats over canopy pixels (1..254) + cover over observed land.
  function summarize(heights, forestThr) {
    const counts = new Uint32Array(256);
    for (let i = 0; i < heights.length; i++) counts[heights[i]]++;
    let observed = 0; // h in [0,254]  (land we actually saw)
    for (let h = 0; h <= 254; h++) observed += counts[h];
    let canopy = 0; // h in [1,254]
    let sum = 0;
    let sumsq = 0;
    let max = 0;
    let aboveThr = 0;
    for (let h = 1; h <= 254; h++) {
      const c = counts[h];
      if (!c) continue;
      canopy += c;
      sum += h * c;
      sumsq += h * h * c;
      max = h;
      if (h >= forestThr) aboveThr += c;
    }
    const mean = canopy ? sum / canopy : 0;
    const variance = canopy ? Math.max(0, sumsq / canopy - mean * mean) : 0;
    const pct = (q) => {
      // q-th percentile of the canopy-height distribution (1..254)
      const target = canopy * q;
      let acc = 0;
      for (let h = 1; h <= 254; h++) {
        acc += counts[h];
        if (acc >= target) return h;
      }
      return max;
    };
    return {
      counts,
      observed,
      canopy,
      mean,
      std: Math.sqrt(variance),
      max,
      coverPct: observed ? (100 * aboveThr) / observed : 0,
      p: { p25: pct(0.25), p50: pct(0.5), p75: pct(0.75), p90: pct(0.9), p95: pct(0.95), p98: pct(0.98) },
    };
  }

  // Rumple index: mean of the per-cell surface-area factor sqrt(1 + (dz/dx)^2 + (dz/dy)^2),
  // which equals (canopy 3-D surface area / planar area). 1.0 = flat; higher = rougher canopy.
  function rumple(heights, w, h, mpp) {
    let sum = 0;
    let n = 0;
    for (let y = 0; y < h; y++) {
      const row = y * w;
      for (let x = 0; x < w; x++) {
        const cur = heights[row + x];
        if (cur > 254) continue;
        let gx = 0;
        let gy = 0;
        if (x + 1 < w) {
          const r = heights[row + x + 1];
          if (r <= 254) gx = (r - cur) / mpp;
        }
        if (y + 1 < h) {
          const d = heights[row + w + x];
          if (d <= 254) gy = (d - cur) / mpp;
        }
        sum += Math.sqrt(1 + gx * gx + gy * gy);
        n++;
      }
    }
    return n ? sum / n : 1;
  }

  // Canopy gaps: 4-connected components of sub-threshold openings (h in [0, thr), excl. 255).
  // Destroys nothing — works on a scratch copy. minGapPx drops speckle below the mapping unit.
  function gaps(heights, w, h, forestThr, mpp, minGapPx) {
    const n = w * h;
    // scratch mask: 1 = open (gap candidate), 0 = canopy/nodata/visited
    const open = new Uint8Array(n);
    for (let i = 0; i < n; i++) open[i] = heights[i] < forestThr ? 1 : 0; // 255 -> 0 (excluded)
    const stack = new Int32Array(n);
    const sizes = [];
    let openPx = 0;
    for (let s = 0; s < n; s++) {
      if (!open[s]) continue;
      let sp = 0;
      stack[sp++] = s;
      open[s] = 0;
      let size = 0;
      while (sp > 0) {
        const p = stack[--sp];
        size++;
        const x = p % w;
        const y = (p - x) / w;
        if (x > 0 && open[p - 1]) { open[p - 1] = 0; stack[sp++] = p - 1; }
        if (x + 1 < w && open[p + 1]) { open[p + 1] = 0; stack[sp++] = p + 1; }
        if (y > 0 && open[p - w]) { open[p - w] = 0; stack[sp++] = p - w; }
        if (y + 1 < h && open[p + w]) { open[p + w] = 0; stack[sp++] = p + w; }
      }
      openPx += size;
      if (size >= minGapPx) sizes.push(size);
    }
    const pxArea = mpp * mpp;
    sizes.sort((a, b) => a - b);
    const count = sizes.length;
    const median = count ? sizes[count >> 1] * pxArea : 0;
    const largest = count ? sizes[count - 1] * pxArea : 0;
    let totalGapPx = 0;
    for (let i = 0; i < count; i++) totalGapPx += sizes[i];
    return {
      count,
      fractionPct: n ? (100 * openPx) / n : 0,
      medianM2: median,
      largestM2: largest,
      meanM2: count ? (totalGapPx * pxArea) / count : 0,
    };
  }

  // Fragmentation: a forest pixel (h >= thr) is "edge" if any 4-neighbour is non-forest
  // (h < thr; nodata neighbours are ignored, not treated as edge). Returns core/edge split
  // and edge density (metres of forest boundary per hectare of observed land).
  function edges(heights, w, h, forestThr, mpp) {
    const n = w * h;
    let forest = 0;
    let edgePx = 0;
    let segments = 0; // forest|non-forest adjacencies = boundary length in pixel-edges
    let observed = 0;
    for (let y = 0; y < h; y++) {
      const row = y * w;
      for (let x = 0; x < w; x++) {
        const cur = heights[row + x];
        if (cur <= 254) observed++;
        if (cur < forestThr || cur > 254) continue; // forest = [thr,254]
        forest++;
        let seg = 0; // non-forest neighbours (excl. nodata) = boundary pixel-edges
        let v;
        if (x > 0 && (v = heights[row + x - 1]) <= 254 && v < forestThr) seg++;
        if (x + 1 < w && (v = heights[row + x + 1]) <= 254 && v < forestThr) seg++;
        if (y > 0 && (v = heights[row - w + x]) <= 254 && v < forestThr) seg++;
        if (y + 1 < h && (v = heights[row + w + x]) <= 254 && v < forestThr) seg++;
        if (seg) { segments += seg; edgePx++; }
      }
    }
    const obsAreaHa = (observed * mpp * mpp) / 1e4;
    return {
      forestPx: forest,
      corePct: forest ? (100 * (forest - edgePx)) / forest : 0,
      edgePct: forest ? (100 * edgePx) / forest : 0,
      edgeDensityMperHa: obsAreaHa ? (segments * mpp) / obsAreaHa : 0,
    };
  }

  // Run the full suite over a mosaic; returns metrics + a per-stage timing breakdown (ms).
  function analyzeMosaic(heights, w, h, opts) {
    const forestThr = opts.forestThr;
    const mpp = opts.mpp;
    const minGapPx = opts.minGapPx == null ? 4 : opts.minGapPx;
    const now = typeof performance !== "undefined" ? () => performance.now() : () => Number(process.hrtime.bigint()) / 1e6;
    const t0 = now();
    const s = summarize(heights, forestThr);
    const t1 = now();
    const ru = rumple(heights, w, h, mpp);
    const t2 = now();
    const g = gaps(heights, w, h, forestThr, mpp, minGapPx);
    const t3 = now();
    const e = edges(heights, w, h, forestThr, mpp);
    const t4 = now();
    return {
      ...s,
      rumple: ru,
      gap: g,
      edge: e,
      mpp,
      ms: { summary: t1 - t0, rumple: t2 - t1, gaps: t3 - t2, edges: t4 - t3, total: t4 - t0 },
    };
  }

  return { summarize, rumple, gaps, edges, analyzeMosaic, NODATA };
});
