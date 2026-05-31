"""Serverless STAC search over the published stac-geoparquet, then read a COG.

No STAC API server required: rustac's DuckdbClient runs the search in-process directly
against the remote `items.parquet`. Found Items carry `s3://` hrefs into Meta's public
COGs, which rasterio reads anonymously.

    uv run --group examples python examples/search_and_read.py
"""

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rustac import DuckdbClient

ITEMS = "https://data.source.coop/tge-labs/meta-chm-v2/stac/items.parquet"
COLLECTION = "dinov3-global-chm-v2-ml3"
BBOX = [13.0, 52.0, 13.4, 52.3]  # Berlin


def main() -> None:
    client = DuckdbClient()
    items = client.search(ITEMS, collections=[COLLECTION], bbox=BBOX, max_items=50)
    print(f"{len(items)} tiles intersect {BBOX}")

    item = items[0]
    href = item["assets"]["chm"]["href"]
    print(f"reading {item['id']} -> {href}")

    # COGs are anonymous in us-east-1; read a decimated overview (no full-res download).
    with (
        rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION="us-east-1"),
        rasterio.open(href) as ds,
    ):
        arr = ds.read(1, out_shape=(1024, 1024), resampling=Resampling.average)
    valid = arr[arr > 0]
    print(
        f"canopy height (m): max={arr.max()} "
        f"mean_nonzero={valid.mean():.2f} cover={np.count_nonzero(arr) / arr.size:.1%}"
    )


if __name__ == "__main__":
    main()
