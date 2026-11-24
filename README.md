# NYC Land Cover Raster 2017

This repo contains a script for resampling [Land Cover Raster Data (2017) – 6in Resolution](https://data.cityofnewyork.us/Environment/Land-Cover-Raster-Data-2017-6in-Resolution/he6d-2qns/about_data). It also has the output of resampling in [releases](https://github.com/dustinmichels/nyc-land-cover-raster-2017/releases).

> **Rationale:** The source is so large it is challenging to work with. It requires ~100GB to unzip. Here are high-quality, ready-made re-samples that are much smaller.

## Source vs. output

|               | Source (`Land_Cover.zip`)                                      | Output (`nyc_landcover_2017_3ft.tif`)        |
| ------------- | -------------------------------------------------------------- | -------------------------------------------- |
| Format        | Erdas Imagine (`.img` + `.ige` + `.rrd` sidecars)              | GeoTIFF                                      |
| Resolution    | 0.5 ft (6 in)                                                  | 3 ft                                         |
| Size (pixels) | 310,844 × 314,414 (~98 billion)                                | 51,808 × 52,403 (~2.7 billion)               |
| Size on disk  | 1.27 GB zipped, ~130 GB unzipped                               | ~79 MB                                       |
| Layout        | 64 × 64 blocks, uncompressed (the zip deflates the whole file) | 512 × 512 tiles, deflate-compressed per tile |
| Overviews     | 11 levels in `.rrd` (nearest-neighbor, 1 ft–~1024 ft)          | none                                         |
| CRS           | EPSG:2263 (NY State Plane Long Island, US ft)                  | same                                         |
| Data type     | `uint8`, nodata = 0                                            | same                                         |

The source `.img` file is only a 9.6 KB header; the pixels live in `NYC_2017_LiDAR_LandCover.ige` (~98 GB) and the prebuilt pyramids in `NYC_2017_LiDAR_LandCover.rrd` (~2 GB). The output is a single self-contained file that opens quickly in QGIS, rasterio, etc.

Class values are the same in both:

| Value | Class            |
| ----- | ---------------- |
| 0     | nodata           |
| 1     | Tree Canopy      |
| 2     | Grass/Shrubs     |
| 3     | Bare Soil        |
| 4     | Water            |
| 5     | Buildings        |
| 6     | Roads            |
| 7     | Other Impervious |
| 8     | Railroads        |

## How it works

Resolutions are in feet: any multiple of 0.5 from 1 to 127.5. Each output is `out/nyc_landcover_2017_<res>ft.tif`, with block size F = res / 0.5 (2 for 1 ft, 6 for 3 ft, 12 for 6 ft).

1. **Reads straight from the zip, without GDAL.** The script parses the 9.6 KB `.img` header itself for the raster size, block size, georeferencing and the location of the pixel data, then streams the `.ige` member's compressed bytes out of `Land_Cover.zip` through [rapidgzip](https://github.com/mxmlnkn/rapidgzip), which decompresses a single deflate stream on all cores. The ~130 GB archive is never extracted. The `.ige` is a small header, a bitmap of valid blocks, then every 64 × 64 block in row-major order as raw `uint8`; blocks marked invalid read as nodata, as they do in GDAL.
2. **Reads sequentially in strips.** The `.ige` is stored as one long deflate stream inside the zip, so seeking backwards is expensive but reading top-to-bottom is cheap. The script reads full-width strips whose height is the smallest multiple of lcm(every F) that is at least 192 rows (192 for 1, 3 and 6 ft; 255 for 127.5 ft), so no strip ever splits an output pixel. Strips don't have to line up with the 64-row source blocks: rows left over from a decoded row of blocks carry into the next strip. All requested resolutions are reduced from the same strip, so the zip is decompressed only once. The vote works on a copy of each strip, about 2 bytes per pixel up to 7.5 ft and 8 bytes per pixel above that, so the script refuses to run if that copy would exceed 8 GiB. That only happens when the factors' lcm is large (e.g. 50 and 50.5 ft together); run those separately.
3. **Prefetches the next strip.** A background thread reads strip _i + 1_ while strip _i_ is being reduced, so decompression and compute overlap.
4. **Majority resampling (F × F → 1), across all cores.** Each F × F block of 6-inch pixels becomes one output pixel holding the most common class in that block. Nodata pixels are ignored in the vote; a block that is entirely nodata stays nodata, and ties go to the lower class number. Ragged right/bottom edges are padded with nodata to a multiple of F. Each strip is split into one column chunk per CPU and reduced in parallel threads. Up to 7.5 ft (F ≤ 15, so every count fits in a byte), each class is counted in turn with a `uint8` comparison and F strided additions along each axis. Above 7.5 ft the 8 class counts are packed into `uint64`s (just enough bits per count for F²), so one vectorized sum counts several classes at once.
5. **Writes a tiled GeoTIFF per resolution.** Output rows are collected one row of 512 × 512 tiles at a time and handed to [tifffile](https://github.com/cgohlke/tifffile), which deflate-compresses the tiles on worker threads. Each output is `uint8` with nodata = 0 and EPSG:2263, with the geotransform scaled by F. BigTIFF is used only when the uncompressed image would exceed 3 GiB. Rows a `--strips` run never reaches are written as empty tiles, which read as nodata.

Majority is used instead of the source's prebuilt pyramids because those are nearest-neighbor (each coarse pixel is one arbitrary sample, so single cells are noisy) and exist only at power-of-two sizes (1, 2, 4 ft, …). Majority keeps the dominant land cover in each cell.

### Caveats

- **Any valid pixel outvotes nodata.** An F × F block with a single classified pixel and the rest nodata takes that pixel's class, so shorelines and the city boundary grow slightly outward (more so at coarser resolutions).
- **Ties favor the lower class number.** Tree Canopy (1) wins every tie it is part of, so it is slightly over-represented compared with a random or center-pixel tie-break.
- **Majority does not preserve area.** Thin or small features (Railroads, Roads, narrow strips of grass) lose share, increasingly so as F grows. Compute class areas from the 6 in source, not these outputs.
- **The extent is padded.** Output sizes are rounded up, so an output can reach up to `res − 0.5` ft farther east and south than the source; the extra area is nodata. At 1 ft there is no padding; at 3 ft it is 2 ft each way; at 6 ft it is 2 ft east and 5 ft south.
- **Values outside 1–8 count as nodata.** The source only contains 0–8, but the script would not warn if that changed.

Verification:

- `majority()` / `majority_parallel()` match a naive per-block `bincount` reference on random arrays (ties, nodata, out-of-range values, uneven column chunking) for F = 2, 3, 6, 12, 15, 16, 31, 64 and 255, covering both single- and multi-`uint64` packing; the `uint8` path matches the same reference for every F from 2 to 15.
- The earlier GDAL-based driver, run on a synthetic 1237 × 2011 raster (ragged on every edge, multiple strips), matched the reference exactly for 1+3+6 ft in one pass, 2.5+20 ft (non-192 strip height) and 8 ft.
- The GDAL-free reader and writer produce pixel-identical rasters to that GDAL-based version, with the same transform, CRS and nodata: over the full extent at 2, 3, 8 and 16 ft, and at 1 ft over the first 300 strips. A full 1 ft run also matches a `bincount` vote over source windows read through GDAL: 55 windows of 256 × 256 source pixels at rows 60,000 to the last row, including the right edge.

## Usage

```sh
# install dependencies
uv sync

# run script
uv run resample.py               # full run, writes out/nyc_landcover_2017_3ft.tif

# alternative sizes
uv run resample.py 2 4 8         # one pass over the zip, writes the 2, 4 and 8 ft files
uv run resample.py 6 --strips 10 # only the first 10 strips, for testing

# clear outputs
mise run clear                   # delete everything in out/
```

## Releasing

```sh
mise run build            # asks to clear out/ (Enter = go, n = cancel), then full 2, 3, 4, 8, 16 ft run
mise run release          # tag next vMAJOR.MINOR: v1.1, v1.2, …
mise run release v2.0     # explicit tag (e.g. a major bump)
```

Rasters are never committed (`out/` is gitignored); they ship only as GitHub release assets (up to 2 GB each). Without an explicit tag, the task fetches tags from `origin`, finds the highest `vMAJOR.MINOR` tag and bumps the minor number (`v1.1` if there is none; `v2.0` → `v2.1`). It sets the version in both citations below (Chicago and BibTeX) to the tag, commits that change, creates an annotated tag on the new `HEAD`, pushes the branch and tag, then runs `gh release create` with every `out/nyc_landcover_2017_*ft.tif`. If the README can't be updated, it prints a note and releases anyway (tag only). It refuses to run if the working tree is dirty, an explicit tag already exists, `out/` has no rasters, or `gh` is missing or logged out.

It uploads whatever is in `out/`, so clear out partial `--strips` test outputs first.

## Citation

If you use this output, please cite it and the source dataset.

Chicago (bibliography):

> Michels, Dustin. _NYC Land Cover Raster 2017: Majority-Resampled GeoTIFFs of the 6 in NYC Land Cover Dataset_. Version v1.1. GitHub, 2026. https://github.com/dustinmichels/nyc-land-cover-raster-2017.
>
> NYC Office of Technology and Innovation (OTI). _Land Cover Raster Data (2017) – 6in Resolution_. Dataset he6d-2qns. NYC Open Data, 2018. https://data.cityofnewyork.us/Environment/Land-Cover-Raster-Data-2017-6in-Resolution/he6d-2qns.

BibTeX:

```bibtex
@misc{michels2026nyclandcover,
  author       = {Michels, Dustin},
  title        = {{NYC Land Cover Raster 2017}: Majority-Resampled {GeoTIFF}s of the 6 in {NYC} Land Cover Dataset},
  year         = {2026},
  version      = {v1.1},
  howpublished = {\url{https://github.com/dustinmichels/nyc-land-cover-raster-2017}},
  note         = {Derived from NYC Office of Technology and Innovation, Land Cover Raster Data (2017) -- 6in Resolution}
}

@misc{nycoti2018landcover,
  author       = {{NYC Office of Technology and Innovation (OTI)}},
  title        = {Land Cover Raster Data (2017) -- 6in Resolution},
  year         = {2018},
  publisher    = {NYC Open Data},
  howpublished = {\url{https://data.cityofnewyork.us/Environment/Land-Cover-Raster-Data-2017-6in-Resolution/he6d-2qns}},
  note         = {Dataset he6d-2qns, made public 2018-12-12}
}
```
