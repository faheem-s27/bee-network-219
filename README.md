# Live bus tracker (Bee Network 219)

A live "next bus" tracker built on the DfT Bus Open Data Service (BODS). It pulls
the SIRI-VM feed (raw GPS positions of every bus), works out when the next few
buses reach a chosen stop, and shows them on a little LED-style departure board.

The same open data the Bee Network app reads, turned into honest ETAs you can
inspect the failure modes of.

## What makes it more than a toy

- **Delay model, not a straight-line guess.** BODS gives positions, not arrival
  predictions. Early versions divided straight-line distance by an assumed speed
  (error band ~7 min, worse with distance). The current model snaps each bus to
  its journey in the published timetable, measures its live delay against the
  schedule, and projects that to your stop. Measured accuracy: **~1 min median,
  90% within ~3 min, flat across distance.**
- **Self-measuring.** Every arrival is logged (predicted vs actual). The model
  reports its own real accuracy, and the geometry fallback self-calibrates.
- **Reliability stats** you cannot get from the app: on-time %, median delay, and
  a by-hour breakdown of when the route is actually dependable.
- **Honest by design.** Estimates are marked with `~`; measured delays are shown
  plainly; the on-time standard used is the UK one (no more than 1 min early /
  5 min late). Where a number is reconstructed or noisy, it says so.

## Architecture

```
Raspberry Pi (always-on)                 Desktop (optional)
- pull SIRI-VM feed                HTTP   - thin-client GUI
- delay model + timetable    ───────────► - reads JSON, draws the board
- accuracy log + calibration   JSON :8219 - a route strip + reliability panel
- serves JSON
```

The Pi does all the work (runs `headless_219.py` as a systemd service) and serves
the current state as JSON. The desktop GUI is just a viewer. Set `REMOTE_URL =
None` in `bus_sign.py` to run everything locally instead.

## Files

| File | Role |
|------|------|
| `route_config.py` | **The only file you edit to switch line/stop.** |
| `next_219.py` | Feed fetch, parsing, geometry ETA, accuracy logger |
| `schedule_219.py` | Downloads + parses the timetable (auto-refreshing) |
| `delay_model.py` | The accurate ETA: timetable run-times + live delay |
| `calibrate.py` | Learns the geometry fallback speed from the log |
| `reliability.py` | On-time % and by-hour stats |
| `presentation.py` | Shared display formatting (Pi and GUI agree) |
| `headless_219.py` | The Pi service: runs everything, serves JSON |
| `bus_sign.py` | The desktop GUI (Tkinter departure board) |
| `discover.py` | Prints the live operator/direction values when switching |

## Setup

```bash
pip install requests
echo YOUR_BODS_KEY > api_key.txt        # or export BODS_API_KEY=...
python bus_sign.py                      # set REMOTE_URL=None for standalone
```

Get a free key at https://data.bus-data.dft.gov.uk .

## Switching to a different line / stop

1. Find your stop on https://bustimes.org , copy its ATCO code + lat/lon into
   `route_config.py`, set the line number.
2. Run `python discover.py` — it prints the exact operator/direction/destination
   values to paste back.
3. Restart (on the Pi: `sudo systemctl restart bus219`). The GUI follows.

## Honest limitations

- No data source gives a *guaranteed* arrival; a position-based ETA cannot
  foresee a specific jam. The model gives a best estimate plus a real error band.
- Reliability stats need a few days of arrivals to be representative.
- The timetable match relies on the operator publishing it to BODS; if a dataset
  is superseded the tracker re-discovers it automatically.

Built with the DfT Bus Open Data Service. Not affiliated with Bee Network / TfGM.
