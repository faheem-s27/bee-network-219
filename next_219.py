"""
Next 219 bus tracker - proof of concept.

Pulls the DfT BODS SIRI-VM live vehicle-position feed, filters to the 219
heading toward Manchester past your stop (Openshaw, near Lees Street), and
prints a rough ETA for the next few buses.

HONESTY NOTE: BODS publishes GPS positions, not arrival predictions (unless the
operator bothers to compute stop-level predictions and put them in the feed).
So unless the feed carries an ExpectedArrivalTime for your exact stop, the ETA
here is: straight-line distance / an assumed average speed. That is wrong on
bends, one-way systems, and in traffic. Treat the geometry-based number as
"roughly N minutes, low confidence", not gospel. Failure modes are listed at
the bottom of this file.
"""

import csv
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")

import requests
import xml.etree.ElementTree as ET

# ============================ CONFIG ============================

def _load_api_key():
    """Read the BODS key from $BODS_API_KEY or a local api_key.txt (gitignored),
    so it never lives in the source you push to GitHub."""
    v = os.environ.get("BODS_API_KEY")
    if v:
        return v.strip()
    if os.path.exists("api_key.txt"):
        with open("api_key.txt", encoding="utf-8") as f:
            return f.read().strip()
    return ""


API_KEY = _load_api_key()

# Route/stop settings live in route_config.py (the one file you edit to switch).
from route_config import (
    LEGS, LINE_REF, OPERATOR_NOC, STOP_NAME, STOP_ATCO, STOP_LAT, STOP_LON,
    DIRECTION, DEST_KEYWORDS, DEST_LABEL,
)
INBOUND_DIRECTION_REF = DIRECTION    # alias used by direction_ok()
_ALL_STOP_ATCOS = {leg["stop_atco"] for leg in LEGS}

# Geometry-ETA assumption. ~18 km/h is a typical urban bus average INCLUDING
# stop dwell time. Tune it once you can compare estimate vs reality.
ASSUMED_SPEED_KMH = 18.0

# Approaching filter: if the bus's heading is more than this many degrees off the
# direction to your stop, treat it as moving away (already passed, or wrong road)
# and drop it. Only applied when the feed gives us a Bearing.
APPROACH_BEARING_TOLERANCE_DEG = 100.0

# How many arrivals to print.
SHOW_N = 5

# Optional learned calibration (a calibrate.Calibration). When set, the fallback
# ETA uses the learned effective speed instead of ASSUMED_SPEED_KMH. Left None
# here; the GUI loads it from the accuracy log and assigns it.
CALIBRATION = None

# Bounding box around the stop. Format that BODS wants: minLon,minLat,maxLon,maxLat
# These are a SAFE FALLBACK (~4km either side) used before the timetable is
# available. Once it warms, refresh_bbox_from_timetable() widens this to cover
# every stop actually on the route (the real 219 spans ~19km end to end,
# Piccadilly to Ashton/Stalybridge/Glossop) - derived from the published data,
# not a guessed padding constant. A too-small box was causing false missed-bus
# alarms: a bus that had genuinely just left the terminus was invisible to us
# for several minutes, indistinguishable from a no-show.
_BBOX_BUFFER_DEG = 0.01   # ~0.7-1km past the outermost tracked stop
BBOX_MIN_LON = STOP_LON - 0.06
BBOX_MAX_LON = STOP_LON + 0.06
BBOX_MIN_LAT = STOP_LAT - 0.03
BBOX_MAX_LAT = STOP_LAT + 0.03


def refresh_bbox_from_timetable():
    """Widen the box to cover every stop on every tracked leg's route, using
    coordinates from the already-downloaded timetable. Cheap and safe to call
    repeatedly (no network calls). Returns False (leaving the fallback box in
    place) if that data is not ready yet."""
    global BBOX_MIN_LON, BBOX_MAX_LON, BBOX_MIN_LAT, BBOX_MAX_LAT
    import schedule_219 as sch   # local import: schedule_219 imports this module
    lats, lons = [], []
    for leg in LEGS:
        for lat, lon in sch.all_stop_coords(leg["direction"], leg["stop_atco"]):
            lats.append(lat)
            lons.append(lon)
    if not lats:
        return False
    BBOX_MIN_LON = min(lons) - _BBOX_BUFFER_DEG
    BBOX_MAX_LON = max(lons) + _BBOX_BUFFER_DEG
    BBOX_MIN_LAT = min(lats) - _BBOX_BUFFER_DEG
    BBOX_MAX_LAT = max(lats) + _BBOX_BUFFER_DEG
    return True

BODS_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"

# Poll once, or loop forever. Do not poll faster than ~15s; the underlying data
# does not refresh faster than that, you would just hammer the API for nothing.
# The accuracy logger only does useful work in LOOP mode (it needs time to watch
# a bus go from "predicted" to "actually arrived").
LOOP = True
POLL_SECONDS = 20

# Accuracy logger. A bus counts as "arrived" once it comes within this radius of
# the stop. 60m absorbs GPS jitter while still being clearly "at the stop".
ACCURACY_LOG = True
ACCURACY_LOG_PATH = "eta_accuracy_log.csv"
# One row per actual arrival with the EXACT delay (vs timetable) at the stop.
# This is the trustworthy source for reliability stats.
ARRIVALS_LOG_PATH = "arrivals_log.csv"
ARRIVAL_RADIUS_KM = 0.06
# After a bus arrives we ignore it for this long, so sitting at the stop or
# looping back later on its next trip does not double-count.
ARRIVAL_COOLDOWN_MIN = 10

# SIRI XML default namespace.
NS = {"s": "http://www.siri.org.uk/siri"}

# ============================ GEOMETRY ============================

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. This is straight-line, NOT road distance."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing from point 1 to point 2, degrees 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a, b):
    """Smallest absolute difference between two bearings, 0..180."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d

# ============================ FEED ============================

def fetch_feed():
    params = {
        "api_key": API_KEY,
        "boundingBox": f"{BBOX_MIN_LON},{BBOX_MIN_LAT},{BBOX_MAX_LON},{BBOX_MAX_LAT}",
        "lineRef": LINE_REF,  # server-side hint; we still filter client-side
    }
    resp = requests.get(BODS_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def text_of(elem, path):
    node = elem.find(path, NS)
    return node.text.strip() if node is not None and node.text else None


def parse_time(s):
    """Parse a SIRI ISO8601 timestamp into an aware datetime, or None."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_vehicles(xml_text):
    """Yield a dict per VehicleActivity matching our line."""
    root = ET.fromstring(xml_text)
    for va in root.iterfind(".//s:VehicleActivity", NS):
        mvj = va.find("s:MonitoredVehicleJourney", NS)
        if mvj is None:
            continue

        line = text_of(mvj, "s:LineRef") or text_of(mvj, "s:PublishedLineName")
        if line != LINE_REF:
            continue

        lat = text_of(mvj, "s:VehicleLocation/s:Latitude")
        lon = text_of(mvj, "s:VehicleLocation/s:Longitude")
        if lat is None or lon is None:
            continue

        bearing = text_of(mvj, "s:Bearing")

        # Look for an operator-provided arrival time at any of our tracked stops,
        # in either the current MonitoredCall or any OnwardCall. Keyed by ATCO so
        # each leg can pick out its own stop's value.
        expected_by_atco = {}
        for call_path in ("s:MonitoredCall", "s:OnwardCalls/s:OnwardCall"):
            for call in mvj.iterfind(call_path, NS):
                ref = text_of(call, "s:StopPointRef")
                if ref in _ALL_STOP_ATCOS and ref not in expected_by_atco:
                    t = (parse_time(text_of(call, "s:ExpectedArrivalTime"))
                         or parse_time(text_of(call, "s:AimedArrivalTime")))
                    if t:
                        expected_by_atco[ref] = t

        yield {
            "vehicle": text_of(mvj, "s:VehicleRef"),
            "direction_ref": text_of(mvj, "s:DirectionRef"),
            "destination": text_of(mvj, "s:DestinationName")
                           or text_of(mvj, "s:DestinationRef"),
            # Journey identity + schedule for the WHOLE journey (origin/dest only,
            # not our stop). Kept for a future timetable cross-reference.
            "journey_ref": text_of(mvj, "s:FramedVehicleJourneyRef/s:DatedVehicleJourneyRef"),
            "origin_aimed_dep": parse_time(text_of(mvj, "s:OriginAimedDepartureTime")),
            "dest_aimed_arr": parse_time(text_of(mvj, "s:DestinationAimedArrivalTime")),
            "lat": float(lat),
            "lon": float(lon),
            "bearing": float(bearing) if bearing else None,
            "recorded": parse_time(text_of(va, "s:RecordedAtTime")),
            "expected_by_atco": expected_by_atco,
        }

# ============================ ETA LOGIC ============================

_DEFAULT_LEG = LEGS[0]


def estimate_for(v, now, leg=_DEFAULT_LEG):
    """Attach distance, approaching flag, eta_min, and source to a vehicle dict,
    relative to the given leg's stop. Only call this on vehicles already known
    to belong to that leg's direction."""
    dist = haversine_km(v["lat"], v["lon"], leg["stop_lat"], leg["stop_lon"])
    v["dist_km"] = dist

    # Is it heading toward the stop? Only knowable if we have a bearing.
    to_stop = bearing_deg(v["lat"], v["lon"], leg["stop_lat"], leg["stop_lon"])
    if v["bearing"] is None:
        v["approaching"] = None  # unknown
    else:
        v["approaching"] = angle_diff(v["bearing"], to_stop) <= APPROACH_BEARING_TOLERANCE_DEG

    v["eta_band"] = None
    expected_at_stop = v.get("expected_by_atco", {}).get(leg["stop_atco"])
    if expected_at_stop is not None:
        v["eta_min"] = (expected_at_stop - now).total_seconds() / 60.0
        v["source"] = "operator"  # someone else's prediction, relayed
    elif CALIBRATION is not None:
        v["eta_min"] = CALIBRATION.eta(dist)
        v["eta_band"] = CALIBRATION.band_min   # 1-sigma, minutes
        v["source"] = "learned"   # calibrated straight-line, bias removed
    else:
        v["eta_min"] = (dist / ASSUMED_SPEED_KMH) * 60.0
        v["source"] = "estimate"  # raw straight-line guess, low confidence
    return v


def estimate(v, now):
    """Back-compat: estimate relative to the first configured leg."""
    return estimate_for(v, now, _DEFAULT_LEG)


def dest_matches_for(v, leg=_DEFAULT_LEG):
    d = (v["destination"] or "").lower()
    if not d:
        return None  # unknown, cannot tell direction from destination
    return any(k in d for k in leg["dest_keywords"])


def dest_matches(v):
    return dest_matches_for(v, _DEFAULT_LEG)


def direction_ok_for(v, leg=_DEFAULT_LEG):
    """True if this vehicle is heading the way this leg cares about, False if
    not, None if undeterminable.

    Primary signal is DirectionRef (structured). If it is missing, fall back to
    matching the destination text.
    """
    dr = (v["direction_ref"] or "").lower()
    if dr == leg["direction"]:
        return True
    if dr:  # some other direction string
        return False
    return dest_matches_for(v, leg)  # DirectionRef absent, fall back


def direction_ok(v):
    """Back-compat: direction check against the first configured leg."""
    return direction_ok_for(v, _DEFAULT_LEG)

# ============================ ACCURACY LOGGER ============================

class AccuracyLogger:
    """Watches buses over successive polls and records, once a bus actually
    reaches the stop, how wrong each earlier ETA prediction was.

    error_min = predicted_arrival - actual_arrival, in minutes.
      positive  -> we predicted it too LATE  (bus came sooner than we said)
      negative  -> we predicted it too EARLY (bus was slower than we said)
    """

    COLUMNS = [
        "logged_at", "vehicle", "observed_at", "dist_km_at_obs",
        "predicted_eta_min", "predicted_arrival", "actual_arrival",
        "error_min", "source",
    ]

    ARRIVAL_COLUMNS = ["arrived_at", "vehicle", "delay_min", "hour", "on_time",
                       "snap_dist_km", "stops_in_journey"]

    def __init__(self, path, arrival_radius_km, cooldown_min, arrivals_path=None):
        self.path = path
        self.arrivals_path = arrivals_path
        self.radius = arrival_radius_km
        self.cooldown = timedelta(minutes=cooldown_min)
        self.history = {}          # vehicle -> list of observation dicts
        self.cooldown_until = {}   # vehicle -> datetime it can be tracked again
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.COLUMNS)
        if arrivals_path:
            if not os.path.exists(arrivals_path):
                with open(arrivals_path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self.ARRIVAL_COLUMNS)
            else:
                self._migrate_arrivals(arrivals_path)

    def _migrate_arrivals(self, path):
        """Add any new ARRIVAL_COLUMNS to an existing file, padding old rows with
        blanks, so old and new logging code agree on column positions."""
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_header = reader.fieldnames or []
            rows = list(reader)
        if existing_header == self.ARRIVAL_COLUMNS:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.ARRIVAL_COLUMNS)
            for r in rows:
                w.writerow([r.get(col, "") for col in self.ARRIVAL_COLUMNS])

    def update(self, now, vehicles):
        """vehicles must already be filtered to this logger's leg/direction -
        this method does not re-check direction, only arrival/approaching."""
        for v in vehicles:
            veh = v["vehicle"]
            if veh is None:
                continue

            cu = self.cooldown_until.get(veh)
            in_cooldown = cu is not None and now < cu

            # Arrival. Prefer the route-snap signal from the delay model (the bus's
            # nearest stop is Lees or beyond), which catches buses the 60m GPS
            # radius misses. Fall back to the radius when the route is unknown.
            arrived = v.get("route_arrived")
            if arrived is None:
                arrived = v["dist_km"] <= self.radius
            if arrived and not in_cooldown:
                if self.history.get(veh):
                    self._flush(now, veh)
                self._log_arrival(now, v)
                self.cooldown_until[veh] = now + self.cooldown
                self.history.pop(veh, None)
                continue

            if in_cooldown:
                continue

            # Only record predictions for buses not clearly moving away (the
            # caller is responsible for direction filtering before calling us).
            if v["approaching"] is False:
                continue

            self.history.setdefault(veh, []).append({
                "observed_at": now,
                "eta_min": v["eta_min"],
                "predicted_arrival": now + timedelta(minutes=v["eta_min"]),
                "dist_km": v["dist_km"],
                "source": v["source"],
            })

    def _flush(self, actual, veh):
        obs_list = self.history[veh]
        errors = []
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for o in obs_list:
                err = (o["predicted_arrival"] - actual).total_seconds() / 60.0
                errors.append(err)
                w.writerow([
                    actual.isoformat(timespec="seconds"),
                    veh,
                    o["observed_at"].isoformat(timespec="seconds"),
                    f"{o['dist_km']:.3f}",
                    f"{o['eta_min']:.1f}",
                    o["predicted_arrival"].isoformat(timespec="seconds"),
                    actual.isoformat(timespec="seconds"),
                    f"{err:.1f}",
                    o["source"],
                ])
        if errors:
            print(f"  [logged] {veh} arrived. {len(errors)} prediction(s), "
                  f"error {min(errors):+.1f} to {max(errors):+.1f} min "
                  f"(+ = we said too late)")

    def _log_arrival(self, actual, v):
        """One row per arrival with the EXACT delay vs timetable (delay_secs at
        the moment the bus reached the stop). Needs a delay-model match."""
        if not self.arrivals_path:
            return
        ds = v.get("delay_secs")
        if ds is None:
            return
        local = actual.astimezone(_LONDON)
        delay_min = ds / 60.0
        on_time = -1.0 <= delay_min <= 5.0       # UK bus punctuality standard
        snap = v.get("snap_dist_km")
        with open(self.arrivals_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                local.isoformat(timespec="seconds"),
                v.get("vehicle"),
                f"{delay_min:.1f}",
                local.hour,
                int(on_time),
                f"{snap:.3f}" if snap is not None else "",
                v.get("journey_stops", ""),
            ])


# ============================ OUTPUT ============================

def run_once(logger=None):
    now = datetime.now(timezone.utc)
    try:
        xml_text = fetch_feed()
    except requests.RequestException as e:
        print(f"feed fetch failed: {e}")
        return

    vehicles = [estimate(v, now) for v in parse_vehicles(xml_text)]

    if logger is not None:
        logger.update(now, vehicles)

    print(f"\n=== {now.astimezone().strftime('%H:%M:%S')}  line {LINE_REF}  "
          f"{len(vehicles)} vehicle(s) in box ===")

    # DISCOVERY dump: every 219 in the box, raw. Use this to confirm the real
    # DirectionRef / DestinationName strings, then trust the filtered list below.
    print("\n-- all 219 seen (raw) --")
    for v in sorted(vehicles, key=lambda x: x["dist_km"]):
        appr = {True: "approaching", False: "moving away", None: "dir unknown"}[v["approaching"]]
        print(f"  veh={v['vehicle'] or '?':<10} dest={str(v['destination']):<22} "
              f"dirRef={str(v['direction_ref']):<10} {v['dist_km']:.2f}km  "
              f"brg={v['bearing']}  {appr}")

    # Filtered: Manchester-bound (or destination-unknown) AND not clearly receding.
    candidates = []
    for v in vehicles:
        if direction_ok(v) is False:      # known to be the wrong direction
            continue
        if v["approaching"] is False:     # bearing says it is leaving us
            continue
        candidates.append(v)

    candidates.sort(key=lambda x: x["eta_min"])

    print(f"\n-- next {SHOW_N} toward Manchester past {STOP_NAME} --")
    if not candidates:
        print("  (none right now)")
    for v in candidates[:SHOW_N]:
        tag = "OPERATOR ETA" if v["source"] == "operator" else "est ~"
        conf = "" if v["source"] == "operator" else "  (straight-line, low confidence)"
        dunk = "  [direction unconfirmed]" if direction_ok(v) is None else ""
        print(f"  {tag}{max(0, v['eta_min']):.0f} min  "
              f"({v['dist_km']:.2f} km away, veh {v['vehicle'] or '?'}){conf}{dunk}")


def main():
    if not API_KEY:
        print("No API key. Put it in api_key.txt or $BODS_API_KEY "
              "(register at data.bus-data.dft.gov.uk).")
        sys.exit(1)

    logger = None
    if ACCURACY_LOG:
        logger = AccuracyLogger(ACCURACY_LOG_PATH, ARRIVAL_RADIUS_KM, ARRIVAL_COOLDOWN_MIN)
        print(f"accuracy logging to {os.path.abspath(ACCURACY_LOG_PATH)}")

    if not LOOP:
        run_once(logger)
        return
    print(f"looping every {POLL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            run_once(logger)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()

# ============================ FAILURE MODES (read me) ============================
# 1. ETA is straight-line/assumed-speed unless the feed carries ExpectedArrivalTime
#    for stop 1800EB34051. Wrong on bends, one-way systems, real traffic. Easily
#    +/- 2-4 min, occasionally nonsense.
# 2. Direction relies on DestinationName containing "piccadilly"/"manchester". If
#    the feed omits DestinationName, an opposite-direction bus can leak in. The
#    "[direction unconfirmed]" tag marks vehicles we could not verify.
# 3. "Approaching" needs a Bearing. No bearing -> we cannot tell approaching from
#    receding, so we keep the bus (shown as "dir unknown") rather than guess.
# 4. Positions are stale by the AVL refresh + BODS poll interval (~10-30s+).
# 5. No route-shape or stop-sequence awareness. A bus 0.3km away straight-line may
#    be 0.8km away by road. That is the next thing to fix, not a v1 concern.
