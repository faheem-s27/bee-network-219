"""
Timetable cross-reference, generalized to any number of (direction, stop) pairs
("legs" - see route_config.py) sharing one timetable download.

The live feed gives no scheduled time at your stop, only the journey's origin
departure and destination arrival. So we pull the published timetable (BODS
Timetables dataset, TransXChange) for the line, walk each journey's run times to
work out the scheduled clock time AT your stop, and key it by (origin departure,
destination arrival) - two timestamps the live feed also carries. Matching on
those avoids the unreliable SIRI-to-timetable journey-ref linkage.

Honesty notes:
- The scheduled time is exact (it is the published timetable).
- The "lateness" computed elsewhere = predicted actual minus this scheduled
  time. The schedule half is solid; how good the prediction half is depends on
  which model produced it.
- TransXChange times are local (Europe/London). The feed is UTC. We convert.
- The timetable dataset id is auto-discovered and re-checked periodically; see
  discover_dataset_id() / DEFAULT_DATASET_ID.
"""

import datetime
import glob
import os
import re
import zipfile
from zoneinfo import ZoneInfo

import requests

import next_219 as core

LONDON = ZoneInfo("Europe/London")
DEFAULT_DATASET_ID = 17472          # used only if discovery fails and nothing local

# Back-compat single-route constants (still read by discover.py etc).
LINE = core.LINE_REF
OPERATOR_NOC = core.OPERATOR_NOC
STOP_ATCO = core.STOP_ATCO
INBOUND = core.DIRECTION

# Auto-refresh policy.
REFRESH_AFTER_DAYS = 7              # re-download the dataset once the file is older
REMOTE_CHECK_HOURS = 6              # how often to ask BODS whether the id changed

# One shared timetable zip (same operator+line for every leg).
_active_path = None  # the zip currently on disk / parsed from
_built_date = None   # date the current partitions were built for
_last_check = None   # last time we asked BODS for the current dataset id

# Per-(direction, stop_atco) parsed results, built lazily from the shared zip.
#   _partitions[(direction, stop_atco)] = (schedule_dict, journeys_dict)
#   schedule[(origin_hhmm, dest_hhmm)] = stop's seconds-of-day
#   journeys[(origin_hhmm, dest_hhmm)] = ordered [(atco, sched_secs, lat, lon)]
_partitions = {}


def _dur_secs(s):
    """ISO8601 duration like PT1M30S -> seconds. None -> 0."""
    if not s:
        return 0
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0
    h, mn, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + sec


def _txt(elem, path):
    n = elem.find(path)
    return n.text.strip() if n is not None and n.text else None


def _hhmm(secs):
    secs %= 86400
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def _zip_for(dataset_id):
    return f"tt_{dataset_id}.zip"


def _age_days(path):
    return (datetime.datetime.now().timestamp() - os.path.getmtime(path)) / 86400.0


def discover_dataset_id():
    """Ask BODS which dataset currently carries this line for this operator.
    Newest wins. Returns an int id, or None on any failure."""
    try:
        r = requests.get("https://data.bus-data.dft.gov.uk/api/v1/dataset/",
                         params={"api_key": core.API_KEY, "noc": OPERATOR_NOC,
                                 "limit": 50, "status": "published"}, timeout=40)
        best = None
        for d in r.json().get("results", []):
            if LINE in (d.get("lines") or []):
                if best is None or d["id"] > best:
                    best = d["id"]
        return best
    except Exception:
        return None


def _download(dataset_id, path):
    url = f"https://data.bus-data.dft.gov.uk/timetable/dataset/{dataset_id}/download/"
    r = requests.get(url, params={"api_key": core.API_KEY}, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def ensure_timetable(remote=True):
    """Make sure a usable zip is on disk, refreshing if the dataset id changed or
    the file is stale. Returns its path. Never raises for network reasons if some
    local copy already exists."""
    dataset_id = discover_dataset_id() if remote else None
    if dataset_id is None:
        if _active_path and os.path.exists(_active_path):
            return _active_path
        local = sorted(glob.glob("tt_*.zip"))
        if local:
            return local[-1]
        dataset_id = DEFAULT_DATASET_ID

    path = _zip_for(dataset_id)
    if not os.path.exists(path) or _age_days(path) > REFRESH_AFTER_DAYS:
        try:
            _download(dataset_id, path)
        except Exception:
            if os.path.exists(path):
                return path                      # stale but usable
            if _active_path and os.path.exists(_active_path):
                return _active_path
            raise
    return path


def _valid_today(name, today_str):
    """Filename carries _START_END_ as yyyymmdd. Keep files valid for today."""
    m = re.search(r"_(\d{8})_(\d{8})_", name)
    if not m:
        return True
    return m.group(1) <= today_str <= m.group(2)


def _parse(zip_path, today_str, direction, stop_atco):
    """Parse one timetable zip for ONE (direction, stop_atco) partition.

      sched[(origin_hhmm, dest_hhmm)]    = the stop's seconds-of-day
      journeys[(origin_hhmm, dest_hhmm)] = ordered [(atco, sched_secs, lat, lon)]
    """
    import xml.etree.ElementTree as ET

    z = zipfile.ZipFile(zip_path)
    files = [n for n in z.namelist()
             if n.startswith(f"{OPERATOR_NOC}_{LINE}_") and _valid_today(n, today_str)]

    sched = {}
    journeys = {}
    for n in files:
        data = z.read(n)
        data = re.sub(rb'xmlns="[^"]+"', b"", data, count=1)   # drop default ns
        root = ET.fromstring(data)

        coords = {}
        for asp in root.iter("AnnotatedStopPointRef"):
            ref = _txt(asp, "StopPointRef")
            lat = _txt(asp, "Location/Latitude")
            lon = _txt(asp, "Location/Longitude")
            if ref and lat and lon:
                coords[ref] = (float(lat), float(lon))

        sections = {}
        for js in root.iter("JourneyPatternSection"):
            links = []
            for l in js.findall("JourneyPatternTimingLink"):
                fr, to = l.find("From"), l.find("To")
                links.append((
                    _txt(fr, "StopPointRef"),
                    _dur_secs(_txt(fr, "WaitTime")),
                    _txt(to, "StopPointRef"),
                    _dur_secs(_txt(l, "RunTime")),
                ))
            sections[js.get("id")] = links

        patterns = {}
        for jp in root.iter("JourneyPattern"):
            refs = [r.text.strip() for r in jp.findall("JourneyPatternSectionRefs")]
            patterns[jp.get("id")] = (_txt(jp, "Direction"), refs)

        for vj in root.iter("VehicleJourney"):
            jpr = _txt(vj, "JourneyPatternRef")
            dep = _txt(vj, "DepartureTime")
            if jpr not in patterns or not dep:
                continue
            jp_direction, refs = patterns[jpr]
            if jp_direction != direction:
                continue
            links = []
            for r in refs:
                links += sections.get(r, [])
            if not links:
                continue

            h, m, s = (int(x) for x in dep.split(":"))
            t = h * 3600 + m * 60 + s
            shift = _txt(vj, "DepartureDayShift")
            if shift:
                t += int(shift) * 86400

            ordered = [links[0][0]]
            times = {ordered[0]: t}
            cur = t
            for fs, fw, ts, rt in links:
                cur += fw + rt
                times[ts] = cur
                ordered.append(ts)

            if stop_atco not in times:
                continue
            key = (_hhmm(t), _hhmm(cur))     # (origin dep, destination arr)
            sched[key] = times[stop_atco] % 86400
            journeys[key] = [
                (st, times[st], *(coords.get(st) or (None, None))) for st in ordered
            ]

    return sched, journeys


def _rebuild_shared(remote):
    """Make sure the shared zip is current. If it changed (new dataset or a new
    day), drop all cached partitions so they rebuild lazily on next use."""
    global _active_path, _built_date
    try:
        path = ensure_timetable(remote=remote)
    except Exception:
        return
    today = datetime.date.today()
    if path != _active_path or today != _built_date:
        _partitions.clear()
        _active_path, _built_date = path, today
        for old in glob.glob("tt_*.zip"):        # tidy superseded downloads
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass


def warm(direction=None, stop_atco=None):
    """Build and cache the schedule for one (direction, stop) pair. Defaults to
    the first configured leg for back-compat with single-route callers."""
    direction = direction or INBOUND
    stop_atco = stop_atco or STOP_ATCO
    if _active_path is None:
        _rebuild_shared(remote=True)
    key = (direction, stop_atco)
    if key not in _partitions and _active_path:
        today_str = _built_date.strftime("%Y%m%d") if _built_date else \
            datetime.date.today().strftime("%Y%m%d")
        _partitions[key] = _parse(_active_path, today_str, direction, stop_atco)
    return _partitions.get(key, ({}, {}))[0]


def maybe_refresh():
    """Call every poll. Cheaply rebuilds on a new day; asks BODS for a newer
    dataset at most every REMOTE_CHECK_HOURS and re-downloads if it changed.
    Existing per-leg partitions are rebuilt lazily via warm()/journey_for()."""
    global _last_check
    now = datetime.datetime.now()
    due = _last_check is None or \
        (now - _last_check).total_seconds() >= REMOTE_CHECK_HOURS * 3600
    if due:
        _last_check = now
    _rebuild_shared(remote=due)


def ready(direction=None, stop_atco=None):
    """True once that leg's schedule is built (fast lookups available)."""
    key = (direction or INBOUND, stop_atco or STOP_ATCO)
    return key in _partitions and bool(_partitions[key][0])


def all_stop_coords(direction=None, stop_atco=None):
    """Every (lat, lon) with known coordinates across all journeys in one leg's
    partition. Used to size a bounding box that covers the real route rather
    than a guessed padding constant."""
    direction = direction or INBOUND
    stop_atco = stop_atco or STOP_ATCO
    warm(direction, stop_atco)
    _, journeys = _partitions.get((direction, stop_atco), ({}, {}))
    return [(lat, lon) for seq in journeys.values()
            for _atco, _secs, lat, lon in seq if lat is not None]


def secs_to_local_dt(secs, now):
    """Seconds-of-day (local) -> an aware Europe/London datetime near 'now',
    correcting a possible midnight wrap. 'now' may be any aware datetime."""
    secs %= 86400
    today = now.astimezone(LONDON).date()
    naive = datetime.datetime.combine(
        today, datetime.time(secs // 3600, secs % 3600 // 60, secs % 60))
    dt = naive.replace(tzinfo=LONDON)
    diff = (dt - now.astimezone(LONDON)).total_seconds()
    if diff > 12 * 3600:
        dt -= datetime.timedelta(days=1)
    elif diff < -12 * 3600:
        dt += datetime.timedelta(days=1)
    return dt


def _match_key(vehicle, direction, stop_atco):
    """The (origin_hhmm, dest_hhmm) key for a live vehicle within one leg's
    partition, or None."""
    sched = warm(direction, stop_atco)
    if not sched:
        return None
    od = vehicle.get("origin_aimed_dep")
    da = vehicle.get("dest_aimed_arr")
    if da is None:
        return None

    def hhmm(dt):
        return dt.astimezone(LONDON).strftime("%H:%M") if dt else None

    key = (hhmm(od), hhmm(da))
    if key in sched:
        return key
    cands = [k for k in sched if k[1] == hhmm(da)]   # destination-only fallback
    return cands[0] if len(cands) == 1 else None


# A match whose scheduled time sits further than this from 'now' is almost
# certainly a stale journey match, not a real fact about a route running every
# few minutes (see delay_model.py's docstring for the full explanation: the
# live feed sometimes reports a vehicle's PREVIOUS trip's aimed times for a
# window after it starts a new one). Mirrors delay_model.MAX_PLAUSIBLE_DELAY_MIN;
# kept as its own constant so this module does not depend on delay_model.
MAX_PLAUSIBLE_OFFSET_MIN = 60.0


def scheduled_lees(vehicle, now, direction=None, stop_atco=None):
    """Scheduled datetime (aware) at the given stop for a live vehicle, or None
    if it cannot be matched OR the match is implausibly far from 'now' (a stale
    match, not a real fact). Defaults to the first configured leg for back-compat."""
    direction = direction or INBOUND
    stop_atco = stop_atco or STOP_ATCO
    sched = warm(direction, stop_atco)
    key = _match_key(vehicle, direction, stop_atco)
    if key is None:
        return None
    dt = secs_to_local_dt(sched[key], now)
    offset_min = abs((dt - now.astimezone(LONDON)).total_seconds()) / 60.0
    if offset_min > MAX_PLAUSIBLE_OFFSET_MIN:
        return None
    return dt


def journey_for(vehicle, direction=None, stop_atco=None):
    """Ordered [(atco, sched_secs, lat, lon)] for the live vehicle's journey
    within one leg's partition, or None if it cannot be matched."""
    direction = direction or INBOUND
    stop_atco = stop_atco or STOP_ATCO
    warm(direction, stop_atco)
    key = _match_key(vehicle, direction, stop_atco)
    journeys = _partitions.get((direction, stop_atco), ({}, {}))[1]
    return journeys.get(key) if key else None


def expected_headway_min(direction, stop_atco, now):
    """Typical minutes between scheduled buses at this stop around 'now', used
    for missed-bus detection. Returns None if there is not enough timetable data
    to bracket 'now' (e.g. no service running at this time of day)."""
    sched = warm(direction, stop_atco)
    secs_list = sorted(set(sched.values()))
    if len(secs_list) < 2:
        return None
    now_secs = now.astimezone(LONDON).hour * 3600 + \
        now.astimezone(LONDON).minute * 60 + now.astimezone(LONDON).second
    # Find the scheduled times immediately before and after now (with wraparound
    # across midnight), and use the gap either side of 'now' as the headway.
    before = max((s for s in secs_list if s <= now_secs), default=secs_list[-1] - 86400)
    after = min((s for s in secs_list if s >= now_secs), default=secs_list[0] + 86400)
    gaps = []
    idx = secs_list.index(before) if before in secs_list else None
    if idx is not None:
        prev = secs_list[idx - 1] if idx > 0 else secs_list[-1] - 86400
        gaps.append((before - prev) / 60.0)
    idx2 = secs_list.index(after) if after in secs_list else None
    if idx2 is not None:
        nxt = secs_list[idx2 + 1] if idx2 + 1 < len(secs_list) else secs_list[0] + 86400
        gaps.append((nxt - after) / 60.0)
    return sum(gaps) / len(gaps) if gaps else None


def find_dataset_id():
    """Helper: re-discover the current dataset id carrying this line/operator."""
    r = requests.get("https://data.bus-data.dft.gov.uk/api/v1/dataset/",
                     params={"api_key": core.API_KEY, "noc": OPERATOR_NOC, "limit": 50},
                     timeout=40)
    for d in r.json().get("results", []):
        if LINE in (d.get("lines") or []):
            print(d.get("id"), d.get("name"))


if __name__ == "__main__":
    sched = warm()
    path = _active_path
    print(f"using {path}")
    print(f"{INBOUND} {LINE} journeys passing {STOP_ATCO}: {len(sched)}")
    for k in sorted(sched)[:6]:
        print(f"  origin {k[0]}  dest {k[1]}  ->  stop time {_hhmm(sched[k])}")
    hw = expected_headway_min(INBOUND, STOP_ATCO, datetime.datetime.now(LONDON))
    print(f"expected headway right now: {hw:.0f} min" if hw else "expected headway: n/a")
