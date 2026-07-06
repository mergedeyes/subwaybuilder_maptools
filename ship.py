import argparse
import zipfile
import json
import sys
from pathlib import Path

def validate_config(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except json.JSONDecodeError:
        print(f"  [ERROR] 'config.json' is not valid JSON.")
        sys.exit(1)

    required_keys = [
        "name", "code", "description", "population", 
        "initialViewState", "creator", "version"
    ]
    
    for key in required_keys:
        if key not in config:
            print(f"  [ERROR] Missing required field '{key}' in config.json. Cancelling.")
            sys.exit(1)

    view_state_keys = ["zoom", "latitude", "longitude", "bearing"]
    
    for key in view_state_keys:
        if key not in config["initialViewState"]:
            print(f"  [ERROR] Missing required field '{key}' inside 'initialViewState' in config.json. Cancelling.")
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Strictly package city map data into a ZIP.")
    parser.add_argument("city_code", help="The city code (e.g., ber). Will be converted to UPPERCASE.")
    parser.add_argument("--source", default=None, help="Source folder (defaults to the city_code).")
    args = parser.parse_args()

    city_code = args.city_code.upper()
    
    if args.source is None:
        source_dir = Path(city_code)
    else:
        source_dir = Path(args.source)

    required_files = [
        f"{city_code}.pmtiles",
        "buildings_index.json",
        "config.json",
        "demand_data.json",
        "roads.geojson",
        "runways_taxiways.geojson"
    ]

    print(f"Checking for required files in: {source_dir.absolute()}")
    
    for filename in required_files:
        file_path = source_dir / filename
        if not file_path.is_file():
            print(f"  [ERROR] Required file '{filename}' is missing in {source_dir.absolute()}. Cancelling.")
            sys.exit(1)

    print("Validating config.json...", end=" ", flush=True)
    config_path = source_dir / "config.json"
    validate_config(config_path)
    print("OK")

    dest_dir = Path("Map_ZIPs")
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_filepath = dest_dir / f"{city_code}.zip"

    print(f"All checks passed. Creating archive: {zip_filepath}...", end=" ", flush=True)

    with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for filename in required_files:
            file_path = source_dir / filename
            zipf.write(file_path, arcname=filename)

    print("OK")

    print(f"\nSuccess! ZIP saved to: {zip_filepath.absolute()}")

if __name__ == "__main__":
    main()