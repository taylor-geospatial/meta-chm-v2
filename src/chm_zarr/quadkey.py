"""Bing/Microsoft quadkey <-> tile/lonlat helpers (no external deps for hot path)."""

import math


def quadkey_to_tile(qk: str) -> tuple[int, int, int]:
    x = y = 0
    z = len(qk)
    for i, c in enumerate(qk):
        bit = z - i - 1
        mask = 1 << bit
        match c:
            case "0":
                pass
            case "1":
                x |= mask
            case "2":
                y |= mask
            case "3":
                x |= mask
                y |= mask
            case _:
                raise ValueError(f"bad quadkey char {c!r} in {qk!r}")
    return x, y, z


def tile_to_lonlat_bbox(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    n = 1 << z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon_w, lat_s, lon_e, lat_n


def tile_to_mercator_bbox(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    n = 1 << z
    R = 20037508.342789244
    minx = -R + 2 * R * x / n
    maxx = -R + 2 * R * (x + 1) / n
    maxy = R - 2 * R * y / n
    miny = R - 2 * R * (y + 1) / n
    return minx, miny, maxx, maxy
