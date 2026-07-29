### ==== SHARED HELPER: DETECT NAMED SPECIAL DEMAND CANDIDATES FROM OSM ====
#
# Scans the already-cleaned .opl file (the same one generate_demand_qzm_local.py
# prepares) for named buildings/areas matching a small set of "special demand"
# categories, and keeps them persisted in the per-city
# {CITY_CODE}_MAP_DATA.json (see bbox_utils.py) under a "special_demand" list.
#
# NOTE: Airports are intentionally NOT auto-detected here. Airports already have
# a separate, working mechanism (AIRPORT_GEOJSON runway/taxiway detection +
# custom_hubs.json + open_maps.py) that snaps real, CSV-derived commuter jobs
# into a terminal-precise mega-hub - that's real workplace-commute data with no
# invented numbers, and stays untouched. If you want a *separate* passenger-
# volume special demand point for an airport (a different, researched number,
# not workplace commuting), you can add a "type": "airport" entry to the JSON
# by hand.
#
# LANDMARK TYPES: university, hospital, stadium, museum, zoo, amusement/water
# parks, plus school and clinic (with a significance filter - see below) -
# individually significant places where a real, researched capacity/
# enrollment/attendance number is a meaningful upgrade over treating the
# building like an ordinary office/residential building.
#
# High-count, low-signal categories (neighborhood sports centres, named
# parks, small ferry docks, "college"-tagged adult-education/vocational
# buildings) are NOT tracked here at all. Every one of them is ALSO already
# flagged is_special=True in generate_demand_qzm_local.py's base OSMHandler
# and gets a boosted share of real CSV-driven commuter demand there - so
# adding a second, independent special-demand point for the same building on
# top of that just double-counted it. At city-wide density (thousands of
# these) that showed up as huge, inflated points sitting on top of each other
# in-game, since depot's add_points() only ever merges two points of the
# *same* special-demand type together, and never with a random nearby one of
# a different type. The base pipeline already models these fine on its own.
#
# school and clinic are numerous too, but unlike park/sports_centre/port,
# they have a real size split worth capturing: a Gymnasium or a genuine
# medical center is a meaningfully bigger, more distinct piece of demand than
# an ordinary building, the same way a real university differs from one of
# its own sub-unit buildings. See _school_is_significant()/
# _clinic_is_significant() - only entries that pass get recorded at all, so
# the flood of small Grundschulen and single-doctor Praxis buildings never
# becomes a candidate in the first place.
#
# Also: no commercial/office/retail/shop tags are matched here at all - those
# only feed the *base* job generation in generate_demand_qzm_local.py (via its
# own JOB_TAGS), which never opens a browser tab.
#
# ONE MORE WRINKLE: big universities tag each faculty/department building
# separately in OSM (e.g. "FB Veterinärmedizin", "FB Geschichts- und
# Kulturwissenschaften" are both Freie Universität Berlin buildings), which
# would otherwise inflate the "university" landmark count the same way ferry
# docks used to inflate "port". Two fixes below: a university-tagged node
# whose own name doesn't read like a real institution (see
# _looks_like_real_institution) is skipped entirely rather than recorded, and
# same-type landmark candidates within CAMPUS_MERGE_RADIUS_DEG of each other
# get merged into one entry (see _merge_nearby_campuses) instead of becoming
# N separate special demand points for what's really one campus.

import os
import random
import re
import math
import subprocess
import osmium

from bbox_utils import load_city_json, save_city_json, bbox_file_path


# (type label, tier, OSM tag key, set of matching tag values)
# Type labels are only used for two things: which depot add_points() "type" we
# pass along later, and helping you tell entries apart in the JSON - they don't
# need to be a perfect taxonomy match.
#
# Every entry is "landmark" tier now (see the module comment above for why the
# old "common" tier - sports_centre/park/port/college - was dropped entirely,
# and why school/clinic stayed but got a significance filter instead). The
# tier field is kept rather than removed so this stays easy to extend later
# without another round of plumbing changes.
TYPE_TAG_RULES = [
    ("university",     "landmark", "amenity", {"university"}),
    ("hospital",       "landmark", "amenity", {"hospital"}),
    ("school",         "landmark", "amenity", {"school"}),
    ("clinic",         "landmark", "amenity", {"clinic"}),
    ("stadium",        "landmark", "leisure", {"stadium"}),
    ("amusement_park", "landmark", "leisure", {"water_park"}),
    ("zoo",            "landmark", "tourism", {"zoo", "aquarium"}),
    ("amusement_park", "landmark", "tourism", {"theme_park"}),
    ("museum",         "landmark", "tourism", {"museum"}),
]
TYPE_TIERS = {label: tier for label, tier, _, _ in TYPE_TAG_RULES}

# Per-city JSONs written before this cleanup may still contain these types
# (plus fake "university" sub-unit entries) - pruned on load, see
# detect_and_confirm_special_demand(). school/clinic are NOT in here anymore -
# they're tracked again (with a significance filter - see below), so existing
# entries of those types stay put rather than getting pruned.
REMOVED_TYPES = {"sports_centre", "park", "port", "college"}

# Auto-estimated capacity ranges for "landmark"-tier types, only used when
# detect_and_confirm_special_demand(..., autofill_landmarks=True) - i.e. you'd
# rather skip individual research entirely for a big city with hundreds of
# real, individually-named institutions/venues (no way to tell "major public
# university" from "small private academy" purely from OSM tags). You can
# always hand-edit the JSON afterward to put a real researched number on the
# handful you actually care about (e.g. FU/HU/TU Berlin) - no browser flow
# needed for that, just edit "total_capacity" and clear "auto_capacity".
# "stadium" is deliberately much lower than you'd guess from famous examples
# (Olympiastadion, Alte Försterei) - OSM tags leisure=stadium on every local
# district-league pitch with any stand at all, so the vast majority of
# auto-detected "stadium" entries in a big city are small neighborhood
# grounds, not professional arenas. A high range here made every one of them
# roll up to 5,000-30,000, which is wildly wrong for that long tail AND still
# wrong for the handful of real ones (random noise instead of their actual,
# well-documented capacity). Hand-edit the JSON for any specific stadium you
# know the real number for (set "total_capacity" + "auto_capacity": false) -
# that's the right place for e.g. Olympiastadion, not this fallback range.
LANDMARK_CAPACITY_RANGES = {
    "university":     (500, 5000),
    "hospital":       (200, 2000),
    "stadium":        (300, 2500),
    "museum":         (500, 3000),
    "zoo":            (1000, 5000),
    "amusement_park": (1000, 5000),
    # school/clinic only ever reach here after passing their significance
    # filter (see _school_is_significant()/_clinic_is_significant()) - a
    # Gymnasium-scale school or a multi-doctor medical center, not the long
    # tail of small Grundschulen/single-practice Praxis buildings that never
    # become candidates at all.
    "school":         (300, 1200),
    "clinic":         (50, 300),
}
LANDMARK_CAPACITY_FALLBACK = (200, 2000)

# Hard cap on how many "landmark"-tier browser tabs get opened in one run.
# Anything beyond this just rolls over to the next run (each entry only ever
# gets its tab opened once, tracked via the "browsed" flag below).
MAX_NEW_TABS_PER_RUN = 15

# Landmark types where a single institution/site legitimately spans many
# separately-tagged OSM nodes (e.g. one university tags each faculty building
# individually). Candidates of the same type within this radius of each other
# get merged into a single entry instead of becoming N special demand points
# for what's really one place. ~600m covers a typical single campus without
# reaching into a genuinely different, nearby-but-separate institution.
CAMPUS_MERGE_TYPES = {"university"}
CAMPUS_MERGE_RADIUS_DEG = 600 / 111_000

# Heuristic for picking which name represents a merged cluster: prefer
# whichever name doesn't look like a department/faculty sub-unit fragment.
_SUBUNIT_NAME_RE = re.compile(
    r"^(FB|Fachbereich|Institut(e)?\s+f[üu]r|Fakult[äa]t|Zentraleinrichtung|ZE)\b",
    re.IGNORECASE,
)

# In practice amenity=university in German OSM data isn't just applied to the
# institution itself - individual institutes, research centers, admin
# departments, workshops, even unrelated facilities on a university's grounds
# all get the same tag (e.g. "Geographisches Institut", "CeDiS", "Zentrale
# Universitätsverwaltung Abteilung Forschung...", or a literal bronze
# foundry). CAMPUS_MERGE_TYPES only helps when a real institution's name is
# nearby to merge into - it does nothing for a standalone sub-unit building
# far from its parent campus. So: a university-tagged node only stays in the
# "landmark" tier if its own name reads like an actual institution (contains
# "Universität"/"Hochschule"/"University"/"College"/"School of"/"Akademie")
# AND doesn't also contain a sub-unit/admin/facility giveaway word (these
# checks are separate because German compounds like "Universitätsverwaltung"
# contain "Universität" as a prefix while still being an admin department,
# not the institution). Anything that fails this is skipped entirely - not
# recorded as a candidate at all.
_UNI_POSITIVE_RE = re.compile(
    r"\b(universit\w*|hochschule|university|college|school of|akademie|academy)\b",
    re.IGNORECASE,
)
_UNI_NEGATIVE_RE = re.compile(
    r"(institut|zentrum|centre|center|fakult[äa]t|fachbereich|abteilung|"
    r"verwaltung|bibliothek|werkstatt|gie[ßs]erei|\blabor\b|klinikum|"
    r"forschungszentrum|sonderforschungsbereich|arbeitsgruppe|lehrstuhl|professur)",
    re.IGNORECASE,
)


def _looks_like_real_institution(name):
    return bool(_UNI_POSITIVE_RE.search(name)) and not _UNI_NEGATIVE_RE.search(name)


# amenity=school covers everything from a 3-classroom Grundschule to a
# 1200-student Gymnasium. isced:level (0=pre-primary .. 3=upper secondary) or
# school:type, when present, are the most reliable signal - level 2+ or a
# type like "gymnasium" reliably means a bigger, more distinct campus than a
# small neighborhood elementary school. Most OSM schools don't have either
# tag though, so this falls back to a name check (same idea as the
# university filter above), and finally to "does it have a capacity tag at
# all" - someone bothering to tag a real number is itself a signal this one
# was considered notable enough to measure.
_SCHOOL_POSITIVE_TYPES = {
    "gymnasium", "gesamtschule", "oberschule", "sekundarschule",
    "berufsschule", "kolleg", "international",
}
_SCHOOL_NEGATIVE_TYPES = {"grundschule", "foerderschule", "förderschule"}
_SCHOOL_NAME_POSITIVE_RE = re.compile(
    r"\b(gymnasium|gesamtschule|oberschule|sekundarschule|berufsschule|"
    r"international school|akademie|academy|kolleg)\b",
    re.IGNORECASE,
)


def _school_is_significant(tags, name):
    isced = tags.get("isced:level")
    if isced:
        try:
            # occasionally a list like "1;2" - take the highest level given
            level = max(int(v) for v in str(isced).split(";") if v.strip().lstrip("-").isdigit())
            return level >= 2
        except ValueError:
            pass
    school_type = (tags.get("school:type") or "").strip().lower()
    if school_type in _SCHOOL_POSITIVE_TYPES:
        return True
    if school_type in _SCHOOL_NEGATIVE_TYPES:
        return False
    if tags.get("capacity"):
        return True
    return bool(_SCHOOL_NAME_POSITIVE_RE.search(name))


# amenity=clinic has no equivalent size-tier tag at all (unlike school) - the
# overwhelming majority are single- or few-doctor outpatient practices, which
# aren't landmarks in any meaningful sense. Only keep ones that either have a
# "beds" tag (meaning it actually functions closer to a small hospital) or a
# name that reads like a genuine multi-practice medical center rather than
# "Praxis Dr. <Name>".
_CLINIC_NAME_POSITIVE_RE = re.compile(
    r"\b(klinik|poliklinik|mvz|medizinisches versorgungszentrum|"
    r"gesundheitszentrum|facharztzentrum|[äa]rztehaus)\b",
    re.IGNORECASE,
)


def _clinic_is_significant(tags, name):
    beds = tags.get("beds")
    if beds:
        try:
            if int(str(beds).replace(",", "").strip()) > 0:
                return True
        except ValueError:
            pass
    if tags.get("capacity"):
        return True
    return bool(_CLINIC_NAME_POSITIVE_RE.search(name))


def _classify(tags):
    for type_label, tier, key, values in TYPE_TAG_RULES:
        if tags.get(key) in values:
            return type_label
    return None


class SpecialDemandScanner(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = bbox
        self.found = []  # list of dicts: name, type, location, total_capacity, auto_capacity

    def _maybe_record(self, tags, lat, lon):
        if not (self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon):
            return
        name = tags.get("name")
        if not name:
            return  # unnamed features aren't useful as named landmarks
        type_label = _classify(tags)
        if type_label is None:
            return

        # university-tagged nodes whose own name doesn't read like an actual
        # institution (see _looks_like_real_institution) are skipped entirely
        # - see the comment above _UNI_POSITIVE_RE for why.
        if type_label == "university" and not _looks_like_real_institution(name):
            return
        if type_label == "school" and not _school_is_significant(tags, name):
            return
        if type_label == "clinic" and not _clinic_is_significant(tags, name):
            return

        capacity = None
        cap_tag = tags.get("capacity")
        if cap_tag:
            try:
                capacity = int(str(cap_tag).replace(",", "").strip())
            except ValueError:
                capacity = None

        self.found.append({
            "name": name,
            "type": type_label,
            "location": [round(lon, 6), round(lat, 6)],
            "total_capacity": capacity,
            "auto_capacity": False,
        })

    def node(self, n):
        if len(n.tags) > 0:
            self._maybe_record(n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if len(w.tags) > 0:
            try:
                self._maybe_record(w.tags, w.nodes[0].location.lat, w.nodes[0].location.lon)
            except osmium.InvalidLocationError:
                pass


def _dedup_key(entry):
    return (entry["name"].strip().lower(), entry["type"])


def _format_breakdown(entries, indent="    "):
    """Multi-line, indented 'type  count' breakdown (one type per line, most
    common first, counts right-aligned) - used instead of printing one line
    per entry, which is unreadable once a city has hundreds."""
    if not entries:
        return f"{indent}(none)"
    counts = {}
    for e in entries:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    name_width = max(len(t) for t in counts)
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    return "\n".join(f"{indent}{t.ljust(name_width)}  {n:>5,}" for t, n in rows)


def _pick_campus_representative(group):
    """Picks which entry's name/location represents a merged cluster: prefer
    one that doesn't look like a department/faculty sub-unit fragment, then
    the longest name (fuller official names tend to beat abbreviations)."""
    non_subunit = [g for g in group if not _SUBUNIT_NAME_RE.match(g["name"].strip())]
    pool = non_subunit if non_subunit else group
    return max(pool, key=lambda g: len(g["name"]))


def _merge_nearby_campuses(entries):
    """Merges same-type candidates within CAMPUS_MERGE_RADIUS_DEG of each
    other (for types in CAMPUS_MERGE_TYPES) into a single entry, so e.g. a
    dozen separately-tagged faculty buildings of one university don't become
    a dozen separate special demand points."""
    to_merge = [e for e in entries if e["type"] in CAMPUS_MERGE_TYPES]
    unmerged = [e for e in entries if e["type"] not in CAMPUS_MERGE_TYPES]

    merged = []
    used = [False] * len(to_merge)
    for i, e in enumerate(to_merge):
        if used[i]:
            continue
        group = [e]
        used[i] = True
        for j in range(i + 1, len(to_merge)):
            if used[j] or to_merge[j]["type"] != e["type"]:
                continue
            o = to_merge[j]
            dist_deg = math.hypot(e["location"][0] - o["location"][0],
                                   e["location"][1] - o["location"][1])
            if dist_deg <= CAMPUS_MERGE_RADIUS_DEG:
                group.append(o)
                used[j] = True

        if len(group) == 1:
            merged.append(group[0])
            continue

        rep = dict(_pick_campus_representative(group))
        rep["merged_count"] = len(group)
        rep["merged_names"] = [g["name"] for g in group if g["name"] != rep["name"]]
        known_caps = [g["total_capacity"] for g in group
                      if g.get("total_capacity") is not None and not g.get("auto_capacity")]
        if known_caps:
            rep["total_capacity"] = known_caps[0]
        merged.append(rep)

    return merged + unmerged


def scan_for_candidates(opl_file, bbox):
    """Scans opl_file for named special-demand candidates within bbox."""
    scanner = SpecialDemandScanner(bbox)
    scanner.apply_file(opl_file, locations=True)

    # De-duplicate features seen more than once (e.g. a way + a node for the
    # same building), keeping the first occurrence and preferring one with a
    # real (non-auto) capacity if a later duplicate has one and the first doesn't.
    by_key = {}
    for entry in scanner.found:
        key = _dedup_key(entry)
        if key not in by_key:
            by_key[key] = entry
        elif by_key[key].get("auto_capacity") and not entry.get("auto_capacity"):
            by_key[key]["total_capacity"] = entry["total_capacity"]
            by_key[key]["auto_capacity"] = entry["auto_capacity"]

    return _merge_nearby_campuses(list(by_key.values()))


def _open_url_detached(url):
    """Opens a URL via xdg-open in a fully detached process (same pattern as
    open_maps.py) so the script doesn't block waiting on the browser."""
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"  [ERROR] Could not launch browser for '{url}': {e}")


def open_candidates_in_browser(entries):
    for entry in entries:
        lon, lat = entry["location"]
        print(f" -> Opening {entry['name']} ({entry['type']}): ({lat}, {lon})")
        url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        _open_url_detached(url)


def detect_and_confirm_special_demand(city_code, raw_base_dir, opl_file, bbox,
                                       autofill_landmarks=False):
    """
    Scans opl_file for new named special-demand candidates and merges any new
    ones into the per-city JSON.

    Only landmark-shaped candidates are tracked at all (universities, real
    hospitals, stadiums, museums, zoos, theme/water parks - see the module
    comment at the top of this file). They normally get a browser tab and a
    pause so you can fill in a real number - capped at MAX_NEW_TABS_PER_RUN
    per run (across new AND previously-queued-but-unopened ones) so this
    never floods your browser. Anything past the cap just rolls over to next
    run.

    autofill_landmarks: bool. If True, skips the browser/review flow entirely
        and auto-estimates a capacity for every landmark entry too (see
        LANDMARK_CAPACITY_RANGES) - useful for a big city where there are just
        too many distinctly-named real institutions/venues to research one by
        one. You can still hand-edit the JSON afterward for the handful you
        actually want a real number on.
        Default: False

    Returns the list of special_demand entries that currently have a non-null
    total_capacity - i.e. the ones ready to actually be used.
    """
    data = load_city_json(city_code, raw_base_dir)
    existing = data["special_demand"]
    path = bbox_file_path(city_code, raw_base_dir)

    # One-time cleanup for per-city JSONs written before "common"-tier types
    # (park/sports_centre/port/college) were dropped entirely - see the
    # module comment at the top of this file for why. Also prunes
    # "university" entries that don't read like a real institution, and
    # school/clinic entries that fail their own significance filter (see
    # _school_is_significant()/_clinic_is_significant()) - covers both
    # entries from before those filters existed, and any hand-added ones.
    # Must happen BEFORE existing_keys is computed below (there is no
    # existing_keys computation left that depends on this order anymore, but
    # keeping it first avoids ever showing/using stale entries).
    def _still_wanted(e):
        etype, name = e.get("type"), e.get("name", "")
        if etype in REMOVED_TYPES:
            return False
        if etype == "university" and not _looks_like_real_institution(name):
            return False
        if etype == "school" and not _school_is_significant(e.get("tags", {}), name):
            return False
        if etype == "clinic" and not _clinic_is_significant(e.get("tags", {}), name):
            return False
        return True

    before_count = len(existing)
    existing[:] = [e for e in existing if _still_wanted(e)]
    pruned = before_count - len(existing)
    if pruned:
        data["special_demand"] = existing
        save_city_json(city_code, raw_base_dir, data)
        print(f"Pruned {pruned:,} entr{'y' if pruned == 1 else 'ies'} for types "
              f"no longer tracked as individual special demand (park/"
              f"sports_centre/college/port - the base pipeline already "
              f"models these via CSV data, so a separate point just double-"
              f"counted them), fake 'university' sub-unit buildings, or "
              f"schools/clinics that don't pass the significance filter. "
              f"Your in-game map won't reflect this until you regenerate it.")

    current_types = set(TYPE_TIERS.keys())
    scanned_types = set(data.get("scanned_types", []))
    missing_types = current_types - scanned_types

    if existing and not missing_types:
        # Already scanned for everything this version of the tool tracks -
        # skip re-parsing the (large) OPL file, the expensive part. Delete
        # entries, the whole file, or just the "scanned_types" key, to force
        # a fresh scan for this city.
        print(f"\n'{path}' already has {len(existing)} special demand entr"
              f"{'y' if len(existing) == 1 else 'ies'} covering every "
              f"currently-tracked type - skipping the OSM scan for new "
              f"candidates.")
    else:
        if existing:
            print(f"\n'{path}' has {len(existing)} entr"
                  f"{'y' if len(existing) == 1 else 'ies'} already, but hasn't "
                  f"been scanned for: {', '.join(sorted(missing_types))}. "
                  f"Scanning once to pick those up - existing entries "
                  f"(including any real numbers you've hand-set) are left "
                  f"untouched.")
        existing_keys = {_dedup_key(e) for e in existing}

        print(f"\nScanning '{opl_file}' for named special demand candidates...")
        candidates = scan_for_candidates(opl_file, bbox)
        new_entries = [c for c in candidates if _dedup_key(c) not in existing_keys]

        if new_entries:
            for e in new_entries:
                e["browsed"] = False

            print(f"\nFound {len(new_entries):,} new special demand candidate(s) "
                  f"needing a real number:")
            print(_format_breakdown(new_entries))

            existing.extend(new_entries)

        data["special_demand"] = existing
        data["scanned_types"] = sorted(current_types)
        save_city_json(city_code, raw_base_dir, data)
        print(f"\nSaved to '{path}'.")

    if autofill_landmarks:
        # Skip the browser/review flow entirely - auto-estimate a capacity
        # for every landmark entry still missing one, whether it was just
        # added above, queued from a previous run, or already had a tab
        # opened for it that you never got around to filling in.
        to_autofill = [
            e for e in data["special_demand"]
            if TYPE_TIERS.get(e.get("type")) == "landmark" and e.get("total_capacity") is None
        ]
        if to_autofill:
            for e in to_autofill:
                lo, hi = LANDMARK_CAPACITY_RANGES.get(e["type"], LANDMARK_CAPACITY_FALLBACK)
                e["total_capacity"] = random.randint(lo, hi)
                e["auto_capacity"] = True
                e.pop("browsed", None)
            save_city_json(city_code, raw_base_dir, data)
            print(f"\n--autofill-special-demand: auto-estimated capacity for "
                  f"{len(to_autofill)} landmark entr"
                  f"{'y' if len(to_autofill) == 1 else 'ies'} instead of opening "
                  f"browser tabs. Hand-edit the JSON afterward for any specific "
                  f"ones you want a real researched number on (e.g. clear "
                  f"\"auto_capacity\" once you set a real \"total_capacity\").")
    else:
        # Landmark entries still missing a real number, that haven't had their
        # browser tab opened yet (whether just added above or queued from a
        # previous, capped-out run).
        pending = [
            e for e in data["special_demand"]
            if TYPE_TIERS.get(e.get("type")) == "landmark"
            and e.get("total_capacity") is None
            and not e.get("browsed")
        ]

        if pending:
            to_open = pending[:MAX_NEW_TABS_PER_RUN]
            rollover = len(pending) - len(to_open)

            print(f"\nOpening {len(to_open)} location(s) in your browser so you can "
                  f"double-check the pin and fill in a real capacity/enrollment/"
                  f"attendance number..."
                  + (f" ({rollover} more queued for future run(s) to avoid opening "
                     f"too many tabs at once)" if rollover else ""))
            open_candidates_in_browser(to_open)
            for e in to_open:
                e["browsed"] = True
            save_city_json(city_code, raw_base_dir, data)

            input(
                f"\nOpen '{path}' and fill in \"total_capacity\" (and adjust "
                f"\"location\" if a pin looks off) for the entries just opened.\n"
                f"Press Enter once you're done - or just leave it for later, "
                f"nothing is lost.\n"
            )

            # Re-read from disk - the user may have edited the file while we waited.
            data = load_city_json(city_code, raw_base_dir)

    all_entries = data["special_demand"]
    ready = [e for e in all_entries if e.get("total_capacity") is not None]
    missing = [e for e in all_entries if e.get("total_capacity") is None]
    auto_ready = [e for e in ready if e.get("auto_capacity")]
    researched_ready = [e for e in ready if not e.get("auto_capacity")]

    if missing:
        print(f"\nStill missing a capacity for {len(missing):,} entr"
              f"{'y' if len(missing) == 1 else 'ies'}:")
        print(_format_breakdown(missing))
        print(f"No worries - continuing without those for now. Fill them in, "
              f"or just re-run (up to {MAX_NEW_TABS_PER_RUN} more get a browser "
              f"tab automatically each time) whenever you're ready.")
    if ready:
        print(f"\nUsing {len(ready):,} special demand point(s):")
        if researched_ready:
            print(f"  Real/researched numbers ({len(researched_ready):,} total):")
            print(_format_breakdown(researched_ready))
        if auto_ready:
            print(f"  Auto-estimated ({len(auto_ready):,} total):")
            print(_format_breakdown(auto_ready))

    return ready
