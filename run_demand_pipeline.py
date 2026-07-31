# CUSTOM DEPOT-STYLE WRAPPER FOR THE DEMAND PIPELINE
#
# Mirrors build.py's shape (build.py: map_builder = MapGen(...);
# map_builder.run_all()): construct a small object with the run
# configuration, then call one of its run_*() methods. All the real work
# still lives in generate_demand_qzm_local.py (OSM parsing, routing,
# clustering, etc.) and enrich_utils.py - DemandGen is just a thin,
# build.py-style entry point wrapping its all()/demand() functions, same as
# build.py wraps depot.maps.MapGen.run_all().
#
# NOTE on city/bbox: unlike MapGen, generate_demand_qzm_local.py resolves
# CITY_CODE and the bbox from .env / bbox_utils.get_bbox() itself at import
# time - every function in it (OSM parsing, file paths, etc.) is wired to
# that single resolved city, not to whatever's passed into a constructor.
# city/bbox are still accepted here, for the same build.py-style shape and
# so a mismatch against what's actually configured in .env fails loudly
# instead of silently running against the wrong city.

import os
from dotenv import load_dotenv

load_dotenv()

city_code = os.getenv("CITY_CODE")
if not city_code:
    raise ValueError("CITY_CODE is missing from your .env file!")
RAW_BASE_DIR = os.getenv("RAW_BASE_DIR", "raw_map_files")

# config.json fields - see config_utils.py for how these are used.
CITY_NAME = os.getenv("CITY_NAME")
MAP_DESCRIPTION = os.getenv("MAP_DESCRIPTION")
MAP_CREATOR = os.getenv("MAP_CREATOR")
for var_name, value in [
    ("CITY_NAME", CITY_NAME),
    ("MAP_DESCRIPTION", MAP_DESCRIPTION),
    ("MAP_CREATOR", MAP_CREATOR),
]:
    if not value:
        raise ValueError(f"{var_name} is missing from your .env file!")

from bbox_utils import get_bbox
bbox = list(get_bbox(city_code, RAW_BASE_DIR))


class DemandGen:
    """
    Thin, build.py-style entry point for the modular demand pipeline - see
    generate_demand_qzm_local.py's build_base_demand()/enrich_demand()/
    all()/demand() for what actually runs.
    """
    def __init__(self, city, bbox, autofill_special_demand=True):
        # generate_demand_qzm_local.py is the actual source of truth for
        # which city/bbox a run targets (via CITY_CODE/get_bbox in .env) -
        # this check just makes sure this object's config agrees with that,
        # instead of silently ignoring a mismatched city/bbox passed in here.
        if city != city_code:
            raise ValueError(
                f"DemandGen was constructed for city='{city}', but "
                f"CITY_CODE in .env is '{city_code}' - "
                f"generate_demand_qzm_local.py always runs against "
                f"CITY_CODE, so these have to match."
            )
        self.city = city
        self.bbox = bbox
        self.autofill_special_demand = autofill_special_demand

    def run_all(self):
        """Full pipeline: OSM parsing + base OSRM routing
        (build_base_demand()), then cluster/consolidate + named special
        demand (enrich_demand()). Regenerates config.json afterward."""
        from generate_demand_qzm_local import all as _all
        result = _all(autofill_special_demand=self.autofill_special_demand)
        if result:
            self._write_config()
        return result

    def run_enrich_only(self):
        """Re-runs only the enrichment stage against the existing base
        checkpoint (demand_data_base.json) - use after tuning
        RES_SIZE_RATIO/JOB_SIZE_RATIO or hand-editing special-demand data,
        without repeating the much slower OSM parsing / airport snapping /
        base OSRM routing steps. Regenerates config.json afterward."""
        from generate_demand_qzm_local import demand as _demand
        result = _demand(autofill_special_demand=self.autofill_special_demand)
        if result:
            self._write_config()
        return result

    def _write_config(self):
        """Rebuilds config.json from .env (CITY_NAME/MAP_DESCRIPTION/
        MAP_CREATOR) plus the population summed fresh from demand_data.json
        - see config_utils.py. Runs automatically after run_all()/
        run_enrich_only() succeed, so config.json can never drift out of
        sync with the actual demand data."""
        from config_utils import write_config
        return write_config(
            city_code=self.city,
            raw_base_dir=RAW_BASE_DIR,
            city_name=CITY_NAME,
            description_template=MAP_DESCRIPTION,
            creator=MAP_CREATOR,
        )


demand_gen = DemandGen(
    city=city_code,
    bbox=bbox,
    autofill_special_demand=True
    )

if __name__ == "__main__":
    demand_gen.run_all()
