"""Resample NYC 2017 6in land cover to coarser grids (FxF majority) directly from the zip.

Parses the ERDAS IMAGINE header (.img) and streams its raw pixel file (.ige)
top to bottom out of the zip, decompressing the single deflate stream on all
cores with rapidgzip. Each strip of full-resolution rows is reduced by taking
the most common non-nodata class in each FxF block, for several resolutions in
one pass, and written as a tiled, deflate-compressed GeoTIFF with tifffile.
The next strip is read in a background thread while the current one is reduced
across all cores.
"""

import argparse
import contextlib
import functools
import io
import math
import os
import posixpath
import queue
import struct
import zipfile
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import rapidgzip
import tifffile
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

ZIP = "Land_Cover.zip"
IMG = "Land_Cover/NYC_2017_LiDAR_LandCover.img"
DST = "out/nyc_landcover_2017_{res}ft.tif"
SRC_RES = 0.5  # ft
BASE_STRIP = 192  # target strip height: 3 source blocks of 64 rows
MAX_MEM = 8 * 2**30  # cap on majority()'s working copy of a strip
NODATA = 0
NCLASS = 9  # values 0..8
MAX_F = 255  # per-class counts are packed into 16 bits at most, so F*F < 2**16
TILE = 512
# uncompressed output bytes above which to write BigTIFF; deflate can't grow
# 3 GiB of pixels past classic TIFF's 4 GiB limit
BIGTIFF_OVER = 3 * 2**30
# The .img names its CRS only by these strings; GDAL resolves them to EPSG:2263.
PRO_NAME = "NAD83 New York State Planes, Long Island, US Foot"
PRO_UNITS = "us_survey_feet"
EPSG = 2263  # NAD83 / New York Long Island (ftUS)
FOOT_US = 9003  # GeoTIFF linear unit code for the US survey foot


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
    return majority_small(a, f) if f * f < 256 else majority_packed(a, f)


def majority_small(a: np.ndarray, f: int) -> np.ndarray:
    """majority() for F <= 15: counts fit uint8, so count one class at a time.

    Each class costs one uint8 compare and F strided uint8 adds per axis, which
    beats majority_packed()'s uint64 reductions over tiny axes by ~10x at F = 2.
    """
    best = np.zeros((a.shape[0] // f, a.shape[1] // f), np.uint8)
    out = np.full(best.shape, NODATA, np.uint8)
    for c in range(1, NCLASS):
        hit = (a == c).view(np.uint8)
        cols = hit[:, ::f].copy()
        for j in range(1, f):
            cols += hit[:, j::f]
        count = cols[::f].copy()
        for i in range(1, f):
            count += cols[i::f]
        more = count > best  # strict, so ties keep the lower class
        out[more] = c
        np.maximum(best, count, out=best)
    return out


def majority_packed(a: np.ndarray, f: int) -> np.ndarray:
    """majority() for any F, summing packed per-class counts (8 bytes per pixel)."""
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


class Fields:
    """Little-endian reader for the fields of one HFA (ERDAS IMAGINE) node."""

    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, fmt: str) -> tuple:
        vals = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return vals

    def string(self) -> str:
        """A 'pc'/'*c' field: count, pointer, then the NUL-terminated chars."""
        n, _ = self.take("II")
        s = self.data[self.pos : self.pos + n]
        self.pos += n
        return s.split(b"\0")[0].decode()

    def pointer(self, fmt: str) -> tuple:
        """A '*o' field holding one object: count, pointer, then the object."""
        self.take("II")
        return self.take(fmt)


def hfa_nodes(b: bytes) -> dict[str, tuple[str, bytes]]:
    """Node tree of an .img header as {"Layer_1/Map_Info": (type, data), ...}."""
    if b[:16] != b"EHFA_HEADER_TAG\0":
        raise SystemExit(f"{IMG} is not an ERDAS IMAGINE file")
    (file_ptr,) = struct.unpack_from("<I", b, 16)
    (root,) = struct.unpack_from("<I", b, file_ptr + 8)
    nodes = {}

    def walk(ptr: int, prefix: str) -> None:
        while ptr:
            nxt, _, _, child, data, size = struct.unpack_from("<5Ii", b, ptr)
            name = b[ptr + 24 : ptr + 88].split(b"\0")[0].decode()
            kind = b[ptr + 88 : ptr + 120].split(b"\0")[0].decode()
            nodes[prefix + name] = (kind, b[data : data + size])
            walk(child, prefix + name + "/")
            ptr = nxt

    (root_child,) = struct.unpack_from("<I", b, root + 12)
    walk(root_child, "")
    return nodes


@dataclass
class Source:
    width: int
    height: int
    block: int  # square blocks of block x block uint8 pixels
    ige: str  # path of the pixel file inside the zip
    flags_offset: int  # block validity bitmap in the .ige
    data_offset: int  # first block in the .ige
    x0: float  # west edge
    y0: float  # north edge
    res: float


def read_source(zf: zipfile.ZipFile) -> Source:
    """Raster layout and georeferencing from the single-band .img header."""
    nodes = hfa_nodes(zf.read(IMG))
    layers = [k for k, (kind, _) in nodes.items() if kind == "Eimg_Layer"]
    if len(layers) != 1:
        raise SystemExit(f"expected one layer in {IMG}, found {layers}")
    layer = layers[0]

    def node(name: str, kind: str) -> Fields:
        found, data = nodes[f"{layer}/{name}"]
        if found != kind:
            raise SystemExit(f"{layer}/{name} is {found}, expected {kind}")
        return Fields(data)

    width, height, _, pixel_type, bw, bh = Fields(nodes[layer][1]).take("iiHHii")
    if pixel_type != 3 or bw != bh:  # u8 in the Eimg_Layer pixelType enum
        raise SystemExit(
            f"expected uint8 square blocks, got type {pixel_type}, {bw}x{bh}"
        )

    ext = node("ExternalRasterDMS", "ImgExternalRaster")
    ige = ext.string()
    flags_lo, flags_hi, data_lo, data_hi, count, index = ext.take("6I")
    if (count, index) != (1, 0):
        raise SystemExit(f"expected a single-layer .ige, got layer {index} of {count}")

    info = node("Map_Info", "Eprj_MapInfo")
    pro_name = info.string()
    ulx, uly = info.pointer("dd")
    info.pointer("dd")  # lower-right center
    res_x, res_y = info.pointer("dd")
    units = info.string()
    if (pro_name, units, res_x, res_y) != (PRO_NAME, PRO_UNITS, SRC_RES, SRC_RES):
        raise SystemExit(
            f"unexpected georeferencing: {pro_name}, {units}, {res_x}x{res_y}"
        )

    return Source(
        width=width,
        height=height,
        block=bw,
        ige=posixpath.join(posixpath.dirname(IMG), ige),
        flags_offset=flags_hi << 32 | flags_lo,
        data_offset=data_hi << 32 | data_lo,
        # Map_Info stores the center of the upper-left pixel
        x0=ulx - res_x / 2,
        y0=uly + res_y / 2,
        res=res_x,
    )


class ZipMember(io.RawIOBase):
    """Seekable view of a zip member's compressed bytes (a raw deflate stream)."""

    def __init__(self, path: str, info: zipfile.ZipInfo) -> None:
        if info.compress_type != zipfile.ZIP_DEFLATED:
            raise SystemExit(f"{info.filename} is not deflate-compressed")
        self.fd = os.open(path, os.O_RDONLY)
        header = os.pread(self.fd, 30, info.header_offset)
        if header[:4] != b"PK\x03\x04":
            raise SystemExit(f"bad local header for {info.filename}")
        name_len, extra_len = struct.unpack_from("<HH", header, 26)
        self.start = info.header_offset + 30 + name_len + extra_len
        self.size, self.pos = info.compress_size, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = (0, self.pos, self.size)[whence] + offset
        return self.pos

    def readinto(self, buf) -> int:
        n = max(0, min(len(buf), self.size - self.pos))
        if n:
            n = os.preadv(self.fd, [memoryview(buf)[:n]], self.start + self.pos)
        self.pos += n
        return n

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
        super().close()


class Pixels:
    """Rows of the .ige pixel file, streamed top to bottom from the zip.

    The .ige is a 26-byte magic string, a small header, a validity bitmap with one
    bit per block (rows padded to whole bytes), then every block, row by row, as
    raw uint8. Invalid blocks read as NODATA, as GDAL does.
    """

    def __init__(self, path: str, info: zipfile.ZipInfo, src: Source) -> None:
        self.src = src
        self.nbx = -(-src.width // src.block)
        nby = -(-src.height // src.block)
        self.stream = rapidgzip.RapidgzipFile(ZipMember(path, info))
        try:
            self.valid = self._read_block_map(nby)
        except BaseException:
            self.stream.close()  # rapidgzip aborts the process if left open at exit
            raise

        self.block_row = bytearray(self.nbx * src.block * src.block)
        self.rows = np.empty(
            (0, self.nbx * src.block), np.uint8
        )  # decoded, not yet returned
        self.next_block_row = 0

    def _read_block_map(self, nby: int) -> np.ndarray:
        """Check the header and return which blocks are valid, as (rows, cols) bools."""
        src = self.src
        head = self._read(src.data_offset)
        if head[:26] != b"ERDAS_IMG_EXTERNAL_RASTER\0":
            raise SystemExit(f"{src.ige} is not an ERDAS external raster")
        layers, w, h, bw, bh = struct.unpack_from("<5i", head, 27)
        if (layers, w, h, bw, bh) != (1, src.width, src.height, src.block, src.block):
            raise SystemExit(f"{src.ige} header {w}x{h} does not match {IMG}")
        *_, rows, cols, _ = struct.unpack_from("<5i", head, src.flags_offset)
        if (rows, cols) != (nby, self.nbx):
            raise SystemExit(
                f"{src.ige} has a {cols}x{rows} block map, expected {self.nbx}x{nby}"
            )
        row_bytes = -(-self.nbx // 8)
        bitmap = np.frombuffer(
            head, np.uint8, nby * row_bytes, src.flags_offset + 20
        ).reshape(nby, row_bytes)
        return np.unpackbits(bitmap, axis=1, bitorder="little")[:, : self.nbx] == 1

    def _read(self, n: int, into: bytearray | None = None) -> bytearray:
        buf = into if into is not None else bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            k = self.stream.readinto(view[got:n])
            if not k:
                raise SystemExit(f"{self.src.ige} ended early")
            got += k
        return buf

    def read_into(self, out: np.ndarray) -> None:
        """Fill out[:, :width] with the next out.shape[0] rows."""
        b, done = self.src.block, 0
        while done < len(out):
            if not len(self.rows):
                blocks = np.frombuffer(
                    self._read(len(self.block_row), self.block_row), np.uint8
                ).reshape(self.nbx, b, b)
                blocks[~self.valid[self.next_block_row]] = NODATA
                self.rows = blocks.transpose(1, 0, 2).reshape(b, self.nbx * b)
                self.next_block_row += 1
            k = min(len(out) - done, len(self.rows))
            out[done : done + k, : self.src.width] = self.rows[:k, : self.src.width]
            self.rows = self.rows[k:]
            done += k

    def close(self) -> None:
        self.stream.close()


def geotiff_tags(src: Source, f: int) -> list[tuple]:
    """tifffile extratags placing an f-times coarser grid at the source's origin."""
    citation = PRO_NAME + "|"
    keys = [1, 1, 0, 5]  # version 1.1.0, 5 keys, sorted by id:
    keys += [1024, 0, 1, 1]  # GTModelType: projected
    keys += [1025, 0, 1, 1]  # GTRasterType: pixel is area
    keys += [1026, 34737, len(citation), 0]  # GTCitation, in GeoAsciiParams
    keys += [3072, 0, 1, EPSG]  # ProjectedCSType
    keys += [3076, 0, 1, FOOT_US]  # ProjLinearUnits
    res = src.res * f
    return [
        (33550, "d", 3, (res, res, 0.0), True),  # ModelPixelScale
        (33922, "d", 6, (0.0, 0.0, 0.0, src.x0, src.y0, 0.0), True),  # ModelTiepoint
        (34735, "H", len(keys), keys, True),  # GeoKeyDirectory
        (34737, "s", 0, citation, True),  # GeoAsciiParams
        (42113, "s", 0, str(NODATA), True),  # GDAL_NODATA
    ]


class TiledWriter:
    """Takes output rows top to bottom and writes a tiled, deflate GeoTIFF.

    Rows collect into one row of tiles; full tiles go through a bounded queue to a
    thread running tifffile, which compresses them on its own worker threads.
    """

    def __init__(self, path: str, width: int, height: int, tags: list[tuple]) -> None:
        self.width, self.height = width, height
        self.tiles_x = -(-width // TILE)
        self.buf = np.empty((TILE, width), np.uint8)
        self.top = self.filled = 0  # first row of the current tile row; rows in buf
        self.queue: queue.Queue = queue.Queue(maxsize=2 * self.tiles_x)
        self.thread = ThreadPoolExecutor(1)
        self.done: Future = self.thread.submit(
            self._write, path, width * height > BIGTIFF_OVER, tags
        )

    def _write(self, path: str, bigtiff: bool, tags: list[tuple]) -> None:
        with tifffile.TiffWriter(path, bigtiff=bigtiff) as tif:
            tif.write(
                self._tiles(),
                shape=(self.height, self.width),
                dtype=np.uint8,
                tile=(TILE, TILE),
                photometric="minisblack",
                compression="zlib",
                compressionargs={"level": 6},
                metadata=None,
                extratags=tags,
            )

    def _tiles(self) -> Iterator[np.ndarray | None]:
        while (tile := self.queue.get()) is not StopIteration:
            yield tile

    def _put(self, item) -> None:
        while True:
            try:
                return self.queue.put(item, timeout=1)
            except queue.Full:
                if self.done.done():
                    self.done.result()  # raises the writer's error
                    raise RuntimeError("GeoTIFF writer stopped early") from None

    def _flush(self) -> None:
        """Send the current tile row; edge tiles are short, tifffile pads them."""
        rows = self.buf[: self.filled]
        for x in range(0, self.width, TILE):
            self._put(np.ascontiguousarray(rows[:, x : x + TILE]))
        self.top += self.filled
        self.filled = 0

    def write(self, rows: np.ndarray) -> None:
        while len(rows):
            k = min(len(rows), TILE - self.filled, self.height - self.top - self.filled)
            self.buf[self.filled : self.filled + k] = rows[:k]
            self.filled += k
            rows = rows[k:]
            if self.filled == TILE or self.top + self.filled == self.height:
                self._flush()

    def close(self) -> None:
        """Finish the file; rows never written (e.g. after --strips) stay NODATA."""
        if self.filled:
            self._flush()
            self.top = -(-self.top // TILE) * TILE  # tifffile padded that row of tiles
        while self.top < self.height:  # empty tiles: GDAL reads them as NODATA
            for _ in range(self.tiles_x):
                self._put(None)
            self.top += TILE
        self._put(StopIteration)
        self.done.result()
        self.thread.shutdown()

    def abort(self) -> None:
        # tifffile then fails on the missing tiles; if the writer already failed,
        # the caller's error matters more than the writer's
        with contextlib.suppress(Exception):
            self._put(StopIteration)
        self.done.exception()
        self.thread.shutdown()


def main(factors: list[int], max_strips: int | None = None) -> None:
    factors = sorted(set(factors))
    lcm = math.lcm(*factors)
    # smallest multiple of every factor >= BASE_STRIP: never splits an output pixel
    strip = -(-BASE_STRIP // lcm) * lcm
    dsts = {f: DST.format(res=f"{f * SRC_RES:g}") for f in factors}
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with zipfile.ZipFile(ZIP) as zf:
        src = read_source(zf)
        ige = zf.getinfo(src.ige)
    out_w = {f: -(-src.width // f) for f in factors}
    out_h = {f: -(-src.height // f) for f in factors}

    n_strips = -(-src.height // strip)
    if max_strips:
        n_strips = min(n_strips, max_strips)
    # pad ragged right/bottom edges to a multiple of every factor
    pad_w = -(-src.width // lcm) * lcm
    # majority() works on a copy of each chunk, all threads at once: uint64 one-hot
    # (8 bytes/pixel) for F > 15, a uint8 class mask plus column sums otherwise
    per_pixel = 8 if max(factors) ** 2 >= 256 else 2
    if per_pixel * strip * pad_w > MAX_MEM:
        raise SystemExit(
            f"strips of {strip} rows (lcm of the block sizes) need "
            f"~{per_pixel * strip * pad_w / 2**30:.0f} GiB; run these resolutions separately"
        )

    ncpu = os.cpu_count() or 1
    # uint8 source: 1 byte per pixel, so progress is in uncompressed source bytes
    total_bytes = min(n_strips * strip, src.height) * src.width

    def read(i: int) -> np.ndarray:
        rows = min(strip, src.height - i * strip)
        pad = np.full((-(-rows // lcm) * lcm, pad_w), NODATA, np.uint8)
        pixels.read_into(pad[:rows])
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

    pixels = Pixels(ZIP, ige, src)
    dst: dict[int, TiledWriter] = {}
    try:
        for f in factors:
            dst[f] = TiledWriter(dsts[f], out_w[f], out_h[f], geotiff_tags(src, f))
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
                    dst[f].write(majority_parallel(a, f, pool, ncpu))
                progress.update(task, advance=rows * src.width, strip=i + 1)
        for d in dst.values():
            d.close()
    except BaseException:
        for d in dst.values():
            d.abort()
        raise
    finally:
        pixels.close()
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
