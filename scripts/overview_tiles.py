"""Generate z0-9 raw-height web tiles from the global overview COG -> MBTiles.

Low zoom levels (z<10) span multiple source tiles, so they can't be built per-source-tile
like z10-14. Instead we read them from the single global overview COG (EPSG:3857, single-band
uint8 height). Output matches the z10-14 shards (lossless grayscale WebP, TMS rows) so the two
merge into one z0-14 raw-height PMTiles.

    python scripts/overview_tiles.py out/chm_overview_z8.tif out/tiles_lo.mbtiles --zmax 9
"""

import argparse
import io
import sqlite3

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from tqdm import tqdm

R = 20037508.342789244


def merc_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    size = 2 * R / (1 << z)
    w = -R + x * size
    n = R - y * size
    return w, n - size, w + size, n  # west, south, east, north


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cog")
    ap.add_argument("out")
    ap.add_argument("--zmax", type=int, default=9)
    args = ap.parse_args()

    con = sqlite3.connect(args.out)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE metadata (name text, value text)")
    con.execute(
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)"
    )
    con.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [
            ("name", "meta-chm-v2-lo"),
            ("format", "webp"),
            ("minzoom", "0"),
            ("maxzoom", str(args.zmax)),
        ],
    )

    n = 0
    with rasterio.open(args.cog) as ds:
        # data latitude band (EPSG:3857 y extent) -> limit y tiles, skip polar ocean
        ymin_m, ymax_m = ds.bounds.bottom, ds.bounds.top
        for z in range(args.zmax + 1):
            ntiles = 1 << z
            # y range overlapping the data band
            y_lo = max(0, int((R - ymax_m) / (2 * R) * ntiles))
            y_hi = min(ntiles - 1, int((R - ymin_m) / (2 * R) * ntiles))
            rows = []
            for x in range(ntiles):
                for y in range(y_lo, y_hi + 1):
                    w, s, e, nth = merc_bounds(x, y, z)
                    win = from_bounds(w, s, e, nth, ds.transform)
                    arr = ds.read(
                        1,
                        window=win,
                        out_shape=(256, 256),
                        boundless=True,
                        fill_value=0,
                        resampling=Resampling.average,
                    ).astype(np.uint8)
                    if arr.max() == 0:
                        continue
                    buf = io.BytesIO()
                    Image.fromarray(arr, "L").save(buf, "WEBP", lossless=True, method=4)
                    rows.append((z, x, (1 << z) - 1 - y, sqlite3.Binary(buf.getvalue())))
            con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
            con.commit()
            n += len(rows)
            tqdm.write(f"z{z}: {len(rows)} tiles")
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    con.close()
    print(f"done: {n} tiles -> {args.out}")


if __name__ == "__main__":
    main()
