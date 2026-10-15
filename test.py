import json
import os

import pyzill

# Set ZILLOW_PROXY in the environment (residential proxy) before running.
proxy_url = os.environ.get("ZILLOW_PROXY")
ne_lat = 38.602951833355434
ne_long = -87.22283859375
sw_lat = 23.42674607019482
sw_long = -112.93084640625
pagination = 1
# pagination is for the list on the right when searching. You don't need to
# iterate over all pages because Zillow sends everything in mapResults on the
# first page -- but it caps at 500 results. If you hit 500, adjust the zoom or
# the coordinates; pagination won't help.

results_rent = pyzill.for_rent(
    pagination=pagination,
    search_value="",
    is_entire_place=False,
    is_room=True,
    min_beds=1,
    max_beds=None,
    min_bathrooms=None,
    max_bathrooms=None,
    min_price=10000,
    max_price=None,
    ne_lat=ne_lat,
    ne_long=ne_long,
    sw_lat=sw_lat,
    sw_long=sw_long,
    zoom_value=15,
    proxy_url=proxy_url,
)
with open("./jsondata_rent2.json", "w") as f:
    f.write(json.dumps(results_rent))
