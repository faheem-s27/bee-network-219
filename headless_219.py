"""
Headless runner for a Raspberry Pi (no GUI). Tracks every leg in
route_config.LEGS (by default: to Manchester, and the return to Ashton) from a
SINGLE shared feed fetch per poll - one BODS call, filtered twice.

Runs the full autonomous pipeline per leg (delay model, auto-refreshing shared
timetable, accuracy logging, self-calibration, missed-bus/gap detection),
prints the next arrivals to stdout (journald under systemd), AND serves the
current state as JSON over HTTP so thin clients (the desktop GUI, an ESP32
sign) can just read them.

Ctrl+C to stop. Pass --once to run a single cycle, print the JSON, and exit.
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import next_219 as core
import schedule_219 as sch
import delay_model
import calibrate
import presentation
import reliability

LONDON = ZoneInfo("Europe/London")
SERVE_HTTP = True
HTTP_PORT = 8219

# Missed-bus / gap detection: flag it when nothing has arrived for this many
# times the expected headway, AND nothing is currently approaching either (so
# a bus that is simply further away does not get mistaken for a no-show).
GAP_MULTIPLIER = 1.5

# Per-leg file naming. Leg 0 keeps the ORIGINAL filenames so existing logged
# history is not orphaned; any additional legs get their own suffixed files.
def _leg_paths(leg, index):
    if index == 0:
        return core.ACCURACY_LOG_PATH, core.ARRIVALS_LOG_PATH
    return (f"eta_accuracy_log_{leg['key']}.csv", f"arrivals_log_{leg['key']}.csv")


_loggers = {}          # leg key -> core.AccuracyLogger
_last_arrival = {}      # leg key -> last UTC datetime a bus arrived, this run
_latest = {"generated": None, "legs": {}}
_lock = threading.Lock()


def _init_loggers():
    for i, leg in enumerate(core.LEGS):
        acc_path, arr_path = _leg_paths(leg, i)
        _loggers[leg["key"]] = core.AccuracyLogger(
            acc_path, core.ARRIVAL_RADIUS_KM, core.ARRIVAL_COOLDOWN_MIN, arr_path)


def _process_leg(leg, index, all_vehicles, now):
    """Filter the shared vehicle list to this leg's direction, run the delay
    model, log, and build the JSON payload for one leg."""
    acc_path, arr_path = _leg_paths(leg, index)

    vehicles = [dict(v) for v in all_vehicles]      # per-leg copy: eta/dist differ per stop
    leg_vehicles = []
    for v in vehicles:
        ok = core.direction_ok_for(v, leg)
        if ok is False:
            continue
        core.estimate_for(v, now, leg)
        try:
            dm = delay_model.predict(v, now, leg)
        except Exception:
            dm = None
        if dm:
            v["stops_away"] = dm["stops_away"]
            v["route_arrived"] = dm["arrived"]
            v["delay_secs"] = dm["delay_secs"]
            v["scheduled_dt"] = dm["scheduled_dt"]
            v["snap_dist_km"] = dm["snap_dist_km"]
            v["journey_stops"] = dm["stops"]
            if dm["passed"]:
                v["approaching"] = False
            else:
                v["approaching"] = True
                v["eta_min"] = dm["eta_min"]
                v["source"] = "delay"
        leg_vehicles.append(v)

    logger = _loggers[leg["key"]]
    logger.update(now, leg_vehicles)
    for v in leg_vehicles:
        if v.get("route_arrived"):
            _last_arrival[leg["key"]] = now

    cands = [v for v in leg_vehicles if v["approaching"] is not False]
    cands.sort(key=lambda x: x["eta_min"])
    rows = [presentation.row(v, now, leg) for v in cands[:5]]

    # Missed-bus / gap check: only when nothing is currently approaching, so a
    # bus that is simply further out is never mistaken for a no-show.
    gap_warning = None
    if not cands:
        last = _last_arrival.get(leg["key"])
        if last is not None:
            try:
                headway = sch.expected_headway_min(leg["direction"], leg["stop_atco"], now)
            except Exception:
                headway = None
            if headway:
                since_min = (now - last).total_seconds() / 60.0
                if since_min > GAP_MULTIPLIER * headway:
                    gap_warning = (f"no {leg['line_ref']} seen for {since_min:.0f} min "
                                   f"(normally every ~{headway:.0f})")

    try:
        cal, _ = calibrate.load(acc_path)
        core.CALIBRATION = cal            # shared fallback model; last leg processed wins
        model = calibrate.model_status(acc_path)
    except Exception:
        model = ""

    try:
        rel = reliability.compute(arr_path, acc_path, leg["direction"], leg["stop_atco"])
    except Exception:
        rel = None

    return {
        "stop": leg["stop_name"], "line": leg["line_ref"],
        "dest_label": leg["dest_label"], "model": model,
        "n": len(leg_vehicles), "arrivals": rows,
        "gap_warning": gap_warning, "reliability": rel,
    }


def cycle():
    now = datetime.now(timezone.utc)
    try:
        sch.maybe_refresh()
    except Exception:
        pass

    try:
        xml = core.fetch_feed()
    except Exception as e:
        print("feed fetch failed:", e)
        return
    all_vehicles = list(core.parse_vehicles(xml))

    legs_out = {}
    for i, leg in enumerate(core.LEGS):
        try:
            legs_out[leg["key"]] = _process_leg(leg, i, all_vehicles, now)
        except Exception as e:
            print(f"leg {leg['key']} error:", e)

    with _lock:
        _latest["generated"] = now.isoformat(timespec="seconds")
        _latest["legs"] = legs_out

    stamp = datetime.now(LONDON).strftime("%H:%M:%S")
    for key, data in legs_out.items():
        print(f"\n[{stamp}] {key}: next {data['line']} to {data['dest_label']} "
              f"@ {data['stop']}  ({data['n']} in box)")
        if data["gap_warning"]:
            print(f"   !! {data['gap_warning']}")
        if not data["arrivals"]:
            print("   (none nearby)")
        for r in data["arrivals"]:
            st = ("· " + r["status_text"]) if r["status_text"] else ""
            print(f"   {r['label']:<8} exp {r['expected']}  {st}  "
                  f"[{r['source']}] veh {r['vehicle']}")
        if data["model"]:
            print(f"   {data['model']}")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            payload = json.dumps(_latest).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass            # stay quiet, do not spam journald


def _serve():
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler).serve_forever()


def main():
    if not core.API_KEY:
        sys.exit("No API key. Put it in api_key.txt or $BODS_API_KEY.")

    if core.ACCURACY_LOG:
        _init_loggers()

    try:
        for leg in core.LEGS:
            sch.warm(leg["direction"], leg["stop_atco"])
        if core.refresh_bbox_from_timetable():
            print(f"bounding box widened to cover the route: "
                  f"lon {core.BBOX_MIN_LON:.4f}..{core.BBOX_MAX_LON:.4f}, "
                  f"lat {core.BBOX_MIN_LAT:.4f}..{core.BBOX_MAX_LAT:.4f}")
    except Exception as e:
        print("timetable warm failed (will retry):", e)

    once = "--once" in sys.argv
    if SERVE_HTTP and not once:
        threading.Thread(target=_serve, daemon=True).start()
        print(f"serving JSON on http://0.0.0.0:{HTTP_PORT}/")

    if once:
        cycle()
        with _lock:
            print(json.dumps(_latest, indent=2))
        return

    print(f"219 headless runner, {len(core.LEGS)} leg(s). "
          f"polling every {core.POLL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                cycle()
            except Exception as e:
                print("cycle error:", e)
            time.sleep(core.POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
