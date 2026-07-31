### ==== SHARED HELPER: config.json GENERATION ====
#
# config.json needs a "population" field that must exactly equal the sum of
# pops[].size in demand_data.json (see depot's demand.py create_description(),
# which asserts config["population"] == that sum) - so population is NEVER
# typed in by hand. It's always computed fresh from demand_data.json, which
# is the only way it can never silently drift out of sync again (as happened
# with BER: config.json said 1,300,000 while the actual demand data summed
# to 1,727,972).
#
# write_config() is called automatically at the end of DemandGen.run_all()
# and DemandGen.run_enrich_only() (run_demand_pipeline.py) - there's no
# separate script to run by hand anymore. It just needs re-running (via
# rerun_enrichment.py, which triggers it too) any time MAP_DESCRIPTION/
# CITY_NAME/MAP_CREATOR change in .env without a new demand run.
#
# "code" is never a separate .env var - it's always the same CITY_CODE
# everything else in this project is already keyed on, so it can't drift
# out of sync with the raw_map_files/{CITY_CODE}/ folder it's written into.
#
# "description" (MAP_DESCRIPTION) is a template - put the literal
# placeholders {CITY_NAME} and/or {POPULATION_ROUNDED} wherever the city
# name / human-readable commuter count should go, and they're substituted
# with CITY_NAME and the real population rounded to the nearest 50k
# (German-style thousands separator, e.g. 1.750.000).
#
# initialViewState is always zoom=15, bearing=0, latitude/longitude = the
# center of the city's saved bbox ({CITY_CODE}_MAP_DATA.json via
# bbox_utils.get_bbox()) - never read from .env.
#
# version is preserved as-is if config.json already exists (ship.py owns
# bumping it on release) and otherwise defaults to "0.1.0" for a first run.

import os
import json

from bbox_utils import get_bbox


def config_file_path(city_code, raw_base_dir):
    return os.path.join(raw_base_dir, city_code, "config.json")


def compute_population(demand_data_file):
    """Exact commuter total = sum of pops[].size in demand_data.json - this
    is the number depot's create_description() asserts config["population"]
    must match exactly, so it's never rounded."""
    if not os.path.exists(demand_data_file):
        raise FileNotFoundError(
            f"'{demand_data_file}' not found - run the demand pipeline "
            f"first so there's demand data to sum."
        )
    with open(demand_data_file, "r") as f:
        data = json.load(f)
    return int(sum(p["size"] for p in data["pops"]))


def round_to_nearest_50k(n):
    return round(n / 50000) * 50000


def format_de(n):
    """1750000 -> '1.750.000' (German-style thousands separator)."""
    return f"{n:,}".replace(",", ".")


def write_config(city_code, raw_base_dir, city_name, description_template, creator):
    """
    Builds and writes {raw_base_dir}/{city_code}/config.json from the given
    values plus the real population summed from that city's demand_data.json
    and the bbox center saved for that city. Returns the config dict.
    """
    city_raw_dir = os.path.join(raw_base_dir, city_code)
    demand_data_file = os.path.join(city_raw_dir, "demand_data.json")
    config_path = config_file_path(city_code, raw_base_dir)

    population = compute_population(demand_data_file)
    population_rounded = round_to_nearest_50k(population)

    description = description_template.replace(
        "{CITY_NAME}", city_name
    ).replace(
        "{POPULATION_ROUNDED}", format_de(population_rounded)
    )

    bbox = list(get_bbox(city_code, raw_base_dir))
    center_lon = round((bbox[0] + bbox[2]) / 2, 5)
    center_lat = round((bbox[1] + bbox[3]) / 2, 5)

    # Preserve an existing version (ship.py bumps it on release) - only
    # default to a fresh "0.1.0" the first time config.json is created.
    version = "0.1.0"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            existing = json.load(f)
        version = existing.get("version", version)

    config = {
        "name": city_name,
        "code": city_code,
        "description": description,
        "population": population,
        "initialViewState": {
            "zoom": 15,
            "latitude": center_lat,
            "longitude": center_lon,
            "bearing": 0
        },
        "creator": creator,
        "version": version
    }

    os.makedirs(city_raw_dir, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)

    print(f"Wrote '{config_path}':")
    print(f"  population: {population:,} (rounded for description: {population_rounded:,})")
    print(f"  initialViewState: zoom=15, lat={center_lat}, lon={center_lon}, bearing=0")
    print(f"  version: {version}")

    return config
