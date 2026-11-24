"""Resample NYC 2017 6in land cover to 3ft (6x6 majority) directly from the zip.

Reads full-resolution strips sequentially (the zip is one deflate stream, so
sequential access is fast and random access is slow) and takes the most common
non-nodata class in each 6x6 block.
"""

import sys
import time

import numpy as np
import rasterio
from rasterio.windows import Window

SRC = "/vsizip/Land_Cover.zip/Land_Cover/NYC_2017_LiDAR_LandCover.img"
DST = "out/nyc_landcover_2017_3ft.tif"
F = 6  # 0.5 ft -> 3 ft
STRIP = 192  # lcm(6, 64 block height): aligned to both source blocks and output pixels
NODATA = 0
NCLASS = 9  # values 0..8


def majority(a: np.ndarray) -> np.ndarray:
    """Majority of each FxF block, ignoring NODATA; ties go to the lower class."""
    h, w = a.shape
    blocks = a.reshape(h // F, F, w // F, F)
    counts = np.stack(
        [(blocks == c).sum(axis=(1, 3), dtype=np.uint8) for c in range(1, NCLASS)]
    )
    out = counts.argmax(axis=0).astype(np.uint8) + 1
    out[counts.max(axis=0) == 0] = NODATA
    return out


def main(max_strips: int | None = None) -> None:
    with rasterio.open(SRC) as src:
        out_w = -(-src.width // F)
        out_h = -(-src.height // F)
        profile = src.profile | {
            "driver": "GTiff",
            "width": out_w,
            "height": out_h,
            "transform": src.transform * src.transform.scale(F, F),
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "compress": "deflate",
            "BIGTIFF": "IF_SAFER",
        }
        n_strips = -(-src.height // STRIP)
        if max_strips:
            n_strips = min(n_strips, max_strips)

        t0 = time.time()
        with rasterio.open(DST, "w", **profile) as dst:
            for i in range(n_strips):
                row = i * STRIP
                rows = min(STRIP, src.height - row)
                data = src.read(1, window=Window(0, row, src.width, rows))
                # pad ragged right/bottom edges to a multiple of F
                pad = np.full((-(-rows // F) * F, out_w * F), NODATA, np.uint8)
                pad[:rows, : src.width] = data
                out = majority(pad)
                dst.write(out, 1, window=Window(0, row // F, out_w, out.shape[0]))

                el = time.time() - t0
                eta = el / (i + 1) * (n_strips - i - 1)
                print(
                    f"\r{i + 1}/{n_strips} strips  {el / 60:.1f} min elapsed  ETA {eta / 60:.1f} min",
                    end="",
                    flush=True,
                )
        print(f"\nwrote {DST}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
