### ==== SCRIPT TO GENERATE REALISTIC DEMAND BASED ON THE OSM BUILDINGS DATA AND CENSUS DATA

import osmium
import json
import math
import random
import subprocess
import os
import sys
import csv
import re
import argparse
import requests
import time
import shutil
import threading
import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely import wkt as shapely_wkt
from pyproj import Transformer
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# Only ever imported, never edited - src/depot is off-limits per this
# project's CLAUDE.md rule ("we only use these methods and functions in our
# scripts located in maps/").
from depot.demand import DemandData

from bbox_utils import get_bbox
from special_demand_utils import detect_and_confirm_special_demand, _open_url_detached
from enrich_utils import (cluster_and_consolidate, add_named_special_demand,
                          route_new_pops, enforce_max_point_size, GreenSpaceIndex,
                          CLUSTER_MAX_POP_THRESHOLD, CLUSTER_BUFFER_METERS)
from zensus_utils import ZensusPopulationGrid, calibrate_residential_capacity

# 0. Parse CLI args. Only --bbox for now - see bbox_utils.get_bbox: a bbox
# already saved for this city always wins, this only matters the first time.
_arg_parser = argparse.ArgumentParser(
    description="Generate demand data from OSM buildings + the QZM commuter matrix."
)
_arg_parser.add_argument(
    "--bbox", type=str, default=None,
    help="Initial bounding box as 'min_lon,min_lat,max_lon,max_lat' (e.g. "
         "copy-pasted from boundingbox.klokantech.com). Only used the first "
         "time this city has no bbox saved yet."
)
_arg_parser.add_argument(
    "--autofill-special-demand", action=argparse.BooleanOptionalAction, default=True,
    help="Default: on. Auto-estimates a capacity for 'landmark'-tier special "
         "demand (universities, hospitals, stadiums, museums, zoos) instead "
         "of opening a browser tab per one and waiting - same as the "
         "high-volume 'common' types already get, so a plain run just goes "
         "straight through. You can still hand-edit the JSON afterward for "
         "specific ones you want a real researched number on. Pass "
         "--no-autofill-special-demand to go back to the manual "
         "review-in-browser flow instead (capped at 15 tabs per run)."
)
# parse_known_args (not parse_args): this module gets imported by other
# scripts too (e.g. run_demand_pipeline.py imports all()/demand() from
# here, and defines its own --stage flag), and we don't want an unrelated
# CLI flag meant for the importing script to blow up with "unrecognized
# arguments".
_cli_args, _ = _arg_parser.parse_known_args()

# 1. Load the environment variables first
load_dotenv()

# 2. Get the city code and other required settings with strict fallbacks
CITY_CODE = os.getenv("CITY_CODE")
if not CITY_CODE:
    raise ValueError("CITY_CODE is missing from your .env file!")

# These all have the same value for every city in this project (same
# country-wide OSM extract, same national commuter matrix, same local folder
# layout) - only override in .env if your setup actually differs.
OSMPBF_FILE = os.getenv("OSMPBF", "germany-latest.osm.pbf")
COMMUTERS_CSV_FILE = os.getenv("CSV", "QZM_1X1KM.csv")
RAW_BASE_DIR = os.getenv("RAW_BASE_DIR", "raw_map_files")
OSRM_URL = os.getenv("OSRM_URL", "http://localhost:5000")

# OPL is no longer read from .env - it always follows this exact pattern, so
# it's derived straight from CITY_CODE instead of being one more thing to set.
OPL_CLEANED_FILE = f"{CITY_CODE.lower()}_cleaned.opl"

# Sanity-check OSRM_URL's shape early (also used elsewhere, e.g. run_demand_pipeline.py).
_osrm_port_match = re.search(r":(\d+)$", OSRM_URL.rstrip("/"))
if not _osrm_port_match:
    raise ValueError(
        f"Could not parse a port number out of OSRM_URL='{OSRM_URL}'. "
        f"Expected http://localhost:<port>."
    )
OSRM_PORT = int(_osrm_port_match.group(1))

# BBOX is no longer read from .env - it's looked up (or interactively captured
# and saved) per city code. See bbox_utils.py.
BBOX_MIN_LON, BBOX_MIN_LAT, BBOX_MAX_LON, BBOX_MAX_LAT = get_bbox(
    CITY_CODE, RAW_BASE_DIR, bbox_arg=_cli_args.bbox
)

# ==========================================
# CONFIGURATION
# ==========================================
CITY_RAW_DIR = f"{RAW_BASE_DIR}/{CITY_CODE}/"

# Optional Zensus 2022 100m population grid (see zensus_utils.py's module
# comment for the download link). Unlike everything else in CITY_RAW_DIR,
# this file is NATIONWIDE - Destatis publishes one file covering all of
# Germany, not one per city - so it lives once, directly alongside this
# script, and gets bbox-filtered per city at load time instead of being
# duplicated into every city's raw folder. Entirely optional: the pipeline
# runs exactly as before if the file hasn't been downloaded yet.


def _find_zensus_grid_csv():
    """
    Looks for the (shared, nationwide) Zensus 2022 100m population grid CSV
    directly in this script's own directory. Returns None (not an error) if
    no matching file exists yet - this is a data-quality refinement, not a
    requirement to run the pipeline.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [f for f in os.listdir(script_dir)
                  if f.lower().endswith(".csv")
                  and "100m" in f.lower()
                  and "zensus" in f.lower()]
    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"  [WARNING] Multiple candidate Zensus 100m grid CSVs found in "
              f"'{script_dir}' - using '{candidates[0]}'. Remove the others "
              f"if that isn't the right file.")
    return os.path.join(script_dir, candidates[0])


OUTPUT_FILE = f"{CITY_RAW_DIR}demand_data.json"
AIRPORT_GEOJSON = f"{CITY_RAW_DIR}runways_taxiways.geojson"
CUSTOM_HUBS_JSON = f"{CITY_RAW_DIR}custom_hubs.json"

# Checkpoint written by build_base_demand() and read by enrich_demand() -
# see the "modular pipeline" split below (all()/build_base_demand()/
# enrich_demand()/demand()). Holds the raw OSM-parsed, QZM-routed points/pops
# BEFORE clustering/consolidation or named special demand - i.e. everything
# that's slow to regenerate (OSM parsing, airport snapping, base OSRM
# routing). OUTPUT_FILE remains the final enriched result either way.
BASE_OUTPUT_FILE = f"{CITY_RAW_DIR}demand_data_base.json"
# Cache of green-space polygons captured during OSM parsing (see
# capture_green_space_relations()/_is_green_space() above), so enrich_demand()
# can rebuild the same GreenSpaceIndex without re-parsing the full cleaned
# OPL file just to get these back. Written by build_base_demand().
GREEN_SPACE_CACHE_FILE = f"{CITY_RAW_DIR}green_space_cache.json"

# None of the docker run commands below set --cpus/--cpuset-cpus, so the
# container already sees every core Docker itself has access to - on native
# Linux Docker that's the whole host; on Docker Desktop (Mac/Windows) it's
# whatever's configured under Settings -> Resources -> CPUs, which can be
# lower than the host's real core count regardless of anything here. OSRM's
# own tools (extract/partition/customize/routed) default their internal
# --threads to "every logical CPU visible to the container" when not given
# explicitly - so this isn't strictly required, just makes the number
# predictable/explicit instead of implicit. Bumped from 14 to the full 16 -
# measured throughput during routing (~1815 req/s at 14 threads, ~130/s per
# thread) suggested a per-thread server ceiling rather than a client
# concurrency one, so it's worth giving OSRM every core during this phase
# rather than reserving 2 - see the CPU utilization sampling around the
# routing loop below for whether that ceiling is actually CPU-bound.
OSRM_THREADS = os.getenv("OSRM_THREADS", "16")

# Single dial for "more, smaller hubs" vs "fewer, bigger hubs" - set
# HUB_SIZE_RATIO=4 in .env for hubs roughly 4x smaller (and correspondingly
# ~4x more numerous) than the defaults below; HUB_SIZE_RATIO=0.5 for
# roughly 2x bigger/fewer. 1 (default) reproduces the original tuning
# exactly.
#
# Applied as TWO different scale factors, not one flat division, because
# the constants below aren't all the same kind of quantity:
#   - _HUB_SIZE_SCALE (= 1/ratio) divides POPULATION/COUNT caps directly
#     (max_point_size, agglomerate_small_threshold, cluster_points()'s
#     pop-count thresholds) - a "4x smaller hub" should hold ~1/4 the
#     people, so these need a straight 1/ratio.
#   - _HUB_RADIUS_SCALE (= 1/sqrt(ratio)) divides SPATIAL radii/distances
#     (RES_MERGE_RADIUS, JOB_MERGE_RADIUS, cluster_points()'s buffer_meters,
#     agglomerate_pops()'s distance thresholds) - these control the AREA
#     that gets merged into one hub, and area scales with radius^2. Halving
#     a radius already quarters the buildings caught inside it (for roughly
#     uniform density), so radii only need to shrink by sqrt(ratio) to
#     achieve the same ~ratio-times reduction in per-hub population that
#     the flat caps get directly. Using 1/ratio on radii too would
#     overshoot to a ratio^2 reduction in practice.
HUB_SIZE_RATIO = float(os.getenv("HUB_SIZE_RATIO", "1"))
if HUB_SIZE_RATIO <= 0:
    raise ValueError(f"HUB_SIZE_RATIO must be positive, got {HUB_SIZE_RATIO}")
_HUB_SIZE_SCALE = 1.0 / HUB_SIZE_RATIO
_HUB_RADIUS_SCALE = 1.0 / math.sqrt(HUB_SIZE_RATIO)

MAX_HUBS_PER_GRID = 300
MIN_ROUTE_SIZE = 10         
MIN_COMMUTER_THRESHOLD = 10 

MAX_ROUTES_LIMIT = 500_000
MAX_CONNECTIONS_PER_JOB = 200   # Cap on residential hubs pointing to a single job hub

# Below this many routes, a 100%-failed pass just means "no valid road route
# exists for this last handful of pairs" (or a one-off timeout) - normal at
# this scale, not a sign anything is actually wrong. Only warn about a
# possible container outage when a large batch fails outright.
OUTAGE_WARNING_MIN_QUEUE = 25

RES_MERGE_RADIUS = 0.2 * _HUB_RADIUS_SCALE
JOB_MERGE_RADIUS = 0.3 * _HUB_RADIUS_SCALE
AIRPORT_EDGE_SNAP_RADIUS_METERS = 1000

# Everything downstream that decides final hub (point) size, scaled by the
# same HUB_SIZE_RATIO - see cluster_and_consolidate()'s call site in
# build_demand() for where these get used. Population caps use
# _HUB_SIZE_SCALE directly; spatial radii/distances use _HUB_RADIUS_SCALE -
# see the HUB_SIZE_RATIO comment above for why the two differ.
HUB_MAX_POINT_SIZE = max(1, round(2000 * _HUB_SIZE_SCALE))
HUB_AGGLOMERATE_SMALL_THRESHOLD = max(1, round(100 * _HUB_SIZE_SCALE))
HUB_AGGLOMERATE_DISTANCE_NONCBD = 0.01 * _HUB_RADIUS_SCALE
HUB_AGGLOMERATE_DISTANCE_CBD = 0.006 * _HUB_RADIUS_SCALE
HUB_CLUSTER_MAX_POP_THRESHOLD = CLUSTER_MAX_POP_THRESHOLD * _HUB_SIZE_SCALE
HUB_CLUSTER_BUFFER_METERS = CLUSTER_BUFFER_METERS * _HUB_RADIUS_SCALE

SPECIAL_DEMAND_SPLIT = 0.2

# These are only ever used as relative gravity WEIGHTS to decide which
# coordinate gets more of a grid cell's real commuter count (see
# assign_commuters() below, "share = commuters_to_assign * (weight /
# total_weight)") - the real CSV totals themselves never change. Previously
# every amenity=*/shop=*/office=* tagged building shared one flat
# CAPACITY_JOBS=(500,3000) range regardless of type, so a kiosk and an office
# tower drew from identical odds - split by rough real-world size instead so
# the weighting (and MAX_HUBS_PER_GRID, which only helps if the underlying
# weights actually differ) has something real to work with.
CAPACITY_SHOP = (2, 40)          # shop=* - almost always a small single unit
CAPACITY_OFFICE = (10, 150)      # office=*
CAPACITY_JOBS = (50, 500)        # everything else job-tagged (commercial/
                                  # industrial/school/hospital/university/
                                  # civic/government/generic amenity=*)
CAPACITY_APARTMENTS = (50, 500)
CAPACITY_HOUSES = (5, 20)
CAPACITY_HOTEL = (20, 200)       # was lumped into CAPACITY_HOUSES - a hotel
                                  # isn't a single-family house
CAPACITY_YES_WILDCARD = (5, 15)

# building:levels (floor count) is present on ~29% of buildings in the BER
# extract - when it's there, use it to differentiate a 2-floor shop building
# from a 20-floor tower of the same tag instead of leaving that to chance.
# sqrt-dampened so a very tall building doesn't get a literal 20x multiplier
# (floor plates shrink, not every floor is identical usable space, etc.).
BUILDING_LEVELS_CAP = 20

def _levels_multiplier(tags):
    levels_raw = tags.get('building:levels')
    if not levels_raw:
        return 1.0
    try:
        levels = float(levels_raw)
    except ValueError:
        return 1.0
    if levels <= 0:
        return 1.0
    levels = min(levels, BUILDING_LEVELS_CAP)
    return max(1.0, math.sqrt(levels))

JOB_TAGS = {
    'commercial', 'industrial', 'retail', 'office',
    'school', 'hospital', 'university', 'civic', 'government'
}

RESIDENTIAL_TAGS = {
    'house', 'detached', 'semidetached_house', 'residential', 'terrace', 'bungalow', 'hotel'
}

SPECIAL_AMENITIES = {'hospital', 'clinic', 'school', 'university', 'college', 'ferry_terminal'}
SPECIAL_LEISURE = {'park', 'nature_reserve', 'stadium', 'sports_centre', 'water_park'}
SPECIAL_TOURISM = {'theme_park', 'zoo', 'aquarium'}

JOB_INDICATOR_KEYS = {'amenity', 'shop', 'office'}

# Parks/nature reserves/water parks/national-park boundaries are counted as a
# base-pipeline "job" hub (via is_special, see SPECIAL_LEISURE/
# SPECIAL_TOURISM below - footfall/staff for that facility), but they're
# almost always mapped as a large polygon (way), and using the polygon's raw
# nodes[0] vertex put the hub at an arbitrary point on the boundary ring -
# which for a big or oddly-shaped park is still deep inside the green fill on
# the rendered map ("hubs on greenland"). Stadium/sports_centre are left out
# of this - those are real addressable buildings, node[0] there is a
# reasonable building-corner location, not the middle of open green space.
GREEN_SPACE_LEISURE = {'park', 'nature_reserve', 'water_park'}
GREEN_SPACE_TOURISM = {'theme_park', 'zoo', 'aquarium'}
# landuse=recreation_ground: added after finding that Tempelhofer Feld (the
# huge former Berlin airport field that kept getting hubs placed inside it)
# isn't tagged leisure=park at all - it's landuse=recreation_ground on a
# multipolygon RELATION. Real OSM tagging is inconsistent about which of
# leisure=park vs landuse=recreation_ground/village_green a given big green
# area gets, so recreation_ground needs the same treatment.
GREEN_SPACE_LANDUSE = {'recreation_ground'}


def _is_green_space(tags):
    return (tags.get('leisure') in GREEN_SPACE_LEISURE
            or tags.get('tourism') in GREEN_SPACE_TOURISM
            or tags.get('landuse') in GREEN_SPACE_LANDUSE
            or tags.get('boundary') == 'national_park')


def _green_space_edge_location(w):
    """
    Computes a rough centroid from all of this way's resolved node
    locations, then pushes the nodes[0] boundary vertex further out along
    the centroid->vertex direction, landing just outside the green area
    (roughly where an entrance or adjacent building would sit) instead of
    on/inside its own boundary ring. Push distance scales with how far the
    vertex already sits from the centroid (a rough proxy for the park's own
    size) so a small pocket park doesn't get flung disproportionately far,
    and a huge park (Tiergarten-scale) gets pushed further than a token 20m.
    This is a cheap approximation, not real point-in-polygon geometry - it
    won't fully clear every concave/oddly-shaped park, but avoids the common
    case of landing deep inside a large convex-ish green fill.
    """
    v_lat, v_lon = w.nodes[0].location.lat, w.nodes[0].location.lon
    centroid_lat = sum(n.location.lat for n in w.nodes) / len(w.nodes)
    centroid_lon = sum(n.location.lon for n in w.nodes) / len(w.nodes)

    lat_scale = 1 / 111_000
    lon_scale = 1 / (111_000 * max(math.cos(math.radians(v_lat)), 0.1))

    dy_m = (v_lat - centroid_lat) / lat_scale
    dx_m = (v_lon - centroid_lon) / lon_scale
    dist_m = math.hypot(dx_m, dy_m)
    if dist_m < 1.0:
        return v_lat, v_lon  # degenerate shape - nothing sensible to push toward

    ux, uy = dx_m / dist_m, dy_m / dist_m
    push_m = min(100.0, max(20.0, dist_m * 0.15))
    out_lat = v_lat + uy * push_m * lat_scale
    out_lon = v_lon + ux * push_m * lon_scale
    return out_lat, out_lon


def capture_green_space_relations(cleaned_opl_path):
    """
    Dedicated second pass over the cleaned OPL file to capture green-space
    polygons that OSMHandler.way() can never see: MULTIPOLYGON RELATIONS.
    Real case that exposed this: Tempelhofer Feld (Berlin) is mapped as a
    relation (type=multipolygon) with its tags (landuse=recreation_ground)
    on the RELATION itself - its member ways carry no tags of their own, so
    they're invisible to way()'s "if len(w.tags) > 0" check no matter what
    _is_green_space() recognizes. osmium.SimpleHandler has no callback for
    assembled multipolygon geometry at all; that requires this separate
    area-assembly pass via osmium.FileProcessor(...).with_areas().

    Only relation-derived areas are kept (from_way() == False) - polygons
    for simple closed ways are already captured more cheaply inline by
    OSMHandler.way()/_capture_green_space_polygon() as they're parsed, so
    keeping only relations here avoids building every building-as-a-way
    polygon in the file twice.

    Handles holes (inner rings) properly via shapely's Polygon(shell,
    holes) - relevant for parks with excluded buildings/lakes inside their
    outer boundary.
    """
    import osmium.filter as osmium_filter

    # KeyFilter only restricts which RELATIONS are even considered as area
    # candidates in the (cheap) first pass - the second pass still builds
    # simple-area geometry for every closed way in the file regardless, so
    # this is an added real cost on top of the main handler.apply_file()
    # parse, not a free lookup. Timed at ~17s against a real Berlin cleaned
    # OPL (605MB) - worth it since this is the only way to catch a park
    # this size and shape mapped as a relation.
    key_filter = osmium_filter.KeyFilter('leisure', 'tourism', 'boundary', 'landuse')
    file_processor = osmium.FileProcessor(cleaned_opl_path).with_areas(key_filter)

    polygons = []
    for area in file_processor:
        if not area.is_area() or area.from_way():
            continue
        tags = dict(area.tags)
        if not _is_green_space(tags):
            continue
        try:
            for outer_ring in area.outer_rings():
                shell = [(node_ref.lon, node_ref.lat) for node_ref in outer_ring]
                if len(shell) < 4:
                    continue
                holes = [[(node_ref.lon, node_ref.lat) for node_ref in inner_ring]
                         for inner_ring in area.inner_rings(outer_ring)]
                poly = Polygon(shell, holes)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_valid and not poly.is_empty and poly.area > 0:
                    polygons.append(poly)
        except RuntimeError:
            # "Illegal access to removed OSM object" - the area assembly
            # buffer can in rare cases get invalidated; extraction happens
            # inline within this same loop iteration specifically to avoid
            # that, but a malformed relation could still hit it. Skip it,
            # same as the try/except around simple-way polygon capture.
            continue
    return polygons


def _save_green_space_cache(polygons, path):
    """
    Serializes captured green-space polygons (built during OSM parsing in
    build_base_demand()) to a small WKT-list JSON file, so enrich_demand()
    can rebuild an identical GreenSpaceIndex later without re-parsing the
    full (multi-hundred-MB) cleaned OPL file just to get these back.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump([poly.wkt for poly in polygons], f)


def _load_green_space_cache(path):
    """
    Inverse of _save_green_space_cache(). Returns [] (not an error) if the
    cache doesn't exist yet - callers should treat this the same as "no
    green-space polygons found", same as build_base_demand() does when OSM
    genuinely has none for this city.
    """
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        wkt_list = json.load(f)
    return [shapely_wkt.loads(w) for w in wkt_list]


def prepare_cleaned_pbf(raw_file, cleaned_file):
    if os.path.exists(cleaned_file):
        print(f"Optimized OPL file '{cleaned_file}' exists. Skipping preparation.")
        return True

    print(f"Preparing map data (Extract -> Filter -> Convert)...")
    
    bbox_str = f"{BBOX_MIN_LON},{BBOX_MIN_LAT},{BBOX_MAX_LON},{BBOX_MAX_LAT}"
    extract_file = "city_extract.osm.pbf"
    subprocess.run(["osmium", "extract", "-b", bbox_str, raw_file, "-o", extract_file, "--overwrite"], check=True)

    temp_filtered = "city_filtered.osm.pbf"
    
    # Adding landuse extraction to the filter
    tags_filter = [
        "nwr/building=*", "nwr/amenity=*", "nwr/shop=*", "nwr/office=*",
        "nwr/leisure=park,nature_reserve,stadium,sports_centre,water_park",
        "nwr/tourism=theme_park,zoo,aquarium",
        "nwr/boundary=national_park",
        # recreation_ground added alongside commercial/industrial/retail
        # after finding Tempelhofer Feld is tagged landuse=recreation_ground
        # (on a relation) rather than leisure=park - without this, the whole
        # thing was silently dropped before Python ever got a chance to see
        # it, "nwr/" here matches nodes/ways/relations alike.
        "nwr/landuse=commercial,industrial,retail,recreation_ground"
    ]
    subprocess.run(["osmium", "tags-filter", extract_file] + tags_filter + ["-o", temp_filtered, "--overwrite"], check=True)

    print(f"Converting to OPL format for high-speed parsing...")
    os.makedirs(os.path.dirname(cleaned_file), exist_ok=True)
    subprocess.run(["osmium", "cat", temp_filtered, "-o", cleaned_file, "--overwrite"], check=True)
    
    os.remove(extract_file)
    os.remove(temp_filtered)
    
    print("Map preparation complete.")
    return True

class OSMHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.raw_job_nodes = []
        self.raw_home_nodes = []
        self.raw_landuse = []
        # Real polygon geometry for green-space ways (park/nature_reserve/
        # water_park/theme_park/zoo/aquarium/national_park boundary) - kept
        # around after use (unlike _green_space_edge_location(), which only
        # needed nodes[0] + a centroid) so enrich_utils.py's GreenSpaceIndex
        # can check whether coordinates WE generate later (split siblings,
        # depot's own SO_ agglomeration centroids) land inside real terrain,
        # not just whether they're too close to another point. See the
        # Tempelhofer Feld case: a huge, mostly-empty park where our own
        # generated points had nothing stopping them from drifting into the
        # middle of it.
        self.green_space_polygons = []
        self.min_lat, self.max_lat = float('inf'), float('-inf')
        self.min_lon, self.max_lon = float('inf'), float('-inf')

    def process_element(self, elem_id, tags, lat, lon):
        if not (BBOX_MIN_LAT <= lat <= BBOX_MAX_LAT and BBOX_MIN_LON <= lon <= BBOX_MAX_LON):
            return
            
        if lat < self.min_lat: self.min_lat = lat
        if lat > self.max_lat: self.max_lat = lat
        if lon < self.min_lon: self.min_lon = lon
        if lon > self.max_lon: self.max_lon = lon
        
        # Parse landuse for synthetic spawning
        landuse = tags.get('landuse')
        if landuse in ['commercial', 'industrial', 'retail']:
            self.raw_landuse.append({"lat": lat, "lon": lon, "type": landuse})
            return # Don't double count landuse polygons as physical buildings unless they have building tags

        if tags.get('floating') == 'yes' or tags.get('location') in ['water', 'underwater']:
            return
            
        b_type = tags.get('building') or ''
        if b_type in ['houseboat', 'boathouse', 'floating_home']:
            return

        is_special = (
            (tags.get('amenity') in SPECIAL_AMENITIES) or
            (tags.get('leisure') in SPECIAL_LEISURE) or
            (tags.get('tourism') in SPECIAL_TOURISM) or
            (tags.get('boundary') == 'national_park')
        )

        is_job = (b_type in JOB_TAGS) or any(tags.get(key) for key in JOB_INDICATOR_KEYS) or is_special
        
        is_home = False
        if not is_job:
            if b_type in RESIDENTIAL_TAGS or b_type == 'apartments' or b_type == 'yes':
                is_home = True

        if not is_job and not is_home:
            return

        if is_job:
            if tags.get('shop'):
                cap = random.randint(*CAPACITY_SHOP)  # no levels scaling - a
                                                        # shop node is one unit
                                                        # regardless of the
                                                        # building's height
            elif tags.get('office'):
                cap = int(random.randint(*CAPACITY_OFFICE) * _levels_multiplier(tags))
            else:
                cap = int(random.randint(*CAPACITY_JOBS) * _levels_multiplier(tags))

            self.raw_job_nodes.append({
                "id": elem_id, "lat": lat, "lon": lon,
                "capacity": cap,
                "is_special": is_special
            })
        elif is_home:
            if b_type == 'apartments':
                cap = int(random.randint(*CAPACITY_APARTMENTS) * _levels_multiplier(tags))
            elif b_type == 'hotel':
                cap = int(random.randint(*CAPACITY_HOTEL) * _levels_multiplier(tags))
            elif b_type == 'yes':
                cap = random.randint(*CAPACITY_YES_WILDCARD)  # too ambiguous a
                                                                # tag to trust
                                                                # levels scaling
            else:
                cap = random.randint(*CAPACITY_HOUSES)  # single-family house -
                                                          # more floors isn't
                                                          # more households

            self.raw_home_nodes.append({"lat": lat, "lon": lon, "capacity": cap})

    def node(self, n):
        if len(n.tags) > 0:
            # A bare point marker directly tagged leisure=park/nature_reserve/
            # water_park or tourism=theme_park/zoo/aquarium (no building) is
            # almost always a sub-feature *inside* a larger park/reserve
            # polygon (a bench, meadow, viewpoint marker, ticket booth) - that
            # polygon (handled in way() below) already generates its own job
            # hub for the whole area. Counting the marker too would double up
            # demand at essentially the same spot, and as a bare point it has
            # no polygon to push outward from either, so it would sit
            # wherever the marker happens to be - often deep inside the green
            # area. Skip it; the parent polygon already covers this facility.
            if _is_green_space(n.tags) and 'building' not in n.tags:
                return
            self.process_element(n.id, n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if len(w.tags) > 0:
            try:
                if _is_green_space(w.tags):
                    lat, lon = _green_space_edge_location(w)
                    self._capture_green_space_polygon(w)
                else:
                    lat, lon = w.nodes[0].location.lat, w.nodes[0].location.lon
                self.process_element(w.id, w.tags, lat, lon)
            except osmium.InvalidLocationError: pass

    def _capture_green_space_polygon(self, w):
        # Real building/park data occasionally has degenerate rings (too
        # few points, self-intersections, duplicate closing vertex issues)
        # - shapely raises/produces an invalid geometry for those. This is
        # a nice-to-have exclusion zone, not a correctness-critical path,
        # so skip anything shapely can't build cleanly rather than let one
        # bad way abort the whole parse.
        try:
            ring = [(n.location.lon, n.location.lat) for n in w.nodes]
            if len(ring) < 4:
                return
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)  # standard shapely fix for minor self-intersections
            if poly.is_valid and not poly.is_empty and poly.area > 0:
                self.green_space_polygons.append(poly)
        except Exception:
            pass

def cluster_organic(nodes, radius_km):
    """
    Merges nearby raw building nodes into consolidated "hub" nodes: walking
    the list in order, each not-yet-absorbed node becomes a new cluster at
    its OWN coordinate (not a centroid), and any other not-yet-absorbed
    node within radius_km of it gets folded in (capacity summed, is_special
    OR'd) and marked absorbed so it's never its own cluster later.

    This is a performance rewrite of the exact same algorithm - same
    Euclidean-in-degrees distance metric the original hand-rolled bucket
    grid used (not true geographic/haversine distance - that approximation
    is unchanged, only how neighbors are found is different), same
    processing order, same output. Neighbor lookups now go through
    scipy's cKDTree instead of a manual Python dict-bucket + nested loop.

    Queries are done LAZILY, one point at a time, only for points still
    unprocessed when the loop reaches them - NOT as one upfront batched
    query_ball_point(coords, ...) call for every point. That seems like
    the obvious "vectorize it" move, but real building data clusters into
    dense blocks, and in a tight cluster of k mutually-close points only
    the FIRST one (in list order) ever actually needs its neighbor list -
    every other point in that cluster gets absorbed and skipped before its
    own turn. Precomputing all n lists upfront still pays the full O(k)
    cost for every one of those k points regardless, which measured out
    as SLOWER than the original bucket grid on realistically-clustered
    data (not uniform-random, which is what made the first version of
    this rewrite look like a win in less representative testing).
    """
    n = len(nodes)
    if n == 0:
        return []

    radius_deg = radius_km / 111.0
    coords = np.array([[node["lat"], node["lon"]] for node in nodes])
    tree = cKDTree(coords)

    processed = np.zeros(n, dtype=bool)
    final_clusters = []

    for i in range(n):
        if processed[i]:
            continue
        node = nodes[i]
        lat_i, lon_i = node["lat"], node["lon"]
        capacity = node["capacity"]
        is_special = node.get("is_special", False)
        processed[i] = True

        for j in tree.query_ball_point((lat_i, lon_i), r=radius_deg):
            if j == i or processed[j]:
                continue
            neighbor = nodes[j]
            # query_ball_point includes points at exactly r (<=); the
            # original bucket-grid code used a strict < radius_deg
            # comparison - re-check exactly so behavior matches bit-for-bit
            # (this candidate set is already small, so the extra check is
            # cheap).
            dlat = lat_i - neighbor["lat"]
            dlon = lon_i - neighbor["lon"]
            if (dlat * dlat + dlon * dlon) ** 0.5 >= radius_deg:
                continue
            capacity += neighbor["capacity"]
            if neighbor.get("is_special"):
                is_special = True
            processed[j] = True

        final_clusters.append({
            "id": f"cluster_{len(final_clusters)}",
            "lat": lat_i, "lon": lon_i,
            "capacity": capacity,
            "is_special": is_special
        })

    return final_clusters

def initialize_map_data():
    target_dir = "raw_map_files/data_osrm"
    target_full_path = os.path.abspath(target_dir)
    # Set project_root to data_osrm directory
    project_root = target_full_path
    os.makedirs(target_full_path, exist_ok=True)

    
    # Check if OSMPBF_FILE file symlink already exists inside target_full_path
    if not os.path.exists(os.path.join(target_full_path, OSMPBF_FILE)):
        print(f"{OSMPBF_FILE} is not present in {target_dir}, copying...")
        shutil.copy(os.path.join(os.getcwd(), OSMPBF_FILE), os.path.join(target_full_path, OSMPBF_FILE))
    
    # Paths relative to the container mount point /data
    pbf_input = f"/data/{OSMPBF_FILE}"
    osrm_output_base = f"/data/germany-latest"
    
    if os.path.exists(f"{target_full_path}/germany-latest.osrm.cell_metrics"):
        print("OSRM map data files already exist. Skipping generation.")
    else:
        try:
            print(f"Generating OSRM map data in {target_dir}...")
            
            if not os.path.exists(os.path.join(target_full_path, "germany-latest.osrm")):
                # 1. Extract: Use -w /data/raw_map_files/data_osrm
                subprocess.run(["docker", "run", "--rm", "-t", "-v", f"{project_root}:/data",
                                "-w", f"{target_full_path}", "osrm/osrm-backend",
                                "osrm-extract", pbf_input, "-p", "/opt/car.lua",
                                "--threads", OSRM_THREADS], check=True)

            if not os.path.exists(os.path.join(target_full_path, "germany-latest.osrm.partition")):
                # 2. Partition: Target the output in the subfolder
                subprocess.run(["docker", "run", "--rm", "-t", "-v", f"{project_root}:/data",
                                "-w", f"{target_full_path}", "osrm/osrm-backend", "osrm-partition",
                                f"{osrm_output_base}.osrm", "--threads", OSRM_THREADS], check=True)

            if not os.path.exists(os.path.join(target_full_path, "germany-latest.osrm.cell_metrics")):
                # 3. Customize: Target the output in the subfolder
                subprocess.run(["docker", "run", "--rm", "-t", "-v", f"{project_root}:/data",
                                "-w", f"{target_full_path}", "osrm/osrm-backend", "osrm-customize",
                                f"{osrm_output_base}.osrm", "--threads", OSRM_THREADS], check=True)
        finally:
            print("Map data generation complete.")
    return target_full_path, osrm_output_base

def start_osrm_container():
    target_full_path, osrm_output_base = initialize_map_data()

    # Clean up any leftover container from a previous crashed/aborted run
    # (otherwise "docker run --name osrm-backend" below fails with "name already in use")
    subprocess.run(["docker", "rm", "-f", "osrm-backend"], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("Starting OSRM backend container...")
    subprocess.run(["docker", "run", "-d", "--name", "osrm-backend", "--network", "host",
                    "-v", f"{target_full_path}:/data", "-w", f"{target_full_path}", "osrm/osrm-backend",
                    "osrm-routed", "--algorithm", "mld", f"{osrm_output_base}.osrm",
                    "--threads", OSRM_THREADS], check=True)

    # Poll until ready. Use a real coordinate pair from this city's own BBOX
    # (rounded like the old hardcoded check) instead of a fixed Frankfurt-area point.
    check_lon1, check_lat1 = round(BBOX_MIN_LON, 2), round(BBOX_MIN_LAT, 2)
    check_lon2, check_lat2 = round(BBOX_MAX_LON, 2), round(BBOX_MAX_LAT, 2)
    health_url = f"{OSRM_URL}/route/v1/driving/{check_lon1},{check_lat1};{check_lon2},{check_lat2}"

    print("Waiting for OSRM to load map into memory...", end="", flush=True)
    for i in range(60): # Try for 60 seconds
        try:
            if requests.get(health_url, timeout=1).status_code == 200:
                print(" Ready!")
                return
        except requests.RequestException:
            print(".", end="", flush=True)
            time.sleep(1)
    raise RuntimeError("OSRM container failed to start in time.")

def stop_osrm_container():
    print("\nStopping and removing OSRM container...")
    # check=False: if the container never started (e.g. start_osrm_container
    # raised before/while starting it), there's nothing to stop/remove - don't
    # let cleanup itself throw and mask the original error.
    subprocess.run(["docker", "stop", "osrm-backend"], check=False)
    subprocess.run(["docker", "rm", "osrm-backend"], check=False)


class _CpuSampler:
    """
    Background sampler that polls `docker stats` for a container's CPU%
    (100% = one full core saturated - docker reports this uncapped, so 4
    cores fully busy shows as 400%) roughly once a second while a `with`
    block runs, so we can tell empirically whether OSRM is actually
    CPU-bound during routing instead of guessing from request-throughput
    math. Runs in a daemon thread so it can't block process exit if
    something goes wrong; failures to sample (docker not found, container
    already gone, transient parse error) are swallowed rather than crashing
    the whole run - this is a diagnostic, not a critical path.
    """
    def __init__(self, container_name, interval=1.0):
        self.container_name = container_name
        self.interval = interval
        self.samples = []
        self._stop_event = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            try:
                result = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}",
                     self.container_name],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    pct = float(result.stdout.strip().rstrip("%"))
                    self.samples.append(pct)
            except Exception:
                pass
            self._stop_event.wait(self.interval)

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def report(self, threads_configured):
        if not self.samples:
            print("  (No CPU samples collected - `docker stats` may not be "
                  "available in this environment.)")
            return
        avg_cpu = sum(self.samples) / len(self.samples)
        max_cpu = max(self.samples)
        ceiling = threads_configured * 100
        print(f"  OSRM container CPU: avg {avg_cpu:.0f}%, peak {max_cpu:.0f}% "
              f"of a possible {ceiling}% (100% = 1 full core, "
              f"{threads_configured} OSRM threads configured) "
              f"[{len(self.samples)} sample(s)]")

def build_base_demand(autofill_special_demand=None):
    """
    Base stage of the modular pipeline: OSM parsing, green-space capture,
    optional Zensus calibration, airport mega-hub snapping, INSPIRE grid
    mapping, QZM commuter assignment, and base OSRM routing - everything
    that's slow to regenerate and doesn't depend on HUB_SIZE_RATIO tuning or
    hand-edited special-demand data. Saves a checkpoint to BASE_OUTPUT_FILE
    (raw points/pops, no clustering/consolidation/special demand yet) plus a
    green-space polygon cache, both consumed by enrich_demand(). Returns
    True on success, False on any early-exit condition (no buildings found,
    missing commuter CSV, airport hub coordinates still pending manual
    input) - see all(), which uses this to decide whether to proceed to
    enrich_demand().

    autofill_special_demand: overrides the --autofill-special-demand CLI
    flag when called programmatically (e.g. from a DemandGen instance) -
    defaults to whatever the CLI flag resolved to (True unless
    --no-autofill-special-demand was passed) when left as None.
    """
    if autofill_special_demand is None:
        autofill_special_demand = _cli_args.autofill_special_demand

    cleaned_opl_path = f"{CITY_RAW_DIR}{OPL_CLEANED_FILE}"
    if not prepare_cleaned_pbf(OSMPBF_FILE, cleaned_opl_path):
        return False

    # Detect/confirm named special demand landmarks (universities, hospitals,
    # stadiums, etc.) from the same cleaned OSM data, before doing anything
    # heavier. This is a good spot for it since it may pause on `input()`
    # waiting on you to fill in capacity numbers - no OSRM container is
    # running yet, so nothing sits idle while you do that. Only entries with
    # a real capacity/enrollment/attendance number get used later, in the
    # enrichment step at the end of this function (see special_demand_utils.py).
    bbox = [BBOX_MIN_LON, BBOX_MIN_LAT, BBOX_MAX_LON, BBOX_MAX_LAT]
    ready_special_demand = detect_and_confirm_special_demand(
        CITY_CODE, RAW_BASE_DIR, cleaned_opl_path, bbox,
        autofill_landmarks=autofill_special_demand,
    )

    # Phase timings, so a slow run tells you exactly where the time went
    # instead of guessing - printed as a summary at the very end. Starts here
    # (not at the top of the function), so it never counts time spent
    # actually reviewing/typing special demand numbers by hand.
    _phase_times = {}
    _t_phase_start = time.time()
    def _mark_phase(label):
        nonlocal _t_phase_start
        now = time.time()
        _phase_times[label] = now - _t_phase_start
        _t_phase_start = now

    print(f"\nLoading '{cleaned_opl_path}'...", end=" ", flush=True)
    handler = OSMHandler()
    handler.apply_file(cleaned_opl_path, locations=True)
    print("OK")

    if not handler.raw_home_nodes and not handler.raw_job_nodes:
        print("\n[ERROR] No buildings found within the specified bounding box.")
        return False

    # Second pass, specifically for green-space polygons mapped as
    # MULTIPOLYGON RELATIONS (e.g. Tempelhofer Feld) rather than a single
    # tagged closed way - see capture_green_space_relations()'s docstring
    # for why OSMHandler.way() can never see these on its own.
    print("Scanning for green-space multipolygon relations...", end=" ", flush=True)
    handler.green_space_polygons.extend(capture_green_space_relations(cleaned_opl_path))
    print("OK")
    _mark_phase("Scanning green-space multipolygon relations")

    # Built once here, from whatever green-space polygons OSM parsing just
    # captured, and reused for every downstream check (split placement,
    # depot's own agglomerate_pops() SO_ centroids) - see GreenSpaceIndex's
    # docstring in enrich_utils.py for why a single OSM-extraction-time edge
    # push isn't enough on its own.
    green_index = GreenSpaceIndex(handler.green_space_polygons)
    if handler.green_space_polygons:
        print(f"  -> Captured {len(handler.green_space_polygons):,} green-space "
              f"polygon(s) for later placement checks.")
    _save_green_space_cache(handler.green_space_polygons, GREEN_SPACE_CACHE_FILE)

    # Optional: calibrate residential building capacity against a real
    # measured Zensus 2022 100m population count, instead of relying solely
    # on the OSM-heuristic CAPACITY_* ranges - see zensus_utils.py's module
    # comment for what this does and why, and _find_zensus_grid_csv()'s
    # docstring for where the file needs to live. No-op (prints a note and
    # continues exactly as before) if the file hasn't been downloaded yet.
    zensus_csv_path = _find_zensus_grid_csv()
    if zensus_csv_path:
        print(f"Loading Zensus 2022 population grid from '{zensus_csv_path}'...")
        zensus_bbox = [handler.min_lon, handler.min_lat, handler.max_lon, handler.max_lat]
        try:
            zensus_grid = ZensusPopulationGrid(zensus_csv_path, bbox=zensus_bbox)
            calibrate_residential_capacity(handler.raw_home_nodes, zensus_grid)
        except Exception as e:
            print(f"  [WARNING] Couldn't use the Zensus population grid ({e}) - "
                  f"falling back to OSM-heuristic residential capacity only.")
    else:
        print("No Zensus 2022 population grid found (optional) - residential "
              "capacity will use OSM-heuristic estimates only. See "
              "zensus_utils.py's module comment for how to add one.")
    _mark_phase("Zensus population grid calibration (optional)")

    avg_lat_rad = math.radians((handler.min_lat + handler.max_lat) / 2.0)
    height_km = (handler.max_lat - handler.min_lat) * 111.0
    width_km = (handler.max_lon - handler.min_lon) * 111.0 * math.cos(avg_lat_rad)
    map_area_sqkm = max(1.0, width_km * height_km)
    
    print(f"\nMap Bounds Detected: {width_km:.1f}km x {height_km:.1f}km")
    print(f"Total Area: {map_area_sqkm:.1f} sq/km")
    print(f"Extracted {len(handler.raw_landuse):,} commercial/industrial zones for procedural overflow.")

    print(f"Clustering {len(handler.raw_home_nodes):,} residential and {len(handler.raw_job_nodes):,} job buildings in parallel...", end=" ", flush=True)
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_homes = executor.submit(cluster_organic, handler.raw_home_nodes, RES_MERGE_RADIUS)
        future_jobs = executor.submit(cluster_organic, handler.raw_job_nodes, JOB_MERGE_RADIUS)
        
        final_home_nodes = future_homes.result()
        final_job_nodes = future_jobs.result()
        
    print("OK")
    print(f"Result: {len(final_home_nodes):,} organic residential hubs.")
    print(f"Result: {len(final_job_nodes):,} organic job hubs.")
    _mark_phase("OSM parsing + organic clustering")

    print("\nGenerating demand points...")
    print("Initializing EPSG:3035 Transformer...", end=" ", flush=True)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    print("OK")

    if os.path.exists(AIRPORT_GEOJSON):
        print(f"Parsing Airport data from {AIRPORT_GEOJSON}...", end=" ", flush=True)
        with open(AIRPORT_GEOJSON, 'r') as f:
            geojson_data = json.load(f)
            
        features_data = []
        for feature in geojson_data.get('features', []):
            geom = feature.get('geometry', {})
            pts = []
            if geom.get('type') == 'Polygon':
                for ring in geom.get('coordinates', []): pts.extend(ring)
            elif geom.get('type') == 'MultiPolygon':
                for poly in geom.get('coordinates', []):
                    for ring in poly: pts.extend(ring)
                        
            if pts:
                lats, lons = [p[1] for p in pts], [p[0] for p in pts]
                features_data.append({
                    "pts": pts, "count": len(pts),
                    "center_lat": sum(lats) / len(lats),
                    "center_lon": sum(lons) / len(lons)
                })
        
        if features_data:
            airport_clusters = []
            CLUSTER_RADIUS_DEG = 0.04 
            
            for feat in sorted(features_data, key=lambda x: x['count'], reverse=True):
                placed = False
                for ap in airport_clusters:
                    dist = math.hypot(feat["center_lat"] - ap["center_lat"], feat["center_lon"] - ap["center_lon"])
                    if dist < CLUSTER_RADIUS_DEG:
                        ap["features"].append(feat)
                        ap["center_lat"] = sum(f["center_lat"] for f in ap["features"]) / len(ap["features"])
                        ap["center_lon"] = sum(f["center_lon"] for f in ap["features"]) / len(ap["features"])
                        placed = True
                        break
                if not placed:
                    airport_clusters.append({
                        "features": [feat],
                        "center_lat": feat["center_lat"],
                        "center_lon": feat["center_lon"]
                    })
            
            print("OK")
            print("Preparing Interactive Hub Configurations...", end=" ", flush=True)
            
            airport_hubs = []
            detected_airports = {}
            
            for i, ap in enumerate(airport_clusters):
                ap_id = f"mega_hub_airport_{i}"
                valid_lats, valid_lons = [], []
                for feat in ap["features"]:
                    for lon, lat in feat["pts"]:
                        valid_lons.append(lon)
                        valid_lats.append(lat)
                
                default_lat = sum(valid_lats) / len(valid_lats)
                default_lon = sum(valid_lons) / len(valid_lons)
                
                all_x, all_y = [], []
                for lon, lat in zip(valid_lons, valid_lats):
                    x, y = transformer.transform(lon, lat)
                    all_x.append(x)
                    all_y.append(y)
                    
                airport_hubs.append({
                    "id": ap_id,
                    "bbox": (min(all_x), max(all_x), min(all_y), max(all_y)),
                    "capacity": 0,
                    "grids": set()
                })
                
                detected_airports[ap_id] = {
                    "name": f"Airport Cluster {i}",
                    "default_center_lat": round(default_lat, 6),
                    "default_center_lon": round(default_lon, 6),
                    "override_lat": None,
                    "override_lon": None
                }

            needs_user_input = False
            custom_hubs_data = {"airports": {}}
            
            if os.path.exists(CUSTOM_HUBS_JSON):
                try:
                    with open(CUSTOM_HUBS_JSON, 'r') as f:
                        custom_hubs_data = json.load(f)
                except json.JSONDecodeError:
                    print(f"\n[ERROR] {CUSTOM_HUBS_JSON} is invalid JSON. Please fix it or delete it.")
                    sys.exit(1)
                    
            for ap_id, def_data in detected_airports.items():
                if ap_id not in custom_hubs_data.get("airports", {}):
                    custom_hubs_data.setdefault("airports", {})[ap_id] = def_data
                    needs_user_input = True
                else:
                    user_data = custom_hubs_data["airports"][ap_id]
                    olat = user_data.get("override_lat")
                    olon = user_data.get("override_lon")
                    
                    if olat is None or olon is None:
                        needs_user_input = True
                    else:
                        olat, olon = float(olat), float(olon)
                        if not (BBOX_MIN_LAT <= olat <= BBOX_MAX_LAT and BBOX_MIN_LON <= olon <= BBOX_MAX_LON):
                            print(f"\n[ERROR] Override coordinates for {ap_id} are outside the map bounding box!")
                            sys.exit(1)
                            
            if needs_user_input:
                os.makedirs(os.path.dirname(CUSTOM_HUBS_JSON), exist_ok=True)
                with open(CUSTOM_HUBS_JSON, 'w') as f:
                    json.dump(custom_hubs_data, f, indent=4)

                pending_airport_ids = [
                    ap_id for ap_id, def_data in custom_hubs_data.get("airports", {}).items()
                    if def_data.get("override_lat") is None or def_data.get("override_lon") is None
                ]
                print(f"\n[ACTION NEEDED] {len(pending_airport_ids)} airport hub(s) need a manual "
                      f"terminal coordinate override in '{CUSTOM_HUBS_JSON}'.")
                print("Opening each one's approximate location in your browser so you can find the exact terminal building...")
                for ap_id in pending_airport_ids:
                    def_data = custom_hubs_data["airports"][ap_id]
                    lat, lon = def_data["default_center_lat"], def_data["default_center_lon"]
                    print(f" -> Opening {ap_id}: ({lat}, {lon})")
                    _open_url_detached(f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")

                input(
                    f"\nOpen '{CUSTOM_HUBS_JSON}' and replace 'null' with the exact lat/lon "
                    f"for the hub(s) above.\nPress Enter once you're done - or just leave it "
                    f"for later, nothing is lost.\n"
                )

                with open(CUSTOM_HUBS_JSON, 'r') as f:
                    custom_hubs_data = json.load(f)

                still_missing = []
                for ap_id in pending_airport_ids:
                    user_data = custom_hubs_data["airports"][ap_id]
                    olat, olon = user_data.get("override_lat"), user_data.get("override_lon")
                    if olat is None or olon is None:
                        still_missing.append(ap_id)
                    else:
                        olat, olon = float(olat), float(olon)
                        if not (BBOX_MIN_LAT <= olat <= BBOX_MAX_LAT and BBOX_MIN_LON <= olon <= BBOX_MAX_LON):
                            print(f"\n[ERROR] Override coordinates for {ap_id} are outside the map bounding box!")
                            sys.exit(1)

                if still_missing:
                    print(f"\nStill missing coordinates for: {', '.join(still_missing)}")
                    print("No worries - continuing without airport hub snapping for now. "
                          "Fill them in and re-run whenever you're ready to include them.")
                    return False

            print("OK (Custom coordinates loaded)")
            
            surviving_job_nodes = []
            for node in final_job_nodes:
                nx, ny = transformer.transform(node["lon"], node["lat"])
                snapped = False
                
                for ap in airport_hubs:
                    min_x, max_x, min_y, max_y = ap["bbox"]
                    dx = max(min_x - nx, 0, nx - max_x)
                    dy = max(min_y - ny, 0, ny - max_y)
                    dist_to_bbox = math.hypot(dx, dy)
                    
                    if dist_to_bbox <= AIRPORT_EDGE_SNAP_RADIUS_METERS: 
                        ap["capacity"] += node["capacity"]
                        grid_id = f"1kmN{int(ny // 1000)}E{int(nx // 1000)}"
                        ap["grids"].add(grid_id)
                        snapped = True
                        break 
                        
                if not snapped:
                    surviving_job_nodes.append(node)
            
            active_airports_count = 0
            for ap in airport_hubs:
                if ap["capacity"] > 0:
                    olat = float(custom_hubs_data["airports"][ap["id"]]["override_lat"])
                    olon = float(custom_hubs_data["airports"][ap["id"]]["override_lon"])
                    
                    surviving_job_nodes.append({
                        "id": ap["id"],
                        "lat": olat,      
                        "lon": olon,      
                        "capacity": ap["capacity"],
                        "is_airport": True,
                        "grids": ap["grids"]
                    })
                    active_airports_count += 1
                    
            final_job_nodes = surviving_job_nodes
            print(f"  -> Generated {active_airports_count} mapped Airport Mega-Hubs.")
    else:
        print(f"Airport GeoJSON '{AIRPORT_GEOJSON}' not found. Skipping airport snapping.")

    points, pops = [], []
    for i, item in enumerate(final_job_nodes):
        if item.get("is_airport"):
            points.append({
                "id": item["id"], 
                "location": [item["lon"], item["lat"]], 
                "jobs": item["capacity"], 
                "residents": 0, 
                "popIds": [],
                "is_airport": True,
                "grids": item["grids"],
                "is_special": item.get("is_special", False),
                "connections": 0
            })
        else:
            points.append({
                "id": f"dp_job_{i}", 
                "location": [item["lon"], item["lat"]], 
                "jobs": item["capacity"], 
                "residents": 0, 
                "popIds": [],
                "is_special": item.get("is_special", False),
                "connections": 0
            })
    
    for item in final_home_nodes:
        points.append({
            "id": item["id"], 
            "location": [item["lon"], item["lat"]], 
            "jobs": 0, 
            "residents": item["capacity"], 
            "popIds": []
        })

    print("Mapping OSM buildings and zones to the INSPIRE 1x1km Grid...", end=" ", flush=True)
    grid_homes, grid_jobs, grid_landuse = {}, {}, {}

    for h in [p for p in points if p["residents"] > 0]:
        x, y = transformer.transform(h["location"][0], h["location"][1])
        gid = f"1kmN{int(y // 1000)}E{int(x // 1000)}"
        grid_homes.setdefault(gid, []).append(h)
        
    for j in [p for p in points if p["jobs"] > 0]:
        is_special = j.get("is_special", False)
        if j.get("is_airport"): is_special = True 
        
        if j.get("is_airport"):
            for gid in j["grids"]: 
                grid_jobs.setdefault(gid, {"normal": [], "special": []})["special"].append(j)
        else:
            x, y = transformer.transform(j["location"][0], j["location"][1])
            gid = f"1kmN{int(y // 1000)}E{int(x // 1000)}"
            grid_jobs.setdefault(gid, {"normal": [], "special": []})
            
            if is_special:
                grid_jobs[gid]["special"].append(j)
            else:
                grid_jobs[gid]["normal"].append(j)

    for lu in handler.raw_landuse:
        x, y = transformer.transform(lu["lon"], lu["lat"])
        gid = f"1kmN{int(y // 1000)}E{int(x // 1000)}"
        grid_landuse.setdefault(gid, []).append(lu)

    print("OK")
    
    pending_routes = []
    
    print(f"\nParsing real-world commuter matrix from {COMMUTERS_CSV_FILE}...", end=" ", flush=True)
    try:
        with open(COMMUTERS_CSV_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
            wo_idx, ao_idx, pendler_idx = headers.index("wo_1km"), headers.index("ao_1km"), headers.index("gesamtpendler")

            def spawn_synthetic_job(grid_id, is_special, target_jobs_list):
                if grid_id in grid_landuse and grid_landuse[grid_id]:
                    zone = random.choice(grid_landuse[grid_id])
                    j_lat = zone["lat"] + random.uniform(-0.001, 0.001)
                    j_lon = zone["lon"] + random.uniform(-0.001, 0.001)
                elif target_jobs_list:
                    base = random.choice(target_jobs_list)
                    j_lat = base["location"][1] + random.uniform(-0.002, 0.002)
                    j_lon = base["location"][0] + random.uniform(-0.002, 0.002)
                else:
                    return False
                    
                new_job = {
                    "id": f"dp_job_synthetic_{len(points)}_{random.randint(1000,9999)}",
                    "location": [j_lon, j_lat],
                    "jobs": 100, 
                    "residents": 0,
                    "popIds": [],
                    "is_special": is_special,
                    "connections": 0
                }
                points.append(new_job)
                target_jobs_list.append(new_job)
                return True

            def assign_commuters(target_jobs, target_count, current_homes, grid_id, is_special):
                if not current_homes: return
                
                commuters_to_assign = target_count
                iterations = 0 
                
                while commuters_to_assign >= MIN_ROUTE_SIZE and iterations < 50:
                    iterations += 1

                    # Hard cap: MAX_ROUTES_LIMIT was previously only checked between
                    # CSV rows, so a single big-demand row could push pending_routes
                    # well past the limit before anything noticed. Check + clamp here too.
                    remaining_budget = MAX_ROUTES_LIMIT - len(pending_routes)
                    if remaining_budget <= 0: break

                    if not target_jobs:
                        if not spawn_synthetic_job(grid_id, is_special, target_jobs): break

                    max_affordable = commuters_to_assign // MIN_ROUTE_SIZE
                    num_conn = min(MAX_HUBS_PER_GRID, len(current_homes), max_affordable, remaining_budget)
                    if num_conn == 0: break

                    potential_routes = []
                    for h in current_homes:
                        for j in target_jobs:
                            if j.setdefault("connections", 0) >= MAX_CONNECTIONS_PER_JOB: continue
                            dist = math.hypot(h["location"][0] - j["location"][0], h["location"][1] - j["location"][1]) * 111000
                            dist = max(dist, 10.0)
                            weight = (h.get("residents", 1) * j.get("jobs", 1)) / (dist ** 2)
                            potential_routes.append((weight, h, j))

                    if not potential_routes:
                        # All current jobs in this grid are maxed out, spawn a new one and loop again
                        if not spawn_synthetic_job(grid_id, is_special, target_jobs): break
                        continue

                    potential_routes.sort(key=lambda x: x[0], reverse=True)

                    # Pick the top num_conn unique (home, job) pairs, respecting connection caps
                    assigned_pairs = set()
                    selected = []
                    for weight, h, j in potential_routes:
                        pair_id = f"{id(h)}_{id(j)}"

                        # Determine if this job hub is an airport
                        is_airport = j.get("is_airport", False)

                        # Bypass connection limit if it is an airport
                        if not is_airport and j.setdefault("connections", 0) >= MAX_CONNECTIONS_PER_JOB:
                            continue

                        if pair_id not in assigned_pairs:
                            assigned_pairs.add(pair_id)
                            # Only increment connections if it is NOT an airport
                            if not is_airport:
                                j["connections"] += 1
                            selected.append((weight, h, j, is_airport))

                        if len(selected) >= num_conn:
                            break

                    if not selected:
                        break

                    # Distribute commuters_to_assign across the selected pairs proportional
                    # to gravity weight instead of splitting it evenly. An even split made
                    # every route/pop in a pass come out roughly the same size regardless of
                    # distance or building size, which is why the demand map looked uniform
                    # (this was PR feedback: "all pops are roughly the same size").
                    total_weight = sum(w for w, _, _, _ in selected)
                    connections_made = 0
                    assigned_commuters = 0
                    route_error = 0.0

                    for weight, h, j, is_airport in selected:
                        share = commuters_to_assign * (weight / total_weight) if total_weight > 0 \
                                else commuters_to_assign / len(selected)
                        exact = share + route_error
                        route_size = max(MIN_ROUTE_SIZE, int(round(exact)))
                        route_error = exact - route_size

                        pop_id = f"pop_{len(pending_routes):06d}"
                        pending_routes.append({
                            "pop_id": pop_id,
                            "h": h,
                            "j": j,
                            "commuters_per_route": route_size
                        })
                        connections_made += 1
                        assigned_commuters += route_size

                    commuters_to_assign -= assigned_commuters

                    if connections_made == 0:
                        break

            for row in reader:
                if len(row) < 3: continue
                count = int(row[pendler_idx])
                if count < MIN_COMMUTER_THRESHOLD: continue 
                if len(pending_routes) >= MAX_ROUTES_LIMIT: break

                wo, ao = row[wo_idx], row[ao_idx]
                if wo in grid_homes and ao in grid_jobs:
                    homes_in_cell = grid_homes[wo]
                    jobs_normal = grid_jobs[ao]["normal"]
                    jobs_special = grid_jobs[ao]["special"]
                    
                    if not homes_in_cell: continue
                    if not jobs_normal and not jobs_special: continue

                    if jobs_special and jobs_normal:
                        special_count = int(count * SPECIAL_DEMAND_SPLIT)
                        normal_count = count - special_count
                    elif jobs_special:
                        special_count = count
                        normal_count = 0
                    else:
                        special_count = 0
                        normal_count = count

                    assign_commuters(jobs_special, special_count, homes_in_cell, ao, True)
                    assign_commuters(jobs_normal, normal_count, homes_in_cell, ao, False)

        print("OK")
        print(f"Prepared {len(pending_routes):,} total route queries.")
        _mark_phase("Airport snapping + grid mapping + CSV commuter assignment")
    except FileNotFoundError:
        print("ERROR")
        print(f"\n[ERROR] '{COMMUTERS_CSV_FILE}' not found. Please place it in the root-directory of this script.")
        return False

    # =========================================================
    # THREADED OSRM FETCHING WITH RETRY ADAPTER & SNAPPING
    # (base routing only - enrichment now runs in its own function/OSRM
    # container lifecycle, see enrich_demand() below)
    # =========================================================
    try:
        start_osrm_container()
        _mark_phase("OSRM container startup (map data prep, cached after the first run)")
        print(f"\nResolving precise distances and times via OSRM ({OSRM_URL})...")

        # requests.Session()'s default HTTPAdapter caps its connection pool at
        # 10 (pool_connections/pool_maxsize), regardless of how many
        # ThreadPoolExecutor workers try to use it - without mounting a
        # bigger pool, most threads just queue for one of 10 real
        # connections instead of genuinely running concurrent requests.
        # Sized generously (TABLE_MAX_WORKERS is defined further below, at
        # 30) since a bigger pool than actually needed costs nothing.
        TABLE_POOL_SIZE = 50
        session = requests.Session()
        _adapter = requests.adapters.HTTPAdapter(pool_connections=TABLE_POOL_SIZE,
                                                  pool_maxsize=TABLE_POOL_SIZE)
        session.mount("http://", _adapter)
        session.mount("https://", _adapter)
        raw_pops = []

        # Many pending_routes can share the exact same (home, job) location
        # pair - assign_commuters()'s while loop can pick the same top-weight
        # pair across multiple rounds when a single CSV row's commuter count
        # needs several rounds to fully assign. The driving duration/distance
        # only depends on the two physical coordinates, never on which pop or
        # how many commuters ride it - so querying OSRM once per UNIQUE pair
        # instead of once per pop cuts real HTTP requests with zero change to
        # the result. Fan-out back to every pop happens after routing below.
        pair_representative = {}
        for task in pending_routes:
            pair_key = (task["h"]["id"], task["j"]["id"])
            if pair_key not in pair_representative:
                pair_representative[pair_key] = task
        unique_tasks = list(pair_representative.values())
        if len(unique_tasks) < len(pending_routes):
            print(f"  {len(pending_routes):,} routes reference only "
                  f"{len(unique_tasks):,} unique home/job pairs - querying "
                  f"OSRM once per unique pair instead of once per route.")

        # OSRM's /table endpoint computes a many-to-many duration/distance
        # MATRIX in one request, instead of /route's one-pair-per-request.
        # Batching TABLE_BATCH_SIZE pairs' homes as "sources" and jobs as
        # "destinations" in one call turns that many individual HTTP round
        # trips into one - we only want the DIAGONAL of the resulting
        # matrix (source i paired with destination i, i.e. each task's own
        # h/j), which wastes server-side compute (an NxN matrix to get N
        # values - at N=40 that's 39x more cells computed than needed) in
        # exchange for fewer requests.
        #
        # This ratio matters because measuring real BER data showed OSRM
        # genuinely CPU-saturated during routing (94-98% of 16 cores' worth)
        # - so the wasted cells are now competing for the same scarce
        # resource as the useful ones, not just filling otherwise-idle
        # capacity. Measured so far: no batching ~69.6s (all per-request
        # overhead, no waste) -> batch=40 60.4s (less overhead, lots of
        # waste) -> batch=10 54.1s (better than both) - since 10 beat both
        # ends, the real optimum is somewhere in between rather than at
        # either extreme. Trying 5 next (waste ratio 9x -> 4x) to see which
        # side of 10 it falls on and narrow in on the actual sweet spot.
        TABLE_BATCH_SIZE = 5
        TABLE_MAX_WORKERS = 30

        batches = [unique_tasks[i:i + TABLE_BATCH_SIZE]
                   for i in range(0, len(unique_tasks), TABLE_BATCH_SIZE)]

        def process_table_batch(batch):
            n = len(batch)
            coords = ([task["h"]["location"] for task in batch]
                      + [task["j"]["location"] for task in batch])
            coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
            radii_str = ";".join(["1000"] * len(coords))
            sources = ";".join(str(i) for i in range(n))
            destinations = ";".join(str(i) for i in range(n, 2 * n))
            url = (f"{OSRM_URL}/table/v1/driving/{coord_str}"
                   f"?sources={sources}&destinations={destinations}"
                   f"&annotations=duration,distance&radiuses={radii_str}")

            try:
                response = session.get(url, timeout=15.0)
                if response.status_code != 200:
                    return {}, batch, []
                data = response.json()
                if data.get("code") != "Ok":
                    return {}, batch, []
                durations = data.get("durations")
                distances = data.get("distances")
                if durations is None or distances is None:
                    return {}, batch, []
            except requests.RequestException:
                return {}, batch, []

            resolved = {}
            fallback_tasks = []
            for i, task in enumerate(batch):
                pair_key = (task["h"]["id"], task["j"]["id"])
                d_sec = durations[i][i]
                d_dist = distances[i][i]
                if d_sec is None or d_dist is None:
                    # This specific pair has no road route - not a batch-
                    # level failure, so fall back to straight-line for just
                    # this pair rather than retrying the whole batch.
                    fallback_tasks.append(task)
                else:
                    resolved[pair_key] = (int(d_sec), int(d_dist))
            return resolved, [], fallback_tasks

        resolved_by_pair = {}
        pending_batches = batches
        attempt = 1

        with _CpuSampler("osrm-backend") as _cpu_sampler:
            while pending_batches:
                pending_pairs = sum(len(b) for b in pending_batches)
                print(f"\n--- OSRM Table Fetch Pass {attempt} ({len(pending_batches):,} "
                      f"batch(es), ~{pending_pairs:,} pairs pending) ---")
                failed_batches = []
                completed_pairs = 0

                with ThreadPoolExecutor(max_workers=TABLE_MAX_WORKERS) as executor:
                    futures = {executor.submit(process_table_batch, b): b for b in pending_batches}

                    for future in as_completed(futures):
                        resolved, retry_batch, fallback_tasks = future.result()
                        resolved_by_pair.update(resolved)
                        completed_pairs += len(resolved)
                        if retry_batch:
                            failed_batches.append(retry_batch)
                        for task in fallback_tasks:
                            h, j = task["h"], task["j"]
                            dist = math.hypot(h["location"][0] - j["location"][0], h["location"][1] - j["location"][1]) * 111000
                            resolved_by_pair[(h["id"], j["id"])] = (int(dist / 8.3), int(dist))

                print(f" -> Resolved {completed_pairs:,} pair(s) in this pass.")

                if not failed_batches:
                    break

                total_failed_pairs = sum(len(b) for b in failed_batches)
                if len(failed_batches) == len(pending_batches):
                    if total_failed_pairs >= OUTAGE_WARNING_MIN_QUEUE:
                        print("\n[WARNING] All remaining batches failed connection. Container might be down.")
                        print("Forcing Euclidean math fallback to clear the queue and prevent infinite loop.")
                    else:
                        print(f"\n{total_failed_pairs} route(s) couldn't be resolved via OSRM after "
                              f"retrying - using straight-line distance for them.")
                    for batch in failed_batches:
                        for task in batch:
                            h, j = task["h"], task["j"]
                            dist = math.hypot(h["location"][0] - j["location"][0], h["location"][1] - j["location"][1]) * 111000
                            resolved_by_pair[(h["id"], j["id"])] = (int(dist / 8.3), int(dist))
                    break

                pending_batches = failed_batches
                attempt += 1

            print("\nOK - OSRM Routing Complete")
            _cpu_sampler.report(int(OSRM_THREADS))
        _mark_phase(f"OSRM routing of {len(pending_routes):,} base commuter routes "
                    f"({len(unique_tasks):,} unique pairs, {len(batches):,} table batches)")

        # Fan each pair's resolved result back out to every pop sharing that
        # pair - each pop keeps its own id/commuters_per_route, just reusing
        # the already-resolved duration/distance instead of a second query.
        for task in pending_routes:
            h, j = task["h"], task["j"]
            seconds, distance = resolved_by_pair[(h["id"], j["id"])]
            raw_pops.append(({
                "id": task["pop_id"],
                "residenceId": h["id"],
                "jobId": j["id"],
                "drivingSeconds": seconds,
                "drivingDistance": distance
            }, h, j, task["commuters_per_route"]))

        print("\nApplying strict real-world commuter counts to physical buildings...", end=" ", flush=True)

        for p in points:
            p.pop("is_airport", None)
            p.pop("grids", None)
            p.pop("connections", None)
            p["residents"], p["jobs"] = 0, 0

        for pop, h, j, raw_size in raw_pops:
            pop["size"] = raw_size
            pops.append(pop)
            h["popIds"].append(pop["id"])
            j["popIds"].append(pop["id"])
            h["residents"] += raw_size
            j["jobs"] += raw_size

        active_points = [p for p in points if p["residents"] > 0 or p["jobs"] > 0]

        final_pop = sum(p["residents"] for p in active_points)
        final_jobs = sum(p["jobs"] for p in active_points)

        print("OK")
        print(f"Final Real-World Values: {final_pop:,} residents | {final_jobs:,} jobs")
        print(f"  -> Normal Jobs: {sum(p['jobs'] for p in active_points if not p.get('is_special')):,} (in {sum(1 for p in active_points if not p.get('is_special') and p['jobs'] > 0):,} hubs)")
        print(f"  -> Special Jobs: {sum(p['jobs'] for p in active_points if p.get('is_special')):,} (in {sum(1 for p in active_points if p.get('is_special') and p['jobs'] > 0):,} hubs)")
        print(f"Total Game Entities: {final_pop + final_jobs:,}\n")

        os.makedirs(os.path.dirname(BASE_OUTPUT_FILE), exist_ok=True)

        print(f"Saving base demand to '{BASE_OUTPUT_FILE}'...", end=" ", flush=True)
        with open(BASE_OUTPUT_FILE, "w") as f:
            json.dump({"points": active_points, "pops": pops}, f, indent=2)
        print("OK")
        _mark_phase("Applying commuter counts + saving base demand")
    finally:
        stop_osrm_container()
        _mark_phase("OSRM container shutdown")

    total = sum(_phase_times.values())
    print("\n--- Phase timing summary (base stage) ---")
    for label, secs in _phase_times.items():
        print(f"  {secs:6.1f}s  ({secs/total*100:4.1f}%)  {label}")
    print(f"  {total:6.1f}s  (100.0%)  TOTAL\n")

    print("Base demand done!")
    return True


def enrich_demand(autofill_special_demand=None):
    """
    Enrichment stage of the modular pipeline: loads the checkpoint
    build_base_demand() saved to BASE_OUTPUT_FILE and runs everything after
    base routing - cluster/consolidate, named special demand, re-routing the
    new special-demand pops, and the final save to OUTPUT_FILE (the file the
    rest of the map tooling actually reads). Runs its own independent OSRM
    container lifecycle and re-detects special demand fresh (cheap once the
    per-city JSON is already populated - see special_demand_utils.py), so it
    never needs to touch OSM parsing, airport snapping, or base OSRM routing
    at all. Use this (via demand()) after only changing HUB_SIZE_RATIO or
    hand-editing a special-demand JSON, instead of re-running the full (much
    slower) base pipeline for no reason. Returns True on success, False if
    no base checkpoint exists yet to enrich.

    autofill_special_demand: see build_base_demand()'s docstring - same
    override/default behavior.
    """
    if autofill_special_demand is None:
        autofill_special_demand = _cli_args.autofill_special_demand

    if not os.path.exists(BASE_OUTPUT_FILE):
        print(f"\n[ERROR] '{BASE_OUTPUT_FILE}' not found. Run the full "
              f"pipeline first (`python generate_demand_qzm_local.py` or "
              f"all()) so there's a base checkpoint to enrich.")
        return False

    bbox = [BBOX_MIN_LON, BBOX_MIN_LAT, BBOX_MAX_LON, BBOX_MAX_LAT]
    cleaned_opl_path = f"{CITY_RAW_DIR}{OPL_CLEANED_FILE}"

    ready_special_demand = detect_and_confirm_special_demand(
        CITY_CODE, RAW_BASE_DIR, cleaned_opl_path, bbox,
        autofill_landmarks=autofill_special_demand,
    )

    green_polygons = _load_green_space_cache(GREEN_SPACE_CACHE_FILE)
    if not green_polygons:
        print(f"  [WARNING] No green-space polygon cache found at "
              f"'{GREEN_SPACE_CACHE_FILE}' - proceeding without green-space "
              f"placement checks. Run build_base_demand()/all() at least "
              f"once to generate it.")
    green_index = GreenSpaceIndex(green_polygons)

    _phase_times = {}
    _t_phase_start = time.time()
    def _mark_phase(label):
        nonlocal _t_phase_start
        now = time.time()
        _phase_times[label] = now - _t_phase_start
        _t_phase_start = now

    print("\nEnriching demand (cluster/consolidate + named special demand)...")
    # verb=False: depot's own add_points()/cluster_points()/etc. print
    # their own internal per-point chatter (e.g. a 2-line "Adding X demand
    # for Y" block per special demand point) - redundant with our own
    # summarized progress prints in cluster_and_consolidate()/
    # add_named_special_demand(), and unreadable at hundreds of points.
    # print_stats() is unconditional, so you still get the final numbers.
    demand = DemandData(BASE_OUTPUT_FILE, CITY_CODE, bbox=bbox, outputdir=CITY_RAW_DIR, verb=False)
    print(f"Loaded {len(demand['points']):,} points / {len(demand['pops']):,} pops.")

    try:
        start_osrm_container()
        _mark_phase("OSRM container startup (map data prep, cached after the first run)")

        print("\n[1/2] Cleaning up base demand (no external data used here)...")
        cluster_and_consolidate(demand, green_index=green_index,
                                agglomerate_small_threshold=HUB_AGGLOMERATE_SMALL_THRESHOLD,
                                agglomerate_distance_noncbd=HUB_AGGLOMERATE_DISTANCE_NONCBD,
                                agglomerate_distance_cbd=HUB_AGGLOMERATE_DISTANCE_CBD,
                                max_point_size=HUB_MAX_POINT_SIZE,
                                cluster_max_pop_threshold=HUB_CLUSTER_MAX_POP_THRESHOLD,
                                cluster_buffer_meters=HUB_CLUSTER_BUFFER_METERS)
        _mark_phase("cluster_and_consolidate (merge + agglomerate + cluster_points + caps)")

        if ready_special_demand:
            print(f"\n[2/2] Adding {len(ready_special_demand)} named special demand point(s)...")
            add_named_special_demand(demand, ready_special_demand)
            _mark_phase(f"add_named_special_demand ({len(ready_special_demand):,} landmarks)")

            print("\nCalculating routes for the newly added special demand pops via "
                  "OSRM...")
            route_new_pops(demand, OSRM_URL)
            _mark_phase("route_new_pops (landmark routes)")

            # add_points() (inside add_named_special_demand()) generates each
            # landmark's own commute pops by gravity-sampling FROM already-
            # existing nearby points - i.e. it adds residents/jobs directly
            # onto ordinary points that enforce_max_point_size() already
            # capped during cluster_and_consolidate() above. With 230+
            # landmarks each doing this, a handful of well-placed ordinary
            # points can get pushed back over the cap (verified on real BER
            # data: 434 points still over 2,000 after this, some by 1,000+).
            # Re-run the same cap here, now that nothing more will add to
            # these points.
            print("\nRe-capping point sizes inflated by newly added special demand...")
            enforce_max_point_size(demand, max_point_size=HUB_MAX_POINT_SIZE, green_index=green_index)
            _mark_phase("re-cap point sizes after landmarks")
        else:
            print("\n[2/2] No named special demand points with capacity data yet - skipping.")
    finally:
        stop_osrm_container()
        _mark_phase("OSRM container shutdown")

    demand.print_stats()
    demand.save(OUTPUT_FILE)
    print(f"\nSaved enriched demand to '{OUTPUT_FILE}'.")
    _mark_phase("print_stats + save")

    total = sum(_phase_times.values())
    print("\n--- Phase timing summary (enrichment stage) ---")
    for label, secs in _phase_times.items():
        print(f"  {secs:6.1f}s  ({secs/total*100:4.1f}%)  {label}")
    print(f"  {total:6.1f}s  (100.0%)  TOTAL\n")

    print("Done!")
    return True


def all(autofill_special_demand=None):
    """
    Runs the full pipeline end to end: build_base_demand() followed by
    enrich_demand() - equivalent to the old single-function build_demand(),
    just split into two independently-runnable/composable stages. Skips
    enrich_demand() if build_base_demand() hit an early-exit condition (no
    buildings found, missing commuter CSV, airport hub coordinates still
    pending manual input in custom_hubs.json).

    autofill_special_demand: see build_base_demand()'s docstring - forwarded
    to both stages so the same override applies throughout the whole run.
    """
    if not build_base_demand(autofill_special_demand=autofill_special_demand):
        return False
    return enrich_demand(autofill_special_demand=autofill_special_demand)


def demand(autofill_special_demand=None):
    """
    Re-runs ONLY the enrichment stage against the existing base checkpoint
    (BASE_OUTPUT_FILE) - use this after changing HUB_SIZE_RATIO, hand-
    editing a special-demand JSON, or tweaking enrich_utils.py, when the raw
    OSM/routing data hasn't changed and re-running the full (much slower)
    base pipeline would just reproduce the same base checkpoint.

    autofill_special_demand: see build_base_demand()'s docstring.
    """
    return enrich_demand(autofill_special_demand=autofill_special_demand)


# Stage selection (choosing all() vs demand()) lives in the wrapper script,
# run_demand_pipeline.py, not here - see its module comment. Running this
# file directly still does the full pipeline, same as before the modular
# split, for anything that already invokes it that way.
if __name__ == "__main__":
    all()