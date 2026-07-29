# CUSTOM DEPOT BUILD.PY

import os
from dotenv import load_dotenv
from depot.maps import MapGen

load_dotenv()

city_code = os.getenv("CITY_CODE")
if not city_code:
    raise ValueError("CITY_CODE is missing from your .env file!")
osmpbf_file = os.getenv("OSMPBF", "germany-latest.osm.pbf")
RAW_BASE_DIR = os.getenv("RAW_BASE_DIR", "raw_map_files")

# BBOX is no longer read from .env - it's looked up (or interactively captured
# and saved) per city code. See bbox_utils.py.
from bbox_utils import get_bbox
bbox = list(get_bbox(city_code, RAW_BASE_DIR))

map_builder = MapGen(
    city=city_code,
    bbox=bbox, 
    
    osmpbf=osmpbf_file, 
    outputdir=RAW_BASE_DIR,
    
    cities=['city', 'town', 'village', 'county'],      
    suburbs=['borough', 'suburb', 'islet', 'isolated_dwelling'],                                         
    neighborhoods=['neighbourhood', 'quarter', 'locality', 'square', 'hamlet', 'natural_region', 'farm'],     
    
    label_name_language="prefer:de",
    road_name_preferred_language="de",
    
    RAM=16,
    ncores=10,
    verb=True,
    redownload_buildings=True,
    create_building_foundations=True,
    cleanup_files=True,
    maxzoom=16
)

map_builder.run_all()