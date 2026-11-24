"""Resample NYC 2017 6in land cover to 3ft (6x6 majority) directly from the zip.

Reads full-resolution strips sequentially (the zip is one deflate stream, so
sequential access is fast and random access is slow) and takes the most common
non-nodata class in each 6x6 block. The next strip is read in a background
thread while the current one is reduced across all cores.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
from rasterio.windows import Window
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

SRC = "/vsizip/Land_Cover.zip/Land_Cover/NYC_2017_LiDAR_LandCover.img"
DST = "out/nyc_landcover_2017_3ft.tif"
F = 6  # 0.5 ft -> 3 ft
STRIP = 192  # lcm(6, 64 block height): aligned to both source blocks and output pixels
NODATA = 0
NCLASS = 9  # values 0..8
BITS = 6  # per-class count field; F*F = 36 < 2**6

# Class c (1..8) -> 1 << BITS*(c-1); NODATA and anything else -> 0. Summing these
# over a block yields all 8 class counts packed into one uint64 in a single pass.
ONEHOT = np.zeros(256, np.uint64)
ONEHOT[1:NCLASS] = np.uint64(1) << (BITS * np.arange(NCLASS - 1, dtype=np.uint64))
SHIFTS = (BITS * np.arange(NCLASS - 1, dtype=np.uint64))[:, None, None]


def majority(a: np.ndarray) -> np.ndarray:
    """Majority of each FxF block, ignoring NODATA; ties go to the lower class."""
    h, w = a.shape
    packed = ONEHOT[a].reshape(h, w // F, F).sum(axis=2)
    packed = packed.reshape(h // F, F, w // F).sum(axis=1)
    counts = ((packed >> SHIFTS) & np.uint64(2**BITS - 1)).astype(np.uint8)
    out = counts.argmax(axis=0).astype(np.uint8) + 1
    out[packed == 0] = NODATA
    return out


def majority_parallel(a: np.ndarray, pool: ThreadPoolExecutor, n: int) -> np.ndarray:
    """majority() over n column chunks (multiples of F) in parallel; numpy drops the GIL."""
    edges = np.linspace(0, a.shape[1] // F, n + 1).astype(int) * F
    parts = pool.map(lambda lr: majority(a[:, lr[0] : lr[1]]), zip(edges, edges[1:]))
    return np.hstack(list(parts))


def main(max_strips: int | None = None) -> None:
    os.makedirs(os.path.dirname(DST), exist_ok=True)
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

        ncpu = os.cpu_count() or 1
        # uint8 source: 1 byte per pixel, so progress is in uncompressed source bytes
        total_bytes = min(n_strips * STRIP, src.height) * src.width

        def read(i: int) -> np.ndarray:
            row = i * STRIP
            rows = min(STRIP, src.height - row)
            # pad ragged right/bottom edges to a multiple of F
            pad = np.full((-(-rows // F) * F, out_w * F), NODATA, np.uint8)
            src.read(
                1, window=Window(0, row, src.width, rows), out=pad[:rows, : src.width]
            )
            return pad

        progress = Progress(
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            TextColumn("strip {task.fields[strip]}/" + str(n_strips)),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
        )

        with (
            rasterio.open(DST, "w", **profile) as dst,
            ThreadPoolExecutor(1) as reader,
            ThreadPoolExecutor(ncpu) as pool,
            progress,
        ):
            task = progress.add_task("resample", total=total_bytes, strip=0)
            nxt = reader.submit(read, 0)
            for i in range(n_strips):
                data = nxt.result()
                if i + 1 < n_strips:
                    nxt = reader.submit(read, i + 1)
                out = majority_parallel(data, pool, ncpu)
                dst.write(out, 1, window=Window(0, i * STRIP // F, out_w, out.shape[0]))
                rows = min(STRIP, src.height - i * STRIP)
                progress.update(task, advance=rows * src.width, strip=i + 1)
        progress.console.print(f"wrote {DST}")


def cli() -> None:
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    cli()
