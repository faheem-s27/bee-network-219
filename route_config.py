"""
========================================================================
THE ONLY FILE YOU EDIT TO CHANGE THE BUS / STOP(S).
========================================================================

LEGS is a list of directions to track. Two by default: out and back. Each entry
is independent (its own stop, direction, destination match) so a round trip just
means two entries pointing at the two stops for the same road.

To switch or add a leg:

  1. Find the stop on https://bustimes.org . The stop page shows the ATCO code
     and coordinates. Pick the stop on the correct side of the road for the
     direction that leg travels.

  2. Run:  python discover.py
     It reads the live feed and prints the exact operator_noc, direction,
     dest_keywords, dest_label for a line right now. No guessing.

  3. If running on the Pi, restart it:  sudo systemctl restart bus219
     (Edit this file ON THE PI - the Pi is what does the tracking.)

To track only one direction, just leave one entry in LEGS.
"""

LEGS = [
    {
        "key": "to_manchester",
        "line_ref": "219",
        "operator_noc": "BNML",
        "stop_name": "Openshaw, near Lees Street",
        "stop_atco": "1800EB34051",
        "stop_lat": 53.472860,
        "stop_lon": -2.168243,
        "direction": "inbound",                       # feed's DirectionRef
        "dest_keywords": ["piccadilly", "manchester"],  # fallback text match
        "dest_label": "Manchester City Centre",
    },
    {
        "key": "to_ashton",
        "line_ref": "219",
        "operator_noc": "BNML",
        "stop_name": "Openshaw, opp Lees Street",
        "stop_atco": "1800EB34041",
        "stop_lat": 53.473030,
        "stop_lon": -2.168876,
        "direction": "outbound",
        "dest_keywords": ["ashton", "stalybridge", "glossop"],
        "dest_label": "Ashton-under-Lyne",
    },
]

# Back-compat single-route constants (= LEGS[0]). Old scripts / discover.py's
# defaults / standalone geometry-fallback code still read these.
LINE_REF = LEGS[0]["line_ref"]
OPERATOR_NOC = LEGS[0]["operator_noc"]
STOP_NAME = LEGS[0]["stop_name"]
STOP_ATCO = LEGS[0]["stop_atco"]
STOP_LAT = LEGS[0]["stop_lat"]
STOP_LON = LEGS[0]["stop_lon"]
DIRECTION = LEGS[0]["direction"]
DEST_KEYWORDS = LEGS[0]["dest_keywords"]
DEST_LABEL = LEGS[0]["dest_label"]
