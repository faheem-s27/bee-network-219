"""
Timetable cross-reference for the 219 at Lees Street.

The live feed gives no scheduled time at our stop, only the journey's origin
departure and destination arrival. So we pull the published timetable (BODS
Timetables dataset, TransXChange) for the 219, walk each inbound journey's
run times to work out the scheduled clock time AT Lees Street, and key it by
(origin departure, destination arrival) - two timestamps the live feed also
carries. Matching on those avoids the unreliable SIRI-to-timetable journey-ref
linkage.

Honesty notes:
- The scheduled time is exact (it is the published timetable).
- The "lateness" we compute later = predicted actual (now + our shaky straight
  line ETA) minus this scheduled time. So the lateness is only as good as the
  ETA. The schedule half is solid, the prediction half is not.
- TransXChange times are local (Europe/London). The feed is UTC. We convert.
- DATASET_ID is hard-coded. BODS supersedes datasets over time; if matching
  goes blank, re-query the dataset id (see find_dataset_id at the bottom).
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
LINE = core.LINE_REF                 # route config lives in next_219.py
OPERATOR_NOC = core.OPERATOR_NOC     # operator code, e.g. BNML
STOP_ATCO = core.STOP_ATCO           # your stop, the one to predict
INBOUND = core.DIRECTION             # the timetable Direction toward your destination

# Auto-refresh policy.
REFRESH_AFTER_DAYS = 7              # re-download the dataset once the file is older
REMOTE_CHECK_HOURS = 6             # how often to ask BODS whether the id changed

_schedule = None     # cached: (origin_hhmm, dest_hhmm) -> Lees seconds-of-day
_journeys = None     # cached: same key -> [(atco, sched_secs, lat, lon), ...]
_active_path = None  # the zip currently parsed into the caches
_built_date = None   # date the caches were built for (rebuild when the day rolls)
_last_check = None   # last time we asked BODS for the current dataset id


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
    """Ask BODS which dataset currently carries line 219 for BNML. Newest wins.
    Returns an int id, or None on any failure (caller keeps what it has)."""
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
        # Fall back to whatever we are already using, then any local tt_*.zip,
        # then the hard-coded default.
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


def _parse(zip_path, today_str):
    """Parse one timetable zip into (sched, journeys) for the given day.

      sched[(origin_hhmm, dest_hhmm)]    = Lees Street seconds-of-day
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

        # Stop coordinates (from the timetable itself).
        coords = {}
        for asp in root.iter("AnnotatedStopPointRef"):
            ref = _txt(asp, "StopPointRef")
            lat = _txt(asp, "Location/Latitude")
            lon = _txt(asp, "Location/Longitude")
            if ref and lat and lon:
                coords[ref] = (float(lat), float(lon))

        # JourneyPatternSection id -> ordered [(from_stop, from_wait, to_stop, run)]
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

        # JourneyPattern id -> (direction, [section refs])
        patterns = {}
        for jp in root.iter("JourneyPattern"):
            refs = [r.text.strip() for r in jp.findall("JourneyPatternSectionRefs")]
            patterns[jp.get("id")] = (_txt(jp, "Direction"), refs)

        for vj in root.iter("VehicleJourney"):
            jpr = _txt(vj, "JourneyPatternRef")
            dep = _txt(vj, "DepartureTime")
            if jpr not in patterns or not dep:
                continue
            direction, refs = patterns[jpr]
            if direction != INBOUND:
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

            if STOP_ATCO not in times:
                continue
            key = (_hhmm(t), _hhmm(cur))     # (origin dep, destination arr)
            sched[key] = times[STOP_ATCO] % 86400
            journeys[key] = [
                (st, times[st], *(coords.get(st) or (None, None))) for st in ordered
            ]

    return sched, journeys


def build():
    """Ensure a current timetable is on disk and parse it for today."""
    path = ensure_timetable(remote=True)
    today_str = datetime.date.today().strftime("%Y%m%d")
    return _parse(path, today_str), path


def _rebuild(remote):
    """(Re)build the caches if needed: first run, the day rolled over, or a new
    dataset was downloaded. Safe to call often; only does work when warranted."""
    global _schedule, _journeys, _active_path, _built_date
    try:
        path = ensure_timetable(remote=remote)
    except Exception:
        return                                   # no usable timetable yet, keep trying
    today = datetime.date.today()
    if _schedule is None or path != _active_path or today != _built_date:
        sched, journeys = _parse(path, today.strftime("%Y%m%d"))
        _schedule, _journeys = sched, journeys
        _active_path, _built_date = path, today
        for old in glob.glob("tt_*.zip"):        # tidy superseded downloads
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass


def warm():
    """Build and cache the schedule. Call once, off the UI thread."""
    if _schedule is None:
        _rebuild(remote=True)
    return _schedule


def maybe_refresh():
    """Call every poll. Cheaply rebuilds on a new day; asks BODS for a newer
    dataset at most every REMOTE_CHECK_HOURS and re-downloads if it changed."""
    global _last_check
    now = datetime.datetime.now()
    due = _last_check is None or \
        (now - _last_check).total_seconds() >= REMOTE_CHECK_HOURS * 3600
    if due:
        _last_check = now
    _rebuild(remote=due)


def ready():
    """True once the schedule is built (fast lookups available)."""
    return _schedule is not None


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


def _match_key(vehicle):
    """The (origin_hhmm, dest_hhmm) key for a live vehicle, or None."""
    if _schedule is None:
        return None
    od = vehicle.get("origin_aimed_dep")
    da = vehicle.get("dest_aimed_arr")
    if da is None:
        return None

    def hhmm(dt):
        return dt.astimezone(LONDON).strftime("%H:%M") if dt else None

    key = (hhmm(od), hhmm(da))
    if key in _schedule:
        return key
    cands = [k for k in _schedule if k[1] == hhmm(da)]   # destination-only fallback
    return cands[0] if len(cands) == 1 else None


def scheduled_lees(vehicle, now):
    """Scheduled Lees Street datetime (aware) for a live vehicle, or None."""
    warm()
    key = _match_key(vehicle)
    if key is None:
        return None
    return secs_to_local_dt(_schedule[key], now)


def journey_for(vehicle):
    """Ordered [(atco, sched_secs, lat, lon)] for the live vehicle's journey,
    or None if it cannot be matched to the timetable."""
    warm()
    key = _match_key(vehicle)
    return _journeys.get(key) if key else None


def find_dataset_id():
    """Helper: re-discover the current dataset id carrying line 219 for BNML."""
    r = requests.get("https://data.bus-data.dft.gov.uk/api/v1/dataset/",
                     params={"api_key": core.API_KEY, "noc": "BNML", "limit": 50},
                     timeout=40)
    for d in r.json().get("results", []):
        if "219" in (d.get("lines") or []):
            print(d.get("id"), d.get("name"))


if __name__ == "__main__":
    (s, j), path = build()
    print(f"using {path}")
    print(f"inbound 219 journeys passing Lees Street: {len(s)}")
    for k in sorted(s)[:6]:
        print(f"  origin {k[0]}  dest {k[1]}  ->  Lees {_hhmm(s[k])}  "
              f"({len(j[k])} stops)")
