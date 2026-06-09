"""Serverless STAC search -> lazily-loaded xarray cube via odc.stac.

Searches the published stac-geoparquet (no server), then loads the matching COGs into a
dask-backed xarray DataArray in native EPSG:3857 (no reprojection). Reads stream as
byte-range requests against the source.coop COGs (https, CORS + range).

    uv run --group examples python examples/odc_stac_load.py
"""

import os

import odc.stac
import pystac
from rustac import DuckdbClient

ITEMS = "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet"
BBOX = [13.30, 52.45, 13.45, 52.55]  # small Berlin AOI

# Public https COGs on source.coop (CORS + range); GDAL/rasterio read them unsigned via /vsicurl.
os.environ.update(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-west-2")


def main() -> None:
    item_dicts = DuckdbClient().search(ITEMS, bbox=BBOX, max_items=25)
    items = [pystac.Item.from_dict(d) for d in item_dicts]
    print(f"{len(items)} items intersect {BBOX}")

    chm = odc.stac.load(
        items,
        bands=["chm"],
        bbox=BBOX,
        resolution=10,  # metres (EPSG:3857)
        chunks={"x": 2048, "y": 2048},  # dask-lazy; reads happen on .compute()
    )
    print(f"cube dims={dict(chm.sizes)} crs={chm.odc.crs}")

    heights = chm["chm"].isel(time=0).compute()
    v = heights.values
    print(
        f"canopy height (m): max={v.max()} "
        f"mean_nonzero={v[v > 0].mean():.2f} cover={(v > 0).mean():.1%}"
    )


if __name__ == "__main__":
    main()
