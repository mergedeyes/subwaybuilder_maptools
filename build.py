import os
from dotenv import load_dotenv
from depot.maps import MapGen

load_dotenv()

# Parse the bounding box from the .env file
bbox_env = os.getenv("BBOX")
if not bbox_env:
    raise ValueError("BBOX not found in .env file")
bbox = [float(coord.strip()) for coord in bbox_env.split(',')]
city_code = os.getenv("CITY_CODE")
if not city_code:
    raise ValueError("CITY_CODE is missing from your .env file!")
osmpbf_file = os.getenv("OSMPBF")
if not osmpbf_file:
    raise ValueError("OSMPBF is missing from your .env file!")

map_builder = MapGen(
    city=city_code,
    bbox=bbox, 
    
    osmpbf=osmpbf_file, 
    outputdir="raw_map_files",
    
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