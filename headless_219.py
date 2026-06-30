"""
Headless 219 runner for a Raspberry Pi (no GUI).

Runs the full autonomous pipeline (live feed, delay model, auto-refreshing
timetable, accuracy logging, self-calibration), prints the next arrivals to
stdout (journald under systemd), AND serves the current arrivals as JSON over
HTTP so thin clients (the desktop GUI, an ESP32 sign) can just read them.

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

_latest = {"stop": core.STOP_NAME, "line": core.LINE_REF,
           "dest_label": core.DEST_LABEL, "generated": None,
           "model": "", "n": 0, "arrivals": [], "reliability": None}
_lock = threading.Lock()


def cycle(logger):
    now = datetime.now(timezone.utc)

    try:
        sch.maybe_refresh()
    except Exception:
        pass
    try:
        cal, _ = calibrate.load()
        core.CALIBRATION = cal
        model = calibrate.model_status()
    except Exception:
        model = ""

    vehicles = [core.estimate(v, now) for v in core.parse_vehicles(core.fetch_feed())]
    for v in vehicles:
        try:
            dm = delay_model.predict(v, now)
        except Exception:
            dm = None
        if not dm:
            continue
        v["stops_away"] = dm["stops_away"]
        v["route_arrived"] = dm["arrived"]
        v["delay_secs"] = dm["delay_secs"]
        if dm["passed"]:
            v["approaching"] = False
        else:
            v["approaching"] = True
            v["eta_min"] = dm["eta_min"]
            v["source"] = "delay"
    if logger is not None:
        logger.update(now, vehicles)

    cands = [v for v in vehicles
             if core.direction_ok(v) is not False and v["approaching"] is not False]
    cands.sort(key=lambda x: x["eta_min"])
    rows = [presentation.row(v, now) for v in cands[:5]]

    try:
        rel = reliability.compute()
    except Exception:
        rel = None

    with _lock:
        _latest["generated"] = now.isoformat(timespec="seconds")
        _latest["model"] = model
        _latest["n"] = len(vehicles)
        _latest["arrivals"] = rows
        _latest["reliability"] = rel

    stamp = datetime.now(LONDON).strftime("%H:%M:%S")
    print(f"\n[{stamp}] next 219 to Manchester @ {core.STOP_NAME}  ({len(vehicles)} in box)")
    if not rows:
        print("   (none nearby)")
    for r in rows:
        st = ("· " + r["status_text"]) if r["status_text"] else ""
        print(f"   {r['label']:<8} exp {r['expected']}  {st}  [{r['source']}] veh {r['vehicle']}")
    if model:
        print(f"   {model}")


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

    logger = None
    if core.ACCURACY_LOG:
        logger = core.AccuracyLogger(
            core.ACCURACY_LOG_PATH, core.ARRIVAL_RADIUS_KM, core.ARRIVAL_COOLDOWN_MIN,
            core.ARRIVALS_LOG_PATH)

    try:
        sch.warm()
    except Exception as e:
        print("timetable warm failed (will retry):", e)

    once = "--once" in sys.argv
    if SERVE_HTTP and not once:
        threading.Thread(target=_serve, daemon=True).start()
        print(f"serving JSON on http://0.0.0.0:{HTTP_PORT}/")

    if once:
        cycle(logger)
        with _lock:
            print(json.dumps(_latest, indent=2))
        return

    print(f"219 headless runner. polling every {core.POLL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                cycle(logger)
            except Exception as e:
                print("cycle error:", e)
            time.sleep(core.POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
