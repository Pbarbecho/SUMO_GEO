/* SUMO-GEO frontend — MapLibre GL (basemap + extruded buildings) with a
 * deck.gl overlay for road-congestion colouring and live vehicles streamed
 * over a WebSocket. Everything runs in the browser, no build step. */

const API = "";                                   // same-origin (nginx proxies /api and /ws)
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
  + location.host + "/ws/live";

const els = {
  play: document.getElementById("btn-play"),
  reset: document.getElementById("btn-reset"),
  fps: document.getElementById("fps"),
  fpsVal: document.getElementById("fps-val"),
  buildings: document.getElementById("toggle-buildings"),
  congestion: document.getElementById("toggle-congestion"),
  rotLeft: document.getElementById("rot-left"),
  rotRight: document.getElementById("rot-right"),
  rotNorth: document.getElementById("rot-north"),
  orbit: document.getElementById("orbit"),
  viewTop: document.getElementById("view-top"),
  zoomIn: document.getElementById("zoom-in"),
  zoomOut: document.getElementById("zoom-out"),
  vehCount: document.getElementById("veh-count"),
  simTime: document.getElementById("sim-time"),
  conn: document.getElementById("conn"),
};

let map, overlay, ws;
let networkGeo = { type: "FeatureCollection", features: [] };
let edgeColors = {};      // edgeId -> [r,g,b]
let vehicles = [];        // latest frame vehicles
let paused = false;
let showCongestion = true;

// speed (m/s) -> colour ramp (red slow → green fast)
function speedColor(v) {
  const t = Math.max(0, Math.min(1, v / 14));     // ~50 km/h reference
  return [Math.round(230 * (1 - t)) + 20, Math.round(200 * t) + 40, 70];
}
function hexToRgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

async function fetchJSON(path, { tries = 20, delay = 2000 } = {}) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(API + path);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) {
      els.conn.textContent = `esperando al backend… (intento ${i + 1})`;
      els.conn.className = "offline";
      await new Promise((res) => setTimeout(res, delay));
    }
  }
  throw new Error("backend no disponible tras varios intentos");
}

async function boot() {
  const meta = await fetchJSON("/api/meta");

  map = new maplibregl.Map({
    container: "map",
    style: "https://tiles.openfreemap.org/styles/liberty",
    center: meta.center,
    zoom: 15.5,
    pitch: 55,
    bearing: -18,
    antialias: true,
  });
  map.on("dragstart", stopOrbit);       // any manual pan/rotate cancels auto-orbit
  map.on("rotatestart", stopOrbit);
  map.on("moveend", syncFromPitch);     // keep the 2D/3D label in sync with gestures

  map.on("load", async () => {
    // --- extruded building polygons (native MapLibre fill-extrusion) --------
    const buildings = await fetchJSON("/api/buildings");
    map.addSource("buildings", { type: "geojson", data: buildings });
    map.addLayer({
      id: "buildings-3d",
      type: "fill-extrusion",
      source: "buildings",
      paint: {
        "fill-extrusion-color": [
          "interpolate", ["linear"], ["get", "height"],
          0, "#7f8fa6", 20, "#9aa7b8", 45, "#c3ccd6",
        ],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-opacity": 0.85,
      },
    });

    // --- road network (deck.gl overlay, recoloured by congestion) ----------
    networkGeo = await fetchJSON("/api/network");
    overlay = new deck.MapboxOverlay({ interleaved: true, layers: buildLayers() });
    map.addControl(overlay);

    connect();
  });
}

function buildLayers() {
  return [
    new deck.GeoJsonLayer({
      id: "network",
      data: networkGeo,
      lineWidthUnits: "meters",
      getLineWidth: 3,
      lineWidthMinPixels: 1.5,
      getLineColor: (f) =>
        (showCongestion && edgeColors[f.properties.id]) || [120, 130, 145],
      updateTriggers: { getLineColor: [edgeColors, showCongestion] },
      pickable: false,
    }),
    new deck.ScatterplotLayer({
      id: "vehicles",
      data: vehicles,
      getPosition: (d) => [d.lon, d.lat],
      getFillColor: (d) => speedColor(d.speed),
      getRadius: 5,
      radiusUnits: "meters",
      radiusMinPixels: 2.5,
      stroked: true,
      getLineColor: [10, 12, 18],
      lineWidthMinPixels: 0.5,
      pickable: true,
    }),
  ];
}

function refreshLayers() {
  if (overlay) overlay.setProps({ layers: buildLayers() });
}

// --- WebSocket live stream --------------------------------------------------
let manualClose = false;
function connect() {
  manualClose = false;
  ws = new WebSocket(WS_URL);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); if (!manualClose) setTimeout(connect, 2000); };
  ws.onerror = () => setConn(false);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "frame") {
      vehicles = msg.vehicles;
      edgeColors = {};
      for (const e of msg.edges) edgeColors[e.id] = hexToRgb(e.color);
      els.vehCount.textContent = vehicles.length;
      els.simTime.textContent = msg.t;
      refreshLayers();
    } else if (msg.type === "end") {
      els.conn.textContent = "simulación finalizada";
    } else if (msg.type === "error") {
      els.conn.textContent = msg.message;
    }
  };
}
function setConn(on) {
  els.conn.textContent = on ? "en vivo" : "desconectado";
  els.conn.className = on ? "online" : "offline";
}

// --- controls ---------------------------------------------------------------
els.play.onclick = () => {
  paused = !paused;
  els.play.textContent = paused ? "▶ Reanudar" : "⏸ Pausar";
  send({ cmd: paused ? "pause" : "play" });
};
els.reset.onclick = () => {
  if (ws) { manualClose = true; ws.close(); }
  vehicles = []; edgeColors = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  connect();
};
els.fps.oninput = () => {
  els.fpsVal.textContent = `${els.fps.value} fps`;
  send({ cmd: "speed", fps: Number(els.fps.value) });
};
els.buildings.onchange = () => {
  if (map.getLayer("buildings-3d"))
    map.setLayoutProperty("buildings-3d", "visibility", els.buildings.checked ? "visible" : "none");
};
els.congestion.onchange = () => { showCongestion = els.congestion.checked; refreshLayers(); };

// --- map rotation ---------------------------------------------------------
els.rotLeft.onclick = () => map && map.easeTo({ bearing: map.getBearing() - 30, duration: 300 });
els.rotRight.onclick = () => map && map.easeTo({ bearing: map.getBearing() + 30, duration: 300 });
els.rotNorth.onclick = () => map && map.easeTo({ bearing: 0, duration: 400 });

let orbitRAF = null;
function stopOrbit() {
  if (orbitRAF) { cancelAnimationFrame(orbitRAF); orbitRAF = null; els.orbit.classList.remove("active"); }
}
els.orbit.onclick = () => {
  if (!map) return;
  if (orbitRAF) { stopOrbit(); return; }        // toggle off
  els.orbit.classList.add("active");            // start a slow continuous orbit
  const spin = () => { map.setBearing(map.getBearing() + 0.15); orbitRAF = requestAnimationFrame(spin); };
  orbitRAF = requestAnimationFrame(spin);
};

// --- zoom -----------------------------------------------------------------
els.zoomIn.onclick = () => map && map.zoomIn();
els.zoomOut.onclick = () => map && map.zoomOut();

// --- top-down (bird's-eye) view toggle ------------------------------------
let topView = false;                              // false = 3D perspective, true = looking straight down
function setViewLabel(top) {
  topView = top;
  els.viewTop.textContent = top ? "3D" : "2D";    // label = the view the next click gives you
  els.viewTop.classList.toggle("active", top);
}
els.viewTop.onclick = () => {
  if (!map) return;
  const goTop = !topView;
  map.easeTo({ pitch: goTop ? 0 : 55, duration: 500 });
  setViewLabel(goTop);                            // optimistic; moveend confirms from real pitch
};
function syncFromPitch() { if (map) setViewLabel(map.getPitch() <= 5); }

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

boot().catch((e) => { els.conn.textContent = "error: " + e.message; });
