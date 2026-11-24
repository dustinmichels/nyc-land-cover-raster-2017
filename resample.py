"""Resample NYC 2017 6in land cover to coarser grids (FxF majority) directly from the zip.

Reads full-resolution strips sequentially (the zip is one deflate stream, so
sequential access is fast and random access is slow) and takes the most common
non-nodata class in each FxF block. Several resolutions are produced in one
pass over the zip. The next strip is read in a background thread while the
current one is reduced across all cores.
"""

import argparse
import functools
import math
import os
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
DST = "out/nyc_landcover_2017_{res}ft.tif"
SRC_RES = 0.5  # ft
BASE_STRIP = 192  # target strip height: 3 source blocks of 64 rows
MAX_MEM = 8 * 2**30  # cap on the uint64 one-hot copy of a strip (8 bytes/pixel)
NODATA = 0
NCLASS = 9  # values 0..8
MAX_F = 255  # per-class counts are packed into 16 bits at most, so F*F < 2**16


@functools.cache
def tables(f: int) -> list[tuple[np.ndarray, np.ndarray, np.uint64]]:
    """Packing tables for an FxF block: each class count gets bits >= log2(F*F + 1).

    Classes are split into groups that fit one uint64 (all 8 for F <= 15). Within a
    group, class c -> 1 << bits*k for its slot k; NODATA, other groups' classes and
    anything else -> 0. Summing these over a block yields every class count in the
    group packed into one uint64 in a single pass.
    """
    bits = (f * f).bit_length()
    per_word = 64 // bits
    groups = []
    for lo in range(1, NCLASS, per_word):
        hi = min(lo + per_word, NCLASS)
        shifts = bits * np.arange(hi - lo, dtype=np.uint64)
        onehot = np.zeros(256, np.uint64)
        onehot[lo:hi] = np.uint64(1) << shifts
        groups.append((onehot, shifts[:, None, None], np.uint64(2**bits - 1)))
    return groups


def majority(a: np.ndarray, f: int) -> np.ndarray:
    """Majority of each fxf block, ignoring NODATA; ties go to the lower class."""
    h, w = a.shape
    counts = []
    for onehot, shifts, mask in tables(f):
        packed = onehot[a].reshape(h, w // f, f).sum(axis=2)
        packed = packed.reshape(h // f, f, w // f).sum(axis=1)
        counts.append(((packed >> shifts) & mask).astype(np.uint16))
    counts = np.concatenate(counts)
    out = counts.argmax(axis=0).astype(np.uint8) + 1
    out[counts.max(axis=0) == 0] = NODATA
    return out


def majority_parallel(
    a: np.ndarray, f: int, pool: ThreadPoolExecutor, n: int
) -> np.ndarray:
    """majority() over n column chunks (multiples of f) in parallel; numpy drops the GIL."""
    edges = np.linspace(0, a.shape[1] // f, n + 1).astype(int) * f
    parts = pool.map(lambda lr: majority(a[:, lr[0] : lr[1]], f), zip(edges, edges[1:]))
    return np.hstack(list(parts))


def factor(res: str) -> int:
    """Output resolution in ft -> integer block size over the 0.5 ft source."""
    try:
        f = float(res) / SRC_RES
    except ValueError:
        f = None
    if f is None or not f.is_integer() or not 2 <= f <= MAX_F:
        raise argparse.ArgumentTypeError(
            f"{res!r}: resolution in ft, a multiple of {SRC_RES:g} between "
            f"{2 * SRC_RES:g} and {MAX_F * SRC_RES:g} (e.g. 1, 3, 6, 7.5)"
        )
    return int(f)


def main(factors: list[int], max_strips: int | None = None) -> None:
    factors = sorted(set(factors))
    lcm = math.lcm(*factors)
    # smallest multiple of every factor >= BASE_STRIP: never splits an output pixel.
    # Strips may cut a 64-row source block; GDAL's block cache serves the remainder.
    strip = -(-BASE_STRIP // lcm) * lcm
    dsts = {f: DST.format(res=f"{f * SRC_RES:g}") for f in factors}
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with rasterio.open(SRC) as src:
        out_w = {f: -(-src.width // f) for f in factors}
        profiles = {
            f: src.profile
            | {
                "driver": "GTiff",
                "width": out_w[f],
                "height": -(-src.height // f),
                "transform": src.transform * src.transform.scale(f, f),
                "tiled": True,
                "blockxsize": 512,
                "blockysize": 512,
                "compress": "deflate",
                "BIGTIFF": "IF_SAFER",
            }
            for f in factors
        }
        n_strips = -(-src.height // strip)
        if max_strips:
            n_strips = min(n_strips, max_strips)
        # pad ragged right/bottom edges to a multiple of every factor
        pad_w = -(-src.width // lcm) * lcm
        # majority() makes a uint64 one-hot copy of each chunk, all threads at once
        if 8 * strip * pad_w > MAX_MEM:
            raise SystemExit(
                f"strips of {strip} rows (lcm of the block sizes) need "
                f"~{8 * strip * pad_w / 2**30:.0f} GiB; run these resolutions separately"
            )

        ncpu = os.cpu_count() or 1
        # uint8 source: 1 byte per pixel, so progress is in uncompressed source bytes
        total_bytes = min(n_strips * strip, src.height) * src.width

        def read(i: int) -> np.ndarray:
            row = i * strip
            rows = min(strip, src.height - row)
            pad = np.full((-(-rows // lcm) * lcm, pad_w), NODATA, np.uint8)
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

        dst = {f: rasterio.open(dsts[f], "w", **profiles[f]) for f in factors}
        try:
            with (
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
                    rows = min(strip, src.height - i * strip)
                    for f in factors:
                        # padding is to a multiple of lcm; keep only rows this f needs
                        a = data[: -(-rows // f) * f, : out_w[f] * f]
                        out = majority_parallel(a, f, pool, ncpu)
                        dst[f].write(
                            out,
                            1,
                            window=Window(0, i * strip // f, out_w[f], out.shape[0]),
                        )
                    progress.update(task, advance=rows * src.width, strip=i + 1)
        finally:
            for d in dst.values():
                d.close()
        for f in factors:
            progress.console.print(f"wrote {dsts[f]}")


def cli() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "res",
        nargs="*",
        type=factor,
        default=[factor("3")],
        metavar="FT",
        help=f"output resolution(s) in ft: multiples of {SRC_RES:g} from "
        f"{2 * SRC_RES:g} to {MAX_F * SRC_RES:g}, e.g. 1 3 6 (default: 3)",
    )
    p.add_argument(
        "--strips", type=int, help="only process the first N strips, for testing"
    )
    args = p.parse_args()
    main(args.res, args.strips)


if __name__ == "__main__":
    cli()
