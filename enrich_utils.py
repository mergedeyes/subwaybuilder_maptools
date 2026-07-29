### ==== SHARED HELPER: CLUSTER/CONSOLIDATE + NAMED SPECIAL DEMAND ====
#
# Wraps depot.demand.DemandData methods (imported, never edited - src/depot is
# off-limits per this project's CLAUDE.md) for two steps used by
# generate_demand_qzm_local.py's enrich_demand() - called either as part of
# the full pipeline (all()) or standalone (demand()), both via
# run_demand_pipeline.py:
#
#   cluster_and_consolidate(demand): merge_identical_commutes -> agglomerate_pops
#     -> cluster_points -> enforce_max_pop_size. No external data - just
#     cleanup of whatever demand already has (fixes tiny overlapping points
#     and uniform-looking pop sizes).
#
#   add_named_special_demand(demand, entries): calls add_points() once per
#     entry from the per-city "special_demand" list (see special_demand_utils.py
#     and bbox_utils.py). Only entries with a non-null total_capacity should be
#     passed in.
#
#   route_new_pops(demand, osrm_url): resolves drivingSeconds/drivingDistance
#     for whatever pops add_points() just created (they always start at 0).
#     Deliberately does NOT use depot's own demand.calculate_routes() - see
#     the big comment above that function for why.
#
# NOTE on cluster_points(): its default max_pop_threshold/buffer_meters are
# plain Python lists, and depot's implementation indexes them with a numpy
# boolean mask, which crashes regardless of numpy version. We work around this
# entirely from the caller side (passing the same values as np.array()) rather
# than editing src/depot.
#
# NOTE on step order (agglomerate_pops BEFORE cluster_points, not after):
# agglomerate_pops() groups small residential pops *by job destination* and
# creates a brand-new "SO_" (super-origin) point per (job destination,
# spatial sub-cluster) - so the same tight-knit residential block generates
# one new SO_ point for every distinct job hub its residents commute to, all
# sitting at roughly the same physical spot. If cluster_points() already ran
# by that point, none of those freshly-created SO_ points ever get a chance
# to be spatially re-merged with each other - they're new, so cluster_points
# never sees them. That's exactly what caused huge numbers of small circles
# stacked on top of each other in dense neighborhoods even after clustering
# supposedly ran. Running agglomerate_pops() first, then letting
# cluster_points() have the last word over the *entire* resulting point set
# (SO_ points included), lets it re-consolidate same-block SO_ points that
# still overlap. Verified with a synthetic test: 100 tightly-clustered
# buildings feeding 5 different job hubs collapsed to 6 overlapping points
# with the old order, vs. 1 correctly-merged point with this order.

import math
import random

import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from scipy.spatial import cKDTree
from shapely.geometry import Point


class GreenSpaceIndex:
    """
    Spatial index over green-space polygons (park/nature_reserve/water_park/
    recreation_ground/theme_park/zoo/aquarium/national_park boundaries - see
    generate_demand_qzm_local.py's _capture_green_space_polygon()/
    capture_green_space_relations()) so coordinates WE generate after OSM
    parsing - split siblings from enforce_max_point_size(), or depot's own
    agglomerate_pops() SO_ centroids - can be checked against real terrain,
    not just against how close they are to other points. The OSM-extraction
    path already pushes park/nature_reserve ways themselves to their
    boundary edge (see _green_space_edge_location()), but that only covers
    points generated DIRECTLY from those ways - it has no way to stop
    something generated later (e.g. by depot's own code) from landing in
    the same polygon. Real case this fixes: Tempelhofer Feld, a huge
    mostly-empty former airport field in Berlin, where split placements and
    SO_ centroids had nothing stopping them from drifting into the middle
    of it.

    Only exposes containment checks - no longer a "push just outside the
    boundary" method. An earlier version had one (push_outside()), but
    nudging every offending point to its nearest boundary spot means many
    unrelated points scattered across one big park (e.g. several different
    SO_ centroids inside Tempelhofer Feld) all converge on roughly the same
    edge location, reproducing the exact overlapping-clusters look this was
    meant to fix - just moved onto the park's boundary instead of its
    interior. See _merge_points_in_green_space() and _place_split_point()'s
    green-space fallback: both now fold an offending point's population
    into an already-valid neighbor instead of moving it anywhere.

    A coarse grid of polygon bounding boxes avoids checking every candidate
    point against every polygon (a city can have thousands of these).
    Parameter/return order is (lon, lat) throughout, matching how
    enrich_utils.py's other placement code (_place_split_point, etc.)
    already works with coordinates.
    """
    def __init__(self, polygons, cell_deg=0.01):
        self.polygons = polygons
        self.cell_deg = cell_deg
        self.cells = {}
        for idx, poly in enumerate(polygons):
            minx, miny, maxx, maxy = poly.bounds
            for cx in range(int(minx // cell_deg), int(maxx // cell_deg) + 1):
                for cy in range(int(miny // cell_deg), int(maxy // cell_deg) + 1):
                    self.cells.setdefault((cx, cy), []).append(idx)

    def _candidates(self, lon, lat):
        cx, cy = int(lon // self.cell_deg), int(lat // self.cell_deg)
        seen = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx in self.cells.get((cx + dx, cy + dy), []):
                    if idx not in seen:
                        seen.append(idx)
        return seen

    def containing_polygon(self, lon, lat):
        """Returns the polygon containing (lon, lat), or None."""
        if not self.polygons:
            return None
        pt = Point(lon, lat)
        for idx in self._candidates(lon, lat):
            poly = self.polygons[idx]
            if poly.contains(pt):
                return poly
        return None


# depot's own documented defaults for cluster_points(), just coerced to numpy
# arrays to dodge the list-indexing bug described above.
CLUSTER_MAX_POP_THRESHOLD = np.array([25, 50, 75, 200, 500, 5000, 15000, np.inf])
# Halved from depot's own documented defaults ([1500, 1000, 500, 250, 200,
# 150, 125, 100]) - cluster_points() now runs AFTER agglomerate_pops() (see
# the module comment above), so it has the last word over the *whole* point
# set instead of just the original raw points. At the original buffer sizes
# that made it merge far more aggressively city-wide than intended - point
# count on a real Berlin run dropped from ~30,000 to ~2,100, producing a
# smaller number of very large (5k-18k) points instead of many small, spread
# out ones. Smaller buffers keep the *fix* (no more same-block SO_ points
# stacked on each other) without also flattening genuinely distinct nearby
# points into one.
CLUSTER_BUFFER_METERS = np.array([750, 500, 250, 150, 100, 75, 60, 50])


def cluster_and_consolidate(demand,
                             agglomerate_small_threshold=100,
                             # Halved from depot's own documented defaults
                             # (0.03 / 0.015, ~3.3km / ~1.6km) for the same
                             # reason as CLUSTER_BUFFER_METERS above - these
                             # decide how far apart same-job-destination
                             # residential flows can be and still fold into
                             # one super-origin point.
                             agglomerate_distance_noncbd=0.01,
                             agglomerate_distance_cbd=0.006,
                             cbd_bbox=None,
                             max_pop_size=100,
                             max_point_size=2000,
                             green_index=None,
                             cluster_max_pop_threshold=None,
                             cluster_buffer_meters=None):
    """
    Cleans up demand's points/pops in place using only data already present -
    no external stats needed. Mutates and also returns `demand`.

    `cluster_max_pop_threshold`/`cluster_buffer_meters` override the module-
    level CLUSTER_MAX_POP_THRESHOLD/CLUSTER_BUFFER_METERS defaults passed to
    depot's cluster_points() - exposed as parameters (rather than just using
    the module constants directly) so a caller can scale them consistently
    with max_point_size/agglomerate_small_threshold (e.g.
    generate_demand_qzm_local.py's HUB_SIZE_RATIO) without having to import
    and mutate the module constants themselves.

    If `green_index` (a GreenSpaceIndex) is given, it's used twice: right
    after cluster_points() to merge away any point - including depot's own
    agglomerate_pops() SO_ centroids, which are plain spatial averages with
    no awareness of terrain - that landed inside a real green-space polygon
    into its nearest valid neighbor (see _merge_points_in_green_space()),
    and again inside enforce_max_point_size() so split siblings don't drift
    into one either.
    """
    print("  Merging pops with identical home/work commutes...")
    demand.merge_identical_commutes()
    print(f"  -> {len(demand['points']):,} points / {len(demand['pops']):,} pops")

    print("  Agglomerating small scattered-origin pops into super-origins...")
    demand.agglomerate_pops(SMALL_THRESHOLD=agglomerate_small_threshold,
                            DISTANCE_THRESHOLD_NONCBD=agglomerate_distance_noncbd,
                            DISTANCE_THRESHOLD_CBD=agglomerate_distance_cbd,
                            cbd_bbox=cbd_bbox)
    print(f"  -> {len(demand['points']):,} points / {len(demand['pops']):,} pops")

    # Runs AFTER agglomerate_pops() so it gets the last word over the *whole*
    # point set, including the new SO_ points agglomerate_pops() just
    # created - see the module comment above for why the order matters.
    print("  Clustering overlapping points (Colin's method)...")
    # Temporarily flip depot's own verbosity flag on for just this call -
    # cluster_points() already has its own built-in "(N) Determining
    # mergers: ..." round-by-round progress printing gated behind self.verb
    # (which we otherwise keep False - see the DemandData(..., verb=False)
    # comment where it's constructed). This is purely additive console
    # output, no logic change - it's the only way to see how many of its
    # up-to-5 rounds actually run without editing depot's own code (off-
    # limits per this project's rule), which tells us whether the deep-copy-
    # per-round cost is a real concern here or converges after round 1-2.
    _prev_verb = demand.verb
    demand.verb = True
    demand.cluster_points(
        max_pop_threshold=(cluster_max_pop_threshold if cluster_max_pop_threshold is not None
                           else CLUSTER_MAX_POP_THRESHOLD),
        buffer_meters=(cluster_buffer_meters if cluster_buffer_meters is not None
                      else CLUSTER_BUFFER_METERS))
    demand.verb = _prev_verb
    print(f"  -> {len(demand['points']):,} points / {len(demand['pops']):,} pops")

    if green_index is not None and green_index.polygons:
        print("  Merging points that landed inside a green-space polygon into their nearest neighbor...")
        _merge_points_in_green_space(demand, green_index)
        print(f"  -> {len(demand['points']):,} points / {len(demand['pops']):,} pops")

    print(f"  Capping pop sizes at {max_pop_size}...")
    demand.enforce_max_pop_size(max_pop_size)
    print(f"  -> {len(demand['pops']):,} pops")

    print(f"  Capping point sizes at {max_point_size}...")
    enforce_max_point_size(demand, max_point_size, green_index=green_index)
    print(f"  -> {len(demand['points']):,} points")

    return demand


# depot's clustering (cluster_points/agglomerate_pops) only ever MERGES
# points together - there's no equivalent to enforce_max_pop_size() for
# points, so a genuinely dense area (or the consolidation cluster_and_
# consolidate() itself just did) can still land as one single giant point.
# Tightening merge/cluster radii only goes so far: real Berlin data still
# had 707 points over 2,000 and 10 over 8,000 even at fairly tight settings,
# because the underlying building-level density is just that high in spots -
# no merge radius will ever *split* that back apart. This does the splitting
# ourselves, the same way enforce_max_pop_size() splits an oversized pop into
# several capped ones.
GOLDEN_ANGLE_RAD = math.pi * (3 - math.sqrt(5))  # ~137.5 deg


class _SpatialIndex:
    """
    Simple uniform-grid spatial index (cell size ~= spacing_meters) so
    "is anything already within spacing_meters of this candidate spot" can be
    checked in roughly constant time instead of against every point on the
    whole map. Used so split placement respects points from OTHER splits and
    pre-existing points too, not just its own siblings - a candidate right
    next to some unrelated point elsewhere would previously never get
    rejected.
    """
    def __init__(self, points, spacing_meters, ref_lat):
        self.cell_deg = spacing_meters / (111_000 * max(math.cos(math.radians(ref_lat)), 0.1))
        self.cells = {}
        for p in points:
            self.add(p["location"][0], p["location"][1])

    def _key(self, lon, lat):
        return (int(lon / self.cell_deg), int(lat / self.cell_deg))

    def add(self, lon, lat):
        self.cells.setdefault(self._key(lon, lat), []).append((lon, lat))

    def nearest_dist(self, lon, lat):
        cx, cy = self._key(lon, lat)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for olon, olat in self.cells.get((cx + dx, cy + dy), []):
                    d = _haversine_m(lon, lat, olon, olat)
                    if best is None or d < best:
                        best = d
        return best


def _place_split_point(index, spacing_meters, base_lon, base_lat, order, bbox,
                        violations=None, max_attempts=20, green_index=None,
                        green_corrections=None):
    """
    Picks a spot for the order-th sibling of a split. order=0 tries the
    exact original location first (so at least one result stays where the
    original data actually pointed) - but unlike before, it now actually
    checks that spot against `index` too instead of returning it
    unconditionally. That matters once a point gets split a SECOND time
    (e.g. a landmark's gravity-sampled pops pushed an already-split-once
    point back over the cap): order=0 there would otherwise reuse a
    location that's already sitting right next to that point's own pass-1
    siblings.

    For every attempt (including a conflicting order=0): the FIRST attempt
    tries a sqrt(order)-radius golden-angle spiral step (guarantees fresh
    siblings fan out in different directions from each other rather than
    clump - see the module note above). If that spot is too close to
    something, later attempts pick a FULLY random angle (not just a small
    wobble around that one golden-angle sector) with a radius that grows
    deterministically each attempt. This is the actual fix for a real bug:
    the old version only ever wobbled the angle by +-0.6 rad around one
    fixed sector and grew the radius by a random (not guaranteed-increasing)
    multiplier - so if that one sector happened to be crowded, every one of
    its 10 retries could plausibly fail, and it then silently kept the last
    failed attempt anyway. Verified on real BER data: 465 points ended up
    within 100m of a neighbor, some as close as 13m, all traced back to this.

    Checked via `index`, which the caller keeps updated with every point
    placed so far (siblings AND unrelated points alike). Clamped inside
    `bbox` ([min_lon, min_lat, max_lon, max_lat]) if given, with a small
    inset so nothing ends up sitting exactly on the map edge.

    If `green_index` (a GreenSpaceIndex) is given, a candidate landing
    inside a captured green-space polygon (park/nature_reserve/water_park/
    etc.) is rejected the same way a too-close spacing violation is -
    fixes split siblings drifting into a huge, mostly-empty park (e.g.
    Tempelhofer Feld) since nothing previously checked terrain, only
    distance to other points.

    If `violations` (a list) is passed and every attempt still lands within
    spacing_meters of something (a genuinely saturated area - no amount of
    retrying will fix that), the least-bad candidate found is used and its
    clearance distance is appended to `violations` for the caller to
    summarize, instead of failing/warning per-point.

    If that final fallback candidate is inside a green-space polygon
    (rather than just a spacing violation), this returns (None, None)
    instead of placing anything there, and counts it in `green_corrections`
    (a list) if given - the caller is expected to fold this sub-point's
    population into an already-placed sibling instead. An earlier version
    nudged it to just outside the polygon boundary via
    GreenSpaceIndex.push_outside(), but for a park with many split siblings
    all originating nearby, that piles several "corrected" points on top of
    each other right at the boundary - the same overlapping-clusters
    problem this was meant to fix, just moved onto the park's edge.
    """
    lat_scale = 1 / 111_000
    lon_scale = 1 / (111_000 * max(math.cos(math.radians(base_lat)), 0.1))

    def _clamp(lon, lat):
        if not bbox:
            return lon, lat
        inset = spacing_meters / 111_000
        return (min(max(lon, bbox[0] + inset), bbox[2] - inset),
                min(max(lat, bbox[1] + inset), bbox[3] - inset))

    def _is_valid(lon, lat):
        nearest = index.nearest_dist(lon, lat)
        spacing_ok = nearest is None or nearest >= spacing_meters
        green_ok = green_index is None or green_index.containing_polygon(lon, lat) is None
        return spacing_ok and green_ok, nearest

    if order == 0:
        lon, lat = _clamp(base_lon, base_lat)
        ok, _ = _is_valid(lon, lat)
        if ok:
            index.add(lon, lat)
            return lon, lat
        # Falls through to the same randomized search below instead of
        # blindly keeping a spot already known to violate spacing or land
        # inside a green-space polygon.

    best = None  # (nearest_dist_or_inf, lon, lat) - kept in case every attempt fails
    for attempt in range(max_attempts):
        if attempt == 0:
            theta = order * GOLDEN_ANGLE_RAD + random.uniform(-0.3, 0.3)
        else:
            theta = random.uniform(0, 2 * math.pi)
        radius = spacing_meters * math.sqrt(max(order, 1)) * (1.0 + attempt * 0.3)

        lon = base_lon + radius * math.cos(theta) * lon_scale
        lat = base_lat + radius * math.sin(theta) * lat_scale
        lon, lat = _clamp(lon, lat)

        ok, nearest = _is_valid(lon, lat)
        if ok:
            index.add(lon, lat)
            return lon, lat
        # Tracked purely by spacing clearance (None/large is "best") - a
        # rejection caused only by green-space with otherwise perfect
        # spacing gets corrected below regardless of which check failed.
        clearance = nearest if nearest is not None else float('inf')
        if best is None or clearance > best[0]:
            best = (clearance, lon, lat)

    clearance, lon, lat = best
    if green_index is not None and green_index.containing_polygon(lon, lat) is not None:
        # Every attempt landed either too close to another point or inside
        # green space, and this final fallback candidate is itself still
        # inside a green-space polygon - don't place anything here at all;
        # signal the caller to fold this sub-point into an already-placed
        # sibling instead (see the docstring above for why).
        if green_corrections is not None:
            green_corrections.append(1)
        return None, None
    if violations is not None and clearance < spacing_meters:
        violations.append(clearance)
    index.add(lon, lat)
    return lon, lat


def _merge_points_in_green_space(demand, green_index):
    """
    Scans every ordinary (non-special-demand) point and, for any that land
    inside a captured green-space polygon, removes it and folds its
    residents/jobs (and every pop referencing it) into its nearest
    still-valid neighbor instead. This is the sweep that catches what the
    OSM-extraction edge-push (_green_space_edge_location()) and split
    placement (_place_split_point()'s green_index check) can't: depot's own
    agglomerate_pops() computes each new SO_ point's location as a plain
    spatial centroid of the residences it groups, with no awareness of
    terrain, so it can place a point in the middle of a large, mostly-empty
    park (e.g. Tempelhofer Feld) even though none of the actual residences
    that fed into it are there. Runs once, right after cluster_points()
    settles - the last point in the pipeline where depot's own code still
    has a chance to create such a point.

    An earlier version of this NUDGED the offending point to just outside
    the polygon boundary instead of merging it away. That's fine for one
    stray point, but a big, mostly-empty park can have MANY SO_ centroids
    scattered across it - nudging every one of them lands most at roughly
    the same nearest edge spot, reproducing the exact overlapping-clusters
    look this was supposed to fix, just now sitting on the park boundary
    instead of inside it. Merging into whatever real point already exists
    nearby (a building/hub that was always going to be there) doesn't
    create anything new at the boundary at all.

    Nearest-neighbor lookup uses a cKDTree over every still-valid ORDINARY
    point's location (same tool already used elsewhere in this codebase for
    cluster_organic()) rather than a linear scan, since a big city can have
    tens of thousands of points.

    Special demand points are never removed (same reasoning as
    enforce_max_point_size()) AND never used as a merge target either - a
    landmark's total_capacity is a real, researched number (see
    add_named_special_demand()'s docstring on why that number must stay
    exact), and a landmark can legitimately sit inside a park's boundary in
    real life (e.g. the church actually located on Tempelhofer Feld) without
    that meaning ordinary residential/job capacity should get folded onto
    it. Mutates and returns `demand`.
    """
    if green_index is None or not green_index.polygons:
        return demand

    points = demand["points"]
    if not points:
        return demand

    special_prefixes = set(demand.special_demand_ids.keys())
    merge_targets = []  # ordinary, non-green points only - eligible to receive merged capacity
    bad_points = []
    for point in points:
        prefix = point["id"].split("_", 1)[0]
        if prefix in special_prefixes:
            continue  # never removed, never merged into - left in demand["points"] untouched
        lon, lat = point["location"]
        if green_index.containing_polygon(lon, lat) is None:
            merge_targets.append(point)
        else:
            bad_points.append(point)

    if not bad_points:
        return demand
    if not merge_targets:
        # No ordinary point anywhere to merge into (degenerate case) - leave
        # these as-is rather than inflating a special-demand landmark's
        # researched capacity number by merging into it instead.
        return demand

    # (lat, lon) ordering, matching cluster_organic()'s existing cKDTree
    # convention elsewhere in this file.
    valid_coords = np.array([[p["location"][1], p["location"][0]] for p in merge_targets])
    tree = cKDTree(valid_coords)

    pops_by_id = {p["id"]: p for p in demand["pops"]}
    merged_residents = 0
    merged_jobs = 0
    for bad_point in bad_points:
        lon, lat = bad_point["location"]
        _, idx = tree.query((lat, lon))
        target = merge_targets[idx]

        target["residents"] += bad_point["residents"]
        target["jobs"] += bad_point["jobs"]
        merged_residents += bad_point["residents"]
        merged_jobs += bad_point["jobs"]

        for pop_id in bad_point["popIds"]:
            pop = pops_by_id.get(pop_id)
            if pop is None:
                continue
            # A pop can reference this point as BOTH residenceId and jobId
            # (a self-loop pop from depot's own cluster_points() merging a
            # residential and job point together) - handle each role
            # independently, same pattern as enforce_max_point_size()/
            # _clear_ordinary_points_within(). Checked against the pop's
            # CURRENT value (not a cached copy) so a pop whose OTHER
            # endpoint is also a bad point being merged in this same pass
            # still ends up correctly pointing at both merge targets.
            if pop["residenceId"] == bad_point["id"]:
                pop["residenceId"] = target["id"]
            if pop["jobId"] == bad_point["id"]:
                pop["jobId"] = target["id"]
            target["popIds"].append(pop_id)

    bad_ids = {p["id"] for p in bad_points}
    demand["points"] = [p for p in points if p["id"] not in bad_ids]

    print(f"  -> Merged {len(bad_points):,} point(s) that landed inside a "
          f"green-space polygon ({merged_residents:,} residents / "
          f"{merged_jobs:,} jobs) into their nearest still-valid neighbor, "
          f"instead of relocating them onto the park boundary.")
    return demand


def enforce_max_point_size(demand, max_point_size=2000, spacing_meters=250, green_index=None):
    """
    Splits any ordinary (non-special-demand) point whose residents+jobs
    total exceeds max_point_size into multiple smaller points, each capped
    at max_point_size, spread out around the original spot so siblings (and
    any other point already on the map) stay at least ~spacing_meters apart
    - see _place_split_point()'s docstring for how. Every pop keeps its
    exact size - they're just divided up across more points near the
    original location. Mutates and returns `demand`.

    spacing_meters is a guess at a gap wide enough that two max-size circles
    won't visually overlap in-game - tune it up/down based on how it actually
    looks, since it depends on Subway Builder's own circle-size-vs-population
    scale, which isn't something we have visibility into from here.

    If `green_index` (a GreenSpaceIndex, built from OSM-captured park/
    nature_reserve/water_park/etc. polygons) is given, split siblings are
    also kept out of real green-space polygons, not just spaced apart from
    other points - see _place_split_point()'s docstring.

    Special demand points (universities, stadiums, etc.) are left alone -
    those are deliberately one single, real-world location with a
    researched capacity; splitting "Olympiastadion" into several scattered
    fake mini-stadiums would be wrong, not a fix.
    """
    special_prefixes = set(demand.special_demand_ids.keys())
    pops_by_id = {p["id"]: p for p in demand["pops"]}
    bbox = getattr(demand, "bbox", None)

    # Seeded with every point on the map up front (not just this function's
    # own splits), and grown as new sub-points get placed, so a split's
    # sub-points never land on top of some unrelated, already-existing point
    # either - not just their own siblings.
    ref_lat = sum(p["location"][1] for p in demand["points"]) / len(demand["points"])
    index = _SpatialIndex(demand["points"], spacing_meters, ref_lat)

    new_points = []
    split_count = 0
    new_subpoint_count = 0
    violations = []
    green_corrections = []
    total_orphaned_pop_ids = 0
    for point in demand["points"]:
        total = point["residents"] + point["jobs"]
        prefix = point["id"].split("_", 1)[0]
        if total <= max_point_size or prefix in special_prefixes:
            new_points.append(point)
            continue

        split_count += 1
        lon, lat = point["location"]
        subs = []

        def _new_sub():
            sub_lon, sub_lat = _place_split_point(index, spacing_meters, lon, lat, len(subs), bbox,
                                                   violations, green_index=green_index,
                                                   green_corrections=green_corrections)
            if sub_lon is None:
                # Every candidate for a fresh sibling landed in green space
                # - fold into the most recently created sibling instead of
                # forcing this capacity into the park (or nudging it to the
                # park edge, which piles several such corrections on top of
                # each other there - see _place_split_point()'s docstring).
                if subs:
                    return subs[-1]
                # No sibling exists yet either (the very first split
                # attempt failed) - only plausible if the ORIGINAL point
                # itself sits deep in/against a huge green area, which the
                # cluster_and_consolidate() green-space merge should
                # already have cleared before enforce_max_point_size() ever
                # runs. Fall back to the original spot unconditionally so
                # this population doesn't just vanish.
                index.add(lon, lat)
                sub = {
                    "id": f"{point['id']}_split{len(subs)}",
                    "location": [lon, lat],
                    "jobs": 0,
                    "residents": 0,
                    "popIds": [],
                }
                subs.append(sub)
                return sub
            sub = {
                "id": f"{point['id']}_split{len(subs)}",
                "location": [sub_lon, sub_lat],
                "jobs": 0,
                "residents": 0,
                "popIds": [],
            }
            subs.append(sub)
            return sub

        current = _new_sub()
        for pop_id in point["popIds"]:
            pop = pops_by_id.get(pop_id)
            if pop is None:
                # A pop can be removed from demand["pops"] by depot's own
                # add_points() merge_within absorption without that removal
                # being reflected in every point's popIds list it touched -
                # same underlying depot behavior route_new_pops() already
                # works around (see its docstring). Simply dropping this
                # id here is correct, not a workaround: the pop no longer
                # exists, so there's nothing to carry into any new sub.
                total_orphaned_pop_ids += 1
                continue
            # A pop can reference this point as BOTH residenceId and jobId
            # (depot's own cluster_points() can merge a residential and job
            # point together, producing a same-origin-same-destination
            # "self-loop" pop) - handle each role independently instead of
            # picking just one, or the unhandled side would keep pointing
            # at this now-deleted point.
            is_residence = pop["residenceId"] == point["id"]
            is_job = pop["jobId"] == point["id"]
            size = pop["size"]
            contribution = size * (int(is_residence) + int(is_job))

            current_total = current["residents"] + current["jobs"]
            if current_total + contribution > max_point_size and current_total > 0:
                current = _new_sub()

            if is_residence:
                current["residents"] += size
                pop["residenceId"] = current["id"]
            if is_job:
                current["jobs"] += size
                pop["jobId"] = current["id"]
            current["popIds"].append(pop_id)

        new_points.extend(subs)
        new_subpoint_count += len(subs)

    demand["points"] = new_points
    if split_count:
        print(f"  -> Split {split_count:,} oversized point(s) into "
              f"{new_subpoint_count:,} smaller one(s).")
    if violations:
        print(f"  [WARNING] {len(violations):,} split sub-point(s) landed in an "
              f"area too dense to keep {spacing_meters}m clearance from every "
              f"neighbor even after retrying (clearance found ranged "
              f"{min(violations):.0f}-{max(violations):.0f}m) - these are the "
              f"tightest spots on the map (very high real building density), "
              f"not something more retries can fix.")
    if green_corrections:
        print(f"  -> Folded {len(green_corrections):,} split sub-point(s) into an "
              f"already-placed sibling instead of placing them inside a "
              f"green-space polygon (park/nature_reserve/water_park/etc.) they "
              f"would otherwise have landed in.")
    if total_orphaned_pop_ids:
        print(f"  [WARNING] Skipped {total_orphaned_pop_ids:,} popId(s) referencing "
              f"a pop no longer in demand['pops'] (depot's own add_points() "
              f"merge_within absorption can remove a pop without updating "
              f"every point's popIds list that still names it - same "
              f"behavior route_new_pops() already works around). Their "
              f"population isn't lost - it was already accounted for "
              f"wherever depot's absorption actually moved it; these are "
              f"just stale leftover references being cleaned up here.")
    return demand


# Sensible per-type defaults for the add_points() knobs that OSM tags can't
# tell us. Any of these can be overridden per-entry in the JSON itself (e.g.
# a custom "merge_within" key on that entry).
#
# Only landmark types are listed here - sports_centre/park/port/college were
# dropped entirely (see special_demand_utils.py's module comment for why: the
# base pipeline already models these via CSV + is_special, and a second
# special-demand point for the same building just double-counted it).
# school/clinic stayed, but only reach here after passing a significance
# filter (also in special_demand_utils.py) that screens out the numerous
# small ones - same reasoning, scoped down instead of dropped entirely.
TYPE_DEFAULTS = {
    "university":     {"pop_size": 100, "merge_within": 300, "max_distance": 25000, "residential_split": 0.05},
    "hospital":       {"pop_size": 50,  "merge_within": 200, "max_distance": 20000},
    "school":         {"pop_size": 50,  "merge_within": 200, "max_distance": 15000},
    # clinic only ever reaches here after passing _clinic_is_significant() in
    # special_demand_utils.py (multi-doctor medical centers, not single-
    # practice offices) - scaled well below a full hospital.
    "clinic":         {"pop_size": 25,  "merge_within": 150, "max_distance": 10000},
    "stadium":        {"pop_size": 100, "merge_within": 250, "max_distance": 40000},
    "zoo":            {"pop_size": 50,  "merge_within": 200, "max_distance": 25000},
    "amusement_park": {"pop_size": 50,  "merge_within": 200, "max_distance": 25000},
    "museum":         {"pop_size": 50,  "merge_within": 150, "max_distance": 20000},
    # Airport passenger demand: merge_within intentionally omitted/0 so this
    # does NOT absorb the existing, separate CSV-driven airport *worker*
    # mega-hub (custom_hubs.json / AIRPORT_GEOJSON) - those stay unmerged.
    "airport":        {"pop_size": 200, "merge_within": 0,   "max_distance": None},
}
FALLBACK_DEFAULTS = {"pop_size": 50, "merge_within": 200, "max_distance": 25000}

# depot itself only recognizes the fixed taxonomy shipped in
# src/depot/special_demand_types.json (parent type ids, plus a handful of
# registered sub_type ids). Any poi "type" add_points() is given that isn't
# in that taxonomy gets accepted silently at add_points() time, but then
# crashes save_schemas() with a KeyError later
# (self.special_demand_subtypes[p['type']]) once you try to actually save.
# This remaps a label to the closest real depot type/subtype id at the
# hand-off - university, hospital, school, stadium, museum, zoo,
# amusement_park, and airport already match depot's taxonomy directly and
# need no remapping. "clinic" is the one exception: it isn't in depot's
# taxonomy at all (neither a parent id nor a registered subtype, and
# depot's "hospital" has no subtypes to attach it to either), so it's
# remapped to "hospital" outright.
DEPOT_TYPE_OVERRIDES = {
    "clinic": "hospital",
}


def _print_type_breakdown(header, type_labels):
    """Prints a header line then a multi-line, indented 'type  count'
    breakdown (one type per line, most common first, counts right-aligned) -
    instead of printing one line per entry, which is unreadable once a city
    has hundreds."""
    if not type_labels:
        return
    counts = {}
    for t in type_labels:
        counts[t] = counts.get(t, 0) + 1
    name_width = max(len(t) for t in counts)
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    print(f"{header} ({len(type_labels):,} total):")
    for t, n in rows:
        print(f"    {t.ljust(name_width)}  {n:>5,}")


def _haversine_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in meters - same formula as depot's own
    src/depot/utils.py:haversine(), duplicated here since that's a private
    helper inside src/depot we don't import from directly."""
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * 6371000


def _clear_ordinary_points_within(demand, location, radius_meters):
    """
    Deletes any ORDINARY (non-special-demand) point within radius_meters of
    location, along with its pops - same bookkeeping as depot's own
    del_points() (cleans up the *other* endpoint's popIds reference for each
    removed pop too), just bulk-selecting everything within a radius instead
    of only the single nearest point per coordinate.

    Call this right before add_points() instead of relying on its own
    merge_within to do the absorption: add_points()'s merge_within ADDS an
    absorbed point's existing jobs/residents onto the new special demand
    point, on top of the freshly generated capacity-based pops - i.e. it's
    additive, not a replacement. For a landmark with a real, researched
    total_capacity, that means the final point ends up bigger than the
    number you actually typed in (typed number + whatever the base pipeline
    already modeled at that same spot - verified on real data: Uber Arena's
    total_capacity=14500 came out as 16,661; a university auto-filled at
    4,510 came out as 13,800 jobs alone). Deleting the ordinary point first
    means the new special demand point starts clean, so total_capacity is
    what actually lands in the file.

    Mirrors add_points()'s own protection against ever merging two DIFFERENT
    special demand points together (checking each point id's prefix against
    demand.special_demand_ids) - so this never touches another special
    demand point, only ordinary base-pipeline points.
    """
    if not radius_meters:
        return 0

    special_prefixes = set(demand.special_demand_ids.keys())
    lon, lat = location

    remove_ids = {
        p["id"] for p in demand["points"]
        if p["id"].split("_", 1)[0] not in special_prefixes
        and _haversine_m(lon, lat, p["location"][0], p["location"][1]) <= radius_meters
    }
    if not remove_ids:
        return 0

    points_by_id = {p["id"]: p for p in demand["points"]}
    pops_by_id = {p["id"]: p for p in demand["pops"]}

    pops_to_remove = set()
    for pid in remove_ids:
        pops_to_remove.update(points_by_id[pid]["popIds"])

    # Clean up the *other* endpoint's popIds list AND its residents/jobs
    # count for each removed pop (only if that endpoint isn't also being
    # deleted) - removing just the popIds entry without also subtracting the
    # pop's size left the other point's residents/jobs stale/inflated,
    # exactly the kind of drift that shows up as print_stats()'s "Workers"
    # and "Residents" totals no longer matching the sum of all pop sizes.
    #
    # pops_by_id.get() (not direct indexing): this runs once per landmark
    # in add_named_special_demand()'s loop, so by the time a LATER
    # landmark's call gets here, an EARLIER landmark's demand.add_points()
    # may have already absorbed/deleted a pop via depot's own internal
    # merge_within logic without updating every point's popIds list that
    # still references it (same underlying depot behavior route_new_pops()
    # already works around) - a stale pop_id here is simply skipped rather
    # than crashing, since there's nothing left to clean up for it.
    for pop_id in pops_to_remove:
        pop = pops_by_id.get(pop_id)
        if pop is None:
            continue
        for role in ("residenceId", "jobId"):
            other_id = pop[role]
            if other_id not in remove_ids:
                other_point = points_by_id.get(other_id)
                if other_point is not None and pop_id in other_point["popIds"]:
                    other_point["popIds"].remove(pop_id)
                    field = "residents" if role == "residenceId" else "jobs"
                    other_point[field] = max(0, other_point[field] - pop["size"])

    demand["pops"] = [p for p in demand["pops"] if p["id"] not in pops_to_remove]
    demand["points"] = [p for p in demand["points"] if p["id"] not in remove_ids]

    return len(remove_ids)


def _predict_point_id(demand, poi):
    """Predicts the point ID depot's add_points() would generate for this
    poi, mirroring add_points()'s own internal ID derivation exactly."""
    poi_type = poi["type"]
    schema_code = demand.special_demand_codes.get(poi_type)
    type_code = schema_code if schema_code else poi_type.replace(" ", "_").upper()
    identifier = poi.get("code") or poi["name"].replace(" ", "_").strip()
    return f"{type_code}_{identifier}"


def add_named_special_demand(demand, entries):
    """
    Calls demand.add_points() once per entry - skipping any entry whose
    predicted point ID already exists in demand['points']. add_points()
    itself doesn't check for an existing point with the same ID before
    appending, so re-running this against an already-enriched file (e.g.
    re-running `python run_demand_pipeline.py --stage demand` more than
    once, or any future workflow that reuses a file across runs) would
    otherwise create a duplicate point for the same real-world landmark
    every time.

    Only pass entries that already have a non-null total_capacity (see
    special_demand_utils.detect_and_confirm_special_demand).
    Returns the list of poi dicts actually passed to add_points(), for logging/inspection.
    """
    existing_ids = {p["id"] for p in demand["points"]}
    added = []
    added_types = []
    skipped_types = []
    cleared_count = 0

    total = len(entries)
    # add_points() recomputes gravity weights over every existing point on
    # each call, so a few thousand entries can take a while - show progress
    # every ~1/15th instead of going silent the whole time (without also
    # spamming the console the way per-entry printing did before).
    progress_interval = max(1, total // 15)

    for i, entry in enumerate(entries, 1):
        type_label = entry["type"]
        defaults = TYPE_DEFAULTS.get(type_label, FALLBACK_DEFAULTS)

        poi = {
            "type": DEPOT_TYPE_OVERRIDES.get(type_label, type_label),
            "name": entry["name"],
            "location": entry["location"],
            "total_capacity": entry["total_capacity"],
            "pop_size": entry.get("pop_size", defaults["pop_size"]),
        }
        if entry.get("code"):
            poi["code"] = entry["code"]

        predicted_id = _predict_point_id(demand, poi)
        if predicted_id in existing_ids:
            skipped_types.append(type_label)
            continue

        merge_within = entry.get("merge_within", defaults.get("merge_within"))
        if merge_within:  # skip 0/None - see the "airport" note above
            poi["merge_within"] = merge_within
            # Pre-clear ordinary points within this radius ourselves instead
            # of letting add_points()'s own merge_within absorb-and-ADD them
            # onto total_capacity - see _clear_ordinary_points_within()'s
            # docstring for why.
            cleared_count += _clear_ordinary_points_within(demand, entry["location"], merge_within)

        max_distance = entry.get("max_distance", defaults.get("max_distance"))
        if max_distance:
            poi["max_distance"] = max_distance

        residential_split = entry.get("residential_split", defaults.get("residential_split"))
        if residential_split:
            poi["residential_split"] = residential_split

        demand.add_points(poi)
        existing_ids.add(predicted_id)
        added.append(poi)
        added_types.append(type_label)

        if i % progress_interval == 0 or i == total:
            print(f"    -> {i:,} / {total:,} processed...")

    if skipped_types:
        _print_type_breakdown("Skipping (already present in the demand file)", skipped_types)
    if added_types:
        _print_type_breakdown("Adding special demand point(s)", added_types)
    if cleared_count:
        print(f"  Cleared {cleared_count:,} ordinary point(s) that would otherwise have "
              f"been absorbed on top of a landmark's total_capacity instead of replaced.")

    return added


# depot's own demand.calculate_routes(routing_method="osrm") is single-
# threaded and, per point in the WHOLE file (not just new ones), makes a
# "nearest" HTTP round trip for the home node, then for each of that point's
# still-unrouted pops another "nearest" round trip for the job node plus the
# route call itself - three sequential requests per pop, each preceded by a
# small sleep, with no concurrency at all. On a file with tens of thousands
# of points that's extremely slow even though only a handful of pops (the
# ones add_points() just created, which always start at drivingSeconds=0)
# actually need anything computed.
#
# route_new_pops() instead batches many pairs into OSRM's /table endpoint at
# once (a many-to-many duration/distance matrix in a single request) instead
# of one /route call per pop - same approach and same tuning as
# generate_demand_qzm_local.py's base ~126k-route commuter routing, where
# measuring real BER data found batch_size=5 beat both no batching (all
# per-request overhead) and much bigger batches (too much wasted matrix
# compute once OSRM was shown to be CPU-saturated, not overhead-bound).
def route_new_pops(demand, osrm_url, max_workers=30, batch_size=5, max_retry_passes=5):
    """
    Resolves drivingSeconds/drivingDistance for any pop that still has
    drivingSeconds <= 0 (i.e. newly created by add_points()). Mutates
    `demand` in place. Returns the number of pops routed.
    """
    points_by_id = {p["id"]: p for p in demand["points"]}

    unrouted = [p for p in demand["pops"] if p.get("drivingSeconds", 0) <= 0]
    # A pop can end up pointing at a point ID that add_points()'s own
    # merge_within absorption already deleted (e.g. two special demand
    # entries both merge-absorbing a shared nearby cluster point - depot's
    # own bookkeeping for that case doesn't always catch every pop
    # referencing it). demand.save() drops these automatically via its own
    # sanitize() step regardless, so just skip them here rather than crash.
    to_route = [p for p in unrouted if p["residenceId"] in points_by_id and p["jobId"] in points_by_id]
    orphaned = len(unrouted) - len(to_route)
    if orphaned:
        print(f"  [WARNING] Skipping {orphaned:,} pop(s) referencing a point "
              f"no longer in the file (absorbed during a special demand "
              f"merge) - these get dropped automatically on save().")

    if not to_route:
        return 0

    print(f"  Routing {len(to_route):,} new pop(s) via OSRM ({osrm_url})...")
    # requests.Session()'s default HTTPAdapter caps its connection pool at 10
    # (pool_connections/pool_maxsize) regardless of max_workers - without
    # mounting a bigger pool, most of those threads just queue for one of 10
    # real connections instead of genuinely running max_workers requests at
    # once.
    session = requests.Session()
    _adapter = requests.adapters.HTTPAdapter(pool_connections=max_workers,
                                              pool_maxsize=max_workers)
    session.mount("http://", _adapter)
    session.mount("https://", _adapter)

    # Several landmarks' gravity-sampled pops can end up sharing the exact
    # same (home, job) point pair - the duration/distance only depends on
    # those two physical points, never on which pop it is, so resolve each
    # unique pair once and fan the result out afterward.
    pair_representative = {}
    for pop in to_route:
        pair_key = (pop["residenceId"], pop["jobId"])
        if pair_key not in pair_representative:
            pair_representative[pair_key] = pop
    unique_pops = list(pair_representative.values())

    def process_table_batch(batch):
        n = len(batch)
        coords = ([points_by_id[pop["residenceId"]]["location"] for pop in batch]
                  + [points_by_id[pop["jobId"]]["location"] for pop in batch])
        coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
        radii_str = ";".join(["1000"] * len(coords))
        sources = ";".join(str(i) for i in range(n))
        destinations = ";".join(str(i) for i in range(n, 2 * n))
        url = (f"{osrm_url}/table/v1/driving/{coord_str}"
               f"?sources={sources}&destinations={destinations}"
               f"&annotations=duration,distance&radiuses={radii_str}")
        try:
            response = session.get(url, timeout=15.0)
            if response.status_code != 200:
                return {}, batch, []
            resp_data = response.json()
            if resp_data.get("code") != "Ok":
                return {}, batch, []
            durations = resp_data.get("durations")
            distances = resp_data.get("distances")
            if durations is None or distances is None:
                return {}, batch, []
        except requests.RequestException:
            return {}, batch, []

        resolved = {}
        fallback_pops = []
        for i, pop in enumerate(batch):
            pair_key = (pop["residenceId"], pop["jobId"])
            d_sec = durations[i][i]
            d_dist = distances[i][i]
            if d_sec is None or d_dist is None:
                # This specific pair has no road route - straight-line
                # fallback for just this pair, not a whole-batch retry.
                fallback_pops.append(pop)
            else:
                resolved[pair_key] = (int(d_sec), int(d_dist))
        return resolved, [], fallback_pops

    resolved_by_pair = {}
    batches = [unique_pops[i:i + batch_size] for i in range(0, len(unique_pops), batch_size)]
    pending_batches = batches
    attempt = 1

    while pending_batches:
        failed_batches = []
        completed_pairs = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_table_batch, b) for b in pending_batches]
            for future in as_completed(futures):
                resolved, retry_batch, fallback_pops = future.result()
                resolved_by_pair.update(resolved)
                completed_pairs += len(resolved)
                if retry_batch:
                    failed_batches.append(retry_batch)
                for pop in fallback_pops:
                    h = points_by_id[pop["residenceId"]]
                    j = points_by_id[pop["jobId"]]
                    dist = math.hypot(h["location"][0] - j["location"][0],
                                       h["location"][1] - j["location"][1]) * 111000
                    resolved_by_pair[(pop["residenceId"], pop["jobId"])] = (int(dist / 8.3), int(dist))
                if completed_pairs and completed_pairs % 5000 < batch_size:
                    print(f"    -> Resolved {completed_pairs:,} / {len(unique_pops):,} unique pair(s) in this pass...")

        if not failed_batches:
            break

        if len(failed_batches) == len(pending_batches) or attempt >= max_retry_passes:
            total_failed = sum(len(b) for b in failed_batches)
            print(f"  [WARNING] {total_failed} route(s) couldn't be resolved via OSRM - "
                  f"falling back to straight-line distance for those.")
            for batch in failed_batches:
                for pop in batch:
                    h = points_by_id[pop["residenceId"]]
                    j = points_by_id[pop["jobId"]]
                    dist = math.hypot(h["location"][0] - j["location"][0],
                                       h["location"][1] - j["location"][1]) * 111000
                    resolved_by_pair[(pop["residenceId"], pop["jobId"])] = (int(dist / 8.3), int(dist))
            break

        pending_batches = failed_batches
        attempt += 1

    # Fan each pair's resolved result back out to every pop sharing that pair.
    for pop in to_route:
        seconds, distance = resolved_by_pair[(pop["residenceId"], pop["jobId"])]
        pop["drivingSeconds"] = seconds
        pop["drivingDistance"] = distance

    print(f"  -> Routed {len(to_route):,} pop(s).")
    return len(to_route)
