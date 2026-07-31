# EXAMPLE: re-run only the enrichment stage of the demand pipeline
#
# Use this after tuning RES_SIZE_RATIO/JOB_SIZE_RATIO in .env, hand-editing a
# special-demand JSON file (raw_map_files/CITY_CODE/.railyard_map/
# special_demand_points.json), or changing CITY_NAME/MAP_DESCRIPTION/
# MAP_CREATOR - it skips the much slower OSM parsing / airport snapping /
# base OSRM routing steps and just re-runs cluster/consolidate + named
# special demand against the existing base checkpoint
# (raw_map_files/CITY_CODE/demand_data_base.json), saves the result to
# demand_data.json - same file run_demand_pipeline.py's full run writes -
# and regenerates config.json from it (see config_utils.py).
#
# Requires run_demand_pipeline.py (DemandGen.run_all(), the full pipeline)
# to have completed at least once for this city already - see its
# run_enrich_only() docstring, and generate_demand_qzm_local.py's
# enrich_demand() for what actually runs.

from run_demand_pipeline import demand_gen

demand_gen.run_enrich_only()
