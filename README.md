# Harvest Accessibility — QGIS Plugin

A QGIS Processing plugin for forestry operations that computes harvest accessibility
using a two-stage distance model: straight-line skidding distance to the nearest forest
road (d1) and shortest network path along the road to the nearest landing point (d2).

## What It Does

Given an operation area polygon, a forest road network, and one or more landing points,
the plugin places a regular grid of sample points across the area and computes:

- **d1** — straight-line (skidding) distance from each sample point to the nearest forest road
- **d2** — shortest network path along the road from the road snap point to the nearest landing

Summary statistics (mean d1, mean d2) are reported in an HTML result report.

Two optional inputs change what is measured:

- Supply **individual tree points** (e.g. from ALS) and they are used as the sample points
  instead of the grid. Extraction distance is a per-tree quantity, so real stem positions beat
  a regular lattice, and stand density is reflected instead of being averaged away.
- Supply a **DEM** and d1 switches from the standing tree to the felled stem: the tree is felled
  in a permitted direction and the near end (top or butt) is winched, so d1 becomes the distance
  from the road to that end. See [Felling model](#felling-model).

## Requirements

- QGIS 3.22 or later
- Input layers must use a **projected CRS in metres** (e.g. EPSG:6676 for Japan)

## Installation

1. Download the latest release ZIP from the [Releases](../../releases) page
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**
3. Select the downloaded ZIP and click **Install Plugin**
4. The algorithm appears in **Processing Toolbox → Harvest Accessibility**

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| Operation area polygon | Vector polygon | — | Harvest block boundary |
| Forest road lines (also network) | Vector line | — | Road network used for both d1 snapping and d2 routing |
| Landing points | Vector point | — | One or more log landing locations |
| Grid spacing (m) | Float | 4.0 | Spacing of the sample grid in metres |
| Network snapping tolerance (m) | Float | 5.0 | Tolerance for snapping start points onto the road network |
| DEM | Raster | — | Optional. Supplying it enables the felling model |
| Individual tree points | Vector point | — | Optional. Used as sample points instead of the grid |
| Tree height field | Field | — | Optional. Per-tree height; falls back to the numeric value below |
| Barriers | Vector line/polygon | — | Optional. Rivers etc. A stem cannot be felled across one |
| Tree height (m) | Float | 20.0 | Used when no height field is given |

Advanced parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| Felling sector half-angle (deg) | Float | 105.0 | Directions allowed, measured from straight downslope |
| Slope allowing any direction (deg) | Float | 10.0 | At or below this slope the tree can be felled any way |
| Direct grapple reach (m) | Float | 3.0 | Within this distance of the road nothing is winched |
| Slope/aspect smoothing window (m) | Float | 5.0 | Cell size the DEM is resampled to before slope and aspect |
| Candidate roads per sample point | Integer | 10 | How many nearby roads are considered |
| Split roads at intersections | Boolean | True | Split road lines at intersections before routing for better connectivity |

## Outputs

| Output | Description |
|--------|-------------|
| Result report (HTML) | Mean d1 and d2, sample point count, unreachable point count |

Enable **Debug mode** (Advanced parameters) to also load intermediate layers into the project:
`debug_p1_grid`, `debug_p2_road_snap`, `debug_routes`, `debug_summary`

## Felling model

With a DEM supplied, d1 stops being a purely geometric quantity and becomes a model of the
work. The tree is felled, and the end nearest the road is what gets pulled.

- Felling is allowed within a sector centred on straight downslope (default ±105°). On gentle
  ground (default ≤10°) any direction is allowed.
- The stem's **horizontal** reach is `L·cos(slope in the felling direction)`, so felling across
  the contour reaches further horizontally than felling straight down a steep face.
- Blocking the road is accepted: if the stem reaches the road, d1 is 0 and the haul starts at
  the crossing point.
- A direction whose stem would cross a **barrier** is not available — a stem thrown over a
  river cannot be pulled back across it.
- Within grapple reach of the road, d1 is 0.

The report states the assumptions used (height, sector, grapple reach, smoothing) alongside
both the geometric and the modelled mean d1, because the model's numbers only mean something
next to the assumptions that produced them.

Direction choice clamps the bearing to the road into the allowed sector. That is exact for a
straight road and an approximation for a curved or branching network.

## Fetching a DEM

**Processing Toolbox → Harvest Accessibility → Fetch DEM from elevation tiles** builds a DEM
covering an operation area from published elevation tiles.

| Source | Resolution | Coverage |
|--------|-----------|----------|
| VIRTUAL SHIZUOKA | ~0.5 m | Shizuoka Prefecture |
| GSI DEM5A | ~4 m | Where surveyed |
| GSI DEM10B | ~8 m | Nationwide |

Run it in the office, save the GeoTIFF, and feed the file to the main algorithm. It is a
separate algorithm on purpose: the main algorithm is used in the forest, where there may be no
network connection.

The DEM is reprojected out of web mercator before it is written. This is not cosmetic — slope
computed on mercator pixels comes out roughly 20% too gentle at Japanese latitudes, and the
felling model branches on a slope threshold.

Tiles are served by 産業技術総合研究所 シームレス標高タイル, carrying 静岡県 VIRTUAL SHIZUOKA
(CC BY 4.0) and 国土地理院 基盤地図情報数値標高モデル. Credit the source when publishing
results.

## Sample Data

The `data/sample/` directory contains sample files in EPSG:6676 (JGD2011 Japan Plane Rectangular CS VIII):

- `operation_area.geojson` — harvest block polygon
- `forest_roads.geojson` — connected road network (one segment intentionally disconnected to demonstrate NULL d2)
- `landings.geojson` — multiple landing points
- `avg_extraction_sample.gpkg` — GeoPackage with all of the above

Suggested parameters: grid spacing = 4 m, snapping tolerance = 5 m.

## Notes

- Points with `d2 = NULL` could not be routed to any landing. This typically means the snap
  point lies on a disconnected road segment. Increase snapping tolerance or check road network
  connectivity.
- The algorithm iterates over all landing points and assigns each sample point the minimum d2,
  so multiple landings are handled correctly.

## License

GPL-3.0 — see [LICENSE](LICENSE)

## Author

Takeshi Hoshikawa — hoshikawa.takeshi@spua.ac.jp
