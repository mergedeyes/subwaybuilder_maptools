from depot.maps import MapGen

map_builder = MapGen(
    city="BER",
    # Bounding box for Berlin [min_lon, min_lat, max_lon, max_lat]
    bbox=[13.088, 52.338, 13.761, 52.675], 
    
    osmpbf="./berlin.osm.pbf", 
    outputdir=".",
    
    cities=['city'],                                        # Berlin itself
    suburbs=['borough', 'suburb'],                          # Bezirke / Ortsteile
    neighborhoods=['neighbourhood', 'quarter', 'locality'], # Kieze / Viertel
    
    label_name_language="prefer:de",
    road_name_preferred_language="de",
    
    RAM=16,
    ncores=10
)

map_builder.run_all()