"""
Little bus-stop departure board. Tracks every leg in route_config.LEGS (by
default: out and back) with a tab to switch which one is shown.

Two modes:
  REMOTE (default): a thin client. Reads already-computed legs from the Pi's
  JSON endpoint and draws them. No feed/timetable/model on this machine.

  LOCAL: set REMOTE_URL = None to compute everything in-process.

Shows a route strip (your stop at the right, buses placed by stops-away) plus
the next arrivals and a reliability panel. Display formatting is shared with
the Pi via presentation.py.

HONESTY: "~" before a time = estimate. on-time plain = measured delay, with a ~
= estimated. Not an official time.
"""

import threading
import time
import tkinter as tk
from datetime import datetime, timezone

import requests

import next_219 as core

# Point at the Pi's JSON endpoint to run as a thin client. None = compute locally.
REMOTE_URL = "http://fams:8219/"
REMOTE_POLL_SECONDS = 10

BG = "#0a0a0a"
PANEL = "#141414"
AMBER = "#ffb000"
AMBER_DIM = "#8a6000"
WHITE = "#e8e8e8"
GREY = "#6a6a6a"
GREEN = "#39d353"
RED = "#e0664f"
BLUE = "#5aa9e6"
MONO = "Consolas"
STATUS_COLOURS = {"ontime": GREEN, "late": RED, "early": BLUE}

REFRESH_MS = 500
MAX_ROWS = 3

_state = {"legs": {}, "updated": None, "error": None}
_lock = threading.Lock()
_stop = threading.Event()


def _publish(generated, legs, error=None):
    with _lock:
        _state["legs"] = legs
        _state["updated"] = generated
        _state["error"] = error


def worker_remote():
    while not _stop.is_set():
        try:
            data = requests.get(REMOTE_URL, timeout=10).json()
            gen = data.get("generated")
            generated = datetime.fromisoformat(gen) if gen else datetime.now(timezone.utc)
            _publish(generated, data.get("legs", {}))
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
        for _ in range(max(1, REMOTE_POLL_SECONDS) * 2):
            if _stop.is_set():
                return
            time.sleep(0.5)


def worker_local():
    import schedule_219 as sch
    import delay_model
    import calibrate
    import presentation
    import reliability

    def leg_paths(leg, i):
        if i == 0:
            return core.ACCURACY_LOG_PATH, core.ARRIVALS_LOG_PATH
        return f"eta_accuracy_log_{leg['key']}.csv", f"arrivals_log_{leg['key']}.csv"

    loggers = {}
    if core.ACCURACY_LOG:
        for i, leg in enumerate(core.LEGS):
            acc, arr = leg_paths(leg, i)
            loggers[leg["key"]] = core.AccuracyLogger(
                acc, core.ARRIVAL_RADIUS_KM, core.ARRIVAL_COOLDOWN_MIN, arr)
    try:
        for leg in core.LEGS:
            sch.warm(leg["direction"], leg["stop_atco"])
        core.refresh_bbox_from_timetable()
    except Exception:
        pass

    while not _stop.is_set():
        now = datetime.now(timezone.utc)
        try:
            sch.maybe_refresh()
        except Exception:
            pass
        try:
            all_vehicles = list(core.parse_vehicles(core.fetch_feed()))
            legs_out = {}
            for i, leg in enumerate(core.LEGS):
                acc, arr = leg_paths(leg, i)
                lv = []
                for v in all_vehicles:
                    v = dict(v)
                    if core.direction_ok_for(v, leg) is False:
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
                        if dm["passed"]:
                            v["approaching"] = False
                        else:
                            v["approaching"] = True
                            v["eta_min"] = dm["eta_min"]
                            v["source"] = "delay"
                    lv.append(v)
                if loggers.get(leg["key"]) is not None:
                    loggers[leg["key"]].update(now, lv)
                cands = [v for v in lv if v["approaching"] is not False]
                cands.sort(key=lambda x: x["eta_min"])
                try:
                    cal, _ = calibrate.load(acc)
                    core.CALIBRATION = cal
                    model_line = calibrate.model_status(acc)
                except Exception:
                    model_line = ""
                try:
                    rel = reliability.compute(arr, acc, leg["direction"], leg["stop_atco"])
                except Exception:
                    rel = None
                legs_out[leg["key"]] = {
                    "stop": leg["stop_name"], "line": leg["line_ref"],
                    "dest_label": leg["dest_label"], "model": model_line,
                    "n": len(lv), "arrivals": [presentation.row(v, now, leg) for v in cands[:5]],
                    "gap_warning": None, "reliability": rel,
                }
            _publish(now, legs_out)
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
        for _ in range(max(1, core.POLL_SECONDS) * 2):
            if _stop.is_set():
                return
            time.sleep(0.5)


class Board:
    def __init__(self, root):
        self.root = root
        self.selected = core.LEGS[0]["key"]
        root.title("Live bus")
        root.configure(bg=BG)
        root.minsize(560, 660)

        # leg tabs (only shown if there is more than one leg)
        self.tabs_frame = tk.Frame(root, bg=BG)
        self.tab_buttons = {}
        if len(core.LEGS) > 1:
            self.tabs_frame.pack(fill="x", padx=18, pady=(14, 0))
            for leg in core.LEGS:
                b = tk.Button(self.tabs_frame, text=f"→ {leg['dest_label']}",
                              font=(MONO, 9, "bold"), relief="flat", bd=0,
                              command=lambda k=leg["key"]: self._select(k))
                b.pack(side="left", padx=(0, 6))
                self.tab_buttons[leg["key"]] = b

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=18, pady=(10, 8))
        self.badge = tk.Label(head, text=core.LINE_REF, font=(MONO, 22, "bold"),
                              bg=AMBER, fg="#0a0a0a", padx=8)
        self.badge.pack(side="left")
        title = tk.Frame(head, bg=BG)
        title.pack(side="left", padx=12)
        self.title_lbl = tk.Label(title, text=f"to {core.DEST_LABEL}",
                                  font=(MONO, 14, "bold"), bg=BG, fg=WHITE, anchor="w")
        self.title_lbl.pack(anchor="w")
        self.stop_lbl = tk.Label(title, text=core.STOP_NAME, font=(MONO, 10),
                                 bg=BG, fg=GREY, anchor="w")
        self.stop_lbl.pack(anchor="w")
        self.live = tk.Label(head, text="● LIVE", font=(MONO, 10, "bold"), bg=BG, fg=GREEN)
        self.live.pack(side="right")

        self.gap_lbl = tk.Label(root, text="", font=(MONO, 10, "bold"),
                                bg=BG, fg=RED, anchor="w")
        self.gap_lbl.pack(fill="x", padx=18, pady=(0, 4))

        tk.Frame(root, bg="#262626", height=1).pack(fill="x", padx=18)

        self.strip = tk.Canvas(root, bg=BG, height=92, highlightthickness=0)
        self.strip.pack(fill="x", padx=18, pady=(8, 0))

        tk.Frame(root, bg="#262626", height=1).pack(fill="x", padx=18)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=10)
        self.rows = []
        for _ in range(MAX_ROWS):
            r = tk.Frame(body, bg=PANEL)
            r.pack(fill="x", pady=4, ipady=6)
            badge = tk.Label(r, text=core.LINE_REF, font=(MONO, 13, "bold"),
                             bg=PANEL, fg=AMBER, width=5)
            badge.pack(side="left", padx=(10, 6))
            mid = tk.Frame(r, bg=PANEL)
            mid.pack(side="left", fill="x", expand=True)
            dest = tk.Label(mid, text="", font=(MONO, 13), bg=PANEL, fg=WHITE, anchor="w")
            dest.pack(anchor="w")
            sub = tk.Label(mid, text="", font=(MONO, 9), bg=PANEL, fg=GREY, anchor="w")
            sub.pack(anchor="w")
            right = tk.Frame(r, bg=PANEL)
            right.pack(side="right", padx=(6, 12))
            eta = tk.Label(right, text="", font=(MONO, 16, "bold"), bg=PANEL,
                           fg=AMBER, width=11, anchor="e")
            eta.pack(anchor="e")
            ontime = tk.Label(right, text="", font=(MONO, 9), bg=PANEL, fg=GREY, anchor="e")
            ontime.pack(anchor="e")
            self.rows.append({"badge": badge, "dest": dest, "sub": sub,
                              "eta": eta, "ontime": ontime})

        tk.Frame(root, bg="#262626", height=1).pack(fill="x", padx=18)
        stats = tk.Frame(root, bg=BG)
        stats.pack(fill="x", padx=18, pady=(6, 0))
        self.rel_head = tk.Label(stats, text="RELIABILITY", font=(MONO, 9, "bold"),
                                 bg=BG, fg=GREY, anchor="w")
        self.rel_head.pack(anchor="w")
        self.rel_summary = tk.Label(stats, text="", font=(MONO, 11), bg=BG, fg=WHITE,
                                    anchor="w")
        self.rel_summary.pack(anchor="w")
        self.rel_canvas = tk.Canvas(stats, bg=BG, height=54, highlightthickness=0)
        self.rel_canvas.pack(fill="x", pady=(2, 0))

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=18, pady=(6, 12))
        self.status = tk.Label(foot, text="connecting..." if REMOTE_URL else "starting...",
                               font=(MONO, 9), bg=BG, fg=GREY, anchor="w")
        self.status.pack(anchor="w")
        self.model = tk.Label(foot, text="", font=(MONO, 9, "bold"), bg=BG, fg=GREEN, anchor="w")
        self.model.pack(anchor="w")
        tk.Label(foot, text="strip: right = your stop, dots = stops, markers = buses.",
                 font=(MONO, 9), bg=BG, fg=AMBER_DIM, anchor="w").pack(anchor="w")
        tk.Label(foot, text="on-time: plain = measured, ~ = estimated. not official.",
                 font=(MONO, 9), bg=BG, fg=AMBER_DIM, anchor="w").pack(anchor="w")

        self._blink = True
        self._update_tabs()
        self.refresh()

    def _select(self, key):
        self.selected = key
        self._update_tabs()

    def _update_tabs(self):
        for key, b in self.tab_buttons.items():
            active = key == self.selected
            b.config(bg=AMBER if active else PANEL, fg="#0a0a0a" if active else GREY)

    def draw_strip(self, rows):
        c = self.strip
        c.delete("all")
        w = c.winfo_width()
        if w < 50:
            w = 520
        xr, xl, y = w - 46, 72, 60
        c.create_line(xl, y, xr, y, fill="#333", width=3)

        buses = [r for r in rows
                 if isinstance(r.get("stops_away"), int) and r["stops_away"] >= 0]
        disp = max(1, min(16, max([b["stops_away"] for b in buses], default=1)))

        def px(s):
            return xr - (min(s, disp) / disp) * (xr - xl)

        for k in range(disp + 1):
            x = px(k)
            if k == 0:
                c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=AMBER, outline="")
            else:
                c.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#555", outline="")
        c.create_text(xr, y + 17, text="YOUR STOP", fill=AMBER, font=(MONO, 8, "bold"))
        c.create_text(xl, y + 17, text=f"{disp} stops", fill=GREY, font=(MONO, 8))

        if not buses:
            c.create_text((xl + xr) / 2, y - 24, text="no buses approaching",
                          fill=GREY, font=(MONO, 9))
            return
        for b in buses:
            x = px(b["stops_away"])
            col = STATUS_COLOURS.get(b.get("status_kind"), AMBER)
            c.create_line(x, y - 4, x, y - 16, fill=col)
            c.create_oval(x - 6, y - 28, x + 6, y - 16, fill=col, outline="")
            c.create_text(x, y - 37, text=b.get("label", ""), fill=col, font=(MONO, 8, "bold"))

    def draw_stats(self, rel):
        c = self.rel_canvas
        c.delete("all")
        if not rel or not rel.get("n"):
            self.rel_head.config(text="RELIABILITY")
            self.rel_summary.config(text="collecting arrivals...")
            return
        self.rel_head.config(
            text=f"RELIABILITY · {rel['source']} · {rel['n']} buses · {rel.get('window', '')}")
        self.rel_summary.config(
            text=f"{rel['on_time_pct']}% on time   ·   median {rel['median']:+.1f} min")
        bh = rel.get("by_hour", [])
        if not bh:
            return
        w = c.winfo_width()
        if w < 50:
            w = 520
        pad, base, maxbar = 28, 40, 32
        slot = (w - 2 * pad) / max(1, len(bh))
        bw = max(8, min(24, slot - 6))
        for i, (h, cnt, otp, avg, snap_m) in enumerate(bh):
            x = pad + i * slot + slot / 2
            col = GREEN if otp >= 80 else (AMBER if otp >= 50 else RED)
            c.create_rectangle(x - bw / 2, base - maxbar * otp / 100,
                               x + bw / 2, base, fill=col, outline="")
            c.create_text(x, base + 8, text=f"{h:02d}", fill=GREY, font=(MONO, 7))

    def refresh(self):
        try:
            self._render()
        except Exception as e:
            # Never let a drawing error (e.g. a data-shape change) silently kill
            # the periodic refresh - that freezes the whole window with no sign
            # of why. Show it in the status line instead, and keep polling.
            self.status.config(text=f"display error: {e}"[:70], fg=RED)
        self.root.after(REFRESH_MS, self.refresh)

    def _render(self):
        with _lock:
            legs = dict(_state["legs"])
            updated = _state["updated"]
            err = _state["error"]

        data = legs.get(self.selected, {})
        rows = data.get("arrivals", [])
        n = data.get("n", 0)
        model_line = data.get("model", "")
        line = data.get("line", core.LINE_REF)
        dest_label = data.get("dest_label", core.DEST_LABEL)
        stop = data.get("stop", core.STOP_NAME)
        reliability = data.get("reliability")
        gap_warning = data.get("gap_warning")

        self.badge.config(text=line)
        self.title_lbl.config(text=f"to {dest_label}")
        self.stop_lbl.config(text=stop)
        self.gap_lbl.config(text=f"!! {gap_warning}" if gap_warning else "")
        self.draw_strip(rows)
        self.draw_stats(reliability)

        for i, w in enumerate(self.rows):
            if i < len(rows):
                r = rows[i]
                dest = r.get("dest", "") + ("  ?" if r.get("unconfirmed") else "")
                w["badge"].config(text=line)
                w["dest"].config(text=dest, fg=WHITE)
                sub = f"expected ~{r.get('expected', '--:--')}"
                if r.get("sched"):
                    sub += f"   ·   sched {r['sched']}"
                sa = r.get("stops_away")
                if sa is not None:
                    sub += "   ·   at stop" if sa <= 0 else \
                        f"   ·   {sa} stop{'' if sa == 1 else 's'} away"
                w["sub"].config(text=sub)
                w["eta"].config(text=r.get("label", ""), fg=GREEN if r.get("due") else AMBER)
                col = STATUS_COLOURS.get(r.get("status_kind"), GREY)
                w["ontime"].config(text=r.get("status_text", ""), fg=col)
            else:
                w["badge"].config(text="")
                w["dest"].config(
                    text=f"no {line} toward {dest_label} nearby" if i == 0 else "", fg=GREY)
                w["sub"].config(text="")
                w["eta"].config(text="")
                w["ontime"].config(text="")

        src = "via Pi · " if REMOTE_URL else ""
        now = datetime.now(timezone.utc)
        if err:
            self.status.config(text=f"{src}error, retrying: {err[:42]}", fg=RED)
            self.live.config(fg=RED)
        elif updated:
            age = int((now - updated).total_seconds())
            self.status.config(
                text=f"{src}updated {age}s ago  ·  {n} in range  ·  "
                     f"{datetime.now().strftime('%H:%M:%S')}", fg=GREY)
            self._blink = not self._blink
            self.live.config(fg=GREEN if self._blink else BG)

        good = model_line.startswith(("model: delay", "model: learned"))
        self.model.config(text=model_line, fg=GREEN if good else AMBER_DIM)


def main():
    if not REMOTE_URL and not core.API_KEY:
        raise SystemExit("No API key (api_key.txt or $BODS_API_KEY), or set REMOTE_URL to a Pi.")

    target = worker_remote if REMOTE_URL else worker_local
    threading.Thread(target=target, daemon=True).start()

    root = tk.Tk()
    Board(root)

    def on_close():
        _stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
