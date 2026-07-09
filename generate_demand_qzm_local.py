### ==== SCRIPT TO GENERATE REALISTIC DEMAND BASED ON THE OSM BUILDINGS DATA AND CENSUS DATA

import osmium
import json
import math
import random
import subprocess
import os
import sys
import csv
from pyproj import Transformer
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# 1. Load the environment variables first
load_dotenv()

# 2. Get the city code and BBOX with strict fallbacks
CITY_CODE = os.getenv("CITY_CODE")
if not CITY_CODE:
    raise ValueError("CITY_CODE is missing from your .env file!")

BBOX_ENV = os.getenv("BBOX")
if not BBOX_ENV:
    raise ValueError("BBOX not found in .env file")
BBOX_MIN_LON, BBOX_MIN_LAT, BBOX_MAX_LON, BBOX_MAX_LAT = [float(coord.strip()) for coord in BBOX_ENV.split(',')]

OSMPBF_FILE = os.getenv("OSMPBF")
if not OSMPBF_FILE:
    raise ValueError("OSMPBF is missing from your .env file!")

OPL_CLEANED_FILE = os.getenv("OPL")
if not OSMPBF_FILE:
    raise ValueError("OPL is missing from your .env file!")

COMMUTERS_CSV_FILE = os.getenv("CSV")
if not COMMUTERS_CSV_FILE:
    raise ValueError("CSV is missing from your .env file!")

RAW_BASE_DIR = os.getenv("RAW_BASE_DIR")
if not RAW_BASE_DIR:
    raise ValueError("RAW_BASE_DIR is missing from your .env file!")

# ==========================================
# CONFIGURATION
# ==========================================
OUTPUT_FILE = f"RAW_BASE_DIR/{CITY_CODE}/demand_data.json"
AIRPORT_GEOJSON = f"RAW_BASE_DIR/{CITY_CODE}/runways_taxiways.geojson"
CUSTOM_HUBS_JSON = f"RAW_BASE_DIR/{CITY_CODE}/custom_hubs.json"

MAX_HUBS_PER_GRID = 100     # Increase this to get more bubbles per 1x1km cell
MIN_ROUTE_SIZE = 10         # Minimum commuters per line
MIN_COMMUTER_THRESHOLD = 10 # All O/D data less than this threshold get dropped

MAX_ROUTES_LIMIT = 200_000     

RES_MERGE_RADIUS = 0.2          
JOB_MERGE_RADIUS = 0.3
AIRPORT_EDGE_SNAP_RADIUS_METERS = 1000

# Ratio of commuters heading to 'special' buildings (0.2 = 20%)
SPECIAL_DEMAND_SPLIT = 0.2

CAPACITY_JOBS = (500, 3000)
CAPACITY_APARTMENTS = (50, 500)
CAPACITY_HOUSES = (5, 20)
CAPACITY_YES_WILDCARD = (5, 15)

JOB_TAGS = {
    'commercial', 'industrial', 'retail', 'office',
    'school', 'hospital', 'university', 'civic', 'government'
}

RESIDENTIAL_TAGS = {
    'house', 'detached', 'semidetached_house', 'residential', 'terrace', 'bungalow', 'hotel'
}

# New sets specifically targeting keys rather than just the 'building' tag
SPECIAL_AMENITIES = {'hospital', 'clinic', 'school', 'university', 'college', 'ferry_terminal'}
SPECIAL_LEISURE = {'park', 'nature_reserve', 'stadium', 'sports_centre', 'water_park'}
SPECIAL_TOURISM = {'theme_park', 'zoo', 'aquarium'}

JOB_INDICATOR_KEYS = {'amenity', 'shop', 'office'}

def prepare_cleaned_pbf(raw_file, cleaned_file):
    # We now target an OPL file for the final high-speed read
    opl_file = cleaned_file.replace(".osm.pbf", ".opl")
    
    if os.path.exists(opl_file):
        print(f"Optimized OPL file '{opl_file}' exists. Skipping preparation.")
        return True

    print(f"Preparing map data (Extract -> Filter -> Convert)...")
    
    # 1. Extract BBOX to reduce file size significantly
    bbox_str = f"{BBOX_MIN_LON},{BBOX_MIN_LAT},{BBOX_MAX_LON},{BBOX_MAX_LAT}"
    extract_file = "city_extract.osm.pbf"
    subprocess.run(["osmium", "extract", "-b", bbox_str, raw_file, "-o", extract_file, "--overwrite"], check=True)

    # 2. Filter tags on the extracted file
    temp_filtered = "city_filtered.osm.pbf"
    tags_filter = [
        "nwr/building=*", "nwr/amenity=*", "nwr/shop=*", "nwr/office=*",
        "nwr/leisure=park,nature_reserve,stadium,sports_centre,water_park",
        "nwr/tourism=theme_park,zoo,aquarium",
        "nwr/boundary=national_park"
    ]
    subprocess.run(["osmium", "tags-filter", extract_file] + tags_filter + ["-o", temp_filtered, "--overwrite"], check=True)

    # 3. Convert to OPL for instant Python loading
    print(f"Converting to OPL format for high-speed parsing...")
    subprocess.run(["osmium", "cat", temp_filtered, "-o", opl_file, "--overwrite"], check=True)
    
    # Cleanup intermediate PBF files to save space
    os.remove(extract_file)
    os.remove(temp_filtered)
    
    print("Map preparation complete.")
    return True

class OSMHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.raw_job_nodes = []
        self.raw_home_nodes = []
        self.min_lat, self.max_lat = float('inf'), float('-inf')
        self.min_lon, self.max_lon = float('inf'), float('-inf')

    def process_element(self, elem_id, tags, lat, lon):
        if not (BBOX_MIN_LAT <= lat <= BBOX_MAX_LAT and BBOX_MIN_LON <= lon <= BBOX_MAX_LON):
            return
            
        if tags.get('floating') == 'yes' or tags.get('location') in ['water', 'underwater']:
            return
            
        b_type = tags.get('building') or ''
        if b_type in ['houseboat', 'boathouse', 'floating_home']:
            return

        # Check standard building tags or our newly allowed special demand keys
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

        if lat < self.min_lat: self.min_lat = lat
        if lat > self.max_lat: self.max_lat = lat
        if lon < self.min_lon: self.min_lon = lon
        if lon > self.max_lon: self.max_lon = lon
        
        if is_job:
            self.raw_job_nodes.append({
                "id": elem_id, "lat": lat, "lon": lon, 
                "capacity": random.randint(*CAPACITY_JOBS),
                "is_special": is_special
            })
        elif is_home:
            if b_type == 'apartments':
                cap = random.randint(*CAPACITY_APARTMENTS)
            elif b_type == 'yes':
                cap = random.randint(*CAPACITY_YES_WILDCARD)
            else:
                cap = random.randint(*CAPACITY_HOUSES)
                
            self.raw_home_nodes.append({"lat": lat, "lon": lon, "capacity": cap})

    def node(self, n):
        if len(n.tags) > 0: self.process_element(n.id, n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if len(w.tags) > 0:
            try:
                self.process_element(w.id, w.tags, w.nodes[0].location.lat, w.nodes[0].location.lon)
            except osmium.InvalidLocationError: pass

def cluster_organic(nodes, radius_km):
    radius_deg = radius_km / 111.0
    bucket_size = radius_deg * 2 
    buckets = {}
    for n in nodes:
        bx, by = int(n["lat"] / bucket_size), int(n["lon"] / bucket_size)
        key = (bx, by)
        if key not in buckets: buckets[key] = []
        buckets[key].append(n)
        
    final_clusters, processed = [], set()
    for n in nodes:
        if id(n) in processed: continue
        bx, by = int(n["lat"] / bucket_size), int(n["lon"] / bucket_size)
        
        # Initialize cluster with the root node's special status
        cluster = {
            "lat": n["lat"], "lon": n["lon"], 
            "capacity": n["capacity"], 
            "is_special": n.get("is_special", False)
        }
        processed.add(id(n))
        
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                b_key = (bx + dx, by + dy)
                if b_key not in buckets: continue
                for neighbor in buckets[b_key]:
                    if id(neighbor) in processed: continue
                    dist = math.sqrt((cluster["lat"] - neighbor["lat"])**2 + (cluster["lon"] - neighbor["lon"])**2)
                    if dist < radius_deg:
                        cluster["capacity"] += neighbor["capacity"]
                        # If a neighbor is special, the whole cluster becomes a special destination
                        if neighbor.get("is_special"):
                            cluster["is_special"] = True
                        processed.add(id(neighbor))
                        
        final_clusters.append({
            "id": f"cluster_{len(final_clusters)}", 
            "lat": cluster["lat"], "lon": cluster["lon"], 
            "capacity": cluster["capacity"],
            "is_special": cluster["is_special"]
        })
    return final_clusters

def build_demand():
    if not prepare_cleaned_pbf(OSMPBF_FILE, OPL_CLEANED_FILE):
        return 

    print(f"\nLoading '{OPL_CLEANED_FILE}'...", end=" ", flush=True)
    handler = OSMHandler()
    handler.apply_file(OPL_CLEANED_FILE, locations=True)
    print("OK")

    if not handler.raw_home_nodes and not handler.raw_job_nodes:
        print("\n[ERROR] No buildings found within the specified bounding box.")
        return

    avg_lat_rad = math.radians((handler.min_lat + handler.max_lat) / 2.0)
    height_km = (handler.max_lat - handler.min_lat) * 111.0
    width_km = (handler.max_lon - handler.min_lon) * 111.0 * math.cos(avg_lat_rad)
    map_area_sqkm = max(1.0, width_km * height_km)
    
    print(f"\nMap Bounds Detected: {width_km:.1f}km x {height_km:.1f}km")
    print(f"Total Area: {map_area_sqkm:.1f} sq/km")

    print(f"Clustering {len(handler.raw_home_nodes):,} residential and {len(handler.raw_job_nodes):,} job buildings in parallel...", end=" ", flush=True)
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_homes = executor.submit(cluster_organic, handler.raw_home_nodes, RES_MERGE_RADIUS)
        future_jobs = executor.submit(cluster_organic, handler.raw_job_nodes, JOB_MERGE_RADIUS)
        
        final_home_nodes = future_homes.result()
        final_job_nodes = future_jobs.result()
        
    print("OK")
    print(f"Result: {len(final_home_nodes):,} organic residential hubs.")
    print(f"Result: {len(final_job_nodes):,} organic job hubs.")

    print("\nGenerating demand points...")

    print("Initializing EPSG:3035 Transformer...", end=" ", flush=True)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    print("OK")

    # =========================================================
    # REVISED: INTERACTIVE MULTI-AIRPORT CONFIG & SNAPPING
    # =========================================================
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
                    
            # Merge detected airports with config file
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
                    
                print(f"\n\n[ACTION REQUIRED] Airport hubs detected but need manual terminal coordinate overrides.")
                print(f" -> A configuration file has been generated at: {CUSTOM_HUBS_JSON}")
                print(f" -> Please open the file, replace 'null' with the exact lat/lon for the hubs, and run this script again.")
                sys.exit(0)
                
            print("OK (Custom coordinates loaded)")
            
            # --- SNAPPING & HUB CREATION ---
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
                        "lat": olat,      # Injecting the manual coords
                        "lon": olon,      # Injecting the manual coords
                        "capacity": ap["capacity"],
                        "is_airport": True,
                        "grids": ap["grids"]
                    })
                    active_airports_count += 1
                    
            final_job_nodes = surviving_job_nodes
            print(f"  -> Generated {active_airports_count} mapped Airport Mega-Hubs.")
    else:
        print(f"Airport GeoJSON '{AIRPORT_GEOJSON}' not found. Skipping airport snapping.")

    # =========================================================

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
                "is_special": item.get("is_special", False) # Airports can be forced to special later
            })
        else:
            points.append({
                "id": f"dp_job_{i}", 
                "location": [item["lon"], item["lat"]], 
                "jobs": item["capacity"], 
                "residents": 0, 
                "popIds": [],
                "is_special": item.get("is_special", False) # <--- THIS WAS MISSING
            })
    
    for item in final_home_nodes:
        points.append({
            "id": item["id"], 
            "location": [item["lon"], item["lat"]], 
            "jobs": 0, 
            "residents": item["capacity"], 
            "popIds": [],
            "is_special": False # <--- Ensure homes default to False
        })

    home_list = [p for p in points if p["residents"] > 0]
    job_list = [p for p in points if p["jobs"] > 0]

    print("Mapping OSM buildings to the INSPIRE 1x1km Grid...", end=" ", flush=True)
    grid_homes, grid_jobs = {}, {}

    for h in [p for p in points if p["residents"] > 0]:
        x, y = transformer.transform(h["location"][0], h["location"][1])
        gid = f"1kmN{int(y // 1000)}E{int(x // 1000)}"
        grid_homes.setdefault(gid, []).append(h)
        
    for j in [p for p in points if p["jobs"] > 0]:
        is_special = j.get("is_special", False)
        # Treat airports as special destinations to guarantee they get 50% of incoming grid demand
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

    print("OK")
    print(f"Mapped {len(grid_homes)} residential grids and {len(grid_jobs)} job grids.")
    
    raw_pops = []
    print(f"\nParsing real-world commuter matrix from {COMMUTERS_CSV_FILE}...", end=" ", flush=True)
    
    try:
        with open(COMMUTERS_CSV_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
            wo_idx, ao_idx, pendler_idx = headers.index("wo_1km"), headers.index("ao_1km"), headers.index("gesamtpendler")

            # DEFINE THE FUNCTION ONCE HERE, OUTSIDE THE LOOP
            def assign_commuters(target_jobs, target_count, current_homes):
                if target_count == 0 or not target_jobs: return
                max_affordable = target_count // MIN_ROUTE_SIZE
                
                num_conn = min(MAX_HUBS_PER_GRID, len(current_homes), len(target_jobs), max_affordable)
                
                if num_conn == 0: return
                commuters_per_route = target_count // num_conn
                if commuters_per_route < MIN_ROUTE_SIZE: return
                
                homes_sorted = sorted(current_homes, key=lambda x: x.get("residents", 0), reverse=True)
                jobs_sorted = sorted(target_jobs, key=lambda x: x.get("jobs", 0), reverse=True)

                for h, j in zip(homes_sorted[:num_conn], jobs_sorted[:num_conn]):
                    dist = math.hypot(h["location"][0] - j["location"][0], h["location"][1] - j["location"][1]) * 111000
                    pop_id = f"pop_{len(raw_pops):03d}"
                    raw_pops.append(({
                        "id": pop_id, "residenceId": h["id"], "jobId": j["id"],
                        "drivingSeconds": int(dist / 8.3), "drivingDistance": int(dist)
                    }, h, j, commuters_per_route))

            # START THE LOOP
            for row in reader:
                if len(row) < 3: continue
                count = int(row[pendler_idx])
                if count < MIN_COMMUTER_THRESHOLD: continue 
                if len(raw_pops) >= MAX_ROUTES_LIMIT: break

                wo, ao = row[wo_idx], row[ao_idx]
                if wo in grid_homes and ao in grid_jobs:
                    homes_in_cell = grid_homes[wo]
                    jobs_normal = grid_jobs[ao]["normal"]
                    jobs_special = grid_jobs[ao]["special"]
                    
                    if not homes_in_cell: continue
                    if not jobs_normal and not jobs_special: continue

                    # Calculate 50/50 split based on availability
                    if jobs_special and jobs_normal:
                        special_count = int(count * SPECIAL_DEMAND_SPLIT)
                        normal_count = count - special_count
                    elif jobs_special:
                        special_count = count
                        normal_count = 0
                    else:
                        special_count = 0
                        normal_count = count

                    # Fire routing logic for both pools
                    assign_commuters(jobs_special, special_count, homes_in_cell)
                    assign_commuters(jobs_normal, normal_count, homes_in_cell)

        print("OK")
        print(f"Successfully generated {len(raw_pops):,} routes!")
    except FileNotFoundError:
        print("ERROR")
        print(f"\n[ERROR] '{COMMUTERS_CSV_FILE}' not found. Please place it in the root-directory of this script.")
        return

    print("\nApplying strict real-world commuter counts to physical buildings...", end=" ", flush=True)

    for p in points:
        p.pop("is_airport", None)
        p.pop("grids", None)
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
    
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    print(f"Saving to '{OUTPUT_FILE}'...", end=" ", flush=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({"points": active_points, "pops": pops}, f, indent=2)
        
    print("Done!")

if __name__ == "__main__":
    build_demand()