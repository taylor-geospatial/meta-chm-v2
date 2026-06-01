"""Merge per-shard MBTiles into one, then convert to PMTiles. Idempotent / resumable.

Shards are disjoint at z10+, so merging is a plain INSERT of each shard's tiles. Re-running
rebuilds the merged MBTiles from whatever shards are present. The final
``pmtiles convert`` step uses the go-pmtiles binary (handles Hilbert clustering + dedup).

Low zooms (z0-9) are intentionally NOT here — the web map uses the global overview COG for
those and this PMTiles for z10-zmax.

    python scripts/merge_tiles.py --shards-dir out/tiles_shards --out out/chm_z14.mbtiles
    pmtiles convert out/chm_z14.mbtiles out/chm_z14.pmtiles   # then upload the .pmtiles
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards-dir", default="out/tiles_shards")
    ap.add_argument("--out", default="out/chm_z14.mbtiles")
    ap.add_argument("--zmax", type=int, default=14)
    args = ap.parse_args()

    shards = sorted(Path(args.shards_dir).glob("shard_*.mbtiles"))
    done = sorted(Path(args.shards_dir).glob("shard_*.done"))
    if len(shards) != len(done):
        print(f"WARNING: {len(shards)} mbtiles but {len(done)} .done — incomplete shards exist")
        print("  re-run the array (completed shards are skipped) before merging for a full build")
    if not shards:
        sys.exit("no shards found")
    print(f"merging {len(shards)} shards -> {args.out}")

    out = Path(args.out)
    out.unlink(missing_ok=True)
    con = sqlite3.connect(out)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE metadata (name text, value text)")
    con.execute(
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)"
    )
    con.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [
            ("name", "meta-chm-v2"),
            ("format", "webp"),
            ("minzoom", "10"),
            ("maxzoom", str(args.zmax)),
            ("type", "overlay"),
        ],
    )

    t0 = time.time()
    total = 0
    for i, sh in enumerate(shards):
        con.execute("ATTACH DATABASE ? AS s", (str(sh),))
        con.execute(
            "INSERT INTO tiles SELECT zoom_level, tile_column, tile_row, tile_data FROM s.tiles"
        )
        con.commit()
        con.execute("DETACH DATABASE s")
        if (i + 1) % 100 == 0:
            total = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
            print(f"  {i + 1}/{len(shards)} shards, {total:,} tiles, {time.time() - t0:.0f}s")

    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.commit()
    total = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    print(f"merged {total:,} tiles in {time.time() - t0:.0f}s -> {args.out}")
    print(f"next: pmtiles convert {args.out} {out.with_suffix('.pmtiles')}")


if __name__ == "__main__":
    main()
