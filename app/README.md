# meta-chm-v2 webapp

Serverless canopy-height viewer. Streams cloud-native COGs from source.coop, colorizes
+ measures live in the browser. No server.

## Dev

```sh
bun install      # no deps yet, but creates bun.lock
bun run dev      # fullstack dev server with HMR -> http://localhost:3000
bun run bench    # headless analytics benchmark (must finish < 80 ms worst case)
bun run build    # minified bundle -> ./dist
```

`bun ./index.html` (what `dev` runs) uses Bun's built-in HTML bundler — ES modules in
`src/` get bundled on the fly; CDN globals (`maplibre-gl`, `geotiff`) stay external.

## Layout

```
app/
├── index.html              entry point (Bun HTML bundler)
├── src/
│   ├── main.js             app entry — map, COG protocol, controls, AOI
│   ├── analytics.js        pure analytics (rumple, gaps, edges, p98, clip)
│   └── style.css
├── bench/analytics.bench.mjs   headless perf check (runs in Bun and Node)
├── public/.nojekyll        copied into dist by build
└── package.json
```

## Deploy

GitHub Pages is served from this folder via `.github/workflows/pages.yml`: every push
to `main` that touches `app/**` rebuilds with Bun and publishes `app/dist`.

**One-time setup** (required to switch off the legacy branch-source):
in repo Settings → Pages → Source, pick **GitHub Actions**.
