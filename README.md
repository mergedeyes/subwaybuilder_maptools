# Custom DEPOT Map Tools written in Python
---
## Setting up .env file
```
# Map Bounding Box [min_lon, min_lat, max_lon, max_lat]
BBOX = 8.396524,49.998003,9.032326,50.302657
# 3-DIGIT CITY CODE
CITY_CODE = "FRA"
CITY_NAME = "Frankfurt"
# OUTPUT FOR OPL
OPL = "fra_cleaned.opl"
# INPUT .ism.pbf
OSMPBF = "germany-latest.osm.pbf"
# COMMUTERS DATA: INSPIRE-Grid OD-MATRIX
CSV = "QZM_1X1KM.csv"
# OUTPUT DIRECTORY FOR ALL DEPOT-OUTPUT AND INPUT DIRECTORY FOR ALL SCRIPT-INPUT FILES
RAW_BASE_DIR = "raw_map_files"
# ZIP DESTINATION DIRECTORY
OUTPUT_DIR = "Map_ZIPs"
GITHUB_TOKEN = "<Github_Token_Classic>"
```
## Running the scripts
### Precautions
1. Download a .osm.pbf file you want to use to extract the mapdata from [Geofabrik](https://download.geofabrik.de/index.html).
2. Get your commuters data in the INSPIRE-Grid format. [Germany](https://mobilithek.info/offers/767359761906577408)
3. Get your initial map data with depot, running build.py
4. Prepare the data, running generate_demand_qzm_local.py
5. Set correct coordinates for airport points, opening Google Maps and entering the correct cords into ./raw_map_files/CITY_CODE/custom_hubs.json, running open_maps.py
6. Generate the demand data, running generate_demand_qzm_local.py again.
7. Create a config.json inside ./raw_map_files/CITY_CODE/ based on the [official documentation](https://www.subwaybuilder.com/docs/v1.0.0/api-reference/cities).
8. Run "ship.py none", to create the ZIP file. (none: No version change, major: Bumb major version, minor: Bump minor version, patch: Bump patch version inside config.json)
9. Import your ZIP into Railyard (locally) and test your map ingame.
10. (Optional) Make changes to the demand configuration, description, etc.
11. Publish your map to Github manually, OR:
12. Edit the parameters of publish.py and automatically publish your map to your Github-Repo, also generating a update.json for your map automatically.
