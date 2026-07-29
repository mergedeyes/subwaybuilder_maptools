# Custom DEPOT Map Tools written in Python
### Valid as of v3.0.0
# DISCLAIMER
- This is a wrapper for [depot](https://github.com/Subway-Builder-Modded/depot).
- BUILT FOR GERMANY, PLEASE MAKE SURE THAT OSM BUILDING TAGGING AND YOUR O/D MATRIX IS PRECISE!
---
## Setting up .env file
```
# Only CITY_CODE has to change per city. Everything below already has a
# working default baked into the scripts - only uncomment/edit if your
# setup actually differs.

# 3-DIGIT CITY CODE
CITY_CODE = "FRA"

# Only needed for publish.py / release_maptools.py.
GITHUB_TOKEN = "<Github_Token_Classic>"

# --- Optional overrides (defaults shown) ---
# OSMPBF = "germany-latest.osm.pbf"
# CSV = "QZM_1X1KM.csv"
# RAW_BASE_DIR = "raw_map_files"
# OUTPUT_DIR = "Map_ZIPs"
# OSRM_URL = "http://localhost:5000"

# Hub (demand point) size dial - 1 = original tuning. Set to 4 for hubs
# roughly 4x smaller and ~4x more numerous; 0.5 for ~2x bigger/fewer.
# Real-world commuter totals never change, only how finely they're split
# across points.
# HUB_SIZE_RATIO = "1"
```
- The bounding box is no longer set in `.env` - it's looked up automatically per city and saved to `raw_map_files/CITY_CODE/CITY_CODE_MAP_DATA.json`. The first time you run a script for a city with no saved bbox yet, it'll prompt you to paste one in (e.g. copied from [boundingbox.klokantech.com](http://boundingbox.klokantech.com)).
- Some city codes can be found [here](https://archive.bresser.de/download/City_Codes/CityCodes_en_BRESSER_v082023a.pdf).

## Usage

### Generating a map

This is handled by `build.py`, a thin wrapper around depot's own `depot.maps.MapGen` class - see depot's own [examples/maps](https://github.com/Subway-Builder-Modded/depot/tree/main/examples/maps) (`HEL.py`/`LAXM.py`) for further `MapGen` configuration options and depot's README for the full `MapGen` parameter/method reference.

    python build.py

### Generating demand data

This is handled by `run_demand_pipeline.py`, wrapping two composable pipeline stages exposed by `generate_demand_qzm_local.py` behind a small `DemandGen` class (`demand_gen`, already constructed for `CITY_CODE` at the bottom of the file) - the same build.py/`MapGen`-style shape, just for demand instead of map geometry.

#### `DemandGen` inputs
| Parameter                 | Description       |
| -------------------------- |:-------------:|
| `city`                      | str. 3-4 character city code. Must match `CITY_CODE` in `.env` - `DemandGen` raises immediately if it doesn't, since every function it wraps is already wired to `CITY_CODE`, not to whatever's passed in here.       |
| `bbox`                      | list of floats. Bounding box for the map `[min_lon, min_lat, max_lon, max_lat]`. Resolved automatically from `.env`/`bbox_utils.get_bbox()` - passed in here mainly to keep this class's shape consistent with `MapGen`.        |
| `autofill_special_demand`   | bool. Default: `True`. Auto-estimates a capacity for "landmark"-tier special demand (universities, hospitals, stadiums, museums, zoos) instead of opening a browser tab per one and waiting - same as the high-volume "common" types already get. Set `False` to go back to the manual review-in-browser flow instead (capped at 15 tabs per run); you can still hand-edit the special-demand JSON afterward either way.        |

#### `DemandGen` methods
| Class method       | Description                                                    |
| ------------------- |:--------------------------------------------------------------:|
| `run_all`            | Full pipeline: OSM parsing, green-space capture, optional Zensus calibration, airport hub snapping, and base OSRM routing (`build_base_demand()`); then cluster/consolidate and named special demand (`enrich_demand()`). This is what `python run_demand_pipeline.py` runs by default.                                 |
| `run_enrich_only`    | Re-runs ONLY the enrichment stage (cluster/consolidate + named special demand) against the existing base checkpoint (`demand_data_base.json`) - skips the much slower OSM parsing / airport snapping / base OSRM routing steps. Use after tuning `HUB_SIZE_RATIO` in `.env` or hand-editing special-demand data. See `rerun_enrichment.py` for a ready-to-run example.       |

`run_all` must complete at least once for a given city before `run_enrich_only` has a base checkpoint to enrich.

    python run_demand_pipeline.py                # DemandGen.run_all()
    python rerun_enrichment.py                    # DemandGen.run_enrich_only()

## Running the scripts
### Precautions
1. Install [depot](https://github.com/Subway-Builder-Modded/depot).
2. Run "pip install -r requirements.txt"
3. Download a .osm.pbf file you want to use to extract the mapdata from [Geofabrik](https://download.geofabrik.de/index.html).
4. Get your commuters data in the INSPIRE-Grid format. [Germany](https://mobilithek.info/offers/767359761906577408)
### Running the scripts
5. Get your initial map data with depot, running build.py (Check examples directory within depot (HEL.py/LAXM.py))
6. Prepare the data, running run_demand_pipeline.py
7. Set correct coordinates for airport points, opening Google Maps and entering the correct cords into ./raw_map_files/CITY_CODE/custom_hubs.json, running open_maps.py
8. Generate the demand data, running run_demand_pipeline.py again.
9. Create a config.json inside ./raw_map_files/CITY_CODE/ based on the [official documentation](https://www.subwaybuilder.com/docs/v1.0.0/api-reference/cities).
```
{
    "name": "Frankfurt",
    "code": "FRA",
    "description": "Bring the subway to Frankfurt! Over 1.200.000 commuters want to go to their workplace and it's your job to get them there! Based on real-life demand data from mobilithek.info/offers/767359761906577408 (Latest access: 6th July 2026).",
    "population": 1247000,
    "initialViewState": {
        "zoom": 16,
        "latitude": 50.11,
        "longitude": 8.67,
        "bearing": 0
    },
    "creator": "MergedEyes",
    "version": "1.0.0"
}
```
10. Run "ship.py none", to create the ZIP file. (none: No version change, major: Bumb major version, minor: Bump minor version, patch: Bump patch version inside config.json)
11. Import your ZIP into Railyard (locally) and test your map ingame.
12. (Optional) Make changes to the map, demand configuration, description, etc. Run build.py (only if you change the initial map/city), run_demand_pipeline.py, ship.py <none/major/minor/patch> again.
    - If you only changed something in the enrichment stage - `HUB_SIZE_RATIO` in `.env`, or hand-edited a special-demand JSON file - you don't need the full run_demand_pipeline.py run again. Run `rerun_enrichment.py` instead (see [`DemandGen` methods](#demandgen-methods) above), which skips the much slower OSM parsing / airport snapping / base OSRM routing steps:
      ```
      python rerun_enrichment.py
      ```
13. Publish your map to Github manually, OR:
14. Edit the parameters of publish.py and automatically publish your map to your Github-Repo, also generating a update.json for the SubwayBuilderModded Registry for your map automatically.

## Scripts overview

Everything above walks through running these in order. Here's what each one actually does, for reference.

### Scripts you run directly

| Script                     | What it does       |
| --------------------------- |:-------------|
| `build.py`                   | Wraps depot's `MapGen` to generate the map's non-demand files (PMTiles, roads, building footprints, labels) - see [Generating a map](#generating-a-map) above.        |
| `run_demand_pipeline.py`     | Wraps `DemandGen` to generate the full demand dataset (who commutes where) from OSM buildings + your O/D matrix - see [Generating demand data](#generating-demand-data) above.        |
| `rerun_enrichment.py`        | Example script re-running just `DemandGen`'s enrichment stage - use after tuning `HUB_SIZE_RATIO` or hand-editing special demand, instead of the full `run_demand_pipeline.py` run.        |
| `open_maps.py`                | Opens every detected airport's approximate location in your browser, so you can find its real terminal coordinates for `custom_hubs.json` (step 7).        |
| `ship.py`                     | Packages a city's generated files into a game-ready ZIP, bumping `config.json`'s version as requested (`none`/`major`/`minor`/`patch`).        |
| `publish.py`                  | Uploads a shipped ZIP to your maps GitHub repo and updates that map's `update.json` entry for the SubwayBuilderModded Registry.        |

### Supporting modules

These aren't run directly - `run_demand_pipeline.py` and `generate_demand_qzm_local.py` import them.

| Module                          | What it does       |
| --------------------------------- |:-------------|
| `generate_demand_qzm_local.py`     | Does the actual demand generation work: parses OSM buildings, snaps airport hubs, assigns real commuter counts from your O/D matrix, and (via its enrichment stage) clusters/consolidates points and adds named special demand. `run_demand_pipeline.py` is the entry point for this - see [Generating demand data](#generating-demand-data) above.        |
| `enrich_utils.py`                  | Shared helpers for the enrichment stage: merging nearby points into fewer/bigger hubs, keeping hubs out of parks/green space, and adding named special demand (universities, hospitals, stadiums, etc.) with real routed commutes.        |
| `zensus_utils.py`                  | Optional: if you've downloaded Destatis's Zensus 2022 100m population grid, calibrates residential building capacity against real measured population counts instead of relying on OSM-tag heuristics alone. No-op if the file isn't present.        |
| `bbox_utils.py`                    | Looks up (or interactively captures and saves) each city's bounding box, so you only ever enter it once per city.        |
| `special_demand_utils.py`          | Scans OSM for named "landmark" buildings (universities, hospitals, stadiums, museums, zoos, significant schools/clinics) and keeps them as candidates for named special demand, with a real capacity/enrollment number per entry.        |
