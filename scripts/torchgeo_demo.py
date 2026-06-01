"""Prove MetaCHMv2 builds from the GeoParquet index (no COG downloads) and streams a patch."""

import time

from torchgeo.samplers import RandomGeoSampler

from chm_zarr.torchgeo_dataset import MetaCHMv2

t0 = time.time()
ds = MetaCHMv2()  # index from published https parquet; opens zero COGs
print(
    f"built index in {time.time() - t0:.1f}s | tiles={len(ds.index):,} | crs={ds.crs} | res={ds.res}"
)
print(f"bounds (3857): {ds.index.total_bounds}")

# Sample one 256x256 patch over Berlin by slicing the dataset directly.
# Berlin ~ lon 13.40, lat 52.52 -> EPSG:3857
x0, y0 = 1492000.0, 6895000.0
side = 256 * ds.res[0]
t1 = time.time()
sample = ds[x0 : x0 + side, y0 - side : y0]  # GeoSlice (x, y); time defaults to full
img = sample["image"]
dt = time.time() - t1
a = img.numpy()
print(f"\nstreamed patch in {dt:.2f}s (https range reads, no full COG download)")
print(
    f"  shape={tuple(img.shape)} dtype={img.dtype} max={a.max():.0f}m mean_nonzero={a[a > 0].mean():.2f}m cover={(a > 0).mean():.1%}"
)

# And a few random samples to exercise the sampler path
sampler = RandomGeoSampler(ds, size=256, length=3)
n = 0
for q in sampler:
    s = ds[q]
    n += 1
print(f"RandomGeoSampler produced {n} patches OK")
