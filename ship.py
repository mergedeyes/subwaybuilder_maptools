import argparse
import zipfile
import json
import sys
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

city_code = os.getenv("CITY_CODE").upper()
name = os.getenv("CITY_NAME")
id = os.getenv("CITY_NAME").lower()
source_dir = Path(f"{os.getenv("RAW_BASE_DIR")}/{city_code}")
dest_dir = Path(f"{os.getenv("OUTPUT_DIR")}/{city_code}")
dest_dir.mkdir(parents=True, exist_ok=True)

required_files = [
    f"{city_code}.pmtiles", f"{city_code}_foundations.pmtiles", "config.json", "demand_data.json", 
    "roads.geojson", "runways_taxiways.geojson", 
    "buildings_index.bin", "ocean_foundations.geojson"
]

def bump_version(version_str, release_type):
    """Bumps version based on release_type: major, minor, patch, or none."""
    if release_type == 'none':
        return version_str
        
    parts = version_str.split('.')
    if len(parts) != 3: return version_str
    try:
        major, minor, patch = map(int, parts)
        if release_type == 'major': major += 1; minor = 0; patch = 0
        elif release_type == 'minor': minor += 1; patch = 0
        else: patch += 1 # patch
        return f"{major}.{minor}.{patch}"
    except ValueError: return version_str

def validate_config(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except json.JSONDecodeError:
        print(f"  [ERROR] 'config.json' is not valid JSON.")
        sys.exit(1)

    required = ["name", "code", "description", "population", "initialViewState", "creator", "version"]
    for key in required:
        if key not in config:
            print(f"  [ERROR] Missing required field '{key}' in config.json.")
            sys.exit(1)
    return config

def main():
    parser = argparse.ArgumentParser(description="Package city map data into a ZIP with a manifest.")
    #parser.add_argument("--source", default=None, help="Source folder.")
    parser.add_argument("release", choices=['major', 'minor', 'patch', 'none'], default='none', help="Release type.")
    #parser.add_argument("name", help="Name for manifest.json.")
    #parser.add_argument("id", help="Base ID for manifest.json.")
    #parser.add_argument("city_code", default=None, help="The city code (e.g., STU).")
    args = parser.parse_args()

    # --- 1. Validate Files ---
    for filename in required_files:
        if not (source_dir / filename).is_file():
            print(f"  [ERROR] Required file '{filename}' is missing.")
            sys.exit(1)

    # --- 2. Update config.json (Version Bump) ---
    config_path = source_dir / "config.json"
    config = validate_config(config_path)
    old_version = config.get("version", "1.0.0")
    new_version = bump_version(old_version, args.release)
    config["version"] = new_version
    
    # Update src directly
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print(f"Version: {old_version} -> {new_version}")

    # --- 3. Create manifest.json (in dest) --- 
    # DEACTIVATED BC WE USE A SCRIPT TO UPLOAD TO GITHUB DIRECTLY
    # AND USE THE UPDATE.JSON FOR MAP UPDATES FOR THE REGISTRY
    #manifest_path = dest_dir / "manifest.json"
    
    #if not manifest_path.exists():
    #    manifest = {
    #        "name": args.name,
    #        "id": f"{args.id}-mergedeyes",
    #        "dependencies": {"subway-builder": ">=1.4.5"}
    #    }
    #    with open(manifest_path, 'w', encoding='utf-8') as f:
    #        json.dump(manifest, f, indent=4, ensure_ascii=False)
    #    print(f"Created new manifest.json with ID: {manifest['id']}")
    #else:
    #    print(f"Manifest already exists at {manifest_path}, skipping creation.")

    # --- 4. Create ZIP ---
    zip_filepath = dest_dir / f"{city_code}.zip"
    files_to_zip = required_files
    
    print(f"Creating archive: {zip_filepath}...")
    with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for filename in files_to_zip:
            zipf.write(source_dir / filename, arcname=filename)

    print(f"\nSuccess! ZIP saved to: {zip_filepath.absolute()}")

if __name__ == "__main__":
    main()