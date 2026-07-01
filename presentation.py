"""
Shared display formatting: turn a computed vehicle dict into a display-ready row.

Both sides use this so they never disagree:
  - the Pi's headless runner builds these rows and serves them as JSON
  - the local GUI, when running standalone, builds the same rows itself

In thin-client mode the GUI just renders rows the Pi already built, so it does no
computation at all. Colour is intentionally NOT decided here (the GUI maps
status_kind to a colour); this module only decides text and meaning.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import next_219 as core
import schedule_219 as sch

LONDON = ZoneInfo("Europe/London")
ONTIME_BAND_MIN = 1.5
_DEFAULT_LEG = core.LEGS[0]


def _band(diff_min, prefix):
    if abs(diff_min) <= ONTIME_BAND_MIN:
        return prefix + "on time", "ontime"
    if diff_min > 0:
        return f"{prefix}{round(diff_min)} min late", "late"
    return f"{prefix}{abs(round(diff_min))} min early", "early"


def row(v, now_utc, leg=_DEFAULT_LEG):
    """Display row for one vehicle on the given leg. now_utc is aware UTC."""
    now_local = now_utc.astimezone(LONDON)
    eta = v["eta_min"]
    due = eta < 1
    if v.get("source") == "operator":
        label = "DUE" if due else f"{round(eta)} min"
    else:
        label = "DUE" if due else f"~{round(eta)} min"
    expected_dt = now_local + timedelta(minutes=max(0.0, eta))

    # Prefer the scheduled time the delay model already matched for THIS leg's
    # stop (attached by the caller). Only fall back to a fresh lookup - using
    # this leg's own direction/stop, not some other leg's - when that is absent
    # (the rare case where the geometry model, not the delay model, is in use).
    sched_dt = v.get("scheduled_dt")
    if sched_dt is None:
        try:
            if sch.ready(leg["direction"], leg["stop_atco"]):
                sched_dt = sch.scheduled_lees(v, now_utc, leg["direction"], leg["stop_atco"])
        except Exception:
            sched_dt = None

    if v.get("delay_secs") is not None:                       # measured delay
        status_text, status_kind = _band(v["delay_secs"] / 60.0, "")
    elif sched_dt is not None:                                # estimated vs schedule
        status_text, status_kind = _band(
            (expected_dt - sched_dt).total_seconds() / 60.0, "~")
    else:
        status_text, status_kind = "", "none"

    stops_away = v.get("stops_away")
    return {
        "dest": (v.get("destination") or "Manchester").replace("_", " ")[:22],
        "label": label,
        "due": due,
        "expected": expected_dt.strftime("%H:%M"),
        "sched": sched_dt.strftime("%H:%M") if sched_dt else None,
        "stops_away": stops_away,
        "status_text": status_text,
        "status_kind": status_kind,
        "unconfirmed": core.direction_ok_for(v, leg) is None,
        "source": v.get("source"),
        "vehicle": v.get("vehicle"),
    }
