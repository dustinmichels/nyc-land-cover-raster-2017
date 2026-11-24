Data Source:

Land Cover Raster Data (2017) – 6in Resolution
https://data.cityofnewyork.us/Environment/Land-Cover-Raster-Data-2017-6in-Resolution/he6d-2qns/about_data

## Source vs. output

|                | Source (`Land_Cover.zip`)                          | Output (`nyc_landcover_2017_3ft.tif`) |
| -------------- | -------------------------------------------------- | ------------------------------------- |
| Format         | Erdas Imagine (`.img` + `.ige` + `.rrd` sidecars)  | GeoTIFF                               |
| Resolution     | 0.5 ft (6 in)                                      | 3 ft                                  |
| Size (pixels)  | 310,844 × 314,414 (~98 billion)                    | 51,808 × 52,403 (~2.7 billion)        |
| Size on disk   | 1.27 GB zipped, ~130 GB unzipped                   | ~79 MB                                |
| Layout         | 64 × 64 blocks, uncompressed (the zip deflates the whole file) | 512 × 512 tiles, deflate-compressed per tile |
| Overviews      | 11 levels in `.rrd` (nearest-neighbor, 1 ft–~1024 ft) | none                               |
| CRS            | EPSG:2263 (NY State Plane Long Island, US ft)      | same                                  |
| Data type      | `uint8`, nodata = 0                                | same                                  |

The source `.img` file is only a 9.6 KB header; the pixels live in
`NYC_2017_LiDAR_LandCover.ige` (~98 GB) and the prebuilt pyramids in
`NYC_2017_LiDAR_LandCover.rrd` (~2 GB). The output is a single
self-contained file that opens quickly in QGIS, rasterio, etc.

Class values are the same in both:

| Value | Class              |
| ----- | ------------------ |
| 0     | nodata             |
| 1     | Tree Canopy        |
| 2     | Grass/Shrubs       |
| 3     | Bare Soil          |
| 4     | Water              |
| 5     | Buildings          |
| 6     | Roads              |
| 7     | Other Impervious   |
| 8     | Railroads          |

## How `resample.py` works

```sh
uv run resample.py        # full run, writes out/nyc_landcover_2017_3ft.tif
uv run resample.py 10     # only the first 10 strips, for testing
```

1. **Reads straight from the zip.** GDAL's `/vsizip/` path opens the `.img`
   inside `Land_Cover.zip`, so the ~130 GB archive never has to be extracted.
2. **Reads sequentially in strips.** The `.ige` is stored as one long deflate
   stream inside the zip, so seeking backwards is expensive but reading
   top-to-bottom is cheap. The script reads full-width strips of 192 rows —
   a multiple of both the source block height (64) and the resampling
   factor (6), so no strip ever splits a source block or an output pixel.
3. **Majority resampling (6 × 6 → 1).** Each 6 × 6 block of 6-inch pixels
   becomes one 3 ft pixel holding the most common class in that block.
   Nodata pixels are ignored in the vote; a block that is entirely nodata
   stays nodata, and ties go to the lower class number. Ragged right/bottom
   edges are padded with nodata to a multiple of 6.
4. **Writes a tiled GeoTIFF.** The output copies the source CRS and dtype,
   scales the geotransform by 6, and writes 512 × 512 deflate-compressed
   tiles (BigTIFF only if needed).

Majority is used instead of the source's prebuilt pyramids because those are
nearest-neighbor (each coarse pixel is one arbitrary sample), have no 3 ft
level (only 2 ft and 4 ft), and give noisier class-area estimates. Majority
keeps the dominant land cover in each 3 ft cell.

## Releasing

```sh
mise run release           # tag defaults to vYYYY.MM.DD
mise run release v1.0.0    # explicit tag
```

Uploads `out/nyc_landcover_2017_3ft.tif` to a GitHub release via `gh`. If the
tag already has a release, the asset is replaced.
