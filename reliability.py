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
    if not os.path.exists(path):
        return None
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out.append((datetime.fromisoformat(r["arrived_at"]).astimezone(LONDON),
                            float(r["delay_min"])))
            except (ValueError, KeyError):
                continue
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


def _delays():
    """Return (source, [(local_dt, delay_min), ...])."""
    exact = read_exact(core.ARRIVALS_LOG_PATH)
    if exact:
        return "exact", exact
    sch.warm()
    sched_secs = sorted(set(sch._schedule.values()))
    if not sched_secs:
        return "none", []
    seen = {}
    if not os.path.exists(core.ACCURACY_LOG_PATH):
        return "exact", []
    with open(core.ACCURACY_LOG_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("vehicle"), r.get("actual_arrival"))
            if key in seen:
                continue
            try:
                seen[key] = datetime.fromisoformat(r["actual_arrival"]).astimezone(LONDON)
            except (ValueError, KeyError):
                continue
    return "reconstructed", [(a, _nearest_delay_min(a, sched_secs))
                             for a in sorted(seen.values())]


def compute():
    """Summary dict, or None if there is nothing to report."""
    source, pairs = _delays()
    if not pairs:
        return None
    pairs = sorted(pairs, key=lambda x: x[0])
    times = [t for t, _ in pairs]
    delays = [d for _, d in pairs]
    ot = [_on_time(d) for d in delays]

    by_hour_map = collections.defaultdict(list)
    for t, d in pairs:
        by_hour_map[t.hour].append(d)
    by_hour = [[h, len(v), round(100 * sum(_on_time(x) for x in v) / len(v)),
                round(statistics.mean(v), 1)] for h, v in sorted(by_hour_map.items())]

    return {
        "source": source,
        "n": len(delays),
        "on_time_pct": round(100 * sum(ot) / len(ot)),
        "median": round(statistics.median(delays), 1),
        "window": f"{times[0]:%d %b %H:%M}-{times[-1]:%H:%M}",
        "by_hour": by_hour,
    }


def main():
    s = compute()
    if not s:
        print("no arrivals logged yet")
        return
    print(f"219 reliability at {core.STOP_NAME}")
    print(f"source: {s['source']}   window: {s['window']}   buses: {s['n']}")
    print(f"on time (-1 to +5 min): {s['on_time_pct']}%   "
          f"median delay: {s['median']:+.1f} min")
    print("\nby hour:  hr   n  on-time  avg")
    for h, n, otp, avg in s["by_hour"]:
        print(f"          {h:02d}  {n:>3}  {otp:>4}%  {avg:+.1f}m")


if __name__ == "__main__":
    main()
