"""
Reliability stats for the bus at your stop.

Prefers the exact arrivals log (the delay the model measured at each arrival). If
that is empty it falls back to reconstructing delay by matching each logged
arrival to the nearest scheduled time, which UNDERSTATES buses more than about
half a headway late, so the source is always labelled.

"On time" uses the UK bus punctuality standard: no more than 1 minute early and
no more than 5 minutes late.

compute() returns a dict for the GUI/JSON; run as a script for a text report.
"""

import collections
import csv
import os
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import next_219 as core
import schedule_219 as sch

LONDON = ZoneInfo("Europe/London")
EARLY_LIMIT = -1.0
LATE_LIMIT = 5.0


def _on_time(d):
    return EARLY_LIMIT <= d <= LATE_LIMIT


def read_exact(path):
    """[(local_dt, delay_min, snap_dist_km_or_None), ...]. snap_dist_km is the
    distance (km) between the bus's GPS and the stop it was matched to at
    arrival - large values mean the route-snap is unreliable for that row, which
    is exactly the failure mode to check before trusting an odd delay reading."""
    if not os.path.exists(path):
        return None
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.fromisoformat(r["arrived_at"]).astimezone(LONDON)
                delay = float(r["delay_min"])
            except (ValueError, KeyError):
                continue
            snap = r.get("snap_dist_km")
            try:
                snap = float(snap) if snap else None
            except ValueError:
                snap = None
            out.append((dt, delay, snap))
    return out or None


def _nearest_delay_min(arr_local, sched_secs):
    s = arr_local.hour * 3600 + arr_local.minute * 60 + arr_local.second
    best = min(sched_secs, key=lambda x: min(abs(s - x), 86400 - abs(s - x)))
    d = s - best
    if d > 43200:
        d -= 86400
    elif d < -43200:
        d += 86400
    return d / 60.0


def _delays(arrivals_path, accuracy_path, direction, stop_atco):
    """Return (source, [(local_dt, delay_min, snap_dist_km_or_None), ...])."""
    exact = read_exact(arrivals_path)
    if exact:
        return "exact", exact
    sched = sch.warm(direction, stop_atco)
    sched_secs = sorted(set(sched.values()))
    if not sched_secs:
        return "none", []
    seen = {}
    if not os.path.exists(accuracy_path):
        return "exact", []
    with open(accuracy_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("vehicle"), r.get("actual_arrival"))
            if key in seen:
                continue
            try:
                seen[key] = datetime.fromisoformat(r["actual_arrival"]).astimezone(LONDON)
            except (ValueError, KeyError):
                continue
    return "reconstructed", [(a, _nearest_delay_min(a, sched_secs), None)
                             for a in sorted(seen.values())]


def compute(arrivals_path=None, accuracy_path=None, direction=None, stop_atco=None):
    """Summary dict, or None if there is nothing to report. Defaults to the
    first configured leg for back-compat single-route callers."""
    arrivals_path = arrivals_path or core.ARRIVALS_LOG_PATH
    accuracy_path = accuracy_path or core.ACCURACY_LOG_PATH
    direction = direction or core.DIRECTION
    stop_atco = stop_atco or core.STOP_ATCO
    source, triples = _delays(arrivals_path, accuracy_path, direction, stop_atco)
    if not triples:
        return None
    triples = sorted(triples, key=lambda x: x[0])
    times = [t for t, _, _ in triples]
    delays = [d for _, d, _ in triples]
    ot = [_on_time(d) for d in delays]

    by_hour_map = collections.defaultdict(list)
    for t, d, s in triples:
        by_hour_map[t.hour].append((d, s))
    by_hour = []
    for h, v in sorted(by_hour_map.items()):
        ds = [d for d, _ in v]
        snaps = [s for _, s in v if s is not None]
        by_hour.append([
            h, len(ds), round(100 * sum(_on_time(x) for x in ds) / len(ds)),
            round(statistics.mean(ds), 1),
            round(statistics.mean(snaps) * 1000) if snaps else None,   # metres
        ])

    by_dow_map = collections.defaultdict(list)
    for t, d, _ in triples:
        by_dow_map[t.weekday()].append(d)
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow = [[dow_names[d], len(v), round(100 * sum(_on_time(x) for x in v) / len(v)),
              round(statistics.mean(v), 1)] for d, v in sorted(by_dow_map.items())]

    return {
        "source": source,
        "n": len(delays),
        "on_time_pct": round(100 * sum(ot) / len(ot)),
        "median": round(statistics.median(delays), 1),
        "window": f"{times[0]:%d %b %H:%M}-{times[-1]:%H:%M}",
        "by_hour": by_hour,          # [hour, n, on_time_pct, avg_delay, avg_snap_m]
        "by_dow": by_dow,            # [day_name, n, on_time_pct, avg_delay]
    }


def main():
    import sys
    leg = core.LEGS[0]
    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        leg = next((l for l in core.LEGS if l["key"] == wanted), leg)
    if len(core.LEGS) > 1:
        acc = core.ACCURACY_LOG_PATH if leg is core.LEGS[0] else f"eta_accuracy_log_{leg['key']}.csv"
        arr = core.ARRIVALS_LOG_PATH if leg is core.LEGS[0] else f"arrivals_log_{leg['key']}.csv"
        s = compute(arr, acc, leg["direction"], leg["stop_atco"])
    else:
        s = compute()
    if not s:
        print(f"no arrivals logged yet for {leg['key']}")
        return
    print(f"{leg['line_ref']} reliability at {leg['stop_name']}")
    print(f"source: {s['source']}   window: {s['window']}   buses: {s['n']}")
    print(f"on time (-1 to +5 min): {s['on_time_pct']}%   "
          f"median delay: {s['median']:+.1f} min")
    print("\nby hour:  hr   n  on-time  avg     avg snap dist")
    for h, n, otp, avg, snap_m in s["by_hour"]:
        snap_txt = f"{snap_m}m" if snap_m is not None else "n/a"
        flag = "  <- check: snap far from stop" if (snap_m or 0) > 150 else ""
        print(f"          {h:02d}  {n:>3}  {otp:>4}%  {avg:+.1f}m   {snap_txt:>6}{flag}")

    print("\nby day:   day   n  on-time  avg")
    for day, n, otp, avg in s.get("by_dow", []):
        print(f"          {day}  {n:>3}  {otp:>4}%  {avg:+.1f}m")


if __name__ == "__main__":
    main()
