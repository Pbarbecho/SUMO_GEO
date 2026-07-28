#!/usr/bin/env bash
# Build a REAL, georeferenced city scenario (roads + building polygons) from
# OpenStreetMap, so it aligns exactly with the web map. The resulting network
# carries a UTM projection, so the backend georeferences it with SUMO's own
# convertXY2LonLat (requires pyproj) instead of the synthetic ENU anchor.
#
# Usage:   SUMO_HOME=/path/to/sumo  ./scripts/build_city.sh "W,S,E,N" [name]
# Cuenca:  ./scripts/build_city.sh "-79.015,-2.912,-78.995,-2.892" cuenca
#          (bbox = minLon,minLat,maxLon,maxLat)
#
# Requires SUMO tools (netconvert, polyconvert, tools/osmGet.py, randomTrips.py)
# and internet access (OpenStreetMap / Overpass).
set -euo pipefail
PY="$(command -v python3 || command -v python || true)"   # macOS usually has python3 only
: "${PY:?Python not found (need python3 or python on PATH)}"

# Locate SUMO robustly: use a valid SUMO_HOME if given, else find the pip wheel
# (works whether eclipse-sumo exposes `import sumo` or sits next to sumolib).
if [ -z "${SUMO_HOME:-}" ] || [ ! -f "${SUMO_HOME}/tools/osmGet.py" ]; then
  for CAND in \
    "$("$PY" -c "import os,sumo;print(os.path.dirname(sumo.__file__))" 2>/dev/null || true)" \
    "$("$PY" -c "import os,sumolib;print(os.path.dirname(os.path.dirname(sumolib.__file__)))" 2>/dev/null || true)/sumo" ; do
    if [ -n "$CAND" ] && [ -f "$CAND/tools/osmGet.py" ]; then SUMO_HOME="$CAND"; break; fi
  done
fi
if [ ! -f "${SUMO_HOME:-}/tools/osmGet.py" ]; then
  echo "ERROR: no encuentro SUMO (falta tools/osmGet.py)." >&2
  echo "Instala las herramientas:  $PY -m pip install eclipse-sumo sumolib" >&2
  exit 1
fi
export SUMO_HOME
export PATH="$SUMO_HOME/bin:$PATH"   # ensure netconvert/polyconvert resolve
echo "SUMO_HOME: $SUMO_HOME"
BBOX="${1:?Pass a bbox (west,south,east,north): e.g. -79.010,-2.903,-79.000,-2.893}"
NAME="${2:-cuenca}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../sumo"

echo "1/4  Descargando OSM ($BBOX)…"
# NOTE: --bbox="$BBOX" (attached with =) so argparse doesn't read the leading
# minus of the west longitude as a new option flag.
"$PY" "$SUMO_HOME/tools/osmGet.py" --bbox="$BBOX" -p "$NAME"
OSM="${NAME}_bbox.osm.xml"

echo "2/4  Red de calles (proyectada UTM)…"
netconvert --osm-files "$OSM" -o "${NAME}.net.xml" \
  --geometry.remove --ramps.guess --junctions.join \
  --tls.guess-signals --tls.discard-simple --tls.join \
  --remove-edges.isolated --keep-edges.by-vclass passenger

echo "3/4  Polígonos de edificios (+ alturas desde OSM)…"
polyconvert --osm-files "$OSM" --net-file "${NAME}.net.xml" \
  --type-file "$SUMO_HOME/data/typemap/osmPolyconvert.typ.xml" \
  -o "${NAME}.poly.xml"
"$PY" "$HERE/enrich_heights.py" "$OSM" "${NAME}.poly.xml"

echo "4/4  Demanda de tráfico + sumocfg…"
"$PY" "$SUMO_HOME/tools/randomTrips.py" -n "${NAME}.net.xml" \
  -o "${NAME}.trips.xml" -r "${NAME}.rou.xml" -b 0 -e 600 -p 2 --validate --seed 42
cat > "${NAME}.sumocfg" <<CFG
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file value="${NAME}.net.xml"/>
        <route-files value="${NAME}.rou.xml"/>
        <additional-files value="${NAME}.poly.xml"/>
    </input>
    <time><begin value="0"/><end value="600"/><step-length value="1.0"/></time>
    <processing><ignore-route-errors value="true"/></processing>
    <report><no-step-log value="true"/></report>
</configuration>
CFG

echo ""
echo "OK -> sumo/${NAME}.sumocfg"
echo "Apunta el backend a este escenario en docker-compose.yml:"
echo "    APP_SUMO_CONFIG: /sumo/${NAME}.sumocfg"
echo "Luego:  docker compose up --build"
echo "La red es proyectada => backend usa convertXY2LonLat (pyproj) y alinea con MapLibre."
