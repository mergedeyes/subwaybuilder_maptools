### ==== ZENSUS 2022 100m POPULATION GRID LOADER ====
#
# Loads Destatis's real, measured population count per 100m INSPIRE grid
# cell ("Bevoelkerungszahlen in Gitterzellen", Zensus 2022) and exposes a
# spatial lookup from (lon, lat) -> that cell's real population count.
#
# WHY: generate_demand_qzm_local.py currently decides WHERE within a QZM
# 1km commuter cell residents get placed using OSM-tag-based heuristic
# capacity ranges (CAPACITY_APARTMENTS/HOUSES - arbitrary random ranges,
# scaled by building:levels). Zensus 2022 instead reports a REAL, MEASURED
# population headcount at 100m resolution - 100x finer than the QZM cell
# and not an estimate at all. Using it as the gravity weight for
# residential placement replaces a guess with a measurement.
#
# Download: https://www.destatis.de/static/DE/zensus/gitterdaten/Zensus2022_Bevoelkerungszahl.zip
# (nationwide, OpenData/CC-BY-style attribution license - the zip also
# contains 10km/1km CSVs for the same theme, neither needed here; only the
# *_100m.csv is used). See zensus2022.de/DE/Ergebnisse-des-Zensus/gitterzellen.html
# for the general grid data catalog if this direct link ever moves.
#
# Column names below are DEFENSIVELY DETECTED (pattern-matched) rather than
# hardcoded - Destatis only serves the exact format spec as a binary xlsx,
# which couldn't be read from here to confirm column names ahead of time.
# The pattern below matches the naming convention Zensus grid products have
# used consistently since Zensus 2011 (Gitter_ID_100m / x_mp_100m /
# y_mp_100m + one theme value column) - if the real file's header doesn't
# match, _detect_columns() raises with the actual header included in the
# error so the pattern can be corrected against the real file rather than
# failing silently or on a wrong column.

import csv


# Confirmed against Destatis's own "Erläuterung zur CSV-Tabelle" for this
# table: suppressed (privacy-redacted) cells are NOT represented by a
# sentinel number - the value column contains a special character instead
# of a number for those rows ("Die Datenzeilen der Werte-Spalten koennen
# anstelle von Zahlen auch Sonderzeichen enthalten..."). Since the exact
# character isn't pinned down here, any value that doesn't parse cleanly as
# a number is treated as suppressed/unavailable - see the parse try/except
# below - rather than guessing a specific symbol. A successfully-parsed
# negative number (which should never occur for a population count) is
# also treated as suppressed defensively, in case some other convention is
# mixed in.

# The Zensus grid is natively defined in ETRS89/LAEA Europe (EPSG:3035)
# meters (the INSPIRE standard for European statistical grids), not lon/
# lat, so cell coordinates need reprojecting before they can be compared
# against OSM buildings. Uses pyproj.Transformer, already a dependency of
# generate_demand_qzm_local.py.
_LAEA_EPSG = "EPSG:3035"
_WGS84_EPSG = "EPSG:4326"


def _detect_columns(header):
    """
    Matches (id_col, x_col, y_col, value_col) indices by pattern rather
    than hardcoding - see the module comment above for why. Raises with the
    actual header quoted if detection fails, so the mismatch is obvious and
    fixable against a real downloaded file instead of silently misreading
    the wrong column.
    """
    lower = [h.strip().lower() for h in header]

    def find(*needles):
        for i, h in enumerate(lower):
            if all(n in h for n in needles):
                return i
        return None

    id_idx = find("gitter_id")
    x_idx = find("x_mp")
    y_idx = find("y_mp")
    value_idx = find("einwohner")
    if value_idx is None:
        # Fallback: whatever column is left over once id/x/y are accounted
        # for, on the assumption (true of every Zensus grid theme file seen
        # so far) that this table has exactly one measured value column.
        used = {id_idx, x_idx, y_idx}
        candidates = [i for i in range(len(header)) if i not in used]
        value_idx = candidates[0] if len(candidates) == 1 else None

    missing = [name for name, idx in
               [("Gitter_ID", id_idx), ("x_mp", x_idx), ("y_mp", y_idx), ("value", value_idx)]
               if idx is None]
    if missing:
        raise ValueError(
            f"Couldn't confidently detect column(s) {missing} in the Zensus "
            f"grid CSV header {header!r} - the real file's column names "
            f"didn't match the expected Gitter_ID_100m/x_mp_100m/"
            f"y_mp_100m/Einwohner pattern. Update _detect_columns() to "
            f"match this actual header.")
    return id_idx, x_idx, y_idx, value_idx


class ZensusPopulationGrid:
    """
    Spatial lookup from (lon, lat) -> the real Zensus 2022 measured
    population count of the 100m grid cell containing that point, or None
    if that cell was suppressed (small population, privacy-redacted) or
    simply has no data (uninhabited terrain, or outside the loaded bbox).

    Only loads cells within `bbox` ([min_lon, min_lat, max_lon, max_lat])
    if given, to avoid holding every one of Germany's several million 100m
    cells in memory when only a single city's worth are ever queried. The
    bbox is reprojected to EPSG:3035 ONCE up front and every row is then
    filtered with a cheap numeric range check on its native x_mp/y_mp
    columns - not by reprojecting every row's coordinates to lon/lat just
    to throw most of them away, which matters here since this file covers
    all of Germany at 100m resolution (millions of rows) for what a single
    city query only needs a tiny slice of.
    """
    def __init__(self, csv_path, bbox=None, delimiter=None):
        try:
            from pyproj import Transformer
        except ImportError as e:
            raise ImportError(
                "pyproj is required to reproject the Zensus grid's native "
                "EPSG:3035 coordinates to lon/lat - already a dependency of "
                "generate_demand_qzm_local.py.") from e

        self.cell_size_m = 100
        self.cells = {}  # (grid_x, grid_y) in EPSG:3035 meters -> population count

        if delimiter is None:
            with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                sample = f.readline()
            delimiter = ";" if sample.count(";") >= sample.count(",") else ","

        to_laea = Transformer.from_crs(_WGS84_EPSG, _LAEA_EPSG, always_xy=True)

        laea_bbox = None
        if bbox is not None:
            corners = [(bbox[0], bbox[1]), (bbox[0], bbox[3]),
                       (bbox[2], bbox[1]), (bbox[2], bbox[3])]
            xs, ys = zip(*(to_laea.transform(lon, lat) for lon, lat in corners))
            laea_bbox = (min(xs), min(ys), max(xs), max(ys))

        loaded = 0
        skipped_suppressed = 0
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter=delimiter)
            header = next(reader)
            id_idx, x_idx, y_idx, value_idx = _detect_columns(header)

            for row in reader:
                if not row or len(row) <= max(id_idx, x_idx, y_idx, value_idx):
                    continue

                try:
                    x_m = float(row[x_idx].strip().replace(",", "."))
                    y_m = float(row[y_idx].strip().replace(",", "."))
                except ValueError:
                    continue

                if laea_bbox is not None and not (
                        laea_bbox[0] <= x_m <= laea_bbox[2] and
                        laea_bbox[1] <= y_m <= laea_bbox[3]):
                    continue

                raw_value = row[value_idx].strip()
                try:
                    value = int(float(raw_value.replace(",", ".")))
                except ValueError:
                    # Non-numeric value column - the suppression marker for
                    # this table (see module comment above).
                    skipped_suppressed += 1
                    continue
                if value < 0:
                    # Should never legitimately occur for a population
                    # count - treated as suppressed defensively too.
                    skipped_suppressed += 1
                    continue

                key = (int(x_m // self.cell_size_m), int(y_m // self.cell_size_m))
                self.cells[key] = value
                loaded += 1

        print(f"  -> Loaded {loaded:,} Zensus 2022 100m grid cell(s) with "
              f"real population counts"
              + (f" ({skipped_suppressed:,} suppressed cell(s) skipped)"
                 if skipped_suppressed else "")
              + (" within the map bbox." if bbox is not None else "."))

        self._to_laea = to_laea

    def population_at(self, lon, lat):
        """
        Returns the real measured population count of the 100m cell
        containing (lon, lat), or None if that cell was suppressed, never
        published, or outside the loaded extent. Callers should fall back
        to their own heuristic for any building this returns None for -
        this is a refinement signal, not a guaranteed value everywhere.
        """
        x_m, y_m = self._to_laea.transform(lon, lat)
        key = (int(x_m // self.cell_size_m), int(y_m // self.cell_size_m))
        return self.cells.get(key)

    def cell_key(self, lon, lat):
        """
        Returns the same (grid_x, grid_y) key population_at() looks up
        internally - exposed so callers that need to GROUP buildings by
        Zensus cell (rather than just read one value) don't have to
        duplicate the coordinate transform/bucketing logic.
        """
        x_m, y_m = self._to_laea.transform(lon, lat)
        return (int(x_m // self.cell_size_m), int(y_m // self.cell_size_m))


def calibrate_residential_capacity(home_nodes, zensus_grid):
    """
    Rescales each home node's heuristic capacity (assigned in
    generate_demand_qzm_local.py's OSMHandler.process_element() from
    CAPACITY_APARTMENTS/HOUSES/HOTEL/YES_WILDCARD - arbitrary random
    ranges) so that every group of home nodes sharing the same real Zensus
    100m grid cell sums to that cell's REAL MEASURED population, instead of
    an arbitrary heuristic total - while preserving each node's RELATIVE
    share within the group (an apartment block still gets proportionally
    more than a house next to it; only the group's total is corrected, not
    the type differentiation process_element() already encodes).

    This is what moves residential placement from "generic priors" to
    "locally calibrated against real statistics" - see the data-quality
    writeup this was built for: the heuristic capacity ranges alone are
    rule-of-thumb, not checked against anything real; this ties them back
    to an actual measured headcount wherever Zensus published one.

    Home nodes whose 100m cell has no Zensus value (suppressed for privacy,
    or genuinely no data for that cell) are left with their heuristic
    capacity unchanged - this is a refinement where real data exists, not a
    requirement that it exists everywhere. Mutates `home_nodes` in place
    (each node's "capacity" field) and returns it.

    Note: a building sitting very close to a 100m cell boundary could in
    principle get attributed to the "wrong" side of that boundary if its
    OSM coordinate and the true building footprint disagree slightly -
    capacity is floored at 1 (never rounded to 0) specifically so a
    boundary mis-attribution can't silently erase a building's population
    rather than just under/over-stating it a little.
    """
    if zensus_grid is None:
        return home_nodes

    groups = {}
    ungrouped = 0
    for node in home_nodes:
        real_pop = zensus_grid.population_at(node["lon"], node["lat"])
        if real_pop is None:
            ungrouped += 1
            continue
        key = zensus_grid.cell_key(node["lon"], node["lat"])
        groups.setdefault(key, (real_pop, []))[1].append(node)

    rescaled = 0
    for real_pop, nodes in groups.values():
        heuristic_total = sum(n["capacity"] for n in nodes)
        if heuristic_total <= 0:
            continue
        for n in nodes:
            share = n["capacity"] / heuristic_total
            n["capacity"] = max(1, round(real_pop * share))
        rescaled += len(nodes)

    print(f"  -> Calibrated {rescaled:,} residential building capacit"
          f"{'y' if rescaled == 1 else 'ies'} against real Zensus 2022 100m "
          f"population counts ({ungrouped:,} left at the OSM-heuristic "
          f"estimate - no Zensus data for their cell).")
    return home_nodes
