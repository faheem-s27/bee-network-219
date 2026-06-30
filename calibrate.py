"""
Self-calibration for the 219 ETA, learned from the accuracy log.

Each logged arrival gives a clean ground-truth pair: how far the bus was
(dist_km_at_obs) and how long it ACTUALLY took to reach the stop
(actual_eta = predicted_eta_min - error_min, which equals actual - observed and
is independent of whatever model made the original prediction). We fit
actual_eta as a straight line in distance:

    actual_eta_min = slope * dist_km + intercept

slope is minutes-per-km on this corridor (effective speed = 60 / slope km/h),
intercept soaks up near-stop dwell. The spread of residuals is the honest
confidence band: it is the scatter we genuinely cannot predict (traffic, lights).

What this fixes: the systematic late-bias of the flat 18 km/h guess.
What it does NOT fix: a specific jam today. That stays in the band.

Honesty guards:
- Refuse to hand back a model below MIN_SAMPLES (cold start would be noise).
- Refuse a nonsensical fit (slope <= 0).
- The band is real scatter, not a decoration. Show it.
"""

import csv
import os
import statistics

LOG_PATH = "eta_accuracy_log.csv"
MIN_SAMPLES = 15        # below this, the learned model is noise; use the default
MAX_DIST_KM = 6.0       # ignore far-away outliers
MAX_ETA_MIN = 60.0


class Calibration:
    def __init__(self, slope, intercept, band_min, n):
        self.slope = slope
        self.intercept = intercept
        self.band_min = band_min     # 1-sigma residual, minutes
        self.n = n

    def eta(self, dist_km):
        return max(0.0, self.slope * dist_km + self.intercept)

    @property
    def speed_kmh(self):
        return 60.0 / self.slope if self.slope > 0 else float("nan")


def _samples(path):
    """Return [(dist_km, actual_eta_min), ...] from the log."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                dist = float(row["dist_km_at_obs"])
                pred = float(row["predicted_eta_min"])
                err = float(row["error_min"])
            except (KeyError, ValueError):
                continue
            actual_eta = pred - err          # = actual_arrival - observed_at
            if 0.0 <= dist <= MAX_DIST_KM and 0.0 < actual_eta <= MAX_ETA_MIN:
                out.append((dist, actual_eta))
    return out


def load(path=LOG_PATH):
    """Build a Calibration from the log, or None if not enough good data."""
    pts = _samples(path)
    n = len(pts)
    if n < MIN_SAMPLES:
        return None, n

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    if slope <= 0:                            # implausible (speed <= 0 or negative)
        return None, n

    resid = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    band = statistics.pstdev(resid) if len(resid) > 1 else 0.0
    return Calibration(slope, intercept, band, n), n


def summary(path=LOG_PATH):
    """Human-readable one-liner about the geometry fallback model."""
    cal, n = load(path)
    if cal is None:
        return f"model: default 18 km/h · learning ({n}/{MIN_SAMPLES} arrivals)"
    return (f"model: learned from {cal.n} arrivals · "
            f"{cal.speed_kmh:.0f} km/h · typical error +/-{cal.band_min:.1f} min")


MIN_DELAY_ARRIVALS = 5


def delay_stats(path=LOG_PATH):
    """(n_arrivals, n_obs, median_abs, p90_abs) of the DELAY model's errors, or None.

    Each arrival is logged ~50 times as the bus approaches, so those rows are
    correlated. We report the count of DISTINCT arrivals (unique vehicle +
    actual arrival time) for honesty, while the error spread is over all rows.
    """
    errs = []
    arrivals = set()
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("source") != "delay":
                continue
            try:
                errs.append(abs(float(r["error_min"])))
            except (KeyError, ValueError):
                continue
            arrivals.add((r.get("vehicle"), r.get("actual_arrival")))
    if len(arrivals) < MIN_DELAY_ARRIVALS:
        return None
    errs.sort()
    p90 = errs[min(len(errs) - 1, int(0.9 * len(errs)))]
    return len(arrivals), len(errs), statistics.median(errs), p90


def model_status(path=LOG_PATH):
    """One-liner describing the model actually driving the board: the delay model
    if it has a track record, otherwise the geometry learning state."""
    d = delay_stats(path)
    if d:
        n_arr, _n_obs, med, p90 = d
        return (f"model: delay (timetable) · {n_arr} arrivals · "
                f"typically {med:.1f} min, 90% within {p90:.1f} min")
    return summary(path)


if __name__ == "__main__":
    cal, n = load()
    print(summary())
    if cal:
        for d in (0.2, 0.5, 1.0, 2.0):
            print(f"  {d:.1f} km -> ~{cal.eta(d):.1f} min  (+/- {cal.band_min:.1f})")
