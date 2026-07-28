#!/usr/bin/env bash
# Regenerate the demo SUMO scenario (grid network + routes + synthetic
# building footprints). Requires SUMO on PATH (SUMO_HOME set).
#
# To use a REAL city instead, replace this with an OSM import, e.g.:
#   python "$SUMO_HOME/tools/osmWebWizard.py"     # interactive, exports a scenario
# or `netconvert --osm-files city.osm ...` + `polyconvert ... --osm.keep-full-type`
# to get building polygons. The backend auto-georeferences projected networks.
set -euo pipefail
: "${SUMO_HOME:?Set SUMO_HOME to your SUMO installation}"
cd "$(dirname "$0")/../sumo"

echo "1/3  network (6x6 grid, 120 m blocks, 2 lanes, guessed traffic lights)"
netgenerate --grid --grid.number 6 --grid.length 120 \
  --default.lanenumber 2 --default.speed 13.9 --tls.guess \
  --no-turnarounds --output-file grid.net.xml

echo "2/3  random trips + routes (0..600 s)"
python "$SUMO_HOME/tools/randomTrips.py" -n grid.net.xml \
  -o trips.xml -r routes.rou.xml -b 0 -e 600 -p 1.5 --validate --seed 42

echo "3/3  synthetic building footprints (buildings.poly.xml)"
python - <<'PY'
import sumolib, random
random.seed(7)
net = sumolib.net.readNet("grid.net.xml")
xmin, ymin, xmax, ymax = net.getBoundary()
step = 120.0; polys = []; pid = 0; y = ymin
while y < ymax - step * 0.5:
    x = xmin
    while x < xmax - step * 0.5:
        cx, cy = x + step / 2, y + step / 2
        hw, hh = random.uniform(22, 30), random.uniform(22, 30)
        cx += random.uniform(-8, 8); cy += random.uniform(-8, 8)
        shape = [(cx-hw, cy-hh), (cx+hw, cy-hh), (cx+hw, cy+hh), (cx-hw, cy+hh), (cx-hw, cy-hh)]
        h = round(random.choice([9,12,15,18,24,30,36,45]) + random.uniform(-2, 4), 1)
        polys.append((f"bld_{pid}", shape, h)); pid += 1; x += step
    y += step
with open("buildings.poly.xml", "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n')
    for name, shape, h in polys:
        s = " ".join(f"{px:.2f},{py:.2f}" for px, py in shape)
        f.write(f'    <poly id="{name}" type="building" color="200,180,160" fill="1" layer="1" shape="{s}">\n')
        f.write(f'        <param key="height" value="{h}"/>\n    </poly>\n')
    f.write('</additional>\n')
print(f"buildings: {len(polys)}")
PY
echo "Done -> sumo/grid.net.xml, routes.rou.xml, buildings.poly.xml"
