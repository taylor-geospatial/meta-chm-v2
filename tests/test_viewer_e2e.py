"""End-to-end browser test for the canopy-height viewer.

Serves app/ over a local HTTP server (native ES modules — no build step), drives
the real app with Playwright (SwiftShader WebGL so MapLibre runs headless), flies
to a forested view, and asserts the forest-structure analytics compute from live
source.coop tiles.

Requires network (source.coop + EOX) and a Playwright Chromium build:
    uv run playwright install chromium
Run with:  uv run pytest tests/test_viewer_e2e.py -v
"""

import functools
import http.server
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parent.parent / "app"

# Black Forest, DE — dense canopy with real CHM coverage; z12 keeps the view to a few tiles.
VIEW = {"center": [8.21, 48.27], "zoom": 12}
# A small polygon inside that view, for the drawn-AOI path.
AOI_POLY = [[8.19, 48.25], [8.24, 48.25], [8.24, 48.29], [8.19, 48.29]]

# Run the fly-to in the page, then poll the metrics panel until the analytics footer
# ("<n> ms · ...") appears, signalling a completed compute pass.
DRIVE_JS = """
async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 80 && !window.__map; i++) await sleep(150);
  if (!window.__map) return { ok: false, text: "map failed to init (WebGL?)" };
  const map = window.__map;
  await new Promise((res) => {
    map.once("idle", res);
    map.jumpTo(__VIEW__);
    setTimeout(res, 8000);
  });
  const body = document.getElementById("metrics-body");
  for (let i = 0; i < 120; i++) {
    const t = body.innerText || "";
    if (t.includes("ms \\u00b7")) return { ok: true, text: t };
    if (/no (canopy|data)/i.test(t)) return { ok: false, text: t };
    await sleep(250);
  }
  return { ok: false, text: "timeout: " + (body.innerText || "") };
}
""".replace("__VIEW__", json.dumps(VIEW))


@pytest.fixture(scope="module")
def app_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(APP))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def page(app_url):
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
        pg.goto(app_url + "/index.html", wait_until="load")
        yield pg
        browser.close()


def test_panel_renders(page):
    # Static UI must render even before any tiles load.
    assert "Canopy Height Analysis" in page.inner_text("h1")
    assert page.is_visible("#forest-thr")  # canopy threshold now always shown


def test_live_structure_metrics(page):
    result = page.evaluate(DRIVE_JS)
    text = result["text"]
    assert result["ok"], f"metrics did not compute: {text!r}"
    # every analytics section + the new structure metrics are present
    for label in [
        "CANOPY HEIGHT",
        "STRUCTURE",
        "Top height p98",
        "Rugosity",
        "Rumple index",
        "Canopy gaps",
        "Core forest",
    ]:
        assert label in text, f"missing {label!r} in:\n{text}"
    # p98 is a robust top height: present, positive, and <= max
    p98_m = re.search(r"Top height p98\s*\n?\s*(\d+)\s*m", text)
    max_m = re.search(r"Max\s*\n?\s*(\d+)\s*m", text)
    assert p98_m, f"could not parse p98 from:\n{text}"
    assert max_m, f"could not parse max from:\n{text}"
    p98, mx = int(p98_m.group(1)), int(max_m.group(1))
    assert 0 < p98 <= mx, f"p98={p98} max={mx}"


# Fly to the view at a measurable zoom, set the AOI polygon programmatically (the same code
# path a drawn polygon takes), and poll until the AOI-scoped metrics compute.
DRIVE_AOI_JS = """
async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 80 && !window.__setAOI; i++) await sleep(150);
  if (!window.__setAOI) return { ok: false, text: "no __setAOI hook" };
  const map = window.__map;
  await new Promise((res) => {
    map.once("idle", res);
    map.jumpTo({ center: [8.215, 48.27], zoom: 13 });
    setTimeout(res, 8000);
  });
  window.__setAOI(__POLY__);
  const body = document.getElementById("metrics-body");
  for (let i = 0; i < 120; i++) {
    const t = body.innerText || "";
    if (t.includes("AOI") && t.includes("ms \\u00b7")) {
      return { ok: true, text: t, scope: document.getElementById("scope-label").innerText };
    }
    if (/no (canopy|data)/i.test(t)) return { ok: false, text: t };
    await sleep(250);
  }
  return { ok: false, text: "timeout: " + (body.innerText || "") };
}
""".replace("__POLY__", json.dumps(AOI_POLY))


def test_drawn_aoi_metrics(page):
    result = page.evaluate(DRIVE_AOI_JS)
    text = result["text"]
    assert result["ok"], f"AOI metrics did not compute: {text!r}"
    assert result["scope"] == "AOI"
    # scoped header reports the AOI area, and the structure metrics still compute
    assert re.search(r"AOI · [\d.]+ (m²|ha|km²)", text, re.IGNORECASE), f"no AOI area in:\n{text}"
    for label in ["Canopy gaps", "Rumple index", "Core forest"]:
        assert label in text, f"missing {label!r} in:\n{text}"
