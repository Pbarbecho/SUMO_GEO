# SUMO-GEO

Web application to **manage SUMO mobility simulations in the browser**, with
**3D building extrusion** and **real-time traffic estimation**. The 3D runs
entirely in the browser (no install, no game engine) using the web-native
MapLibre GL JS + deck.gl stack — the same stack that
[GeoLibre](https://github.com/opengeos/GeoLibre) productizes.

```
 SUMO (Docker) ──TraCI──► FastAPI backend ──REST──►  network + buildings + traffic lights (GeoJSON)
                                          └─WebSocket► vehicles + per-edge LOS + signal state ──► MapLibre + deck.gl (browser)
```

> Prototype status. It ships a runnable demo scenario (a 6×6 grid with 400
> vehicles and synthetic building footprints, geo-anchored to Cuenca, Ecuador)
> so `docker compose up` shows moving, congestion-coloured traffic over 3D
> buildings out of the box. Swap in a real OSM city when you are ready
> (see *Using a real city* below).

## Quick start

```bash
docker compose up --build
# backend  -> http://localhost:8000/api/health
# frontend -> http://localhost:8080
```

Open **http://localhost:8080**: buildings extrude in 3D, vehicles stream live,
and roads recolour by Level of Service (A–F). Controls (top-left): pause,
reset, speed, and layer toggles.

### Run without Docker (dev)

```bash
# backend (installs SUMO via the eclipse-sumo wheel)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
APP_SUMO_CONFIG=../sumo/demo.sumocfg uvicorn app.main:app --reload

# frontend (any static server; API calls need the backend on :8000)
cd ../frontend && python -m http.server 8080
```

For the no-Docker frontend, point `API`/`WS_URL` in `frontend/app.js` at
`http://localhost:8000` (or serve behind the provided nginx proxy).

## Architecture

| Layer | Tech | Responsibility |
|-------|------|----------------|
| Simulation | **SUMO** + TraCI/libsumo | Microscopic mobility model |
| Backend | **FastAPI** (`backend/app`) | Steps the sim, converts network & polygons to GeoJSON, estimates per-edge congestion, streams frames over WebSocket |
| Frontend | **MapLibre GL JS + deck.gl** (`frontend`) | Basemap, `fill-extrusion` buildings, deck.gl vehicle & congestion layers |
| Delivery | **nginx** | Serves the SPA and reverse-proxies `/api` + `/ws` (same-origin, no CORS) |

### Backend modules

- `config.py` — env-driven settings (`APP_*`).
- `geo.py` — network → GeoJSON edges, polygons → extrudable buildings,
  **traffic-light positions** (one per approach, mast-arm vs. straight pole by
  building proximity), and coordinate conversion. Projected (OSM) nets use SUMO's
  projection; synthetic nets use the `APP_ORIGIN_LON/LAT` ENU anchor.
- `sumo_bridge.py` — TraCI/libsumo lifecycle (unique connection label per
  WebSocket), vehicles + live **signal state** per step, `managed` vs `remote`.
- `traffic.py` — density (veh/km/lane) → HCM-style LOS + colour ramp.
- `main.py` — REST (`/api/network`, `/api/buildings`, `/api/trafficlights`,
  `/api/meta`) + `WS /ws/live` (vehicles, per-edge LOS, signal colours).

### Connecting to *your* SUMO container (remote mode)

The default `managed` mode makes the backend self-contained. To reuse your
existing SUMO Docker container, start it as a TraCI server and switch modes:

```bash
# in your SUMO container
sumo -c /sumo/demo.sumocfg --remote-port 8813 --step-length 1.0 --start
```

```yaml
# docker-compose.yml (backend service)
environment:
  APP_SUMO_MODE: remote
  APP_SUMO_HOST: sumo
  APP_SUMO_PORT: "8813"
```

## Using a real city (buildings + roads)

The default demo is a **synthetic grid** anchored to Cuenca's coordinates — it
deliberately does NOT match the real streets under the basemap. For a real area,
build a **projected** OSM scenario; the backend then georeferences it with SUMO's
own `convertXY2LonLat` (uses `pyproj`) so buildings, roads and vehicles line up
exactly with the map.

One command (needs SUMO + internet on the host):

```bash
export SUMO_HOME=/path/to/sumo          # your SUMO install
./scripts/build_city.sh "-79.015,-2.912,-78.995,-2.892" cuenca
# bbox = minLon,minLat,maxLon,maxLat  (this one covers Cuenca's centro)
```

That downloads OSM, runs `netconvert` (roads) + `polyconvert` (building
footprints), enriches building heights from OSM tags (`scripts/enrich_heights.py`),
generates demand, and writes `sumo/cuenca.sumocfg`. Then point the backend at it
in `docker-compose.yml` and rebuild:

```yaml
environment:
  APP_SUMO_CONFIG: /sumo/cuenca.sumocfg
```

Don't have SUMO on the host? `pip install eclipse-sumo sumolib` and set
`SUMO_HOME=$(python -c "import os,sumolib;print(os.path.dirname(os.path.dirname(sumolib.__file__))+'/sumo')")`.

Alternatively, prepare/clean the building footprints in **GeoLibre** (OSM PBF or
Overture Maps → export GeoJSON) — it shares this exact MapLibre+deck.gl runtime.

> Note: projected networks require `pyproj` (already in `requirements.txt`). The
> synthetic grid uses the ENU anchor (`APP_ORIGIN_LON/LAT`) and needs no pyproj.

## Changing the map / scenario

Everything the viewer draws — the **road network**, **building footprints** and
**traffic lights** — is derived automatically from the SUMO scenario when the
backend starts. There is **no manual step to "generate" the traffic lights**: the
backend reads them from the network (`net.getTrafficLights()`), computes each
signal's position and its mast-arm vs. straight-pole style, and streams the live
signal colours from SUMO over TraCI. So changing the map is just:

1. Produce the new scenario (see *Using a real city* above, or bring your own
   `.net.xml` + `.poly.xml` + `.sumocfg`) and place it under `sumo/`.
2. Point the backend at it in `docker-compose.yml`:
   ```yaml
   environment:
     APP_SUMO_CONFIG: /sumo/<your>.sumocfg
   ```
3. Restart the backend so it recomputes the static geometry, then hard-refresh
   the browser (Cmd/Ctrl + Shift + R):
   ```bash
   docker compose up -d --build      # or: docker compose restart backend
   ```

The new network, buildings and signals appear automatically — nothing is cached
to disk, the geometry is rebuilt on each backend start.

**Requirements for the new network**

- **Traffic lights must exist in the `.net.xml`.** OSM often lacks them, so build
  the network with guessed signals:
  `netconvert --osm-files city.osm -o city.net.xml --tls.guess`. If the net has no
  TLS, no signals are shown (they simply don't exist — it's not an error).
  `scripts/build_city.sh` already enables `--tls.guess`.
- **Use the matching `.poly.xml`** for that area — buildings drive both the 3D
  extrusion and the mast-arm vs. straight-pole choice (a junction with no building
  within ~9 m gets a mast-arm). Tune the threshold via the `clearance` argument of
  `trafficlights_geojson` in `backend/app/geo.py`.
- Projected (OSM) networks georeference exactly via `pyproj`; synthetic grids use
  the `APP_ORIGIN_LON/LAT` ENU anchor.

## Tool evaluation

`docs/` contains the deliverables comparing the 3D options (Unity/SUMO2Unity,
Unreal/Sumo2Unreal, Blender/BlenderGIS, Godot, sumo3Dviz, and the web-native
MapLibre+deck.gl / CesiumJS / three.js paths) and GeoLibre, scored on
scalability, realism, SUMO interface and **browser viability**:

- `docs/Evaluacion_Herramientas_3D_SUMO.docx` — full evaluation report.
- `docs/Matriz_Comparativa_Herramientas.xlsx` — weighted scoring matrix.
- `ROADMAP.md` — phased technical plan.

**Bottom line for a browser-only prototype:** MapLibre GL JS + deck.gl
(GeoLibre stack) is the recommended runtime; game engines (Unity/Unreal/Godot)
and Blender don't meet the "no-install, in-browser" constraint at city scale;
sumo3Dviz is ideal for offline cinematic renders, not interactive control.

## Repository layout

```
backend/            FastAPI app (SUMO bridge, geo, traffic) + Dockerfile
frontend/           MapLibre + deck.gl SPA + nginx proxy config
sumo/               demo scenario: grid.net.xml, routes.rou.xml, buildings.poly.xml, demo.sumocfg
scripts/            generate_scenario.sh
docs/               evaluation report (.docx) + comparison matrix (.xlsx)
docker-compose.yml  managed-mode stack (backend + frontend)
ROADMAP.md          phased technical plan
```

## License

Code MIT. SUMO is EPL-2.0; MapLibre GL JS and deck.gl are BSD-3/MIT.
