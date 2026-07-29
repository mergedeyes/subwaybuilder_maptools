### ==== SHARED HELPER: PER-CITY MAP DATA (BBOX + SPECIAL DEMAND) JSON ====
#
# Bounding boxes are no longer read from .env - only CITY_CODE and RAW_BASE_DIR
# are needed. Each city has a persistent
#   {RAW_BASE_DIR}/{CITY_CODE}/{CITY_CODE}_MAP_DATA.json
# (named for what it actually holds now, not just the bbox) file, shaped like:
#   {
#     "bbox": [min_lon, min_lat, max_lon, max_lat],
#     "special_demand": [
#       {"name": ..., "type": ..., "location": [lon, lat], "total_capacity": ...},
#       ...
#     ]
#   }
#
# If no bbox is saved yet, get_bbox() either uses a CLI-supplied value or
# prompts you to paste one in (e.g. copied straight from
# boundingbox.klokantech.com as "min_lon,min_lat,max_lon,max_lat") and saves
# it, so you never have to re-enter or re-guess it for that city again.
#
# special_demand_utils.py reads/writes the "special_demand" list in this same
# file via load_city_json()/save_city_json() below.

import os
import json


def bbox_file_path(city_code, raw_base_dir):
    return os.path.join(raw_base_dir, city_code, f"{city_code}_MAP_DATA.json")


def load_city_json(city_code, raw_base_dir):
    """
    Returns the full per-city JSON dict (bbox + special_demand), or a fresh
    skeleton if the file doesn't exist yet. Always guarantees both keys are
    present, for files written before "special_demand" existed.
    """
    path = bbox_file_path(city_code, raw_base_dir)
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("bbox", None)
    data.setdefault("special_demand", [])
    # Which special-demand type labels the OSM scan has already covered for
    # this city - lets detect_and_confirm_special_demand() tell "nothing new
    # to scan for" apart from "a type was just added to TYPE_TAG_RULES and
    # this city has never been scanned for it", without needing to guess from
    # the contents of "special_demand" alone (a city can legitimately have
    # zero of some type). Missing entirely on older files - treated as "not
    # scanned for anything yet", which triggers exactly one full re-scan next
    # run to catch up, merging into (not replacing) whatever's already there.
    data.setdefault("scanned_types", [])
    return data


def save_city_json(city_code, raw_base_dir, data):
    path = bbox_file_path(city_code, raw_base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def get_bbox(city_code, raw_base_dir, bbox_arg=None):
    """
    Returns (min_lon, min_lat, max_lon, max_lat) for the given city code.

    Resolution order:
      1. Already saved in {raw_base_dir}/{city_code}/{city_code}_MAP_DATA.json
         - always wins if present, bbox_arg is ignored (and a note is printed).
      2. bbox_arg, if provided (e.g. from a --bbox CLI flag) - saved for next time.
      3. Interactive prompt (paste straight from boundingbox.klokantech.com,
         e.g. "5.9559,45.818,10.4921,47.8084") - saved for next time.
    """
    data = load_city_json(city_code, raw_base_dir)
    path = bbox_file_path(city_code, raw_base_dir)

    if data["bbox"] is not None:
        bbox = tuple(float(v) for v in data["bbox"])
        if bbox_arg:
            print(f"Note: --bbox was given but '{path}' already has a saved "
                  f"bbox - using the saved one. Delete that file (or its "
                  f"\"bbox\" key) if you want to replace it.")
        print(f"Using saved bbox for {city_code}: {bbox} (from '{path}')")
        return bbox

    if bbox_arg:
        raw = bbox_arg
        source = "--bbox argument"
    else:
        print(f"\nNo bounding box saved for '{city_code}' yet.")
        print("Paste one below (copy straight from boundingbox.klokantech.com), e.g.:")
        print("  5.9559,45.818,10.4921,47.8084")
        raw = None
        source = "interactive prompt"

    while True:
        if raw is None:
            raw = input(f"BBOX for {city_code} (min_lon,min_lat,max_lon,max_lat): ").strip()
        parts = [v.strip() for v in raw.split(",")]
        if len(parts) != 4:
            print("Couldn't parse that - expected exactly 4 comma-separated "
                  "numbers (min_lon,min_lat,max_lon,max_lat).")
            if source == "--bbox argument":
                raise ValueError(f"Invalid --bbox value: '{bbox_arg}'")
            raw = None
            continue
        try:
            min_lon, min_lat, max_lon, max_lat = [float(v) for v in parts]
        except ValueError:
            print("Couldn't parse that as numbers.")
            if source == "--bbox argument":
                raise ValueError(f"Invalid --bbox value: '{bbox_arg}'")
            raw = None
            continue
        break

    data["bbox"] = [min_lon, min_lat, max_lon, max_lat]
    save_city_json(city_code, raw_base_dir, data)
    print(f"Saved bbox to '{path}' (from {source}) - future runs for "
          f"{city_code} will use this automatically.\n")

    return (min_lon, min_lat, max_lon, max_lat)
