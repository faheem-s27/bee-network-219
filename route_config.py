"""
========================================================================
THE ONLY FILE YOU EDIT TO CHANGE THE BUS / STOP.
========================================================================

To switch to a different line and stop:

  1. Find your stop on https://bustimes.org . Open the stop page. It shows the
     ATCO code (e.g. 1800EB34051) and the coordinates. Put them below, and set
     LINE_REF to the bus number. Pick the stop on the correct side of the road
     for the direction you travel.

  2. Run:  python discover.py
     It reads the live feed and prints the exact OPERATOR_NOC, DIRECTION,
     DEST_KEYWORDS and DEST_LABEL lines to paste back here. No guessing.

  3. If running on the Pi, restart it:  sudo systemctl restart bus219
     (Edit this file ON THE PI, because the Pi is what does the tracking.)
"""

LINE_REF = "219"
OPERATOR_NOC = "BNML"                       # operator code (discover.py prints it)

STOP_NAME = "Openshaw, near Lees Street"    # any label you like
STOP_ATCO = "1800EB34051"
STOP_LAT = 53.472860
STOP_LON = -2.168243

DIRECTION = "inbound"                        # the feed's DirectionRef toward you go
DEST_KEYWORDS = ["piccadilly", "manchester"]  # fallback text match
DEST_LABEL = "Manchester City Centre"          # shown as the GUI title
