"""
Delay-based ETA for the 219 at Lees Street.

This is the accurate model. Instead of straight-line distance / a speed guess,
it uses the published run-times and the bus's live position:

  1. Match the live bus to its timetabled journey (origin + destination times).
  2. Snap the bus to the nearest stop on that journey. We now know the SCHEDULED
     time at the bus's current position.
  3. delay = now - scheduled_time_at_that_position. A measured number.
  4. predicted_Lees = scheduled_Lees + delay.

Why this beats the geometry model: the run-times already encode the real road,
bends, and one-way systems, so there is no straight-line error. "On time" stops
being a stack of estimates and becomes the observed delay.

Honest limits (stated, not hidden):
  - Snapping is nearest-STOP, not a projection onto the road polyline. Off by up
    to a stop's worth where the bus is mid-link. Interpolating between stops is a
    clear future refinement.
  - Snapping can pick the wrong stop where the route doubles back or overlaps.
  - Delay is read at one instant. Smoothing over the last few polls would steady
    it. Not done yet.
  - Falls back to None (caller keeps the geometry estimate) whenever the journey
    cannot be matched or has no usable coordinates.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import next_219 as core
import schedule_219 as sch

LONDON = ZoneInfo("Europe/London")
_DEFAULT_LEG = core.LEGS[0]


def predict(vehicle, now_utc, leg=_DEFAULT_LEG):
    """Return a dict with delay-based ETA for the given leg's stop, or None to
    fall back to geometry. 'leg' is one of route_config.LEGS."""
    direction, stop_atco = leg["direction"], leg["stop_atco"]
    seq = sch.journey_for(vehicle, direction, stop_atco)
    if not seq:
        return None

    lees_i = next((i for i, s in enumerate(seq) if s[0] == stop_atco), None)
    if lees_i is None:
        return None

    blat, blon = vehicle["lat"], vehicle["lon"]

    # Snap: nearest stop on this journey that has coordinates.
    best = None
    for i, (atco, secs, lat, lon) in enumerate(seq):
        if lat is None:
            continue
        d = core.haversine_km(blat, blon, lat, lon)
        if best is None or d < best[0]:
            best = (d, i, secs)
    if best is None:
        return None
    snap_dist, j, sched_here = best

    now_local = now_utc.astimezone(LONDON)
    now_secs = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    delay = now_secs - (sched_here % 86400)
    if delay > 43200:            # midnight wrap correction
        delay -= 86400
    elif delay < -43200:
        delay += 86400

    lees_secs = seq[lees_i][1]
    scheduled_dt = sch.secs_to_local_dt(lees_secs, now_utc)
    predicted_lees = scheduled_dt + timedelta(seconds=delay)
    eta_min = (predicted_lees - now_local).total_seconds() / 60.0

    return {
        "eta_min": eta_min,
        "delay_secs": delay,
        "scheduled_dt": scheduled_dt,   # pure timetable time, no live delay applied
        "predicted_lees": predicted_lees,
        "passed": j > lees_i,          # snapped past Lees -> already gone
        "arrived": j >= lees_i,        # nearest stop is Lees or beyond = at/through it
        "stops_away": lees_i - j,      # how many stops before Lees (0 = at it)
        "snap_dist_km": snap_dist,
        "snap_index": j,
        "lees_index": lees_i,
        "stops": len(seq),
    }


if __name__ == "__main__":
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    sch.warm()
    xml = core.fetch_feed()
    vs = [core.estimate(v, now) for v in core.parse_vehicles(xml)]
    inb = [v for v in vs if core.direction_ok(v) is not False]
    print(f"inbound 219 in box: {len(inb)}")
    for v in inb:
        dm = predict(v, now)
        if not dm:
            print(f"  {v['vehicle']}: no journey match (geometry only)")
            continue
        tail = "PASSED" if dm["passed"] else f"ETA ~{dm['eta_min']:.1f} min"
        print(f"  {v['vehicle']}: snap stop #{dm['snap_index']}/{dm['stops']} "
              f"({dm['snap_dist_km']*1000:.0f} m off), "
              f"delay {dm['delay_secs']/60:+.1f} min, {tail}")
