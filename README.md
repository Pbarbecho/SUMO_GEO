# SUMO-GEO

Web application to **run and visualise SUMO mobility simulations in the browser**,
with **3D building extrusion**, **real-time traffic estimation** and
**SUMO-synchronised traffic lights**. Everything renders in the browser — no
install, no game engine — on the web-native **MapLibre GL JS + deck.gl** stack
(the same stack that [GeoLibre](https://github.com/opengeos/GeoLibre) productizes).

![SUMO-GEO architecture](sumo_geo_frontend_backend_arquitectura.png)

```
 SUMO (Docker) ──TraCI──► FastAPI backend ──REST──────► network + buildings + traffic lights (GeoJSON)
                                          └─WebSocket──► vehicles + per-edge LOS + live signal state ──► MapLibre + deck.gl (browser)
```

> **Prototype / research preview.** Built at Universidad de Cuenca (Ecuador). The
> stack is self-contained: `docker compose up` starts SUMO, streams live traffic,
> colours roads by Level of Service, extrudes buildings and draws signalised
> intersections that follow SUMO's phases in real time.

---

## Features

- **In-browser 3D** — MapLibre `fill-extrusion` buildings + a deck.gl overlay for
  roads, vehicles and traffic lights. WebGL interleaved compositing, so buildings
  and signal poles occlude each other correctly in perspective.
- **Live SUMO stream** — each viewer drives its own SUMO run over TraCI; vehicles,
  per-edge congestion and signal colours update every simulation step.
- **Real-time traffic estimation** — per-edge density → HCM-style Level of Service
  (A–F) colour ramp, computed live from TraCI edge measures.
- **SUMO-synchronised traffic lights** — signal heads (3-lens housing) coloured
  from SUMO's live phase state; open junctions get a mast-arm ("ménsula") over the
  road, tight ones a straight roadside pole. Positions are derived automatically
  from the network — no manual placement.
- **Scenario selector (Bajo / Medio / Alto)** — switch traffic-demand level from
  the app; the backend hot-swaps the matching SUMO route file.
- **24 h diurnal demand** — synthetic demand with realistic AM/PM rush peaks, a
  catalogue of real passenger-car models (`vtypes.add.xml`) plus buses.
- **Camera controls** — zoom, rotate, auto-orbit and a one-click 2D top-down ⇄ 3D
  perspective toggle.
- **Real cities from OSM** — one script imports roads + building footprints from
  OpenStreetMap, georeferenced so everything lines up with the basemap.

---

## Quick start

```bash
docker compose up --build
# backend  -> http://localhost:8000/api/health
# frontend -> http://localhost:8081
```

Open **http://localhost:8081**. Roads recolour by congestion, vehicles stream
live, and signalised intersections follow SUMO. Use the panel (top-left) and the
toolbar (top-centre) to control the view and the simulation.

> **First-clone note.** The default scenario is the **metropolitan Cuenca**
> network (`mapa/`). Its `network.net.xml` (~93 MB) is **git-ignored** (too large
> for a normal repo), so on a fresh clone you must either regenerate it (see
> *Building a real city from OSM*) **or** switch the default to the lighter
> **Cuenca-centro** scenario (`sumo/`), whose network *is* committed and which also
> ships 3D buildings — see *Scenarios & traffic levels*.

---

## Using the app

**Panel (top-left)**

| Control | Action |
|---|---|
| ⏸ Pausar / ▶ Reanudar | Pause/resume the running simulation |
| ⟳ Reiniciar | Restart the current scenario from the beginning |
| **Escenario** | Traffic level: **Bajo / Medio / Alto** (reconnects to that scenario) |
| Velocidad | Playback speed, 1–30 fps (WebSocket frame cap) |
| Edificios 3D | Toggle extruded buildings |
| Congestión (LOS) | Toggle the per-edge congestion colouring |
| Semáforos | Toggle the traffic-light layer |
| Stats / legend | Live vehicle count, sim time `t`, connection state, LOS A–F ramp |

**Toolbar (top-centre)**

| Button | Action |
|---|---|
| `+` / `−` | Zoom in / out |
| ↺ / N / ↻ | Rotate left · orient north · rotate right |
| ◐ | Toggle automatic orbit |
| 2D / 3D | Toggle bird's-eye (top-down) ⇄ 3D perspective |

---

## Scenarios & traffic levels

Two scenario families ship in the repo:

| Family | Dir | Network | Buildings | Notes |
|---|---|---|---|---|
| **Metro** (default) | `mapa/` | Metropolitan Cuenca — `network.net.xml` (~93 MB, ~34k edges, ~26×19 km, **1038 signals**) | ✗ none | `network.net.xml` is git-ignored; large but city-wide |
| **Centro** | `sumo/` | `cuenca.net.xml` (~0.6 MB) | ✓ `cuenca.poly.xml` | Lighter, has 3D buildings; committed to git |

The **Bajo / Medio / Alto** selector maps to per-level SUMO configs via
`APP_SUMO_CONFIG_TEMPLATE` (`{level}` → `low`/`mid`/`high`):

- Metro: `mapa/metro_low.sumocfg` · `metro_mid.sumocfg` · `metro_high.sumocfg`
- Centro: `sumo/cuenca_low.sumocfg` · `cuenca_mid.sumocfg` · `cuenca_high.sumocfg`

Each level shares one network and one `vtypes.add.xml` + `transit.add.xml`; only
the route file changes. Levels **open at 07:00** (`APP_SIM_BEGIN=25200`) so traffic
is already flowing when you connect, and run through the 24 h day.

**Switching the default scenario.** Edit `docker-compose.yml` (backend service):

```yaml
# Metro (default)
APP_SUMO_CONFIG: /mapa/metro_mid.sumocfg
APP_SUMO_CONFIG_TEMPLATE: /mapa/metro_{level}.sumocfg

# Cuenca-centro (lighter, with 3D buildings)
# APP_SUMO_CONFIG: /sumo/cuenca_mid.sumocfg
# APP_SUMO_CONFIG_TEMPLATE: /sumo/cuenca_{level}.sumocfg
```

Then `docker compose up -d --build` and hard-refresh the browser (Cmd/Ctrl + Shift + R).

---

## Architecture

| Layer | Tech | Responsibility |
|---|---|---|
| Simulation | **SUMO** + TraCI/libsumo | Microscopic mobility model |
| Backend | **FastAPI** (`backend/app`) | Steps the sim, converts network & polygons to GeoJSON, derives traffic-light geometry, estimates per-edge congestion, streams frames over WebSocket |
| Frontend | **MapLibre GL JS + deck.gl** (`frontend`) | Basemap (OpenFreeMap `liberty`), `fill-extrusion` buildings, deck.gl road / vehicle / signal layers |
| Delivery | **nginx** | Serves the SPA and reverse-proxies `/api` + `/ws` (same-origin, no CORS) |

### Backend modules

- `config.py` — env-driven settings (`APP_*`), including the scenario template and
  the 07:00 open time.
- `geo.py` — network → GeoJSON edges, polygons → extrudable buildings,
  **traffic-light positions** (one per approach, mast-arm vs. straight pole by
  building proximity), and coordinate conversion. Projected (OSM) nets use SUMO's
  own projection; synthetic nets use the `APP_ORIGIN_LON/LAT` ENU anchor.
- `sumo_bridge.py` — TraCI/libsumo lifecycle with a **unique connection label per
  WebSocket** (so reconnects and multiple viewers don't collide), per-step vehicles
  and **live signal state**, `managed` vs. `remote` mode, optional `--begin`.
- `traffic.py` — density (veh/km/lane) → HCM-style LOS + colour ramp.
- `main.py` — REST geometry + `WS /ws/live` streaming; reads the `?level=` query
  param to pick the scenario.

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness + active SUMO mode |
| `GET /api/meta` | Map centre, bounds, origin, step length |
| `GET /api/network` | Road edges as GeoJSON (cached) |
| `GET /api/buildings` | Building polygons as GeoJSON with height (cached) |
| `GET /api/trafficlights` | Signal positions + mast/pole flag + approach bearing |
| `WS /ws/live?level=low\|mid\|high` | Per-step frames: vehicles + per-edge LOS + signal state |

WebSocket control messages (client → server): `{"cmd":"pause"}` / `{"cmd":"play"}`
/ `{"cmd":"speed","fps":20}`.

---

## Traffic lights

Everything the viewer draws — road network, buildings **and traffic lights** — is
derived automatically from the SUMO scenario when the backend starts. **There is no
manual step to "generate" the traffic lights:** the backend reads them from the
network (`net.getTrafficLights()`), computes one signal per traffic light and
incoming approach at its stop line, decides mast-arm vs. straight pole by building
proximity, and streams the **live signal colours** from SUMO over TraCI.

So changing the map needs no signal work — just point the backend at the new
`.sumocfg` and restart. Two requirements for the new network:

- **Traffic lights must exist in the `.net.xml`.** OSM often lacks them, so build
  with guessed signals (`netconvert … --tls.guess`). `scripts/build_city.sh`
  already does this. No TLS → no signals shown (not an error).
- **Use the matching `.poly.xml`** — buildings drive both the 3D extrusion and the
  mast-arm vs. pole choice (a junction with no building within ~9 m gets a
  mast-arm). Tune the threshold via the `clearance` argument of
  `trafficlights_geojson` in `backend/app/geo.py`.

---

## Generating traffic demand

`scripts/gen_traffic.py` writes 24 h demand with a realistic **diurnal profile**
(nightly valley, AM & PM rush peaks), sampling origins/destinations across the
network and letting SUMO route on the fly (no slow `duarouter` pass). It emits a
`<trip>` route file **and** a ready-to-run `.sumocfg` for each level.

```bash
python scripts/gen_traffic.py \
  --net mapa/network.net.xml --types mapa/vtypes.add.xml --transit mapa/transit.add.xml \
  --outdir mapa --prefix metro --low 3000 --mid 8000 --high 18000 \
  --bus-share 0.05 --end 86400 --seed 42
```

- **Car fleet** — `vtypes.add.xml`: real passenger-car models (Chevrolet, Toyota,
  Mitsubishi, Kia, Hyundai, …), `vClass="evehicle"`.
- **Buses** — `transit.add.xml` (`vClass="bus"`); a share of trips (`--bus-share`)
  are buses. The shipped scenarios are **cars + buses (no tram)**.
- **Tram (optional)** — the script can also add a fixed cross-town tram corridor
  (`--tram-period`); left disabled in the shipped scenarios.

---

## Building a real city from OSM (roads + buildings)

Import a **projected** OSM scenario so buildings, roads and vehicles line up exactly
with the map; the backend georeferences it with SUMO's own `convertXY2LonLat`
(needs `pyproj`).

```bash
export SUMO_HOME=/path/to/sumo          # or install: pip install eclipse-sumo sumolib
./scripts/build_city.sh "-79.015,-2.912,-78.995,-2.892" cuenca
# bbox = minLon,minLat,maxLon,maxLat   (this one covers Cuenca's centro)
```

That downloads OSM, runs `netconvert` (roads, with `--tls.guess-signals`) +
`polyconvert` (building footprints), enriches building heights from OSM tags
(`scripts/enrich_heights.py`), generates demand, and writes `sumo/cuenca.sumocfg`.
Point the backend at it in `docker-compose.yml` and rebuild.

`scripts/generate_scenario.sh` regenerates the tiny synthetic **grid** demo instead
(useful offline; no internet needed).

> Building footprints can also be prepared/cleaned in **GeoLibre** (OSM PBF or
> Overture Maps → GeoJSON) — it shares this exact MapLibre + deck.gl runtime.

---

## Connecting to *your* SUMO container (remote mode)

The default `managed` mode makes the backend self-contained (it launches SUMO). To
reuse an existing SUMO Docker container, start it as a TraCI server and switch:

```bash
# in your SUMO container
sumo -c /sumo/cuenca.sumocfg --remote-port 8813 --step-length 1.0 --start
```

```yaml
# docker-compose.yml (backend service)
environment:
  APP_SUMO_MODE: remote
  APP_SUMO_HOST: sumo
  APP_SUMO_PORT: "8813"
```

(An optional `sumo` service is stubbed in `docker-compose.yml`.)

---

## Run without Docker (dev)

```bash
# backend (installs SUMO via the eclipse-sumo wheel)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
APP_SUMO_CONFIG=../sumo/cuenca_mid.sumocfg uvicorn app.main:app --reload

# frontend (any static server; the API needs the backend on :8000)
cd ../frontend && python -m http.server 8081
```

For the no-Docker frontend, either serve it behind the provided nginx proxy or
point `API`/`WS_URL` in `frontend/app.js` at `http://localhost:8000`.

---

## Configuration reference (`APP_*` env vars)

| Variable | Default | Meaning |
|---|---|---|
| `APP_SUMO_MODE` | `managed` | `managed` (launch SUMO) or `remote` (connect to a TraCI server) |
| `APP_SUMO_BINARY` | `sumo` | `sumo` or `sumo-gui` |
| `APP_SUMO_CONFIG` | `/sumo/demo.sumocfg` | Base scenario used to build the static geometry |
| `APP_SUMO_CONFIG_TEMPLATE` | `/mapa/metro_{level}.sumocfg` | Per-level scenario; `{level}` = the selector value |
| `APP_SIM_BEGIN` | `25200` | Simulation start time in seconds (07:00) for level runs |
| `APP_SUMO_HOST` / `APP_SUMO_PORT` | `sumo` / `8813` | TraCI server (remote mode) |
| `APP_USE_LIBSUMO` | `false` | Use in-process libsumo if available |
| `APP_STEP_LENGTH` | `1.0` | Seconds per simulation step |
| `APP_NET_FILE` / `APP_POLY_FILE` | — | Explicit overrides (otherwise parsed from the `.sumocfg`) |
| `APP_ORIGIN_LON` / `APP_ORIGIN_LAT` | `-79.0045` / `-2.9006` | ENU anchor for synthetic (unprojected) nets |
| `APP_MAX_FPS` | `10` | WebSocket frame-rate cap |
| `APP_CORS_ORIGINS` | `*` | Comma-separated origins or `*` |

---

## Repository layout

```
backend/            FastAPI app (config, geo, sumo_bridge, traffic, main) + Dockerfile
frontend/           MapLibre + deck.gl SPA (index.html, app.js, style.css) + nginx.conf
mapa/               Metro scenario: network.net.xml*, metro_{low,mid,high}.{rou.xml,sumocfg},
                    vtypes.add.xml, transit.add.xml       (*net.xml git-ignored — regenerate)
sumo/               Centro + demo scenarios: cuenca.net.xml, cuenca.poly.xml,
                    cuenca_{low,mid,high}.*, grid.net.xml, buildings.poly.xml, demo.sumocfg
scripts/            gen_traffic.py (24 h demand) · build_city.sh (OSM import) ·
                    enrich_heights.py · generate_scenario.sh (grid demo)
docs/               Evaluacion_Herramientas_3D_SUMO.docx · Matriz_Comparativa_Herramientas.xlsx
docker-compose.yml  managed-mode stack (backend + frontend on :8081)
ROADMAP.md          phased technical plan
```

---

## Tool evaluation

`docs/` contains the deliverables comparing the 3D options (Unity/SUMO2Unity,
Unreal/Sumo2Unreal, Blender/BlenderGIS, Godot, sumo3Dviz, and the web-native
MapLibre+deck.gl / CesiumJS / three.js paths) and GeoLibre, scored on scalability,
realism, SUMO interface and **browser viability**:

- `docs/Evaluacion_Herramientas_3D_SUMO.docx` — full evaluation report.
- `docs/Matriz_Comparativa_Herramientas.xlsx` — weighted scoring matrix.
- `ROADMAP.md` — phased technical plan.

**Bottom line for a browser-only prototype:** MapLibre GL JS + deck.gl (GeoLibre
stack) is the recommended runtime; game engines (Unity/Unreal/Godot) and Blender
don't meet the "no-install, in-browser" constraint at city scale; sumo3Dviz is
ideal for offline cinematic renders, not interactive control.

---

## Notes & known limitations

- The **metro** network is city-wide (~93 MB, ~34k edges) and has **no building
  footprints** — heavier to load and shows roads/vehicles/signals over the basemap
  without 3D buildings. For 3D buildings use the **centro** scenario.
- `mapa/network.net.xml` is git-ignored; regenerate it with `build_city.sh` or use
  the committed centro network.
- 3D vehicle meshes (glTF) don't render reliably in the deck.gl script-tag bundle,
  so vehicles are drawn as coloured markers (fast and robust).

## License

Code MIT. SUMO is EPL-2.0; MapLibre GL JS and deck.gl are BSD-3/MIT.
