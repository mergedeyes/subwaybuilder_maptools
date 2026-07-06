import osmium
import json
import math
import random
import subprocess
import os
import csv
from pyproj import Transformer

# ==========================================
# CONFIGURATION
# ==========================================
RAW_PBF_FILE = "berlin.osm.pbf"
CLEANED_PBF_FILE = "berlin_cleaned.osm.pbf"
OUTPUT_FILE = "BER/demand_data.json"
PENDLER_CSV_FILE = "QZM_1X1KM.csv"

MIN_COMMUTER_THRESHOLD = 15  # Ignore commuter grids with less than MIN_COMMUTER_THRESHOLD
MAX_ROUTES_LIMIT = 100_000     # Maximum number of routes

RES_MERGE_RADIUS = 0.1          # 0.1 = 100m merge radius
JOB_MERGE_RADIUS = 0.2          # 0.2 = 200m merge radius

# (min, max)
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

JOB_INDICATOR_KEYS = {'amenity', 'shop', 'office'}

def prepare_cleaned_pbf(raw_file, cleaned_file):
    if os.path.exists(cleaned_file):
        print(f"Cleaned PBF '{cleaned_file}' already exists. Skipping filtering.")
        return True

    if not os.path.exists(raw_file):
        print(f"Error: Raw PBF file '{raw_file}' not found!")
        return False

    print(f"Cleaned PBF not found. Filtering '{raw_file}' now (this may take a minute)...", end=" ", flush=True)
    
    command = [
        "osmium", "tags-filter", raw_file,
        "nwr/building=apartments,house,detached,semidetached_house,residential,terrace,bungalow,hotel,commercial,industrial,retail,office,school,hospital,university,civic,government,yes",
        "nwr/amenity",
        "nwr/shop",
        "nwr/office",
        "-o", cleaned_file,
        "--overwrite"
    ]
    
    try:
        subprocess.run(command, check=True)
        print("OK")
        print(f"Successfully created optimized file: '{cleaned_file}'")
        return True
    except FileNotFoundError:
        print("CRITICAL ERROR")
        print("\n[CRITICAL ERROR] 'osmium' command line tool is not installed or not in PATH.")
        return False
    except subprocess.CalledProcessError as e:
        print("ERROR")
        print(f"\n[ERROR] Osmium filtering failed: {e}")
        return False

class OSMHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.raw_job_nodes = []
        self.raw_home_nodes = []
        
        self.min_lat, self.max_lat = float('inf'), float('-inf')
        self.min_lon, self.max_lon = float('inf'), float('-inf')

    def process_element(self, elem_id, tags, lat, lon):
        if tags.get('floating') == 'yes' or tags.get('location') in ['water', 'underwater']:
            return
            
        b_type = tags.get('building') or ''
        if b_type in ['houseboat', 'boathouse', 'floating_home']:
            return

        is_job = (b_type in JOB_TAGS) or any(tags.get(key) for key in JOB_INDICATOR_KEYS)
        
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
                "capacity": random.randint(*CAPACITY_JOBS)
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
        if len(n.tags) == 0:
            return
        self.process_element(n.id, n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if len(w.tags) == 0:
            return
        try:
            self.process_element(w.id, w.tags, w.nodes[0].location.lat, w.nodes[0].location.lon)
        except osmium.InvalidLocationError:
            pass

def build_demand():
    success = prepare_cleaned_pbf(RAW_PBF_FILE, CLEANED_PBF_FILE)
    if not success:
        return 

    print(f"\nLoading '{CLEANED_PBF_FILE}'...", end=" ", flush=True)
    handler = OSMHandler()
    handler.apply_file(CLEANED_PBF_FILE, locations=True)
    print("OK")

    avg_lat_rad = math.radians((handler.min_lat + handler.max_lat) / 2.0)
    height_km = (handler.max_lat - handler.min_lat) * 111.0
    width_km = (handler.max_lon - handler.min_lon) * 111.0 * math.cos(avg_lat_rad)
    
    map_area_sqkm = max(1.0, width_km * height_km)
    
    print(f"\nMap Bounds Detected: {width_km:.1f}km x {height_km:.1f}km")
    print(f"Total Area: {map_area_sqkm:.1f} sq/km")

    def cluster_organic(nodes, radius_km):
        radius_deg = radius_km / 111.0
        bucket_size = radius_deg * 2 
        buckets = {}
        
        for n in nodes:
            bx = int(n["lat"] / bucket_size)
            by = int(n["lon"] / bucket_size)
            key = (bx, by)
            if key not in buckets: buckets[key] = []
            buckets[key].append(n)
            
        final_clusters = []
        processed = set()
        
        for n in nodes:
            if id(n) in processed: continue
            
            bx = int(n["lat"] / bucket_size)
            by = int(n["lon"] / bucket_size)
            
            cluster = {"lat": n["lat"], "lon": n["lon"], "capacity": n["capacity"]}
            processed.add(id(n))
            
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    b_key = (bx + dx, by + dy)
                    if b_key not in buckets: continue
                    
                    for neighbor in buckets[b_key]:
                        if id(neighbor) in processed: continue
                        
                        dist = math.sqrt((cluster["lat"] - neighbor["lat"])**2 + 
                                         (cluster["lon"] - neighbor["lon"])**2)
                        
                        if dist < radius_deg:
                            cluster["capacity"] += neighbor["capacity"]
                            processed.add(id(neighbor))
            
            final_clusters.append({
                "id": f"cluster_{len(final_clusters)}",
                "lat": cluster["lat"],
                "lon": cluster["lon"],
                "capacity": cluster["capacity"]
            })
            
        return final_clusters

    print(f"Clustering {len(handler.raw_home_nodes):,} residential buildings organically...", end=" ", flush=True)
    final_home_nodes = cluster_organic(handler.raw_home_nodes, RES_MERGE_RADIUS)
    print("OK")
    print(f"Result: {len(final_home_nodes):,} organic residential hubs.")

    print(f"Clustering {len(handler.raw_job_nodes):,} job buildings organically...", end=" ", flush=True)
    final_job_nodes = cluster_organic(handler.raw_job_nodes, JOB_MERGE_RADIUS)
    print("OK")
    print(f"Result: {len(final_job_nodes):,} organic job hubs.")
    
    points, pops = [], []
    
    print("Generating demand points...")
    for i, item in enumerate(final_job_nodes):
        points.append({
            "id": f"dp_job_{i}", 
            "location": [item["lon"], item["lat"]], 
            "jobs": item["capacity"], 
            "residents": 0, 
            "popIds": []
        })
    
    for item in final_home_nodes:
        points.append({
            "id": item["id"], 
            "location": [item["lon"], item["lat"]], 
            "jobs": 0, 
            "residents": item["capacity"], 
            "popIds": []
        })

    home_list = [p for p in points if p["residents"] > 0]
    job_list = [p for p in points if p["jobs"] > 0]

    if not home_list or not job_list:
        print("Error: Not enough OSM data found.")
        return

    print("\nInitializing EPSG:3035 Transformer...", end=" ", flush=True)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    print("OK")

    print("Mapping OSM buildings to the INSPIRE 1x1km Grid...", end=" ", flush=True)
    grid_homes = {}
    grid_jobs = {}

    for h in home_list:
        x_meters, y_meters = transformer.transform(h["location"][0], h["location"][1])
        grid_id = f"1kmN{int(y_meters // 1000)}E{int(x_meters // 1000)}"
        
        if grid_id not in grid_homes: grid_homes[grid_id] = []
        grid_homes[grid_id].append(h)

    for j in job_list:
        x_meters, y_meters = transformer.transform(j["location"][0], j["location"][1])
        grid_id = f"1kmN{int(y_meters // 1000)}E{int(x_meters // 1000)}"
        
        if grid_id not in grid_jobs: grid_jobs[grid_id] = []
        grid_jobs[grid_id].append(j)

    print("OK")
    print(f"Mapped {len(grid_homes)} residential grids and {len(grid_jobs)} job grids.")
    
    raw_pops = []
    
    print(f"\nParsing real-world commuter matrix from {PENDLER_CSV_FILE}...", end=" ", flush=True)
    
    try:
        with open(PENDLER_CSV_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
            
            wo_idx = headers.index("wo_1km")
            ao_idx = headers.index("ao_1km")
            pendler_idx = headers.index("gesamtpendler")

            for row in reader:
                if len(row) < 3: continue
                    
                count = int(row[pendler_idx])
                
                if count < MIN_COMMUTER_THRESHOLD: continue 
                if len(raw_pops) >= MAX_ROUTES_LIMIT: break

                wo = row[wo_idx]
                ao = row[ao_idx]
                
                if wo in grid_homes and ao in grid_jobs:
                    homes_in_cell = grid_homes[wo]
                    jobs_in_cell = grid_jobs[ao]
                    
                    if not homes_in_cell or not jobs_in_cell: continue

                    num_connections = min(3, len(homes_in_cell), len(jobs_in_cell))
                    if num_connections == 0: continue
                    
                    commuters_per_route = count // num_connections
                    if commuters_per_route < 5: continue
                    
                    chosen_homes = random.sample(homes_in_cell, num_connections)
                    chosen_jobs = random.sample(jobs_in_cell, num_connections)

                    for h, j in zip(chosen_homes, chosen_jobs):
                        dist = math.hypot(h["location"][0] - j["location"][0], h["location"][1] - j["location"][1]) * 111000
                        
                        pop_id = f"pop_{len(raw_pops):03d}"
                        raw_pops.append(({
                            "id": pop_id, "residenceId": h["id"], "jobId": j["id"],
                            "drivingSeconds": int(dist / 8.3), "drivingDistance": int(dist)
                        }, h, j, commuters_per_route))

        print("OK")
        print(f"Successfully generated {len(raw_pops):,} routes!")
    except FileNotFoundError:
        print("ERROR")
        print(f"\n[ERROR] '{PENDLER_CSV_FILE}' not found. Please place it in the same directory.")
        return

    print("\nApplying strict real-world commuter counts to physical buildings...", end=" ", flush=True)
#
    for p in points:
        p["residents"] = 0
        p["jobs"] = 0

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
    print(f"Total Game Entities: {final_pop + final_jobs:,}\n")
    
    print(f"Saving to '{OUTPUT_FILE}'...", end=" ", flush=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({"points": active_points, "pops": pops}, f, indent=2)
        
    print("Done!")

if __name__ == "__main__":
    build_demand()