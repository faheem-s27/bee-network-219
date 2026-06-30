"""
Discover the config values for a route from the live feed.

To switch the tracker to a new line/stop:
  1. In next_219.py set LINE_REF and the STOP_* values (ATCO + lat/lon, looked up
     on https://bustimes.org). The bounding box derives from the stop.
  2. Run `python discover.py`. It prints the OperatorRef, DirectionRef, and
     DestinationName the feed actually uses for your line right now.
  3. Copy those into next_219.py: OPERATOR_NOC, DIRECTION (the one toward your
     destination), and DEST_KEYWORDS / DEST_LABEL.

No guessing: you set the values to what the feed literally reports.
"""

import collections
import xml.etree.ElementTree as ET

import next_219 as core


def main():
    root = ET.fromstring(core.fetch_feed())
    seen = collections.Counter()
    n = 0
    for va in root.iterfind(".//s:VehicleActivity", core.NS):
        mvj = va.find("s:MonitoredVehicleJourney", core.NS)
        line = core.text_of(mvj, "s:LineRef") or core.text_of(mvj, "s:PublishedLineName")
        if line != core.LINE_REF:
            continue
        n += 1
        seen[(
            core.text_of(mvj, "s:OperatorRef"),
            core.text_of(mvj, "s:DirectionRef"),
            core.text_of(mvj, "s:DestinationName") or core.text_of(mvj, "s:DestinationRef"),
        )] += 1

    print(f"line {core.LINE_REF}: {n} vehicle(s) in the box around {core.STOP_NAME}")
    if not n:
        print("none right now; run again when buses are out.")
        return
    print(f"\n{'OperatorRef':12} {'DirectionRef':12} DestinationName")
    print("-" * 52)
    for (op, dr, dn), c in seen.most_common():
        print(f"{str(op):12} {str(dr):12} {dn}  (x{c})")

    # Best guess at the toward-you direction: the most common one is usually it,
    # but you choose by which DestinationName is where you are heading.
    op, dr, dn = seen.most_common(1)[0][0]
    label = (dn or "").replace("_", " ")
    kw = [w.lower() for w in label.split() if len(w) > 3][:2]
    print("\nPaste into route_config.py (pick DIRECTION by your destination):")
    print(f'  OPERATOR_NOC  = "{op}"')
    print(f'  DIRECTION     = "{dr}"')
    print(f'  DEST_KEYWORDS = {kw}')
    print(f'  DEST_LABEL    = "{label}"')


if __name__ == "__main__":
    main()
