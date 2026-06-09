"""Headless responsiveness profiler for the canopy viewer.

Serves app/ raw (native ES modules — no build needed), drives the real MapLibre
app with Playwright (SwiftShader WebGL), and measures the things that actually
gate "snappy":

  1. cold tile decode   — per-tile {read, color, bitmap} ms at the protocol
  2. recolor latency    — slider change -> all tiles repainted (does it re-decode?)
  3. metrics wall-clock — moveend -> panel updated (serial tile reads + analytics)
  4. main-thread jank   — longtask count/total while panning

Run:  uv run python app/bench/profile_responsiveness.py
"""

import functools
import http.server
import json
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parent.parent
VIEW = {"center": [8.21, 48.27], "zoom": 12}  # Black Forest — real dense canopy


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# Everything below runs in the page. Returns a dict of measurements.
PROFILE_JS = r"""
async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const map = window.__map;
  const perf = window.__perf;
  perf.on = true;

  // longtask observer (main-thread blocks > 50ms) — the source of visible jank
  const longtasks = [];
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) longtasks.push(e.duration); })
      .observe({ entryTypes: ["longtask"] });
  } catch (e) {}

  const idle = (ms = 12000) => new Promise((res) => { map.once("idle", res); setTimeout(res, ms); });

  // --- 1. cold load at the view ---
  window.__perfReset();
  const tCold = performance.now();
  map.jumpTo(VIEW);
  await idle();
  const coldMs = performance.now() - tCold;
  const cold = perf.rec.slice();

  // --- 3. metrics wall-clock: time a forced recompute (serial tile reads + analytics) ---
  await sleep(500);
  const body = document.getElementById("metrics-body");
  const tM = performance.now();
  map.fire("moveend");                       // triggers scheduleMetrics (350ms debounce)
  for (let i = 0; i < 200; i++) {            // poll until the analytics footer lands
    if ((body.innerText || "").includes("ms ·")) break;
    await sleep(50);
  }
  const metricsWall = performance.now() - tM;
  const footer = (body.innerText.match(/([\d.]+) ms · (\d+) tiles/) || []);

  // --- 2. recolor latency: nudge the LUT, time until all tiles repaint, see if it re-decodes ---
  window.__perfReset();
  const tR = performance.now();
  const h = document.getElementById("hmax");
  h.value = String(Math.max(20, (+h.value) - 3));
  h.dispatchEvent(new Event("input", { bubbles: true }));  // applyColor (140ms debounce) -> setTiles
  await idle();
  const recolorMs = performance.now() - tR;
  const recolor = perf.rec.slice();

  // --- 4. jank while panning a few tiles ---
  longtasks.length = 0;
  const c = map.getCenter();
  for (const d of [0.03, -0.03, 0.03, -0.03]) {
    map.panTo([c.lng + d, c.lat], { duration: 250 });
    await sleep(400);
  }
  await idle(4000);

  const agg = (recs, k) => {
    const v = recs.map((r) => r[k]).sort((a, b) => a - b);
    if (!v.length) return null;
    const q = (p) => v[Math.min(v.length - 1, Math.floor(v.length * p))];
    return { n: v.length, med: q(0.5), p90: q(0.9), max: v[v.length - 1],
             sum: v.reduce((a, b) => a + b, 0) };
  };
  return {
    coldMs, recolorMs, metricsWall,
    metricsTiles: +footer[2] || null, metricsAnalyticsMs: +footer[1] || null,
    cold: { tiles: cold.length, read: agg(cold, "read"), color: agg(cold, "color"), bitmap: agg(cold, "bitmap") },
    recolor: { tiles: recolor.length, read: agg(recolor, "read"), color: agg(recolor, "color"), bitmap: agg(recolor, "bitmap") },
    longtasks: { count: longtasks.length, totalMs: longtasks.reduce((a, b) => a + b, 0), maxMs: Math.max(0, ...longtasks) },
  };
}
""".replace("VIEW", json.dumps(VIEW))


def fmt(agg: dict | None) -> str:
    if not agg:
        return "—"
    return f"med {agg['med']:.1f}  p90 {agg['p90']:.1f}  max {agg['max']:.1f} ms  (sum {agg['sum']:.0f})"


def main() -> None:
    server, url = serve(APP)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                args=[
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                    "--ignore-gpu-blocklist",
                ],
            )
            pg = browser.new_page(viewport={"width": 1280, "height": 900})
            pg.goto(url + "/index.html", wait_until="load")
            pg.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            r = pg.evaluate(PROFILE_JS)
            browser.close()
    finally:
        server.shutdown()

    print("\n=== canopy viewer responsiveness ===")
    print(f"cold load to idle : {r['coldMs']:.0f} ms  ({r['cold']['tiles']} tile decodes)")
    print(f"  read   (fetch+decode): {fmt(r['cold']['read'])}")
    print(f"  color  (LUT loop)    : {fmt(r['cold']['color'])}")
    print(f"  bitmap (createBitmap): {fmt(r['cold']['bitmap'])}")
    print(
        f"\nrecolor (slider) to idle: {r['recolorMs']:.0f} ms  ({r['recolor']['tiles']} tile re-decodes)"
    )
    print(
        f"  read   (fetch+decode): {fmt(r['recolor']['read'])}   <- if large, recolor is needlessly re-decoding"
    )
    print(f"  color  (LUT loop)    : {fmt(r['recolor']['color'])}")
    print(f"  bitmap (createBitmap): {fmt(r['recolor']['bitmap'])}")
    print(
        f"\nmetrics moveend->panel: {r['metricsWall']:.0f} ms wall  "
        f"(analytics {r['metricsAnalyticsMs']} ms over {r['metricsTiles']} tiles)"
    )
    print(
        f"  -> serial tile-read overhead ~= {r['metricsWall'] - (r['metricsAnalyticsMs'] or 0) - 350:.0f} ms "
        f"(wall - analytics - 350ms debounce)"
    )
    print(
        f"\njank while panning: {r['longtasks']['count']} longtasks, "
        f"{r['longtasks']['totalMs']:.0f} ms total, worst {r['longtasks']['maxMs']:.0f} ms"
    )
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
